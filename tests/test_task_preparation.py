from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_agent.cli import diagnostics
from local_agent.daemon.service import load_task_file
from local_agent.repository.binding import resolve_execution_target
from local_agent.runtime.task_preparation import prepare_task
from local_agent.runtime.task_transport import write_task_bundle

BINDING = "fe99a852-0486-4b3a-b09b-5d295e9355a8"
HOST_BINDING = "16d688b6-b0ef-4905-a5bd-24e59c99cfb4"
LOCAL_BINDING = "b0e94f93-478f-4fc6-abed-159d3702362f"
DISABLED_BINDING = "00000000-0000-4000-8000-000000000001"


class TaskPreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.catalog = self.root / "catalog.json"
        self.catalog.write_text(
            json.dumps(
                {
                    "version": 1,
                    "agents": [
                        {"id": "demo", "repository": "owner/demo", "agent_binding": BINDING},
                        {
                            "id": "host-ops",
                            "repository": "owner/host-ops",
                            "agent_binding": HOST_BINDING,
                        },
                        {
                            "id": "local-agent",
                            "repository": "owner/local-agent",
                            "agent_binding": LOCAL_BINDING,
                            "execution_enabled": True,
                        },
                        {
                            "id": "disabled",
                            "repository": "owner/disabled",
                            "agent_binding": DISABLED_BINDING,
                            "execution_enabled": False,
                        },
                    ],
                }
            )
        )
        self.registry = self.root / "registry.json"
        self.control = self.root / "control"
        self.work = self.root / "work"
        self.registry.write_text(
            json.dumps(
                {
                    "version": 1,
                    "repositories": [
                        {
                            "id": "demo",
                            "repository": "owner/demo",
                            "agent_binding": BINDING,
                            "control_dir": str(self.control),
                            "work_dir": str(self.work),
                            "checkpoints_dir": str(self.root / "checkpoints"),
                        }
                    ],
                }
            )
        )
        binding_file = self.control / ".agent/binding.json"
        binding_file.parent.mkdir(parents=True)
        binding_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "repository_id": "demo",
                    "repository": "owner/demo",
                    "agent_binding": BINDING,
                }
            )
        )

    def draft(self) -> dict:
        return {"id": "prepared-demo", "commands": ["echo codex"], "command_timeout": 60}

    def args(self, *arguments: str):
        return diagnostics.build_parser().parse_args(
            [
                *arguments,
                "--repository",
                "demo",
                "--catalog",
                str(self.catalog),
                "--registry",
                str(self.registry),
            ]
        )

    def test_resolves_explicit_id_or_remote_case_insensitively(self) -> None:
        for target in ("demo", "DEMO", "OWNER/DEMO"):
            self.assertEqual(
                resolve_execution_target(target, path=self.catalog).agent_binding, BINDING
            )
        for target in ("missing", " demo", "disabled"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                resolve_execution_target(target, path=self.catalog)

    def test_draft_receives_exact_binding_and_defaults_without_mutation(self) -> None:
        draft = self.draft()
        original = copy.deepcopy(draft)
        task = prepare_task(draft, repository="demo", catalog_path=self.catalog)
        self.assertEqual(draft, original)
        self.assertEqual(task["agent_binding"], BINDING)
        self.assertEqual(task["resources"], [])
        self.assertNotIn("profile", task)

    def test_existing_wrong_or_missing_binding_is_not_silently_replaced(self) -> None:
        for binding in (HOST_BINDING, None):
            with self.subTest(binding=binding), self.assertRaises(ValueError):
                prepare_task(
                    {**self.draft(), "agent_binding": binding},
                    repository="demo",
                    catalog_path=self.catalog,
                )

    def test_execution_disabled_target_never_produces_task(self) -> None:
        with self.assertRaisesRegex(ValueError, "execution-disabled"):
            prepare_task(self.draft(), repository="disabled", catalog_path=self.catalog)

    def test_chat_context_never_selects_or_authorizes_execution_target(self) -> None:
        task = prepare_task(
            {
                **self.draft(),
                "conversation": {"repository_id": "host-ops", "agent_binding": HOST_BINDING},
            },
            repository="demo",
            catalog_path=self.catalog,
        )
        self.assertEqual(task["agent_binding"], BINDING)
        with self.assertRaisesRegex(ValueError, "execution-disabled"):
            prepare_task(task, repository="disabled", catalog_path=self.catalog)

    def test_preparation_preserves_execution_budgets(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot fit"):
            prepare_task(
                {**self.draft(), "command_timeout": 900, "task_timeout": 120},
                repository="demo",
                catalog_path=self.catalog,
            )

    def test_hardware_requires_named_resources(self) -> None:
        for resources in (None, [], ["machine"]):
            draft = self.draft()
            if resources is not None:
                draft["resources"] = resources
            with (
                self.subTest(resources=resources),
                self.assertRaisesRegex(ValueError, "named resources"),
            ):
                prepare_task(
                    draft, repository="demo", profile="hardware", catalog_path=self.catalog
                )
        task = prepare_task(
            {**self.draft(), "resources": ["device:demo"]},
            repository="demo",
            profile="hardware",
            catalog_path=self.catalog,
        )
        self.assertEqual(task["resources"], ["device:demo"])

    def test_host_profile_requires_host_target_and_preserves_resource_scope(self) -> None:
        with self.assertRaisesRegex(ValueError, "local-agent"):
            prepare_task(
                self.draft(),
                repository="demo",
                profile="host-maintenance",
                catalog_path=self.catalog,
            )

        default_task = prepare_task(
            self.draft(),
            repository="local-agent",
            profile="host-maintenance",
            catalog_path=self.catalog,
        )
        self.assertEqual(default_task["agent_binding"], LOCAL_BINDING)
        self.assertEqual(default_task["resources"], [])

        for resources in (
            [],
            ["browser:chrome"],
            ["usb:esp32"],
            ["codex-cli"],
            ["browser:chrome", "usb:esp32"],
            ["machine"],
        ):
            with self.subTest(resources=resources):
                task = prepare_task(
                    {**self.draft(), "resources": resources},
                    repository="local-agent",
                    profile="host-maintenance",
                    catalog_path=self.catalog,
                )
                self.assertEqual(task["resources"], resources)

        with self.assertRaisesRegex(ValueError, "must be declared alone"):
            prepare_task(
                {**self.draft(), "resources": ["machine", "usb:esp32"]},
                repository="local-agent",
                profile="host-maintenance",
                catalog_path=self.catalog,
            )

        with self.assertRaisesRegex(ValueError, "local-agent"):
            prepare_task(
                self.draft(),
                repository="host-ops",
                profile="host-maintenance",
                catalog_path=self.catalog,
            )

    def test_bundle_load_uses_manifest_directory_without_rebinding_globals(self) -> None:
        task = prepare_task(self.draft(), repository="demo", catalog_path=self.catalog)
        manifest = write_task_bundle(self.root / "publication/.agent/tasks", task)
        self.assertEqual(load_task_file(manifest), task)

    def test_cli_prepares_bundle_and_refuses_overwrite(self) -> None:
        draft = self.root / "draft.json"
        draft.write_text(json.dumps(self.draft()))
        output = self.root / "publication/.agent/tasks"
        args = self.args("prepare-task", str(draft), "--output-dir", str(output))
        with mock.patch.object(diagnostics, "print_json") as printed:
            self.assertEqual(args.func(args), 0)
            self.assertTrue(printed.call_args.args[0]["prepared"])
            self.assertEqual(args.func(args), 1)
            self.assertIn("already exists", printed.call_args.args[0]["error"])
        self.assertEqual(load_task_file(output / "prepared-demo.json")["agent_binding"], BINDING)

    def test_cli_cannot_write_to_control_clone_or_symlink_alias(self) -> None:
        draft = self.root / "draft.json"
        draft.write_text(json.dumps(self.draft()))
        alias = self.root / "control-alias"
        alias.symlink_to(self.control, target_is_directory=True)
        for output in (self.control / ".agent/tasks", alias / ".agent/tasks"):
            args = self.args("prepare-task", str(draft), "--output-dir", str(output))
            with (
                self.subTest(output=output),
                mock.patch.object(diagnostics, "print_json") as printed,
            ):
                self.assertEqual(args.func(args), 1)
                self.assertIn("daemon control checkout", printed.call_args.args[0]["error"])
        self.assertFalse((self.control / ".agent/tasks").exists())

    def test_cli_protects_disabled_control_checkouts(self) -> None:
        payload = json.loads(self.registry.read_text())
        payload["repositories"][0]["enabled"] = False
        self.registry.write_text(json.dumps(payload))
        draft = self.root / "draft.json"
        draft.write_text(json.dumps(self.draft()))
        args = self.args(
            "prepare-task", str(draft), "--output-dir", str(self.control / ".agent/tasks")
        )
        with mock.patch.object(diagnostics, "print_json") as printed:
            self.assertEqual(args.func(args), 1)
            self.assertIn("daemon control checkout", printed.call_args.args[0]["error"])

    def test_preflight_checks_all_target_identities_and_preserves_disable_precedence(self) -> None:
        task = prepare_task(self.draft(), repository="demo", catalog_path=self.catalog)
        args = self.args("validate-task", "unused.json")
        with (
            mock.patch.object(diagnostics, "validate_repository"),
            mock.patch.object(diagnostics, "is_disabled", return_value=False),
        ):
            self.assertTrue(diagnostics.task_preflight(task, args)["ready"])
            wrong = diagnostics.task_preflight({**task, "agent_binding": HOST_BINDING}, args)
            self.assertFalse(wrong["ready"])
            self.assertIn("mismatch", wrong["blocked_reason"])
            binding_file = self.control / ".agent/binding.json"
            binding_file.unlink()
            self.assertIn("missing", diagnostics.task_preflight(task, args)["blocked_reason"])
        with mock.patch.object(diagnostics, "is_disabled", return_value=True):
            report = diagnostics.task_preflight(task, args)
        self.assertFalse(report["ready"])
        self.assertEqual(report["blocked_reason"], "global operator disable marker")

    def test_validate_cli_reports_ready_and_blocked_exit_codes_for_prepared_bundle(self) -> None:
        task = prepare_task(self.draft(), repository="demo", catalog_path=self.catalog)
        manifest = write_task_bundle(self.root / "publication/.agent/tasks", task)
        args = self.args("validate-task", str(manifest))
        with (
            mock.patch.object(diagnostics, "validate_repository"),
            mock.patch.object(diagnostics, "is_disabled", return_value=False),
            mock.patch.object(diagnostics, "print_json") as printed,
        ):
            self.assertEqual(args.func(args), 0)
            self.assertTrue(printed.call_args.args[0]["preflight"]["ready"])
        with (
            mock.patch.object(diagnostics, "validate_repository"),
            mock.patch.object(diagnostics, "is_disabled", return_value=True),
            mock.patch.object(diagnostics, "print_json") as printed,
        ):
            self.assertEqual(args.func(args), 1)
            self.assertTrue(printed.call_args.args[0]["valid"])
            self.assertFalse(printed.call_args.args[0]["preflight"]["ready"])

    def test_preflight_rejects_registry_identity_drift_and_invalid_branch(self) -> None:
        task = prepare_task(self.draft(), repository="demo", catalog_path=self.catalog)
        args = self.args("validate-task", "unused.json")
        with (
            mock.patch.object(diagnostics, "validate_repository"),
            mock.patch.object(diagnostics, "is_disabled", return_value=False),
        ):
            invalid_branch = diagnostics.task_preflight({**task, "work_branch": "../other"}, args)
            self.assertFalse(invalid_branch["ready"])
            self.assertIn("work_branch", invalid_branch["blocked_reason"])
            payload = json.loads(self.registry.read_text())
            payload["repositories"][0]["agent_binding"] = HOST_BINDING
            self.registry.write_text(json.dumps(payload))
            report = diagnostics.task_preflight(task, args)
        self.assertFalse(report["ready"])
        self.assertIn("registry identity differs", report["blocked_reason"])


if __name__ == "__main__":
    unittest.main()
