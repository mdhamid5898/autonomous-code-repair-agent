# Repro tests — the ground-truth contract

Each file here is the **failing test that defines "resolved"** for one issue.
`verify.py` copies `repros/<id>.py` into the cloned repo as
`test_mechanic_<id>.py` and runs it. The required outcome:

| On `base_commit` (bug present) | After the agent's patch (bug fixed) |
|--------------------------------|-------------------------------------|
| test **FAILS** (red)           | test **PASSES** (green)             |

That red→green flip is the only definition of resolution the eval uses.

## Rules

1. **File name = issue id**: `freezegun-348.py`, `furl-163.py`, … (matches `id:` in `issues.yaml`).
2. **Self-contained**: import only the target library + stdlib + pytest. No repo-internal test helpers (the file is dropped in cold).
3. **It must FAIL by assertion, not by error.** An `ImportError`/`SyntaxError`/collection error makes `verify.py` report `REPRO_ERROR`, not a verified repro. Import the real public API and assert on the wrong value.
4. **Encode the *expected* behavior**, i.e. what the fixed code should do. The assertion is what the agent has to make pass.
5. **Keep it tight** — one focused test function, derived verbatim from the issue body where possible.

## verify.py outcomes

- `REPRODUCED` → exit 1, assertion failed → ✅ verified (this is the goal)
- `BUG_ABSENT` → exit 0, test passed on base_commit → ⚠️ bug already fixed upstream; drop or re-pin the issue
- `REPRO_ERROR` → other exit → the test file is broken, fix it
- `MISSING` → no file here yet

## Example shape (illustrative — verify against the real issue before trusting)

```python
# furl-163.py  — netloc must split on the LAST '@', not the first
from furl import furl

def test_multiple_at_signs_use_last_at():
    f = furl("http://user:p@ss@example.com/path")
    assert f.host == "example.com"   # buggy split('@',1) yields 'ss@example.com'
```
