import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard import Dashboard, make_handler, read_logs
from config import Settings


class DashboardHTTPTests(unittest.TestCase):
    def setUp(self):
        self.app = Dashboard()
        self.app.start = MagicMock()
        self.app.stop = MagicMock()
        self.app.status = MagicMock(return_value={"connected": False, "logs": []})
        self.server = HTTPServer(("127.0.0.1", 0), make_handler(self.app, 0))
        port = self.server.server_address[1]
        self.server.RequestHandlerClass = make_handler(self.app, port)
        self.base = f"http://127.0.0.1:{port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, path, body=None, **headers):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_read_only_api_and_html(self):
        status, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("Пульт наблюдения", body.decode())
        self.assertNotIn("__CONTROL_KEY__", body.decode())
        status, body = self.request("/api/status")
        self.assertEqual(status, 200)
        self.assertNotIn(self.app.key, body.decode())
        self.assertEqual(self.request("/../.env")[0], 404)

    def test_panel_assets_are_served_without_exposing_arbitrary_files(self):
        for name in ["panel.css", "panel.js"]:
            status, body = self.request("/assets/" + name)
            self.assertEqual(status, 200)
            self.assertTrue(body)
        self.assertEqual(self.request("/assets/../.env")[0], 404)
        self.assertEqual(self.request("/assets/config.py")[0], 404)

    def test_controls_reject_foreign_origin_missing_key_and_host(self):
        cases = [
            {},
            {"X-Control-Key": self.app.key, "Origin": "https://example.com"},
            {
                "X-Control-Key": self.app.key,
                "Origin": self.base,
                "Host": "evil.example",
            },
        ]
        for headers in cases:
            with self.subTest(headers=headers):
                self.assertEqual(
                    self.request("/api/start", {"mode": "execute"}, **headers)[0], 403
                )
        self.app.start.assert_not_called()

    def test_authorized_control_and_mode_validation(self):
        headers = {"X-Control-Key": self.app.key, "Origin": self.base}
        self.assertEqual(
            self.request("/api/start", {"mode": "observe"}, **headers)[0], 200
        )
        self.app.start.assert_called_once_with(False)
        self.assertEqual(
            self.request("/api/start", {"mode": "invalid"}, **headers)[0], 400
        )
        self.assertEqual(self.request("/api/stop", {}, **headers)[0], 200)
        self.app.stop.assert_called_once()

    def test_backtest_returns_job_id_and_preserves_options(self):
        self.app.start_backtest = MagicMock(return_value="job123")
        options = {
            "from_date": "2026-09-01",
            "to_date": "2026-09-30",
            "fast": 20,
            "slow": 60,
        }
        status, body = self.request(
            "/api/backtest",
            options,
            **{"X-Control-Key": self.app.key, "Origin": self.base},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["job_id"], "job123")
        self.app.start_backtest.assert_called_once_with(options)

    def test_profile_selection_uses_existing_validated_profile(self):
        headers = {"X-Control-Key": self.app.key, "Origin": self.base}
        self.assertEqual(
            self.request("/api/profile/select", {"id": "trend_sber"}, **headers)[0], 200
        )
        self.assertEqual(self.app.profile.id, "trend_sber")
        self.assertFalse(self.app.data["connected"])
        self.assertEqual(
            self.request("/api/profile/select", {"id": "../invalid"}, **headers)[0], 400
        )
        self.assertEqual(self.app.profile.id, "trend_sber")

    def test_paper_start_needs_current_selected_instrument_data(self):
        headers = {"X-Control-Key": self.app.key, "Origin": self.base}
        self.assertEqual(
            self.request("/api/start", {"mode": "paper"}, **headers)[0], 400
        )
        self.assertFalse(self.app.paper.running)
        self.app.start.assert_not_called()

    def test_session_refresh_and_foreign_origin_rejection(self):
        status, body = self.request("/api/control-session")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["key"], self.app.key)
        self.assertEqual(
            self.request("/api/control-session", Origin="https://evil.example")[0], 403
        )
        self.assertEqual(
            self.request("/api/control-session", **{"Sec-Fetch-Site": "cross-site"})[0],
            403,
        )
        self.assertEqual(
            self.request(
                "/api/start", [], **{"X-Control-Key": self.app.key, "Origin": self.base}
            )[0],
            400,
        )

    def test_controls_recover_after_panel_key_rotation(self):
        previous = self.app.key
        self.app.key = "new-session-key"
        self.assertEqual(
            self.request(
                "/api/start",
                {"mode": "observe"},
                **{"X-Control-Key": previous, "Origin": self.base},
            )[0],
            403,
        )
        status, body = self.request("/api/control-session")
        key = json.loads(body)["key"]
        self.assertEqual(
            self.request(
                "/api/start",
                {"mode": "observe"},
                **{"X-Control-Key": key, "Origin": self.base},
            )[0],
            200,
        )
        self.app.start.assert_called_once_with(False)

    def test_execution_flags_checked_before_process_creation(self):
        app = Dashboard()
        with patch("dashboard.load_settings", return_value=Settings("fake")), patch(
            "dashboard.subprocess.Popen"
        ) as popen:
            with self.assertRaises(RuntimeError):
                app.start(True)
            popen.assert_not_called()

    def test_stale_profile_cannot_start_a_different_strategy(self):
        app = Dashboard()
        with patch("dashboard.load_settings", return_value=Settings("fake")), patch(
            "dashboard.subprocess.Popen"
        ) as popen:
            with self.assertRaises(ValueError):
                app.start(False, "trend_sber")
            popen.assert_not_called()
        with self.assertRaises(ValueError):
            app.start_paper("trend_sber")

    def test_log_tail_includes_rotated_archives_in_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trading_bot.log"
            path.with_name(path.name + ".1").write_text("old1\nold2\n")
            path.write_text("new1\nnew2\n")
            with patch("dashboard.LOG_PATH", path):
                self.assertEqual(read_logs(3), ["old2", "new1", "new2"])

    def test_log_request_count_is_bounded(self):
        self.assertEqual(self.request("/api/status?lines=999999")[0], 200)
        self.app.status.assert_called_with(5000)
        self.assertEqual(self.request("/api/status?lines=bad")[0], 400)
