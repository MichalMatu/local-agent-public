"""Operator repository menu delegates to safe Core; no shell command construction."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from local_agent.cli import repositories, polling
from local_agent.cli.diagnostics import build_parser


class RepositoryMenuTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="la-cli-registry-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.registry = self.root / "repositories.json"
        self.registry.write_text(json.dumps({"version": 1, "repositories": [
            {"id": "example-project", "repository": "owner/example-project",
             "work_dir": str(self.root / "owner checkout"), "work_mode": "attached",
             "control_dir": str(self.root / "private control"),
             "checkpoints_dir": str(self.root / "checkpoints"),
             "agent_binding": "b0e94f93-478f-4fc6-abed-159d3702362f",
             "enabled": True},
            {"id": "idle-repo", "repository": "owner/idle-repo",
             "work_dir": str(self.root / "idle source"),
             "control_dir": str(self.root / "idle control"),
             "checkpoints_dir": str(self.root / "idle checkpoints"),
             "agent_binding": "fe99a852-0486-4b3a-b09b-5d295e9355a8",
             "enabled": False},
        ]}) + "\n")
        self.owner = self.root / "owner checkout"
        self.owner.mkdir()
        (self.owner / "dirty-file").write_text("never change this")
        self.saved = self.registry.read_bytes()

    def invoke(self, inputs: list[str]) -> str:
        with mock.patch("builtins.input", side_effect=inputs), redirect_stdout(io.StringIO()) as output:
            repositories.command_repositories_menu(self.registry)
        return output.getvalue()

    def test_menu_and_parser_show_repositories_without_change(self) -> None:
        self.assertIsNotNone(build_parser().parse_args(["menu", "--list"]).func)
        with redirect_stdout(io.StringIO()) as listing:
            polling.command_menu(argparse.Namespace(list=True, state_dir=self.root))
        self.assertIn("Repositories", listing.getvalue())
        output = self.invoke(["1", "6", "example-project", "0"])
        self.assertIn("1 enabled / 2 registered", output)
        self.assertIn("owner checkout", output)
        self.assertIn("agent-control", output)
        self.assertIn("idle-repo", output)
        self.assertEqual(self.registry.read_bytes(), self.saved)

    def test_list_is_compact_and_does_not_expose_unrequested_paths(self) -> None:
        with (mock.patch("subprocess.run", side_effect=AssertionError("no git or subprocess")),
              mock.patch("subprocess.Popen", side_effect=AssertionError("no process")),
              redirect_stdout(io.StringIO()) as out):
            repositories._show(self.registry)
        result = out.getvalue()
        self.assertIn("1 enabled / 2 registered", result)
        self.assertIn("ID: example-project", result)
        self.assertIn("[Enabled]  owner/example-project", result)
        self.assertIn("ID: idle-repo", result)
        self.assertIn("[Disabled]  owner/idle-repo", result)
        self.assertIn("Use the ID shown above for options 3-6", result)
        self.assertIn("Option 6 shows paths, branches and worktree type", result)
        self.assertNotIn(str(self.root / "owner checkout"), result)
        self.assertNotIn(str(self.root / "private control"), result)
        self.assertEqual(self.registry.read_bytes(), self.saved)

    def test_compact_rows_align_id_state_and_github_without_worktree_column(self) -> None:
        with redirect_stdout(io.StringIO()) as out:
            repositories._show(self.registry)
        entries = [line for line in out.getvalue().splitlines()
                   if line.startswith("  ID: ")]
        self.assertEqual(len(entries), 2)
        self.assertEqual([line.index("[") for line in entries],
                         [entries[0].index("["), entries[0].index("[")])
        self.assertIn("  [Enabled]  owner/example-project", entries[0])
        self.assertIn("  [Disabled]  owner/idle-repo", entries[1])
        self.assertNotIn("attached", entries[0])
        self.assertNotIn("managed", entries[1])
        self.assertEqual(self.registry.read_bytes(), self.saved)

    def test_empty_registry_list_has_no_id_rows_and_no_changes(self) -> None:
        empty = self.root / "empty-registry.json"
        empty.write_text(json.dumps({"version": 1, "repositories": []}), encoding="utf-8")
        old = empty.read_bytes()
        with redirect_stdout(io.StringIO()) as out:
            repositories._show(empty)
        self.assertIn("No optional repositories registered", out.getvalue())
        self.assertNotIn("  ID: ", out.getvalue())
        self.assertEqual(empty.read_bytes(), old)

    def test_details_explain_three_distinct_dirs_and_configured_branches(self) -> None:
        with (mock.patch("subprocess.run", side_effect=AssertionError("no git or subprocess")),
              redirect_stdout(io.StringIO()) as out):
            repositories._show(self.registry, "example-project")
        result = out.getvalue()
        self.assertIn("ID: example-project", result)
        self.assertIn("attached (owner-managed; never reset/clean)", result)
        self.assertIn(f"Project source/worktree: {(self.root / 'owner checkout').resolve()}", result)
        self.assertIn("Project target branch (configured): main", result)
        self.assertIn(f"Private Git control/inbox: {(self.root / 'private control').resolve()}", result)
        self.assertIn("Control branch (configured): agent-control", result)
        self.assertIn(f"Task checkpoints/history: {(self.root / 'checkpoints').resolve()}", result)
        self.assertIn("not a live Git or daemon check", result)
        self.assertIn("Disabling preserves folders and history", result)
        self.assertNotIn("idle-repo", result)
        self.assertEqual(self.registry.read_bytes(), self.saved)
        self.assertEqual((self.owner / "dirty-file").read_text(), "never change this")

    def test_untrusted_terminal_control_characters_are_escaped(self) -> None:
        self.assertEqual(repositories._display("name\\n\x1b[31m"), "name\\n\\x1b[31m")
        self.assertNotIn("\x1b", repositories._display("bad\x1b[2J"))

    def test_unknown_repository_detail_fails_closed_without_writing(self) -> None:
        result = self.invoke(["6", "missing-repository", "0"])
        self.assertIn("Blocked (no unsafe action): ValueError", result)
        self.assertIn("not registered", result)
        self.assertEqual(self.registry.read_bytes(), self.saved)

    def test_repository_menu_vertical_choices_and_zero_back(self) -> None:
        with (mock.patch("local_agent.repository.registration_status.repository_registration_status",
                         side_effect=AssertionError("no registry read")),
              mock.patch("local_agent.repository.onboarding.plan_repository_add",
                         side_effect=AssertionError("no plan")),
              mock.patch("builtins.input", side_effect=["0"]),
              redirect_stdout(io.StringIO()) as out):
            repositories.command_repositories_menu(self.registry)
        menu = out.getvalue()
        self.assertIn("Repositories\n1. List repositories\n2. Add repository\n", menu)
        self.assertIn("3. Enable repository\n4. Disable repository\n", menu)
        self.assertIn("5. Detach repository (keep files)\n", menu)
        self.assertIn("6. Repository details (paths and branches)\n\n0. Back", menu)
        self.assertEqual(self.registry.read_bytes(), self.saved)

    def test_zero_back_at_add_identifier_skips_plan_apply_and_binding(self) -> None:
        with (mock.patch("local_agent.repository.onboarding.plan_repository_add") as plan,
              mock.patch("local_agent.repository.transactional_add.apply_repository_add") as apply,
              mock.patch("local_agent.repository.binding_enrollment.ensure_local_repository_binding") as enroll):
            self.invoke(["2", "0", "0"])
        plan.assert_not_called()
        apply.assert_not_called()
        enroll.assert_not_called()
        self.assertEqual(self.registry.read_bytes(), self.saved)

    def test_repository_id_prompts_reference_the_list_without_side_effects(self) -> None:
        with (mock.patch("builtins.input", side_effect=["3", "0", "6", "0", "0"]) as ask,
              mock.patch("local_agent.repository.registration_enable.plan_repository_enable",
                         side_effect=AssertionError("no enable plan")),
              mock.patch.object(repositories, "_show",
                                side_effect=AssertionError("no detail read")),
              redirect_stdout(io.StringIO())):
            repositories.command_repositories_menu(self.registry)
        prompts = [call.args[0] for call in ask.call_args_list if call.args]
        self.assertEqual(prompts.count("Enter repository ID from the list (0 = Back): "), 2)
        self.assertEqual(self.registry.read_bytes(), self.saved)

    def test_zero_back_at_repository_id_skips_mutating_and_detail_plans(self) -> None:
        operations = (
            ("3", "enable", "registration_enable"),
            ("4", "disable", "registration_disable"),
            ("5", "detach", "registration_detach"),
        )
        for choice, action, module in operations:
            with (self.subTest(action=action),
                  mock.patch(f"local_agent.repository.{module}.plan_repository_{action}") as plan,
                  mock.patch(f"local_agent.repository.{module}.apply_repository_{action}") as apply):
                self.invoke([choice, "0", "0"])
                plan.assert_not_called()
                apply.assert_not_called()
        with (mock.patch.object(repositories, "_show") as details,
              mock.patch("builtins.input", side_effect=["6", "0", "0"]),
              redirect_stdout(io.StringIO())):
            repositories.command_repositories_menu(self.registry)
        details.assert_not_called()
        self.assertEqual(self.registry.read_bytes(), self.saved)

    def test_zero_back_at_add_confirmation_keeps_preview_only(self) -> None:
        with (mock.patch("local_agent.repository.onboarding.plan_repository_add",
                         return_value=self.planned_add()) as plan,
              mock.patch("local_agent.repository.transactional_add.apply_repository_add") as apply,
              mock.patch("local_agent.repository.binding_enrollment.ensure_local_repository_binding") as enroll,
              mock.patch("local_agent.repository.registration_disable._require_offline_installation",
                         side_effect=AssertionError("no offline mutation"))):
            output = self.invoke(["2", "owner/new", "0", "0"])
        self.assertIn("Back to Repositories. No changes applied", output)
        plan.assert_called_once()
        apply.assert_not_called()
        enroll.assert_not_called()
        self.assertEqual(self.registry.read_bytes(), self.saved)

    def test_zero_back_at_mutation_confirmations_preserves_registration(self) -> None:
        for choice, action, module, token in (
            ("3", "enable", "registration_enable", "APPLY"),
            ("4", "disable", "registration_disable", "APPLY"),
            ("5", "detach", "registration_detach", "DETACH"),
        ):
            with (self.subTest(action=action),
                  mock.patch(f"local_agent.repository.{module}.plan_repository_{action}",
                             return_value={"status": "planned_only",
                                           "expected_sha256": "a" * 64}) as plan,
                  mock.patch(f"local_agent.repository.{module}.apply_repository_{action}") as apply):
                output = self.invoke([choice, "idle-repo", "0", "0"])
                self.assertIn(f"Type {token} for OFFLINE registry change (0 = Back)", output)
                self.assertIn("Back to Repositories. No changes applied", output)
                plan.assert_called_once()
                apply.assert_not_called()
        self.assertEqual(self.registry.read_bytes(), self.saved)
        self.assertEqual((self.owner / "dirty-file").read_text(), "never change this")

    def test_zero_back_prompt_is_visible_at_identifier_and_confirmation(self) -> None:
        with (mock.patch("builtins.input",
                         side_effect=["2", "0", "2", "owner/new", "0", "0"]) as ask,
              mock.patch("local_agent.repository.onboarding.plan_repository_add",
                         return_value=self.planned_add()),
              redirect_stdout(io.StringIO())):
            repositories.command_repositories_menu(self.registry)
        prompts = [call.args[0] for call in ask.call_args_list if call.args]
        self.assertIn("GitHub owner/repository (0 = Back): ", prompts)
        self.assertIn("Type APPLY to register while daemon is OFFLINE (0 = Back): ",
                      prompts)
        self.assertEqual(self.registry.read_bytes(), self.saved)

    def test_offline_session_menu_confirms_automatic_restart(self) -> None:
        with (mock.patch.dict("os.environ", {"LOCAL_AGENT_MAINTENANCE_SESSION": "1"}),
              redirect_stdout(io.StringIO()) as output):
            repositories._restart_note()
        self.assertIn("Exit the menu to automatically restart", output.getvalue())
        with (mock.patch.dict("os.environ", {"LOCAL_AGENT_MAINTENANCE_SESSION": ""}),
              redirect_stdout(io.StringIO()) as output):
            repositories._restart_note()
        self.assertIn("guarded operator workflow", output.getvalue())

    def test_parent_menu_option_four_calls_repository_menu(self) -> None:
        with (mock.patch("sys.stdin") as stdin,
              mock.patch("builtins.input", side_effect=["4", "0"]),
              mock.patch.object(repositories, "command_repositories_menu") as submenu,
              redirect_stdout(io.StringIO())):
            stdin.isatty.return_value = True
            self.assertEqual(polling.command_menu(argparse.Namespace(
                list=False, state_dir=self.root, registry_file=self.registry,
            )), 0)
        submenu.assert_called_once_with(self.registry)

    def planned_add(self, *, binding: str | None = "existing-binding") -> dict:
        return {
            "repository": "owner/new", "repository_id": "new",
            "work": {"path": str(self.root / "managed/new/work"),
                     "action": "clone_if_missing"},
            "control": {"path": str(self.root / "managed/new/control"),
                        "action": "clone_if_missing"},
            "checkpoints": {"path": str(self.root / "managed/new/checkpoints")},
            "catalog_binding": binding,
        }

    def test_add_mvp_defaults_no_path_prompts_and_no_changes_without_apply(self) -> None:
        with (mock.patch("local_agent.repository.onboarding.plan_repository_add",
                         return_value=self.planned_add()) as plan,
              mock.patch("local_agent.repository.transactional_add.apply_repository_add") as apply):
            output = self.invoke(["2", "owner/new", "preview", "0"])
        self.assertIn("managed/new/work", output)
        self.assertIn("Local Agent defaults", output)
        self.assertIn("No changes applied", output)
        self.assertEqual(plan.call_args.args, ("owner/new",))
        self.assertEqual(plan.call_args.kwargs, {"registry_path": self.registry})
        apply.assert_not_called()
        self.assertEqual((self.owner / "dirty-file").read_text(), "never change this")
        self.assertEqual(self.registry.read_bytes(), self.saved)

    def test_add_mvp_previews_new_binding_without_any_write(self) -> None:
        with (mock.patch("local_agent.repository.onboarding.plan_repository_add",
                         return_value=self.planned_add(binding=None)) as plan,
              mock.patch("local_agent.repository.transactional_add.apply_repository_add") as apply,
              mock.patch("local_agent.repository.binding_enrollment.ensure_local_repository_binding") as enroll):
            output = self.invoke(["2", "owner/new", "preview", "0"])
        self.assertIn("new private canonical binding", output)
        self.assertIn("No changes applied", output)
        plan.assert_called_once()
        apply.assert_not_called()
        enroll.assert_not_called()
        self.assertEqual(self.registry.read_bytes(), self.saved)

    def test_add_missing_binding_enrolls_only_inside_offline_apply(self) -> None:
        from contextlib import contextmanager

        active = False

        @contextmanager
        def offline_fixture():
            nonlocal active
            active = True
            try:
                yield
            finally:
                active = False

        def enrollment(*_args):
            self.assertTrue(active)
            return "12222222-2222-4222-8222-222222222222"

        def apply_new(*_args, **_kwargs):
            self.assertTrue(active)
            return {"status": "registered"}

        with (mock.patch("local_agent.repository.onboarding.plan_repository_add",
                         return_value=self.planned_add(binding=None)),
              mock.patch("local_agent.repository.registration_disable._require_offline_installation",
                         return_value=offline_fixture()),
              mock.patch("local_agent.repository.binding_enrollment.ensure_local_repository_binding",
                         side_effect=enrollment) as enroll,
              mock.patch("local_agent.repository.transactional_add.apply_repository_add",
                         side_effect=apply_new) as apply):
            output = self.invoke(["2", "owner/new", "APPLY", "0"])
        self.assertIn('"status": "registered"', output)
        enroll.assert_called_once_with("new", "owner/new")
        apply.assert_called_once_with("owner/new", registry_path=self.registry)
        self.assertFalse(active)
        self.assertEqual(self.registry.read_bytes(), self.saved)

    def test_active_daemon_never_creates_new_binding(self) -> None:
        with (mock.patch("local_agent.repository.onboarding.plan_repository_add",
                         return_value=self.planned_add(binding=None)),
              mock.patch("local_agent.repository.registration_disable._require_offline_installation",
                         side_effect=RuntimeError("installation daemon is active")),
              mock.patch("local_agent.repository.binding_enrollment.ensure_local_repository_binding") as enroll,
              mock.patch("local_agent.repository.transactional_add.apply_repository_add") as apply):
            output = self.invoke(["2", "owner/new", "APPLY", "0"])
        self.assertIn("daemon is active", output)
        enroll.assert_not_called()
        apply.assert_not_called()
        self.assertEqual(self.registry.read_bytes(), self.saved)

    def test_add_apply_uses_only_default_layout_after_offline_guard(self) -> None:
        from contextlib import nullcontext

        with (mock.patch("local_agent.repository.onboarding.plan_repository_add",
                         return_value=self.planned_add()),
              mock.patch("local_agent.repository.registration_disable._require_offline_installation",
                         return_value=nullcontext()) as offline,
              mock.patch("local_agent.repository.transactional_add.apply_repository_add",
                         return_value={"status": "registered",
                                       "control_branch_created": True}) as apply):
            result = self.invoke(["2", "owner/new", "APPLY", "0"])
        self.assertIn("control_branch_created", result)
        self.assertEqual(apply.call_args.args, ("owner/new",))
        self.assertEqual(apply.call_args.kwargs, {"registry_path": self.registry})
        from local_agent.daemon import service as agentd
        offline.assert_called_once_with(agentd.STATE_DIR)
        self.assertEqual(self.registry.read_bytes(), self.saved)
        self.assertEqual((self.owner / "dirty-file").read_text(), "never change this")

    def test_apply_add_fails_closed_when_daemon_active(self) -> None:
        with (mock.patch("local_agent.repository.onboarding.plan_repository_add",
                         return_value=self.planned_add()),
              mock.patch("local_agent.repository.registration_disable._require_offline_installation",
                         side_effect=RuntimeError("installation daemon is active")),
              mock.patch("local_agent.repository.transactional_add.apply_repository_add") as apply):
            output = self.invoke(["2", "owner/new", "APPLY", "0"])
        self.assertIn("daemon is active", output)
        apply.assert_not_called()
        self.assertEqual(self.registry.read_bytes(), self.saved)

    def test_disable_enable_and_detach_preview_never_apply_without_token(self) -> None:
        candidates = (
            ("3", "enable", "APPLY"),
            ("4", "disable", "APPLY"),
            ("5", "detach", "DETACH"),
        )
        for choice, kind, token in candidates:
            module = {"enable": "registration_enable",
                      "disable": "registration_disable",
                      "detach": "registration_detach"}[kind]
            with (self.subTest(kind=kind),
                  mock.patch(f"local_agent.repository.{module}.plan_repository_{kind}",
                             return_value={"status": "planned_only",
                                           "expected_sha256": "a" * 64}),
                  mock.patch(f"local_agent.repository.{module}.apply_repository_{kind}") as apply):
                result = self.invoke([choice, "idle-repo", "NO", "0"])
                self.assertIn("No changes applied", result)
                apply.assert_not_called()
                self.assertIn(token, result)
        self.assertEqual(self.registry.read_bytes(), self.saved)

    def test_operator_detach_only_calls_guarded_registry_detach_on_exact_token(self) -> None:
        with (mock.patch("local_agent.repository.registration_detach.plan_repository_detach",
                         return_value={"status": "planned_only",
                                       "expected_sha256": "b" * 64}),
              mock.patch("local_agent.repository.registration_detach.apply_repository_detach",
                         return_value={"status": "detached", "changes_applied": True}) as apply):
            output = self.invoke(["5", "idle-repo", "DETACH", "0"])
        self.assertIn("keep", output.lower())
        apply.assert_called_once_with(
            "idle-repo", expected_sha256="b" * 64, registry_path=self.registry,
        )
        self.assertEqual(self.registry.read_bytes(), self.saved)


if __name__ == "__main__":
    unittest.main()
