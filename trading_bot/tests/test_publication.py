import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import Settings, load_settings
from t_invest_client import SandboxBroker
from t_tech.invest import _error_hub


class PublicationSafetyTests(unittest.TestCase):
    def test_sdk_telemetry_is_disabled_before_client_entry(self):
        previous = _error_hub.ERROR_HUB_DSN
        try:
            context = MagicMock()
            context.__enter__.side_effect = (
                lambda: self.assertEqual(_error_hub.ERROR_HUB_DSN, "") or MagicMock()
            )
            with patch("t_invest_client.Client", return_value=context):
                with SandboxBroker(Settings("synthetic-test-token")):
                    with patch("sentry_sdk.init") as sentry_init:
                        _error_hub.init_error_hub(SimpleNamespace(_target="sandbox"))
                        self.assertEqual(sentry_init.call_args.kwargs["dsn"], "")
        finally:
            _error_hub.ERROR_HUB_DSN = previous

    def test_secret_file_config_and_safe_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secret"
            path.write_text("T_INVEST_TOKEN=synthetic-test-token\nSANDBOX=true\n")
            with patch.dict(os.environ, {"TRADING_ENV_FILE": str(path)}, clear=True):
                settings = load_settings()
                self.assertFalse(settings.enable_sandbox_orders)
                self.assertFalse(settings.enable_auto_trading)
                self.assertNotIn(settings.token, repr(settings))

    def test_source_export_excludes_local_runtime(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
        from check_publication import source_files

        files = source_files()
        for path in files:
            self.assertNotEqual(path.name, ".env")
            self.assertFalse(
                any(
                    part in path.parts
                    for part in [".runtime", "reports", "logs", ".venv", "__pycache__"]
                )
            )
            self.assertNotIn(".sqlite3", path.name)

    def test_publication_check_without_git(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
        from check_publication import check

        with patch("check_publication.subprocess.run", side_effect=FileNotFoundError):
            files, problems = check()
        self.assertTrue(files)
        self.assertEqual(problems, [])
