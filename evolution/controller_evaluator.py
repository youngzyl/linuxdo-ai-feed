"""Trusted OpenEvolve evaluator bridge. Candidate code runs only in Docker."""
from pathlib import Path

from fixtures import training_cases
from sandbox import run_candidate, SandboxError
from scorer import score_suite


def evaluate(program_path):
    code = Path(program_path).read_text(encoding='utf-8')
    cases = training_cases()
    try:
        result = score_suite(cases, [run_candidate(code, case) for case in cases])
    except (SandboxError, OSError, UnicodeError):
        return {'combined_score': 0.0, 'valid': 0.0}
    return {k: float(result[k]) for k in
            ('combined_score', 'valid', 'coverage', 'image_coverage', 'historical_coverage')}
