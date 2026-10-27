"""Trusted operator-result publication boundary and durable spool safety."""

from __future__ import annotations

import inspect
import unittest
from pathlib import Path
from unittest import mock

from local_agent.conversation.operator_queue import OperatorWorkItem
from local_agent.supervisor import conversation


class OperatorPublisherInterfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.item = OperatorWorkItem(
            request_id="operator-request-001",
            request_digest="sha256:" + "1" * 64,
            request_path=Path("/nonexistent/requests/operator-request-001.json"),
            result_path=Path("/nonexistent/results/operator-request-001.json"),
        )
        self.request = {"id": self.item.request_id}
        self.result = {"id": self.item.request_id, "synthetic": True}

    def _run(self, remote_matches):
        patches = [
            mock.patch.object(conversation, "runtime_config", return_value=object()),
            mock.patch.object(
                conversation.operator_queue,
                "next_spooled_result",
                return_value=(self.item, self.request, self.result),
            ),
            mock.patch.object(
                conversation.operator_queue, "require_control_request_match"
            ),
            mock.patch.object(
                conversation.operator_queue, "control_result_matches"
            ),
            mock.patch.object(conversation, "_require_remote_request_match"),
            mock.patch.object(conversation, "_remote_result_matches", side_effect=remote_matches),
            mock.patch.object(conversation, "_refresh_remote_control"),
            mock.patch.object(conversation.operator_queue, "discard_spool"),
            mock.patch.object(
                conversation.agentd, "publish_control_json",
                side_effect=lambda *args, **kwargs: kwargs["post_pull_validate"](),
            ),
            mock.patch.object(
                conversation.agentd.core, "publish_control_json",
                side_effect=AssertionError("wrong legacy publisher"),
            ),
        ]
        mocks = []
        try:
            for patch in patches:
                mocks.append(patch.start())
            result = conversation.publish_pending_result_only()
            return result, mocks
        finally:
            for patch in reversed(patches):
                patch.stop()

    def test_trusted_writer_supports_required_remote_guard(self):
        signature = inspect.signature(conversation.agentd.publish_control_json)
        self.assertIn("ensure_remote", signature.parameters)
        self.assertIn("post_pull_validate", signature.parameters)

    def test_result_publication_uses_trusted_writer_and_rechecks_origin(self):
        actual, mocks = self._run([False, True])
        self.assertIs(actual, True)
        writer = mocks[-2]
        legacy = mocks[-1]
        legacy.assert_not_called()
        writer.assert_called_once()
        args, kwargs = writer.call_args
        self.assertEqual(args[0], ".agent/conversation/results/operator-request-001.json")
        self.assertEqual(args[1], self.result)
        self.assertEqual(kwargs["ensure_remote"], True)
        self.assertEqual(
            kwargs["commit_message"], "Conversation result: operator-request-001"
        )
        self.assertEqual(mocks[2].call_count, 2)
        mocks[6].assert_called_once()
        mocks[7].assert_called_once_with(self.item)

    def test_existing_remote_result_is_idempotent_without_new_publish(self):
        actual, mocks = self._run([True, True])
        self.assertIs(actual, True)
        mocks[-2].assert_not_called()
        mocks[7].assert_called_once_with(self.item)

    def test_unconfirmed_remote_result_preserves_local_spool(self):
        with self.assertRaisesRegex(RuntimeError, "not confirmed on origin"):
            self._run([False, False])


if __name__ == "__main__":
    unittest.main()
