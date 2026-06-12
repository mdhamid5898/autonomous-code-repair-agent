"""Repro for freezegun issue #558.

When freezegun freezes time it replaces ``time.perf_counter`` with its own
``fake_perf_counter``. The original ``perf_counter`` is a C builtin that
silently ignores extra positional arguments. Some libraries (e.g. fontTools'
``Timer``) store the timer as a *class attribute*::

    class Timer:
        _time = timeit.default_timer   # == time.perf_counter

    ...
    elapsed = self._time()             # bound-method call -> passes `self`

Because a plain function stored on the class becomes a bound method, calling
``self._time()`` passes ``self`` as the first positional argument. With the
buggy fake this raises:

    TypeError: fake_perf_counter() takes 0 positional arguments but 1 was given

The fix (same shape as #382, which fixed the identical problem for
``fake_monotonic``) is to let the fake accept and ignore extra positional /
keyword arguments. This test asserts that post-fix behavior: a frozen
``time.perf_counter`` invoked as a bound method must return a float, not raise.
"""
import time

from freezegun import freeze_time


def test_fake_perf_counter_accepts_bound_method_call() -> None:
    with freeze_time("2024-03-12 12:30:00"):
        # Capture freezegun's fake_perf_counter and bind it as a CLASS
        # attribute (mirroring fontTools' Timer._time = timeit.default_timer).
        captured_perf_counter = time.perf_counter

        class Timer:
            _time = captured_perf_counter

            def measure(self) -> float:
                # Bound-method call: Python passes `self` positionally.
                return self._time()

        # On the buggy version this raises TypeError; post-fix it returns a float.
        result = Timer().measure()

    assert isinstance(result, float)
