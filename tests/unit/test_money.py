from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from opspod.domain.money import Money, MoneyError

amounts = st.integers(0, 10**12)


@given(amounts, amounts)
def test_add_sub_roundtrip(a, b):
    x, y = Money(a, "USD"), Money(b, "USD")
    assert (x + y) - y == x
    assert (x + y).minor == a + b


@given(amounts, st.integers(0, 1000), st.integers(1, 1000))
def test_split_ratio_never_exceeds(a, num, den):
    num = min(num, den)
    assert Money(a, "USD").split_ratio(num, den) <= Money(a, "USD")


@given(amounts, st.integers(1, 100))
def test_split_parts_sum_within_total(a, n):
    parts = [Money(a, "USD").split_ratio(1, n) for _ in range(n)]
    assert sum(p.minor for p in parts) <= a


def test_rejects_floats_negatives_mixed_currency():
    with pytest.raises(MoneyError):
        Money(1.5, "USD")  # type: ignore[arg-type]
    with pytest.raises(MoneyError):
        Money(True, "USD")  # type: ignore[arg-type]
    with pytest.raises(MoneyError):
        Money(-1, "USD")
    with pytest.raises(MoneyError):
        Money(1, "usd")
    with pytest.raises(MoneyError):
        Money(1, "USD") + Money(1, "EUR")
    with pytest.raises(MoneyError):
        Money(1, "USD") - Money(2, "USD")


def test_from_decimal():
    assert Money.from_decimal(Decimal("12.34"), "USD").minor == 1234
    with pytest.raises(MoneyError):
        Money.from_decimal(Decimal("0.001"), "USD")
    with pytest.raises(MoneyError):
        Money.from_decimal(1.5, "USD")  # type: ignore[arg-type]
