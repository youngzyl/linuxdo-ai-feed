import json
import unittest

from flex_http_errors import MAX_ERROR_BODY, classify_http_error


class HttpErrorClassificationTests(unittest.TestCase):
    def test_flex_resource_unavailable_is_retryable(self):
        self.assertEqual(classify_http_error(429, b'{"error":{"code":"resource_unavailable"}}'), 'rate_limited')

    def test_exact_flex_unavailable_fixture_and_quota_precedence(self):
        for error, expected in (({'code': 'flex_unavailable', 'type': 'resource_unavailable'}, 'rate_limited'),
                                ({'code': 'flex_unavailable'}, 'rate_limited'),
                                ({'type': 'resource_unavailable'}, 'rate_limited'),
                                ({'code': 'insufficient_quota', 'type': 'resource_unavailable'}, 'quota_exhausted'),
                                ({'code': 'flex_unavailable', 'type': 'insufficient_quota'}, 'quota_exhausted')):
            with self.subTest(error=error):
                self.assertEqual(classify_http_error(429, json.dumps({'error': error}).encode()), expected)

    def test_quota_and_resource_429_codes(self):
        for code, expected in (('insufficient_quota', 'quota_exhausted'),
                               ('rate_limit_exceeded', 'rate_limited'),
                               ('resource_exhausted', 'rate_limited')):
            with self.subTest(code=code):
                body = json.dumps({'error': {'code': code, 'message': 'private'}}).encode()
                self.assertEqual(classify_http_error(429, body), expected)

    def test_ambiguous_429_is_unknown(self):
        for body in (None, b'', b'{}', b'not json', b'[]', b'{"error":null}',
                     b'{"error":{"code":null}}', b'{"error":{"code":"other"}}',
                     b'{"error":{"message":"insufficient_quota"}}',
                     b'{"error":{"code":"insufficient_quota","code":"rate_limit_exceeded"}}',
                     b'\xff', b'x' * (MAX_ERROR_BODY + 1)):
            with self.subTest(body=str(body)[:80]):
                self.assertEqual(classify_http_error(429, body), 'outcome_unknown')

    def test_bounded_quota_body(self):
        prefix = b'{"error":{"code":"insufficient_quota"},"padding":"'
        suffix = b'"}'
        body = prefix + b'x' * (MAX_ERROR_BODY - len(prefix) - len(suffix)) + suffix
        self.assertEqual(len(body), MAX_ERROR_BODY)
        self.assertEqual(classify_http_error(429, body), 'quota_exhausted')
        self.assertEqual(classify_http_error(429, body + b' '), 'outcome_unknown')

    def test_deterministic_statuses_do_not_depend_on_body(self):
        for status, expected in ((400, 'http_rejected'), (413, 'http_rejected'),
                                 (401, 'auth_rejected'), (403, 'auth_rejected'),
                                 (408, 'http_rejected'), (500, 'http_rejected')):
            with self.subTest(status=status):
                self.assertEqual(classify_http_error(status, None), expected)


if __name__ == '__main__':
    unittest.main()
