#!/usr/bin/env python3
"""
Mechanic — eval setup & repro-verification harness.

For each issue in eval/issues.yaml this script:
  1. CLONE   clones the repo into eval/.repos/<id> (reused if present) and
             records the current HEAD SHA -> pins base_commit.
  2. INSTALL creates an isolated venv and runs the issue's install command,
             then guarantees pytest is available.
  3. BASELINE runs the repo's existing suite just to confirm it *collects and
             runs* at base_commit (informational; not a hard gate).
  4. REPRO   if eval/repros/<id>.py exists, copies it into the repo as
             test_mechanic_<id>.py and runs ONLY that file, EXPECTING it to
             FAIL (a present bug => red test). This is what flips
             repro_verified to true — the single most important check.

Outputs:
  - eval/setup_report.json   full machine record (SHAs, step results, timings)
  - backfills base_commit into eval/issues.yaml IF ruamel.yaml is installed
    (comments preserved); otherwise the SHAs live only in the report.

Usage:
  python3 eval/verify.py                 # all 15 issues
  python3 eval/verify.py --only furl-163 # one issue
  python3 eval/verify.py --no-install    # just clone + pin SHAs
  python3 eval/verify.py --fresh         # delete cached clones first
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
_DEFAULT_MANIFEST = EVAL_DIR / "issues.yaml"
REPOS_DIR = EVAL_DIR / ".repos"
REPROS_DIR = EVAL_DIR / "repros"
REPORT = EVAL_DIR / "setup_report.json"

# Resolved at parse time from --manifest; set in main() before any step runs.
MANIFEST: Path = _DEFAULT_MANIFEST

INSTALL_TIMEOUT = 600
SUITE_TIMEOUT = 300
REPRO_TIMEOUT = 120
CLONE_TIMEOUT = 300


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def which(name: str) -> str | None:
    return shutil.which(name)


def run(cmd, cwd=None, env=None, timeout=120, shell=False):
    """Run a command, capture output, never raise on non-zero exit."""
    try:
        p = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            shell=shell,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",  # tool/test output isn't always valid UTF-8 (e.g. sphinx emits 0xb2);
            timeout=timeout,   # decode-replace instead of crashing the whole run with UnicodeDecodeError
        )
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        return 124, e.stdout or "", f"TIMEOUT after {timeout}s"
    except FileNotFoundError as e:
        return 127, "", str(e)


def tail(text: str, n: int = 25) -> str:
    lines = (text or "").strip().splitlines()
    return "\n".join(lines[-n:])


def load_manifest(manifest_path: Path | None = None):
    """Returns (data, doc_or_None, ruamel_yaml_or_None). doc is the round-trip
    object used to write base_commit back with comments preserved."""
    path = manifest_path or MANIFEST
    try:
        from ruamel.yaml import YAML

        yaml = YAML()
        yaml.preserve_quotes = True
        with path.open() as f:
            doc = yaml.load(f)
        return doc, doc, yaml
    except ImportError:
        try:
            import yaml as pyyaml
        except ImportError:
            sys.exit(
                "Need a YAML reader. Install one:\n"
                "  pip install ruamel.yaml   (preferred — lets the script pin base_commit back into issues.yaml)\n"
                "  pip install pyyaml        (read-only; SHAs will live only in setup_report.json)"
            )
        with path.open() as f:
            data = pyyaml.safe_load(f)
        return data, None, None


def venv_paths(repo_dir: Path):
    venv = repo_dir / ".venv"
    bindir = venv / ("Scripts" if os.name == "nt" else "bin")
    return venv, bindir


def venv_env(bindir: Path) -> dict:
    env = os.environ.copy()
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    env["VIRTUAL_ENV"] = str(bindir.parent)
    env.pop("PYTHONHOME", None)
    return env


# --------------------------------------------------------------------------- #
# steps
# --------------------------------------------------------------------------- #
def step_clone(issue, fresh: bool) -> dict:
    iid = issue["id"]
    repo_dir = REPOS_DIR / iid
    if fresh and repo_dir.exists():
        shutil.rmtree(repo_dir)

    if not repo_dir.exists():
        REPOS_DIR.mkdir(parents=True, exist_ok=True)
        code, out, err = run(
            ["git", "clone", "--depth", "1", issue["clone"], str(repo_dir)],
            timeout=CLONE_TIMEOUT,
        )
        if code != 0:
            return {"ok": False, "sha": None, "log": tail(err or out)}

    # If a base_commit is pinned and differs from current HEAD, check it out so
    # the eval always runs against the frozen commit — even if upstream drifted.
    # (No-op when base_commit == HEAD, which is the case for HEAD-pinned issues,
    # so this never re-fetches in the common path.)
    base = issue.get("base_commit")
    cur = run(["git", "rev-parse", "HEAD"], cwd=repo_dir)[1].strip()
    if base and base != cur:
        shallow = run(["git", "rev-parse", "--is-shallow-repository"], cwd=repo_dir)[1].strip() == "true"
        if shallow:
            run(["git", "fetch", "--unshallow", "--quiet"], cwd=repo_dir, timeout=CLONE_TIMEOUT)
        code, _, err = run(["git", "checkout", "--quiet", base], cwd=repo_dir)
        if code != 0:
            return {"ok": False, "sha": cur, "log": f"could not checkout base_commit {base}: {tail(err)}"}

    code, out, err = run(["git", "rev-parse", "HEAD"], cwd=repo_dir)
    sha = out.strip() if code == 0 else None
    return {"ok": sha is not None, "sha": sha, "log": "" if sha else tail(err)}


def step_install(issue, repo_dir: Path) -> dict:
    venv, bindir = venv_paths(repo_dir)
    use_uv = which("uv") is not None

    # recreate venv fresh. NOTE: `uv venv` makes a venv WITHOUT pip, so the
    # manifest's `pip install ...` would fall through to system pip. --seed
    # installs pip into the venv so manifest commands target it correctly.
    if venv.exists():
        shutil.rmtree(venv)
    if use_uv:
        code, out, err = run(["uv", "venv", "--seed", str(venv)], cwd=repo_dir, timeout=120)
    else:
        code, out, err = run([sys.executable, "-m", "venv", str(venv)], cwd=repo_dir, timeout=120)
    if code != 0:
        return {"ok": False, "stage": "venv", "log": tail(err or out)}

    env = venv_env(bindir)
    py = str(bindir / ("python.exe" if os.name == "nt" else "python"))
    install_cmd = issue.get("install", "pip install -e .")

    # run the manifest install command with the venv on PATH (pip now resolves
    # to the venv's pip thanks to --seed / stdlib venv).
    code, out, err = run(install_cmd, cwd=repo_dir, env=env, timeout=INSTALL_TIMEOUT, shell=True)
    if code != 0:
        return {"ok": False, "stage": "install", "cmd": install_cmd, "log": tail(err or out)}

    # guarantee pytest in the venv, targeting the venv interpreter explicitly
    run([py, "-m", "pip", "install", "pytest"], cwd=repo_dir, env=env, timeout=180)

    return {"ok": True, "stage": "done", "cmd": install_cmd, "uv": use_uv, "log": tail(out)}


def _pytest_summary(out: str, err: str) -> str:
    blob = (out or "") + "\n" + (err or "")
    for line in reversed(blob.splitlines()):
        if "passed" in line or "failed" in line or "error" in line:
            return line.strip(" =")
    return tail(blob, 3)


def step_baseline(issue, repo_dir: Path) -> dict:
    _, bindir = venv_paths(repo_dir)
    env = venv_env(bindir)
    test_cmd = issue.get("test_cmd", "pytest -q")
    code, out, err = run(test_cmd, cwd=repo_dir, env=env, timeout=SUITE_TIMEOUT, shell=True)
    # baseline is informational: exit 0 (green) or 1 (some reds) both mean the
    # suite RAN. exit 2/3/4/5 means collection/config broke -> worth flagging.
    ran = code in (0, 1)
    return {"ran": ran, "exit": code, "summary": _pytest_summary(out, err), "log": tail(err or out)}


def step_repro(issue, repo_dir: Path) -> dict:
    iid = issue["id"]
    repro_src = REPROS_DIR / f"{iid}.py"
    if not repro_src.exists():
        return {"status": "MISSING", "verified": False,
                "note": f"author {repro_src.relative_to(EVAL_DIR.parent)} from the issue body"}

    dst = repo_dir / f"test_mechanic_{iid.replace('-', '_')}.py"
    shutil.copyfile(repro_src, dst)

    _, bindir = venv_paths(repo_dir)
    env = venv_env(bindir)
    # Neutralize the repo's own addopts (-o addopts=). Our repro is a single
    # dropped-in test; we don't want the project's --cov / --doctest-modules /
    # lint plugins (pytest-isort, -pydocstyle, ...) applied to it — those force
    # plugin deps that aren't in the venv and abort collection (e.g. dictdiffer).
    code, out, err = run(
        ["python", "-m", "pytest", dst.name, "-q", "-rN", "--no-header",
         "-p", "no:cacheprovider", "-o", "addopts="],
        cwd=repo_dir, env=env, timeout=REPRO_TIMEOUT,
    )
    summary = _pytest_summary(out, err)
    # 1 => tests ran and failed => bug REPRODUCED (what we want)
    # 0 => tests passed => bug already absent on base_commit (DANGER)
    # else => collection/import error => repro test itself is broken
    if code == 1:
        return {"status": "REPRODUCED", "verified": True, "exit": code, "summary": summary}
    if code == 0:
        return {"status": "BUG_ABSENT", "verified": False, "exit": code, "summary": summary,
                "note": "test passes on base_commit — bug not present; drop or re-pin this issue"}
    return {"status": "REPRO_ERROR", "verified": False, "exit": code, "summary": summary,
            "log": tail(err or out)}


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
def verdict_for(rec: dict) -> str:
    if not rec["clone"]["ok"]:
        return "CLONE_FAIL"
    if rec.get("install") and not rec["install"]["ok"]:
        return "INSTALL_FAIL"
    repro = rec.get("repro") or {}
    if repro.get("verified"):
        return "READY"
    if repro.get("status") == "MISSING":
        return "NEEDS_REPRO"
    if repro.get("status") == "BUG_ABSENT":
        return "BUG_ABSENT"
    if repro.get("status") == "REPRO_ERROR":
        return "REPRO_ERROR"
    return "PINNED"  # cloned+installed, repro skipped


def process(issue, args) -> dict:
    iid = issue["id"]
    repo_dir = REPOS_DIR / iid
    rec = {"id": iid, "repo": issue["repo"], "issue": issue["issue"]}
    t0 = time.time()

    # fast path: reuse an existing clone + venv, run only the repro step
    if args.repro_only:
        sha = None
        if repo_dir.exists():
            code, out, _ = run(["git", "rev-parse", "HEAD"], cwd=repo_dir)
            sha = out.strip() if code == 0 else None
        rec["clone"] = {"ok": repo_dir.exists(), "sha": sha, "log": ""}
        venv, _ = venv_paths(repo_dir)
        if not repo_dir.exists():
            rec["verdict"] = "CLONE_FAIL"
        elif not venv.exists():
            rec["verdict"] = "NEEDS_INSTALL"
        else:
            rec["repro"] = step_repro(issue, repo_dir)
            rec["verdict"] = verdict_for(rec)
        rec["seconds"] = round(time.time() - t0, 1)
        return rec

    rec["clone"] = step_clone(issue, args.fresh)
    if not rec["clone"]["ok"]:
        rec["verdict"] = "CLONE_FAIL"
        rec["seconds"] = round(time.time() - t0, 1)
        return rec

    if not args.no_install:
        rec["install"] = step_install(issue, repo_dir)
        if rec["install"]["ok"]:
            rec["baseline"] = step_baseline(issue, repo_dir)
            if not args.no_repro:
                rec["repro"] = step_repro(issue, repo_dir)

    rec["verdict"] = verdict_for(rec)
    rec["seconds"] = round(time.time() - t0, 1)
    return rec


def backfill_pins(doc, yaml, results: dict, manifest_path: Path | None = None):
    """Write base_commit back into the manifest, comments preserved."""
    path = manifest_path or MANIFEST
    if doc is None or yaml is None:
        print("\n(ruamel.yaml not installed — base_commit NOT written back; SHAs are in setup_report.json)")
        return
    changed = 0
    for issue in doc["issues"]:
        r = results.get(issue["id"])
        if not r:
            continue
        if r["clone"]["ok"] and r["clone"]["sha"] and issue.get("base_commit") != r["clone"]["sha"]:
            issue["base_commit"] = r["clone"]["sha"]
            changed += 1
        # flip repro_verified only when the repro step actually ran this pass
        repro = r.get("repro")
        if repro is not None and "verified" in repro:
            v = bool(repro["verified"])
            if issue.get("repro_verified") != v:
                issue["repro_verified"] = v
                changed += 1
    if changed:
        with path.open("w") as f:
            yaml.dump(doc, f)
        print(f"\nUpdated {changed} field(s) in {path.name} (base_commit / repro_verified)")


def print_table(records: list[dict]):
    w_id, w_v = 22, 12
    print("\n" + "=" * 78)
    print(f"{'ISSUE':<{w_id}} {'SHA':<9} {'INSTALL':<8} {'BASE':<6} {'REPRO':<12} {'VERDICT':<{w_v}}")
    print("-" * 78)
    for r in records:
        sha = (r["clone"].get("sha") or "----")[:7]
        inst = "ok" if (r.get("install") or {}).get("ok") else ("--" if "install" not in r else "FAIL")
        base = "ok" if (r.get("baseline") or {}).get("ran") else ("--" if "baseline" not in r else "ERR")
        repro = (r.get("repro") or {}).get("status", "--")
        print(f"{r['id']:<{w_id}} {sha:<9} {inst:<8} {base:<6} {repro:<12} {r['verdict']:<{w_v}}")
    print("=" * 78)
    counts: dict[str, int] = {}
    for r in records:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    print("  " + "  ".join(f"{k}:{v}" for k, v in sorted(counts.items())))


def main():
    ap = argparse.ArgumentParser(description="Pin, install, and repro-verify the eval issue set.")
    ap.add_argument("--manifest", default=None,
                    help="path to the issues manifest YAML (default: eval/issues.yaml)")
    ap.add_argument("--only", help="run a single issue id (e.g. furl-163)")
    ap.add_argument("--no-install", action="store_true", help="clone + pin SHAs only")
    ap.add_argument("--no-repro", action="store_true", help="skip the repro-verification step")
    ap.add_argument("--repro-only", action="store_true",
                    help="reuse existing clone+venv, run only the repro step (fast self-check loop)")
    ap.add_argument("--fresh", action="store_true", help="delete cached clones before running")
    args = ap.parse_args()

    if not which("git"):
        sys.exit("git not found on PATH.")

    manifest_path = Path(args.manifest).resolve() if args.manifest else _DEFAULT_MANIFEST
    if not manifest_path.exists():
        sys.exit(f"Manifest not found: {manifest_path}")

    # Derive a per-manifest report file so v2 runs don't clobber the v1 report.
    report_path = EVAL_DIR / f"setup_report_{manifest_path.stem}.json"

    doc, rt_doc, yaml = load_manifest(manifest_path)
    issues = doc["issues"]
    if args.only:
        issues = [i for i in issues if i["id"] == args.only]
        if not issues:
            sys.exit(f"No issue with id '{args.only}'.")

    print(f"Manifest: {manifest_path.relative_to(EVAL_DIR.parent)}")
    print(f"Processing {len(issues)} issue(s). uv={'yes' if which('uv') else 'no'}  "
          f"clones -> {REPOS_DIR.relative_to(EVAL_DIR.parent)}/")

    records, results = [], {}
    for issue in issues:
        print(f"\n→ {issue['id']} ({issue['repo']}#{issue['issue']}) ...", flush=True)
        rec = process(issue, args)
        records.append(rec)
        results[issue["id"]] = rec
        print(f"  {rec['verdict']}  ({rec['seconds']}s)")

    backfill_pins(rt_doc, yaml, results, manifest_path)

    report_path.write_text(json.dumps(records, indent=2))
    print(f"\nFull report -> {report_path.relative_to(EVAL_DIR.parent)}")
    print_table(records)


if __name__ == "__main__":
    main()
