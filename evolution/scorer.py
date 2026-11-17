"""Trusted scoring of returned data; never executes candidate code.

Coverage denominators are slot-count upper bounds, not packing guarantees.
"""
MAX_POSTS = 8
MAX_IMAGES = 8
MAX_BYTES = 4 * 1024 * 1024


def feasible(row):
    return all(type(row[k]) is int and 0 <= row[k] <= cap
               for k, cap in (("images", MAX_IMAGES), ("image_bytes", MAX_BYTES)))


def score_case(rows, output):
    eligible = [r for r in rows if feasible(r)]
    by_id = {r['id']: r for r in rows}
    reason = None
    if not isinstance(output, list) or any(type(i) is not int for i in output):
        reason = 'output must be a list of integers'
    elif len(output) > MAX_POSTS or len(set(output)) != len(output):
        reason = 'too many or duplicate IDs'
    elif any(i not in by_id or not feasible(by_id[i]) for i in output):
        reason = 'unknown or individually infeasible ID'
    else:
        selected = [by_id[i] for i in output]
        if sum(r['images'] for r in selected) > MAX_IMAGES or sum(r['image_bytes'] for r in selected) > MAX_BYTES:
            reason = 'resource limit exceeded'
        else:
            futures = [r for r in eligible if r['future']]
            if futures and not any(r['future'] and r['age'] == min(f['age'] for f in futures) for r in selected):
                reason = 'newest feasible future missing'
    if reason:
        return dict(valid=False, reason=reason, coverage=0.0, image_coverage=0.0,
                    historical_coverage=0.0, combined_score=0.0)
    def ratio(predicate):
        denominator = min(MAX_POSTS, sum(bool(predicate(r)) for r in eligible))
        return sum(bool(predicate(r)) for r in selected) / denominator if denominator else 1.0
    coverage = ratio(lambda r: True)
    images = ratio(lambda r: r['images'] > 0)
    history = ratio(lambda r: not r['future'])
    return dict(valid=True, reason=None, coverage=coverage, image_coverage=images,
                historical_coverage=history,
                combined_score=0.7 * coverage + 0.15 * images + 0.15 * history)


def score_suite(cases, outputs):
    """Score data returned from isolation; any missing/invalid case fails closed."""
    if not cases or not isinstance(outputs, list) or len(cases) != len(outputs):
        return dict(valid=False, combined_score=0.0, cases=[])
    results = [score_case(rows, output) for rows, output in zip(cases, outputs)]
    metrics = {key: sum(result[key] for result in results) / len(results)
               for key in ('coverage', 'image_coverage', 'historical_coverage')}
    valid = all(result['valid'] for result in results)
    return dict(valid=valid, cases=results, **metrics,
                combined_score=(0.7 * metrics['coverage'] + 0.15 * metrics['image_coverage']
                                + 0.15 * metrics['historical_coverage']) if valid else 0.0)
