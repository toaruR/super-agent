"""Account balances built from a stream of postings.

A posting moves Money from one account to another. The ledger must always
balance: the sum of every account's balance is zero.
"""
from __future__ import annotations

from dataclasses import dataclass

from .money import Money


@dataclass(frozen=True)
class Posting:
    debit: str
    credit: str
    amount: Money


class Ledger:
    def __init__(self) -> None:
        self._postings: list[Posting] = []

    def post(self, debit: str, credit: str, amount: Money) -> None:
        if amount.cents <= 0:
            raise ValueError("amount must be positive")
        if debit == credit:
            raise ValueError("debit and credit must differ")
        self._postings.append(Posting(debit, credit, amount))

    def balance(self, account: str) -> Money:
        total = 0
        for p in self._postings:
            if p.debit == account:
                total += p.amount.cents
            elif p.credit == account:
                total -= p.amount.cents
        return Money(total)

    def accounts(self) -> list[str]:
        seen: set[str] = set()
        for p in self._postings:
            seen.add(p.debit)
            seen.add(p.credit)
        return sorted(seen)

    def is_balanced(self) -> bool:
        return sum(self.balance(a).cents for a in self.accounts()) == 0
