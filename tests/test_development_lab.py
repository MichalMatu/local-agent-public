from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from local_agent.development.lab import (
    PROTECTED_OPERATIONAL_BRANCHES,
    adopt_existing_browser_profile,
    build_dev_lab_layout,
    dev_lab_status,
    initialize_dev_lab,
    protected_production_paths,
    rebind_dev_lab_checkout,
)


class DevelopmentLabTests(unittest.TestCase):
    @staticmethod
    def _seed_adoptable_profile(layout) -> dict[str, bytes]:
        profile = layout.browser_profile_dir
        default = profile / "Default"
        default.mkdir(parents=True)
        payloads = {
            "Local State": b"local-state",
            "Default/Preferences": b"preferences",
            "Default/Cookies": b"cookies",
        }
        for relative, payload in payloads.items():
            path = profile / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        return payloads

    def test_default_layout_is_separate_and_synthetic_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            layout = build_dev_lab_layout(home=home)
            expected_home = home.resolve()

            self.assertEqual(layout.home, expected_home)
            self.assertEqual(layout.checkout, expected_home / "local-agent-dev")
            self.assertEqual(
                layout.root,
                expected_home / "Library" / "Application Support" / "local-agent-dev",
            )
            self.assertEqual(layout.production_checkout, expected_home / "local-agent")
            manifest = layout.manifest()
            self.assertEqual(manifest["mode"], "synthetic-only")
            self.assertEqual(
                manifest["protected_operational_branches"],
                list(PROTECTED_OPERATIONAL_BRANCHES),
            )
            self.assertEqual(
                manifest["capabilities"],
                {
                    "executor_enabled": False,
                    "remote_control_enabled": False,
                    "real_chrome_profile_enabled": False,
                    "native_host_registration_enabled": False,
                },
            )

    def test_initialize_creates_only_dev_namespace_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            layout = build_dev_lab_layout(home=home)
            first = initialize_dev_lab(layout)
            second = initialize_dev_lab(layout)

            self.assertEqual(first, second)
            self.assertTrue(layout.marker_path.is_file())
            self.assertEqual(
                json.loads(layout.marker_path.read_text(encoding="utf-8")),
                layout.manifest(),
            )
            for path in (
                layout.state_dir,
                layout.repositories_dir,
                layout.browser_profile_dir,
                layout.logs_dir,
                layout.fixtures_dir,
            ):
                self.assertTrue(path.is_dir())

            protected = protected_production_paths(
                home=home,
                production_checkout=layout.production_checkout,
            )
            self.assertFalse(protected["checkout"].exists())
            self.assertFalse(protected["state"].exists())
            self.assertFalse(protected["workspace"].exists())
            self.assertFalse(protected["launch_agent"].exists())
            self.assertFalse(protected["stdout_log"].exists())
            self.assertFalse(protected["stderr_log"].exists())
            self.assertFalse(protected["chrome_profile_root"].exists())

            status = dev_lab_status(layout)
            self.assertTrue(status["healthy"])
            self.assertEqual(status["problems"], [])

    def test_adopt_profile_adds_only_lab_metadata_and_preserves_browser_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            layout = build_dev_lab_layout(home=home)
            payloads = self._seed_adoptable_profile(layout)

            adopted = adopt_existing_browser_profile(layout)

            self.assertEqual(adopted, layout.manifest())
            self.assertEqual(
                json.loads(layout.marker_path.read_text(encoding="utf-8")),
                layout.manifest(),
            )
            for relative, payload in payloads.items():
                self.assertEqual((layout.browser_profile_dir / relative).read_bytes(), payload)
            for path in (
                layout.state_dir,
                layout.repositories_dir,
                layout.browser_profile_dir,
                layout.logs_dir,
                layout.fixtures_dir,
            ):
                self.assertTrue(path.is_dir())
            self.assertTrue(dev_lab_status(layout)["healthy"])

    def test_adopt_profile_rejects_unexpected_root_entries_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            layout = build_dev_lab_layout(home=home)
            payloads = self._seed_adoptable_profile(layout)
            unexpected = layout.root / "unexpected.txt"
            unexpected.write_text("do not adopt", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "contain only browser-profile"):
                adopt_existing_browser_profile(layout)

            self.assertFalse(layout.marker_path.exists())
            self.assertEqual(unexpected.read_text(encoding="utf-8"), "do not adopt")
            for relative, payload in payloads.items():
                self.assertEqual((layout.browser_profile_dir / relative).read_bytes(), payload)

    def test_adopt_profile_rejects_missing_chromium_identity_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            layout = build_dev_lab_layout(home=home)
            default = layout.browser_profile_dir / "Default"
            default.mkdir(parents=True)
            (default / "Preferences").write_text("preferences", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "Local State"):
                adopt_existing_browser_profile(layout)

            self.assertFalse(layout.marker_path.exists())
            self.assertFalse(layout.state_dir.exists())

    def test_adopt_profile_rejects_profile_symlink_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            layout = build_dev_lab_layout(home=home)
            self._seed_adoptable_profile(layout)
            external = home / "external.txt"
            external.write_text("external", encoding="utf-8")
            link = layout.browser_profile_dir / "Default" / "linked"
            link.symlink_to(external)

            with self.assertRaisesRegex(RuntimeError, "refuses symbolic links"):
                adopt_existing_browser_profile(layout)

            self.assertFalse(layout.marker_path.exists())
            self.assertEqual(external.read_text(encoding="utf-8"), "external")

    def test_rebind_checkout_updates_only_marker_for_unused_healthy_lab(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            old_checkout = home / "local-agent-dev"
            new_checkout = home / "local-agent-conversation-operator"
            old_checkout.mkdir()
            new_checkout.mkdir()
            old_layout = build_dev_lab_layout(home=home, checkout=old_checkout)
            payloads = self._seed_adoptable_profile(old_layout)
            adopt_existing_browser_profile(old_layout)
            new_layout = build_dev_lab_layout(
                home=home,
                root=old_layout.root,
                checkout=new_checkout,
            )

            rebound = rebind_dev_lab_checkout(new_layout)

            self.assertEqual(rebound, new_layout.manifest())
            self.assertEqual(
                json.loads(new_layout.marker_path.read_text(encoding="utf-8")),
                new_layout.manifest(),
            )
            self.assertTrue(dev_lab_status(new_layout)["healthy"])
            self.assertFalse(dev_lab_status(old_layout)["healthy"])
            for relative, payload in payloads.items():
                self.assertEqual((new_layout.browser_profile_dir / relative).read_bytes(), payload)

    def test_rebind_checkout_rejects_nonempty_lab_state_without_marker_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            old_checkout = home / "local-agent-dev"
            new_checkout = home / "local-agent-conversation-operator"
            old_checkout.mkdir()
            new_checkout.mkdir()
            old_layout = build_dev_lab_layout(home=home, checkout=old_checkout)
            self._seed_adoptable_profile(old_layout)
            adopt_existing_browser_profile(old_layout)
            original = old_layout.marker_path.read_text(encoding="utf-8")
            (old_layout.state_dir / "used.json").write_text("{}", encoding="utf-8")
            new_layout = build_dev_lab_layout(
                home=home,
                root=old_layout.root,
                checkout=new_checkout,
            )

            with self.assertRaisesRegex(RuntimeError, "inert lab state"):
                rebind_dev_lab_checkout(new_layout)

            self.assertEqual(old_layout.marker_path.read_text(encoding="utf-8"), original)

    def test_rebind_checkout_rejects_tampered_marker_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            old_checkout = home / "local-agent-dev"
            new_checkout = home / "local-agent-conversation-operator"
            old_checkout.mkdir()
            new_checkout.mkdir()
            old_layout = build_dev_lab_layout(home=home, checkout=old_checkout)
            self._seed_adoptable_profile(old_layout)
            adopt_existing_browser_profile(old_layout)
            payload = old_layout.manifest()
            payload["capabilities"]["executor_enabled"] = True
            old_layout.marker_path.write_text(json.dumps(payload), encoding="utf-8")
            original = old_layout.marker_path.read_text(encoding="utf-8")
            new_layout = build_dev_lab_layout(
                home=home,
                root=old_layout.root,
                checkout=new_checkout,
            )

            with self.assertRaisesRegex(RuntimeError, "canonical existing DEV lab marker"):
                rebind_dev_lab_checkout(new_layout)

            self.assertEqual(old_layout.marker_path.read_text(encoding="utf-8"), original)

    def test_rebind_checkout_rejects_missing_new_checkout_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            old_checkout = home / "local-agent-dev"
            old_checkout.mkdir()
            old_layout = build_dev_lab_layout(home=home, checkout=old_checkout)
            self._seed_adoptable_profile(old_layout)
            adopt_existing_browser_profile(old_layout)
            original = old_layout.marker_path.read_text(encoding="utf-8")
            new_layout = build_dev_lab_layout(
                home=home,
                root=old_layout.root,
                checkout=home / "missing-operator-checkout",
            )

            with self.assertRaisesRegex(RuntimeError, "existing regular checkout directory"):
                rebind_dev_lab_checkout(new_layout)

            self.assertEqual(old_layout.marker_path.read_text(encoding="utf-8"), original)

    def test_rejects_root_or_checkout_overlapping_production_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            protected = protected_production_paths(
                home=home,
                production_checkout=home / "local-agent",
            )
            bad_roots = (
                protected["state"],
                protected["state"] / "dev",
                protected["workspace"],
                protected["chrome_profile_root"] / "DevProfile",
                home / "Library" / "Application Support",
            )
            for root in bad_roots:
                with self.subTest(root=root):
                    with self.assertRaisesRegex(ValueError, "overlaps production"):
                        build_dev_lab_layout(home=home, root=root)

            with self.assertRaisesRegex(ValueError, "overlaps production"):
                build_dev_lab_layout(home=home, checkout=home / "local-agent")
            with self.assertRaisesRegex(ValueError, "overlaps production"):
                build_dev_lab_layout(home=home, checkout=home / "local-agent" / "dev")

    def test_rejects_dev_state_inside_checkout_or_checkout_inside_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            with self.assertRaisesRegex(ValueError, "must be disjoint"):
                build_dev_lab_layout(
                    home=home,
                    root=home / "local-agent-dev" / "state",
                    checkout=home / "local-agent-dev",
                )
            with self.assertRaisesRegex(ValueError, "must be disjoint"):
                build_dev_lab_layout(
                    home=home,
                    root=home / "dev-lab",
                    checkout=home / "dev-lab" / "checkout",
                )

    def test_symlink_alias_to_production_state_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            production_state = home / "Library" / "Application Support" / "local-agent"
            production_state.mkdir(parents=True)
            alias = home / "dev-state-alias"
            alias.symlink_to(production_state, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "overlaps production"):
                build_dev_lab_layout(home=home, root=alias)

    def test_nonempty_unmarked_root_is_not_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            layout = build_dev_lab_layout(home=home)
            layout.root.mkdir(parents=True)
            (layout.root / "unexpected.txt").write_text("do not adopt", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "refusing to adopt"):
                initialize_dev_lab(layout)

    def test_existing_marker_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            layout = build_dev_lab_layout(home=home)
            initialize_dev_lab(layout)
            payload = layout.manifest()
            payload["capabilities"]["executor_enabled"] = True
            layout.marker_path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "does not match"):
                initialize_dev_lab(layout)
            status = dev_lab_status(layout)
            self.assertFalse(status["healthy"])
            self.assertIn("marker_layout_mismatch", status["problems"])

    def test_initialize_rejects_symlinked_internal_directory_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            layout = build_dev_lab_layout(home=home)
            external = home / "external-state"
            external.mkdir()
            layout.root.mkdir(parents=True)
            layout.state_dir.symlink_to(external, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "must stay below lab root"):
                initialize_dev_lab(layout)

            self.assertEqual(tuple(external.iterdir()), ())
            self.assertFalse(layout.marker_path.exists())

    def test_initialize_rejects_symlinked_marker_without_overwriting_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            layout = build_dev_lab_layout(home=home)
            layout.root.mkdir(parents=True)
            external_marker = home / "external-marker.json"
            original = json.dumps(layout.manifest(), sort_keys=True)
            external_marker.write_text(original, encoding="utf-8")
            layout.marker_path.symlink_to(external_marker)

            with self.assertRaisesRegex(ValueError, "must stay below lab root"):
                initialize_dev_lab(layout)

            self.assertEqual(external_marker.read_text(encoding="utf-8"), original)

    def test_status_detects_missing_or_symlinked_lab_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            layout = build_dev_lab_layout(home=home)
            initialize_dev_lab(layout)
            layout.logs_dir.rmdir()

            status = dev_lab_status(layout)
            self.assertFalse(status["healthy"])
            self.assertIn("directory_missing_or_unsafe:logs", status["problems"])

            real_logs = layout.root / "real-logs"
            real_logs.mkdir()
            layout.logs_dir.symlink_to(real_logs, target_is_directory=True)
            status = dev_lab_status(layout)
            self.assertFalse(status["healthy"])
            self.assertIn("directory_missing_or_unsafe:logs", status["problems"])


if __name__ == "__main__":
    unittest.main()
