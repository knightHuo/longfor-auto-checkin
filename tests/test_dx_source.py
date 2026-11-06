"""设备标识来源合同；使用虚构资料，不连接任何真实服务。"""

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import db, longhu, main, parser
from app.security import Credentials


DEVICE = "fake-browser-device-0123456789"
TOKEN = "fake-member-token-0123456789"
CURL = ("curl 'https://gw2c-hw-open.longfor.com/signature/page-info' "
        f"-H 'X-LF-UserToken: {TOKEN}' -H 'X-LF-DXRisk-Token: {DEVICE}' "
        "-H 'X-LF-DXRisk-Source: 3' -H 'User-Agent: observed-browser-agent'")


class SourceHeaderTests(unittest.TestCase):
    def test_browser_source_and_observed_agent_are_preserved(self):
        account = {"token": TOKEN, "dx_token": DEVICE, "dx_source": "3",
                   "user_agent": "observed-browser-agent"}
        headers = longhu._task_headers(account)
        self.assertEqual(headers["X-LF-DXRisk-Source"], "3")
        self.assertEqual(headers["User-Agent"], account["user_agent"])
        self.assertEqual(headers["X-LF-DXRisk-Token"], DEVICE)
        self.assertEqual(longhu._task_headers({})["X-LF-DXRisk-Source"], "5")

    def test_invalid_source_never_reaches_state_or_submission(self):
        account = {"token": TOKEN, "dx_token": DEVICE, "dx_source": "9"}
        with patch.object(longhu, "sign_state") as state, patch.object(longhu, "_request") as request:
            result = longhu.checkin(account)
        self.assertEqual(result[0], "todo")
        self.assertIn("来源无效", result[1])
        state.assert_not_called()
        request.assert_not_called()
        with patch.object(longhu, "_request") as request:
            with self.assertRaises(longhu.LonghuError):
                longhu.sign_state(account)
        request.assert_not_called()


class SourceParserTests(unittest.TestCase):
    def test_curl_and_har_retain_captured_source(self):
        self.assertEqual(parser.parse_curl(CURL)["dx_source"], "3")
        har = {"log": {"entries": [{"request": {
            "url": "https://gw2c-hw-open.longfor.com/signature/page-info",
            "headers": [{"name": "lmToken", "value": TOKEN},
                        {"name": "X-LF-DXRisk-Source", "value": "1"}],
        }}]}}
        self.assertEqual(parser.parse_har(json.dumps(har))["dx_source"], "1")

    def test_invalid_captured_source_is_rejected(self):
        with self.assertRaises(parser.ParseError):
            parser.parse_curl(CURL.replace("Source: 3", "Source: 9"))


class SourceMigrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = str(Path(directory.name) / "test.db")
        self.patch = patch.object(db, "DB_PATH", path)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_existing_device_is_preserved_with_legacy_source(self):
        with closing(sqlite3.connect(db.DB_PATH)) as connection, connection:
            connection.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT, token TEXT, dx_token TEXT, updated_at TEXT)")
            connection.execute("INSERT INTO accounts (id,name,token,dx_token) VALUES (1,?,?,?)", ("test", TOKEN, DEVICE))
        db.init()
        db.init()  # A restart must not reset a recorded source.
        account = db.get_account(1)
        self.assertEqual((account["dx_token"], account["dx_source"]), (DEVICE, "5"))
        db.update_account(1, dx_source="3")
        db.init()
        self.assertEqual(db.get_account(1)["dx_source"], "3")

    def test_new_database_round_trip_and_source_constraint(self):
        db.init()
        account_id = db.create_account({"name": "test", "token": TOKEN,
                                        "dx_token": DEVICE, "dx_source": "3"})
        self.assertEqual(db.get_account(account_id)["dx_source"], "3")
        with self.assertRaises(sqlite3.IntegrityError):
            db.update_account(account_id, dx_source="9")


class SourceApiTests(unittest.TestCase):
    def setUp(self):
        salt = b"fake-auth-salt-16"
        self.password = "fake-console-password"
        self.credentials = Credentials(b"operator", salt,
            hashlib.pbkdf2_hmac("sha256", self.password.encode(), salt, 200000), 200000)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"AUX_SCHEDULE_ENABLED": "0"}))
        self.stack.enter_context(patch.object(main.security, "load_credentials", return_value=self.credentials))
        self.stack.enter_context(patch.object(main.db, "init"))
        self.client = self.stack.enter_context(TestClient(main.app))
        self.auth = ("operator", self.password)

    def test_binding_persists_source_before_any_later_submission(self):
        with patch.object(main.longhu, "probe_token", return_value={}) as probe, \
                patch.object(main.db, "accounts_with_dx", return_value=[]), \
                patch.object(main.db, "list_accounts", return_value=[]), \
                patch.object(main.db, "create_account", return_value=7) as create, \
                patch.object(main.db, "add_log"):
            response = self.client.post("/api/accounts/bind", auth=self.auth,
                                        data={"mode": "curl", "name": "test", "payload": CURL})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(create.call_args.args[0]["dx_source"], "3")
        self.assertEqual(create.call_args.args[0]["user_agent"], "observed-browser-agent")
        probe.assert_called_once()

    def test_invalid_binding_source_is_refused_before_upstream(self):
        with patch.object(main.longhu, "probe_token") as probe, \
                patch.object(main.db, "create_account") as create:
            response = self.client.post("/api/accounts/bind", auth=self.auth,
                data={"mode": "curl", "payload": CURL, "dx_source": "9"})
        self.assertEqual(response.status_code, 400)
        probe.assert_not_called()
        create.assert_not_called()

    def test_setting_source_is_explicit_and_does_not_change_agent(self):
        account = {"id": 7, "name": "test", "dx_source": "1", "user_agent": "observed-browser-agent"}
        with patch.object(main.db, "get_account", return_value=account), \
                patch.object(main.db, "accounts_with_dx", return_value=[]), \
                patch.object(main.db, "update_account") as update, \
                patch.object(main.db, "add_log") as log:
            response = self.client.post("/api/accounts/7/dx", auth=self.auth,
                                        data={"dx_token": DEVICE, "dx_source": "3"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["dx_source_label"], "普通浏览器")
        update.assert_called_once_with(7, dx_token=DEVICE, dx_source="3")
        self.assertIn("待业务验证", log.call_args.args[-1])
        self.assertNotIn(DEVICE, response.text)

    def test_omitting_source_preserves_the_existing_source(self):
        with patch.object(main.db, "get_account", return_value={"name": "test", "dx_source": "3"}), \
                patch.object(main.db, "accounts_with_dx", return_value=[]), \
                patch.object(main.db, "update_account") as update, patch.object(main.db, "add_log"):
            response = self.client.post("/api/accounts/7/dx", auth=self.auth, data={"dx_token": DEVICE})
        self.assertEqual(response.status_code, 200)
        update.assert_called_once_with(7, dx_token=DEVICE, dx_source="3")

    def test_invalid_update_does_not_write_or_expose_a_device_value(self):
        with patch.object(main.db, "get_account", return_value={"name": "test", "dx_source": "3"}), \
                patch.object(main.db, "update_account") as update, patch.object(main.db, "add_log") as log:
            response = self.client.post("/api/accounts/7/dx", auth=self.auth,
                                        data={"dx_token": DEVICE, "dx_source": "9"})
        self.assertEqual(response.status_code, 400)
        update.assert_not_called()
        log.assert_not_called()
        self.assertNotIn(DEVICE, response.text)

    def test_account_decoration_exposes_only_source_type(self):
        decorated = main._decorate({"token": TOKEN, "dx_token": DEVICE,
                                    "dx_source": "3", "user_agent": "private-agent"})
        self.assertEqual(decorated["dx_source"], "3")
        self.assertEqual(decorated["dx_source_label"], "普通浏览器")
        self.assertNotIn(DEVICE, json.dumps(decorated))
        self.assertNotIn("user_agent", decorated)


if __name__ == "__main__":
    unittest.main()
