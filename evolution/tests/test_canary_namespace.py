import datetime as dt,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from receipts import ReceiptStore
from daily import run_daily

class CanaryTests(unittest.TestCase):
 def test_namespace_admission_limit_and_unresolved_block(self):
  with tempfile.TemporaryDirectory() as root:
   base=Path(root);runs=base/'runs';runs.mkdir();r=base/'receipts';r.mkdir();today=dt.datetime.now(dt.timezone.utc).date().isoformat()
   old={'run_id':'2026-11-17','attempts':[{'outcome':'unknown','disposition':'abandoned_no_retry','abandon_source_sha256':'a'*64,'recovery_nonce':'b'*64,'abandoned_at':'x'}]}
   (r/'2026-11-17.json').write_text(json.dumps(old));cid='canary-'+today+'-'+'1'*32
   (r/('operator-canary-'+today+'.intent.json')).write_text(json.dumps({'canary_id':cid,'authorization':'lead-user-extra-canary-budget-two','prior_receipt_sha256':'c41f9c7d0c6049037fd908f5c9ef46c656549ec92d5c40c8dd357847378a6ec1'}))
   class H:
    def hexdigest(self):return 'c41f9c7d0c6049037fd908f5c9ef46c656549ec92d5c40c8dd357847378a6ec1'
   with patch('receipts.hashlib.sha256',return_value=H()):
    with ReceiptStore(r).singleton() as store:
     ok,rec=store.admit_canary(cid);self.assertTrue(ok);self.assertEqual(rec['run_id'],cid)
     self.assertEqual(store.begin_attempt(cid,prompt_sha256='a'*64),1)
     with self.assertRaises(RuntimeError):store.begin_attempt(cid,prompt_sha256='b'*64)
    with ReceiptStore(r).singleton() as store:
     self.assertFalse(store.admit_canary(cid)[0])
     with self.assertRaises((RuntimeError,ValueError)):store.admit_canary('canary-'+today+'-'+'2'*32)
   code,result=run_daily(runs,r,'unused',now=dt.datetime(2026,11,19,tzinfo=dt.timezone.utc),runner=lambda *a,**k:self.fail('unsafe call'))
   self.assertEqual(code,1);self.assertEqual(result['status'],'blocked_unresolved')

if __name__=='__main__':unittest.main()
