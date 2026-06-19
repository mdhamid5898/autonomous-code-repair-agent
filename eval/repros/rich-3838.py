# rich-3838.py — background color bleeds past last character when soft_wrap=True
#
# Bug: Console.print() with soft_wrap=True applies a style (incl. background
# color) to ALL segments in the rendered stream — including the trailing newline
# segment.  The ANSI reset escape (\x1b[0m) is emitted AFTER the newline
# instead of before it, causing the background to paint the newline and
# potentially subsequent lines.
#
# Fix: introduce Segment.split_lines_terminator() (segment.py) and use it in
# Console._check_buffer() (console.py) to apply the style line-by-line,
# emitting a plain newline OUTSIDE the styled region.
import pytest
from rich.console import Console


def test_soft_wrap_background_resets_before_newline():
    """With soft_wrap=True and a background style the ANSI reset must appear
    BEFORE the \\n, not after it.

    Pre-fix output:  '\\x1b[34;47msoft wrap is on\\n\\x1b[0mNext line\\n'
                                              ^^^ reset is AFTER newline
    Post-fix output: '\\x1b[34;47msoft wrap is on\\x1b[0m\\nNext line\\n'
                                              ^^^ reset is BEFORE newline
    """
    console = Console(color_system="standard", width=80, force_terminal=True)
    with console.capture() as capture:
        console.print("soft wrap is on", style="blue on white", soft_wrap=True)
        console.print("Next line")

    output = capture.get()
    expected = "\x1b[34;47msoft wrap is on\x1b[0m\nNext line\n"

    assert output == expected, (
        f"Bug: background style bleeds past last character with soft_wrap=True.\n"
        f"  got:      {repr(output)}\n"
        f"  expected: {repr(expected)}"
    )
