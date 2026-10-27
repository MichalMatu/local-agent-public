from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from local_agent.repository.context import RepositoryContext
from local_agent.repository.host_routing import (
    HostEnrollment,
    HostIdentity,
    build_host_status,
    canonical_host_id,
    host_record_path,
    host_status_is_fresh,
    host_status_observation,
    parse_host_status_record,
    host_task_path,
    parse_operator_host_registry,
    require_host_task_target,
    validate_host_enrollments,
)

HOST_INSTANCE = '550e8400-e29b-41d4-a716-446655440000'
ANOTHER_INSTANCE = '550e8400-e29b-41d4-a716-446655440001'
BINDING = 'b0e94f93-478f-4fc6-abed-159d3702362f'
OTHER_BINDING = 'fe99a852-0486-4b3a-b09b-5d295e9355a8'


class HostRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identity = HostIdentity('worker-mac', HOST_INSTANCE)
        self.enrollment = HostEnrollment.from_operator_record({
            'version': 1, 'host_id': 'worker-mac', 'installation_id': HOST_INSTANCE,
            'enabled': True, 'repositories': [{'repository_id': 'local-agent', 'agent_binding': BINDING}],
        })
        self.repository = RepositoryContext(
            repository_id='local-agent', repository='example-owner/local-agent',
            control=Path('/control'), work=Path('/work'), checkpoints=Path('/checkpoints'),
            agent_binding=BINDING,
        )
        self.task = {
            'id': 'routine-001', 'target_host_id': 'worker-mac',
            'target_installation_id': HOST_INSTANCE, 'target_repository_id': 'local-agent',
            'agent_binding': BINDING,
        }

    def test_safe_namespaced_paths(self) -> None:
        self.assertEqual(host_record_path('worker-mac', 'system.json'),
                         PurePosixPath('.agent/hosts/worker-mac/system.json'))
        self.assertEqual(host_task_path('worker-mac', 'routine-001'),
                         PurePosixPath('.agent/hosts/worker-mac/tasks/routine-001.json'))
        self.assertEqual(host_task_path('worker-mac', 'routine-001', result=True),
                         PurePosixPath('.agent/hosts/worker-mac/results/routine-001.json'))
        for unsafe in ('../evil', 'a', 'UPPER', 'worker_mac', '-bad', 'bad/name', 'bad..id', 'łódź'):
            with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                canonical_host_id(unsafe)
        with self.assertRaises(ValueError):
            host_task_path('worker-mac', '../task')
        with self.assertRaises(ValueError):
            host_record_path('worker-mac', 'private-key.json')

    def test_identity_requires_supported_version_and_uuid(self) -> None:
        self.assertEqual(HostIdentity.from_record({
            'version': 1, 'host_id': 'worker-mac', 'installation_id': HOST_INSTANCE,
        }), self.identity)
        for broken in ({'version': True, 'host_id': 'worker-mac', 'installation_id': HOST_INSTANCE},
                       {'version': 2, 'host_id': 'worker-mac', 'installation_id': HOST_INSTANCE},
                       {'version': 1, 'host_id': 'worker-mac', 'installation_id': 'secret'},
                       self.identity.as_record() | {'permissions': 'unrestricted'}):
            with self.subTest(broken=broken), self.assertRaises(ValueError):
                HostIdentity.from_record(broken)

    def test_explicit_target_and_two_identities_required(self) -> None:
        require_host_task_target(self.task, local_identity=self.identity,
                                 enrollment=self.enrollment, repository=self.repository)
        for change in (
            {'target_host_id': 'worker-pc'}, {'target_host_id': None},
            {'target_installation_id': ANOTHER_INSTANCE}, {'target_installation_id': None},
            {'target_repository_id': 'example-project'}, {'agent_binding': OTHER_BINDING},
            {'agent_binding': None}, {'id': '../escape'},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                require_host_task_target(self.task | change, local_identity=self.identity,
                                         enrollment=self.enrollment, repository=self.repository)

    def test_disabled_reenrolled_and_unauthorized_repository_rejected(self) -> None:
        for record in (
            {'enabled': False},
            {'installation_id': ANOTHER_INSTANCE},
            {'repositories': []},
            {'repositories': [{'repository_id': 'local-agent', 'agent_binding': OTHER_BINDING}]},
        ):
            baseline = {
                'version': 1, 'host_id': 'worker-mac', 'installation_id': HOST_INSTANCE,
                'enabled': True, 'repositories': [{'repository_id': 'local-agent', 'agent_binding': BINDING}],
            }
            enrollment = HostEnrollment.from_operator_record(baseline | record)
            with self.subTest(record=record), self.assertRaises(ValueError):
                require_host_task_target(self.task, local_identity=self.identity,
                                         enrollment=enrollment, repository=self.repository)

    def test_duplicate_enrollment_ids_and_malformed_authority_rejected(self) -> None:
        base = {
            'version': 1, 'host_id': 'worker-mac', 'installation_id': HOST_INSTANCE,
            'enabled': True, 'repositories': [{'repository_id': 'local-agent', 'agent_binding': BINDING}],
        }
        for change in ({'enabled': 'true'}, {'version': True},
                       {'repositories': [{'repository_id': 'local-agent', 'agent_binding': BINDING},
                                         {'repository_id': 'LOCAL-AGENT', 'agent_binding': BINDING}]},
                       {'repositories': [{'repository_id': 'local-agent', 'agent_binding': 'random'}]},
                       {'repositories': [{}]}, {'repositories': [None]},
                       {'permissions': 'unrestricted'},
                       {'repositories': [{'repository_id': 'local-agent',
                                          'agent_binding': BINDING,
                                          'allow_all': True}]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                HostEnrollment.from_operator_record(base | change)

    def test_enrollment_registry_blocks_collisions_without_distributed_fencing(self) -> None:
        first = {
            'version': 1, 'host_id': 'worker-mac', 'installation_id': HOST_INSTANCE,
            'enabled': True, 'repositories': [{'repository_id': 'local-agent', 'agent_binding': BINDING}],
        }
        second = {
            'version': 1, 'host_id': 'worker-pc', 'installation_id': ANOTHER_INSTANCE,
            'enabled': True, 'repositories': [{'repository_id': 'example-project', 'agent_binding': OTHER_BINDING}],
        }
        self.assertEqual(len(validate_host_enrollments([first, second])), 2)
        for duplicate in (
            second | {'host_id': 'worker-mac'},
            second | {'installation_id': HOST_INSTANCE},
            second | {'repositories': [{'repository_id': 'local-agent', 'agent_binding': BINDING}]},
            second | {'repositories': [{'repository_id': 'LOCAL-AGENT', 'agent_binding': OTHER_BINDING}]},
        ):
            with self.subTest(duplicate=duplicate), self.assertRaises(ValueError):
                validate_host_enrollments([first, duplicate])
        self.assertEqual(len(validate_host_enrollments([
            first, second | {'enabled': False, 'repositories': [{'repository_id': 'local-agent', 'agent_binding': BINDING}]},
        ])), 2)

    def test_operator_manifest_is_explicit_and_fail_closed(self) -> None:
        host = {
            'version': 1, 'host_id': 'worker-mac', 'installation_id': HOST_INSTANCE,
            'enabled': True, 'repositories': [{'repository_id': 'local-agent', 'agent_binding': BINDING}],
        }
        manifest = json.dumps({'version': 1, 'hosts': [host]}).encode()
        parsed = parse_operator_host_registry(manifest)
        self.assertEqual(parsed[0].identity, self.identity)
        for invalid in (
            b'', b'not-json', b'\xff', b'[]',
            b'{"version":1,"hosts":[],"version":1}',
            b'{"version":true,"hosts":[]}',
            b'{"version":1,"hosts":[],"extra":1}',
            b'{"version":1,"hosts":[{"version":1,"host_id":"a-b","installation_id":"bad","enabled":true,"repositories":[]}]}' ,
            b'{"version":1,"hosts":[],"value":NaN}',
            b' ' * (64 * 1024 + 1),
        ):
            with self.subTest(invalid=invalid[:64]), self.assertRaises(ValueError):
                parse_operator_host_registry(invalid)
        with self.assertRaises(ValueError):
            parse_operator_host_registry(None)

    def test_bounded_status_record_roundtrip(self) -> None:
        now = datetime(2026, 10, 12, 17, 0, tzinfo=timezone.utc)
        output = build_host_status(self.identity, source_revision='a' * 40,
                                   state='idle', observed_at=now)
        self.assertTrue(host_status_is_fresh(output, local_identity=self.identity, now=now))
        self.assertEqual(HostIdentity.from_record(self.identity.as_record()), self.identity)
        for invalid in (
            {'source_revision': 'main'}, {'state': 'online'},
            {'observed_at': datetime(2026, 10, 12, 17, 0)},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                build_host_status(self.identity, **({
                    'source_revision': 'a' * 40, 'state': 'idle', 'observed_at': now,
                } | invalid))

    def test_host_status_schema_is_exact_and_bounded(self) -> None:
        now = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
        baseline = build_host_status(
            self.identity, source_revision='a' * 40,
            state='idle', observed_at=now,
        )
        self.assertEqual(parse_host_status_record(json.dumps(baseline).encode()), baseline)
        self.assertEqual(host_status_observation(baseline, local_identity=self.identity, now=now), 'idle')
        self.assertEqual(host_status_observation(baseline | {'state': 'busy'}, local_identity=self.identity, now=now), 'busy')
        self.assertEqual(host_status_observation(baseline | {'state': 'disabled'}, local_identity=self.identity, now=now), 'unknown')
        for bad in (
            baseline | {'source_revision': 'main'},
            baseline | {'source_revision': True},
            baseline | {'execution_authority': 'automatic'},
            baseline | {'state': 1},
            baseline | {'version': True},
            baseline | {'updated_at': ''},
            {k: v for k, v in baseline.items() if k != 'source_revision'},
        ):
            with self.subTest(bad=bad):
                self.assertEqual(host_status_observation(bad, local_identity=self.identity, now=now), 'unknown')
                self.assertFalse(host_status_is_fresh(bad, local_identity=self.identity, now=now))
                with self.assertRaises(ValueError):
                    parse_host_status_record(json.dumps(bad).encode())
        for raw in (
            b'', b'[]', b'not-json', b'\xff', b'{' + b' ' * 4096 + b'}',
            b'{"version":1,"version":1}',
            b'{"version":1,"state":NaN}',
        ):
            with self.subTest(raw=raw[:30]), self.assertRaises(ValueError):
                parse_host_status_record(raw)
        self.assertEqual(host_status_observation(
            baseline | {'updated_at': (now - timedelta(seconds=121)).isoformat()},
            local_identity=self.identity, now=now,
        ), 'unknown')

    def test_freshness_is_observation_not_execution_authority(self) -> None:
        now = datetime(2026, 10, 12, 17, 0, tzinfo=timezone.utc)
        baseline = {'version': 1, 'host_id': 'worker-mac', 'installation_id': HOST_INSTANCE,
                    'state': 'idle', 'source_revision': 'a' * 40, 'updated_at': now.isoformat()}
        self.assertTrue(host_status_is_fresh(baseline, local_identity=self.identity, now=now))
        self.assertTrue(host_status_is_fresh(baseline | {'state': 'busy'}, local_identity=self.identity, now=now))
        for change in (
            {'version': 2}, {'version': True}, {'host_id': 'worker-pc'}, {'installation_id': ANOTHER_INSTANCE},
            {'state': 'disabled'}, {'state': []}, {'updated_at': '2026-10-12T17:00:00'},
            {'updated_at': 'not-a-date'},
            {'updated_at': (now - timedelta(seconds=121)).isoformat()},
            {'updated_at': (now + timedelta(seconds=31)).isoformat()},
        ):
            with self.subTest(change=change):
                self.assertFalse(host_status_is_fresh(baseline | change, local_identity=self.identity, now=now))
        with self.assertRaises(ValueError):
            host_status_is_fresh(baseline, local_identity=self.identity, now=now, max_age_seconds=0)
        with self.assertRaises(ValueError):
            host_status_is_fresh(baseline, local_identity=self.identity, now=datetime.now())


if __name__ == '__main__':
    unittest.main()
