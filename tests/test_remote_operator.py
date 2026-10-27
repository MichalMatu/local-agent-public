from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

import local_agent.operator.local as agent_operator
from local_agent.operator import remote


class RemoteOperatorValidationTests(unittest.TestCase):
    def test_validated_state_accepts_only_version_one_and_safe_request_id(self) -> None:
        self.assertEqual(
            remote._validated_state(
                {
                    "version": 1,
                    "desired_state": "disabled",
                    "request_id": "stop-1",
                }
            ),
            ("disabled", "stop-1"),
        )
        invalid = (
            {"version": True, "desired_state": "disabled", "request_id": "stop-1"},
            {"version": 1.0, "desired_state": "disabled", "request_id": "stop-1"},
            {"version": 1, "desired_state": "disabled", "request_id": "x" * 121},
            {"version": 2, "desired_state": "disabled", "request_id": "stop-1"},
            {"version": 1, "desired_state": "paused", "request_id": "stop-1"},
            {"version": 1, "desired_state": "disabled", "request_id": "../stop"},
            {"version": 1, "desired_state": "disabled", "request_id": 7},
        )
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                remote._validated_state(payload)

    def test_remote_ref_rejects_missing_and_malformed_refs(self) -> None:
        repository = Path("/tmp/local-agent")
        cases = (
            ({"exit_code": 1, "output": "network down"}, "network down"),
            ({"exit_code": 0, "output": ""}, "branch is missing"),
            ({"exit_code": 0, "output": "not-a-sha\trefs/heads/operator-control"}, "invalid"),
        )
        for result, message in cases:
            with self.subTest(result=result), mock.patch.object(
                remote,
                "_git",
                return_value=result,
            ), self.assertRaisesRegex(RuntimeError, message):
                remote._remote_ref(repository)

    def test_remote_ref_normalizes_hex_sha(self) -> None:
        repository = Path("/tmp/local-agent")
        sha = "ABCDEF0123456789ABCDEF0123456789ABCDEF01"
        with mock.patch.object(
            remote,
            "_git",
            return_value={
                "exit_code": 0,
                "output": f"{sha}\trefs/heads/operator-control",
            },
        ):
            self.assertEqual(remote._remote_ref(repository), sha.lower())

    def test_load_payload_rejects_ref_change_during_fetch(self) -> None:
        expected = "a" * 40
        results = iter(
            (
                {"exit_code": 0, "output": ""},
                {"exit_code": 0, "output": "b" * 40},
            )
        )
        with mock.patch.object(remote, "_git", side_effect=lambda *_a, **_k: next(results)):
            with self.assertRaisesRegex(RuntimeError, "changed during fetch"):
                remote._load_remote_payload(Path("/tmp/local-agent"), expected)

    def test_load_payload_rejects_missing_state_and_non_object_json(self) -> None:
        expected = "a" * 40
        missing_results = iter(
            (
                {"exit_code": 0, "output": ""},
                {"exit_code": 0, "output": expected},
                {"exit_code": 1, "output": "missing"},
            )
        )
        with mock.patch.object(
            remote,
            "_git",
            side_effect=lambda *_a, **_k: next(missing_results),
        ), self.assertRaisesRegex(ValueError, "missing"):
            remote._load_remote_payload(Path("/tmp/local-agent"), expected)

        list_results = iter(
            (
                {"exit_code": 0, "output": ""},
                {"exit_code": 0, "output": expected},
                {"exit_code": 0, "output": "[]"},
            )
        )
        with mock.patch.object(
            remote,
            "_git",
            side_effect=lambda *_a, **_k: next(list_results),
        ), self.assertRaisesRegex(ValueError, "root must be an object"):
            remote._load_remote_payload(Path("/tmp/local-agent"), expected)


class RemoteOperatorFailClosedTests(unittest.TestCase):
    def test_invalid_reachable_payload_fails_closed(self) -> None:
        state = remote.RemoteOperatorState()
        ref = "a" * 40
        with mock.patch.object(remote, "_remote_ref", return_value=ref), mock.patch.object(
            remote,
            "_load_remote_payload",
            return_value={
                "version": 99,
                "desired_state": "enabled",
                "request_id": "bad-version",
            },
        ), mock.patch.object(agent_operator, "is_disabled", return_value=False), mock.patch.object(
            agent_operator,
            "disable_agent",
        ) as disable:
            result = remote.poll_remote_operator(
                state,
                self_repo=Path("/tmp/local-agent"),
                now=10.0,
                force=True,
            )

        self.assertEqual(result, "disabled")
        self.assertEqual(state.last_ref, ref)
        self.assertEqual(state.desired_state, "disabled")
        self.assertEqual(state.request_id, "invalid-aaaaaaaaaaaa")
        disable.assert_called_once_with(
            control_id="invalid-aaaaaaaaaaaa",
            reason="remote_operator_control",
        )

    def test_transport_failure_preserves_last_known_enabled_state(self) -> None:
        state = remote.RemoteOperatorState(
            last_poll_at=0.0,
            last_ref="a" * 40,
            desired_state="enabled",
            request_id="enable-1",
        )
        with mock.patch.object(
            remote,
            "_remote_ref",
            side_effect=RuntimeError("network down"),
        ):
            result = remote.poll_remote_operator(
                state,
                self_repo=Path("/tmp/local-agent"),
                now=10.0,
            )

        self.assertEqual(result, "enabled")
        self.assertEqual(state.desired_state, "enabled")
        self.assertEqual(state.request_id, "enable-1")

    def test_fetch_failure_after_new_ref_preserves_last_known_disabled_state(self) -> None:
        state = remote.RemoteOperatorState(
            last_poll_at=0.0,
            last_ref="a" * 40,
            desired_state="disabled",
            request_id="stop-1",
        )
        with mock.patch.object(remote, "_remote_ref", return_value="b" * 40), mock.patch.object(
            remote,
            "_load_remote_payload",
            side_effect=RuntimeError("fetch failed"),
        ), mock.patch.object(agent_operator, "is_disabled", return_value=True), mock.patch.object(
            agent_operator,
            "disable_agent",
        ) as disable:
            result = remote.poll_remote_operator(
                state,
                self_repo=Path("/tmp/local-agent"),
                now=10.0,
            )

        self.assertEqual(result, "disabled")
        self.assertEqual(state.last_ref, "a" * 40)
        self.assertEqual(state.request_id, "stop-1")
        disable.assert_not_called()

    def test_poll_interval_uses_cached_state_without_network(self) -> None:
        state = remote.RemoteOperatorState(
            last_poll_at=10.0,
            last_ref="a" * 40,
            desired_state="enabled",
            request_id="enable-1",
        )
        with mock.patch.object(remote, "_remote_ref") as probe:
            result = remote.poll_remote_operator(
                state,
                self_repo=Path("/tmp/local-agent"),
                now=11.0,
            )
        self.assertEqual(result, "enabled")
        probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
