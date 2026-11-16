"""tagaxis - cross-version performance regression localization with revert proof."""

from .core import (  # noqa: F401
    TagaxisError,
    Worktree,
    aa_control,
    bisect,
    commit_subject,
    commits_between,
    detect_steps,
    hunt,
    is_ancestor,
    list_tags,
    measure,
    measure_in,
    rev_parse,
    revert_and_measure,
    run_once,
    summarize,
    trajectory,
)

__version__ = "0.1.0"

__all__ = [
    "TagaxisError",
    "Worktree",
    "aa_control",
    "bisect",
    "commit_subject",
    "commits_between",
    "detect_steps",
    "hunt",
    "is_ancestor",
    "list_tags",
    "measure",
    "measure_in",
    "rev_parse",
    "revert_and_measure",
    "run_once",
    "summarize",
    "trajectory",
    "__version__",
]
