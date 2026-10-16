#!/usr/bin/env python3
"""Fail-closed bootstrap/sync for the two ActivityWatch repositories.

This helper exists to keep deployment deterministic and to repair one known,
safe nesting failure mode without broad filesystem cleanup.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

CODE_SLUG = os.environ.get("AW_CODE_REPO_SLUG", "gernalix/activity-watch-uploader-clean")
DATA_SLUG = os.environ.get("AW_DATA_REPO_SLUG", "")
BRANCH = "main"
GIT_TIMEOUT_S = 60


class BootstrapError(RuntimeError):
    pass


def github_repo_slug(remote_url: str) -> str | None:
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", remote_url.strip())
    return match.group(1) if match else None


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=15")
    return env


def run_git(path: Path | None, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    command = ["git", *args]
    try:
        return subprocess.run(
            command,
            cwd=path,
            env=_git_env(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=GIT_TIMEOUT_S,
            check=check,
        )
    except subprocess.TimeoutExpired as exc:
        raise BootstrapError(f"git {' '.join(args[:2])} timed out") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "git command failed").strip().splitlines()
        tail = detail[-1] if detail else "git command failed"
        raise BootstrapError(f"git {' '.join(args[:2])} failed: {tail[:300]}") from exc


def origin_slug(path: Path) -> str | None:
    proc = run_git(path, "remote", "get-url", "origin")
    return github_repo_slug(proc.stdout.strip())


def has_head(path: Path) -> bool:
    return run_git(path, "rev-parse", "--verify", "HEAD", check=False).returncode == 0


def branch(path: Path) -> str:
    return run_git(path, "branch", "--show-current").stdout.strip()


def dirty_paths(path: Path) -> list[str]:
    raw = run_git(path, "status", "--porcelain", "-z").stdout
    paths: list[str] = []
    records = raw.split("\0")
    i = 0
    while i < len(records):
        record = records[i]
        i += 1
        if not record:
            continue
        candidate = record[3:] if len(record) >= 4 else record
        paths.append(candidate)
        if record[:2].strip().startswith(("R", "C")) and i < len(records) and records[i]:
            paths.append(records[i])
            i += 1
    return paths


def assert_expected_repo(path: Path, slug: str) -> None:
    if not (path / ".git").is_dir():
        raise BootstrapError(f"{path} is not a Git worktree")
    actual = origin_slug(path)
    if actual != slug:
        raise BootstrapError(f"{path} origin is {actual or 'unrecognized'}, expected {slug}")


def sync_existing_repo(path: Path, slug: str, baseline: str | None) -> dict[str, Any]:
    assert_expected_repo(path, slug)
    dirty = dirty_paths(path)
    if dirty:
        raise BootstrapError(f"{path} has local changes: {', '.join(dirty[:8])}")

    run_git(path, "fetch", "--prune", "origin", BRANCH)
    if has_head(path):
        current_branch = branch(path)
        if current_branch != BRANCH:
            raise BootstrapError(f"{path} is on branch {current_branch!r}, expected {BRANCH!r}")
        run_git(path, "merge", "--ff-only", f"origin/{BRANCH}")
        action = "fast-forwarded"
    else:
        # Safe for an unborn/partial clone after the remote has been validated.
        run_git(path, "checkout", "-B", BRANCH, f"origin/{BRANCH}")
        action = "materialized-head"

    if baseline:
        result = run_git(path, "merge-base", "--is-ancestor", baseline, "HEAD", check=False)
        if result.returncode != 0:
            raise BootstrapError(f"{path} HEAD does not include required baseline {baseline}")

    head = run_git(path, "rev-parse", "HEAD").stdout.strip()
    return {"path": str(path), "repo": slug, "action": action, "head": head}


def clone_repo(path: Path, slug: str, baseline: str | None) -> dict[str, Any]:
    if path.exists():
        raise BootstrapError(f"refusing clone because path already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    run_git(
        path.parent,
        "clone",
        "--branch",
        BRANCH,
        "--single-branch",
        f"https://github.com/{slug}.git",
        path.name,
    )
    return sync_existing_repo(path, slug, baseline) | {"action": "cloned"}


def known_nested_candidate(canonical: Path) -> Path | None:
    """Return the sole same-name nested repo, otherwise None.

    We deliberately do not guess around any other directory shape.
    """
    if not canonical.is_dir() or (canonical / ".git").exists():
        return None
    entries = list(canonical.iterdir())
    nested = canonical / canonical.name
    if len(entries) == 1 and entries[0] == nested and (nested / ".git").is_dir():
        return nested
    return None


def promote_known_nested(canonical: Path, slug: str, baseline: str | None) -> dict[str, Any]:
    nested = known_nested_candidate(canonical)
    if nested is None:
        raise BootstrapError(f"{canonical} is neither a Git worktree nor the known safe nested layout")

    # Validate and complete the nested clone before moving anything.
    result = sync_existing_repo(nested, slug, baseline)
    if dirty_paths(nested):
        raise BootstrapError(f"nested checkout became dirty unexpectedly: {nested}")

    staging = canonical.parent / f".{canonical.name}.bootstrap-promote"
    if staging.exists():
        raise BootstrapError(f"staging path already exists: {staging}")

    nested.rename(staging)
    try:
        canonical.rmdir()  # succeeds only if the outer directory is now truly empty
        staging.rename(canonical)
    except Exception:
        # Best-effort rollback that never deletes user content.
        if staging.exists():
            if canonical.exists():
                rollback_target = canonical / canonical.name
                if not rollback_target.exists():
                    staging.rename(rollback_target)
            else:
                staging.rename(canonical)
        raise

    result.update({"path": str(canonical), "action": "promoted-known-nested"})
    return result


def ensure_repo(
    path: Path,
    slug: str,
    baseline: str | None,
    *,
    allow_known_nested_repair: bool,
) -> dict[str, Any]:
    if not path.exists():
        return clone_repo(path, slug, baseline)
    if (path / ".git").is_dir():
        return sync_existing_repo(path, slug, baseline)
    if allow_known_nested_repair and known_nested_candidate(path) is not None:
        return promote_known_nested(path, slug, baseline)
    raise BootstrapError(f"unsafe/non-canonical existing path: {path}")


def parse_args() -> argparse.Namespace:
    home = Path.home()
    parser = argparse.ArgumentParser()
    parser.add_argument("--code-root", type=Path, default=home / "projects" / "activity-watch-uploader")
    parser.add_argument("--data-root", type=Path, default=home / "projects" / "activity-watch-data")
    parser.add_argument("--code-baseline")
    parser.add_argument("--data-baseline")
    parser.add_argument(
        "--repair-known-nested",
        action="store_true",
        help="allow only the exact same-name nested checkout repair documented by this project",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", DATA_SLUG):
            raise BootstrapError("AW_DATA_REPO_SLUG must identify the private data repository")
        code = ensure_repo(
            args.code_root.expanduser().resolve(),
            CODE_SLUG,
            args.code_baseline,
            allow_known_nested_repair=args.repair_known_nested,
        )
        data = ensure_repo(
            args.data_root.expanduser().resolve(),
            DATA_SLUG,
            args.data_baseline,
            allow_known_nested_repair=False,
        )
        print(json.dumps({"status": "ok", "code": code, "data": data}, sort_keys=True))
        return 0
    except BootstrapError as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
