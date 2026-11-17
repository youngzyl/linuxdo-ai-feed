"""Tests for controller-owned bridge and offline configuration."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import controller_evaluator
import controller_runner
from sandbox import SandboxError


class ControllerTests(unittest.TestCase):
    def test_bridge_fails_closed_when_sandbox_unavailable(self):
        with tempfile.TemporaryDirectory() as d:
            candidate = Path(d) / 'untrusted.py'
            candidate.write_text("raise RuntimeError('must not execute')\n")
            with patch.object(controller_evaluator, 'run_candidate', side_effect=SandboxError('no docker')):
                self.assertEqual(controller_evaluator.evaluate(candidate),
                                 {'combined_score': 0.0, 'valid': 0.0})

    def test_config_has_single_worker_and_offline_only(self):
        config = controller_runner.make_config()
        self.assertEqual(config.evaluator.parallel_evaluations, 1)
        self.assertEqual(config.llm.models[0].retries, 0)
        self.assertIs(config.llm.models[0].init_client, controller_runner.make_offline_model)
        self.assertEqual(config.max_iterations, 2)

class RunnerIntegrationTests(unittest.TestCase):
    def test_live_gate_is_fail_closed(self):
        with self.assertRaises(RuntimeError):
            controller_runner.require_go(None)
        self.assertIsNone(controller_runner.require_go('GO'))

    def test_report_gate_requires_valid_nonregressing_holdout(self):
        good = {'valid': True, 'combined_score': 0.8}
        bad = {'valid': False, 'combined_score': 0.9}
        self.assertFalse(controller_runner.review_needed(good, bad, good, good))
        self.assertFalse(controller_runner.review_needed(good, good, good, {'valid': True, 'combined_score': 0.7}))
        self.assertTrue(controller_runner.review_needed({'valid': True, 'combined_score': 0.7}, good, good, good))

    def test_worker_factory_config_does_not_contain_key(self):
        class Service:
            def worker_factory(self):
                from responses_adapter import WorkerFactory
                return WorkerFactory('/tmp/nonexistent', 10)
        config = controller_runner.make_service_config(Service())
        self.assertIsNone(config.llm.models[0].api_key)
        self.assertEqual(config.llm.models[0].retries, 0)
        self.assertEqual(config.evaluator.parallel_evaluations, 1)


if __name__ == '__main__':
    unittest.main()
