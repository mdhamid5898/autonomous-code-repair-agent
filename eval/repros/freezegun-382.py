"""Repro for freezegun issue #382.

When time.time is assigned as a class attribute and invoked as a bound method,
the descriptor protocol passes the instance (``self``) as the first positional
argument. The real ``time.time`` tolerates this extra positional arg. Once
``freeze_time`` patches ``time.time`` with freezegun's fake callable, the fake
must also tolerate the bound-method ``self`` positional, otherwise:

    TypeError: fake_time() takes 0 positional arguments but 1 was given

This test asserts the correct, post-fix behavior: calling the frozen
``time.time`` as a bound method returns the frozen timestamp (a float) instead
of raising TypeError.
"""
import time

from freezegun import freeze_time


def test_frozen_time_callable_as_bound_method():
    frozen = "2012-01-14 03:21:34"

    with freeze_time(frozen):
        # Sanity: the plain (unbound) call works and returns a float.
        expected = time.time()
        assert isinstance(expected, float)

        # Assigning time.time onto a class turns it into a method; calling it
        # via an instance passes the instance as an implicit positional arg.
        class Clock:
            now = time.time

        result = Clock().now()

        assert isinstance(result, float)
        assert result == expected
