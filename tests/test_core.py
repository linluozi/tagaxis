"""Unit tests for tagaxis core primitives (no oracle needed)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tagaxis import core  # noqa: E402
from tagaxis.cli import main  # noqa: E402


def _run(cwd, *args, env=None):
    full = dict(os.environ)
    if env:
        full.update(env)
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, env=full)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.fixture()
def tiny_repo(tmp_path):
    """Three commits, one tag each; commit 2 doubles the work."""
    repo = tmp_path / "tiny"
    repo.mkdir()
    _run(repo, "git", "init", "-q", "-b", "main")
    _run(repo, "git", "config", "user.name", "t")
    _run(repo, "git", "config", "user.email", "t@example.invalid")
    (repo / "work.py").write_text(
        "import json, time\n"
        "n = 40000\n"
        "t0 = time.perf_counter_ns()\n"
        "s = 0\n"
        "for i in range(n):\n"
        "    s += i*i\n"
        "dt = time.perf_counter_ns() - t0\n"
        "print(json.dumps({'ns': dt, 'sum': s}))\n"
    )
    _run(repo, "git", "add", "-A")
    _run(repo, "git", "commit", "-q", "-m", "c0")
    _run(repo, "git", "tag", "v1")
    (repo / "work.py").write_text(
        "import json, time\n"
        "n = 40000\n"
        "t0 = time.perf_counter_ns()\n"
        "s = 0\n"
        "for i in range(n):\n"
        "    s += i*i\n"
        "    s += 0\n"
        "dt = time.perf_counter_ns() - t0\n"
        "print(json.dumps({'ns': dt, 'sum': s}))\n"
    )
    _run(repo, "git", "add", "-A")
    _run(repo, "git", "commit", "-q", "-m", "c1 adds a redundant add")
    _run(repo, "git", "tag", "v2")
    (repo / "work.py").write_text(
        "import json, time\n"
        "n = 40000\n"
        "t0 = time.perf_counter_ns()\n"
        "s = 0\n"
        "for i in range(n):\n"
        "    s += i*i\n"
        "    s += 0\n"
        "    s = s % 1000000007\n"
        "dt = time.perf_counter_ns() - t0\n"
        "print(json.dumps({'ns': dt, 'sum': s}))\n"
    )
    _run(repo, "git", "add", "-A")
    _run(repo, "git", "commit", "-q", "-m", "c2 adds a modulo")
    _run(repo, "git", "tag", "v3")
    return repo


def test_pct_linear_interpolation():
    assert core._pct([1, 2, 3, 4], 0.5) == pytest.approx(2.5)
    assert core._pct([10], 0.25) == 10.0
    assert core._pct([0, 10], 0.25) == pytest.approx(2.5)


def test_summarize_reports_median_and_quartiles():
    s = core.summarize("rev", "lbl", [5, 1, 3, 2, 4], "internal")
    assert s["median_ns"] == 3
    assert s["min_ns"] == 1 and s["max_ns"] == 5
    assert s["p25_ns"] == pytest.approx(2.0)
    assert s["p75_ns"] == pytest.approx(4.0)
    assert s["source"] == "internal"


def test_detect_steps_thresholds_and_floor():
    traj = [
        {"label": "a", "rev": "a", "median_ns": 100.0},
        {"label": "b", "rev": "b", "median_ns": 110.0},   # 1.10, under floor
        {"label": "c", "rev": "c", "median_ns": 130.0},   # 1.18, over floor
        {"label": "d", "rev": "d", "median_ns": 129.0},   # 0.99, no step
    ]
    steps = core.detect_steps(traj, min_ratio=1.15)
    assert len(steps) == 1
    assert steps[0]["from_label"] == "b" and steps[0]["to_label"] == "c"
    assert steps[0]["ratio"] == pytest.approx(1.1818, rel=1e-3)


def test_run_once_prefers_internal_ns(tiny_repo):
    ns, source, out = core.run_once(str(tiny_repo), [sys.executable, "work.py"])
    assert source == "internal" and ns > 0
    assert json.loads(out.splitlines()[-1])["sum"] > 0


def test_run_once_falls_back_to_wall_clock(tiny_repo):
    ns, source, _ = core.run_once(str(tiny_repo), [sys.executable, "-c", "print('no json here')"])
    assert source == "wall" and ns > 0


def test_run_once_raises_on_failure(tiny_repo):
    with pytest.raises(core.TagaxisError):
        core.run_once(str(tiny_repo), [sys.executable, "-c", "import sys; sys.exit(3)"])


def test_run_once_parses_internal_ns_and_rejects_bogus(tiny_repo):
    ns, source, _ = core.run_once(
        str(tiny_repo), [sys.executable, "-c", "import json;print(json.dumps({'ns': 4242}))"]
    )
    assert (ns, source) == (4242, "internal")
    _, source, _ = core.run_once(
        str(tiny_repo), [sys.executable, "-c", "import json;print(json.dumps({'ns': -1}))"]
    )
    assert source == "wall"


def test_worktree_is_detached_and_cleaned(tiny_repo):
    with core.Worktree(str(tiny_repo), "v1") as wt:
        p = Path(wt)
        assert p.exists() and (p / "work.py").exists()
        assert "for i in range(n):\n    s += i*i\n" in (p / "work.py").read_text()
        created = p
    assert not created.exists()


def test_commits_between_and_ancestry(tiny_repo):
    commits = core.commits_between(str(tiny_repo), "v1", "v3")
    assert len(commits) == 2
    assert core.is_ancestor(str(tiny_repo), "v1", "v3")
    assert not core.is_ancestor(str(tiny_repo), "v3", "v1")
    assert core.list_tags(str(tiny_repo)) == ["v1", "v2", "v3"]


def test_measure_and_aa_control(tiny_repo):
    m = core.measure(str(tiny_repo), "v1", [sys.executable, "work.py"], samples=3, warmup=1)
    assert m["n"] == 3 and len(m["samples_ns"]) == 3
    aa = core.aa_control(str(tiny_repo), "v1", [sys.executable, "work.py"],
                         rounds=2, samples=3, warmup=1)
    assert len(aa["medians_ns"]) == 2
    assert aa["rel_dev"] >= 0.0


def test_revert_removes_the_regression_and_restores_timing(tiny_repo):
    """Reverse-applying the regression commit restores the previous source."""
    sha = core.commits_between(str(tiny_repo), "v1", "v2")[0]
    with core.Worktree(str(tiny_repo), sha) as wt:
        assert "s += 0" in Path(wt, "work.py").read_text()
        patch = subprocess.run(
            ["git", "-C", str(tiny_repo), "show", "--format=", sha],
            capture_output=True, text=True).stdout
        proc = subprocess.run(["git", "apply", "-R", "-"], cwd=wt, input=patch,
                              text=True, capture_output=True)
        assert proc.returncode == 0, proc.stderr
        reverted = Path(wt, "work.py").read_text()
        assert "s += 0" not in reverted and "s += i*i" in reverted
    rev = core.revert_and_measure(str(tiny_repo), sha, [sys.executable, "work.py"],
                                  samples=3, warmup=1)
    base = core.measure(str(tiny_repo), "v1", [sys.executable, "work.py"],
                        samples=3, warmup=1)
    assert rev["n"] == 3 and rev["median_ns"] > 0
    # the reverted tree is the previous tree, so timings must agree within 15%
    assert abs(rev["median_ns"] - base["median_ns"]) / base["median_ns"] < 0.15


def test_bisect_refuses_non_ancestor(tiny_repo):
    with pytest.raises(core.TagaxisError):
        core.bisect(str(tiny_repo), "v3", "v1", [sys.executable, "work.py"], samples=1, warmup=0)


def test_cli_tags(tiny_repo, capsys):
    rc = main(["tags", "--repo", str(tiny_repo)])
    assert rc == 0
    assert "v3" in capsys.readouterr().out


def test_cli_unknown_workload_returns_1(tiny_repo, capsys):
    rc = main(["aa", "--repo", str(tiny_repo), "--rev", "v1",
               "--cmd", "%s -c 'import sys; sys.exit(1)'" % sys.executable,
               "--samples", "1", "--warmup", "0"])
    assert rc == 1
    assert "tagaxis:" in capsys.readouterr().err
