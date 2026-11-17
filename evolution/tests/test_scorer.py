import unittest


def row(i, future=False, age=1, images=0, image_bytes=0):
    return dict(id=i, future=future, age=age, images=images, image_bytes=image_bytes)


class ScorerTests(unittest.TestCase):
    def test_scoring_valid_coverage_and_empty_categories(self):
        from scorer import score_case
        result = score_case([row(1), row(2, images=1)], [1])
        self.assertTrue(result['valid'])
        self.assertEqual(result['coverage'], 0.5)
        self.assertEqual(result['image_coverage'], 0)
        self.assertEqual(result['historical_coverage'], 0.5)
        self.assertEqual(score_case([], [])['combined_score'], 1)

    def test_fixtures_are_fixed_distinct_and_holdout_differs(self):
        from fixtures import training_cases, holdout_cases
        train, holdout = training_cases(), holdout_cases()
        self.assertEqual(train, training_cases())
        self.assertEqual(holdout, holdout_cases())
        self.assertNotEqual(train, holdout)
        strip_ids = lambda cases: [[{k: v for k, v in r.items() if k != 'id'} for r in case] for case in cases]
        self.assertNotEqual(strip_ids(train), strip_ids(holdout))
        self.assertGreaterEqual(len(train), 8)
        self.assertTrue(any(not case for case in train))
        self.assertTrue(any(any(r['images'] > 8 or r['image_bytes'] > 4 * 1024 * 1024 for r in case) for case in train))
        self.assertTrue(any(any(r['images'] > 0 and r['image_bytes'] == 0 for r in case) for case in train))

    def test_fixture_includes_greedy_trap_with_better_valid_packing(self):
        from fixtures import training_cases
        from seed import choose_batch
        from scorer import score_case
        cases = training_cases()
        improves = False
        for rows in cases:
            if len(rows) < 3:
                continue
            baseline = score_case(rows, choose_batch(rows))
            for excluded in rows:
                candidate = choose_batch([r for r in rows if r is not excluded])
                score = score_case(rows, candidate)
                if score['valid'] and score['combined_score'] > baseline['combined_score']:
                    improves = True
        self.assertTrue(improves)

    def test_seed_packs_newest_first_and_skips_oversize(self):
        from seed import choose_batch
        rows = [row(1, age=3), row(2, age=1, images=9), row(3, age=2, images=8), row(4, age=4, images=1)]
        self.assertEqual(choose_batch(rows), [3, 1])

    def test_invalid_output_and_future_constraints(self):
        from scorer import score_case
        rows = [row(1, future=True, age=0), row(2, future=True, age=1), row(3, images=8, image_bytes=4*1024*1024), row(4, images=1), row(5, images=9, future=True, age=-1)]
        for output in (None, (1,), [True], [1, 1], [99], [5], [2], [1, 3, 4], list(range(1, 10))):
            with self.subTest(output=output):
                result = score_case(rows, output)
                self.assertFalse(result['valid'])
                self.assertEqual(result['combined_score'], 0)
        self.assertTrue(score_case(rows, [1, 3])['valid'])
        self.assertTrue(score_case([row(1, images=-1, future=True), row(2, future=True)], [2])['valid'])

    def test_suite_score_uses_mean_and_any_invalid_zeroes_combined(self):
        from scorer import score_suite
        cases = [[row(1)], [row(2), row(3, images=1)]]
        result = score_suite(cases, [[1], [2]])
        self.assertTrue(result['valid'])
        self.assertAlmostEqual(result['combined_score'], .7 * .75 + .15 * .5 + .15 * .75)
        self.assertEqual(score_suite(cases, [[1], [2, 2]])['combined_score'], 0)
        self.assertEqual(score_suite(cases, [[1]])['combined_score'], 0)


if __name__ == '__main__':
    unittest.main()
