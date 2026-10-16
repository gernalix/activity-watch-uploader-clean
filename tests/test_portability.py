import os, tempfile, unittest
from unittest.mock import patch
from pathlib import Path
from activity_watch_uploader import Config, SyncError
class PortabilityTests(unittest.TestCase):
    def test_missing_data_identity_refuses_before_any_io(self):
        with patch.dict(os.environ,{},clear=True):
            with self.assertRaises(SyncError):Config.from_env()
    def test_private_configuration_preserves_exact_remote_guard(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ,{'AW_DATA_REPO_SLUG':'example/private-activity-data'},clear=True):
            config=Config.from_env(Path(temporary))
        self.assertEqual(config.expected_data_repo,'example/private-activity-data')
