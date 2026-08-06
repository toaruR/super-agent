from ledger.accounts import Ledger
from ledger.money import Money


def test_parse_and_repr():
    assert Money.parse("10.50").cents == 1050
    assert repr(Money.parse("10.50")) == "10.50"
    assert Money.parse("3").cents == 300


def test_simple_transfer():
    lg = Ledger()
    lg.post("expenses", "cash", Money.parse("10.50"))
    assert lg.balance("expenses") == Money.parse("10.50")
    assert lg.balance("cash") == Money(-1050)


def test_ledger_balances_to_zero():
    lg = Ledger()
    lg.post("expenses", "cash", Money.parse("10.50"))
    lg.post("rent", "cash", Money.parse("100.00"))
    assert lg.is_balanced()


def test_rejects_bad_postings():
    lg = Ledger()
    try:
        lg.post("a", "a", Money.parse("1.00"))
        raise AssertionError("should reject same account")
    except ValueError:
        pass
