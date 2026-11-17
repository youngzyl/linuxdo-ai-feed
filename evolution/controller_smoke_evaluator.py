"""Offline plumbing-only evaluator: never executes code; scores are NOT evidence."""
from pathlib import Path


def evaluate(program_path):
    source = Path(program_path).read_text(encoding='utf-8')
    return {'combined_score': 0.6 if '# offline-smoke-mutation' in source else 0.5}
