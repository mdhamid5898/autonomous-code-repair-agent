"""Repro for gruns/icecream#225.

icecream v2.2.0 guards colorama.init() behind sys.platform == 'win', so on
non-Windows platforms the ANSI color escape sequences emitted by ic() are no
longer stripped when stderr is redirected to a non-TTY stream. Version 2.1.4
called colorama.init() unconditionally, and colorama wraps a non-TTY stream
with an ANSI-stripping proxy, so redirected/captured output was plain text.

The correct, post-fix behavior: when ic() writes to a non-TTY stream (here an
io.StringIO standing in for stderr redirected to a file), the captured text
must contain NO raw ANSI escape sequences (\\x1b[ ...).
"""

import io
import sys

from icecream import ic


def test_ic_does_not_leak_ansi_to_nontty_stderr():
    # ic()'s default output routes through sys.stderr (via stderr_print), and
    # the stream is read at call time, so redirecting sys.stderr to a non-TTY
    # StringIO mirrors redirecting stderr to a file on the command line.
    buf = io.StringIO()
    assert not buf.isatty()  # sanity: this is a non-TTY stream

    saved_stderr = sys.stderr
    sys.stderr = buf
    try:
        value = 123
        ic(value)
    finally:
        sys.stderr = saved_stderr

    captured = buf.getvalue()

    # Output must have actually been produced (guards against ic() being
    # disabled or routed elsewhere, which would make the assertion vacuous).
    assert "value" in captured and "123" in captured, repr(captured)

    # Post-fix: no raw ANSI escape sequences should reach a non-TTY stream.
    # On buggy v2.2.0 (non-Windows) the SolarizedDark color codes leak through.
    assert "\x1b[" not in captured, (
        "ic() leaked raw ANSI escape sequences into a non-TTY stream: "
        + repr(captured)
    )
