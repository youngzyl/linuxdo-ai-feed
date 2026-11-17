"""Sanitized classification of known HTTP rejections for the flex transport.

429 responses require a complete, bounded JSON error body: status alone does not
establish whether the rejection is temporary, quota-related, or ambiguous.
"""
import json

MAX_ERROR_BODY = 65536


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def classify_http_error(status, body=None):
    """Return a sanitized FlexError code for a known HTTP error status.

    For 429, pass the complete response as bytes, or None when reading failed.
    Callers must read at most MAX_ERROR_BODY + 1 bytes and close the connection;
    an oversized, malformed, ambiguous, or unrecognized error is unknown.
    No body content is included in the result.
    """
    if status in (401, 403):
        return 'auth_rejected'
    if status != 429:
        return 'http_rejected'
    if not isinstance(body, bytes) or len(body) > MAX_ERROR_BODY:
        return 'outcome_unknown'
    try:
        obj = json.loads(body, object_pairs_hook=_unique_object)
        error = obj.get('error') if isinstance(obj, dict) else None
        code = error.get('code') if isinstance(error, dict) else None
        error_type = error.get('type') if isinstance(error, dict) else None
    except (ValueError, UnicodeError, RecursionError):
        return 'outcome_unknown'
    if code in ('insufficient_quota','credit_balance_exhausted','organization_spend_limit_exceeded','project_spend_limit_exceeded') or error_type == 'insufficient_quota':
        return 'quota_exhausted'
    if code in ('rate_limit_exceeded', 'resource_exhausted', 'resource_unavailable'):
        return 'rate_limited'
    return 'outcome_unknown'
