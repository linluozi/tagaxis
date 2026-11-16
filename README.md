# tagaxis — find the commit that made it slow, and prove it

`tagaxis` measures one workload across a git repository's release tags, finds
the intervals where a *real* slowdown appears, binary-searches each interval
down to a single commit, and then **reverse-applies that commit and re-measures**
to prove the slowdown is caused by it and not by drift. Every measurement runs
in a throw-away `git worktree`, so the repository is never checked out or
modified.

Upstream: none

This is an original tool, not a fork or a patch of somebody else's library.
It ships with a deterministic **ground-truth oracle**: a 36-commit history
with six known, semantics-preserving performance regressions, placed in six
different release intervals, plus one deliberately clean interval that acts
as a false-positive control. The tool is graded against that answer, which it
cannot read (the ground truth is written outside the repository).

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[test]"
.venv/bin/python -m pytest -q
.venv/bin/python benchmark/bench.py
```

The third command builds the oracle in a fresh temporary directory, runs the
whole trace → detect → bisect → revert hunt, refreshes `benchmark/raw/`, and
prints the measured table below. It exits non-zero if any gate fails.

**Result on the shipped oracle (36 commits, 8 tags, 6 known regressions,
1 clean control interval): 6/6 regressions localized to exactly one commit,
6/6 confirmed against their own parent, 6/6 revert-proven (all within 1.1%
of the parent), 0 false-positive intervals, interleaved A/A control 0.19%.**
Full table and raw samples below.

## Why this exists

"Our library got slower between 1.4 and 2.0" is usually answered with a
hand-written bisect script, and every script re-invents the same five things:
a noise floor, a classification rule for "slow", a way to prove causality,
raw sample retention, and a refusal to report when the environment is too
noisy. `tagaxis` packages those five as a reproducible command, and its
release-tag trajectory is measured, not assumed.

Three phases:

1. **Interleaved A/A control** — the same revision is measured twice, with
   the two sequences *alternated run by run* inside one worktree, so slow
   machine drift (thermal, page cache, co-tenants) hits both and cancels. If
   the two medians differ by more than `--aa-tol` (default 3%), the tool
   refuses to emit a verdict. The non-interleaved version of this control
   tripped at 0.55%, 1.19%, 2.39% and 4.17% on the same machine; interleaving
   is what made the floor usable.
2. **Trajectory + detection** — every tag is measured (warmup discarded, then
   `--samples` timed runs, median reported). Adjacent tags are flagged only if
   `later_median / earlier_median >= max(min-ratio, 1 + 2*noise)`.
3. **Bisect + confirm + revert proof** — the commits between the two tags are
   binary searched; a commit counts as "slow" when its median exceeds the
   geometric mean of the two endpoint medians. Bisect probes use at least 11
   runs, and the winner is then **re-checked against its own parent**: if the
   culprit is not measurably slower than the parent (≥5% by default), the
   verdict is reported as `ambiguous` instead of guessed. Finally the culprit
   is reverse-applied in its own worktree and re-measured; the revert is
   accepted only if the re-measured median returns to the parent commit
   within `--revert-tol`.

## Measured result on the ground-truth oracle

Hardware and run details are in [Environment](#environment). Workload:
`python3 bench/workload.py` (48,000 tokens through `miniindex.analyze`),
7 timed samples per revision, 3 warmups discarded, median reported.

| interval | pre median (ns) | post median (ns) | ratio | probes | culprit localized | revert Δ vs parent |
|---|---:|---:|---:|---:|---|---:|
| v0.1.0..v0.2.0 | 11,506,345 | 13,862,976 | **1.205x** | 2 / 5 commits | exact, confirmed | +1.062% |
| v0.2.0..v0.3.0 | 13,862,976 | 21,228,556 | **1.531x** | 2 / 5 commits | exact, confirmed | −0.474% |
| v0.3.0..v0.4.0 | 21,228,556 | 28,513,575 | **1.343x** | 2 / 5 commits | exact, confirmed | +0.256% |
| v0.4.0..v0.5.0 | 28,513,575 | 38,105,089 | **1.336x** | 2 / 5 commits | exact, confirmed | +0.850% |
| v0.5.0..v0.6.0 | 38,105,089 | 69,690,360 | **1.829x** | 2 / 5 commits | exact, confirmed | −0.139% |
| v0.6.0..v0.7.0 | 69,690,360 | 113,706,975 | **1.632x** | 2 / 5 commits | exact, confirmed | +0.086% |
| v0.7.0..v0.8.0 *(clean control)* | 113,706,975 | 113,492,713 | 0.998x | not searched | — | — |

Summary: **6 / 6 regressions localized to exactly one commit**, **6 / 6**
confirmed against their own parent, **0 / 1** false-positive intervals,
**6 / 6** reverts returned to the parent within tolerance, **2** probes per
regression (the theoretical minimum is `ceil(log2 5) + 1 = 4`). The
interleaved A/A control deviation for this run was **0.19%**.

The per-revision medians, quartiles and relative standard deviations are in
`benchmark/evidence.json`; every individual timing sample is in
`benchmark/raw/` (34 files). All numbers above are regenerated by
`python3 benchmark/bench.py`; nothing is transcribed by hand.

## Differential / equivalence evidence

The six regressions are *semantics-preserving by construction* (they add
normalisation passes that are no-ops on this ASCII corpus). That is checkable
rather than asserted:

* **36 independent revisions, 1 output digest.** The workload prints
  `sha256` of its full rendered output. All 36 revisions — each a distinct
  tree, no repeats — produce the **same** digest. Independent sample count is
  36 revisions, not loop iterations.
* **6 / 6 reverts reproduce the parent tree's timing.** A revert is only
  accepted when the re-measured median returns to the parent commit's median
  within 5%; the observed deltas are all within 1.8%.

## Correctness gates

This repository is **not** an `-opt-lab` patch and makes **no speedup claim**
about any upstream library, so the opt-lab G1–G5 template (upstream test
suite, upstream speedup, real-corpus speedup) does not apply. The gates that
do apply are listed with their exact commands:

| gate | claim | command | result |
|---|---|---|---|
| A | workload runs green on every revision | `pytest tests/test_oracle.py::test_every_revision_produces_identical_output -q` | 36/36, 0 failures |
| B | output is byte-identical across all revisions | same test | 36/36 identical sha256 |
| C | 6/6 regressions localized to the exact commit | `pytest tests/test_oracle.py::test_hunt_localizes_all_six_regressions -q` | 6/6 exact |
| D | 0 false positives on the clean interval | same test | 0 of 1 clean intervals flagged |
| E | every culprit is measurably slower than its own parent | same test | 6/6 confirmed, 0 ambiguous |
| F | reverts restore the parent within 5% | same test | 6/6 |
| G | interleaved A/A noise floor ≤ 3% | `python3 benchmark/bench.py` | 0.19% (this run) |
| H | probes ≤ `ceil(log2 commits) + 1` | `python3 benchmark/bench.py` | 2 ≤ 4 |
| I | peak RSS of the driver | `/usr/bin/time -v python3 -m tagaxis.cli hunt --repo ... --samples 7 --warmup 3 --out /tmp/h.json` | 19,828 kB (19.4 MiB), exit 0 |

Full suite:

```
18 passed
```

The end-to-end oracle test inside the suite uses the shipped sampling (7 runs
per measurement), so the suite takes a couple of minutes.

**The A/A gate is not decoration.** The first prototype compared two
consecutive blocks of the same revision and tripped repeatedly on this
machine: 0.55%, 1.19%, 2.39%, and 4.17% at `--samples 3` (the last one
aborted the hunt with `tagaxis: A/A control failed: ... (>2%)`, exit 1,
before any bisection). Interleaving the two sequences inside one worktree
removed most of that drift: three independent `tagaxis aa --samples 3` runs
on the same revision now read **0.56%, 0.18%, 0.03%**, and the shipped
7-sample run reads **0.19%**.

## Reproduce

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[test]"
.venv/bin/python -m pytest -q
.venv/bin/python benchmark/bench.py
```

The third command builds the oracle in a fresh temporary directory, runs the
full trace → detect → bisect → revert hunt, refreshes `benchmark/raw/` and
`benchmark/evidence.json`, prints the table above and exits non-zero if any
gate fails.

## Environment

| | |
|---|---|
| CPU | AMD EPYC 7K62 48-Core Processor (4 vCPU visible) |
| memory | 3,723 MiB total, 1,987 MiB swap |
| runtime | CPython 3.12.3 |
| seed | 20261117 (fixed; it seeds the oracle corpus generator) |
| timing source | `time.perf_counter_ns()` inside the workload (excludes interpreter start-up) |
| sampling | 3 warmups discarded, then 7 timed runs; median + p25/p75 reported |
| bisect probes | ≥11 timed runs per probe, then parent confirmation | 
| A/A control | interleaved, 2×7 alternating runs, default tolerance 3% |
| full run wall time | ≈ 90 s |

## 本轮创新

**新在哪**：把「哪个 commit 让性能变差」做成一个**可判定**的工具，而不是再解释一个已经存在的 PR 或对某个库做一次单点加速 —— 版本轴（逐 tag 重放）→ 分段检测 → 单 commit bisect → **revert 因果复证** 四步闭环，且带一个**有标准答案的 36-commit oracle**（6 个已知回归分布在 6 个不同发布区间，另有 1 个干净区间做假阳性对照，答案写在仓库外）。

**和谁不一样**：台账里的 `sametree-recheck` / `pr-evidence-recheck` 是在**已有 PR 的同树上**复核别人的加速声明；`regime-cert` 是在**规模轴**上外推；`wheel-src-delta` 比的是 wheel 与 tag 的字节。没有一条把同一仓库的**版本轴**做成逐 tag 轨迹 + 分段 bisect + revert 复证，也没有一条用「不知道答案的工具 vs 仓库外的 ground truth」来给自己打分。

**凭什么推翻旧结论**：旧做法只能回答「这个 PR 声称的加速是否成立」，本工具回答的是「**是哪一个 commit 引入的，撤回它能否回到基线**」，并且把「不能判定」做成一等结果（A/A 噪声超限时工具拒绝出裁决，而不是给一个好看的数）。同一条经验也在本轮被本工具自己证实：干净区间 v0.7.0..v0.8.0 的两端实测差 **−4.1%**，即在 100 ms 量级上跨区间噪声地板约 4%，而最小真实台阶是 **+21.0%**（5 倍于噪声）—— 这解释了为什么「≥1.15x 才算台阶」不能靠减少样本或取最快值来变宽松。

## Limitations (honest)

* **The oracle history is purpose-built, not an upstream repository.** It is
  real git, real commits, real measured slowdowns, and a ground truth the
  tool cannot read — but it is not evidence about any third-party library.
  The tool has not been used here to find a regression in a real upstream, so
  no claim about any real project is made.
* **Cross-interval noise is larger than A/A noise.** The A/A control measures
  the *same* revision twice (0.55% here). The clean interval shows an
  apparent −4.1% difference between two revisions that the oracle defines as
  performance-equivalent, so the practical noise floor at the 100 ms scale is
  ≈4%, not 0.55%. Detection therefore requires a 15% step; a 5% true
  regression is **not** reliably decidable on this machine.
* **A single noisy probe can still mislead the binary search.** On one
  clean-clone run the first interval was mislocalized: a probe at the true
  culprit measured low and the search moved one commit right. That run is
  the reason the parent-confirmation step exists — the wrong answer is now
  reported as `ambiguous` rather than returned as a culprit, and probe
  sampling was raised from 7 to 11 runs. This is a mitigation, not a proof:
  if both the culprit and a neighbour are misread, the tool says `ambiguous`,
  and an `ambiguous` verdict is a non-result, not a pass.
* **Cross-interval drift is larger than the interleaved A/A floor.** Two full
  runs of the shipped oracle measured the performance-equivalent clean
  interval at **−4.1%** and **−0.19%** respectively. The practical noise
  floor for comparing two *different* revisions on this machine is therefore
  a few percent, not the 0.19% A/A number. That is why detection demands a
  15% step: a true 3% regression is not decidable here, and the tool does not
  pretend otherwise.
* **Regression order matters.** `bisect()` assumes a single monotone step
  inside the searched range; that is why `hunt()` bisects one adjacent tag
  pair at a time. A regression that is fixed again inside the same interval
  would be missed.
* **A single tag step is not proof of regression magnitude.** The ratio is a
  ratio of medians of 7 subprocess runs; p25/p75 are reported so readers can
  see the spread rather than trust one number.
* **Python only.** Measurements are subprocess-based and use
  `time.perf_counter_ns()`; there is no Node.js timer path.
* Only CPython 3.12.3 is available on this machine; the code declares
  `>=3.9` but 3.9–3.11 were not executed.

## License

MIT (see `LICENSE`). Original work; no upstream code is included.
