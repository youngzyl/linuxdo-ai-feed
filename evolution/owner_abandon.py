#!/usr/bin/env python3
"""Record an explicit owner abandonment; never sends a provider request."""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
from pathlib import Path

from receipts import ReceiptStore

ROOT = Path('/home/youngzyl/linuxdo-evolve/experiment-receipts')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-sha256', required=True)
    parser.add_argument('--confirm', required=True, choices=['ABANDON_DO_NOT_RETRY'])
    args = parser.parse_args()
    with (ROOT / '.daily.lock').open('a+b') as daily_lock:
        fcntl.flock(daily_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store = ReceiptStore(ROOT)
        with store.singleton():
            run_id = dt.datetime.now(dt.timezone.utc).date().isoformat()
            receipt = store.owner_abandon(run_id, args.expected_sha256)
            path = ROOT / (run_id + '.json')
            print(json.dumps({'run_id': run_id, 'outcome': receipt['attempts'][0]['outcome'],
                              'disposition': receipt['attempts'][0]['disposition'],
                              'receipt_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                              'paid_calls_started': 0}))


if __name__ == '__main__':
    main()
