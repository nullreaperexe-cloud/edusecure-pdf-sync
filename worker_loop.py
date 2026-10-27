"""Five-hour GitHub Actions worker. Runs EduSecure/Supabase sync every five minutes.

Each cycle is a fresh process (new GitHub OIDC token and browser), so an error
cannot poison subsequent cycles. A workflow finalizer dispatches the next
five-hour worker; a separate scheduled watchdog covers failed dispatches.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPORT=Path("sync_report.json")
SUMMARY=Path("worker_summary.json")
STOP=False


def _stop(signum, _frame):
    global STOP
    STOP=True
    print(f"Worker received signal {signum}: will stop after the active cycle.", flush=True)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def run_cycle(timeout_seconds=1080, command=None, *, full_reconciliation=False):
    """One EduSecure cycle: full source/Supabase reconciliation when selected."""
    argv=command or [sys.executable,"sync_repair.py"]
    try:
        options = (
            {"env": {**os.environ, "EDUSECURE_FULL_RECONCILIATION": "1"}}
            if full_reconciliation else {}
        )
        process=subprocess.run(argv,timeout=timeout_seconds,check=False,**options)
        return int(process.returncode)
    except subprocess.TimeoutExpired:
        print(f"EduSecure cycle exceeded {timeout_seconds}s, moving to next check.",flush=True)
        return 124
    except Exception as error:
        print(f"EduSecure subprocess failed: {type(error).__name__}",flush=True)
        return 2


def run_drive_backup(timeout_seconds=240):
    """Separate bounded backup. Failure cannot fail EduSecure publication."""
    try:
        result = subprocess.run([sys.executable, "drive_backup.py"],
                                timeout=timeout_seconds, check=False)
        return int(result.returncode)
    except subprocess.TimeoutExpired:
        print("Drive backup timed out; primary Supabase sync remains unaffected.", flush=True)
        return 124
    except Exception as exc:
        print("Drive backup subprocess error: " + type(exc).__name__, flush=True)
        return 2


def worker(duration_seconds=18000,interval_seconds=300,timeout_seconds=1080,
           max_cycles=0,command=None):
    """Run for up to five hours. Do not shorten a cycle already in progress."""
    global STOP
    if duration_seconds<=0 or interval_seconds<=0 or timeout_seconds<=0:
        raise ValueError("Worker duration/interval/timeout must be positive")
    started=time.monotonic()
    deadline=started+duration_seconds
    next_tick=started
    last_backup_started=None
    backup_interval=max(300, int(os.getenv("GDRIVE_BACKUP_INTERVAL_SECONDS","300")))
    backup_retry_interval=max(300, int(os.getenv("GDRIVE_BACKUP_RETRY_SECONDS","300")))
    reconciliation_every=max(1, int(os.getenv("EDUSECURE_RECONCILIATION_EVERY_CYCLES","24")))
    last_backup_rc=None
    backup_timeout=max(60, int(os.getenv("GDRIVE_BACKUP_TIMEOUT_SECONDS","240")))
    summary={"started_at":now_iso(),"planned_minutes":round(duration_seconds/60,2),
             "check_interval_seconds":interval_seconds,"checks":0,
             "successful":0,"failed":0,"timed_out":0,"last_exit_code":None,
             "last_run_at":None,"check_log":[],
             "drive_backup_checks":0,"drive_backup_failures":0,"last_drive_backup_at":None,
             "full_reconciliation_attempts":0,"last_full_reconciliation_at":None}
    print(f"Starting {duration_seconds/3600:.2f}h 8aPDF worker; "+
          f"checking EduSecure every {interval_seconds}s.",flush=True)
    try:
        while not STOP and (max_cycles==0 or summary["checks"]<max_cycles):
            if time.monotonic()>=deadline and summary["checks"]>0:
                break
            remaining=next_tick-time.monotonic()
            if remaining>0:
                time.sleep(min(remaining,max(0,deadline-time.monotonic())))
                if STOP or time.monotonic()>=deadline:
                    break
            n=summary["checks"]+1
            checked=now_iso()
            print(f"====== EduSecure / Supabase check #{n} at {checked} ======",flush=True)
            full_reconciliation=(summary["checks"] % reconciliation_every == 0)
            if full_reconciliation:
                summary["full_reconciliation_attempts"]+=1
                summary["last_full_reconciliation_at"]=checked
                print("Checking all EduSecure history against Supabase before Drive backup.",flush=True)
            rc=run_cycle(timeout_seconds,command,full_reconciliation=full_reconciliation)
            # Verify Drive after every five-minute Supabase reconciliation,
            # not just once per hour. Failed backups are retried, while all
            # Drive operations remain isolated from the source/Supabase sync.
            due_after=backup_retry_interval if last_backup_rc not in (None, 0) else backup_interval
            if last_backup_started is None or time.monotonic()-last_backup_started>=due_after:
                last_backup_started=time.monotonic()
                print("Starting Google Drive backup (failed attempts retry after 5 minutes).", flush=True)
                backup_rc=run_drive_backup(backup_timeout)
                last_backup_rc=backup_rc
                summary["drive_backup_checks"]+=1
                summary["drive_backup_failures"]+=int(backup_rc!=0)
                summary["last_drive_backup_at"]=now_iso()
                print("Drive backup subprocess exit: "+str(backup_rc),flush=True)
            summary["checks"]=n
            summary["last_run_at"]=checked
            summary["last_exit_code"]=rc
            summary["successful"]+=int(rc==0)
            summary["failed"]+=int(rc!=0)
            summary["timed_out"]+=int(rc==124)
            summary["check_log"].append({"check":n,"at":checked,"exit_code":rc})
            SUMMARY.write_text(json.dumps(summary,indent=2),encoding="utf-8")
            print(f"Check #{n} exit code {rc}; total {summary['checks']} "+
                  f"(good: {summary['successful']}, failed: {summary['failed']}).",flush=True)
            # Strict five-minute cadence when a cycle fits; otherwise run again
            # immediately after a slow cycle rather than launching concurrent browsers.
            next_tick+=interval_seconds
            if next_tick<time.monotonic():
                next_tick=time.monotonic()
    finally:
        summary["finished_at"]=now_iso()
        summary["reason"]="signal" if STOP else "window_complete"
        SUMMARY.write_text(json.dumps(summary,indent=2),encoding="utf-8")
        print("Worker session complete: "+json.dumps({
            k:v for k,v in summary.items() if k!="check_log"
        }),flush=True)
    return 0 if summary["successful"] or not summary["checks"] else 1


def main(argv=None):
    parser=argparse.ArgumentParser(description="Persistent EduSecure five-hour Supabase worker")
    parser.add_argument("--duration-minutes",type=float,
                        default=float(os.getenv("WORKER_DURATION_MINUTES","300")))
    parser.add_argument("--interval-seconds",type=float,
                        default=float(os.getenv("WORKER_INTERVAL_SECONDS","300")))
    parser.add_argument("--cycle-timeout-seconds",type=int,
                        default=int(os.getenv("WORKER_CYCLE_TIMEOUT_SECONDS","1080")))
    parser.add_argument("--max-cycles",type=int,default=0)
    args=parser.parse_args(argv)
    signal.signal(signal.SIGTERM,_stop)
    signal.signal(signal.SIGINT,_stop)
    return worker(duration_seconds=args.duration_minutes*60,
      interval_seconds=args.interval_seconds,
      timeout_seconds=args.cycle_timeout_seconds,max_cycles=args.max_cycles)


if __name__=="__main__":
    raise SystemExit(main())
