# eval/issues_v2 — Candidate Research Log

All 15 issues selected for v2 were independently confirmed "red on base commit"
before being added to the manifest.  Every rejected candidate is logged below
with a one-line reason.

---

## VERIFIED (kept — 15 total)

| # | ID | Repo | Tier | Why kept |
|---|---|---|---|---|
| 1 | deepdiff-414 | qlustered/deepdiff | hard | iterable_compare_func drops inner diffs for moved items; confirmed empty diff at tag 7.0.1 |
| 2 | typeguard-489 | agronholm/typeguard | medium | @typechecked raises NameError with Literal + `from typing import *`; confirmed at HEAD |
| 3 | boltons-348 | mahmoud/boltons | medium | LRU.values() returns stale entry after update; confirmed at commit just before fix #349 |
| 4 | arrow-1240 | arrow-py/arrow | medium | humanize() says "a month" for 16-day delta; confirmed at HEAD |
| 5 | arrow-1185 | arrow-py/arrow | hard | span_range(exact=True) skips 2-3 days when day-of-month clamps across months; confirmed at HEAD |
| 6 | voluptuous-529 | alecthomas/voluptuous | hard | REMOVE_EXTRA + Required(Any("@id","id")) raises MultipleInvalid for extra keys; confirmed at tag 0.15.2 |
| 7 | dateutil-1398 | dateutil/dateutil | hard | rrulestr BYSETPOS picks wrong day when BYDAY is in non-calendar order + WKST set; confirmed at HEAD |
| 8 | humanize-174 | python-humanize/humanize | medium | naturaldelta rounds down: 90 s → "a minute" not "2 minutes"; confirmed at commit before PR #272 |
| 9 | parse-223 | r1chardj0n3s/parse | medium | float(".501902") * 1e6 loses precision → microsecond 501901 ≠ 501902; confirmed at tag 1.21.0 |
| 10 | inflect-222 | jaraco/inflect | medium | singular_noun("pair of scissors") raises TypeError; None-guard missing on regex match; confirmed at HEAD |
| 11 | yarl-984 | aio-libs/yarl | medium | URL / "path" / "" drops trailing slash; confirmed at tag v1.9.4 |
| 12 | boltons-393 | mahmoud/boltons | hard | research() skips branches with CPython-interned identical tuple objects; confirmed at tag 24.1.0 |
| 13 | marshmallow-2170 | marshmallow-code/marshmallow | medium | schema validator error uses attr name not data_key; confirmed at commit before PR #2792 |
| 14 | jmespath-318 | jmespath/jmespath.py | medium | literal `[]`/`{}` AST node shared across calls; mutation in one call corrupts next; confirmed at HEAD |
| 15 | packaging-788 | pypa/packaging | hard | Specifier("<3.0.0a8").contains("3.0.0a7") → False (should be True); confirmed at tag 24.1 |

---

## REJECTED

### REJECTED-already-patched

| Candidate | Reason |
|---|---|
| deepdiff-410 | Listed in v1 "Dropped" section; already patched at HEAD — BUG_ABSENT |
| astor-225 | Listed in v1 "Dropped" section; already patched at HEAD — BUG_ABSENT |
| treelib-218 | Listed in v1 "Dropped" section; already patched at HEAD — BUG_ABSENT |
| bidict `__eq__` ANY | Investigated bidict v0.22.1 for `__eq__(ANY)` returning False; already returns NotImplemented correctly at that tag — BUG_ABSENT |

### REJECTED-no-clean-repro

| Candidate | Reason |
|---|---|
| multidict-1113 (CIMultiDict mutation) | Mutation confirmed but subsequent `dict(ci)` raised KeyError; repro not self-contained without internal multidict test helpers |
| multidict CIMultiDict case-insensitivity | Test at v6.3.0 showed correct case-insensitive lookup; could not isolate the bug |
| wcwidth VS-16 emoji width | GitHub tags (e.g. 0.8.1) differ from PyPI versions (0.2.13); could not pin a consistent pre-fix commit |
| bidict `__eq__` ANY (re-test) | v0.22.1 `bidict().__eq__(ANY)` returns NotImplemented and `bidict() == ANY` returns True; bug was not present |

### REJECTED-install-complexity

| Candidate | Reason |
|---|---|
| pendulum | pyproject.toml requires Rust/C extensions via maturin; build failed in isolated venv |
| sortedcontainers-235 | irange() with neg key is documented "works as expected" by maintainers; not a fixable code bug |

### REJECTED-wrong-difficulty (too easy, duplicate, or not a code-logic bug)

| Candidate | Reason |
|---|---|
| humanize naturaldelta (jmoiron/humanize) | Repo moved to python-humanize/humanize; old repo is archived — used new org |
| humanize precisedelta #220 | "Limitation not a bug" per maintainers; timedelta has no month information |
| parse-221 | Wrong issue number — #221 is a feature request (grouping char); correct issue is #223 |
| requests network issues | All candidates require live HTTP server; not deterministic in isolation |
| natsort stable sort | Non-determinism is by design; PRESORT flag was the intended API for this case |

---

## Notes on approach

1. **Red-on-base discipline**: every candidate was tested with a standalone Python
   script in an isolated venv at the exact pinned commit.  "Open" GitHub issues were
   treated as suspects, not facts — several were already fixed at HEAD.

2. **Tag vs. commit SHA**: git annotated tags return the tag-object SHA from
   `git rev-parse`; the actual commit SHA was obtained with `tag^{commit}` and
   used in base_commit.

3. **Diversity targets met**: 13 distinct libraries, 6 bug types (wrong-value,
   exception, wrong-exception, wrong-order, precision, API-contract).
   Tiers: 5 hard, 10 medium.

4. **yarl install**: `YARL_NO_EXTENSIONS=1 pip install -e .` used to skip Cython
   extension build; the bug is in pure-Python URL joining logic.
