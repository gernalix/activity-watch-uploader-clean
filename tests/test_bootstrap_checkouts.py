import importlib.util
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "bootstrap_checkouts.py"
SPEC = importlib.util.spec_from_file_location("bootstrap_checkouts", MODULE_PATH)
assert SPEC and SPEC.loader
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


class BootstrapCheckoutsTests(unittest.TestCase):
    def test_github_repo_slug_accepts_https_and_ssh(self):
        self.assertEqual(
            bootstrap.github_repo_slug("https://github.com/example/activity-uploader.git"),
            "example/activity-uploader",
        )
        self.assertEqual(
            bootstrap.github_repo_slug("git@github.com:example/private-activity-data.git"),
            "example/private-activity-data",
        )

    def test_known_nested_candidate_accepts_only_exact_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            canonical = Path(tmp) / "activity-watch-uploader"
            nested = canonical / canonical.name
            (nested / ".git").mkdir(parents=True)
            self.assertEqual(bootstrap.known_nested_candidate(canonical), nested)

            (canonical / "user-file.txt").write_text("keep", encoding="utf-8")
            self.assertIsNone(bootstrap.known_nested_candidate(canonical))

    def test_known_nested_candidate_rejects_normal_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            canonical = Path(tmp) / "activity-watch-uploader"
            (canonical / ".git").mkdir(parents=True)
            nested = canonical / canonical.name
            (nested / ".git").mkdir(parents=True)
            self.assertIsNone(bootstrap.known_nested_candidate(canonical))

    def test_known_nested_candidate_rejects_wrong_child_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            canonical = Path(tmp) / "activity-watch-uploader"
            (canonical / "other" / ".git").mkdir(parents=True)
            self.assertIsNone(bootstrap.known_nested_candidate(canonical))


if __name__ == "__main__":
    unittest.main()
