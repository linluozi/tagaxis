"""Command line entry point for tagaxis."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import subprocess
import sys
import time
from pathlib import Path

from . import __version__
from .core import (
    TagaxisError,
    aa_control,
    bisect,
    hunt,
    list_tags,
    measure,
    revert_and_measure,
    trajectory,
)


def _json_default(obj):
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError("not serializable: %r" % (obj,))


def _dump(path, payload):
    text = json.dumps(payload, indent=2, sort_keys=False, default=_json_default)
    if path:
        Path(path).write_text(text + "\n")
        print("wrote %s" % path)
    else:
        print(text)


def _env_fingerprint(seed):
    cpus = []
    try:
        out = subprocess.run(["lscpu"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            if line.startswith(("Model name", "CPU(s)", "Architecture", "Thread(s)")):
                cpus.append(line.strip())
    except OSError:
        pass
    mem_kb = None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal"):
                mem_kb = int(line.split()[1])
    except OSError:
        pass
    return {
        "lscpu": cpus,
        "mem_total_kb": mem_kb,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "seed": seed,
        "tagaxis_version": __version__,
    }


def _add_common(p):
    p.add_argument("--repo", required=True, help="path or URL of a local git repository")
    p.add_argument("--cmd", required=True, help="workload command line")
    p.add_argument("--samples", type=int, default=7)
    p.add_argument("--warmup", type=int, default=3)
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--py-path", action="store_true",
                   help="prepend the worktree root to PYTHONPATH")
    p.add_argument("--seed", type=int, default=20261117)


def _cmd(argv):
    return shlex.split(argv)


def build_parser():
    ap = argparse.ArgumentParser(prog="tagaxis", description=__doc__)
    ap.add_argument("--version", action="version", version="tagaxis %s" % __version__)
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("tags", help="list tags in creator order")
    p.add_argument("--repo", required=True)
    p.add_argument("--pattern", default=None)
    p.add_argument("--limit", type=int, default=None)

    p = sub.add_parser("aa", help="A/A null test on one revision")
    _add_common(p)
    p.add_argument("--rev", required=True)
    p.add_argument("--rounds", type=int, default=2)
    p.add_argument("--out", default=None)

    p = sub.add_parser("trace", help="measure a per-revision timing trajectory")
    _add_common(p)
    p.add_argument("--pattern", default=None)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--revs", nargs="*", default=None)
    p.add_argument("--out", default=None)

    p = sub.add_parser("bisect", help="binary-search a known regression range")
    _add_common(p)
    p.add_argument("--good", required=True)
    p.add_argument("--bad", required=True)
    p.add_argument("--out", default=None)

    p = sub.add_parser("revert", help="reverse-apply one commit and re-measure")
    _add_common(p)
    p.add_argument("--rev", required=True)
    p.add_argument("--out", default=None)

    p = sub.add_parser("hunt", help="trace + detect + bisect + revert proof")
    _add_common(p)
    p.add_argument("--pattern", default=None)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--min-ratio", type=float, default=1.15)
    p.add_argument("--aa-tol", type=float, default=0.03)
    p.add_argument("--aa-rounds", type=int, default=2)
    p.add_argument("--revert-tol", type=float, default=0.05)
    p.add_argument("--out", required=True)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    started = time.time()
    try:
        if args.command == "tags":
            for t in list_tags(args.repo, pattern=args.pattern, limit=args.limit):
                print(t)
            return 0

        if args.command == "aa":
            result = aa_control(args.repo, args.rev, _cmd(args.cmd),
                                pairs=max(args.samples, 7) * max(args.rounds, 1),
                                warmup=args.warmup,
                                timeout=args.timeout, py_path=args.py_path)
            _dump(args.out, {"meta": _env_fingerprint(args.seed), "aa": result})
            return 0

        if args.command == "trace":
            revs = args.revs or list_tags(args.repo, pattern=args.pattern, limit=args.limit)
            traj = trajectory(args.repo, revs, _cmd(args.cmd), samples=args.samples,
                              warmup=args.warmup, timeout=args.timeout, py_path=args.py_path)
            _dump(args.out, {"meta": _env_fingerprint(args.seed), "trajectory": traj})
            return 0

        if args.command == "bisect":
            result = bisect(args.repo, args.good, args.bad, _cmd(args.cmd),
                            samples=args.samples, warmup=args.warmup,
                            timeout=args.timeout, py_path=args.py_path)
            _dump(args.out, {"meta": _env_fingerprint(args.seed), "bisect": result})
            return 0

        if args.command == "revert":
            result = revert_and_measure(args.repo, args.rev, _cmd(args.cmd),
                                        samples=args.samples, warmup=args.warmup,
                                        timeout=args.timeout, py_path=args.py_path)
            _dump(args.out, {"meta": _env_fingerprint(args.seed), "revert": result})
            return 0

        if args.command == "hunt":
            payload = {
                "meta": _env_fingerprint(args.seed),
                "repo": os.path.abspath(args.repo),
                "cmd": args.cmd,
                "samples": args.samples,
                "warmup": args.warmup,
            }
            payload.update(hunt(
                args.repo, _cmd(args.cmd), pattern=args.pattern, limit=args.limit,
                min_ratio=args.min_ratio, samples=args.samples, warmup=args.warmup,
                aa_tol=args.aa_tol, aa_rounds=args.aa_rounds, revert_tol=args.revert_tol,
                timeout=args.timeout, py_path=args.py_path,
                progress=lambda m: print("[tagaxis] %s" % m, file=sys.stderr),
            ))
            payload["elapsed_s"] = time.time() - started
            _dump(args.out, payload)
            return 0
    except TagaxisError as exc:
        print("tagaxis: %s" % exc, file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired as exc:
        print("tagaxis: workload timed out after %ss" % exc.timeout, file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
