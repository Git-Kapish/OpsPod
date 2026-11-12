"""Money as integer minor units. Floats are rejected everywhere."""

from dataclasses import dataclass
from decimal import Decimal

from opspod.core.errors import OpsPodError


class MoneyError(OpsPodError):
    pass


@dataclass(frozen=True)
class Money:
    minor: int
    currency: str

    def __post_init__(self) -> None:
        if isinstance(self.minor, bool) or not isinstance(self.minor, int):
            raise MoneyError(f"minor units must be int, got {type(self.minor).__name__}")
        if self.minor < 0:
            raise MoneyError("money cannot be negative")
        if len(self.currency) != 3 or not self.currency.isupper():
            raise MoneyError(f"bad currency code {self.currency!r}")

    @classmethod
    def from_decimal(cls, value: Decimal, currency: str, exponent: int = 2) -> "Money":
        if not isinstance(value, Decimal):
            raise MoneyError("from_decimal requires Decimal")
        scaled = value * (10**exponent)
        if scaled != scaled.to_integral_value():
            raise MoneyError("sub-minor-unit precision")
        return cls(int(scaled), currency)

    def _same(self, other: "Money") -> None:
        if self.currency != other.currency:
            raise MoneyError(f"currency mismatch {self.currency} vs {other.currency}")

    def __add__(self, other: "Money") -> "Money":
        self._same(other)
        return Money(self.minor + other.minor, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        self._same(other)
        return Money(self.minor - other.minor, self.currency)  # raises if negative

    def __le__(self, other: "Money") -> bool:
        self._same(other)
        return self.minor <= other.minor

    def __lt__(self, other: "Money") -> bool:
        self._same(other)
        return self.minor < other.minor

    def __ge__(self, other: "Money") -> bool:
        return other <= self

    def __gt__(self, other: "Money") -> bool:
        return other < self

    def split_ratio(self, num: int, den: int) -> "Money":
        """Floor of self * num/den; exact integer math, never exceeds self when num <= den."""
        if den <= 0 or num < 0:
            raise MoneyError("bad ratio")
        return Money(self.minor * num // den, self.currency)
