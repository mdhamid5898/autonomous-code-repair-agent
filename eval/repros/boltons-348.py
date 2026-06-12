# boltons-348.py — LRU.values() returns stale value after an entry is updated
# Bug: after setting cache[key] = new_value, calling cache.values() still returns
# the old value because the doubly-linked-list node is updated but the internal
# dict entry still points to the old node.
from boltons.cacheutils import LRU


def test_lru_values_reflect_update():
    cache = LRU(max_size=5)
    cache["a"] = "original"
    cache["b"] = "other"

    # Update the value for an existing key
    cache["a"] = "updated"

    values = list(cache.values())
    assert "updated" in values, (
        "Bug: LRU.values() still contains old value after update. values=%r" % values
    )
    assert "original" not in values, (
        "Bug: LRU.values() still contains stale 'original' after update. values=%r" % values
    )


def test_lru_dict_reflects_update():
    cache = LRU(max_size=5)
    cache["x"] = 1
    cache["x"] = 99

    as_dict = dict(cache)
    assert as_dict["x"] == 99, (
        "Bug: dict(lru_cache) returns stale value %r after update" % as_dict["x"]
    )
