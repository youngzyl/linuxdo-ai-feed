import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from daily import run_daily, attempt_resolved
from receipts import ReceiptStore


class HistoricalAbandonTests(unittest.TestCase):
    def test_explicit_abandon_unblocks_daily_and_preserves_nonce(self):
        for run_id in ('2026-11-16', 'canary-2026-11-16-' + '1'*32):
            with self.subTest(run_id=run_id), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); receipts = root/'receipts'; runs = root/'runs'
                receipts.mkdir(); runs.mkdir()
                original = {'run_id':run_id,'attempts':[{'number':1,'outcome':'unknown','marker':'first'},
                            {'number':2,'outcome':'unknown','marker':'second','recovery_nonce':'a'*64}]}
                path = receipts/(run_id+'.json');path.write_text(json.dumps(original));raw=path.read_bytes();digest=hashlib.sha256(raw).hexdigest()
                now=dt.datetime(2026,11,18,tzinfo=dt.timezone.utc)
                runner=Mock(side_effect=RuntimeError('admission-reached-no-provider'))
                code,result=run_daily(runs,receipts,'path-only',now=now,runner=runner)
                self.assertEqual((code,result['status']),(1,'blocked_unresolved'));runner.assert_not_called()
                with ReceiptStore(receipts).singleton() as store:
                    with self.assertRaises(ValueError):store.owner_abandon_all(run_id,'0'*64)
                    self.assertEqual(path.read_bytes(),raw)
                    done=store.owner_abandon_all(run_id,digest)
                self.assertEqual((receipts/('owner-snapshot-'+digest+'.json')).read_bytes(),raw)
                self.assertEqual(done['attempts'][0]['outcome'],'unknown')
                self.assertTrue(all(attempt_resolved(a) for a in done['attempts']))
                self.assertEqual(done['attempts'][1]['recovery_nonce'],'a'*64)
                self.assertEqual(len(done['attempts'][0]['recovery_nonce']),64)
                with self.assertRaisesRegex(RuntimeError,'admission-reached-no-provider'):
                    run_daily(runs,receipts,'path-only',now=now,runner=runner)
                self.assertEqual(runner.call_count,1)
                self.assertEqual(json.loads(path.read_text()),done)

    def test_invalid_canary_and_bad_existing_nonce_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); store=ReceiptStore(root)
            with store.singleton():
                for bad in ('../2026-11-16','canary-2026-11-16-'+'z'*32,'canary-2026-11-16-'+'a'*32+'/x'):
                    with self.assertRaises(ValueError):store.owner_abandon_all(bad,'0'*64)
                rid='canary-2026-11-16-'+'a'*32;p=root/(rid+'.json')
                p.write_text(json.dumps({'run_id':rid,'attempts':[{'outcome':'unknown','recovery_nonce':'bad'}]}))
                raw=p.read_bytes()
                with self.assertRaises(ValueError):store.owner_abandon_all(rid,hashlib.sha256(raw).hexdigest())
                self.assertEqual(p.read_bytes(),raw)

if __name__=='__main__':unittest.main()
