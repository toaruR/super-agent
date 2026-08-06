from util.cache import TTLCache
from util.retry import retry, retry_on


def test_retry_succeeds_eventually():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ValueError("not yet")
        return "ok"

    assert retry(flaky, attempts=3) == "ok"
    assert calls["n"] == 3


def test_retry_on_specific_exception():
    def always_key_error():
        raise KeyError("nope")

    try:
        retry_on(always_key_error, KeyError, attempts=2)
        raise AssertionError("should have raised")
    except KeyError:
        pass


def test_cache_hit():
    c = TTLCache(ttl=60)
    c.put("a", 1)
    assert c.get("a") == 1
    assert c.size() == 1


def test_cache_get_or_load():
    c = TTLCache(ttl=60)
    loaded = c.get_or_load("k", lambda: "value")
    assert loaded == "value"
    assert c.get("k") == "value"


def test_cache_expiry():
    c = TTLCache(ttl=-1)
    c.put("a", 1)
    assert c.get("a") is None
