#!/usr/bin/env python3
"""Mirror ActivityWatch raw buckets into Git-friendly daily JSONL files."""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable

UTC = dt.timezone.utc
MANAGED_PREFIXES = ("buckets/", "metadata/")
class SyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    # root is the dedicated data repository, not the uploader code repository.
    root: pathlib.Path
    runtime_root: pathlib.Path | None = None
    aw_base_url: str = "http://127.0.0.1:5600"
    refresh_days: int = 2
    full_reconcile_hours: int = 168
    request_timeout_s: int = 20
    export_timeout_s: int = 300
    request_attempts: int = 3
    git_timeout_s: int = 120
    git_remote: str = "origin"
    git_branch: str = "main"
    expected_data_repo: str = ""

    @classmethod
    def from_env(cls, root: pathlib.Path | None = None) -> "Config":
        expected_repo = os.getenv("AW_DATA_REPO_SLUG", "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", expected_repo):
            raise SyncError("AW_DATA_REPO_SLUG must explicitly identify the private data repository")
        code_root = pathlib.Path(__file__).resolve().parent
        data_root = root or pathlib.Path(
            os.path.expandvars(
                os.getenv("AW_DATA_REPO_PATH", str(pathlib.Path.home() / "projects" / "activity-watch-data"))
            )
        ).expanduser()
        runtime_root = pathlib.Path(
            os.path.expandvars(os.getenv("AW_RUNTIME_DIR", str(code_root / ".runtime")))
        ).expanduser()
        return cls(
            root=data_root.resolve(),
            runtime_root=runtime_root.resolve(),
            aw_base_url=os.getenv("AW_BASE_URL", "http://127.0.0.1:5600").rstrip("/"),
            refresh_days=max(1, int(os.getenv("AW_REFRESH_DAYS", "2"))),
            full_reconcile_hours=max(1, int(os.getenv("AW_FULL_RECONCILE_HOURS", "168"))),
            request_timeout_s=max(1, int(os.getenv("AW_REQUEST_TIMEOUT_S", "20"))),
            export_timeout_s=max(30, int(os.getenv("AW_EXPORT_TIMEOUT_S", "300"))),
            request_attempts=max(1, int(os.getenv("AW_REQUEST_ATTEMPTS", "3"))),
            git_timeout_s=max(10, int(os.getenv("GIT_TIMEOUT_S", "120"))),
            git_remote=os.getenv("GIT_REMOTE", "origin"),
            git_branch=os.getenv("GIT_BRANCH", "main"),
            expected_data_repo=expected_repo,
        )


def utc_now() -> dt.datetime:
    return dt.datetime.now(UTC)


def parse_timestamp(value: str) -> dt.datetime:
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def iso_z(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def bucket_storage_key(bucket_id: str) -> str:
    return urllib.parse.quote(bucket_id, safe="._-")


def github_repo_slug(remote_url: str) -> str | None:
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", remote_url.strip())
    return match.group(1) if match else None


def canonical_event(event: dict[str, Any]) -> dict[str, Any]:
    # ActivityWatch's canonical export intentionally scrubs database event IDs.
    out = {k: v for k, v in event.items() if k != "id"}
    if not isinstance(out.get("timestamp"), str):
        raise SyncError("ActivityWatch event is missing a string timestamp")
    if "duration" not in out or "data" not in out:
        raise SyncError("ActivityWatch event is missing duration or data")
    return out


def group_events_by_utc_day(events: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for raw in events:
        event = canonical_event(raw)
        day = parse_timestamp(event["timestamp"]).date().isoformat()
        grouped.setdefault(day, []).append(event)
    for day_events in grouped.values():
        day_events.sort(
            key=lambda e: (
                parse_timestamp(e["timestamp"]),
                float(e.get("duration", 0.0)),
                json.dumps(e.get("data", {}), sort_keys=True, ensure_ascii=False, separators=(",", ":")),
            )
        )
    return grouped


def json_bytes(value: Any, *, pretty: bool = False) -> bytes:
    if pretty:
        text_value = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    else:
        text_value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    return text_value.encode("utf-8")


def jsonl_bytes(events: Iterable[dict[str, Any]]) -> bytes:
    return b"".join(json_bytes(event) for event in events)


def atomic_write_if_changed(path: pathlib.Path, content: bytes) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.read_bytes() == content:
            return False
    except FileNotFoundError:
        pass
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp_path = pathlib.Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        return True
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()


def read_json(path: pathlib.Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_state(path: pathlib.Path, state: dict[str, Any]) -> None:
    atomic_write_if_changed(path, json_bytes(state, pretty=True))


class ActivityWatchAPI:
    def __init__(self, config: Config):
        self.config = config

    def get_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_s: int | None = None,
    ) -> Any:
        url = f"{self.config.aw_base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={"User-Agent": "activity-watch-uploader/2"})
        last_exc: Exception | None = None
        for attempt in range(1, self.config.request_attempts + 1):
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=timeout_s or self.config.request_timeout_s,
                ) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                if exc.code < 500 and exc.code != 429:
                    raise SyncError(f"ActivityWatch HTTP {exc.code} for {path}") from exc
                last_exc = exc
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                last_exc = exc
            if attempt < self.config.request_attempts:
                time.sleep(min(2 ** (attempt - 1), 5))
        raise SyncError(f"ActivityWatch request failed for {path}: {type(last_exc).__name__}") from last_exc

    def buckets(self) -> dict[str, dict[str, Any]]:
        value = self.get_json("/api/0/buckets")
        if not isinstance(value, dict):
            raise SyncError("ActivityWatch bucket list has unexpected shape")
        return {str(k): dict(v) for k, v in value.items() if isinstance(v, dict)}

    def export_all(self) -> dict[str, dict[str, Any]]:
        # The REST endpoint wraps the canonical export in {"buckets": {...}}.
        value = self.get_json("/api/0/export", timeout_s=self.config.export_timeout_s)
        if not isinstance(value, dict) or not isinstance(value.get("buckets"), dict):
            raise SyncError("ActivityWatch export has unexpected shape")
        result: dict[str, dict[str, Any]] = {}
        for bucket_id, bucket in value["buckets"].items():
            if isinstance(bucket, dict):
                result[str(bucket_id)] = dict(bucket)
        return result

    def events(self, bucket_id: str, start: dt.datetime, end: dt.datetime) -> list[dict[str, Any]]:
        escaped = urllib.parse.quote(bucket_id, safe="")
        value = self.get_json(
            f"/api/0/buckets/{escaped}/events",
            {"start": iso_z(start), "end": iso_z(end), "limit": -1},
        )
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise SyncError(f"ActivityWatch events for {bucket_id!r} have unexpected shape")
        return [dict(item) for item in value]


class GitRepo:
    def __init__(self, config: Config):
        self.config = config
        self.env = os.environ.copy()
        self.env["GIT_TERMINAL_PROMPT"] = "0"
        self.env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=15")

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["git", *args],
                cwd=self.config.root,
                env=self.env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.config.git_timeout_s,
                check=check,
            )
        except subprocess.TimeoutExpired as exc:
            raise SyncError(f"git {' '.join(args[:2])} timed out") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "git command failed").strip().splitlines()[-1]
            raise SyncError(f"git {' '.join(args[:2])} failed: {detail[:300]}") from exc

    def ensure_branch(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.config.expected_data_repo):
            raise SyncError("explicit private data repository identity required")
        if not (self.config.root / ".git").exists():
            raise SyncError(f"Not a git repository: {self.config.root}")
        branch = self.run("branch", "--show-current").stdout.strip()
        if branch != self.config.git_branch:
            raise SyncError(f"Expected branch {self.config.git_branch!r}, found {branch!r}")
        remote_url = self.run("remote", "get-url", self.config.git_remote).stdout.strip()
        actual_repo = github_repo_slug(remote_url)
        if actual_repo != self.config.expected_data_repo:
            raise SyncError(
                f"Refusing to write ActivityWatch data to {actual_repo or 'unknown remote'}; "
                f"expected {self.config.expected_data_repo}"
            )

    def status_paths(self) -> list[str]:
        raw = self.run("status", "--porcelain", "-z").stdout
        paths: list[str] = []
        records = raw.split("\0")
        i = 0
        while i < len(records):
            record = records[i]
            i += 1
            if not record:
                continue
            path = record[3:] if len(record) >= 4 else record
            # Renames/copies use a second NUL-delimited path; either is outside managed data in normal operation.
            if record[:2].strip().startswith(("R", "C")) and i < len(records) and records[i]:
                paths.append(records[i])
                i += 1
            paths.append(path)
        return paths

    def assert_only_managed_dirty(self) -> None:
        unexpected = [p for p in self.status_paths() if not p.startswith(MANAGED_PREFIXES)]
        if unexpected:
            raise SyncError("Refusing to touch repository with unrelated local changes: " + ", ".join(unexpected[:8]))

    def stage_managed(self) -> None:
        self.run("add", "--", "buckets", "metadata")

    def staged_changes(self) -> bool:
        return self.run("diff", "--cached", "--quiet", check=False).returncode != 0

    def commit_managed(self, message: str) -> bool:
        self.stage_managed()
        if not self.staged_changes():
            return False
        self.run("commit", "-m", message)
        return True

    def ahead_behind(self) -> tuple[int, int]:
        proc = self.run(
            "rev-list", "--left-right", "--count",
            f"HEAD...{self.config.git_remote}/{self.config.git_branch}",
        )
        parts = proc.stdout.strip().split()
        if len(parts) != 2:
            raise SyncError("Could not determine git ahead/behind state")
        return int(parts[0]), int(parts[1])

    def fetch(self) -> None:
        self.run("fetch", "--prune", self.config.git_remote, self.config.git_branch)

    def rebase_onto_remote(self) -> None:
        try:
            self.run("rebase", f"{self.config.git_remote}/{self.config.git_branch}")
        except SyncError:
            self.run("rebase", "--abort", check=False)
            raise

    def push(self) -> None:
        self.run("push", self.config.git_remote, f"HEAD:{self.config.git_branch}")

    def reconcile_before_sync(self) -> None:
        self.ensure_branch()
        self.assert_only_managed_dirty()
        # A prior crash may have left generated data uncommitted. Preserve it before touching refs.
        if self.status_paths():
            self.commit_managed(f"ActivityWatch recovery {iso_z(utc_now())}")
        self.fetch()
        ahead, behind = self.ahead_behind()
        if behind:
            if ahead:
                self.rebase_onto_remote()
            else:
                self.run("merge", "--ff-only", f"{self.config.git_remote}/{self.config.git_branch}")
        ahead, _ = self.ahead_behind()
        if ahead:
            self.push()

    def commit_and_push(self, message: str) -> bool:
        changed = self.commit_managed(message)
        self.fetch()
        ahead, behind = self.ahead_behind()
        if behind:
            self.rebase_onto_remote()
            ahead, _ = self.ahead_behind()
        if ahead:
            try:
                self.push()
            except SyncError:
                # One bounded retry handles a remote commit racing this run.
                self.fetch()
                self.rebase_onto_remote()
                self.push()
        return changed


def metadata_document(
    buckets: dict[str, dict[str, Any]],
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Keep metadata for buckets that disappeared from ActivityWatch so the Git
    # archive remains self-describing for all historically collected data.
    normalized: dict[str, dict[str, Any]] = {}
    previous_buckets = previous.get("buckets", {}) if isinstance(previous, dict) else {}
    if isinstance(previous_buckets, dict):
        for bucket_id, raw in previous_buckets.items():
            if isinstance(raw, dict):
                item = dict(raw)
                item["present_in_activitywatch"] = False
                normalized[str(bucket_id)] = item

    for bucket_id in sorted(buckets):
        item = dict(buckets[bucket_id])
        item.pop("events", None)
        item["storage_key"] = bucket_storage_key(bucket_id)
        item["present_in_activitywatch"] = True
        normalized[bucket_id] = item

    return {
        "format": "activitywatch-jsonl-v1",
        "partition_timezone": "UTC",
        "data_path_pattern": "buckets/<bucket-id>/YYYY/MM/YYYY-MM-DD.jsonl",
        "buckets": dict(sorted(normalized.items())),
    }


def bucket_day_path(root: pathlib.Path, bucket_id: str, day: str) -> pathlib.Path:
    return root / "buckets" / bucket_storage_key(bucket_id) / day[:4] / day[5:7] / f"{day}.jsonl"


def canonical_export_events(bucket_id: str, bucket: dict[str, Any]) -> list[dict[str, Any]]:
    raw_events = bucket.get("events", [])
    if not isinstance(raw_events, list) or not all(isinstance(event, dict) for event in raw_events):
        raise SyncError(f"Exported events for {bucket_id!r} have unexpected shape")
    return [canonical_event(event) for event in raw_events]


def full_reconcile_summary(
    exported: dict[str, dict[str, Any]],
    completed_at: dt.datetime,
) -> dict[str, Any]:
    """Compact, non-sensitive proof of what the last full export contained."""
    bucket_summaries: dict[str, dict[str, Any]] = {}
    total_events = 0
    unknown_hostname_bucket_ids: list[str] = []
    empty_bucket_ids: list[str] = []

    for bucket_id in sorted(exported):
        bucket = exported[bucket_id]
        events = canonical_export_events(bucket_id, bucket)
        total_events += len(events)
        if str(bucket.get("hostname") or "") == "unknown":
            unknown_hostname_bucket_ids.append(bucket_id)
        if not events:
            empty_bucket_ids.append(bucket_id)

        timestamps = [parse_timestamp(event["timestamp"]) for event in events]
        bucket_summaries[bucket_id] = {
            "event_count": len(events),
            "first_event_utc": iso_z(min(timestamps)) if timestamps else None,
            "last_event_utc": iso_z(max(timestamps)) if timestamps else None,
            "hostname": bucket.get("hostname"),
            "type": bucket.get("type"),
        }

    return {
        "format": "activitywatch-full-reconcile-v1",
        "completed_at_utc": iso_z(completed_at),
        "bucket_count": len(exported),
        "event_count": total_events,
        "bucket_ids": sorted(exported),
        "unknown_hostname_bucket_ids": unknown_hostname_bucket_ids,
        "empty_bucket_ids": empty_bucket_ids,
        "buckets": bucket_summaries,
    }


def rewrite_bucket_days(root: pathlib.Path, bucket_id: str, grouped: dict[str, list[dict[str, Any]]], *, prune_days: set[str] | None = None) -> int:
    changed = 0
    if prune_days is not None:
        for day in prune_days:
            path = bucket_day_path(root, bucket_id, day)
            if day not in grouped and path.exists():
                path.unlink()
                changed += 1
    for day, events in grouped.items():
        if atomic_write_if_changed(bucket_day_path(root, bucket_id, day), jsonl_bytes(events)):
            changed += 1
    return changed


def full_reconcile(
    config: Config,
    api: ActivityWatchAPI,
    buckets: dict[str, dict[str, Any]],
    now: dt.datetime,
) -> int:
    exported = api.export_all()
    if set(exported) != set(buckets):
        missing = sorted(set(buckets) - set(exported))
        extra = sorted(set(exported) - set(buckets))
        raise SyncError(
            f"ActivityWatch bucket/export mismatch: missing={missing!r} extra={extra!r}"
        )
    changed = 0
    for bucket_id, bucket in exported.items():
        events = canonical_export_events(bucket_id, bucket)
        grouped = group_events_by_utc_day(events)
        bucket_dir = config.root / "buckets" / bucket_storage_key(bucket_id)
        existing_days = {p.stem for p in bucket_dir.rglob("*.jsonl")} if bucket_dir.exists() else set()
        # Full reconcile mirrors per-day contents for buckets still present in ActivityWatch.
        changed += rewrite_bucket_days(config.root, bucket_id, grouped, prune_days=existing_days | set(grouped))
    # Do not delete an entire bucket directory if the source bucket disappears: Git remains an archive.
    metadata_path = config.root / "metadata" / "buckets.json"
    previous_metadata = read_json(metadata_path, {})
    if atomic_write_if_changed(
        metadata_path,
        json_bytes(metadata_document(buckets, previous_metadata), pretty=True),
    ):
        changed += 1

    summary_path = config.root / "metadata" / "full-reconcile.json"
    if atomic_write_if_changed(
        summary_path,
        json_bytes(full_reconcile_summary(exported, now), pretty=True),
    ):
        changed += 1
    return changed


def verify_full_reconcile_artifacts(root: pathlib.Path) -> dict[str, int]:
    """Fail closed if the compact full-reconcile proof disagrees with the mirror."""
    metadata = read_json(root / "metadata" / "buckets.json", None)
    summary = read_json(root / "metadata" / "full-reconcile.json", None)
    if not isinstance(metadata, dict):
        raise SyncError("metadata/buckets.json is missing or invalid")
    if not isinstance(summary, dict):
        raise SyncError("metadata/full-reconcile.json is missing or invalid")
    if summary.get("format") != "activitywatch-full-reconcile-v1":
        raise SyncError("full reconcile summary has unexpected format")

    metadata_buckets = metadata.get("buckets")
    summary_buckets = summary.get("buckets")
    summary_ids = summary.get("bucket_ids")
    if not isinstance(metadata_buckets, dict) or not isinstance(summary_buckets, dict) or not isinstance(summary_ids, list):
        raise SyncError("full reconcile metadata has unexpected shape")

    present_ids = sorted(
        str(bucket_id)
        for bucket_id, item in metadata_buckets.items()
        if isinstance(item, dict) and item.get("present_in_activitywatch") is True
    )
    normalized_summary_ids = sorted(str(bucket_id) for bucket_id in summary_ids)
    if normalized_summary_ids != present_ids:
        raise SyncError(
            f"full reconcile bucket mismatch: summary={normalized_summary_ids!r} metadata={present_ids!r}"
        )
    if sorted(str(bucket_id) for bucket_id in summary_buckets) != normalized_summary_ids:
        raise SyncError("full reconcile per-bucket summary does not match bucket_ids")

    unknown_hostname_ids: list[str] = []
    empty_ids: list[str] = []
    total_events = 0
    for bucket_id in normalized_summary_ids:
        item = summary_buckets.get(bucket_id)
        if not isinstance(item, dict):
            raise SyncError(f"full reconcile bucket {bucket_id!r} has invalid summary")
        event_count = item.get("event_count")
        if not isinstance(event_count, int) or isinstance(event_count, bool) or event_count < 0:
            raise SyncError(f"full reconcile bucket {bucket_id!r} has invalid event_count")
        total_events += event_count
        if str(item.get("hostname") or "") == "unknown":
            unknown_hostname_ids.append(bucket_id)
        if event_count == 0:
            empty_ids.append(bucket_id)
        else:
            bucket_dir = root / "buckets" / bucket_storage_key(bucket_id)
            if not bucket_dir.exists() or not any(bucket_dir.rglob("*.jsonl")):
                raise SyncError(f"full reconcile bucket {bucket_id!r} has events but no JSONL files")

    if summary.get("bucket_count") != len(normalized_summary_ids):
        raise SyncError("full reconcile bucket_count does not match bucket_ids")
    if summary.get("event_count") != total_events:
        raise SyncError("full reconcile event_count does not match per-bucket counts")
    if sorted(str(value) for value in summary.get("unknown_hostname_bucket_ids", [])) != unknown_hostname_ids:
        raise SyncError("full reconcile unknown-hostname list is inconsistent")
    if sorted(str(value) for value in summary.get("empty_bucket_ids", [])) != empty_ids:
        raise SyncError("full reconcile empty-bucket list is inconsistent")

    return {"bucket_count": len(normalized_summary_ids), "event_count": total_events}


def incremental_reconcile(config: Config, api: ActivityWatchAPI, buckets: dict[str, dict[str, Any]], now: dt.datetime) -> int:
    today = now.astimezone(UTC).date()
    start_day = today - dt.timedelta(days=config.refresh_days - 1)
    start = dt.datetime.combine(start_day, dt.time.min, tzinfo=UTC)
    end = dt.datetime.combine(today + dt.timedelta(days=1), dt.time.min, tzinfo=UTC)
    target_days = {(start_day + dt.timedelta(days=i)).isoformat() for i in range(config.refresh_days)}
    changed = 0
    for bucket_id in sorted(buckets):
        grouped = group_events_by_utc_day(api.events(bucket_id, start, end))
        # Limit writes/deletes to the bounded rolling window.
        grouped = {day: events for day, events in grouped.items() if day in target_days}
        changed += rewrite_bucket_days(config.root, bucket_id, grouped, prune_days=target_days)
    metadata_path = config.root / "metadata" / "buckets.json"
    previous_metadata = read_json(metadata_path, {})
    if atomic_write_if_changed(
        metadata_path,
        json_bytes(metadata_document(buckets, previous_metadata), pretty=True),
    ):
        changed += 1
    return changed


def should_full_reconcile(state: dict[str, Any], config: Config, now: dt.datetime) -> bool:
    raw = state.get("last_full_reconcile_utc")
    if not isinstance(raw, str):
        return True
    try:
        previous = parse_timestamp(raw)
    except (ValueError, TypeError):
        return True
    return now - previous >= dt.timedelta(hours=config.full_reconcile_hours)


def known_bucket_ids(metadata: dict[str, Any]) -> set[str]:
    buckets = metadata.get("buckets", {}) if isinstance(metadata, dict) else {}
    return {str(bucket_id) for bucket_id in buckets} if isinstance(buckets, dict) else set()


def run_sync(config: Config) -> tuple[str, int]:
    runtime = config.runtime_root or (config.root / ".runtime")
    runtime.mkdir(parents=True, exist_ok=True)
    lock_path = runtime / "sync.lock"
    state_path = runtime / "state.json"
    with lock_path.open("a+") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "already-running", 0

        git = GitRepo(config)
        git.reconcile_before_sync()
        api = ActivityWatchAPI(config)
        buckets = api.buckets()
        now = utc_now()
        state = read_json(state_path, {})
        if not isinstance(state, dict):
            state = {}

        metadata_path = config.root / "metadata" / "buckets.json"
        previous_metadata = read_json(metadata_path, {})
        new_bucket_ids = set(buckets) - known_bucket_ids(previous_metadata)
        full_summary_missing = not (config.root / "metadata" / "full-reconcile.json").is_file()

        if should_full_reconcile(state, config, now) or new_bucket_ids or full_summary_missing:
            mode = "full"
            changed_files = full_reconcile(config, api, buckets, now)
            verify_full_reconcile_artifacts(config.root)
            state["last_full_reconcile_utc"] = iso_z(now)
        else:
            mode = "incremental"
            changed_files = incremental_reconcile(config, api, buckets, now)
        state["last_successful_data_sync_utc"] = iso_z(now)
        write_state(state_path, state)

        committed = git.commit_and_push(f"ActivityWatch sync {iso_z(now)}")
        return mode + ("+commit" if committed else "+nochange"), changed_files


def main() -> int:
    started = time.monotonic()
    run_id = utc_now().strftime("%Y%m%dT%H%M%SZ")
    config = Config.from_env()
    if sys.argv[1:]:
        if sys.argv[1:] != ["--verify-mirror"]:
            print("usage: activity_watch_uploader.py [--verify-mirror]", file=sys.stderr)
            return 2
        try:
            result = verify_full_reconcile_artifacts(config.root)
        except Exception as exc:
            print(f"VERIFY_ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        print(f"VERIFY_OK bucket_count={result['bucket_count']} event_count={result['event_count']}")
        return 0
    try:
        mode, changed_files = run_sync(config)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        print(f"OK run_id={run_id} mode={mode} changed_files={changed_files} elapsed_ms={elapsed_ms}")
        return 0
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        message = f"run_id={run_id}; {type(exc).__name__}: {exc}"
        print(f"ERROR {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
