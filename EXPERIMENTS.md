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
> _Last updated: 2026-06-26._

---

## 1. SWE-bench Verified — hard multi-file subset (14 instances), OFFICIAL grader, pass@1

Subset = `eval/swebench_subset.json` (large-repo, 2–21 source files, 7 repos).

| date | engine | model | budget | coverage | resolved | notes |
|---|---|---|---|---|---|---|
| 2026-06-20 | solve (single) | deepseek-v4-pro | 60 steps | **FULL (14)** | **11/14 (79%)** | ≤3f 9/9 · ≥4f 2/5; ~$0.07/run |
| 2026-06-20 | solve (single) | deepseek-v4-flash | 60 steps | **FULL (14)** | **9/14 (64%)** | ≤3f 8/9 · ≥4f 1/5; ~$0.03/run |
| 2026-06-20 | solve (single) | deepseek-chat | 60 steps | PARTIAL (4 ladder) | 2/4 | early ladder; superseded by v4 runs |
| 2026-06-20 | multi | deepseek-chat | 4 iters | PARTIAL (4 ladder) | 2/4 | **tied single on the same 4** |
| 2026-06-20 | multi | deepseek-v4-pro | 4 iters | SPOT (1: django-11138) | 1/1 | resolved where single-v4-pro submitted wrong |
| 2026-06-20 | governed | — | — | **NONE** | — | never run on SWE-bench (local only) |
| 2026-06-20 | bestofn (flash→pro) | escalate | 40–60 | SPOT (2) | see §3 | live selection thin — NOT a clean win |

### Per-instance — the two FULL single sweeps
| instance | files | v4-flash | v4-pro |
|---|---|---|---|
| astropy-13398 | 4 | ✗ | ✗ |
| astropy-14369 | 2 | ✓ | ✓ |
| django-11138 | 4 | ✗ | ✗ (submitted wrong) |
| django-11532 | 5 | ✓ | ✓ |
| matplotlib-14623 | 3 | ✓ | ✓ |
| matplotlib-25775 | 3 | ✓ | ✓ |
| scikit-learn-12682 | 2 | ✓ | ✓ |
| scikit-learn-25102 | 2 | ✓ | ✓ |
| sphinx-7590 | 3 | ✗ | ✓ |
| sphinx-10673 | 3 | ✓ | ✓ |
| sympy-13091 | 21 | ✗ | ✗ |
| sympy-16597 | 6 | ✗ | ✓ |
| xarray-3095 | 2 | ✓ | ✓ |
| xarray-3305 | 2 | ✓ | ✓ |
| **TOTAL** | | **9/14** | **11/14** |

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

---

## 4. Honest coverage gaps (as of 2026-06-26)
- **multi was NEVER full-swept on SWE-bench** — only 4-instance ladder (2/4, tied single) + 1 django spot. No multi SWE-bench number exists.
- **governed never run on SWE-bench** — the single/multi/governed comparison lives only on local v2/v3.
- **best-of-N**: live selection validated N=2 only; no full sweep.
- The eval-gate CI **resolution job has never executed on GitHub** (needs a self-hosted runner for the Docker eval).
- Langfuse **trace delivery** wired but never live-confirmed (no project keys).
