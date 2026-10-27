from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import local_agent.daemon.service as agentd
from local_agent.conversation import operator_queue
from local_agent.conversation.operator_contract import build_operator_result
from local_agent.foundation.process import LEASE_FDS_ENV, LEASE_KEYS_DIGEST_ENV, RESOURCE_LEASE_FDS_ENV
from local_agent.supervisor import conversation


PARENT_URL = "https://chatgpt.com/c/6abfcdf9-8438-83eb-86da-1c4b209afc47"
CHILD_URL = "https://chatgpt.com/c/6ac0456e-f1b0-83eb-ad85-5de71ce63530"
DIGEST = "sha256:" + ("a" * 64)


def request_payload() -> dict:
    return {
        "schema_version": 1,
        "id": "operator-request-001",
        "workflow_id": "operator-workflow-001",
        "parent_conversation_url": PARENT_URL,
        "children": [
            {
                "request_id": "operator-child-001",
                "node_id": "operator-node-001",
                "role": "verification",
                "summary": "Verify supervisor integration.",
                "paths": ["local_agent/supervisor/conversation.py"],
            }
        ],
    }


def completed_result(request: dict) -> dict:
    return build_operator_result(
        request,
        {
            "children": [
                {
                    "request_id": "operator-child-001",
                    "child_state": "retired",
                    "summary": "SUPERVISOR_OK",
                    "child_conversation_url": CHILD_URL,
                    "evidence_digest": DIGEST,
                }
            ]
        },
    )


def enabled_env(root: Path) -> dict[str, str]:
    return {
        conversation.OPERATOR_ENABLED_ENV: "1",
        conversation.OPERATOR_HOME_ENV: str(root / "home"),
        conversation.OPERATOR_ROOT_ENV: str(root / "lab"),
        conversation.OPERATOR_CHECKOUT_ENV: str(root / "checkout"),
        conversation.OPERATOR_PRODUCTION_CHECKOUT_ENV: str(root / "production"),
    }


class _FakeProc:
    def __init__(self, return_code=None) -> None:
        self.pid = 1234
        self.return_code = return_code

    def poll(self):
        return self.return_code


class SupervisorConversationTests(unittest.TestCase):
    def test_runtime_config_is_disabled_by_default_and_fail_closed_when_incomplete(self) -> None:
        self.assertIsNone(conversation.runtime_config({}))
        with self.assertRaisesRegex(ValueError, "missing"):
            conversation.runtime_config({conversation.OPERATOR_ENABLED_ENV: "1"})

    def test_runtime_config_requires_dev_checkout_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = enabled_env(root)
            env[conversation.OPERATOR_CHECKOUT_ENV] = env[
                conversation.OPERATOR_PRODUCTION_CHECKOUT_ENV
            ]
            with self.assertRaisesRegex(ValueError, "isolated"):
                conversation.runtime_config(env)

    def test_service_control_plane_stages_remote_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = root / "control"
            state = root / "state"
            request_dir = control / operator_queue.CONTROL_REQUEST_DIR
            request_dir.mkdir(parents=True)
            request = request_payload()
            (request_dir / f"{request['id']}.json").write_text(
                json.dumps(request), encoding="utf-8"
            )
            env = enabled_env(root)
            with mock.patch.dict(conversation.os.environ, env, clear=True), mock.patch.object(
                agentd.core, "CONTROL", control
            ), mock.patch.object(agentd, "STATE_DIR", state):
                conversation.service_control_plane()
            staged = operator_queue.next_staged_request(state)
            self.assertIsNotNone(staged)
            self.assertEqual(staged.request_id if staged else None, request["id"])

    def test_service_control_plane_discards_spool_only_after_fresh_origin_proof(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = root / "control"
            state = root / "state"
            request_dir = control / operator_queue.CONTROL_REQUEST_DIR
            request_dir.mkdir(parents=True)
            request = request_payload()
            request_path = request_dir / f"{request['id']}.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            item = operator_queue.stage_next_control_request(control, state)
            assert item is not None
            self.assertTrue(operator_queue.reserve_launch_once(state, item))
            result = completed_result(request)
            operator_queue.persist_spooled_result(item.result_path, request, result)
            env = enabled_env(root)

            def publish_side_effect(*_args, **kwargs):
                kwargs["post_pull_validate"]()
                return True

            with mock.patch.dict(conversation.os.environ, env, clear=True), mock.patch.object(
                agentd.core, "CONTROL", control
            ), mock.patch.object(agentd, "STATE_DIR", state), mock.patch.object(
                conversation, "_require_remote_request_match"
            ) as remote_request, mock.patch.object(
                conversation, "_remote_result_matches", side_effect=[False, True]
            ) as remote_result, mock.patch.object(
                conversation, "_refresh_remote_control"
            ) as refresh, mock.patch.object(
                agentd, "publish_control_json", side_effect=publish_side_effect
            ) as publish:
                conversation.service_control_plane()

            self.assertEqual(remote_result.call_count, 2)
            self.assertEqual(remote_request.call_count, 2)
            refresh.assert_called_once_with()
            kwargs = publish.call_args.kwargs
            self.assertTrue(kwargs["ensure_remote"])
            self.assertTrue(callable(kwargs["post_pull_validate"]))
            self.assertFalse(item.request_path.exists())
            self.assertFalse(item.result_path.exists())
            self.assertFalse(operator_queue.launch_reconciliation_required(state, item))

    def test_service_control_plane_preserves_spool_when_origin_proof_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = root / "control"
            state = root / "state"
            request_dir = control / operator_queue.CONTROL_REQUEST_DIR
            request_dir.mkdir(parents=True)
            request = request_payload()
            (request_dir / f"{request['id']}.json").write_text(
                json.dumps(request), encoding="utf-8"
            )
            item = operator_queue.stage_next_control_request(control, state)
            assert item is not None
            self.assertTrue(operator_queue.reserve_launch_once(state, item))
            result = completed_result(request)
            operator_queue.persist_spooled_result(item.result_path, request, result)
            env = enabled_env(root)
            with mock.patch.dict(conversation.os.environ, env, clear=True), mock.patch.object(
                agentd.core, "CONTROL", control
            ), mock.patch.object(agentd, "STATE_DIR", state), mock.patch.object(
                conversation, "_require_remote_request_match"
            ), mock.patch.object(
                conversation, "_remote_result_matches", side_effect=[False, False]
            ), mock.patch.object(
                conversation, "_refresh_remote_control"
            ), mock.patch.object(
                agentd, "publish_control_json", return_value=True
            ):
                with self.assertRaisesRegex(RuntimeError, "not confirmed on origin"):
                    conversation.service_control_plane()
            self.assertTrue(item.request_path.exists())
            self.assertTrue(item.result_path.exists())
            self.assertTrue(operator_queue.launch_reconciliation_required(state, item))

    def test_service_control_plane_refreshes_even_when_cached_remote_result_matches(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = root / "control"
            state = root / "state"
            request_dir = control / operator_queue.CONTROL_REQUEST_DIR
            request_dir.mkdir(parents=True)
            request = request_payload()
            (request_dir / f"{request['id']}.json").write_text(
                json.dumps(request), encoding="utf-8"
            )
            item = operator_queue.stage_next_control_request(control, state)
            assert item is not None
            self.assertTrue(operator_queue.reserve_launch_once(state, item))
            result = completed_result(request)
            operator_queue.persist_spooled_result(item.result_path, request, result)
            env = enabled_env(root)
            with mock.patch.dict(conversation.os.environ, env, clear=True), mock.patch.object(
                agentd.core, "CONTROL", control
            ), mock.patch.object(agentd, "STATE_DIR", state), mock.patch.object(
                conversation, "_require_remote_request_match"
            ) as remote_request, mock.patch.object(
                conversation, "_remote_result_matches", side_effect=[True, True]
            ) as remote_result, mock.patch.object(
                conversation, "_refresh_remote_control"
            ) as refresh, mock.patch.object(
                agentd, "publish_control_json"
            ) as publish:
                conversation.service_control_plane()
            publish.assert_not_called()
            refresh.assert_called_once_with()
            self.assertEqual(remote_request.call_count, 2)
            self.assertEqual(remote_result.call_count, 2)
            self.assertFalse(item.request_path.exists())
            self.assertFalse(item.result_path.exists())
            self.assertFalse(operator_queue.launch_reconciliation_required(state, item))

    def test_service_control_plane_preserves_cached_match_spool_when_fresh_origin_disagrees(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = root / "control"
            state = root / "state"
            request_dir = control / operator_queue.CONTROL_REQUEST_DIR
            request_dir.mkdir(parents=True)
            request = request_payload()
            (request_dir / f"{request['id']}.json").write_text(
                json.dumps(request), encoding="utf-8"
            )
            item = operator_queue.stage_next_control_request(control, state)
            assert item is not None
            self.assertTrue(operator_queue.reserve_launch_once(state, item))
            result = completed_result(request)
            operator_queue.persist_spooled_result(item.result_path, request, result)
            env = enabled_env(root)
            with mock.patch.dict(conversation.os.environ, env, clear=True), mock.patch.object(
                agentd.core, "CONTROL", control
            ), mock.patch.object(agentd, "STATE_DIR", state), mock.patch.object(
                conversation, "_require_remote_request_match"
            ), mock.patch.object(
                conversation, "_remote_result_matches", side_effect=[True, False]
            ), mock.patch.object(
                conversation, "_refresh_remote_control"
            ), mock.patch.object(
                agentd, "publish_control_json"
            ) as publish:
                with self.assertRaisesRegex(RuntimeError, "not confirmed on origin"):
                    conversation.service_control_plane()
            publish.assert_not_called()
            self.assertTrue(item.request_path.exists())
            self.assertTrue(item.result_path.exists())
            self.assertTrue(operator_queue.launch_reconciliation_required(state, item))

    def test_start_if_pending_strips_inherited_execution_leases(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "state"
            request_path = state / "request.json"
            result_path = state / "result.json"
            request_path.parent.mkdir(parents=True)
            request_path.write_text(json.dumps(request_payload()), encoding="utf-8")
            item = operator_queue.OperatorWorkItem(
                request_id="operator-request-001",
                request_digest=DIGEST,
                request_path=request_path,
                result_path=result_path,
            )
            env = enabled_env(root)
            env.update(
                {
                    LEASE_FDS_ENV: "1,2",
                    LEASE_KEYS_DIGEST_ENV: "sha256:" + ("b" * 64),
                    RESOURCE_LEASE_FDS_ENV: "3,4",
                }
            )
            fake = _FakeProc()
            with mock.patch.dict(conversation.os.environ, env, clear=True), mock.patch.object(
                agentd, "STATE_DIR", state
            ), mock.patch.object(
                operator_queue, "next_staged_request", return_value=item
            ), mock.patch.object(
                conversation, "popen_registered", return_value=fake
            ) as spawn, mock.patch.object(
                conversation, "repository_root", return_value=root
            ):
                slot = conversation.start_if_pending()
            self.assertIsNotNone(slot)
            kwargs = spawn.call_args.kwargs
            self.assertNotIn(LEASE_FDS_ENV, kwargs["env"])
            self.assertNotIn(LEASE_KEYS_DIGEST_ENV, kwargs["env"])
            self.assertNotIn(RESOURCE_LEASE_FDS_ENV, kwargs["env"])
            self.assertTrue(kwargs["start_new_session"])

    def test_remote_result_match_requires_fetched_origin_content(self) -> None:
        request = request_payload()
        result = completed_result(request)
        with tempfile.TemporaryDirectory() as temp:
            control = Path(temp)
            with mock.patch.object(agentd.core, "CONTROL", control), mock.patch.object(
                agentd.core, "CONTROL_BRANCH", "agent-control"
            ), mock.patch.object(
                agentd.core,
                "process",
                return_value={"exit_code": 0, "output": json.dumps(result)},
            ) as process:
                self.assertTrue(conversation._remote_result_matches(request, result))
            self.assertIn(
                "refs/remotes/origin/agent-control:.agent/conversation/results/operator-request-001.json",
                process.call_args.args[0],
            )

    def test_reap_interrupted_by_disable_preserves_request_for_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            request_path = root / "request.json"
            result_path = root / "result.json"
            request_path.write_text(json.dumps(request_payload()), encoding="utf-8")
            slot = conversation.RunningOperatorCampaign(
                request_id="operator-request-001",
                proc=_FakeProc(return_code=143),
                started_at=conversation.time.monotonic(),
                request_path=request_path,
                result_path=result_path,
            )
            with mock.patch.object(conversation, "unregister_process"):
                self.assertIsNone(conversation.reap(slot, interrupted=True))
            self.assertTrue(request_path.is_file())
            self.assertFalse(result_path.exists())

    def test_result_publish_pending_tracks_local_terminal_spool(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "state"
            requests = state / operator_queue.LOCAL_SPOOL_DIR / "requests"
            results = state / operator_queue.LOCAL_SPOOL_DIR / "results"
            requests.mkdir(parents=True)
            results.mkdir(parents=True)
            request = request_payload()
            (requests / f"{request['id']}.json").write_text(
                json.dumps(request), encoding="utf-8"
            )
            (results / f"{request['id']}.json").write_text(
                json.dumps(completed_result(request)), encoding="utf-8"
            )
            with mock.patch.dict(
                conversation.os.environ, enabled_env(root), clear=True
            ), mock.patch.object(agentd, "STATE_DIR", state):
                self.assertTrue(conversation.result_publish_pending())

    def test_reap_persists_failure_if_worker_exits_without_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            request_path = root / "request.json"
            result_path = root / "result.json"
            request_path.write_text(json.dumps(request_payload()), encoding="utf-8")
            slot = conversation.RunningOperatorCampaign(
                request_id="operator-request-001",
                proc=_FakeProc(return_code=7),
                started_at=conversation.time.monotonic(),
                request_path=request_path,
                result_path=result_path,
            )
            with mock.patch.object(conversation, "unregister_process"):
                self.assertIsNone(conversation.reap(slot))
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(result["state"], "failed")
            self.assertIn("exited 7", result["children"][0]["error"])


if __name__ == "__main__":
    unittest.main()
