"""Narrow mapping from the isolated normalized result to Luna owner API payload.

This helper doesn't prove provider completion, authenticate a result, or apply it.
The service remains the authority for reservation and source/eligibility checks.
"""
from luna_writer import Invalid,Conflict

FIELDS=('i','topic_id','source_version','valuable','score','category','reason','summary')
def to_writer_payload(run_id,group_id,plan):
 if not isinstance(plan,dict):raise Invalid('plan')
 results=plan.get('verdicts')
 if group_id=='realtime':
  if not isinstance(results,list):raise Invalid('realtime_plan')
  rows=results
 else:
  if not isinstance(results,dict) or group_id not in results:raise Conflict('group_not_completed')
  if group_id in (plan.get('errors') or {}):raise Conflict('group_failed')
  rows=results[group_id]
 if not isinstance(rows,list) or not 1<=len(rows)<=8:raise Invalid('rows')
 projected=[]
 for row in rows:
  if not isinstance(row,dict) or any(k not in row for k in FIELDS):raise Invalid('row')
  if row.get('model')!='gpt-6-luna' or row.get('prompt_version')!='v1':raise Invalid('row_provenance')
  projected.append({k:row[k] for k in FIELDS})
 return {'run_id':run_id,'group_id':group_id,'model':'gpt-6-luna','provider':'openai','items':projected}
