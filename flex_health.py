"""Public, bounded diagnostics for a blocked future-flex dispatch."""
from datetime import datetime


def blocked_attention(flex, filter_error):
    flex = flex if isinstance(flex, dict) else {}
    status = flex.get('status')
    if status == 'auth_disabled' or (status is None and filter_error == 'auth_disabled'):
        kind, reason, default = 'future_flex_auth_disabled', 'future flex authentication rejected; dispatch disabled', 'auth_rejected'
        allowed = {'auth_rejected'}
    elif status == 'billing_disabled' or (status is None and filter_error == 'billing_disabled'):
        kind, reason, default = 'future_flex_billing_disabled', 'future flex quota exhausted; dispatch disabled', 'quota_exhausted'
        allowed = {'quota_exhausted'}
    elif status == 'configuration_rejected' or (status is None and filter_error == 'configuration_rejected'):
        kind, reason, default = 'future_flex_configuration_rejected', 'future flex request rejected; dispatch disabled', 'http_rejected'
        allowed = {'http_rejected'}
    elif status == 'unknown' or (status is None and filter_error in ('unknown', 'outcome_unknown', 'response_invalid')):
        kind, reason, default = 'future_flex_unknown', 'future flex dispatch outcome uncertain; dispatch blocked', 'outcome_unknown'
        allowed = {'outcome_unknown', 'response_invalid', 'http_rejected'}
    else:
        return None
    code = flex.get('last_error_code')
    if not isinstance(code, str) or code not in allowed:
        code = filter_error if isinstance(filter_error, str) and filter_error in allowed else default
    started = flex.get('pending_started_at')
    try:
        dt = datetime.fromisoformat(started.replace('Z', '+00:00')) if isinstance(started, str) and len(started) <= 40 else None
        started = dt.isoformat() if dt and dt.tzinfo else None
    except (ValueError, OverflowError):
        started = None
    ids = flex.get('pending_ids')
    count = len(ids) if isinstance(ids, list) and len(ids) <= 8 and all(type(x) is int and x > 0 for x in ids) else None
    return {'needed': True, 'reason': reason, 'kind': kind, 'code': code,
            'started_at': started, 'count': count}
