"""Isolated owner-only Luna snapshot/apply candidate. Not a verified deployment."""
import copy,datetime as dt,hashlib,json,math,re,time
import canary
import learn
from store import source_fingerprint,now_iso

Invalid=canary.Invalid
Conflict=canary.Conflict
PersistenceFailure=canary.PersistenceFailure
MAX_TOPICS=1024
MAX_SNAPSHOT_BYTES=8_000_000

def enc(x):return json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
def _time(text):
 try:
  if not isinstance(text,str):raise ValueError()
  value=dt.datetime.fromisoformat(text.replace('Z','+00:00'))
  if value.tzinfo is None or value.utcoffset() is None:raise ValueError()
  return value.timestamp()
 except (ValueError,OverflowError):raise Conflict('invalid_created_at') from None

def _group_id(run,members):
 return 'g-'+hashlib.sha256(enc({'run':run,'members':members}).encode()).hexdigest()[:32]

def _snapshot_topic(topic,version):
 fields=('id','title','body_text','excerpt','tags','category','reply_count','views','like_count','created_at')
 result={}
 for name in fields:
  if name not in topic:continue
  value=topic[name]
  if name in ('title','body_text','excerpt','category','created_at'):
   if value is not None and (not isinstance(value,str) or len(value)>262144):raise Conflict('snapshot_field_invalid')
  elif name=='tags':
   if value is not None and (not isinstance(value,list) or len(value)>128 or any(not isinstance(x,str) or len(x)>256 for x in value)):raise Conflict('snapshot_field_invalid')
  elif name in ('id','reply_count','views','like_count') and value is not None and type(value) is not int:raise Conflict('snapshot_field_invalid')
  result[name]=copy.deepcopy(value)
 old=topic.get('filter')
 if old:
  if not isinstance(old,dict):raise Conflict('snapshot_field_invalid')
  result['filter']={'valuable':old.get('valuable'),'source_version':old.get('source_version')}
 result['source_version']=version
 return result

def _result(store,run):
 if 'snapshot' in run:
  return copy.deepcopy(run['snapshot'])
 result={}
 for gid,members in run['groups'].items():
  rows=[]
  for m in members:
   topic=store.data['topics'].get(str(m['id']))
   if not topic or source_fingerprint(topic)!=m['source_version']:raise Conflict('snapshot_stale')
   item=copy.deepcopy(topic);item['source_version']=m['source_version'];rows.append(item)
  result[gid]=rows
 out={'run_id':run['run_id'],'t0':run['t0'],'taste':run.get('taste',''),'groups':result}
 if len(enc(out).encode())>MAX_SNAPSHOT_BYTES:raise Conflict('snapshot_too_large')
 return out

def snapshot(store,run_id,t0):
 if not canary._valid_run(run_id) or type(t0) is not int or not 0<=t0<=253402300799 or t0>time.time()+60:raise Invalid('snapshot')
 with store.lock:
  history=store.data.get('luna_history') or {}
  existing=store.data.get('luna')
  if run_id in history:
   if history[run_id].get('t0')!=t0:raise Conflict('run_exists')
   return _result(store,history[run_id])
  if existing:
   if existing.get('run_id')==run_id:
    if existing.get('t0')!=t0:raise Conflict('run_exists')
    return _result(store,existing)
   if existing.get('status','active')=='active':raise Conflict('run_exists')
  eligible=[];seen=set()
  for t in store.data['topics'].values():
   if not store.needs_model_verdict(t):continue
   if type(t.get('id')) is not int or t['id'] in seen:raise Conflict('invalid_topic_id')
   seen.add(t['id']);created=_time(t.get('created_at'))
   if created<t0:eligible.append((created,t['id'],t))
  eligible.sort(key=lambda x:(x[0],x[1]),reverse=True)
  chosen=eligible[:8]+[x for x in eligible[8:] if x[0]>=t0-86400]
  if len(chosen)>MAX_TOPICS:raise Conflict('too_many_topics')
  def member(x):return {'id':x[1],'source_version':source_fingerprint(x[2])}
  groups={'realtime':[member(x) for x in chosen[:8]]}
  for i in range(8,len(chosen),8):
   members=[member(x) for x in chosen[i:i+8]];gid=_group_id(run_id,members)
   if gid in groups:raise Conflict('group_collision')
   groups[gid]=members
  taste=learn.render_examples(learn.examples_for_prompt(store))
  if len(taste.encode('utf-8'))>65536:raise Conflict('taste_too_large')
  saved={};size=0
  for gid,members in groups.items():
   rows=[]
   for m in members:
    row=_snapshot_topic(store.data['topics'][str(m['id'])],m['source_version'])
    size+=len(enc(row).encode())
    if size>MAX_SNAPSHOT_BYTES:raise Conflict('snapshot_too_large')
    rows.append(row)
   saved[gid]=rows
  result={'run_id':run_id,'t0':t0,'taste':taste,'groups':saved}
  if len(enc(result).encode())>MAX_SNAPSHOT_BYTES:raise Conflict('snapshot_too_large')
  reservation={'run_id':run_id,'t0':t0,'taste':taste,'groups':groups,'receipts':{},'snapshot':result,'status':'active' if chosen else 'completed'}
  candidate=copy.deepcopy(store.data)
  if existing:candidate.setdefault('luna_history',{})[existing['run_id']]=copy.deepcopy(existing)
  candidate['luna']=reservation
  canary._commit(store,candidate)
  return result

def apply(store,p):
 if not isinstance(p,dict) or set(p)!={'run_id','group_id','model','provider','items'}:raise Invalid('payload')
 if not canary._valid_run(p['run_id']) or not isinstance(p['group_id'],str) or not re.fullmatch(r'(?:realtime|g-[0-9a-f]{32})',p['group_id']):raise Invalid('id')
 if p['model']!='gpt-6-luna' or p['provider']!='openai':raise Invalid('provenance')
 rows=p['items'];keys={'i','topic_id','source_version','valuable','score','category','reason','summary'}
 if not isinstance(rows,list) or not 1<=len(rows)<=8:raise Invalid('items')
 for i,r in enumerate(rows,1):
  if not isinstance(r,dict) or set(r)!=keys or type(r['i']) is not int or r['i']!=i or type(r['topic_id']) is not int or type(r['valuable']) is not bool:raise Invalid('row')
  if not isinstance(r['source_version'],str) or len(r['source_version'])>80:raise Invalid('version')
  if type(r['score']) not in (int,float) or not math.isfinite(r['score']) or not 0<=r['score']<=100:raise Invalid('score')
  for k,limit in (('category',24),('reason',120),('summary',220)):
   if not isinstance(r[k],str) or not r[k].strip() or len(r[k])>limit:raise Invalid('text')
 digest=hashlib.sha256(enc(p).encode()).hexdigest()
 with store.lock:
  reservation=store.data.get('luna') or {}
  if reservation.get('run_id')!=p['run_id']:
   reservation=(store.data.get('luna_history') or {}).get(p['run_id']) or {}
  if reservation.get('run_id')!=p['run_id']:raise Conflict('run')
  gid=p['group_id'];members=reservation['groups'].get(gid)
  if members is None:raise Conflict('group')
  if len(rows)!=len(members):raise Conflict('group_size')
  if gid!='realtime' and 'realtime' not in reservation['receipts']:raise Conflict('realtime_not_committed')
  prior=reservation['receipts'].get(gid)
  if prior:
   if prior['digest']!=digest:raise Conflict('idempotency')
   return copy.deepcopy(prior['result'])
  if reservation.get('status','active')!='active' or reservation is not store.data.get('luna'):raise Conflict('run_terminal')
  stale=[]
  for row,m in zip(rows,members):
   if row['topic_id']!=m['id'] or row['source_version']!=m['source_version']:raise Conflict('membership')
   t=store.data['topics'].get(str(m['id']))
   if not t or source_fingerprint(t)!=m['source_version'] or not store.needs_model_verdict(t):stale.append(m['id'])
  candidate=copy.deepcopy(store.data)
  for row in rows:
   if row['topic_id'] in stale:continue
   t=candidate['topics'][str(row['topic_id'])]
   t['source_version']=row['source_version']
   t['filter']={'valuable':row['valuable'],'score':row['score'],'category':row['category'],'reason':row['reason'],'summary':row['summary'],'model':'gpt-6-luna','provider':'openai','provenance_assurance':'owner_asserted','prompt_version':'v1','source_version':row['source_version'],'at':now_iso(),'luna_run_id':p['run_id'],'luna_group_id':gid}
   store._refresh_state(t)
  result={'run_id':p['run_id'],'group_id':gid,'accepted':len(rows)-len(stale),'stale':stale,'provenance_assurance':'owner_asserted'}
  candidate['luna']['receipts'][gid]={'digest':digest,'result':result}
  if set(candidate['luna']['receipts'])==set(candidate['luna']['groups']):
   candidate['luna']['status']='reconciled' if any(r['result'].get('stale') for r in candidate['luna']['receipts'].values()) else 'completed'
  canary._commit(store,candidate)
  return result

def finish(store,run_id,status):
 if not canary._valid_run(run_id) or status not in ("cancelled","reconciled"):raise Invalid("finish")
 with store.lock:
  run=store.data.get("luna") or {}
  if run.get("run_id")!=run_id:raise Conflict("run")
  if run.get("status")==status:return {"run_id":run_id,"status":status}
  if run.get("status","active")!="active":raise Conflict("run_terminal")
  if status=="reconciled" and set(run.get("receipts",{}))!=set(run.get("groups",{})):raise Conflict("outstanding_groups")
  data=copy.deepcopy(store.data);data["luna"]["status"]=status;canary._commit(store,data)
  return {"run_id":run_id,"status":status}
