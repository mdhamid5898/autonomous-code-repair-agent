"""best-of-N selection logic — deterministic, no Docker/API.

Mocks the executor (tracks a 'live diff' + grades patches from a scripted map) and
monkeypatches run_single (the per-attempt agent run) so we can assert the winner-selection,
early-stop, and fallback behavior of run_best_of_n without spinning a container or hitting an API.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import swebench_solve as s  # noqa: E402


class FakeEx:
    """Stand-in for SweBenchExecutor: a single mutable 'live diff' + a scripted grade map."""

    def __init__(self, grades):
        self.grades = grades
        self._cur = ""

    def reset_clean(self):
        self._cur = ""

    def apply_patch(self, p):
        self._cur = p

    def git_diff(self):
        return self._cur

    def grade_patch(self, p):
        """Grade map values: bool (stable verdict) OR list of bools (scripted per-call sequence,
        e.g. [True, False] = flaky pass-then-fail; exhausted list repeats its last value)."""
        v = self.grades.get(p, False)
        if isinstance(v, list):
            ok = bool(v.pop(0)) if len(v) > 1 else bool(v[0]) if v else False
        else:
            ok = bool(v)
        return {"resolved": ok, "exit": 0 if ok else 1, "summary": "pass" if ok else "fail"}


def _patch_run_single(monkeypatch, patches):
    """Make run_single 'produce' patches[i] as the live diff on the i-th call."""
    state = {"i": 0}

    def fake(instance, model, max_steps, verbose, governed, ex, temperature=0.0, seed_hint=""):
        ex._cur = patches[state["i"]]
        meta = {"steps": 5 + state["i"], "stop_reason": "submitted", "submitted_summary": f"fix#{state['i']}"}
        state["i"] += 1
        return meta

    monkeypatch.setattr(s, "run_single", fake)


def _run(monkeypatch, patches, grades, n=3, early_stop=True):
    _patch_run_single(monkeypatch, patches)
    ex = FakeEx(grades)
    res = s.run_best_of_n({"instance_id": "x", "repo": "r"}, "m", 10, False, ex, n=n, early_stop=early_stop)
    return res, ex.git_diff()


def test_first_passing_candidate_wins_and_is_applied(monkeypatch):
    res, applied = _run(monkeypatch, ["A", "B", "C"], {"C": True})
    assert res["winner_index"] == 2
    assert res["n_passing"] == 1
    assert res["n_candidates"] == 3
    assert res["stop_reason"] == "bestofn_pass"
    assert applied == "C"  # winner left applied for capture + official grade


def test_early_stop_stops_after_first_pass(monkeypatch):
    res, applied = _run(monkeypatch, ["A", "B", "C"], {"A": True})
    assert res["n_candidates"] == 1  # did not sample B or C
    assert res["winner_index"] == 0
    assert applied == "A"


def test_no_early_stop_samples_all_and_picks_first_pass(monkeypatch):
    res, applied = _run(monkeypatch, ["A", "B", "C"], {"B": True, "C": True}, early_stop=False)
    assert res["n_candidates"] == 3
    assert res["n_passing"] == 2
    assert res["winner_index"] == 1
    assert applied == "B"


def test_fallback_to_largest_patch_when_none_pass(monkeypatch):
    res, applied = _run(monkeypatch, ["short", "longest_patch", "mid"], {})
    assert res["n_passing"] == 0
    assert res["stop_reason"] == "bestofn_nopass"
    assert applied == "longest_patch"
    assert res["edited"] is True


def test_all_empty_patches_reports_nopass_and_not_edited(monkeypatch):
    res, applied = _run(monkeypatch, ["", "", ""], {})
    assert res["n_passing"] == 0
    assert res["stop_reason"] == "bestofn_nopass"
    assert res["edited"] is False
    assert applied == ""


# --- flake guard: a pass must CONFIRM on a second grade before it can win ---
# (earned by sympy-13091: a hash-randomization-dependent RecursionError made the same patch
# grade PASS/FAIL nondeterministically; selection early-stopped on the lucky pass and the
# official grader refuted it)

def test_flaky_pass_is_not_selected_and_sampling_continues(monkeypatch):
    # A passes its 1st grade but fails the confirmation -> flaky, NOT a winner; B confirms 2/2.
    res, applied = _run(monkeypatch, ["A", "B", "C"], {"A": [True, False], "B": True})
    assert res["winner_index"] == 1
    assert res["n_passing"] == 1                      # only CONFIRMED passes count
    assert res["candidates"][0]["flaky"] is True
    assert res["candidates"][0]["incontainer_pass"] is False
    assert res["stop_reason"] == "bestofn_pass"
    assert applied == "B"


def test_confirmed_pass_early_stops_after_two_grades(monkeypatch):
    res, applied = _run(monkeypatch, ["A", "B", "C"], {"A": True})
    assert res["n_candidates"] == 1                   # stable pass -> confirmed -> early-stop
    assert res["candidates"][0]["flaky"] is False
    assert res["candidates"][0]["incontainer_pass"] is True
    assert applied == "A"


def test_fallback_prefers_flaky_over_largest_when_nothing_confirms(monkeypatch):
    # A is small but passed once (flaky) — strictly more evidence than the never-passing big patch.
    res, applied = _run(monkeypatch, ["A", "BIGGER_PATCH", "C"], {"A": [True, False]})
    assert res["n_passing"] == 0
    assert res["stop_reason"] == "bestofn_nopass"
    assert res["winner_index"] == 0
    assert applied == "A"
