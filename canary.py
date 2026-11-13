"""Isolated canary candidate: no network, single Store writer only."""
import copy
import hashlib
import json
import math
import os
import re
import tempfile
from store import source_fingerprint, now_iso

class Invalid(Exception): pass
class Conflict(Exception): pass
class PersistenceFailure(Exception): pass

def enabled(cfg):
    return cfg.get('canary_enabled') is True

def _valid_run(run):
    return isinstance(run, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,48}', run) is not None

def _failstop():
    # Process-wide, immediate exit while the caller still owns Store.lock. A
    # handler exception or SystemExit would merely terminate one server thread.
    os._exit(70)

def _publish(store, data):
    # Preserve topic identity and unchanged nested values held by other code.
    live_topics = store.data['topics']
    for key, topic in data['topics'].items():
        if key in live_topics:
            current = live_topics[key]
            for field, value in topic.items():
                if field not in current or current[field] != value:
                    current[field] = value
            for field in list(current):
                if field not in topic:
                    del current[field]
        else:
            live_topics[key] = topic
    for key in list(live_topics):
        if key not in data['topics']:
            del live_topics[key]
    if 'canary' in data:
        store.data['canary'] = data['canary']
    if 'luna' in data:
        store.data['luna'] = data['luna']
    if 'luna_history' in data:
        store.data['luna_history'] = data['luna_history']

def _commit(store, data):
    # Called only while Store.lock is held. No external IO or inference here.
    fd, name = tempfile.mkstemp(prefix='.canary-', dir=str(store.path.parent))
    replace_started = False
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        # From this point an error may mean the rename happened: do not allow
        # any thread to continue, save stale data, or acknowledge success.
        replace_started = True
        os.replace(name, store.path)
        _publish(store, data)
        dfd = os.open(str(store.path.parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except BaseException as exc:
        if replace_started:
            _failstop()
            # Defensive if a broken injected failstop mistakenly returns.
            os._exit(70)
        raise PersistenceFailure('not_committed') from exc
    finally:
        if os.path.exists(name):
            os.unlink(name)

def snapshot(store, run_id):
    if not _valid_run(run_id): raise Invalid('run_id')
    with store.lock:

        existing = store.data.get('canary')
        if existing:
            if existing.get('run_id') != run_id: raise Conflict('one_run_only')
            topics=[]
            for item in existing['reservation']:
                t=store.data['topics'].get(str(item['id']))
                if not t or source_fingerprint(t)!=item['source_version']:
                    raise Conflict('snapshot_stale')
                topics.append(copy.deepcopy(t))
            return {'run_id':run_id, 'batch_size':8, 'topics':topics}
        items=store.pending()[:40]
        if len(items)!=40: raise Conflict('requires_40_eligible')
        topics=copy.deepcopy(items)
        reservation=[{'id':int(t['id']),'source_version':source_fingerprint(t)} for t in topics]
        for t,r in zip(topics,reservation): t['source_version']=r['source_version']
        data=copy.deepcopy(store.data)
        data['canary']={'run_id':run_id,'reservation':reservation,'receipts':{}}
        _commit(store,data)
        return {'run_id':run_id,'batch_size':8,'topics':topics}

def apply(store,p):
    if not isinstance(p,dict) or set(p)!={'run_id','batch','model','provider','items'}: raise Invalid('payload')
    if not _valid_run(p['run_id']) or type(p['batch']) is not int or not 0<=p['batch']<5: raise Invalid('batch')
    if p['model']!='gpt-6.1-terra' or p['provider']!='openai-codex': raise Invalid('provenance')
    rows=p['items']
    if not isinstance(rows,list) or len(rows)!=8: raise Invalid('items')
    keys={'i','topic_id','source_version','valuable','score','category','reason','summary'}
    for i,r in enumerate(rows,1):
        if not isinstance(r,dict) or set(r)!=keys or type(r['i']) is not int or r['i']!=i or type(r['topic_id']) is not int or type(r['valuable']) is not bool: raise Invalid('row')
        if not isinstance(r['source_version'],str) or len(r['source_version'])>80: raise Invalid('version')
        if type(r['score']) not in (int,float) or not math.isfinite(r['score']) or not 0<=r['score']<=100: raise Invalid('score')
        for k,limit in (('category',24),('reason',120),('summary',220)):
            if not isinstance(r[k],str) or not r[k].strip() or len(r[k])>limit: raise Invalid('text')
    digest=hashlib.sha256(json.dumps(p,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()
    with store.lock:

        run=store.data.get('canary') or {}
        if run.get('run_id')!=p['run_id']: raise Conflict('run')
        b=str(p['batch']); receipts=run['receipts']
        if b in receipts:
            if receipts[b]['digest']!=digest: raise Conflict('idempotency')
            return copy.deepcopy(receipts[b]['result'])
        if p['batch']!=len(receipts): raise Conflict('batch_order')
        expected=run['reservation'][p['batch']*8:p['batch']*8+8]
        for r,e in zip(rows,expected):
            if r['topic_id']!=e['id'] or r['source_version']!=e['source_version']: raise Conflict('reservation')
            t=store.data['topics'].get(str(e['id']))
            if not t or source_fingerprint(t)!=e['source_version'] or not store.needs_model_verdict(t): raise Conflict('stale_or_competing')
        data=copy.deepcopy(store.data)
        for r in rows:
            t=data['topics'][str(r['topic_id'])]
            t['source_version']=r['source_version']
            t['filter']={'valuable':r['valuable'],'score':r['score'],'category':r['category'],'reason':r['reason'],'summary':r['summary'],'model':p['model'],'provider':p['provider'],'prompt_version':'v1','source_version':r['source_version'],'at':now_iso(),'canary_run_id':p['run_id'],'canary_batch':p['batch']}
            store._refresh_state(t)
        result={'run_id':p['run_id'],'batch':p['batch'],'accepted':8}
        data['canary']['receipts'][b]={'digest':digest,'result':result}
        _commit(store,data)
        return result
