"""End-to-end oracle test: six known regressions must be localized exactly.

The oracle history is generated into a temporary directory.  Its ground truth
is written *outside* the repository, so this test can compare the tool's
verdict against an answer the tool never sees.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import oracle_gen  # noqa: E402
from tagaxis import core  # noqa: E402


@pytest.fixture(scope="module")
def oracle(tmp_path_factory):
    dest = tmp_path_factory.mktemp("oracle") / "history"
    oracle_gen.build(dest)
    gt = json.loads(Path(str(dest) + ".ground_truth.json").read_text())
    return dest, gt


def test_oracle_shape(oracle):
    repo, gt = oracle
    assert len(gt["commits"]) == 36
    assert len(gt["tags"]) == 8
    assert len(gt["regressions"]) == 6
    assert gt["clean_intervals"] == ["v0.7.0..v0.8.0"]
    assert all(len(i["regressions"]) <= 1 for i in gt["intervals"])
    shas = [c["sha"] for c in gt["commits"]]
    assert len(set(shas)) == 36  # 36 independent revisions, no repeats


def test_every_revision_produces_identical_output(oracle):
    """The six regressions are semantics-preserving: output is byte-identical.

    Measured over all 36 revisions; the comparison is a sha256 of the full
    rendered output, so any change in content would be caught.
    """
    repo, gt = oracle
    digests = {}
    sizes = {}
    for commit in gt["commits"]:
        with core.Worktree(str(repo), commit["sha"]) as wt:
            _, source, out = core.run_once(wt, [sys.executable, "bench/workload.py"])
        assert source == "internal"
        payload = json.loads(out.splitlines()[-1])
        digests[commit["sha"]] = payload["sha256_out"]
        sizes[commit["sha"]] = payload["chars"]
    assert len(digests) == 36
    assert len(set(digests.values())) == 1, "revisions disagreed on output bytes"
    assert len(set(sizes.values())) == 1


def test_hunt_localizes_all_six_regressions(oracle):
    repo, gt = oracle
    result = core.hunt(
        str(repo),
        [sys.executable, "bench/workload.py"],
        samples=5,
        warmup=2,
        min_ratio=1.15,
        revert_tol=0.05,
    )

    # A/A control passed inside hunt() and is reported.
    assert result["aa"]["rel_dev"] <= 0.02

    # Exactly one step per regression interval, none in the clean interval.
    assert len(result["steps"]) == 6
    stepped = {(s["from_label"], s["to_label"]) for s in result["steps"]}
    expected = {(i["from_tag"], i["to_tag"]) for i in gt["intervals"] if i["regressions"]}
    clean = {(i["from_tag"], i["to_tag"]) for i in gt["intervals"] if not i["regressions"]}
    assert stepped == expected
    assert not (stepped & clean)

    by_interval = {(i["from_tag"], i["to_tag"]): i for i in gt["intervals"]}
    for finding in result["findings"]:
        key = (finding["from_label"], finding["to_label"])
        truth = by_interval[key]["regressions"][0]
        assert finding["bisect"]["culprit"] == truth
        assert finding["bisect"]["n_probes"] <= math.ceil(math.log2(5)) + 1
        assert finding["revert"]["within_tolerance"] is True
        assert abs(finding["revert"]["rel_delta_vs_parent"]) <= 0.05


def test_trajectory_is_monotone_in_the_regressed_intervals(oracle):
    repo, gt = oracle
    traj = core.trajectory(str(repo), core.list_tags(str(repo)),
                           [sys.executable, "bench/workload.py"], samples=3, warmup=2)
    assert len(traj) == 8
    steps = core.detect_steps(traj, min_ratio=1.15)
    assert len(steps) == 6
    # the largest tag is measurably slower than the first: >5x
    assert traj[-1]["median_ns"] / traj[0]["median_ns"] > 5.0
