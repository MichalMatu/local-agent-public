from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import local_agent.daemon.service as agentd
from local_agent.repository.context import RepositoryContext
from local_agent.supervisor import conversation
from local_agent.supervisor import observability


class OperatorObservabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state_patch = mock.patch.object(agentd, "STATE_DIR", self.root / "state")
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.control = self.root / "control"
        self.control.mkdir()
        self.control_patch = mock.patch.object(agentd.core, "CONTROL", self.control)
        self.control_patch.start()
        self.addCleanup(self.control_patch.stop)
        self.repository = RepositoryContext(
            repository_id="host-ops",
            repository="owner/host-ops",
            control=self.control,
            work=self.root / "work",
            checkpoints=self.root / "checkpoints",
        )

    def operator_env(self) -> dict[str, str]:
        return {
            conversation.OPERATOR_ENABLED_ENV: "1",
            conversation.OPERATOR_ROOT_ENV: str(self.root / "operator-root"),
            conversation.OPERATOR_CHECKOUT_ENV: str(self.root / "operator-checkout"),
            conversation.OPERATOR_PRODUCTION_CHECKOUT_ENV: str(self.root / "production"),
            conversation.OPERATOR_HOME_ENV: str(self.root / "home"),
        }

    def write_run(self, name: str, payload: dict[str, object]) -> None:
        path = agentd.STATE_DIR / "repositories" / "host-ops" / "runs" / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    def test_dedupe_snapshot_counts_durable_reasons_and_reconciliation(self) -> None:
        self.write_run(
            "dup-a",
            {"event": "duplicate_task_suppressed", "duplicate_reason": "queued_duplicate"},
        )
        self.write_run(
            "dup-b",
            {"event": "duplicate_task_suppressed", "duplicate_reason": "recent_duplicate"},
        )
        self.write_run(
            "conflict",
            {"event": "task_rejected", "failure_reason": "dedupe_intent_conflict"},
        )
        receipt = (
            agentd.STATE_DIR
            / "repositories"
            / "host-ops"
            / "task-dedupe"
            / "receipt.json"
        )
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(
            json.dumps(
                {
                    "version": 1,
                    "state": "completed",
                    "expires_at_epoch": 10_000,
                    "reconciled_from_published_run": True,
                    "reconciliation_reason": "published_run_after_claim_release",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        with mock.patch.object(observability.time, "time", return_value=100):
            snapshot = observability.dedupe_observability([self.repository])

        self.assertEqual(snapshot["suppressed_count"], 2)
        self.assertEqual(
            snapshot["suppression_reasons"],
            {"queued_duplicate": 1, "recent_duplicate": 1},
        )
        self.assertEqual(snapshot["rejected_count"], 1)
        self.assertEqual(
            snapshot["rejection_reasons"],
            {"dedupe_intent_conflict": 1},
        )
        self.assertEqual(snapshot["reconciled_count"], 1)
        self.assertEqual(
            snapshot["reconciliation_reasons"],
            {"published_run_after_claim_release": 1},
        )
        self.assertFalse(snapshot["scan_truncated"])

    def test_bad_operator_config_remains_observable(self) -> None:
        env = {
            conversation.OPERATOR_ENABLED_ENV: "1",
            conversation.OPERATOR_ROOT_ENV: str(self.root / "operator-root"),
        }
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            conversation,
            "result_publish_pending",
            side_effect=ValueError("operator checkout is unavailable"),
        ):
            snapshot = observability.operator_observability(None)

        self.assertTrue(snapshot["enabled"])
        self.assertFalse(snapshot["configured"])
        self.assertFalse(snapshot["running"])
        self.assertFalse(snapshot["result_publish_pending"])
        self.assertIsNone(snapshot["active_request"])
        self.assertIsInstance(snapshot["configuration_error"], str)
        self.assertTrue(snapshot["configuration_error"])

    def test_operator_snapshot_exposes_identity_not_child_prompt_content(self) -> None:
        request_path = self.root / "request.json"
        request = {
            "schema_version": 3,
            "id": "obs-request",
            "workflow_id": "workflow-42",
            "parent_conversation_url": "https://chatgpt.com/c/parent-observability",
            "repository_ids": ["host-ops", "local-agent"],
            "children": [
                {
                    "request_id": "child-a",
                    "node_id": "node-a",
                    "role": "research",
                    "summary": "private bounded reasoning summary",
                    "paths": ["sensitive/path"],
                },
                {
                    "request_id": "child-b",
                    "node_id": "node-b",
                    "role": "verification",
                    "summary": "another private summary",
                    "paths": ["other/path"],
                },
            ],
        }
        request_path.write_text(json.dumps(request), encoding="utf-8")
        campaign = conversation.RunningOperatorCampaign(
            request_id="obs-request",
            proc=SimpleNamespace(poll=lambda: None),
            started_at=0.0,
            request_path=request_path,
            result_path=self.root / "result.json",
        )

        with mock.patch.dict(os.environ, self.operator_env(), clear=False), mock.patch.object(
            conversation, "result_publish_pending", return_value=False
        ):
            snapshot = observability.operator_observability(campaign)

        self.assertTrue(snapshot["enabled"])
        self.assertTrue(snapshot["configured"])
        self.assertTrue(snapshot["running"])
        self.assertEqual(snapshot["active_request"]["workflow_id"], "workflow-42")
        self.assertEqual(snapshot["active_request"]["children_total"], 2)
        self.assertEqual(
            set(snapshot["active_request"]),
            {"workflow_id", "children_total"},
        )
        encoded = json.dumps(snapshot)
        self.assertNotIn("private bounded reasoning summary", encoded)
        self.assertNotIn("sensitive/path", encoded)
        self.assertNotIn("https://chatgpt.com/c/parent-observability", encoded)
        self.assertNotIn("host-ops", encoded)

    def test_request_error_does_not_publish_local_path(self) -> None:
        missing = self.root / "private" / "operator" / "request.json"
        campaign = conversation.RunningOperatorCampaign(
            request_id="missing-request",
            proc=SimpleNamespace(poll=lambda: None),
            started_at=0.0,
            request_path=missing,
            result_path=self.root / "result.json",
        )
        with mock.patch.dict(os.environ, self.operator_env(), clear=False), mock.patch.object(
            conversation, "result_publish_pending", return_value=False
        ):
            snapshot = observability.operator_observability(campaign)

        self.assertEqual(snapshot["active_request_error"], "operator_request_invalid")
        self.assertNotIn(str(self.root), json.dumps(snapshot))

    def test_bounded_dedupe_sample_never_walks_full_history(self) -> None:
        runs = agentd.STATE_DIR / "repositories" / "host-ops" / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        for index in range(observability.MAX_DEDUPE_EVIDENCE_FILES_PER_REPOSITORY + 20):
            (runs / f"run-{index:03d}.json").write_text(
                json.dumps({
                    "event": "duplicate_task_suppressed",
                    "duplicate_reason": "queued_duplicate",
                }) + "\n",
                encoding="utf-8",
            )

        snapshot = observability.dedupe_observability([self.repository])

        self.assertTrue(snapshot["scan_truncated"])
        self.assertLessEqual(
            snapshot["observed_run_records"],
            observability.MAX_DEDUPE_EVIDENCE_FILES_PER_REPOSITORY,
        )

    def test_future_heartbeat_timestamp_is_due(self) -> None:
        future = {
            "updated_at": "2999-01-01T00:00:00+00:00",
        }
        self.assertTrue(observability._heartbeat_due(future))

    def test_publication_is_change_driven_with_bounded_heartbeat(self) -> None:
        existing = {
            "schema_version": 1,
            "daemon": {"daemon_version": "4.20.6"},
            "operator": {"running": False},
            "dedupe": {"suppressed_count": 0},
            "updated_at": "2026-10-06T00:00:00+00:00",
        }
        status_path = self.control / observability.REMOTE_OPERATOR_STATUS
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(json.dumps(existing), encoding="utf-8")
        semantic = {key: value for key, value in existing.items() if key != "updated_at"}

        with mock.patch.object(
            observability, "build_operator_status", return_value=semantic
        ), mock.patch.object(
            observability, "_heartbeat_due", return_value=False
        ), mock.patch.object(agentd, "publish_control_json") as publish:
            self.assertFalse(
                observability.publish_operator_status(
                    [self.repository], None, max_workers=4
                )
            )
        publish.assert_not_called()

        with mock.patch.object(
            observability, "build_operator_status", return_value=semantic
        ), mock.patch.object(
            observability, "_heartbeat_due", return_value=True
        ), mock.patch.object(
            agentd, "now_iso", return_value="2026-10-06T00:05:00+00:00"
        ), mock.patch.object(
            agentd, "publish_control_json", return_value=True
        ) as publish:
            self.assertTrue(
                observability.publish_operator_status(
                    [self.repository], None, max_workers=4
                )
            )
        payload = publish.call_args.args[1]
        self.assertEqual(payload["updated_at"], "2026-10-06T00:05:00+00:00")
        self.assertEqual(
            publish.call_args.kwargs["timeout"],
            observability.OPERATOR_STATUS_PUBLISH_TIMEOUT_SECONDS,
        )
        self.assertEqual(publish.call_args.kwargs["attempts"], 1)


if __name__ == "__main__":
    unittest.main()
