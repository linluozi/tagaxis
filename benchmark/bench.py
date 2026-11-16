#!/usr/bin/env python3
"""Reproducible benchmark driver for tagaxis.

Builds the deterministic oracle history, runs the full
trace -> detect -> bisect -> revert hunt, and writes every raw sample to
``benchmark/raw/`` plus a machine-readable summary to ``benchmark/evidence.json``.

Two modes::

    python3 benchmark/bench.py                 # build oracle in a temp dir and run
    python3 benchmark/bench.py --from-json X   # re-split an existing hunt JSON

Every number in the summary comes from samples stored in ``benchmark/raw/``.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import oracle_gen  # noqa: E402
from tagaxis import core  # noqa: E402


def _slug(text):
    keep = [c if (c.isalnum() or c in "-._") else "_" for c in text]
    return "".join(keep)[:120]


def write_raw(payload, raw_dir):
    raw_dir = Path(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    for old in raw_dir.glob("*.json"):
        old.unlink()
    written = []

    def emit(name, obj):
        path = raw_dir / (_slug(name) + ".json")
        path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")
        written.append(path.name)

    for i, run in enumerate(payload["aa"]["runs"]):
        emit("aa-rev%d-%s" % (i, payload["aa"]["rev"]), run)
    for m in payload["trajectory"]:
        emit("traj-%s" % m["label"], m)
    for f in payload["findings"]:
        key = "%s..%s" % (f["from_label"], f["to_label"])
        for m in f["bisect"]["measured"]:
            emit("bisectprobe-%s-%s" % (key, m["label"]), m)
        if f["revert"]:
            emit("revert-%s" % f["bisect"]["culprit"][:12], f["revert"]["reverted_measurement"])
    return sorted(written)


def summarize(payload, ground_truth):
    gt_by_interval = {"%s..%s" % (i["from_tag"], i["to_tag"]): i for i in ground_truth["intervals"]}
    findings = []
    for f in payload["findings"]:
        key = "%s..%s" % (f["from_label"], f["to_label"])
        truth = gt_by_interval[key]["regressions"]
        culprit = f["bisect"]["culprit"]
        findings.append({
            "interval": key,
            "ratio": f["ratio"],
            "from_median_ns": f["from_median_ns"],
            "to_median_ns": f["to_median_ns"],
            "culprit": culprit,
            "culprit_subject": f["bisect"]["culprit_subject"],
            "n_commits_in_range": f["bisect"]["n_commits"],
            "n_probes": f["bisect"]["n_probes"],
            "localized_exactly": truth == [culprit],
            "revert_rel_delta_vs_parent": f["revert"]["rel_delta_vs_parent"],
            "revert_within_tolerance": f["revert"]["within_tolerance"],
        })
    cleaned = sorted(payload["steps"][i]["from_label"] + ".." + payload["steps"][i]["to_label"]
                     for i in range(len(payload["steps"])))
    false_positives = [s for s in cleaned if s in ground_truth["clean_intervals"]]
    return {
        "oracle": {
            "commits": len(ground_truth["commits"]),
            "tags": len(ground_truth["tags"]),
            "known_regressions": len(ground_truth["regressions"]),
            "clean_intervals": ground_truth["clean_intervals"],
        },
        "aa_control_rel_dev": payload["aa"]["rel_dev"],
        "trajectory": [
            {"label": m["label"], "median_ns": m["median_ns"], "p25_ns": m["p25_ns"],
             "p75_ns": m["p75_ns"], "rsd_pct": m["rsd_pct"], "n": m["n"],
             "samples_ns": m["samples_ns"]}
            for m in payload["trajectory"]
        ],
        "findings": findings,
        "totals": {
            "steps_detected": len(payload["steps"]),
            "localized_exactly": sum(1 for f in findings if f["localized_exactly"]),
            "revert_within_tolerance": sum(1 for f in findings if f["revert_within_tolerance"]),
            "false_positive_intervals": len(false_positives),
            "max_probes_per_regression": max((f["n_probes"] for f in findings), default=0),
        },
        "meta": payload.get("meta", {}),
        "elapsed_s": payload.get("elapsed_s"),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workdir", default=None, help="where to build the oracle repo")
    ap.add_argument("--from-json", default=None, help="split an existing hunt JSON instead of running")
    ap.add_argument("--out-json", default=str(ROOT / "evidence" / "oracle_hunt.json"))
    ap.add_argument("--raw-dir", default=str(ROOT / "benchmark" / "raw"))
    ap.add_argument("--evidence", default=str(ROOT / "benchmark" / "evidence.json"))
    ap.add_argument("--samples", type=int, default=7)
    ap.add_argument("--warmup", type=int, default=3)
    args = ap.parse_args(argv)

    workdir = args.workdir or tempfile.mkdtemp(prefix="tagaxis-oracle-")
    repo, gt_path = oracle_gen.build(workdir)
    ground_truth = json.loads(Path(gt_path).read_text())

    if args.from_json:
        payload = json.loads(Path(args.from_json).read_text())
    else:
        payload = {
            "meta": {"repo": str(repo), "oracle_ground_truth": str(gt_path)},
            "repo": str(repo),
            "cmd": "%s bench/workload.py" % sys.executable,
            "samples": args.samples,
            "warmup": args.warmup,
        }
        payload.update(core.hunt(
            str(repo), [sys.executable, "bench/workload.py"],
            samples=args.samples, warmup=args.warmup,
            min_ratio=1.15, aa_rounds=2, revert_tol=0.05,
            progress=lambda m: print("[bench] %s" % m, file=sys.stderr),
        ))
        Path(args.out_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out_json).write_text(json.dumps(payload, indent=2) + "\n")

    raw = write_raw(payload, args.raw_dir)
    summary = summarize(payload, ground_truth)
    summary["raw_files"] = raw
    Path(args.evidence).parent.mkdir(parents=True, exist_ok=True)
    Path(args.evidence).write_text(json.dumps(summary, indent=2) + "\n")

    t = summary["totals"]
    print("oracle: %d commits / %d tags / %d known regressions / clean %s"
          % (summary["oracle"]["commits"], summary["oracle"]["tags"],
             summary["oracle"]["known_regressions"],
             ", ".join(summary["oracle"]["clean_intervals"])))
    print("A/A control deviation: %.4f%%" % (summary["aa_control_rel_dev"] * 100.0))
    print("%-16s %9s %9s %8s %8s %6s %8s %10s" % (
        "interval", "base_ns", "opt_ns", "ratio", "probes", "exact", "revert%", "within_tol"))
    for f in summary["findings"]:
        print("%-16s %9.0f %9.0f %8.3f %8d %6s %+8.3f%% %10s" % (
            f["interval"], f["from_median_ns"], f["to_median_ns"], f["ratio"],
            f["n_probes"], f["localized_exactly"],
            f["revert_rel_delta_vs_parent"] * 100.0, f["revert_within_tolerance"]))
    print("steps=%d localized=%d revert_ok=%d false_positives=%d max_probes=%d"
          % (t["steps_detected"], t["localized_exactly"], t["revert_within_tolerance"],
             t["false_positive_intervals"], t["max_probes_per_regression"]))
    print("raw samples: %d files in %s" % (len(raw), args.raw_dir))
    print("evidence: %s" % args.evidence)
    ok = (t["steps_detected"] == summary["oracle"]["known_regressions"]
          and t["localized_exactly"] == summary["oracle"]["known_regressions"]
          and t["revert_within_tolerance"] == summary["oracle"]["known_regressions"]
          and t["false_positive_intervals"] == 0
          and summary["aa_control_rel_dev"] <= 0.02)
    print("GATE: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
