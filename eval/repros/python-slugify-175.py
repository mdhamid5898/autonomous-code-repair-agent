"""Repro for python-slugify issue #175.

The CLI accepts --regex-pattern and parse_args() correctly stores it on the
namespace as `regex_pattern`, but slugify_params() builds the kwargs dict for
slugify() WITHOUT a `regex_pattern` key. As a result the custom regex is
silently dropped and the CLI ignores --regex-pattern.

Per the issue, running:
    slugify --regex-pattern "[^-a-z0-9_]+" "___This is a test___"
should produce "___this-is-a-test___" (underscores preserved), but the buggy
code produces "this-is-a-test".
"""

from slugify import slugify
from slugify.__main__ import parse_args, slugify_params


def test_cli_forwards_regex_pattern_to_slugify():
    argv = ["slugify", "--regex-pattern", "[^-a-z0-9_]+", "___This is a test___"]

    args = parse_args(argv)
    params = slugify_params(args)

    # The custom regex must actually take effect on the slugified output.
    result = slugify(**params)

    assert result == "___this-is-a-test___"
