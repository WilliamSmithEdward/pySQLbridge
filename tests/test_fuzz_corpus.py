"""Every fuzz seed through its target, so a finding that became a seed stays fixed.

The targets and what each may raise are in fuzz_targets.py; the Fuzz
workflow runs the same targets under Atheris. Here each seed only has to
come back without anything else escaping.
"""

from pathlib import Path

import pytest

from .fuzz_targets import TARGETS

CORPUS = Path(__file__).parent / "fuzz_corpus"

SEEDS = sorted(
    (target.name, seed.name)
    for target in CORPUS.iterdir() if target.is_dir()
    for seed in target.iterdir() if seed.is_file()
)


def test_every_target_has_seeds():
    assert {target for target, _ in SEEDS} == set(TARGETS)


@pytest.mark.parametrize(("target", "seed"), SEEDS)
def test_seed(target, seed):
    TARGETS[target]((CORPUS / target / seed).read_bytes())
