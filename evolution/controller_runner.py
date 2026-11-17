"""Run offline smoke or an explicitly authorized receipt-bounded experiment."""
import argparse
import asyncio
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from openevolve.config import Config, DatabaseConfig, EvaluatorConfig, LLMConfig, LLMModelConfig, PromptConfig
from openevolve.controller import OpenEvolve
from openevolve.llm.base import LLMInterface

ROOT = Path(__file__).resolve().parent
PIN = 'a7950ffc0150f1e7282a39d1ffac57d675dc5551'
SYSTEM_CONSTRAINTS = ('Optimize only choose_batch(rows)->list[int]. Row keys id(int),future(bool),age(nonnegative),images(nonnegative int),image_bytes(nonnegative int). '
                      'Return distinct supplied IDs, <=8 rows, total images<=8 and image_bytes<=4194304. '
                      'Exclude individually infeasible rows. If any individually feasible future=True row exists, ALWAYS include its smallest-age member. '
                      'Keep this interface and safety constraints; no I/O, tools, imports of external services, or evaluator changes. '
                      'Improve packing under the fixed scorer; retaining a valid baseline is preferable to invalid outputs.')


class OfflineModel(LLMInterface):
    async def generate(self, prompt, **kwargs):
        return await self.generate_with_context('', [{'content': prompt}], **kwargs)

    async def generate_with_context(self, system_message, messages, **kwargs):
        code = (ROOT / 'seed.py').read_text(encoding='utf-8')
        return '```python\n' + code + '\n# offline-smoke-mutation\n```'


def make_offline_model(config):
    return OfflineModel()


def make_config():
    model = LLMModelConfig(name='offline-static', init_client=make_offline_model,
                           max_tokens=6000, retries=0)
    return Config(max_iterations=2, checkpoint_interval=1, random_seed=42,
                  diff_based_evolution=False, language='python', max_code_length=10000,
                  prompt=PromptConfig(system_message=SYSTEM_CONSTRAINTS),
                  llm=LLMConfig(models=[model], evaluator_models=[model], retries=0,
                                max_tokens=6000),
                  database=DatabaseConfig(num_islands=1, population_size=10,
                                          archive_size=10, log_prompts=False),
                  evaluator=EvaluatorConfig(parallel_evaluations=1, max_retries=0,
                                            cascade_evaluation=False, timeout=30))


async def smoke(output):
    output = Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise RuntimeError('smoke output must be an empty directory')
    controller = OpenEvolve(str(ROOT / 'seed.py'), str(ROOT / 'controller_smoke_evaluator.py'),
                            make_config(), output_dir=str(output))
    best = await asyncio.wait_for(controller.run(iterations=2), timeout=120)
    controller._save_checkpoint(controller.database.last_iteration)
    if best is None or len(controller.database.programs) < 2 or not controller.database.archive:
        raise RuntimeError('controller failed to populate population/archive')
    checkpoints = sorted(str(p.relative_to(output)) for p in (output / 'checkpoints').iterdir())
    report = {'mode': 'offline_mock_plumbing_only', 'paid_calls': 0,
              'expected_source_commit_unverified_at_runtime': PIN, 'controller': 'openevolve.controller.OpenEvolve',
              'population_count': len(controller.database.programs),
              'archive_count': len(controller.database.archive), 'best_id': best.id,
              'best_mock_score': best.metrics.get('combined_score'), 'checkpoints': checkpoints,
              'safety_and_candidate_quality_evidence': False}
    (output / 'smoke-report.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def require_go(value):
    if value != 'GO':
        raise RuntimeError('live execution requires --go GO after independent sandbox review')


def make_service_config(service):
    """Build a credential-free worker configuration for a future gated runner."""
    from responses_adapter import install_worker_client
    config = make_config()
    for model in config.llm.models + config.llm.evaluator_models:
        install_worker_client(model, service)
    config.llm.api_key = None
    return config


def unique_mutation_count(programs):
    return len({hashlib.sha256(p.code.encode('utf-8')).hexdigest()
                for p in programs.values() if p.parent_id is not None})


def review_needed(seed_train, best_train, seed_holdout, best_holdout):
    scores = (seed_train, best_train, seed_holdout, best_holdout)
    return (all(x.get('valid') is True and isinstance(x.get('combined_score'), (int, float))
                and math.isfinite(x['combined_score']) for x in scores)
            and best_train['combined_score'] > seed_train['combined_score']
            and best_holdout['combined_score'] >= seed_holdout['combined_score'])


def evaluate_suite(code, cases):
    from sandbox import run_candidate, SandboxError
    from scorer import score_suite
    try:
        result = score_suite(cases, [run_candidate(code, case) for case in cases])
    except (SandboxError, OSError, UnicodeError, ValueError, RecursionError):
        return {'valid': False, 'combined_score': 0.0, 'error': 'sandbox failure'}
    # Avoid retaining held-out fixture contents or individual selections.
    return {key: result[key] for key in ('valid', 'combined_score', 'coverage',
                                         'image_coverage', 'historical_coverage')}


async def offline_evaluate(output, deadline):
    from fixtures import training_cases, holdout_cases
    output = Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise RuntimeError('output must be an empty directory')
    controller = OpenEvolve(str(ROOT / 'seed.py'), str(ROOT / 'controller_evaluator.py'),
                            make_config(), output_dir=str(output))
    best = await asyncio.wait_for(controller.run(iterations=2), timeout=deadline)
    controller._save_checkpoint(controller.database.last_iteration)
    if best is None or len(controller.database.programs) < 2 or not controller.database.archive:
        raise RuntimeError('controller failed to populate population/archive')
    seed = (ROOT / 'seed.py').read_text(encoding='utf-8')
    train = training_cases()
    holdout = holdout_cases()
    seed_train = evaluate_suite(seed, train)
    best_train = evaluate_suite(best.code, train)
    seed_holdout = evaluate_suite(seed, holdout)
    best_holdout = evaluate_suite(best.code, holdout)
    report = {'mode': 'offline_static_mutation_docker_evaluator', 'paid_calls': 0,
              'seed_training': seed_train, 'best_training': best_train,
              'seed_holdout': seed_holdout, 'best_holdout': best_holdout,
              'review_needed': review_needed(seed_train, best_train, seed_holdout, best_holdout),
              'safety_valid': all(x.get('valid') is True for x in
                                  (seed_train, best_train, seed_holdout, best_holdout)),
              'population_count': len(controller.database.programs),
              'archive_count': len(controller.database.archive), 'best_id': best.id,
              'checkpoints': sorted(str(p.relative_to(output)) for p in (output / 'checkpoints').iterdir()),
              'model': 'offline-static', 'tier': None, 'usage': None,
              'promotion': 'not authorized',
              'coverage_denominator': 'slot-count upper bound, not packing guarantee'}
    (output / 'offline-report.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


async def live_evaluate(output, receipt_root, credential_file, deadline, resume_sha256=None, canary_id=None):
    from fixtures import training_cases, holdout_cases
    from responses_adapter import start_isolated_service
    if canary_id is not None:
        import re
        if not re.fullmatch(r'canary-\d{4}-\d{2}-\d{2}-[0-9a-f]{32}', canary_id) or resume_sha256 is not None:
            raise ValueError('invalid canary admission')
    output, receipts = Path(output).resolve(), Path(receipt_root).resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise RuntimeError('output must be an empty directory')
    if receipts == output or output in receipts.parents or receipts in output.parents:
        raise ValueError('receipt and output directories must be separate')
    output.mkdir(parents=True, exist_ok=True)
    receipts.mkdir(parents=True, exist_ok=True)
    before = set(receipts.glob('????-??-??.json'))
    service = None
    report = {'mode': 'live', 'promotion': 'not authorized', 'review_needed': False,
              'status': 'failed', 'model_requested': 'gpt-6-luna', 'tier_requested': 'flex',
              'reasoning_requested': 'high', 'attempts': [], 'error': None}
    try:
        service = start_isolated_service(receipts, credential_file=credential_file,
                                         deadline_seconds=deadline, resume_sha256=resume_sha256, canary_id=canary_id)
        controller = OpenEvolve(str(ROOT / 'seed.py'), str(ROOT / 'controller_evaluator.py'),
                                make_service_config(service), output_dir=str(output))
        best = await asyncio.wait_for(controller.run(iterations=2), timeout=deadline)
        controller._save_checkpoint(controller.database.last_iteration)
        if best is None or len(controller.database.programs) < 2 or not controller.database.archive:
            raise RuntimeError('controller failed to populate population/archive')
        seed = (ROOT / 'seed.py').read_text(encoding='utf-8')
        train, holdout = training_cases(), holdout_cases()
        scores = [evaluate_suite(code, cases) for code, cases in
                  ((seed, train), (best.code, train), (seed, holdout), (best.code, holdout))]
        report.update(dict(zip(('seed_training', 'best_training', 'seed_holdout', 'best_holdout'), scores)))
        report.update(review_needed=review_needed(*scores),
                      safety_valid=all(x.get('valid') is True for x in scores),
                      population_count=len(controller.database.programs),
                      archive_count=len(controller.database.archive), best_id=best.id,
                      unique_mutations=unique_mutation_count(controller.database.programs))
        (output / 'selected-candidate.py').write_text(best.code, encoding='utf-8')
        report['candidate_artifact'] = 'selected-candidate.py'
        report['status'] = 'completed' if report['safety_valid'] else 'invalid_evaluation'
    except Exception:
        report['error'] = 'controller or adapter failed; inspect durable receipts'
    finally:
        if service is not None:
            service.stop()
        new = set(receipts.glob('????-??-??.json')) - before
        if canary_id is not None:
            candidate = receipts / (canary_id + '.json')
            new = {candidate} if candidate.is_file() else set()
        if resume_sha256 is not None:
            import datetime as dt
            existing = receipts / (dt.datetime.now(dt.timezone.utc).date().isoformat() + '.json')
            new = {existing} if existing in before and existing.is_file() else set()
        if len(new) == 1:
            receipt_path = new.pop()
            report['receipt'] = str(receipt_path)
            report['attempts'] = json.loads(receipt_path.read_text())['attempts']
            report['provider_attempts'] = len(report['attempts'])
            report['provider_usage_source'] = 'durable receipt; framework iteration counters can double-count cached responses'
            if not any(a.get('outcome') == 'success' for a in report['attempts']):
                deferred = any(a.get('outcome') == 'capacity_deferred' for a in report['attempts']) and not any(a.get('outcome') in ('unknown', 'pending') for a in report['attempts'])
                report['status'] = 'capacity_deferred' if deferred else 'failed'
                report['error'] = 'provider Flex capacity deferred; next UTC day only' if deferred else 'no successful paid mutation recorded'
            elif any(a.get('outcome') in ('unknown', 'pending') and a.get('disposition') != 'abandoned_no_retry' for a in report['attempts']):
                report['status'] = 'failed'
                report['error'] = 'unresolved paid attempt; no retry authorized'
        else:
            report['error'] = 'admission failed or receipt ambiguous; no retry authorized'
            report['status'] = 'failed'
        (output / 'live-report.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def run_bounded(output, deadline, *, live=False, receipts=None, credential_file=None, resume_sha256=None, canary_id=None):
    """Bound controller, workers and holdout, including executor shutdown."""
    if not math.isfinite(deadline) or deadline <= 0:
        raise ValueError('deadline must be positive and finite')
    env = {k: v for k, v in os.environ.items()
           if not any(x in k.upper() for x in ('KEY', 'TOKEN', 'SECRET', 'PASS', 'CREDENTIAL'))}
    argv = [sys.executable, str(Path(__file__).resolve()),
            '--live-worker' if live else '--offline-worker', str(output), '--deadline', str(deadline)]
    if live:
        argv += ['--go', 'GO', '--receipts', str(receipts), '--credential-file', str(credential_file)]
        if resume_sha256 is not None:
            argv += ['--resume-sha256', resume_sha256]
        if canary_id is not None:
            argv += ['--canary-id', canary_id]
    proc = subprocess.Popen(argv, start_new_session=True, env=env)
    try:
        return proc.wait(timeout=deadline)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        raise TimeoutError('whole-run deadline exceeded') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--offline-smoke', metavar='EMPTY_OUTPUT_DIR')
    mode.add_argument('--offline-docker', metavar='EMPTY_OUTPUT_DIR')
    mode.add_argument('--offline-worker', metavar='EMPTY_OUTPUT_DIR', help=argparse.SUPPRESS)
    mode.add_argument('--live', metavar='EMPTY_OUTPUT_DIR')
    mode.add_argument('--live-worker', metavar='EMPTY_OUTPUT_DIR', help=argparse.SUPPRESS)
    parser.add_argument('--go', default=None)
    parser.add_argument('--receipts', help='persistent private receipt directory')
    parser.add_argument('--credential-file', help='private owner-only OpenAI key file')
    parser.add_argument('--deadline', type=float, default=600)
    parser.add_argument('--resume-sha256', help='operator-approved abandoned receipt digest')
    parser.add_argument('--canary-id', help='explicit operator canary receipt id')
    args = parser.parse_args()
    if args.live or args.live_worker:
        require_go(args.go)
        if not args.receipts or not args.credential_file:
            parser.error('live requires --receipts and --credential-file')
        if args.live:
            raise SystemExit(run_bounded(args.live, args.deadline, live=True,
                                         receipts=args.receipts, credential_file=args.credential_file,
                                         resume_sha256=args.resume_sha256, canary_id=args.canary_id))
        report = asyncio.run(live_evaluate(args.live_worker, args.receipts, args.credential_file, args.deadline, resume_sha256=args.resume_sha256, canary_id=args.canary_id))
        print(json.dumps(report, indent=2))
        if report['status'] not in ('completed', 'capacity_deferred'):
            raise SystemExit(1)
    elif args.offline_smoke:
        print(json.dumps(asyncio.run(smoke(args.offline_smoke)), indent=2))
    elif args.offline_docker:
        raise SystemExit(run_bounded(args.offline_docker, args.deadline))
    else:
        report = asyncio.run(offline_evaluate(args.offline_worker, args.deadline))
        print(json.dumps(report, indent=2))
        if not report['safety_valid']:
            raise SystemExit('offline evaluation failed closed: sandbox or candidate invalid')


if __name__ == '__main__':
    main()
