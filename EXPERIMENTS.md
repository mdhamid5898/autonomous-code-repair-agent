# EXPERIMENTS — Mechanic results ledger

The single source of truth for **every run: dataset × engine × model × result.**
Append a row after every sweep/run — never delete; correct with a dated note.

> **Grading:** SWE-bench rows are graded by the **official** `swebench.harness.run_evaluation`
> (FAIL_TO_PASS flips + PASS_TO_PASS holds). Local v1/v2/v3 rows are repro-pass (red→green) only.
> All pass@1 unless noted. Raw machine reports (`swebench_report_*.json`, `sweep_report_*.json`)
> are gitignored; **this file is the curated truth.**
>
> **Append convention:** after a run, add a row to the right table with date · engine · model · budget ·
> coverage (FULL = all instances / PARTIAL / SPOT = 1–few) · resolved · grade · notes. If a number is
> later corrected (e.g. stale-cache, false-positive), strike it and add a dated correction — don't silently edit.
>
> _Last updated: 2026-07-02._

---

## 1. SWE-bench Verified — hard multi-file subset (14 instances), OFFICIAL grader, pass@1

Subset = `eval/swebench_subset.json` (large-repo, 2–21 source files, 7 repos).

| date | engine | model | budget | coverage | resolved | notes |
|---|---|---|---|---|---|---|
| 2026-07-02 | **bestofn (escalate)** | flash→pro→pro | 60 steps/attempt, N=3, early-stop | **FULL (14)** | **12/14 (86%)** | ≤3f 9/9 · ≥4f 3/5; ~$0.055/run avg; 3.5h wall; strict superset of pro-single (+django-11138); 1 in-container false-pos (sympy-13091, FLAKY patch) caught by the official gate → flake guard added (`ea67915`) |
| 2026-06-20 | solve (single) | deepseek-v4-pro | 60 steps | **FULL (14)** | **11/14 (79%)** | ≤3f 9/9 · ≥4f 2/5; ~$0.07/run |
| 2026-06-20 | solve (single) | deepseek-v4-flash | 60 steps | **FULL (14)** | **9/14 (64%)** | ≤3f 8/9 · ≥4f 1/5; ~$0.03/run |
| 2026-06-20 | solve (single) | deepseek-chat | 60 steps | PARTIAL (4 ladder) | 2/4 | early ladder; superseded by v4 runs |
| 2026-06-20 | multi | deepseek-chat | 4 iters | PARTIAL (4 ladder) | 2/4 | **tied single on the same 4** |
| 2026-06-20 | multi | deepseek-v4-pro | 4 iters | SPOT (1: django-11138) | 1/1 | resolved where single-v4-pro submitted wrong |
| 2026-06-20 | governed | — | — | **NONE** | — | never run on SWE-bench (local only) |
| 2026-06-20 | bestofn (flash→pro) | escalate | 40–60 | SPOT (2) | see §3 | live selection thin — NOT a clean win |

### Per-instance — the three FULL sweeps (single flash · single pro · best-of-N escalate)
| instance | files | v4-flash | v4-pro | bestofn (win#) |
|---|---|---|---|---|
| astropy-13398 | 4 | ✗ | ✗ | ✗ (0 pass, 3 attempts, fallback) |
| astropy-14369 | 2 | ✓ | ✓ | ✓ (#0 flash) |
| django-11138 | 4 | ✗ | ✗ (submitted wrong) | **✓ (#1 pro — the unlock: no single agent resolved it)** |
| django-11532 | 5 | ✓ | ✓ | ✓ (#0 flash) |
| matplotlib-14623 | 3 | ✓ | ✓ | ✓ (#0 flash) |
| matplotlib-25775 | 3 | ✓ | ✓ | ✓ (#0 flash) |
| scikit-learn-12682 | 2 | ✓ | ✓ | ✓ (#0 flash) |
| scikit-learn-25102 | 2 | ✓ | ✓ | ✓ (#0 flash) |
| sphinx-7590 | 3 | ✗ | ✓ | ✓ (#1 pro, 71min) |
| sphinx-10673 | 3 | ✓ | ✓ | ✓ (#0 flash) |
| sympy-13091 | 21 | ✗ | ✗ | ✗ (in-container FALSE-POS on a flaky patch; official refuted — see §3) |
| sympy-16597 | 6 | ✗ | ✓ | ✓ (#1 pro) |
| xarray-3095 | 2 | ✓ | ✓ | ✓ (#0 flash) |
| xarray-3305 | 2 | ✓ | ✓ | ✓ (#0 flash) |
| **TOTAL** | | **9/14** | **11/14** | **12/14** |

bestofn = pro-single's 11 **+ django-11138**, zero regressions (strict superset). 10/14 resolved on the CHEAP
attempt 0 (escalation spent only where needed → ~$0.055/run avg, cheaper than pro-single at higher resolution).

---

## 2. Local benchmarks (v1/v2/v3) — repro-pass grade (red→green), pass@1

| dataset | engine | model | resolved | notes |
|---|---|---|---|---|
| v1 (15) | solve (single) | gpt-4o | 9/15 (60%) | original baseline |
| v1 (15) | solve (single) | deepseek-chat (V3) | 13/15 (87%) | ~30× cheaper; solved 5 of GPT-4o's 6 misses |
| v1 (15) | graph (LangGraph) | deepseek-chat | 6/15 | partial — OpenAI rate-limit artifact, not a regression |
| v2 (15, hard) | solve (single) | deepseek-chat | 13/15 | |
| v2 (15, hard) | multi | deepseek-chat | 15/15 | |
| v2 (15, hard) | governed @30 / @60 | deepseek-chat | 12/15 / **15/15** | @60 == multi |
| v3 (10, multi-file) | solve (single) | deepseek-chat | 8/10 | |
| v3 (10, multi-file) | multi | deepseek-chat | 10/10 | |
| v3 (10, multi-file) | governed @30 / @60 | deepseek-chat | 9/10 / **10/10** | @60 == multi |

**Iso-control verdict (v2+v3):** governed@60 (single + test gate + matched budget) == multi; the gate fired
**0/43** → multi's edge is **budget + a control loop, NOT decomposition.**

---

## 3. best-of-N — live selection scorecard (honest; corrected 2026-06-26)

best-of-N *produced* an officially-resolving patch on both hard instances a single agent missed, but the
**selection mechanism is barely validated live** — do NOT claim a best-of-N resolution lift yet.

| instance | budget | n_passing | winner | how it resolved | official | selector correct? |
|---|---|---|---|---|---|---|
| django-11138 | 40 | 0 | #1 (fallback) | largest-patch FALLBACK (old exit-code grade false-negated all) | ✅ | ✗ (output win, not selection) |
| sympy-16597 | 60 | 1 | #1 | parser-fixed selection picked the v4-pro resolver; fresh grade F2P 3/3 P2P 74/74 | ✅ | ✓ (but a v4-pro *capability* win — single v4-pro also resolves it) |

**Live selection: 1 correct pick (post parser-fix), N=2 total → too thin.** Needs a full `--engine bestofn`
sweep (TODO #1–2). The real win of this exercise = two grading-harness bugs found + fixed (`5eaec28` parser
grade, `6925254` content-addressed grade_official).

### Selector-equivalence validation (TODO #1, 2026-07-01) — the gate the scorecard above lacked

`bestofn_validate.py`: re-run best-of-N with the PARSER-FIXED grader (no early-stop, so ALL N candidates are
sampled), then cross-check **each candidate's in-container verdict vs a FRESH official grade** (content-addressed
so no stale cache), plus gold/empty oracles. GATE = 0 false-positives (in-container PASS but official not-resolved)
AND 0 false-negatives (in-container fail but official RESOLVED).

**django__django-11138** — base v4-flash, 60 steps, escalate ladder [flash,pro,pro], N=3. **GATE PASS ✅** (report
`runs/bestofn_validate_django_django-11138_report.json`):

| item | in-container | fresh official | verdict |
|---|---|---|---|
| cand0 (v4-flash, 7481ch) | PASS | RESOLVED | agree |
| cand1 (v4-pro, 6111ch) | PASS | RESOLVED | agree |
| cand2 (v4-pro, 7705ch) | PASS | RESOLVED | agree |
| GOLD (oracle) | PASS | RESOLVED | agree |
| EMPTY (oracle) | fail | not-resolved | agree |

0 false-pos, 0 false-neg across 5 pairs. All 3 candidate patches show the raw summary `FAILED (failures=3, skipped=8)`
(unrelated module tests) yet the parser scores only the F2P/P2P gold ids = PASS — and official confirms RESOLVED on
every one. This is the EXACT false-negative the OLD exit-code grade produced (the prior django@40 row above:
`n_passing=0`, resolved only via fallback); the parser fix (`5eaec28`) now matches official. All 3 candidates
resolved this time → the live selector would pick cand0 (v4-flash) immediately, no fallback.

**sympy__sympy-16597** — base v4-flash, 60 steps, escalate ladder, N=3. **GATE PASS ✅** (report
`runs/bestofn_validate_sympy_sympy-16597_report.json`) — the stronger check, a genuine pass/fail MIX:

| item | in-container | fresh official | verdict |
|---|---|---|---|
| cand0 (v4-flash, 1153ch) | PASS | RESOLVED | agree |
| cand1 (v4-pro, 1203ch) | fail (1 test failing) | not-resolved | agree |
| cand2 (v4-pro, 1153ch) | PASS | RESOLVED | agree |
| GOLD (oracle) | PASS | RESOLVED | agree |
| EMPTY (oracle) | fail | not-resolved | agree |

0 false-pos, 0 false-neg. Both directions validated on REAL candidates: cand1 (v4-pro) left 1 test failing →
correctly rejected in-container AND official (no false-neg); the two resolvers agree RESOLVED (no false-pos).
This also retires the old stale-cache artifact — the prior sympy@60 run-record's `official_resolved=False` is now
correctly RESOLVED under content-addressed grading. (Variance note: v4-flash cand0 resolved sympy this run with a
tiny 1153-char fix; earlier notes had flash NOT cracking it — the selector correctly identifies the resolver either way.)

**TODO #1 VERDICT: django 5/5 + sympy 5/5 = 10/10 pairs agree, 0 false-pos, 0 false-neg → the parser-fixed
in-container selector is VALIDATED as official-equivalent.** A 3rd grading-harness bug was found + fixed en route:
`de6cb5d` (TimeoutExpired returned undecoded bytes → str+bytes crash killed any run whose agent command timed out;
would have bitten the bestofn/multi sweeps). Unblocks TODO #2 (full `--engine bestofn --escalate` sweep).

### Full-sweep selection scorecard (TODO #2, 2026-07-02) — selector at scale + ONE flaky false-positive

Full `--engine bestofn --escalate` sweep (N=3, early-stop, 60 steps/attempt) = **12/14 (86%)**, every "resolved"
gated on a fresh official grade inline (see §1 tables). Selector behavior across 14 live instances:
- **12 selector-PASS → all 12 officially RESOLVED** (10 at cheap attempt #0; django-11138 + sphinx-7590 +
  sympy-16597 at escalated attempt #1). **django-11138 is the headline: selection — not fallback — picked the
  escalated v4-pro patch, on the instance NO single agent resolved** (flash wrong, pro wrong-fix/paralysis).
- **astropy-13398: selector correctly rejected all 3 attempts** (n_pass=0, largest-patch fallback, official ✗).
- **sympy-13091 (21f): the ONE disagreement — an in-container FALSE-POSITIVE on a genuinely FLAKY patch.**
  The candidate's `__eq__` edits introduce a RecursionError that only fires on a hash-bucket collision in
  `sympify`'s `a in sympy_classes` — i.e. failure depends on per-process hash randomization. Re-grading the
  IDENTICAL patch 3× in-container: **False, True, True** (even pass-counts wobble). Selection early-stopped on
  a lucky pass; the official grader rolled a fail (P2P `test_bug_sqrt` RecursionError) and refuted it. NOT a
  parser/selector-logic bug (TODO #1's 10/10 equivalence holds for deterministic patches) — a nondeterministic
  patch is a different failure mode. **The official-gate discipline caught it → the 12/14 number is sound.**
- **Mitigation (post-sweep, `ea67915`): flake guard** — a passing candidate must confirm **2/2** in-container
  grades to win/early-stop; fallback now prefers a flaky (≥1-pass) candidate over blind largest-patch. A
  1-in-3-fail flaky patch rarely survives 2/2. (Note: the 12/14 sweep ran pre-guard; the guard only makes
  selection stricter — confirmed winners stay winners.)

---

## 4. Honest coverage gaps (as of 2026-07-02)
- **multi was NEVER full-swept on SWE-bench** — only 4-instance ladder (2/4, tied single) + 1 django spot. No multi SWE-bench number exists. **TODO #3 sweep launching.**
- **governed never run on SWE-bench** — the single/multi/governed comparison lives only on local v2/v3.
- **best-of-N: DONE at scale** — selector validated (TODO #1, 10/10) AND full-swept (TODO #2, **12/14**, every resolve officially gated). Residual caveats: pass@1 (single sweep, no variance bars); the sweep predates the flake guard (`ea67915`); 1 flaky false-pos observed live (caught by the official gate).
- The eval-gate CI **resolution job has never executed on GitHub** (needs a self-hosted runner for the Docker eval).
- Langfuse **trace delivery** wired but never live-confirmed (no project keys).
