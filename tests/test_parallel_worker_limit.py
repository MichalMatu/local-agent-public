from __future__ import annotations

import os
import unittest
from unittest import mock

from local_agent.supervisor import scheduling


class ParallelWorkerLimitTests(unittest.TestCase):
    def test_four_workers_are_accepted(self) -> None:
        self.assertEqual(scheduling.MAX_MAX_WORKERS, 4)
        self.assertEqual(scheduling.resolve_max_workers(4), 4)

    def test_five_workers_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be 1..4"):
            scheduling.resolve_max_workers(5)

    def test_environment_can_request_four_workers(self) -> None:
        with mock.patch.dict(
            os.environ,
            {scheduling.MAX_WORKERS_ENV: "4"},
            clear=False,
        ):
            self.assertEqual(scheduling.resolve_max_workers(None), 4)


if __name__ == "__main__":
    unittest.main()
