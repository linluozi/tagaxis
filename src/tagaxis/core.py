"""tagaxis core: cross-version performance regression localization.

The unit of work is a *revision* (tag or commit) of a git repository plus a
workload command.  A workload is run inside a throw-away ``git worktree`` so
that every revision is measured from its own pristine tree; the repository
itself is never checked out or mutated.

Workload protocol
-----------------
The command may print a JSON object on its last stdout line, e.g.
``{"ns": 1234567}``.  That value is used as the timing (it excludes
interpreter start-up).  Otherwise the wall clock of the subprocess is used.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import statistics
import subprocess
import tempfile
import time
from pathlib import Path

__all__ = [
    "TagaxisError",
    "Worktree",
    "list_tags",
    "rev_parse",
    "is_ancestor",
    "commits_between",
    "commit_subject",
    "run_once",
    "measure",
    "measure_in",
    "trajectory",
    "detect_steps",
    "aa_control",
    "bisect",
    "revert_and_measure",
    "hunt",
]


class TagaxisError(RuntimeError):
    """Raised when git or the workload fails."""


# --------------------------------------------------------------------------
# git helpers
# --------------------------------------------------------------------------

def _git(repo, *args, check=True, input_text=None):
    proc = subprocess.run(
        ["git", "-C", os.fspath(repo), *args],
        capture_output=True,
        text=True,
        input=input_text,
    )
    if check and proc.returncode != 0:
        raise TagaxisError(
            "git %s failed (%d): %s"
            % (" ".join(args), proc.returncode, proc.stderr.strip()[:400])
        )
    return proc


def list_tags(repo, pattern=None, limit=None):
    """Tags in creator order (oldest first)."""
    args = ["tag", "--sort=creatordate"]
    if pattern:
        args += ["-l", pattern]
    tags = [t for t in _git(repo, *args).stdout.splitlines() if t.strip()]
    if limit:
        tags = tags[-limit:]
    return tags


def rev_parse(repo, rev):
    return _git(repo, "rev-parse", "--verify", "%s^{commit}" % rev).stdout.strip()


def is_ancestor(repo, older, newer):
    return _git(repo, "merge-base", "--is-ancestor", older, newer, check=False).returncode == 0


def commits_between(repo, good, bad):
    """Commits in ``good..bad``, oldest first (excludes ``good``, includes ``bad``)."""
    return _git(repo, "rev-list", "--reverse", "%s..%s" % (good, bad)).stdout.split()


def commit_subject(repo, rev):
    return _git(repo, "log", "-1", "--format=%s", rev).stdout.strip()


# --------------------------------------------------------------------------
# worktrees
# --------------------------------------------------------------------------

class Worktree:
    """Context manager yielding a detached worktree for one revision."""

    def __init__(self, repo, rev):
        self.repo = os.path.abspath(os.fspath(repo))
        self.rev = rev
        self.path = None

    def __enter__(self):
        self.path = tempfile.mkdtemp(prefix="tagaxis-wt-")
        os.rmdir(self.path)  # git worktree add insists on a non-existent path
        _git(self.repo, "worktree", "add", "--detach", "--force", self.path, self.rev)
        return self.path

    def __exit__(self, *exc):
        if self.path:
            _git(self.repo, "worktree", "remove", "--force", self.path, check=False)
            shutil.rmtree(self.path, ignore_errors=True)
            _git(self.repo, "worktree", "prune", check=False)
        return False


# --------------------------------------------------------------------------
# running a workload
# --------------------------------------------------------------------------

def run_once(workdir, cmd, env=None, timeout=300):
    """Run the workload once.  Returns ``(ns, source, stdout)``."""
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    start = time.perf_counter_ns()
    proc = subprocess.run(
        cmd,
        cwd=workdir,
        env=full_env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    wall = time.perf_counter_ns() - start
    if proc.returncode != 0:
        raise TagaxisError(
            "workload failed (%d) in %s: %s"
            % (proc.returncode, workdir, (proc.stderr or proc.stdout).strip()[-500:])
        )
    ns, source = wall, "wall"
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if lines:
        try:
            payload = json.loads(lines[-1])
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            reported = payload.get("ns")
            if isinstance(reported, int) and reported > 0:
                ns, source = reported, "internal"
    return ns, source, proc.stdout


def _pct(sorted_vals, q):
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return float(sorted_vals[0])
    pos = (n - 1) * q
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    if lo == hi:
        return float(sorted_vals[lo])
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def summarize(rev, label, samples, source):
    ordered = sorted(samples)
    mean = statistics.fmean(ordered)
    sd = statistics.stdev(ordered) if len(ordered) > 1 else 0.0
    return {
        "rev": rev,
        "label": label,
        "source": source,
        "n": len(ordered),
        "samples_ns": list(samples),
        "median_ns": statistics.median(ordered),
        "p25_ns": _pct(ordered, 0.25),
        "p75_ns": _pct(ordered, 0.75),
        "mean_ns": mean,
        "stdev_ns": sd,
        "rsd_pct": (sd / mean * 100.0) if mean else 0.0,
        "min_ns": ordered[0],
        "max_ns": ordered[-1],
    }


def measure_in(workdir, rev, cmd, samples=7, warmup=3, env=None, timeout=300,
               label=None, py_path=False):
    """Measure ``cmd`` inside an existing directory."""
    full_env = dict(env or {})
    if py_path:
        prefix = os.path.abspath(workdir)
        inherited = os.environ.get("PYTHONPATH")
        full_env["PYTHONPATH"] = prefix + ((os.pathsep + inherited) if inherited else "")
    source = "wall"
    for _ in range(warmup):
        _, source, _ = run_once(workdir, cmd, env=full_env, timeout=timeout)
    values = []
    for _ in range(samples):
        ns, source, _ = run_once(workdir, cmd, env=full_env, timeout=timeout)
        values.append(ns)
    return summarize(rev, label or rev, values, source)


def measure(repo, rev, cmd, samples=7, warmup=3, env=None, timeout=300,
            label=None, py_path=False):
    """Measure one revision in a fresh worktree."""
    sha = rev_parse(repo, rev)
    with Worktree(repo, sha) as wt:
        return measure_in(wt, sha, cmd, samples=samples, warmup=warmup, env=env,
                          timeout=timeout, label=label, py_path=py_path)


def trajectory(repo, revs, cmd, samples=7, warmup=3, **kw):
    """Measure every revision in order.  Returns a list of measurement dicts."""
    out = []
    for rev in revs:
        out.append(measure(repo, rev, cmd, samples=samples, warmup=warmup, label=rev, **kw))
    return out


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------

def detect_steps(traj, min_ratio=1.15, noise_frac=0.03):
    """Flag adjacent release pairs whose median ratio clears both thresholds."""
    floor = max(min_ratio, 1.0 + 2.0 * noise_frac)
    steps = []
    for i in range(len(traj) - 1):
        a, b = traj[i], traj[i + 1]
        ratio = b["median_ns"] / a["median_ns"]
        if ratio >= floor:
            steps.append({
                "from_index": i,
                "to_index": i + 1,
                "from_label": a["label"],
                "to_label": b["label"],
                "from_rev": a["rev"],
                "to_rev": b["rev"],
                "from_median_ns": a["median_ns"],
                "to_median_ns": b["median_ns"],
                "ratio": ratio,
            })
    return steps


def aa_control(repo, rev, cmd, rounds=2, samples=7, warmup=3, **kw):
    """A/A null test: measure the same revision twice, same conditions."""
    runs = [measure(repo, rev, cmd, samples=samples, warmup=warmup, label=rev, **kw)
            for _ in range(rounds)]
    medians = [r["median_ns"] for r in runs]
    dev = (max(medians) - min(medians)) / min(medians)
    return {"rev": rev, "medians_ns": medians, "rel_dev": dev, "runs": runs}


# --------------------------------------------------------------------------
# bisection
# --------------------------------------------------------------------------

def bisect(repo, good, bad, cmd, samples=7, warmup=3, **kw):
    """Localize the first commit in ``good..bad`` that is measurably slower.

    Assumes a single monotone step inside the range (callers bisect one
    adjacent tag pair at a time).  Classification threshold is the geometric
    mean of the two endpoint medians, so a partial step is still resolvable.
    """
    good_sha = rev_parse(repo, good)
    bad_sha = rev_parse(repo, bad)
    if not is_ancestor(repo, good_sha, bad_sha):
        raise TagaxisError("good revision %s is not an ancestor of %s" % (good, bad))
    commits = commits_between(repo, good_sha, bad_sha)
    if not commits:
        raise TagaxisError("no commits in %s..%s" % (good, bad))

    gm = measure(repo, good_sha, cmd, samples=samples, warmup=warmup, label=good, **kw)
    bm = measure(repo, bad_sha, cmd, samples=samples, warmup=warmup, label=bad, **kw)
    if gm["median_ns"] >= bm["median_ns"]:
        raise TagaxisError("bad revision is not slower than good revision")
    threshold = math.sqrt(gm["median_ns"] * bm["median_ns"])

    cache = {gm["rev"]: gm, bm["rev"]: bm}
    probes = []
    lo, hi = -1, len(commits) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        sha = commits[mid]
        m = measure(repo, sha, cmd, samples=samples, warmup=warmup, label=sha[:12], **kw)
        cache[sha] = m
        probes.append({"sha": sha, "median_ns": m["median_ns"],
                       "slower_than_threshold": m["median_ns"] > threshold})
        if m["median_ns"] > threshold:
            hi = mid
        else:
            lo = mid

    culprit = commits[hi]
    measured = [cache[c] for c in commits if c in cache]
    return {
        "good": good,
        "bad": bad,
        "good_sha": good_sha,
        "bad_sha": bad_sha,
        "n_commits": len(commits),
        "n_probes": len(probes),
        "log2_commits": math.log2(len(commits)) if len(commits) > 1 else 0.0,
        "threshold_ns": threshold,
        "culprit": culprit,
        "culprit_subject": commit_subject(repo, culprit),
        "probes": probes,
        "measured": measured,
    }


# --------------------------------------------------------------------------
# causal revert
# --------------------------------------------------------------------------

def revert_and_measure(repo, sha, cmd, samples=7, warmup=3, **kw):
    """Reverse-apply ``sha`` in a worktree at ``sha`` and re-measure."""
    patch = _git(repo, "show", "--format=", "--no-color", sha).stdout
    with Worktree(repo, sha) as wt:
        proc = subprocess.run(["git", "apply", "-R", "--whitespace=nowarn", "-"],
                              cwd=wt, input=patch, text=True, capture_output=True)
        if proc.returncode != 0:
            proc = subprocess.run(["git", "apply", "-R", "--3way", "--whitespace=nowarn", "-"],
                                  cwd=wt, input=patch, text=True, capture_output=True)
        if proc.returncode != 0:
            raise TagaxisError("could not reverse-apply %s: %s" % (sha[:12], proc.stderr.strip()[:400]))
        return measure_in(wt, sha, cmd, samples=samples, warmup=warmup, label=sha[:12], **kw)


# --------------------------------------------------------------------------
# one-shot hunt
# --------------------------------------------------------------------------

def hunt(repo, cmd, tags=None, pattern=None, limit=None, min_ratio=1.15,
         samples=7, warmup=3, aa_rounds=2, revert_tol=0.05, progress=None, **kw):
    """Trace tags, detect steps, bisect each step, prove causality by revert."""
    revs = tags if tags is not None else list_tags(repo, pattern=pattern, limit=limit)
    if len(revs) < 2:
        raise TagaxisError("need at least two revisions, got %d" % len(revs))

    def say(msg):
        if progress:
            progress(msg)

    say("A/A control on %s" % revs[0])
    aa = aa_control(repo, revs[0], cmd, rounds=aa_rounds, samples=samples, warmup=warmup, **kw)
    if aa["rel_dev"] > 0.02:
        raise TagaxisError(
            "A/A control failed: same revision differs by %.2f%% (>2%%)"
            % (aa["rel_dev"] * 100.0)
        )

    say("trajectory over %d revisions" % len(revs))
    traj = []
    for rev in revs:
        m = measure(repo, rev, cmd, samples=samples, warmup=warmup, label=rev, **kw)
        traj.append(m)
        say("  %-14s %10.0f ns (rsd %.2f%%)" % (rev, m["median_ns"], m["rsd_pct"]))

    steps = detect_steps(traj, min_ratio=min_ratio)
    findings = []
    for step in steps:
        say("bisect %s..%s (ratio %.3fx)" % (step["from_label"], step["to_label"], step["ratio"]))
        b = bisect(repo, step["from_rev"], step["to_rev"], cmd,
                   samples=samples, warmup=warmup, **kw)
        parent = _git(repo, "rev-parse", "%s^" % b["culprit"], check=False).stdout.strip()
        revert = None
        if parent:
            base = measure(repo, parent, cmd, samples=samples, warmup=warmup,
                           label=parent[:12], **kw)
            rev_m = revert_and_measure(repo, b["culprit"], cmd, samples=samples,
                                       warmup=warmup, **kw)
            rel = (rev_m["median_ns"] - base["median_ns"]) / base["median_ns"]
            revert = {
                "parent": parent,
                "parent_median_ns": base["median_ns"],
                "reverted_median_ns": rev_m["median_ns"],
                "rel_delta_vs_parent": rel,
                "within_tolerance": abs(rel) <= revert_tol,
                "tolerance": revert_tol,
                "reverted_measurement": rev_m,
            }
        findings.append({**step, "bisect": b, "revert": revert})
        say("  culprit %s  revert delta %s"
            % (b["culprit"][:12],
               ("%.3f%%" % (revert["rel_delta_vs_parent"] * 100.0)) if revert else "n/a"))

    return {
        "aa": aa,
        "trajectory": traj,
        "steps": steps,
        "findings": findings,
    }