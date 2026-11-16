"""Build a deterministic git history with six known performance regressions.

The generated repository is a small, real text-statistics library
(``miniindex``).  Its workload is committed once, at the first commit, and
never changes.  Over 36 commits the library gains ordinary features.  At six
known commits, one line is added to the *hot loop* of ``analyze()``: an extra
normalisation pass that is a no-op on this corpus (``strip``, ``casefold``,
UTF-8 round-trip, ASCII round-trip, a regex scrub, NFC normalisation) but
costs real CPU per token.  Because every regression targets the same hot
loop, each one adds time in proportion to the current total, so all six are
visible in the timing trajectory.

Each regression sits in a different release interval and one interval is left
clean, which gives a built-in false-positive control.

Usage::

    python3 tools/oracle_gen.py /tmp/oracle-history
    # writes <dest>/ (git repo) and <dest>.ground_truth.json

The ground truth is written *outside* the repository on purpose: the tool
under test must not be able to read the answer.
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

COMMITS = 36
TAG_EVERY = 5
TAG_COUNT = 8  # tags at commits 0,5,...,35 -> 8 tags, 7 intervals

# commit index -> the normalisation line introduced at that commit
REGRESSIONS = {
    2: "        t = t.strip().casefold()",
    8: "        t = t.encode('utf-8').decode('utf-8')",
    13: "        t = t.encode('ascii', 'ignore').decode('ascii')",
    18: "        t = ' '.join([p for p in t.split() if p])",
    23: "        t = ''.join(ch for ch in t if ch.isprintable())",
    28: "        t = re.sub(r'[^0-9a-z]+', '', t)",
}

# The interval [31..35] is intentionally clean (control).

HEADER = '''"""miniindex pipeline: tokenise, normalise and rank word frequencies."""

import collections
import re
import unicodedata


'''

ANALYZE_PREFIX = '''def analyze(text):
    counts = {}
    for word in text.split():
        t = word.lower()
'''

ANALYZE_SUFFIX = '''        if t in counts:
            counts[t] = counts[t] + 1
        else:
            counts[t] = 1

    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return "\\n".join("%s\\t%d" % (w, c) for w, c in ranked)
'''

WORKLOAD = '''"""Workload for the miniindex history.  Committed once, never changed."""
import hashlib
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from miniindex.pipeline import analyze


def make_text():
    rnd = random.Random(20261117)
    vocab = ["w%05d" % i for i in range(4000)]
    return "\\n".join(
        " ".join(rnd.choice(vocab) for _ in range(8)) for _ in range(6000)
    )


TEXT = make_text()

best = None
out = ""
for _ in range(3):
    start = time.perf_counter_ns()
    out = analyze(TEXT)
    elapsed = time.perf_counter_ns() - start
    best = elapsed if best is None else min(best, elapsed)

print(json.dumps({
    "ns": best,
    "chars": len(out),
    "sha256_out": hashlib.sha256(out.encode("utf-8")).hexdigest(),
}))
'''


def _git(repo, *args, env=None):
    full = dict(os.environ)
    if env:
        full.update(env)
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, env=full)
    if proc.returncode != 0:
        raise SystemExit("git %s failed: %s" % (" ".join(args), proc.stderr))
    return proc.stdout


def pipeline_source(chain):
    body = HEADER + ANALYZE_PREFIX
    for line in chain:
        body += line + "\n"
    body += ANALYZE_SUFFIX
    return body


def feature_source(k):
    return (
        "\ndef feature_%03d(value=None):\n"
        "    \"\"\"Feature added at commit %03d.\"\"\"\n"
        "    return value if value is not None else %d\n" % (k, k, k)
    )


def build(dest):
    dest = Path(dest).resolve()
    gt_path = Path(str(dest) + ".ground_truth.json")
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    _git(dest, "init", "-q", "-b", "main")
    _git(dest, "config", "user.name", "oracle")
    _git(dest, "config", "user.email", "oracle@example.invalid")
    _git(dest, "config", "commit.gpgsign", "false")

    (dest / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    (dest / "bench").mkdir()
    (dest / "bench" / "workload.py").write_text(WORKLOAD)
    (dest / "README.md").write_text(
        "# miniindex\n\nDeterministic text-statistics pipeline used as a history oracle.\n"
    )

    pkg = dest / "miniindex"
    pkg.mkdir()
    (pkg / "__init__.py").write_text('"""miniindex."""\n')

    chain = []
    ground_truth = {"regressions": [], "commits": []}
    tags = {}

    for k in range(COMMITS):
        features = list(range(1, k + 1))
        if k in REGRESSIONS:
            chain.append(REGRESSIONS[k])
        body = pipeline_source(chain)
        for f in features:
            body += feature_source(f)
        body += "\nFEATURES = [%s]\n" % ", ".join("feature_%03d" % f for f in features)
        (pkg / "pipeline.py").write_text(body)

        date = (datetime.date(2026, 1, 1) + datetime.timedelta(days=k)).strftime(
            "%Y-%m-%dT%H:%M:%S+00:00")
        env = {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        _git(dest, "add", "-A")
        subject = "commit %02d" % k
        if k in REGRESSIONS:
            subject = "analyze: normalise tokens with %s" % REGRESSIONS[k].split("= ")[1]
        _git(dest, "commit", "-q", "-m", subject, env=env)
        sha = _git(dest, "rev-parse", "HEAD").strip()
        ground_truth["commits"].append({"index": k, "sha": sha, "subject": subject})
        if k in REGRESSIONS:
            ground_truth["regressions"].append({
                "commit_index": k, "sha": sha,
                "line": REGRESSIONS[k].strip(), "subject": subject,
            })
        if k % TAG_EVERY == 0 and k // TAG_EVERY < TAG_COUNT:
            tag = "v0.%d.0" % (k // TAG_EVERY + 1)
            _git(dest, "tag", tag)
            tags[tag] = sha

    index_of = {c["sha"]: c["index"] for c in ground_truth["commits"]}
    intervals = []
    tag_list = sorted(tags, key=lambda t: index_of[tags[t]])
    for i in range(len(tag_list) - 1):
        lo = index_of[tags[tag_list[i]]]
        hi = index_of[tags[tag_list[i + 1]]]
        regs = [r for r in ground_truth["regressions"] if lo < r["commit_index"] <= hi]
        intervals.append({
            "from_tag": tag_list[i], "to_tag": tag_list[i + 1],
            "from_index": lo, "to_index": hi,
            "regressions": [r["sha"] for r in regs],
        })
    ground_truth["tags"] = tags
    ground_truth["intervals"] = intervals
    ground_truth["clean_intervals"] = [i["from_tag"] + ".." + i["to_tag"]
                                       for i in intervals if not i["regressions"]]
    gt_path.write_text(json.dumps(ground_truth, indent=2) + "\n")
    return dest, gt_path


def main(argv):
    dest = argv[1] if len(argv) > 1 else "/tmp/oracle-history"
    repo, gt = build(dest)
    data = json.loads(Path(gt).read_text())
    print("repo:        %s" % repo)
    print("commits:     %d" % len(data["commits"]))
    print("tags:        %d" % len(data["tags"]))
    print("regressions: %d" % len(data["regressions"]))
    for r in data["regressions"]:
        print("  commit %02d %s  %s" % (r["commit_index"], r["sha"][:12], r["line"]))
    print("clean intervals: %s" % ", ".join(data["clean_intervals"]))
    print("ground truth: %s" % gt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
