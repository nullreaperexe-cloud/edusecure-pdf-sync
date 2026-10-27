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
                  patch.object(w,"run_cycle",side_effect=[2,0]) as run,
                  patch.object(w,"run_drive_backup",return_value=0) as backup):
                status=w.worker(duration_seconds=10,interval_seconds=0.002,
                                timeout_seconds=10,max_cycles=2)
                summary=json.loads((Path(folder)/"worker.json").read_text())
            self.assertEqual(status,0)
            self.assertEqual(run.call_count,2)
            self.assertEqual(summary["checks"],2)
            self.assertEqual(summary["failed"],1)
            self.assertEqual(summary["successful"],1)
            self.assertEqual(backup.call_count,1)
            self.assertEqual(summary["drive_backup_checks"],1)

    def test_all_failed_checks_report_failure_but_complete_window(self):
        with tempfile.TemporaryDirectory() as folder:
            with (patch.object(w,"SUMMARY",Path(folder)/"worker.json"),
                  patch.object(w,"STOP",False),
                  patch.object(w,"run_cycle",return_value=2),
                  patch.object(w,"run_drive_backup",return_value=1)):
                status=w.worker(duration_seconds=10,interval_seconds=0.001,
                                timeout_seconds=10,max_cycles=2)
            self.assertEqual(status,1)

    def test_drive_failure_does_not_mark_supabase_sync_failed(self):
        with tempfile.TemporaryDirectory() as folder:
            with (patch.object(w,"SUMMARY",Path(folder)/"worker.json"),
                  patch.object(w,"STOP",False),
                  patch.object(w,"run_cycle",return_value=0),
                  patch.object(w,"run_drive_backup",return_value=1)):
                self.assertEqual(w.worker(duration_seconds=10,interval_seconds=0.001,
                                          timeout_seconds=5,max_cycles=1),0)
                summary=json.loads((Path(folder)/"worker.json").read_text())
            self.assertEqual(summary["successful"],1)
            self.assertEqual(summary["drive_backup_failures"],1)

    def test_full_reconciliation_passes_environment_only_to_selected_cycle(self):
        with patch.object(w.subprocess,"run",return_value=Mock(returncode=0)) as invoke:
            self.assertEqual(w.run_cycle(10, ["python", "sync_repair.py"],
                                         full_reconciliation=True),0)
            env=invoke.call_args.kwargs["env"]
            self.assertEqual(env["EDUSECURE_FULL_RECONCILIATION"],"1")
        with patch.object(w.subprocess,"run",return_value=Mock(returncode=0)) as invoke:
            self.assertEqual(w.run_cycle(10, ["python", "sync_repair.py"]),0)
            self.assertNotIn("env",invoke.call_args.kwargs)

    def test_first_worker_cycle_audits_full_history(self):
        with tempfile.TemporaryDirectory() as folder:
            with (patch.object(w,"SUMMARY",Path(folder)/"worker.json"),
                  patch.object(w,"STOP",False),
                  patch.dict(w.os.environ,{"EDUSECURE_RECONCILIATION_EVERY_CYCLES":"24"}),
                  patch.object(w,"run_cycle",return_value=0) as run,
                  patch.object(w,"run_drive_backup",return_value=0)):
                self.assertEqual(w.worker(duration_seconds=10,interval_seconds=0.002,
                                          timeout_seconds=5,max_cycles=2),0)
                summary=json.loads((Path(folder)/"worker.json").read_text())
            self.assertEqual(run.call_count,2)
            self.assertTrue(run.call_args_list[0].kwargs["full_reconciliation"])
            self.assertFalse(run.call_args_list[1].kwargs["full_reconciliation"])
            self.assertEqual(summary["full_reconciliation_attempts"],1)

    def test_timeout_is_not_retried_in_the_same_cycle(self):
        with patch.object(w.subprocess,"run",side_effect=subprocess.TimeoutExpired("python",5)) as run:
            self.assertEqual(w.run_cycle(5,["python","sync_repair.py"]),124)
            self.assertEqual(run.call_count,1)

    def test_rejects_zero_or_negative_worker_window(self):
        with self.assertRaises(ValueError):
            w.worker(duration_seconds=0,interval_seconds=300)


if __name__=="__main__":
    unittest.main()
