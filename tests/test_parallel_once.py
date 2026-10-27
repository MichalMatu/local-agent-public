from __future__ import annotations

import unittest

import local_agent.supervisor.orchestrator as parallel


class ParallelOnceTests(unittest.TestCase):
    def test_once_deferral_becomes_terminal_at_limit(self) -> None:
        counts: dict[str, int] = {}
        failed: set[str] = set()

        first = parallel.record_once_deferral(
            counts,
            failed,
            "repo-a",
            limit=2,
        )
        self.assertEqual(first, 1)
        self.assertNotIn("repo-a", failed)

        second = parallel.record_once_deferral(
            counts,
            failed,
            "repo-a",
            limit=2,
        )
        self.assertEqual(second, 2)
        self.assertIn("repo-a", failed)

    def test_once_deferral_counts_are_repository_scoped(self) -> None:
        counts: dict[str, int] = {}
        failed: set[str] = set()

        parallel.record_once_deferral(counts, failed, "repo-a", limit=3)
        parallel.record_once_deferral(counts, failed, "repo-b", limit=3)
        parallel.record_once_deferral(counts, failed, "repo-a", limit=3)

        self.assertEqual(counts, {"repo-a": 2, "repo-b": 1})
        self.assertEqual(failed, set())


if __name__ == "__main__":
    unittest.main()
