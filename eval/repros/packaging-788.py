# packaging-788.py — Specifier '<' and '>' incorrectly detect prereleases=False
# Bug: Specifier('<3.0.0a8').prereleases returns False even though the specifier
# contains a pre-release version.  All other exclusive operators that use a
# pre-release specifier (>=, <=, ==) correctly return prereleases=True, but '<'
# and '>' do not.  As a result, Specifier('<3.0.0a8').contains('3.0.0a7') returns
# False even though 3.0.0a7 strictly satisfies the constraint.
from packaging.specifiers import Specifier


def test_less_than_prerelease_specifier_prereleases_property():
    # <3.0.0a8 — the specifier's own version is a pre-release;
    # prereleases should default to True (same as <=3.0.0a8, >=3.0.0a8, ==3.0.0a8).
    spec = Specifier("<3.0.0a8")
    assert spec.prereleases is True, (
        "Bug: Specifier('<3.0.0a8').prereleases is %r; "
        "expected True because the specifier version is a pre-release. "
        "(<=3.0.0a8.prereleases correctly returns True.)" % spec.prereleases
    )


def test_greater_than_prerelease_specifier_prereleases_property():
    spec = Specifier(">3.0.0a8")
    assert spec.prereleases is True, (
        "Bug: Specifier('>3.0.0a8').prereleases is %r; "
        "expected True because the specifier version is a pre-release." % spec.prereleases
    )


def test_less_than_specifier_contains_earlier_prerelease():
    # With prereleases correctly detected, contains() should work without override.
    spec = Specifier("<3.0.0a8")
    assert spec.contains("3.0.0a7"), (
        "Bug: Specifier('<3.0.0a8').contains('3.0.0a7') returned False; "
        "3.0.0a7 < 3.0.0a8 and the specifier uses a pre-release version, "
        "so pre-releases should be allowed by default."
    )
