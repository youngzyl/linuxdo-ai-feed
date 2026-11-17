"""Sanitized classification of known HTTP rejections for the flex transport.

429 responses require a complete, bounded JSON error body: status alone does not
establish whether the rejection is temporary, quota-related, or ambiguous.
"""
import json
import hashlib
import re

MAX_ERROR_BODY = 65536


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def diagnostic_429(body, complete):
    """Describe only observed bytes; partial hashes/lengths are not whole-body values."""
    d = {'body_read_complete': bool(complete),
         'body_length': len(body) if isinstance(body, bytes) else None,
         'body_sha256': hashlib.sha256(body).hexdigest() if isinstance(body, bytes) else None,
         'json_shape': 'unavailable'}
    if not complete:
        return d
    try:
        obj = json.loads(body, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError):
        d['json_shape'] = 'invalid_json'
        return d
    if not isinstance(obj, dict):
        d['json_shape'] = 'non_object'
        return d
    error = obj.get('error')
    d['json_shape'] = 'object_error_object' if isinstance(error, dict) else 'object_other'
    if isinstance(error, dict):
        for field in ('code', 'type'):
            value = error.get(field)
            if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,96}', value, flags=re.ASCII):
                d['error_' + field] = value
    return d


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
    quota_codes = ('insufficient_quota','credit_balance_exhausted','organization_spend_limit_exceeded','project_spend_limit_exceeded')
    if code in quota_codes or error_type in quota_codes:
        return 'quota_exhausted'
    if code in ('rate_limit_exceeded', 'resource_exhausted', 'resource_unavailable', 'flex_unavailable') or error_type == 'resource_unavailable':
        return 'rate_limited'
    return 'outcome_unknown'
