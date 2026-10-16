import datetime as dt
import os
import pathlib
import tempfile
import unittest
from unittest import mock

import activity_watch_uploader as awu


class ActivityWatchUploaderTests(unittest.TestCase):
    def test_group_events_strips_ids_sorts_and_partitions_utc(self):
        events = [
            {"id": 9, "timestamp": "2026-09-18T00:00:01Z", "duration": 1, "data": {"b": 2}},
            {"id": 8, "timestamp": "2026-09-17T23:59:59+00:00", "duration": 1, "data": {"a": 1}},
        ]
        grouped = awu.group_events_by_utc_day(events)
        self.assertEqual(list(grouped), ["2026-09-18", "2026-09-17"])
        self.assertNotIn("id", grouped["2026-09-18"][0])

    def test_bucket_storage_key_keeps_normal_activitywatch_ids(self):
        self.assertEqual(awu.bucket_storage_key("aw-watcher-window_fedora"), "aw-watcher-window_fedora")
        self.assertEqual(awu.bucket_storage_key("odd/name"), "odd%2Fname")

    def test_github_repo_slug_accepts_https_and_ssh(self):
        self.assertEqual(
            awu.github_repo_slug("https://github.com/example/private-activity-data.git"),
            "example/private-activity-data",
        )
        self.assertEqual(
            awu.github_repo_slug("git@github.com:example/private-activity-data.git"),
            "example/private-activity-data",
        )

    def test_bucket_day_path_is_chatgpt_friendly_and_bounded(self):
        root = pathlib.Path("/tmp/data")
        self.assertEqual(
            awu.bucket_day_path(root, "aw-watcher-window_fedora", "2026-09-18"),
            root / "buckets" / "aw-watcher-window_fedora" / "2026" / "09" / "2026-09-18.jsonl",
        )

    def test_atomic_write_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "x" / "a.json"
            self.assertTrue(awu.atomic_write_if_changed(path, b"one\n"))
            self.assertFalse(awu.atomic_write_if_changed(path, b"one\n"))
            self.assertTrue(awu.atomic_write_if_changed(path, b"two\n"))

    def test_rewrite_bucket_days_deletes_only_targeted_missing_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            old_day = awu.bucket_day_path(root, "bucket", "2026-09-16")
            target_day = awu.bucket_day_path(root, "bucket", "2026-09-17")
            old_day.parent.mkdir(parents=True)
            target_day.parent.mkdir(parents=True, exist_ok=True)
            old_day.write_text("old\n")
            target_day.write_text("old\n")
            changed = awu.rewrite_bucket_days(root, "bucket", {}, prune_days={"2026-09-17", "2026-09-18"})
            self.assertEqual(changed, 1)
            self.assertTrue(old_day.exists())
            self.assertFalse(target_day.exists())

    def test_full_reconcile_due(self):
        config = awu.Config(root=pathlib.Path("."), full_reconcile_hours=168)
        now = dt.datetime(2026, 9, 18, tzinfo=awu.UTC)
        self.assertTrue(awu.should_full_reconcile({}, config, now))
        recent = {"last_full_reconcile_utc": "2026-09-17T00:00:00Z"}
        self.assertFalse(awu.should_full_reconcile(recent, config, now))

    def test_metadata_preserves_historical_buckets(self):
        previous = {
            "buckets": {
                "old-bucket": {"storage_key": "old-bucket", "type": "legacy"}
            }
        }
        current = {"new-bucket": {"type": "current"}}
        metadata = awu.metadata_document(current, previous)
        self.assertFalse(metadata["buckets"]["old-bucket"]["present_in_activitywatch"])
        self.assertTrue(metadata["buckets"]["new-bucket"]["present_in_activitywatch"])

    def test_known_bucket_ids_reads_metadata(self):
        self.assertEqual(
            awu.known_bucket_ids({"buckets": {"a": {}, "b": {}}}),
            {"a", "b"},
        )


    def test_full_reconcile_summary_distinguishes_unknown_hostname_from_absent(self):
        completed = dt.datetime(2026, 9, 18, 17, 0, tzinfo=awu.UTC)
        exported = {
            "aw-stopwatch": {
                "hostname": "unknown",
                "type": "general.stopwatch",
                "events": [],
            },
            "aw-watcher-window_fedora": {
                "hostname": "fedora",
                "type": "currentwindow",
                "events": [
                    {
                        "timestamp": "2026-09-18T16:00:00Z",
                        "duration": 1.5,
                        "data": {"app": "x"},
                    }
                ],
            },
        }
        summary = awu.full_reconcile_summary(exported, completed)
        self.assertEqual(summary["bucket_count"], 2)
        self.assertEqual(summary["event_count"], 1)
        self.assertEqual(summary["unknown_hostname_bucket_ids"], ["aw-stopwatch"])
        self.assertEqual(summary["empty_bucket_ids"], ["aw-stopwatch"])
        self.assertEqual(
            summary["buckets"]["aw-watcher-window_fedora"]["first_event_utc"],
            "2026-09-18T16:00:00Z",
        )

    def test_canonical_export_events_rejects_malformed_event(self):
        with self.assertRaises(awu.SyncError):
            awu.canonical_export_events(
                "bucket-a",
                {"events": [{"timestamp": "2026-09-18T16:00:00Z", "duration": 1, "data": {}}, "bad"]},
            )

    def test_verify_full_reconcile_artifacts_matches_metadata_and_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            metadata = {
                "buckets": {
                    "active": {"present_in_activitywatch": True},
                    "empty": {"present_in_activitywatch": True},
                    "historical": {"present_in_activitywatch": False},
                }
            }
            summary = {
                "format": "activitywatch-full-reconcile-v1",
                "bucket_count": 2,
                "event_count": 1,
                "bucket_ids": ["active", "empty"],
                "unknown_hostname_bucket_ids": ["empty"],
                "empty_bucket_ids": ["empty"],
                "buckets": {
                    "active": {"event_count": 1, "hostname": "fedora"},
                    "empty": {"event_count": 0, "hostname": "unknown"},
                },
            }
            (root / "metadata").mkdir()
            (root / "metadata" / "buckets.json").write_bytes(awu.json_bytes(metadata, pretty=True))
            (root / "metadata" / "full-reconcile.json").write_bytes(awu.json_bytes(summary, pretty=True))
            data_path = awu.bucket_day_path(root, "active", "2026-09-18")
            data_path.parent.mkdir(parents=True)
            data_path.write_text("{}\n")

            result = awu.verify_full_reconcile_artifacts(root)
            self.assertEqual(result, {"bucket_count": 2, "event_count": 1})

            summary["bucket_ids"].append("ghost")
            (root / "metadata" / "full-reconcile.json").write_bytes(awu.json_bytes(summary, pretty=True))
            with self.assertRaises(awu.SyncError):
                awu.verify_full_reconcile_artifacts(root)

    def test_export_all_unwraps_activitywatch_payload(self):
        api = awu.ActivityWatchAPI(awu.Config(root=pathlib.Path(".")))
        api.get_json = lambda *args, **kwargs: {
            "buckets": {"bucket-a": {"id": "bucket-a", "events": []}}
        }
        self.assertEqual(api.export_all()["bucket-a"]["id"], "bucket-a")



if __name__ == "__main__":
    unittest.main()
