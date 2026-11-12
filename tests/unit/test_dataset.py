import importlib.util
from collections import Counter
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "gen", Path(__file__).parents[2] / "scripts" / "generate_dataset.py"
)
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)


def test_deterministic():
    assert gen.generate() == gen.generate()


def test_size_splits_and_tags():
    cases = gen.generate()
    assert len(cases) >= 150
    assert {c["split"] for c in cases} == {"dev", "holdout"}
    tags = Counter(t for c in cases for t in c["tags"])
    for t in (
        "clear_cut",
        "ambiguous",
        "fraud",
        "injection",
        "missing_data",
        "conflicting_evidence",
        "high_value",
        "duplicate_webhook",
    ):
        assert tags[t] > 0, t


def test_money_is_integer():
    for c in gen.generate():
        assert isinstance(c["expected"]["amount"], int)
