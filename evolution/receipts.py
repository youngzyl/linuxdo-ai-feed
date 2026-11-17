"""Durable, fail-closed UTC experiment admission and paid-attempt journal."""
import contextlib
import datetime as dt
import fcntl
import hashlib
import re
import secrets
import json
import os
from pathlib import Path
import tempfile


class ReceiptStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = None
        self._admitted = set()
        self.recovery_nonce = None
        self.run_nonce = None

    @contextlib.contextmanager
    def singleton(self):
        if self._lock is not None:
            raise RuntimeError('singleton already held')
        handle = open(self.root / '.lock', 'a+b')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock = handle
            yield self
        finally:
            self._admitted.clear()
            self.recovery_nonce = None
            self.run_nonce = None
            self._lock = None
            handle.close()

    def _require_lock(self):
        if self._lock is None:
            raise RuntimeError('singleton lock required')

    def _write(self, path, value):
        data = json.dumps(value, sort_keys=True, separators=(',', ':')).encode() + b'\n'
        fd, name = tempfile.mkstemp(dir=self.root, prefix='.receipt-')
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def admit(self, when=None):
        self._require_lock()
        when = when or dt.datetime.now(dt.timezone.utc)
        if when.tzinfo is None or when.utcoffset() is None:
            raise ValueError('timezone-aware datetime required')
        run_id = when.astimezone(dt.timezone.utc).date().isoformat()
        path = self.root / (run_id + '.json')
        if path.exists():
            return False, json.loads(path.read_text())
        self.run_nonce = secrets.token_hex(32)
        receipt = {'run_id': run_id, 'status': 'admitted', 'attempts': [], 'run_nonce': self.run_nonce}
        self._write(path, receipt)
        self._admitted.add(run_id)
        return True, receipt

    def admit_canary(self, run_id):
        """Explicit operator canary; shared lock and separate, immutable two-intent cap."""
        self._require_lock()
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        if not re.fullmatch(r'canary-' + today + r'-[0-9a-f]{32}', run_id):
            raise ValueError('invalid canary id or UTC date')
        intent_path = self.root / ('operator-canary-' + today + '.intent.json')
        intent = json.loads(intent_path.read_text())
        if intent.get('canary_id') != run_id or intent.get('authorization') != 'lead-user-extra-canary-budget-two' or intent.get('prior_receipt_sha256') != 'c41f9c7d0c6049037fd908f5c9ef46c656549ec92d5c40c8dd357847378a6ec1':
            raise ValueError('operator canary intent mismatch')
        from daily import attempt_resolved
        path = self.root / (run_id + '.json')
        if path.exists():
            return False, json.loads(path.read_text())
        if list(self.root.glob('canary-' + today + '-*.json')):
            raise RuntimeError('operator canary already exists for UTC day')
        prior = self.root / '2026-11-17.json'
        if not prior.is_file() or hashlib.sha256(prior.read_bytes()).hexdigest() != 'c41f9c7d0c6049037fd908f5c9ef46c656549ec92d5c40c8dd357847378a6ec1':
            raise RuntimeError('operator evidence digest mismatch')
        for path in list(self.root.glob('????-??-??.json')) + list(self.root.glob('canary-????-??-??-*.json')):
            old = json.loads(path.read_text())
            attempts = old.get('attempts')
            if not isinstance(attempts, list) or not attempts or len(attempts) > 2 or not all(attempt_resolved(x) for x in attempts):
                raise RuntimeError('unresolved prior intent')
        path = self.root / (run_id + '.json')
        if path.exists():
            return False, json.loads(path.read_text())
        self.run_nonce = secrets.token_hex(32)
        receipt = {'run_id': run_id, 'status': 'admitted', 'attempts': [], 'run_nonce': self.run_nonce,
                   'purpose': 'lead-authorized independent operator canary',
                   'prior_receipt_sha256': 'c41f9c7d0c6049037fd908f5c9ef46c656549ec92d5c40c8dd357847378a6ec1'}
        self._write(path, receipt)
        self._admitted.add(run_id)
        return True, receipt

    def _load_active(self, run_id):
        self._require_lock()
        if run_id not in self._admitted:
            raise RuntimeError('run was not admitted by this lock holder')
        path = self.root / (run_id + '.json')
        return path, json.loads(path.read_text())

    def owner_abandon(self, run_id, expected_sha256):
        """Record an explicit no-retry decision; this does not authorize dispatch."""
        self._require_lock()
        if run_id != dt.datetime.now(dt.timezone.utc).date().isoformat():
            raise ValueError('recovery only allowed on original UTC day')
        path = self.root / (run_id + '.json')
        raw = path.read_bytes()
        receipt = json.loads(raw)
        attempts = receipt.get('attempts', [])
        if receipt.get('run_id') != run_id or len(attempts) != 1 or attempts[0].get('outcome') != 'unknown':
            raise ValueError('receipt is not one unknown intent')
        first = attempts[0]
        if first.get('disposition') == 'abandoned_no_retry' and first.get('abandon_source_sha256') == expected_sha256:
            return receipt
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise ValueError('receipt digest mismatch')
        if first.get('disposition') is not None:
            raise ValueError('receipt already has disposition')
        first.update(disposition='abandoned_no_retry', abandon_source_sha256=expected_sha256,
                     abandoned_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                     recovery_nonce=secrets.token_hex(32))
        self._write(path, receipt)
        return receipt

    def owner_abandon_all(self, run_id, expected_sha256):
        """CAS-audit unknown intents without asserting provider acceptance or charges."""
        self._require_lock()
        if not (re.fullmatch(r'\d{4}-\d{2}-\d{2}', run_id) or
                re.fullmatch(r'canary-\d{4}-\d{2}-\d{2}-[0-9a-f]{32}', run_id)):
            raise ValueError('invalid run id')
        dt.date.fromisoformat(run_id[7:17] if run_id.startswith('canary-') else run_id)
        path = self.root / (run_id + '.json')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise ValueError('receipt digest mismatch')
        receipt = json.loads(raw)
        attempts = receipt.get('attempts')
        if receipt.get('run_id') != run_id or not isinstance(attempts, list) or not attempts or any(a.get('outcome') == 'pending' for a in attempts):
            raise ValueError('invalid or pending receipt')
        unknown = [a for a in attempts if a.get('outcome') == 'unknown' and a.get('disposition') != 'abandoned_no_retry']
        if not unknown:
            raise ValueError('no unresolved unknown intent')
        for attempt in unknown:
            nonce = attempt.get('recovery_nonce')
            if nonce is not None and (not isinstance(nonce, str) or not re.fullmatch(r'[0-9a-f]{64}', nonce)):
                raise ValueError('invalid existing recovery nonce')
        audit_nonce = secrets.token_hex(32)
        archive = self.root / ('owner-snapshot-' + expected_sha256 + '.json')
        try:
            fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if archive.read_bytes() != raw:
                raise ValueError('snapshot mismatch')
        else:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(directory)
            finally: os.close(directory)
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        for attempt in unknown:
            if attempt.get('recovery_nonce') is None:
                attempt['recovery_nonce'] = audit_nonce
            attempt.update(disposition='abandoned_no_retry', abandon_source_sha256=expected_sha256,
                           abandoned_at=now, abandonment_authority='lead-owner-decision')
        self._write(path, receipt)
        return receipt

    def owner_resume(self, run_id, expected_sha256):
        """Explicit operator-only admission of the remaining attempt, under lock."""
        self._require_lock()
        if run_id != dt.datetime.now(dt.timezone.utc).date().isoformat():
            raise ValueError('recovery only allowed on original UTC day')
        path = self.root / (run_id + '.json')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise ValueError('receipt digest mismatch')
        receipt = json.loads(raw)
        attempts = receipt.get('attempts', [])
        if (receipt.get('run_id') != run_id or len(attempts) != 1 or
                attempts[0].get('outcome') != 'unknown' or
                attempts[0].get('disposition') != 'abandoned_no_retry' or
                not re.fullmatch(r'[0-9a-f]{64}', attempts[0].get('recovery_nonce', ''))):
            raise ValueError('recovery not authorized')
        self.recovery_nonce = attempts[0]['recovery_nonce']
        self._admitted.add(run_id)
        return receipt

    def begin_attempt(self, run_id, *, prompt_sha256=None):
        path, receipt = self._load_active(run_id)
        attempts = receipt['attempts']
        recovery = (len(attempts) == 1 and self.recovery_nonce and
                    attempts[0].get('outcome') == 'unknown' and
                    attempts[0].get('disposition') == 'abandoned_no_retry' and
                    attempts[0].get('recovery_nonce') == self.recovery_nonce)
        if len(attempts) >= 2 or (attempts and attempts[-1]['outcome'] != 'success' and not recovery):
            raise RuntimeError('attempt limit or unresolved prior intent')
        if prompt_sha256 is not None and not re.fullmatch(r'[0-9a-f]{64}', prompt_sha256):
            raise ValueError('invalid prompt digest')
        if recovery and (not prompt_sha256 or prompt_sha256 == attempts[0].get('prompt_sha256')):
            raise ValueError('distinct prompt digest required')
        number = len(attempts) + 1
        intent = {'number': number, 'outcome': 'pending'}
        if prompt_sha256:
            intent['prompt_sha256'] = prompt_sha256
        if recovery:
            intent['recovery_nonce'] = self.recovery_nonce
        attempts.append(intent)
        self._write(path, receipt)
        return number

    def record_attempt(self, run_id, number, *, response_handle, model, tier, usage, outcome, diagnostic=None):
        path, receipt = self._load_active(run_id)
        attempts = receipt['attempts']
        if not attempts or attempts[-1]['number'] != number or attempts[-1]['outcome'] != 'pending':
            raise RuntimeError('attempt is not pending')
        if outcome not in ('success', 'error', 'unknown', 'capacity_deferred'):
            raise ValueError('invalid outcome')
        if outcome == 'success' and not response_handle:
            raise ValueError('success requires a response handle')
        attempts[-1].update(response_handle=response_handle, model=model, tier=tier, usage=usage, outcome=outcome)
        if diagnostic is not None:
            attempts[-1]['diagnostic'] = diagnostic
        self._write(path, receipt)
        return receipt
