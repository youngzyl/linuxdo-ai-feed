#!/usr/bin/env python3
"""One UTC-day OpenEvolve admission, preserving all durable experiment evidence."""
import argparse
import datetime as dt
import fcntl
import json
import math
import os
from pathlib import Path
import sys

ROOT = Path('/home/youngzyl/linuxdo-evolve')
RUNS = ROOT / 'daily-runs'
RECEIPTS = ROOT / 'experiment-receipts'


def load_json(path):
    obj = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(obj, dict):
        raise ValueError('not an object: ' + str(path))
    return obj


def attempt_resolved(attempt):
    if not isinstance(attempt, dict):
        return False
    if attempt.get('outcome') in ('success', 'error'):
        return True
    if attempt.get('outcome') == 'capacity_deferred':
        d = attempt.get('diagnostic') or {}
        return (d.get('http_status') == 429 and d.get('body_read_complete') is True and
                d.get('json_shape') == 'object_error_object' and
                (d.get('provider_code') == 'flex_unavailable' or d.get('provider_type') == 'resource_unavailable'))
    import re
    return (attempt.get('outcome') == 'unknown' and attempt.get('disposition') == 'abandoned_no_retry'
            and isinstance(attempt.get('abandon_source_sha256'), str)
            and re.fullmatch(r'[0-9a-f]{64}', attempt['abandon_source_sha256']) is not None
            and isinstance(attempt.get('recovery_nonce'), str)
            and re.fullmatch(r'[0-9a-f]{64}', attempt['recovery_nonce']) is not None
            and isinstance(attempt.get('abandoned_at'), str))


def prior_best(runs, today):
    # Report existing artifacts; never rewrite or promote them.
    out = []
    for path in sorted(runs.glob('????-??-??/live-report.json'), reverse=True):
        if path.parent.name >= today:
            continue
        recovery = path.parent / 'recovery-attempt-2' / 'live-report.json'
        if recovery.is_file():
            path = recovery
        obj = load_json(path)
        day = path.parent.parent.name if path.parent.name == 'recovery-attempt-2' else path.parent.name
        out.append({'date': day, 'status': obj.get('status'),
                    'best_id': obj.get('best_id'), 'best_training': obj.get('best_training'),
                    'best_holdout': obj.get('best_holdout'), 'report': str(path),
                    'candidate_artifact': obj.get('candidate_artifact')})
    return out


def run_daily(runs=RUNS, receipts=RECEIPTS, credential_file=None, deadline=1100,
              now=None, runner=None):
    if not math.isfinite(deadline) or not 0 < deadline <= 1200:
        raise ValueError('deadline must be in (0, 1200] seconds')
    runs, receipts = Path(runs).resolve(), Path(receipts).resolve()
    if runs == receipts or runs in receipts.parents or receipts in runs.parents:
        raise ValueError('runs and receipts must be separate')
    runs.mkdir(mode=0o700, parents=True, exist_ok=True)
    receipts.mkdir(mode=0o700, parents=True, exist_ok=True)
    when = now or dt.datetime.now(dt.timezone.utc)
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError('timezone-aware time required')
    today = when.astimezone(dt.timezone.utc).date().isoformat()
    with (receipts / '.daily.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = prior_best(runs, today)
        current = receipts / (today + '.json')
        report_path = runs / today / 'live-report.json'
        recovery_report = runs / today / 'recovery-attempt-2' / 'live-report.json'
        if recovery_report.is_file():
            report_path = recovery_report
        journal = {}
        blocked = None
        for path in sorted(list(receipts.glob('????-??-??.json')) + list(receipts.glob('canary-????-??-??-*.json'))):
            obj = load_json(path)
            if obj.get('run_id') != path.stem or not isinstance(obj.get('attempts'), list):
                raise ValueError('invalid receipt: ' + str(path))
            journal[path.stem] = obj
            attempts = obj['attempts']
            if not attempts or len(attempts) > 2 or any(not attempt_resolved(a) for a in attempts):
                blocked = blocked or path
        # A duplicate completed run is a read-only lookup, not new paid work.
        # It may still be returned when a different day is unresolved.
        if current.exists() and report_path.is_file():
            saved = load_json(report_path)
            attempts = journal[today]['attempts']
            resolved = (0 < len(attempts) <= 2 and
                        all(attempt_resolved(a) for a in attempts))
            if saved.get('status') == 'completed' and resolved and any(a.get('outcome') == 'success' and a.get('response_handle') for a in attempts):
                return 0, {'status': 'existing_report', 'report': saved, 'report_path': str(report_path),
                           'prior_runs': previous, 'paid_calls_started': 0}
        if blocked is not None:
            return 1, {'status': 'blocked_unresolved', 'receipt': str(blocked),
                       'report': load_json(report_path) if report_path.is_file() else None,
                       'report_path': str(report_path),
                       'prior_runs': previous, 'paid_calls_started': 0}
        if current.exists() or report_path.exists() or (runs / today).exists():
            if not current.exists() or not report_path.is_file():
                return 1, {'status': 'blocked_incomplete_existing_run', 'receipt': str(current),
                           'report': str(report_path), 'prior_runs': previous, 'paid_calls_started': 0}
            report = load_json(report_path)
            attempts = journal[today]['attempts']
            successful = any(a.get('outcome') == 'success' and a.get('response_handle') for a in attempts)
            deferred = report.get('status') == 'capacity_deferred' and all(attempt_resolved(a) for a in attempts) and any(a.get('outcome') == 'capacity_deferred' for a in attempts)
            code = 0 if (report.get('status') == 'completed' and successful) or deferred else 1
            return code, {'status': 'existing_report', 'report': report, 'report_path': str(report_path),
                          'prior_runs': previous, 'paid_calls_started': 0}
        if not credential_file:
            raise ValueError('credential file required')
        # The runner enforces a process-group deadline, and its isolated provider
        # process enforces the separate persistent receipt lock and two-intent cap.
        if runner is None:
            from controller_runner import run_bounded
            runner = run_bounded
        output = runs / today
        exit_code = runner(output, deadline, live=True, receipts=receipts,
                           credential_file=Path(credential_file))
        report = load_json(report_path) if report_path.is_file() else None
        # A missing/invalid report must never be counted as success.
        recorded = load_json(current) if current.is_file() else {}
        attempts = recorded.get('attempts', [])
        successful = (isinstance(attempts, list) and 0 < len(attempts) <= 2 and
                      all(isinstance(a, dict) and a.get('outcome') in ('success', 'error') for a in attempts) and
                      any(a.get('outcome') == 'success' and a.get('response_handle') for a in attempts))
        deferred = (isinstance(attempts, list) and bool(attempts) and all(attempt_resolved(a) for a in attempts)
                    and any(a.get('outcome') == 'capacity_deferred' for a in attempts))
        code = 0 if exit_code == 0 and report and ((report.get('status') == 'completed' and successful) or (report.get('status') == 'capacity_deferred' and deferred)) else 1
        return code, {'status': 'ran', 'report': report, 'report_path': str(report_path),
                      'prior_runs': previous, 'runner_exit': exit_code}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credential-file', required=True, help='path only, never a key value')
    parser.add_argument('--deadline', type=float, default=1100)
    args = parser.parse_args()
    try:
        code, report = run_daily(credential_file=args.credential_file, deadline=args.deadline)
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        code, report = 1, {'status': 'blocked_or_failed', 'error_type': type(exc).__name__,
                           'instruction': 'inspect durable receipts before any further run'}
    print(json.dumps(report, indent=2))
    return code


if __name__ == '__main__':
    sys.exit(main())
