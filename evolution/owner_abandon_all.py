#!/usr/bin/env python3
"""Explicit owner CAS abandonment of historical daily or operator-canary unknowns."""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
from receipts import ReceiptStore

ROOT = Path('/home/youngzyl/linuxdo-evolve/experiment-receipts')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--expected-sha256', required=True)
    parser.add_argument('--confirm', choices=['ABANDON_DO_NOT_RETRY'], required=True)
    args = parser.parse_args()
    with (ROOT / '.daily.lock').open('a+b') as daily_lock:
        fcntl.flock(daily_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with ReceiptStore(ROOT).singleton() as store:
            receipt = store.owner_abandon_all(args.run_id, args.expected_sha256)
            path = ROOT / (args.run_id + '.json')
            print(json.dumps({'run_id': args.run_id, 'receipt_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                              'snapshot': str(ROOT / ('owner-snapshot-' + args.expected_sha256 + '.json')),
                              'attempts': [{'number': a.get('number'), 'outcome': a.get('outcome'),
                                            'disposition': a.get('disposition')} for a in receipt['attempts']],
                              'paid_calls_started': 0}))


if __name__ == '__main__':
    main()
