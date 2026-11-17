"""Baseline: stable newest-first greedy packing of individually eligible rows."""

def choose_batch(rows):
    chosen = []
    images = size = 0
    for row in sorted(rows, key=lambda r: r['age']):
        if len(chosen) == 8:
            break
        if not (type(row['images']) is int and type(row['image_bytes']) is int
                and 0 <= row['images'] <= 8 and 0 <= row['image_bytes'] <= 4 * 1024 * 1024):
            continue
        if images + row['images'] > 8 or size + row['image_bytes'] > 4 * 1024 * 1024:
            continue
        chosen.append(row['id'])
        images += row['images']
        size += row['image_bytes']
    return chosen
