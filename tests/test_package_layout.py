from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_agent import entrypoint
from local_agent.daemon import service
from local_agent.paths import repository_root
from local_agent.repository.context import RepositoryContext
from local_agent.supervisor import orchestrator, serial

ROOT = Path(__file__).resolve().parents[1]
LAUNCHERS = {
    "agentd.py": ("local_agent.daemon.launcher", "run"),
    "agent_entrypoint.py": ("local_agent.entrypoint", "main"),
    "agent_parallel.py": ("local_agent.supervisor.orchestrator", "main"),
    "agent_multirepo.py": ("local_agent.supervisor.serial", "main"),
}


class PackageLayoutTests(unittest.TestCase):
    def test_only_approved_operational_launchers_remain_at_root(self) -> None:
        self.assertEqual({path.name for path in ROOT.glob("*.py")}, set(LAUNCHERS))
        for filename, (module, function) in LAUNCHERS.items():
            with self.subTest(filename=filename):
                source = (ROOT / filename).read_text(encoding="utf-8")
                self.assertLessEqual(len(source.splitlines()), 8)
                tree = ast.parse(source)
                self.assertEqual(len(tree.body), 3)
                self.assertIsInstance(tree.body[0], ast.Expr)
                self.assertIsInstance(tree.body[1], ast.ImportFrom)
                self.assertEqual(tree.body[1].module, module)
                self.assertEqual([name.name for name in tree.body[1].names], [function])
                self.assertIsInstance(tree.body[2], ast.If)
                self.assertNotIn("sys.modules", source)
                self.assertNotIn("__file__", source)

    def test_flat_platform_product_homes(self) -> None:
        import local_agent

        for system in ("macos", "linux", "windows"):
            self.assertTrue((ROOT / "platforms" / system / "local-agent").is_dir())
            self.assertFalse((ROOT / "platforms" / system / "agent").exists())
        mac = ROOT / "platforms/macos/local-agent"
        self.assertEqual(Path(entrypoint.__file__).resolve(), mac / "entrypoint.py")
        self.assertEqual(list(map(Path, local_agent.__path__)), [mac])

    def test_sources_never_import_legacy_root_modules(self) -> None:
        for folder in ("local_agent", "platforms/macos/local-agent", "tests", "scripts"):
            for path in (ROOT / folder).rglob("*.py"):
                with self.subTest(path=str(path.relative_to(ROOT))):
                    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                        modules = []
                        if isinstance(node, ast.Import):
                            modules = [alias.name for alias in node.names]
                        elif isinstance(node, ast.ImportFrom) and node.module:
                            modules = [node.module]
                        elif isinstance(node, ast.Call) and node.args:
                            if isinstance(node.func, ast.Name) and node.func.id == "__import__":
                                if isinstance(node.args[0], ast.Constant):
                                    modules = [node.args[0].value]
                        for module in modules:
                            name = module.split(".")[0]
                            self.assertFalse(
                                name.startswith("agent_") or name in {"agentd", "agentctl"}
                            )

    def test_paths_are_checkout_scoped_and_independent_of_cwd(self) -> None:
        self.assertEqual(repository_root(), ROOT)
        self.assertEqual(service.SELF_REPO, ROOT)
        self.assertEqual(service.DAEMON_LOCK_PATH.name, "agentd.lock")
        self.assertEqual(entrypoint.REPO_ROOT, ROOT)
        with tempfile.TemporaryDirectory(prefix="local agent paths ") as tmp:
            link = Path(tmp) / "checkout link"
            link.symlink_to(ROOT, target_is_directory=True)
            for filename in LAUNCHERS:
                if filename == "agentd.py":
                    # The daemon has no help CLI; exercise its real launcher with
                    # only the fail-closed launcher patched to avoid starting a daemon.
                    code = (
                        "import runpy; from unittest.mock import patch; "
                        "from local_agent.daemon import launcher, service; "
                        f"assert str(service.SELF_REPO) == {str(ROOT)!r}; "
                        "p=patch.object(launcher, 'run', return_value=None); p.start(); "
                        f"runpy.run_path({str(link / filename)!r}, run_name='__main__')"
                    )
                    command = [sys.executable, "-c", code]
                    env = dict(os.environ, PYTHONPATH=str(link))
                else:
                    command = [sys.executable, str(link / filename), "--help"]
                    env = None
                result = subprocess.run(
                    command, cwd=tmp, env=env, capture_output=True, text=True, timeout=10
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("RuntimeWarning", result.stderr)

    def test_packaged_workers_and_supervisors_execute(self) -> None:
        repository = RepositoryContext(
            repository_id="test",
            repository="owner/test",
            control=Path("/tmp/control"),
            work=Path("/tmp/work"),
            checkpoints=Path("/tmp/checkpoints"),
        )
        for supervisor in (orchestrator, serial):
            command = supervisor.worker_command(
                repository, registry_path=Path("/tmp/registry.json")
            )
            self.assertEqual(command[1], "-m")
            result = subprocess.run(
                command + ["--help"], cwd=ROOT, capture_output=True, text=True, timeout=10
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--expected-config-digest", result.stdout)
        for module in (
            "local_agent.supervisor.orchestrator",
            "local_agent.supervisor.serial",
            "local_agent.entrypoint",
            "local_agent.operator.local",
            "local_agent.repository.admin",
            "local_agent.cli.diagnostics",
        ):
            result = subprocess.run(
                [sys.executable, "-m", module, "--help"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("usage:", result.stdout)
            self.assertNotIn("RuntimeWarning", result.stderr)

    def test_daemon_restart_uses_installed_launcher(self) -> None:
        with (
            mock.patch.object(service, "publish_daemon_status"),
            mock.patch.object(service, "log"),
            mock.patch.object(service.os, "execv") as execute,
        ):
            service.restart_self("layout-test")
        execute.assert_called_once_with(sys.executable, [sys.executable, str(ROOT / "agentd.py")])

    def test_parallel_restart_preserves_mode_and_options(self) -> None:
        with (
            mock.patch.object(service, "publish_daemon_status"),
            mock.patch.object(orchestrator, "log"),
            mock.patch.object(orchestrator.os, "execv") as execute,
        ):
            orchestrator.restart_parallel_supervisor(
                "layout-test",
                registry_path=Path("/tmp/custom registry.json"),
                max_workers=2,
                once=True,
            )
        execute.assert_called_once_with(
            sys.executable,
            [
                sys.executable,
                str(ROOT / "agent_parallel.py"),
                "--registry",
                "/tmp/custom registry.json",
                "--max-workers",
                "2",
                "--once",
            ],
        )


if __name__ == "__main__":
    unittest.main()
