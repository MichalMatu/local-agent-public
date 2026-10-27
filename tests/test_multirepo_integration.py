from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from local_agent.repository.worker import MULTIREPO_DAEMON_VERSION
from local_agent.repository.context import load_repository_registry, repository_config_digest


REPO_ROOT = Path(__file__).resolve().parents[1]


def git(args: list[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed:\n{result.stdout}")
    return result.stdout


def configure_identity(path: Path) -> None:
    git(["config", "user.name", "local-agent-tests"], cwd=path)
    git(["config", "user.email", "local-agent-tests@example.invalid"], cwd=path)


def write_test_catalog(root: Path, repository_id: str, repository: str, agent_binding: str) -> Path:
    catalog = root / "agent_bindings.json"
    payload = {"version": 1, "agents": []}
    if catalog.exists():
        payload = json.loads(catalog.read_text(encoding="utf-8"))
    payload["agents"] = [
        item for item in payload["agents"]
        if str(item.get("id", "")).casefold() != repository_id.casefold()
    ]
    payload["agents"].append(
        {
            "id": repository_id,
            "repository": repository,
            "agent_binding": agent_binding,
            "execution_enabled": True,
        }
    )
    catalog.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return catalog


def create_repository_fixture(root: Path, repository_id: str) -> dict[str, Path | str]:
    repository = f"test/{repository_id}"
    agent_binding = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"local-agent-integration:{repository}")
    )
    write_test_catalog(root, repository_id, repository, agent_binding)
    remote = root / f"{repository_id}.git"
    seed = root / f"{repository_id}-seed"
    control = root / f"{repository_id}-control"
    work = root / f"{repository_id}-work"
    checkpoints = root / f"{repository_id}-checkpoints"

    git(["init", "--bare", str(remote)])
    git(["init", str(seed)])
    configure_identity(seed)
    (seed / "README.md").write_text(f"# {repository_id}\n", encoding="utf-8")
    git(["add", "README.md"], cwd=seed)
    git(["commit", "-m", "Initial main"], cwd=seed)
    git(["branch", "-M", "main"], cwd=seed)
    git(["remote", "add", "origin", str(remote)], cwd=seed)
    git(["push", "-u", "origin", "main"], cwd=seed)

    git(["checkout", "--orphan", "agent-control"], cwd=seed)
    git(["rm", "-rf", "."], cwd=seed)
    for directory in (
        ".agent/tasks",
        ".agent/results",
        ".agent/runs",
        ".agent/status",
        ".agent/daemon/acks",
    ):
        target = seed / directory
        target.mkdir(parents=True, exist_ok=True)
        (target / ".gitkeep").write_text("", encoding="utf-8")
    (seed / ".agent" / "binding.json").write_text(
        json.dumps(
            {
                "version": 1,
                "repository_id": repository_id,
                "repository": repository,
                "agent_binding": agent_binding,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    git(["add", ".agent"], cwd=seed)
    git(["commit", "-m", "Initialize bound agent control"], cwd=seed)
    git(["push", "-u", "origin", "agent-control"], cwd=seed)

    git(["clone", "--branch", "agent-control", str(remote), str(control)])
    git(["clone", "--branch", "main", str(remote), str(work)])
    configure_identity(control)
    configure_identity(work)

    task = {
        "id": "shared-task-id",
        "agent_binding": agent_binding,
        "mode": "commands",
        "work_branch": "main",
        "allow_write": False,
        "resources": [],
        "command_timeout": 30,
        "idle_timeout": 10,
        "task_timeout": 120,
        "memory_limit_mb": 256,
        "steps": [
            {
                "name": "identity",
                "command": f"printf '{repository_id}-ok\\n'",
                "timeout": 30,
            }
        ],
    }
    task_path = control / ".agent" / "tasks" / "shared-task-id.json"
    task_path.write_text(json.dumps(task, indent=2) + "\n", encoding="utf-8")
    git(["add", ".agent/tasks/shared-task-id.json"], cwd=control)
    git(["commit", "-m", f"Queue {repository_id} smoke task"], cwd=control)
    git(["push", "origin", "agent-control"], cwd=control)

    return {
        "id": repository_id,
        "repository": repository,
        "agent_binding": agent_binding,
        "control": control,
        "work": work,
        "checkpoints": checkpoints,
    }


def write_registry(root: Path, repositories: tuple[dict[str, Path | str], ...]) -> Path:
    registry = root / "repositories.json"
    registry.write_text(
        json.dumps(
            {
                "version": 1,
                "repositories": [
                    {
                        "id": item["id"],
                        "repository": item["repository"],
                        "agent_binding": item["agent_binding"],
                        "control_dir": str(item["control"]),
                        "work_dir": str(item["work"]),
                        "checkpoints_dir": str(item["checkpoints"]),
                    }
                    for item in repositories
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return registry


def test_environment(root: Path) -> tuple[Path, dict[str, str]]:
    home = root / "home"
    home.mkdir()
    hook = root / "sitecustomize.py"
    hook.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "import local_agent.repository.binding as binding\n"
        "catalog = os.environ.get('LOCAL_AGENT_TEST_CATALOG')\n"
        "if catalog:\n"
        "    binding.DEFAULT_CATALOG_PATH = Path(catalog)\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    # Disposable fixture daemons use isolated HOME/state; they must not inherit
    # the running Mac service's mandatory admission policy.
    # Explicit admission tests enroll their own independent fixture gate.
    for runtime_key in (
        "LOCAL_AGENT_MACOS_ADMISSION_REQUIRED",
        "LOCAL_AGENT_LEASE_FDS",
        "LOCAL_AGENT_LEASE_KEYS_DIGEST",
        "LOCAL_AGENT_RESOURCE_LEASE_FDS",
    ):
        env.pop(runtime_key, None)
    env["HOME"] = str(home)
    env["LOCAL_AGENT_TEST_CATALOG"] = str(root / "agent_bindings.json")
    env["PYTHONPATH"] = os.pathsep.join((str(root), str(REPO_ROOT)))
    return home, env


def assert_repository_result(
    case: unittest.TestCase,
    item: dict[str, Path | str],
    home: Path,
) -> dict:
    result_path = Path(item["control"]) / ".agent" / "results" / "shared-task-id.json"
    case.assertTrue(result_path.exists())
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    case.assertEqual(payload["status"], "done")
    case.assertEqual(payload["daemon_version"], MULTIREPO_DAEMON_VERSION)
    case.assertIn(f"{item['id']}-ok", payload["commands"][0]["output"])

    claim_root = (
        home
        / "Library"
        / "Application Support"
        / "local-agent"
        / "repositories"
        / str(item["id"])
        / "claims"
    )
    case.assertEqual(list(claim_root.glob("*.json")), [])
    case.assertEqual(git(["status", "--porcelain"], cwd=Path(item["work"])), "")
    return payload


def queue_equivalent_duplicate(
    root: Path,
    item: dict[str, Path | str],
) -> Path:
    control = Path(item["control"])
    original_path = control / ".agent" / "tasks" / "shared-task-id.json"
    original = json.loads(original_path.read_text(encoding="utf-8"))
    counter = root / "serial-dedupe-counter.txt"
    command = (
        f"printf 'run\\n' >> {str(counter)!r}; "
        f"printf '{item['id']}-ok\\n'"
    )
    original["steps"][0]["command"] = command
    original_path.write_text(json.dumps(original, indent=2) + "\n", encoding="utf-8")

    duplicate = json.loads(json.dumps(original))
    duplicate["id"] = "zz-duplicate-task-id"
    duplicate_path = control / ".agent" / "tasks" / "zz-duplicate-task-id.json"
    duplicate_path.write_text(json.dumps(duplicate, indent=2) + "\n", encoding="utf-8")

    git(
        [
            "add",
            ".agent/tasks/shared-task-id.json",
            ".agent/tasks/zz-duplicate-task-id.json",
        ],
        cwd=control,
    )
    git(["commit", "-m", "Queue equivalent duplicate tasks"], cwd=control)
    git(["push", "origin", "agent-control"], cwd=control)
    return counter


class MultiRepositoryIntegrationTests(unittest.TestCase):
    def test_broken_first_control_mailbox_does_not_block_other_repository_at_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = create_repository_fixture(root, "project-a")
            second = create_repository_fixture(root, "project-b")
            registry = write_registry(root, (first, second))
            home, env = test_environment(root)
            interrupted = Path(first["control"]) / ".git/rebase-merge"
            interrupted.mkdir()
            (interrupted / "orig-head").write_text("a" * 40 + "\n")
            result = subprocess.run(
                [sys.executable, str(REPO_ROOT / "agent_parallel.py"),
                 "--registry", str(registry), "--max-workers", "2", "--once"],
                cwd=REPO_ROOT, env=env, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, timeout=60, check=False,
            )
            self.assertNotEqual(result.returncode, 0, result.stdout)
            assert_repository_result(self, second, home)
            self.assertIn("continuing unrelated task admission", result.stdout)
            self.assertTrue(interrupted.exists())
            self.assertFalse((Path(first["control"]) / ".agent/results/shared-task-id.json").exists())

    def test_two_repository_workers_with_same_task_id_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = create_repository_fixture(root, "project-a")
            second = create_repository_fixture(root, "project-b")
            registry = write_registry(root, (first, second))
            home, env = test_environment(root)
            repositories = {
                repository.repository_id: repository
                for repository in load_repository_registry(home=home, path=registry)
            }

            for item in (first, second):
                repository_id = str(item["id"])
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m", "local_agent.repository.worker",
                        "--repository-id",
                        repository_id,
                        "--expected-config-digest",
                        repository_config_digest(repositories[repository_id]),
                        "--registry",
                        str(registry),
                    ],
                    cwd=REPO_ROOT,
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=60,
                    check=False,
                )
                self.assertEqual(result.returncode, 10, result.stdout)

            first_result = assert_repository_result(self, first, home)
            second_result = assert_repository_result(self, second, home)
            self.assertNotEqual(
                first_result["commands"][0]["output"],
                second_result["commands"][0]["output"],
            )

    def test_supervisor_processes_two_repositories_across_serial_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = create_repository_fixture(root, "project-a")
            second = create_repository_fixture(root, "project-b")
            registry = write_registry(root, (first, second))
            home, env = test_environment(root)

            for _ in range(2):
                result = subprocess.run(
                    [
                        sys.executable,
                        str(REPO_ROOT / "agent_multirepo.py"),
                        "--registry",
                        str(registry),
                        "--once",
                    ],
                    cwd=REPO_ROOT,
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=90,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout)

            first_result = assert_repository_result(self, first, home)
            second_result = assert_repository_result(self, second, home)
            self.assertNotEqual(
                first_result["commands"][0]["output"],
                second_result["commands"][0]["output"],
            )

    def test_serial_supervisor_uses_parallel_dedupe_admission_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item = create_repository_fixture(root, "project-a")
            counter = queue_equivalent_duplicate(root, item)
            registry = write_registry(root, (item,))
            _home, env = test_environment(root)

            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "agent_multirepo.py"),
                    "--registry",
                    str(registry),
                    "--once",
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=90,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(counter.read_text(encoding="utf-8").splitlines(), ["run"])

            control = Path(item["control"])
            executed = json.loads(
                (control / ".agent" / "results" / "shared-task-id.json").read_text(
                    encoding="utf-8"
                )
            )
            suppressed = json.loads(
                (control / ".agent" / "results" / "zz-duplicate-task-id.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(executed["status"], "done")
            self.assertEqual(suppressed["status"], "done")
            self.assertEqual(suppressed["outcome"], "duplicate_task_suppressed")
            self.assertEqual(suppressed["duplicate_of"], "shared-task-id")
            self.assertEqual(suppressed["duplicate_reason"], "queued_duplicate")


    def test_git_published_attached_task_preserves_owner_checkout(self) -> None:
        """One disposable Git transport, real subprocess worker, and retained output."""
        from local_agent.repository.snapshot_review import inspect_attached_snapshots

        with tempfile.TemporaryDirectory(prefix="la-attached-git-e2e-") as tmp:
            root = Path(tmp)
            item = create_repository_fixture(root, "project-attached")
            registry = write_registry(root, (item,))
            data = json.loads(registry.read_text(encoding="utf-8"))
            data["repositories"][0]["work_mode"] = "attached"
            registry.write_text(json.dumps(data) + "\n", encoding="utf-8")
            home, env = test_environment(root)
            work = Path(item["work"])
            control = Path(item["control"])
            remote = root / "project-attached.git"

            # Preserve exactly the owner's branch, index, staged/unstaged,
            # and untracked files across a remote Git-controlled task.
            (work / "README.md").write_text("staged\n", encoding="utf-8")
            git(["add", "README.md"], cwd=work)
            (work / "README.md").write_text("staged\nunstaged\n", encoding="utf-8")
            (work / "untracked-owner.txt").write_text("owner only\n", encoding="utf-8")

            def owner_state() -> tuple[str, str, str, str, bytes, bytes]:
                return (
                    git(["branch", "--show-current"], cwd=work),
                    git(["diff", "--cached"], cwd=work),
                    git(["diff"], cwd=work),
                    git(["status", "--porcelain=v1"], cwd=work),
                    (work / ".git" / "index").read_bytes(),
                    (work / "untracked-owner.txt").read_bytes(),
                )

            before = owner_state()
            task_path = control / ".agent" / "tasks" / "shared-task-id.json"
            task = json.loads(task_path.read_text(encoding="utf-8"))
            task["allow_write"] = True
            task["writes"] = [
                {"path": "agent-output.txt", "content": "isolated result\n"},
            ]
            task_path.write_text(json.dumps(task, indent=2) + "\n", encoding="utf-8")
            git(["add", ".agent/tasks/shared-task-id.json"], cwd=control)
            git(["commit", "-m", "Queue attached result smoke"], cwd=control)
            git(["push", "origin", "agent-control"], cwd=control)

            repository = load_repository_registry(home=home, path=registry)[0]
            self.assertEqual(repository.work_mode, "attached")
            command = [
                sys.executable, "-m", "local_agent.repository.worker",
                "--repository-id", "project-attached",
                "--expected-config-digest", repository_config_digest(repository),
                "--registry", str(registry),
            ]
            run = subprocess.run(
                command, cwd=REPO_ROOT, env=env, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=90, check=False,
            )
            self.assertEqual(run.returncode, 10, run.stdout)
            self.assertEqual(owner_state(), before)
            self.assertFalse((work / "agent-output.txt").exists())

            # Read the real, independently published Git ref, not merely a
            # local worker artifact or mock of result publication.
            published = json.loads(git([
                "show", "refs/heads/agent-control:.agent/results/shared-task-id.json",
            ], cwd=remote))
            self.assertEqual(published["status"], "done", published)
            self.assertIn(
                "project-attached-ok", published["commands"][0]["output"],
            )
            self.assertEqual(
                json.loads((control / ".agent/results/shared-task-id.json").read_text())["status"],
                "done",
            )
            snapshots = inspect_attached_snapshots(Path(item["checkpoints"]))
            self.assertEqual(len(snapshots["snapshots"]), 1)
            self.assertEqual(snapshots["snapshots"][0]["review_state"], "completed_with_changes")
            self.assertFalse(snapshots["promotion_allowed"])
            retained = next((Path(item["checkpoints"]) / "attached-task-workspaces").iterdir())
            self.assertEqual(
                (retained / "work" / "agent-output.txt").read_text(encoding="utf-8"),
                "isolated result\n",
            )

            # A subsequent worker turn must not execute a terminal task again.
            second = subprocess.run(
                command, cwd=REPO_ROOT, env=env, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=90, check=False,
            )
            self.assertEqual(second.returncode, 0, second.stdout)
            self.assertEqual(owner_state(), before)
            self.assertEqual(
                len(list((Path(item["checkpoints"]) / "attached-task-workspaces").iterdir())),
                1,
            )


if __name__ == "__main__":
    unittest.main()
