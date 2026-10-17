"""Offline tests for five-hour self-renewing EduSecure GitHub worker."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

import worker_loop as w


class WorkerLoopTests(unittest.TestCase):
    def test_runs_successful_check(self):
        with patch.object(w.subprocess,"run",return_value=Mock(returncode=0)) as run:
            self.assertEqual(w.run_cycle(10,["python","example.py"]),0)
            run.assert_called_once()
            self.assertEqual(run.call_args.kwargs["timeout"],10)

    def test_one_failed_check_does_not_stop_next_check(self):
        with tempfile.TemporaryDirectory() as folder:
            with (patch.object(w,"SUMMARY",Path(folder)/"worker.json"),
                  patch.object(w,"STOP",False),
                  patch.object(w,"run_cycle",side_effect=[2,0]) as run):
                status=w.worker(duration_seconds=10,interval_seconds=0.002,
                                timeout_seconds=10,max_cycles=2)
                summary=json.loads((Path(folder)/"worker.json").read_text())
            self.assertEqual(status,0)
            self.assertEqual(run.call_count,2)
            self.assertEqual(summary["checks"],2)
            self.assertEqual(summary["failed"],1)
            self.assertEqual(summary["successful"],1)

    def test_all_failed_checks_report_failure_but_complete_window(self):
        with tempfile.TemporaryDirectory() as folder:
            with (patch.object(w,"SUMMARY",Path(folder)/"worker.json"),
                  patch.object(w,"STOP",False),
                  patch.object(w,"run_cycle",return_value=2)):
                status=w.worker(duration_seconds=10,interval_seconds=0.001,
                                timeout_seconds=10,max_cycles=2)
            self.assertEqual(status,1)

    def test_timeout_is_not_retried_in_the_same_cycle(self):
        with patch.object(w.subprocess,"run",side_effect=subprocess.TimeoutExpired("python",5)) as run:
            self.assertEqual(w.run_cycle(5,["python","sync_repair.py"]),124)
            self.assertEqual(run.call_count,1)

    def test_rejects_zero_or_negative_worker_window(self):
        with self.assertRaises(ValueError):
            w.worker(duration_seconds=0,interval_seconds=300)


if __name__=="__main__":
    unittest.main()
