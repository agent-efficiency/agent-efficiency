"""Closed opt-in result summaries. No arbitrary output or labels enter receipts."""

from __future__ import annotations

import json
import os
import stat

COUNTS = (
    "planned",
    "passed",
    "failed",
    "baseline_failed",
    "setup_errors",
    "survived",
    "timed_out",
    "not_run",
)
MAX_BYTES = 65536


def classify_summary(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"version", *COUNTS}
        or type(value["version"]) is not int
        or value["version"] != 1
        or any(
            type(value[k]) is not int or not 0 <= value[k] <= 1000000000 for k in COUNTS
        )
        or value["planned"] < 1
        or sum(value[k] for k in COUNTS[1:]) != value["planned"]
    ):
        raise ValueError("invalid structured verification summary")
    for key, result, reason in (
        ("timed_out", "blocked", "structured_timeout"),
        ("baseline_failed", "inconclusive", "structured_baseline_failed"),
        ("setup_errors", "inconclusive", "structured_setup_error"),
        ("not_run", "inconclusive", "structured_incomplete"),
        ("survived", "fail", "structured_survived"),
        ("failed", "fail", "structured_failed"),
    ):
        if value[key]:
            return result, reason
    return "pass", None


def signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def file_signature(path):
    try:
        return signature(os.stat(path, follow_symlinks=False))
    except OSError:
        return None


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate structured verification field")
        result[key] = value
    return result


def read_summary(path, previous):
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size > MAX_BYTES
            or signature(info) == previous
        ):
            raise ValueError(
                "structured verification summary is unavailable or unchanged"
            )
        data = os.read(descriptor, MAX_BYTES + 1)
        if len(data) > MAX_BYTES or signature(os.fstat(descriptor)) != signature(info):
            raise ValueError("structured verification summary changed while reading")
    finally:
        os.close(descriptor)
    try:
        value = json.loads(data, object_pairs_hook=_unique)
        classify_summary(value)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("invalid structured verification summary") from exc
    return value
