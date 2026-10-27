from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_agent.development import operator_campaign


PARENT_URL = "https://chatgpt.com/c/6abfcdf9-8438-83eb-86da-1c4b209afc47"
CHILD_URL = "https://chatgpt.com/c/6ac04019-0698-83eb-8bcd-708b01bd08d8"
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
                "role": "research",
                "summary": "Audit the operator boundary.",
                "paths": ["local_agent/development/mvp_flow.py"],
            }
        ],
    }


def targeted_request_payload() -> dict:
    payload = request_payload()
    payload["schema_version"] = 2
    payload["repository_id"] = "example-project"
    return payload


def multirepo_request_payload() -> dict:
    payload = request_payload()
    payload["schema_version"] = 3
    payload["repository_ids"] = ["example-device", "example-project"]
    return payload


def completed_campaign() -> dict:
    return {
        "schema_version": 1,
        "status": "completed",
        "workflow_id": "operator-workflow-001",
        "workflow_state": "completed",
        "failures": {},
        "children": [
            {
                "request_id": "operator-child-001",
                "request_digest": "sha256:" + ("b" * 64),
                "child_conversation_url": CHILD_URL,
                "child_state": "retired",
                "assistant_identity": "assistant-1",
                "summary": "OPERATOR_SLICE_OK",
                "evidence_digest": DIGEST,
            }
        ],
    }


class OperatorCampaignTests(unittest.TestCase):
    def _paths(self, root: Path, payload: dict | None = None) -> tuple[Path, Path]:
        request_path = root / "request.json"
        result_path = root / "result.json"
        request_path.write_text(json.dumps(payload or request_payload()), encoding="utf-8")
        return request_path, result_path

    def test_runner_maps_request_to_existing_mvp_and_persists_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            request_path, result_path = self._paths(root)
            layout = object()
            with mock.patch.object(
                operator_campaign, "build_dev_lab_layout", return_value=layout
            ) as build_layout, mock.patch.object(
                operator_campaign, "run_mvp_campaign", return_value=completed_campaign()
            ) as run_campaign:
                result = operator_campaign.run_operator_campaign(
                    request_path=request_path,
                    result_path=result_path,
                    home=root / "home",
                    root=root / "lab",
                    checkout=root / "checkout",
                    production_checkout=root / "production",
                    login_timeout_seconds=12,
                    result_timeout_seconds=34,
                )

            build_layout.assert_called_once()
            args, kwargs = run_campaign.call_args
            self.assertIs(args[0], layout)
            self.assertEqual(args[1]["workflow_id"], "operator-workflow-001")
            self.assertNotIn("id", args[1])
            self.assertNotIn("identity_provider", kwargs)
            self.assertEqual(kwargs["login_timeout_seconds"], 12)
            self.assertEqual(kwargs["result_timeout_seconds"], 34)
            self.assertEqual(result["state"], "completed")
            self.assertEqual(result["children"][0]["summary"], "OPERATOR_SLICE_OK")
            self.assertEqual(json.loads(result_path.read_text(encoding="utf-8")), result)

    def test_repository_context_never_injects_execution_identity_provider(self) -> None:
        cases = (
            (targeted_request_payload(), "example-project"),
            (multirepo_request_payload(), "example-device, example-project"),
        )
        for payload, expected in cases:
            with self.subTest(schema_version=payload["schema_version"]):
                with tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    request_path, result_path = self._paths(root, payload)
                    layout = object()
                    with mock.patch.object(
                        operator_campaign, "build_dev_lab_layout", return_value=layout
                    ), mock.patch.object(
                        operator_campaign, "run_mvp_campaign", return_value=completed_campaign()
                    ) as run_campaign:
                        operator_campaign.run_operator_campaign(
                            request_path=request_path,
                            result_path=result_path,
                            home=root / "home",
                            root=root / "lab",
                            checkout=root / "checkout",
                            production_checkout=root / "production",
                        )

                    args, kwargs = run_campaign.call_args
                    self.assertNotIn("identity_provider", kwargs)
                    self.assertIn(
                        f"Reasoning repository context (not execution authority): {expected}. ",
                        args[1]["children"][0]["summary"],
                    )

    def test_persist_result_is_idempotent_for_exact_same_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            request_path, result_path = self._paths(root)
            with mock.patch.object(
                operator_campaign, "build_dev_lab_layout", return_value=object()
            ), mock.patch.object(
                operator_campaign, "run_mvp_campaign", return_value=completed_campaign()
            ):
                first = operator_campaign.run_operator_campaign(
                    request_path=request_path,
                    result_path=result_path,
                    home=root,
                    root=root / "lab",
                    checkout=root / "checkout",
                    production_checkout=root / "production",
                )
                second = operator_campaign.run_operator_campaign(
                    request_path=request_path,
                    result_path=result_path,
                    home=root,
                    root=root / "lab",
                    checkout=root / "checkout",
                    production_checkout=root / "production",
                )
            self.assertEqual(first, second)

    def test_persist_result_rejects_conflicting_same_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            request_path, result_path = self._paths(root)
            first_campaign = completed_campaign()
            second_campaign = completed_campaign()
            second_campaign["children"][0]["summary"] = "DIFFERENT"
            with mock.patch.object(
                operator_campaign, "build_dev_lab_layout", return_value=object()
            ), mock.patch.object(
                operator_campaign,
                "run_mvp_campaign",
                side_effect=[first_campaign, second_campaign],
            ):
                operator_campaign.run_operator_campaign(
                    request_path=request_path,
                    result_path=result_path,
                    home=root,
                    root=root / "lab",
                    checkout=root / "checkout",
                    production_checkout=root / "production",
                )
                with self.assertRaisesRegex(RuntimeError, "conflicts"):
                    operator_campaign.run_operator_campaign(
                        request_path=request_path,
                        result_path=result_path,
                        home=root,
                        root=root / "lab",
                        checkout=root / "checkout",
                        production_checkout=root / "production",
                    )

    def test_invalid_request_fails_before_campaign_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            request_path = root / "request.json"
            result_path = root / "result.json"
            payload = request_payload()
            payload["children"][0]["role"] = "review"
            request_path.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(operator_campaign, "run_mvp_campaign") as run_campaign:
                with self.assertRaisesRegex(ValueError, "operator child role"):
                    operator_campaign.run_operator_campaign(
                        request_path=request_path,
                        result_path=result_path,
                        home=root,
                        root=root / "lab",
                        checkout=root / "checkout",
                        production_checkout=root / "production",
                    )
            run_campaign.assert_not_called()
            self.assertFalse(result_path.exists())


if __name__ == "__main__":
    unittest.main()
