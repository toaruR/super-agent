"""Money as integer minor units. Avoids float rounding."""
from __future__ import annotations


class Money:
    __slots__ = ("cents",)

    def __init__(self, cents: int) -> None:
        if not isinstance(cents, int):
            raise TypeError("cents must be int")
        self.cents = cents

    @classmethod
    def parse(cls, text: str) -> "Money":
        text = text.strip()
        neg = text.startswith("-")
        if neg:
            text = text[1:]
        if "." in text:
            whole, frac = text.split(".", 1)
            if len(frac) != 2:
                raise ValueError(f"bad minor units: {text!r}")
        else:
            whole, frac = text, "00"
        if not whole.isdigit() or not frac.isdigit():
            raise ValueError(f"not a number: {text!r}")
        total = int(whole) * 100 + int(frac)
        return cls(-total if neg else total)

    def __add__(self, other: "Money") -> "Money":
        return Money(self.cents + other.cents)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Money) and other.cents == self.cents

    def __repr__(self) -> str:
        sign = "-" if self.cents < 0 else ""
        a = abs(self.cents)
        return f"{sign}{a // 100}.{a % 100:02d}"
