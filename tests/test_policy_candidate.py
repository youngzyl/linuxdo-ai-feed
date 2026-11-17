import unittest
from test_future_flex import Tests
from future_flex import FutureFlex, FlexError, backfill_exclusions

class PolicyTests(Tests):
 def test_quarantined_id_stays_excluded_when_old_batch_released(self):
  data={'luna':{'run_id':'old','status':'cancelled','groups':{'group':[{'id':10},{'id':11}]},'receipts':{}}}
  meta={'excluded_ids':[10,11],'quarantined_ids':[10],'backfill_prior_run_id':'old','backfill_release':{'unprocessed_groups':['group']}}
  self.assertEqual(backfill_exclusions(data,meta),{10})

 def test_completed_malformed_quarantine_receipt_and_next_topic(self):
  from store import Store
  self.cfg['future_flex'].update(completed_invalid_policy='quarantine',max_batch=1)
  for i in (10,11):self.add(i,self.stamp(self.now-1),self.stamp(self.now-1))
  calls=[]
  def bad(*a):
   calls.append(a[0][0]['id']); b=self.provider(*a);b['id']='resp_paid';b['output'][0]['content'][0]['text']='{"results":[';return b
  f=FutureFlex(self.cfg,self.store,lambda x:None,transport=bad,clock=lambda:self.now);f.run();tid=calls[0]
  self.store=Store(self.store.path,self.store.failure_log);m=self.store.data['future_flex'];r=m['unresolved_receipts'][0]
  self.assertEqual(m['status'],'ready');self.assertEqual(r['response_id'],'resp_paid');self.assertEqual(r['ids'],[tid]);self.assertEqual(r['usage']['input_tokens'],10);self.assertIn(str(tid),r['source_versions']);self.assertIn(str(tid),r['image_versions']);self.assertIsNone(self.store.get(tid)['filter'])
  f=FutureFlex(self.cfg,self.store,lambda x:None,transport=self.provider,clock=lambda:self.now);self.assertEqual(f.run()['judged'],1);self.assertIsNone(self.store.get(tid)['filter'])

 def test_incomplete_wrong_tier_stays_unknown(self):
  self.cfg['future_flex']['completed_invalid_policy']='quarantine';self.add(10,self.stamp(self.now-1),self.stamp(self.now-1))
  def invalid(*a):
   b=self.provider(*a);b.update(id='resp_test',status='incomplete',service_tier='default');return b
  FutureFlex(self.cfg,self.store,lambda x:None,transport=invalid,clock=lambda:self.now).run();self.assertEqual(self.store.data['future_flex']['status'],'unknown')

 def test_quarantine_reload_progress(self):
  self.cfg['future_flex'].update(completed_invalid_policy='quarantine',max_batch=1)
  for i in (10,11): self.add(i,self.stamp(self.now-1),self.stamp(self.now-1))
  calls=[]
  def invalid(*a):
   calls.append(a[0][0]['id']); b=self.provider(*a); b.update(id='resp_test',status='incomplete',incomplete_details={'reason':'max_output_tokens'}); return b
  f=FutureFlex(self.cfg,self.store,lambda x:None,transport=invalid,clock=lambda:self.now)
  f.run(); m=self.store.data['future_flex']; tid=calls[0]
  self.assertEqual(m['status'],'ready'); self.assertEqual(m['quarantined_ids'],[tid]); self.assertEqual(m['unresolved_receipts'][0]['ids'],[tid]); self.assertIsNone(self.store.get(tid)['filter'])
  from store import Store
  self.store=Store(self.store.path,self.store.failure_log)
  f=FutureFlex(self.cfg,self.store,lambda x:None,transport=self.provider,clock=lambda:self.now)
  self.assertEqual(f.run()['judged'],1); self.assertIsNone(self.store.get(tid)['filter'])
  self.assertIn(tid,backfill_exclusions(self.store.data,self.store.data['future_flex']))
 def test_quarantine_requires_opt_in_and_id(self):
  self.add(10,self.stamp(self.now-1),self.stamp(self.now-1))
  def invalid(*a):
   b=self.provider(*a);b.update(id='resp_test',status='incomplete');return b
  FutureFlex(self.cfg,self.store,lambda x:None,transport=invalid,clock=lambda:self.now).run()
  self.assertEqual(self.store.data['future_flex']['status'],'unknown')
 def test_billing_and_configuration_persist(self):
  from store import Store
  for code,status in [('quota_exhausted','billing_disabled'),('http_rejected','configuration_rejected')]:
   with self.subTest(code=code):
    self.add(10,self.stamp(self.now-1),self.stamp(self.now-1)); calls=[]
    def rejected(*a):calls.append(1);raise FlexError(code,429 if code=='quota_exhausted' else 400)
    f=FutureFlex(self.cfg,self.store,lambda x:None,transport=rejected,clock=lambda:self.now); f.run()
    self.store=Store(self.store.path,self.store.failure_log)
    self.assertEqual(self.store.attention(self.cfg)['kind'],'future_flex_'+status)
    self.assertEqual(FutureFlex(self.cfg,self.store,lambda x:None,transport=rejected,clock=lambda:self.now).run()['error'],status); self.assertEqual(len(calls),1)
    self.store.data['future_flex']['status']='ready'
