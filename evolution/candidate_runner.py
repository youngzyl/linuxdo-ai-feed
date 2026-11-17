"""Trusted container-side entrypoint; never import this module on the host."""
import json
import sys


def main():
    payload = json.load(sys.stdin)
    namespace = {'__name__': '__candidate__'}
    exec(compile(payload['code'], '<candidate>', 'exec'), namespace)
    result = namespace['choose_batch'](payload['rows'])
    sys.stdout.write(json.dumps(result, allow_nan=False, separators=(',', ':')))
    sys.stdout.flush()


if __name__ == '__main__':
    main()
