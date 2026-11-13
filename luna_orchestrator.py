#!/usr/bin/env python3
"""Isolated loopback-writer orchestrator. Explicit execution can incur API charges.

No Store is opened: SnapshotView is an immutable, in-memory view of the owner
HTTP snapshot. This candidate is NOT approved for production deployment.
"""
import argparse,contextlib,fcntl,http.client,json,os,pwd,re,sqlite3,stat,sys,threading
from pathlib import Path
from cryptography.fernet import Fernet,InvalidToken
from batch_offline import Ledger,OfflineError,prepare,load_payload,reconcile,enc,digest
from batch_transport import BatchTransport,execute_upload,execute_submit,poll_batch,download_results,run_realtime,TransportError,_rt_table
from luna_bridge import to_writer_payload
from store import source_fingerprint

class OrchestrationError(Exception):pass

def fail(code):raise OrchestrationError(code) from None

def _private_file(path,max_size=32_000_000):
 p=Path(path)
 try:
  st=p.lstat()
  if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.geteuid() or st.st_mode&0o077 or st.st_size>max_size:fail('private_file_required')
  fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW)
  with os.fdopen(fd,'rb') as f:
   st2=os.fstat(f.fileno())
   if not stat.S_ISREG(st2.st_mode) or st2.st_uid!=os.geteuid() or st2.st_mode&0o077:fail('private_file_required')
   data=f.read(max_size+1)
  if len(data)>max_size:fail('private_file_too_large')
  return data
 except OrchestrationError:raise
 except Exception:fail('private_file_unavailable')

def credential_file(path):
 def callback():
  try:
   s=_private_file(path,4096).decode('ascii').strip()
   if not s or any(ord(c)<33 or ord(c)>126 for c in s):fail('credential_unavailable')
   return s
  except Exception:fail('credential_unavailable')
 return callback

class ExecutorGuard:
 """One shared service lock plus one lexical, symlink-free directory per run."""
 SERVICE_ROOT=Path('/home/youngzyl/services/linuxdo-ai')
 def __init__(self,root,uid):
  self.root=Path(root);self.uid=uid
 @classmethod
 def production(cls):
  try:uid=pwd.getpwnam('youngzyl').pw_uid
  except KeyError:fail('expected_service_user_missing')
  if os.geteuid()!=uid:fail('wrong_service_user')
  return cls(cls.SERVICE_ROOT,uid)
 @classmethod
 def for_test(cls,root):
  # Only tests call this; the CLI always constructs production().
  return cls(root,os.geteuid())
 def _check_component_chain(self,path):
  if not path.is_absolute() or '..' in path.parts:fail('noncanonical_path')
  current=Path('/')
  for part in path.parts[1:]:
   current=current/part
   try:st=current.lstat()
   except FileNotFoundError:break
   if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):fail('symlink_or_nondirectory')
 def validate(self,run,directory):
  if not isinstance(run,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,48}',run):fail('invalid_run')
  expected=self.root/'luna-runs'/run
  if os.fspath(directory)!=str(expected):fail('noncanonical_workdir')
  if os.geteuid()!=self.uid:fail('wrong_service_user')
  self._check_component_chain(self.root);self._check_component_chain(expected)
  try:s=self.root.lstat()
  except OSError:fail('service_root_missing')
  if not stat.S_ISDIR(s.st_mode) or s.st_uid!=self.uid or s.st_mode&0o022:fail('unsafe_service_root')
  for path in (self.root/'luna-runs',expected):
   try:s=path.lstat()
   except FileNotFoundError:continue
   if not stat.S_ISDIR(s.st_mode) or s.st_uid!=self.uid or s.st_mode&0o077:fail('unsafe_run_directory')
  return expected
 def claims(self):
  """Append-only cross-workdir identity record; caller holds the global lock."""
  path=self.root/'luna-executor-claims.sqlite'
  try:st=path.lstat()
  except FileNotFoundError:
   runs=self.root/'luna-runs'
   if runs.exists() and any(runs.iterdir()):fail('missing_executor_claims')
   fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_RDWR|os.O_NOFOLLOW,0o600);os.close(fd)
   dfd=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY)
   try:os.fsync(dfd)
   finally:os.close(dfd)
   st=path.lstat()
  if not stat.S_ISREG(st.st_mode) or st.st_uid!=self.uid or st.st_mode&0o077 or st.st_nlink!=1:fail('unsafe_executor_claims')
  db=sqlite3.connect(path,timeout=10,isolation_level=None);db.execute('PRAGMA journal_mode=DELETE');db.execute('PRAGMA synchronous=FULL')
  db.executescript('''CREATE TABLE IF NOT EXISTS claims(run TEXT PRIMARY KEY,t0 INTEGER NOT NULL,dir_dev INTEGER NOT NULL,dir_ino INTEGER NOT NULL,key_dev INTEGER NOT NULL,key_ino INTEGER NOT NULL,ledger_dev INTEGER NOT NULL,ledger_ino INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS paid_attempts(run TEXT NOT NULL,phase TEXT NOT NULL,PRIMARY KEY(run,phase));
CREATE TRIGGER IF NOT EXISTS paid_attempts_no_update BEFORE UPDATE ON paid_attempts BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS paid_attempts_no_delete BEFORE DELETE ON paid_attempts BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS claims_no_update BEFORE UPDATE ON claims BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS claims_no_delete BEFORE DELETE ON claims BEGIN SELECT RAISE(ABORT,'append_only'); END;''')
  return db
 def identity(self,directory):
  paths=(Path(directory),Path(directory)/'encryption.key',Path(directory)/'ledger.sqlite')
  stats=[]
  for i,path in enumerate(paths):
   try:s=path.lstat()
   except FileNotFoundError:fail('claimed_run_artifact_missing')
   if (i==0 and not stat.S_ISDIR(s.st_mode)) or (i>0 and (not stat.S_ISREG(s.st_mode) or s.st_nlink!=1)) or s.st_uid!=self.uid or s.st_mode&0o077:fail('unsafe_claimed_run_artifact')
   stats.extend((s.st_dev,s.st_ino))
  return tuple(stats)
 @contextlib.contextmanager
 def lock(self):
  # Caller must validate first. Never lock a symlink or pre-existing public file.
  path=self.root/'luna-executor.lock'
  try:
   fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
  except OSError:fail('global_lock_unavailable')
  try:
   s=os.fstat(fd);pathstat=path.lstat()
   if not stat.S_ISREG(s.st_mode) or s.st_uid!=self.uid or s.st_mode&0o077 or s.st_nlink!=1 or (s.st_dev,s.st_ino)!=(pathstat.st_dev,pathstat.st_ino):fail('unsafe_global_lock')
   try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
   except BlockingIOError:fail('executor_busy')
   yield
  finally:
   fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)

class OwnerClient:
 """HTTP over fixed IPv4 loopback only. No proxy, redirect, or public owner URL."""
 def __init__(self,port,credential_callback,timeout=30,max_response=8_100_000):
  if type(port) is not int or not 1<=port<=65535 or not callable(credential_callback) or type(timeout) not in (int,float) or not 0<timeout<=120 or type(max_response) is not int or not 1<=max_response<=8_100_000:fail('invalid_owner_configuration')
  self.port=port;self.callback=credential_callback;self.timeout=timeout;self.max_response=max_response
 def _post(self,path,obj):
  try:
   try:token=self.callback()
   except Exception:fail('owner_credential_unavailable')
   if not isinstance(token,str) or not token or len(token)>4096 or not token.isascii() or any(ord(c)<33 or ord(c)>126 for c in token):fail('owner_credential_unavailable')
   payload=enc(obj).encode()
   if len(payload)>65536:fail('owner_request_too_large')
   conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=self.timeout)
   try:
    conn.request('POST',path,payload,{'Authorization':'Bearer '+token,'Content-Type':'application/json','Accept':'application/json'})
    resp=conn.getresponse()
    if resp.status!=200:fail('owner_http_'+str(resp.status))
    n=resp.getheader('Content-Length')
    if n and n.isdecimal() and int(n)>self.max_response:fail('owner_response_too_large')
    raw=resp.read(self.max_response+1)
    if len(raw)>self.max_response:fail('owner_response_too_large')
    result=json.loads(raw)
    if not isinstance(result,dict):fail('invalid_owner_response')
    return result
   finally:conn.close()
  except OrchestrationError:raise
  except Exception:fail('owner_outcome_unknown')
 def snapshot(self,run,t0):return self._post('/api/luna/snapshot',{'run_id':run,'t0':t0})
 def apply(self,payload):return self._post('/api/luna/apply',payload)
 def finish(self,run,status):return self._post('/api/luna/finish',{'run_id':run,'status':status})

class SnapshotView:
 """Only a copy of HTTP snapshot data; no file IO, live Store, or state writes."""
 def __init__(self,snap,run,t0):
  if not isinstance(snap,dict) or set(snap)!={'run_id','t0','taste','groups'} or snap['run_id']!=run or snap['t0']!=t0 or not isinstance(snap['taste'],str) or len(snap['taste'].encode())>65536:fail('invalid_snapshot')
  groups=snap['groups']
  if not isinstance(groups,dict) or 'realtime' not in groups or not isinstance(groups['realtime'],list) or len(groups['realtime'])>8 or (not groups['realtime'] and len(groups)!=1):fail('invalid_snapshot')
  self.data={'topics':{}};self.lock=threading.RLock();self.groups={}
  for gid,rows in groups.items():
   if not isinstance(gid,str) or not re.fullmatch(r'(?:realtime|g-[0-9a-f]{32})',gid) or not isinstance(rows,list) or len(rows)>8 or (not rows and gid!='realtime'):fail('invalid_snapshot')
   members=[]
   for row in rows:
    if not isinstance(row,dict) or type(row.get('id')) is not int or not isinstance(row.get('source_version'),str):fail('invalid_snapshot')
    tid=row['id'];version=row['source_version']
    if str(tid) in self.data['topics'] or source_fingerprint(row)!=version:fail('invalid_snapshot')
    self.data['topics'][str(tid)]=row;members.append({'id':tid,'source_version':version})
   self.groups[gid]=members
  if len(self.data['topics'])>1024:fail('invalid_snapshot')
 def needs_model_verdict(self,topic):
  verdict=topic.get('filter') or {};value=verdict.get('valuable')
  return not isinstance(value,bool) or verdict.get('source_version')!=source_fingerprint(topic)

def _write_secret(path,data):
 p=Path(path);fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
 try:
  with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
  d=os.open(p.parent,os.O_RDONLY|os.O_DIRECTORY)
  try:os.fsync(d)
  finally:os.close(d)
 except BaseException:p.unlink(missing_ok=True);raise

class Orchestrator:
 def __init__(self,directory,run,t0,owner,provider,*,guard=None):
  if not isinstance(run,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,48}',run) or type(t0) is not int or not 0<=t0<=253402300799:fail('invalid_run')
  self.guard=guard or ExecutorGuard.production()
  self.guard.validate(run,directory)
  with self.guard.lock():
   self.guard.validate(run,directory)
   (self.guard.root/'luna-runs').mkdir(mode=0o700,exist_ok=True)
   self.guard.validate(run,directory)
   with contextlib.closing(self.guard.claims()) as db:
    row=db.execute('SELECT t0,dir_dev,dir_ino,key_dev,key_ino,ledger_dev,ledger_ino FROM claims WHERE run=?',(run,)).fetchone()
    if row is not None:
     if row[0]!=t0 or tuple(row[1:])!=self.guard.identity(directory):fail('executor_claim_mismatch')
     self._verify_existing_ledger(Path(directory)/'ledger.sqlite',run,t0,owner,provider)
    else:
     path=Path(directory)
     if path.exists() and any(path.iterdir()):fail('unregistered_existing_run')
    self._initialize(directory,run,t0,owner,provider)
    if row is None:
     db.execute('BEGIN IMMEDIATE')
     try:
      db.execute('INSERT INTO claims VALUES(?,?,?,?,?,?,?,?)',(run,t0,*self.guard.identity(directory)))
      db.execute('COMMIT')
     except BaseException:
      if db.in_transaction:db.execute('ROLLBACK')
      raise
 @staticmethod
 def _verify_existing_ledger(path,run,t0,owner,provider):
  # Do not let sqlite CREATE repair a truncated or replacement ledger on resume.
  try:
   with contextlib.closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=10)) as db:
    if db.execute('PRAGMA quick_check').fetchone()!=('ok',):fail('run_ledger_corrupt')
    rows=db.execute('SELECT run,t0,owner_port,provider_host,provider_port FROM orchestration_meta').fetchall()
    if rows!=[(run,t0,owner.port,provider._host,provider._port)]:fail('run_ledger_identity_mismatch')
    names={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'orchestration_meta','orchestration_events','orchestration_outcomes','runs','events','reserved','members'}<=names:fail('run_ledger_corrupt')
  except OrchestrationError:raise
  except Exception:fail('run_ledger_corrupt')
 def _initialize(self,directory,run,t0,owner,provider):
  self.dir=Path(directory);self.run=run;self.t0=t0;self.owner=owner;self.provider=provider
  self.dir.mkdir(mode=0o700,parents=False,exist_ok=True)
  st=self.dir.lstat()
  if not stat.S_ISDIR(st.st_mode) or st.st_uid!=os.geteuid() or st.st_mode&0o077:fail('private_directory_required')
  self.keypath=self.dir/'encryption.key'
  if not self.keypath.exists():
   if any(x.name not in {'lock'} for x in self.dir.iterdir()):fail('encryption_key_missing')
   _write_secret(self.keypath,Fernet.generate_key())
  self.key=_private_file(self.keypath,100)
  try:self.cipher=Fernet(self.key)
  except Exception:fail('invalid_encryption_key')
  self.ledger=Ledger(self.dir/'ledger.sqlite')
  with self.ledger.db() as d:
   d.executescript('''CREATE TABLE IF NOT EXISTS orchestration_meta(run TEXT PRIMARY KEY,t0 INTEGER NOT NULL,owner_port INTEGER NOT NULL,provider_host TEXT NOT NULL,provider_port INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS orchestration_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,run TEXT NOT NULL,kind TEXT NOT NULL,gid TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS orchestration_outcomes(run TEXT NOT NULL,gid TEXT NOT NULL,stale TEXT NOT NULL,PRIMARY KEY(run,gid));
CREATE TRIGGER IF NOT EXISTS orch_outcomes_no_update BEFORE UPDATE ON orchestration_outcomes BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS orch_outcomes_no_delete BEFORE DELETE ON orchestration_outcomes BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS orch_meta_no_update BEFORE UPDATE ON orchestration_meta BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS orch_meta_no_delete BEFORE DELETE ON orchestration_meta BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS orch_events_no_update BEFORE UPDATE ON orchestration_events BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS orch_events_no_delete BEFORE DELETE ON orchestration_events BEGIN SELECT RAISE(ABORT,'append_only'); END;''')
   d.execute('BEGIN IMMEDIATE')
   try:
    row=d.execute('SELECT run,t0,owner_port,provider_host,provider_port FROM orchestration_meta').fetchall()
    expected=(run,t0,owner.port,provider._host,provider._port)
    if not row:d.execute('INSERT INTO orchestration_meta VALUES(?,?,?,?,?)',expected)
    elif row!=[expected]:fail('run_identity_mismatch')
    d.execute('COMMIT')
   except BaseException:
    if d.in_transaction:d.execute('ROLLBACK')
    raise
 def _event(self,kind,gid,dig):
  with self.ledger.db() as d:d.execute('INSERT INTO orchestration_events(run,kind,gid,digest) VALUES(?,?,?,?)',(self.run,kind,gid,dig))
 def _snapshot(self):
  path=self.dir/'snapshot.enc'
  with self.ledger.db() as d:r=d.execute("SELECT digest FROM orchestration_events WHERE run=? AND kind='snapshot' ORDER BY seq DESC LIMIT 1",(self.run,)).fetchone()
  if path.exists():
   try:raw=self.cipher.decrypt(_private_file(path,20_000_000));snap=json.loads(raw)
   except Exception:fail('snapshot_unreadable')
   if r and digest(raw)!=r[0]:fail('snapshot_hash_mismatch')
  elif r:fail('snapshot_missing')
  else:
   snap=self.owner.snapshot(self.run,self.t0);raw=enc(snap).encode();SnapshotView(snap,self.run,self.t0)
   _write_secret(path,self.cipher.encrypt(raw))
  view=SnapshotView(snap,self.run,self.t0)
  if not r:self._event('snapshot','',digest(raw))
  return snap,view
 def _prepare(self,snap,view):
  with self.ledger.db() as d:r=d.execute('SELECT t0 FROM runs WHERE run=?',(self.run,)).fetchone()
  if r is None:
   prepare(self.ledger,self.run,self.t0,view,snap['taste'],self.dir/'input.enc',self.key)
  elif r[0]!=self.t0:fail('ledger_t0_mismatch')
  groups=self.ledger.members(self.run)
  expected={k:v for k,v in view.groups.items() if k!='realtime'}
  with self.ledger.db() as d:reserved=d.execute('SELECT topic_id,source_version FROM reserved WHERE run=? ORDER BY ord',(self.run,)).fetchall()
  if groups!=expected or reserved!=[(m['id'],m['source_version']) for m in view.groups['realtime']]:fail('reservation_mismatch')
  load_payload(self.ledger,self.run,self.key)
  return groups
 def _paid_start(self,phase):
  if phase not in {'realtime','upload','submit'}:fail('invalid_paid_phase')
  with contextlib.closing(self.guard.claims()) as db:
   try:db.execute('INSERT INTO paid_attempts(run,phase) VALUES(?,?)',(self.run,phase))
   except sqlite3.IntegrityError:fail('paid_outcome_requires_reconciliation')
 def _recover_realtime(self,view,snap):
  _rt_table(self.ledger)
  with self.ledger.db() as d:r=d.execute('SELECT state,path,response_hash FROM realtime_events WHERE run=? ORDER BY seq DESC LIMIT 1',(self.run,)).fetchone()
  if not r:
   self._paid_start('realtime')
   run_realtime(self.ledger,self.run,view,snap['taste'],self.provider,self.key,self.dir/'realtime.enc')
   with self.ledger.db() as d:r=d.execute('SELECT state,path,response_hash FROM realtime_events WHERE run=? ORDER BY seq DESC LIMIT 1',(self.run,)).fetchone()
  if not r or r[0] not in {'received','validated'} or r[1]!=str((self.dir/'realtime.enc').resolve()):fail('realtime_outcome_unknown')
  try:raw=self.cipher.decrypt(_private_file(r[1],20_000_000));body=json.loads(raw)
  except Exception:fail('realtime_result_unreadable')
  if digest(raw)!=r[2]:fail('realtime_result_hash_mismatch')
  line=enc({'custom_id':'realtime','response':{'status_code':200,'body':body},'error':None})
  plan=reconcile({'realtime':view.groups['realtime']},[line],[])
  return {'verdicts':plan['verdicts']['realtime'],'usage':plan['usage'][0]}
 def _apply(self,gid,payload):
  sha=digest(enc(payload).encode())
  with self.ledger.db() as d:
   d.execute('BEGIN IMMEDIATE')
   try:
    row=d.execute("SELECT kind,digest FROM orchestration_events WHERE run=? AND gid=? AND kind IN ('apply_unknown','applied') ORDER BY seq DESC LIMIT 1",(self.run,gid)).fetchone()
    if row and row[1]!=sha:fail('apply_payload_conflict')
    if row and row[0]=='applied':
     outcome=d.execute('SELECT stale FROM orchestration_outcomes WHERE run=? AND gid=?',(self.run,gid)).fetchone()
     if outcome is not None:d.execute('COMMIT');return json.loads(outcome[0])
    if not row:d.execute("INSERT INTO orchestration_events(run,kind,gid,digest) VALUES(?, 'apply_unknown',?,?)",(self.run,gid,sha))
    d.execute('COMMIT')
   except BaseException:
    if d.in_transaction:d.execute('ROLLBACK')
    raise
  receipt=self.owner.apply(payload)
  stale=receipt.get('stale')
  ids={r['topic_id'] for r in payload['items']}
  if receipt.get('run_id')!=self.run or receipt.get('group_id')!=gid or receipt.get('provenance_assurance')!='owner_asserted' or not isinstance(stale,list) or any(type(i) is not int or i not in ids for i in stale) or len(set(stale))!=len(stale) or type(receipt.get('accepted')) is not int or receipt['accepted']!=len(ids)-len(stale):fail('invalid_writer_receipt')
  with self.ledger.db() as d:
   d.execute('BEGIN IMMEDIATE')
   try:
    d.execute('INSERT INTO orchestration_outcomes(run,gid,stale) VALUES(?,?,?)',(self.run,gid,enc(stale)))
    d.execute("INSERT INTO orchestration_events(run,kind,gid,digest) VALUES(?, 'applied',?,?)",(self.run,gid,sha))
    d.execute('COMMIT')
   except BaseException:
    if d.in_transaction:d.execute('ROLLBACK')
    raise
  return stale
 def _recover_batch(self,groups):
  with self.ledger.db() as d:r=d.execute("SELECT handle1,handle2 FROM events WHERE run=? AND state='downloaded' ORDER BY seq DESC LIMIT 1",(self.run,)).fetchone()
  if not r or r[0]!=str((self.dir/'output.enc').resolve()):fail('batch_result_missing')
  try:raw=self.cipher.decrypt(_private_file(r[0],50_000_000));blob=json.loads(raw)
  except Exception:fail('batch_result_unreadable')
  if digest(raw)!=r[1] or not isinstance(blob,dict):fail('batch_result_hash_mismatch')
  return reconcile(groups,blob.get('output',[]),blob.get('error',[]))
 def _verify_claim(self):
  with contextlib.closing(self.guard.claims()) as db:
   row=db.execute('SELECT t0,dir_dev,dir_ino,key_dev,key_ino,ledger_dev,ledger_ino FROM claims WHERE run=?',(self.run,)).fetchone()
  if row is None or row[0]!=self.t0 or tuple(row[1:])!=self.guard.identity(self.dir):fail('executor_claim_mismatch')
  self._verify_existing_ledger(self.dir/'ledger.sqlite',self.run,self.t0,self.owner,self.provider)
 def advance(self):
  self.guard.validate(self.run,self.dir)
  with self.guard.lock():
   self.guard.validate(self.run,self.dir)
   self._verify_claim()
   return self._advance_locked()
 def _advance_locked(self):
  lockpath=self.dir/'lock';fd=os.open(lockpath,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
  try:
   try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
   except BlockingIOError:fail('run_busy')
   with self.ledger.db() as d:
    finished=d.execute("SELECT kind FROM orchestration_events WHERE run=? AND kind IN ('finish_unknown','partial_cancelled') ORDER BY seq DESC LIMIT 1",(self.run,)).fetchone()
   if finished and finished[0]=='partial_cancelled':return {'state':'partial_cancelled'}
   if finished:fail('finish_outcome_unknown')
   snap,view=self._snapshot()
   if not view.groups['realtime']:return {'state':'complete','batch_groups':0,'actual_billing':None}
   groups=self._prepare(snap,view)
   rt=self._recover_realtime(view,snap);stale_ids=self._apply('realtime',to_writer_payload(self.run,'realtime',rt))
   if not groups:return {'state':'reconciled' if stale_ids else 'complete','stale':stale_ids,'batch_groups':0,'usage':{'realtime':rt['usage'],'batch':{'input_tokens':0,'output_tokens':0,'cached_tokens':0,'reasoning_tokens':0,'cache_write_tokens':None}},'actual_billing':None}
   state=self.ledger.status(self.run)
   if state in {'upload_unknown','submit_unknown'}:fail(state)
   if state=='prepared':
    self._paid_start('upload');execute_upload(self.ledger,self.run,self.key,self.provider);state=self.ledger.status(self.run)
   if state=='uploaded':
    self._paid_start('submit');execute_submit(self.ledger,self.run,self.provider);return {'state':'submitted'}
   if state in {'submitted','validating','in_progress','finalizing','cancelling'}:
    status=poll_batch(self.ledger,self.run,self.provider);state=status['status']
    if state in {'validating','in_progress','finalizing','cancelling'}:return {'state':state}
   if state in {'completed','failed','expired','cancelled'}:
    download_results(self.ledger,self.run,self.key,self.provider,self.dir/'output.enc');state=self.ledger.status(self.run)
   if state not in {'downloaded','reconciled','partial'}:fail('unexpected_batch_state')
   plan=self._recover_batch(groups)
   if state=='downloaded':self.ledger.transition(self.run,{'downloaded'},'reconciled' if plan['complete'] else 'partial')
   for gid in sorted(plan['verdicts']):stale_ids+=self._apply(gid,to_writer_payload(self.run,gid,plan))
   totals={k:sum(x[k] for x in plan['usage']) for k in ('input_tokens','output_tokens','cached_tokens','reasoning_tokens')}
   totals['cache_write_tokens']=None
   state='partial' if not plan['complete'] else ('reconciled' if stale_ids else 'complete')
   return {'state':state,'stale':stale_ids,'provenance_assurance':'owner_asserted','batch_groups_applied':len(plan['verdicts']),'batch_groups_failed':len(plan['errors']),'usage':{'realtime':rt['usage'],'batch':totals},'actual_billing':None}
  finally:
   fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)

 def finish_partial(self):
  """Explicitly cancel an already terminal/downloaded incomplete Batch run."""
  self.guard.validate(self.run,self.dir)
  with self.guard.lock():
   self.guard.validate(self.run,self.dir)
   self._verify_claim()
   with self.ledger.db() as d:
    finished=d.execute("SELECT kind FROM orchestration_events WHERE run=? AND kind IN ('finish_unknown','partial_cancelled') ORDER BY seq DESC LIMIT 1",(self.run,)).fetchone()
   if finished and finished[0]=='partial_cancelled':return {'state':'partial_cancelled'}
   state=self.ledger.status(self.run)
   if state not in {'downloaded','partial'}:fail('batch_not_terminal_partial')
   if not finished:self._event('finish_unknown','',digest(b'cancelled'))
   result=self.owner.finish(self.run,'cancelled')
   if result!={'run_id':self.run,'status':'cancelled'}:fail('invalid_finish_receipt')
   self._event('partial_cancelled','',digest(b'cancelled'))
   return {'state':'partial_cancelled'}

def main():
 p=argparse.ArgumentParser(description='Isolated Luna orchestrator; not production approved')
 p.add_argument('--workdir',required=True);p.add_argument('--run-id',required=True);p.add_argument('--t0',required=True,type=int);p.add_argument('--owner-port',required=True,type=int);p.add_argument('--owner-token-file',required=True);p.add_argument('--openai-key-file',required=True);p.add_argument('--enable-paid-requests',action='store_true');p.add_argument('--finish-partial',action='store_true')
 args=p.parse_args()
 if not args.finish_partial and not args.enable_paid_requests:fail('explicit_paid_request_gate_required')
 owner=OwnerClient(args.owner_port,credential_file(args.owner_token_file));provider=BatchTransport(credential_file(args.openai_key_file))
 worker=Orchestrator(args.workdir,args.run_id,args.t0,owner,provider)
 result=worker.finish_partial() if args.finish_partial else worker.advance()
 print(json.dumps(result,sort_keys=True))
if __name__=='__main__':
 try:main()
 except (OrchestrationError,OfflineError,TransportError) as e:print(json.dumps({'state':'blocked','reason':str(e)}));sys.exit(2)
 except Exception:print(json.dumps({'state':'blocked','reason':'unexpected_error'}));sys.exit(2)
