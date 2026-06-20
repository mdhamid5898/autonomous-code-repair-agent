"""ci_gate.count_resolved — no I/O beyond a tmp file, no Docker/API."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ci_gate  # noqa: E402


def _write(tmp_path, rows):
    p = tmp_path / "report.json"
    p.write_text(json.dumps(rows))
    return p


def test_counts_resolved_field(tmp_path):
    p = _write(tmp_path, [{"id": "a", "resolved": True}, {"id": "b", "resolved": False}])
    assert ci_gate.count_resolved(p) == (1, 2)


def test_counts_official_resolved_field(tmp_path):
    # SWE-bench rows carry official_resolved (resolved may mirror it)
    p = _write(tmp_path, [{"instance_id": "a", "official_resolved": True, "resolved": True},
                          {"instance_id": "b", "official_resolved": None, "resolved": False},
                          {"instance_id": "c", "official_resolved": True, "resolved": True}])
    assert ci_gate.count_resolved(p) == (2, 3)


def test_empty_report(tmp_path):
    p = _write(tmp_path, [])
    assert ci_gate.count_resolved(p) == (0, 0)
