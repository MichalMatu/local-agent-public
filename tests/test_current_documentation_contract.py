from __future__ import annotations
import re
import unittest
from pathlib import Path
from local_agent.version import RELEASE_VERSION
REPO_ROOT = Path(__file__).resolve().parents[1]
CURRENT_OPERATIONAL_DOCS = ("README.md", "AGENTS.md", "CONTRIBUTING.md", "docs/ARCHITECTURE.md", "docs/OPERATIONS.md", "docs/SECURITY_MODEL.md")
CURRENT_SCHEDULER_DOCS = CURRENT_OPERATIONAL_DOCS
class CurrentDocumentationContractTests(unittest.TestCase):
    def test_current_operational_docs_do_not_restore_obsolete_two_worker_commands(self) -> None:
        for relative in CURRENT_OPERATIONAL_DOCS:
            with self.subTest(path=relative):
                text = (REPO_ROOT / relative).read_text(encoding="utf-8")
                self.assertNotIn("--max-workers 2", text)

    def test_current_operational_docs_do_not_claim_obsolete_three_worker_cap(self) -> None:
        forbidden = (
            "hard cap remains three",
            "hard cap: `3`",
            "hard-caps the value at three",
        )
        for relative in CURRENT_OPERATIONAL_DOCS:
            text = (REPO_ROOT / relative).read_text(encoding="utf-8")
            for phrase in forbidden:
                with self.subTest(path=relative, phrase=phrase):
                    self.assertNotIn(phrase, text)

    def test_current_scheduler_docs_describe_final_behavior_not_unresolved_candidate_work(self) -> None:
        forbidden = (
            "candidate fix must",
            "The candidate fix must",
            "The v4.18.14 candidate is not production",
            "The current release candidate is v4.18.14",
            "After a validated v4.18.14 candidate is explicitly advanced to `main`",
        )
        for relative in CURRENT_SCHEDULER_DOCS:
            text = (REPO_ROOT / relative).read_text(encoding="utf-8")
            for phrase in forbidden:
                with self.subTest(path=relative, phrase=phrase):
                    self.assertNotIn(phrase, text)

    def test_host_ops_effect_taxonomy_is_canonical_and_complete(self) -> None:
        security = (
            REPO_ROOT / "docs" / "host_ops" / "security" / "SECURITY_MODEL.md"
        ).read_text(encoding="utf-8")
        inventory = (
            REPO_ROOT / "docs" / "host_ops" / "TOOL_INVENTORY.md"
        ).read_text(encoding="utf-8")

        for label in (
            "PASSIVE_READ",
            "ACTIVE_READ",
            "MUTATION",
            "DISRUPTIVE",
            "NONE",
            "FIXED_LOCAL_EXEC",
            "FIXED_REMOTE_DEVICE_EXEC",
            "FIXED_DEVICE_IO",
            "ARBITRARY_CODE_LIKE",
        ):
            with self.subTest(label=label):
                self.assertIn(label, security)

        required_rows = (
            "| ADB devices / identity / logcat |",
            "| serial transact |",
            "| macOS unmount / eject |",
            "| artifact deploy |",
            "| SSH exec |",
            "| remote-Git run / run-current |",
            "| remote-Git cache list |",
            "| managed / interactive browser start |",
        )
        for row in required_rows:
            with self.subTest(row=row):
                self.assertIn(row, inventory)

        self.assertIn(
            "| remote-Git cache list | MUTATION | FIXED_REMOTE_DEVICE_EXEC |",
            inventory,
        )
        self.assertIn(
            "| SSH exec | DISRUPTIVE | ARBITRARY_CODE_LIKE |",
            inventory,
        )
        self.assertIn(
            "effect/authority classification never creates scheduler resources",
            security,
        )

    def test_host_ops_target_locator_and_scheduler_resource_identity_are_separate(self) -> None:
        target_model = (
            REPO_ROOT / "docs" / "host_ops" / "architecture" / "TARGET_MODEL.md"
        ).read_text(encoding="utf-8")
        inventory = (
            REPO_ROOT / "docs" / "host_ops" / "TOOL_INVENTORY.md"
        ).read_text(encoding="utf-8")

        required = (
            "operation target",
            "transport locator",
            "durable identity evidence",
            "tool-runtime lock scope",
            "scheduler resource",
            "For wireless ADB, IP:port is only a",
            "diskN, diskNsN, /dev/disk*, mount point and volume name are locators",
            "HostTarget.alias is the stable logical configured target",
            "workspace lock and optional broader host lock are tool-runtime",
            "resources: [] is the normal contract for project-dedicated hardware",
            "a scheduler resource key never substitutes for live target validation",
        )
        for phrase in required:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, target_model)

        forbidden_inventory = (
            "future resource identity should be the resolved serial device",
            "explicit target-disk resource identity",
            "make remote workspace and optional heavy-job lock identities explicit future resources",
        )
        for phrase in forbidden_inventory:
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, inventory)

        self.assertIn(
            "disk number is not treated as durable physical-media identity",
            inventory,
        )
        self.assertIn(
            "tool-runtime correctness scopes; do not automatically translate them into scheduler resources",
            inventory,
        )
        self.assertIn(
            "AdbIdentity profile as mismatch evidence rather than a universally unique immutable hardware identifier",
            inventory,
        )

    def test_golden_standard_release_state_matches_source_version(self) -> None:
        golden = (REPO_ROOT / "docs" / "GOLDEN_STANDARD.md").read_text(encoding="utf-8")
        source_match = re.search(r"source release marker is `v([^`]+)`", golden)
        self.assertIsNotNone(source_match, "missing source release declaration")
        assert source_match is not None
        self.assertEqual(source_match.group(1), RELEASE_VERSION)
        self.assertIn("[release support](SUPPORT.md)", golden)
        self.assertNotIn("current source and production release is", golden)
        self.assertNotIn("until the explicit release decision advances `main`", golden)

    def test_release_version_has_matching_release_notes_and_changelog_entry(self) -> None:
        notes = REPO_ROOT / "docs" / f"RELEASE_NOTES_V{RELEASE_VERSION}.md"
        self.assertTrue(notes.is_file(), f"missing release notes for {RELEASE_VERSION}")
        notes_text = notes.read_text(encoding="utf-8")
        self.assertTrue(notes_text.startswith(f"# Local Agent {RELEASE_VERSION}\n"))
        self.assertNotIn("must not be tagged/frozen until", notes_text)
        self.assertNotIn("A follow-up candidate adds", notes_text)
        changelog = (REPO_ROOT / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(f"## v{RELEASE_VERSION}\n", changelog)


    def test_customer_entrypoints_have_explicit_private_transport(self):
        readme = (REPO_ROOT / "README.md").read_text()
        install = (REPO_ROOT / "INSTALL_WITH_CODEX.md").read_text()
        self.assertIn("https://buymeacoffee.com/michalmatuh", readme)
        self.assertIn("https://github.com/MichalMatu/local-agent-public", readme)
        self.assertIn("terminal access", readme)
        self.assertIn("Never use the public source repository as an inbox", install)
        self.assertIn("PASS/PENDING/FAIL", install)

    def test_first_install_has_no_fabricated_rollback_commit(self):
        guide = (REPO_ROOT / "platforms/linux/local-agent/INSTALL_WITH_CODEX.md").read_text()
        self.assertIn("stopped and disabled", guide)
        self.assertIn("Do not fabricate a previous commit", guide)
        for name in ("local-agent.service.example", "local-agent-pi.service.example"):
            unit = (REPO_ROOT / "deploy/linux" / name).read_text()
            self.assertIn("KillMode=control-group", unit)
            self.assertIn("UMask=0077", unit)
            self.assertIn("RestartSec=10", unit)

    def test_public_tree_does_not_contain_operational_history(self):
        for name in ("docs/history", "docs/checkpoints", "docs/CURRENT_HANDOFF.md", "docs/NEXT_SESSION_PROMPT.md", ".agent"):
            self.assertFalse((REPO_ROOT / name).exists(), name)
