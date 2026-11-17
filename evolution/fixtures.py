"""Synthetic fixed training/holdout replay inputs. Never put holdout in prompts."""
import random

TRAIN_SEED = 3701
HOLDOUT_SEED = 81013
MIB = 1024 * 1024


def make_cases(seed):
    rng = random.Random(seed)
    serial = iter(rng.sample(range(1000, 9000000), 200))
    offset = rng.randrange(1, 10000)
    def row(age, images=0, size=0, future=False):
        return dict(id=next(serial), future=future, age=age + offset, images=images, image_bytes=size)
    return [
        [],
        [row(1), row(2, future=True)],
        [row(i) for i in range(12)],
        [row(i, rng.randint(1, 3), MIB // 2, i == 0) for i in range(10)],
        [row(0, 1, MIB, True), row(1, 2, MIB, True), row(2), row(3, 1, MIB)],
        [row(0, 9, 0, True), row(1, 0, 4 * MIB + 1, True), row(2, 1, 10, True), row(3)],
        [row(0, 0, 0, True), row(1, 7, 3 * MIB)] + [row(i, 1, MIB // 8) for i in range(2, 11)],
        [row(1, 1, MIB // 4, True), row(1, 1, MIB // 4, True), row(1)],
        [row(i, 1, 0, i == 0) for i in range(10)],
    ]


def training_cases():
    return make_cases(TRAIN_SEED)


def holdout_cases():
    return make_cases(HOLDOUT_SEED)
