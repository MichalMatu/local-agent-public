from __future__ import annotations

import copy
import hashlib
import unittest

from local_agent.conversation import contract, github_fabric_dispatch, operator_contract


PARENT = "https://chatgpt.com/c/6ac6840c-eaec-83ed-bde2-6971e9ffa220"
BINDING = "b0e94f93-478f-4fc6-abed-159d3702362f"


def operator_request() -> dict:
    return {
        "schema_version": operator_contract.OPERATOR_REQUEST_SCHEMA_VERSION,
        "id": "operator-request-001",
        "workflow_id": "workflow-001",
        "parent_conversation_url": PARENT,
        "repository_ids": ["local-agent"],
        "children": [
            {
                "request_id": "child-research",
                "node_id": "research",
                "role": "research",
                "summary": "Audit the bounded transport.",
                "paths": ["chat_bridge"],
            },
            {
                "request_id": "child-verify",
                "node_id": "verify",
                "role": "verification",
                "summary": "Verify the bounded transport.",
                "paths": ["local_agent/conversation"],
            },
        ],
    }


def child_request(request_id: str, node_id: str, role: str, summary: str, paths: list[str]) -> dict:
    request = {
        "schema_version": contract.CHILD_REQUEST_SCHEMA_VERSION,
        "id": request_id,
        "workflow_id": "workflow-001",
        "workflow_node_id": node_id,
        "workflow_node_revision": 0,
        "workflow_node_introduction_digest": "sha256:" + "1" * 64,
        "parent_conversation_url": PARENT,
        "created_at": "2026-10-07T20:00:00Z",
        "role": role,
        "repository_id": "local-agent",
        "agent_binding": BINDING,
        "repository_ref": "main",
        "repository_commit_sha": "a" * 40,
        "scope": {"summary": summary, "paths": paths},
        "context_refs": [],
        "bootstrap_contract_version": contract.BOOTSTRAP_CONTRACT_VERSION,
    }
    contract.validate_child_request(request)
    return request


def admitted_children() -> list[dict]:
    return [
        child_request(
            "child-research",
            "research",
            "research",
            "Audit the bounded transport.",
            ["chat_bridge"],
        ),
        child_request(
            "child-verify",
            "verify",
            "verification",
            "Verify the bounded transport.",
            ["local_agent/conversation"],
        ),
    ]


class GithubFabricDispatchTests(unittest.TestCase):
    def test_build_is_deterministic_and_projects_exact_existing_spawn_evidence(self) -> None:
        request = operator_request()
        children = admitted_children()

        first = github_fabric_dispatch.build_github_fabric_dispatch(request, children)
        second = github_fabric_dispatch.build_github_fabric_dispatch(
            copy.deepcopy(request),
            list(reversed(copy.deepcopy(children))),
        )

        self.assertEqual(first, second)
        github_fabric_dispatch.validate_github_fabric_dispatch(first)
        self.assertEqual(first["request_id"], request["id"])
        self.assertEqual(
            first["request_digest"],
            operator_contract.operator_request_digest(request),
        )
        self.assertRegex(first["campaign_id"], r"^cf-[0-9a-f]{16}$")
        self.assertEqual(
            [child["request_id"] for child in first["children"]],
            ["child-research", "child-verify"],
        )
        self.assertTrue(
            all(child["spawn"]["transaction_id"].startswith("spawn-") for child in first["children"])
        )
        self.assertTrue(
            all(
                child["spawn"]["child_request_digest"].startswith("sha256:")
                for child in first["children"]
            )
        )
        self.assertTrue(
            all(
                child["spawn"]["bootstrap_digest"].startswith("sha256:")
                for child in first["children"]
            )
        )
        self.assertTrue(
            all(
                child["spawn"]["bootstrap_text"].startswith("LOCAL AGENT CHILD BOOTSTRAP\n")
                for child in first["children"]
            )
        )

    def test_private_bootstrap_publishes_via_github_without_dom_terminal_marker(self) -> None:
        dispatch = github_fabric_dispatch.build_github_fabric_dispatch(
            operator_request(), admitted_children()
        )
        for child in dispatch["children"]:
            message = child["spawn"]["bootstrap_text"]
            self.assertNotIn("LOCAL_AGENT_CF_CHILD_COMPLETE", message)
            self.assertIn("connected GitHub Connector", message)
            self.assertIn("Chat Bridge will not read your answer", message)
            self.assertIn(f"/results/{dispatch['id']}/{child['request_id']}.json", message)
            self.assertIn(child["spawn"]["transaction_id"], message)
            self.assertEqual(
                child["spawn"]["bootstrap_digest"],
                "sha256:" + hashlib.sha256(message.encode("utf-8")).hexdigest(),
            )

    def test_dispatch_has_no_browser_runtime_identity_or_top_level_execution_authority(self) -> None:
        dispatch = github_fabric_dispatch.build_github_fabric_dispatch(
            operator_request(),
            admitted_children(),
        )

        self.assertNotIn("tab_id", dispatch)
        for child in dispatch["children"]:
            self.assertNotIn("tab_id", child["spawn"])
            self.assertNotIn("agent_binding", child)
            self.assertNotIn("repository_id", child)
            self.assertNotIn("scheduler_resources", child)

    def test_same_id_changed_payload_is_a_permanent_conflict(self) -> None:
        dispatch = github_fabric_dispatch.build_github_fabric_dispatch(
            operator_request(),
            admitted_children(),
        )
        self.assertEqual(
            github_fabric_dispatch.reconcile_github_fabric_dispatch(dispatch, copy.deepcopy(dispatch)),
            dispatch,
        )

        changed = copy.deepcopy(dispatch)
        changed["children"][0]["spawn"]["bootstrap_text"] += "\nchanged"
        with self.assertRaisesRegex(ValueError, "same-id dispatch conflict"):
            github_fabric_dispatch.reconcile_github_fabric_dispatch(dispatch, changed)

    def test_admitted_children_must_match_operator_request_exactly(self) -> None:
        request = operator_request()
        children = admitted_children()
        children[0]["role"] = "integration"

        with self.assertRaisesRegex(ValueError, "role does not match"):
            github_fabric_dispatch.build_github_fabric_dispatch(request, children)

    def test_missing_or_extra_child_is_rejected(self) -> None:
        request = operator_request()
        children = admitted_children()

        with self.assertRaisesRegex(ValueError, "do not match operator request"):
            github_fabric_dispatch.build_github_fabric_dispatch(request, children[:1])

        extra = child_request(
            "child-extra",
            "extra",
            "integration",
            "Extra child.",
            ["docs"],
        )
        with self.assertRaisesRegex(ValueError, "do not match operator request"):
            github_fabric_dispatch.build_github_fabric_dispatch(request, children + [extra])


class GithubFabricSyntheticPublicationTests(unittest.TestCase):
    HEAD = "b" * 40

    def _plan(self, **changes):
        from local_agent.conversation import github_fabric_publication

        arguments = {
            "existing_record": None,
            "existing_index": None,
            "expected_head_sha": self.HEAD,
            "enabled": True,
            "writer_authorized": True,
        }
        arguments.update(changes)
        return github_fabric_publication.preflight_synthetic_publication(
            operator_request(), admitted_children(), **arguments
        )

    def test_disabled_by_default_and_permission_denied(self) -> None:
        from local_agent.conversation import github_fabric_publication

        with self.assertRaises(PermissionError):
            github_fabric_publication.preflight_synthetic_publication(
                operator_request(), admitted_children(),
                existing_record=None, existing_index=None, expected_head_sha=self.HEAD
            )
        with self.assertRaises(PermissionError):
            self._plan(writer_authorized=False)
        with self.assertRaises(PermissionError):
            self._plan(enabled=False)

    def test_exact_fixture_only_rejects_private_content_and_forged_identity(self) -> None:
        from local_agent.conversation import github_fabric_publication

        for field, value in [("workflow_id", "secret-workflow"), ("id", "forged-id")]:
            altered = operator_request()
            altered[field] = value
            with self.subTest(field=field), self.assertRaises(PermissionError):
                github_fabric_publication.preflight_synthetic_publication(
                    altered, admitted_children(), existing_record=None,
                    existing_index=None, expected_head_sha=self.HEAD,
                    enabled=True, writer_authorized=True
                )

        altered_children = admitted_children()
        altered_children[0]["scope"]["summary"] = "Private client task"
        with self.assertRaises(PermissionError):
            github_fabric_publication.preflight_synthetic_publication(
                operator_request(), altered_children, existing_record=None,
                existing_index=None, expected_head_sha=self.HEAD,
                enabled=True, writer_authorized=True
            )

    def test_proposes_record_before_index_and_replays_after_restart(self) -> None:
        from local_agent.conversation import github_fabric_publication

        first = self._plan()
        self.assertEqual(first.operation, "create_record")
        self.assertEqual(
            first.path,
            github_fabric_publication.RECORD_ROOT + first.payload["id"] + ".json"
        )
        self.assertEqual(first.expected_head_sha, self.HEAD)
        self.assertEqual(first.payload, github_fabric_dispatch.build_github_fabric_dispatch(
            operator_request(), admitted_children()
        ))

        # Lost success acknowledgement: only a fresh view of the immutable record
        # advances to index publication, without proposing a second record.
        second = self._plan(existing_record=first.payload, expected_head_sha="c" * 40)
        self.assertEqual(second.operation, "update_index")
        self.assertEqual(second.path, github_fabric_publication.INDEX_PATH)
        self.assertEqual(second.payload, {
            "schema_version": 1, "dispatch_ids": [first.payload["id"]]
        })
        self.assertEqual(second.expected_head_sha, "c" * 40)

        # A lost index acknowledgement or process restart is a replay/no-op.
        final = self._plan(
            existing_record=first.payload,
            existing_index=second.payload,
            expected_head_sha="d" * 40
        )
        self.assertEqual(final.operation, "replay")
        self.assertIsNone(final.path)
        self.assertIsNone(final.payload)

    def test_order_independent_fixture_and_no_overwrite(self) -> None:
        from local_agent.conversation import github_fabric_publication

        first = self._plan()
        second = github_fabric_publication.preflight_synthetic_publication(
            copy.deepcopy(operator_request()), list(reversed(admitted_children())),
            existing_record=None, existing_index=None, expected_head_sha=self.HEAD,
            enabled=True, writer_authorized=True
        )
        self.assertEqual(first, second)
        changed = copy.deepcopy(first.payload)
        changed["children"][0]["spawn"]["bootstrap_text"] += "\nmodified"
        with self.assertRaisesRegex(ValueError, "same-id dispatch conflict"):
            self._plan(existing_record=changed)
        with self.assertRaisesRegex(ValueError, "missing immutable record"):
            self._plan(existing_index={"schema_version": 1, "dispatch_ids": [first.payload["id"]]})

    def test_index_capacity_duplicate_and_malformed_fail_closed(self) -> None:
        first = self._plan()
        full = {"schema_version": 1, "dispatch_ids": [
            "fabric-" + c * 32 for c in "abcd"
        ]}
        with self.assertRaisesRegex(ValueError, "capacity exhausted"):
            self._plan(existing_record=first.payload, existing_index=full)
        with self.assertRaisesRegex(ValueError, "capacity exhausted"):
            self._plan(existing_record=None, existing_index=full)
        for index in [
            {"schema_version": True, "dispatch_ids": []},
            {"schema_version": 1, "dispatch_ids": ["fabric-" + "a" * 32] * 2},
            {"schema_version": 1, "dispatch_ids": ["../escape"]},
            {"schema_version": 1, "dispatch_ids": [0]},
            {"schema_version": 1, "dispatch_ids": [] , "url": "https://example.invalid"},
            {"schema_version": 1, "dispatch_ids": ["fabric-" + c * 32 for c in "abcde"]},
        ]:
            with self.subTest(index=index), self.assertRaises(ValueError):
                self._plan(existing_index=index)

    def test_missing_or_unverified_remote_head_fails_closed(self) -> None:
        for head in ["", "main", "A" * 40, "a" * 39, None]:
            with self.subTest(head=head), self.assertRaises(ValueError):
                self._plan(expected_head_sha=head)




if __name__ == "__main__":
    unittest.main()
