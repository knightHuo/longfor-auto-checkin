import base64
import hashlib
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.security import ConsoleAuthMiddleware, Credentials, load_credentials


SALT = bytes(range(16))
ITERATIONS = 200000
PASSWORD = "test-console-password"
HASH = hashlib.pbkdf2_hmac("sha256", PASSWORD.encode(), SALT, ITERATIONS)


class ConsoleSecurityTests(unittest.TestCase):
    def setUp(self):
        self.application = FastAPI()
        self.application.state.console_credentials = Credentials(b"operator", SALT, HASH, ITERATIONS)
        self.application.add_middleware(ConsoleAuthMiddleware)

        @self.application.get("/api/health")
        def health():
            return {"ok": True}

        @self.application.api_route("/private", methods=["GET", "POST"])
        def private():
            return {"ok": True}

        self.client = TestClient(self.application)

    def test_health_only_is_anonymous(self):
        self.assertEqual(self.client.get("/api/health").json(), {"ok": True})
        for path in ("/", "/private", "/api/accounts", "/static/index.html", "/docs", "/openapi.json"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 401)
                self.assertIn("Basic", response.headers["www-authenticate"])

    def test_bad_and_malformed_credentials_are_refused(self):
        response = self.client.get("/private", auth=("operator", "wrong"))
        self.assertEqual(response.status_code, 401)
        for header in ("Bearer anything", "Basic !!!", "Basic " + base64.b64encode(b"no-colon").decode()):
            with self.subTest(header=header):
                self.assertEqual(self.client.get("/private", headers={"Authorization": header}).status_code, 401)

    def test_authenticated_page_is_not_cached(self):
        response = self.client.get("/private", auth=("operator", PASSWORD))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-frame-options"], "DENY")

    def test_cross_origin_writes_are_refused_and_cli_remains_usable(self):
        for headers in ({"Origin": "https://evil.example"}, {"Origin": "null"},
                        {"Referer": "https://evil.example/form"}, {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.client.post("/private", auth=("operator", PASSWORD), headers=headers).status_code, 403)
        for headers in ({}, {"Origin": "http://testserver"}, {"Referer": "http://testserver/console/"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.client.post("/private", auth=("operator", PASSWORD), headers=headers).status_code, 200)

    def test_explicit_https_public_origin_behind_proxy(self):
        self.application.state.console_credentials = Credentials(
            b"operator", SALT, HASH, ITERATIONS, ("https", "example.test", 9443))
        self.assertEqual(self.client.post("/private", auth=("operator", PASSWORD),
                                         headers={"Origin": "https://example.test:9443"}).status_code, 200)
        self.assertEqual(self.client.post("/private", auth=("operator", PASSWORD),
                                         headers={"Origin": "http://example.test:9443"}).status_code, 403)

    def test_root_prefix_keeps_health_public_and_console_private(self):
        client = TestClient(self.application, root_path="/console")
        self.assertEqual(client.get("/console/api/health").status_code, 200)
        self.assertEqual(client.get("/console/private").status_code, 401)
        self.assertEqual(client.get("/console/private", auth=("operator", PASSWORD)).status_code, 200)

    def test_missing_runtime_configuration_is_closed(self):
        del self.application.state.console_credentials
        self.assertEqual(self.client.get("/private", auth=("operator", PASSWORD)).status_code, 503)


class CredentialFileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "console-auth.json"
        self.path.write_text(json.dumps({"username": "operator", "salt": SALT.hex(),
                                         "hash": HASH.hex(), "iterations": ITERATIONS}), encoding="utf-8")
        self.path.chmod(0o600)

    def test_loads_hash_only_configuration(self):
        with patch.dict(os.environ, {"AUX_AUTH_FILE": str(self.path), "AUX_PUBLIC_ORIGIN": "https://example.test:9443"}):
            credentials = load_credentials()
        self.assertEqual(credentials.password_hash, HASH)
        self.assertEqual(credentials.public_origin, ("https", "example.test", 9443))

    def test_missing_or_invalid_configuration_fails_without_secrets(self):
        with patch.dict(os.environ, {"AUX_AUTH_FILE": "", "AUX_PUBLIC_ORIGIN": ""}):
            with self.assertRaisesRegex(RuntimeError, "not configured correctly"):
                load_credentials()
        self.path.write_text('{"username":"secret-sentinel","salt":"bad"}', encoding="utf-8")
        with patch.dict(os.environ, {"AUX_AUTH_FILE": str(self.path), "AUX_PUBLIC_ORIGIN": ""}):
            with self.assertRaises(RuntimeError) as error:
                load_credentials()
        self.assertNotIn("secret-sentinel", str(error.exception))

    @unittest.skipUnless(os.name == "posix", "POSIX permission contract")
    def test_world_readable_configuration_is_refused(self):
        self.path.chmod(0o644)
        with patch.dict(os.environ, {"AUX_AUTH_FILE": str(self.path), "AUX_PUBLIC_ORIGIN": ""}):
            with self.assertRaises(RuntimeError):
                load_credentials()

    def test_main_does_not_start_scheduler_when_disabled(self):
        from app import main
        with patch.dict(os.environ, {"AUX_AUTH_FILE": str(self.path), "AUX_PUBLIC_ORIGIN": "",
                                     "AUX_SCHEDULE_ENABLED": "0"}), \
                patch.object(main.db, "init"), patch.object(main.scheduler, "start") as start:
            with TestClient(main.app, root_path="/console") as client:
                page = client.get("/console/", auth=("operator", PASSWORD))
                self.assertEqual(page.status_code, 200)
                self.assertIn('name="app-root" content="/console"', page.text)
                self.assertNotIn("__APP_ROOT__", page.text)
                self.assertEqual(client.get("/console/api/health").json(), {"ok": True})
                for route in ("/console/static/index.html", "/console/docs", "/console/openapi.json"):
                    self.assertEqual(client.get(route).status_code, 401)
                    self.assertEqual(client.get(route, auth=("operator", PASSWORD)).status_code, 200)
            start.assert_not_called()

    def test_main_settings_cannot_enable_scheduling_when_environment_paused(self):
        from app import main
        with patch.dict(os.environ, {"AUX_AUTH_FILE": str(self.path), "AUX_PUBLIC_ORIGIN": "https://example.test:9443",
                                     "AUX_SCHEDULE_ENABLED": "0"}), \
                patch.object(main.db, "init"), patch.object(main.db, "save_settings") as save, \
                patch.object(main.scheduler, "reschedule") as reschedule, \
                patch.object(main.scheduler, "window_text", return_value="08:00–09:00"):
            with TestClient(main.app, root_path="/console") as client:
                route = "/console/api/settings"
                response = client.post(route, auth=("operator", PASSWORD),
                                       headers={"Origin": "https://evil.example"}, data={"checkin_time": "08:00"})
                self.assertEqual(response.status_code, 403)
                save.assert_not_called()
                response = client.post(route, auth=("operator", PASSWORD),
                                       headers={"Origin": "https://example.test:9443"}, data={"checkin_time": "08:00"})
                self.assertEqual(response.status_code, 200)
                self.assertFalse(response.json()["schedule_enabled"])
                self.assertEqual(response.json()["next_run"], "")
                save.assert_called_once()
                reschedule.assert_not_called()

    def test_main_startup_without_credentials_fails_before_database_or_scheduler(self):
        from app import main
        with patch.dict(os.environ, {"AUX_AUTH_FILE": "", "AUX_SCHEDULE_ENABLED": "0"}), \
                patch.object(main.db, "init") as initialize, patch.object(main.scheduler, "start") as start:
            with self.assertRaisesRegex(RuntimeError, "not configured correctly"):
                with TestClient(main.app):
                    pass
            initialize.assert_not_called()
            start.assert_not_called()

    def test_readonly_account_status_checks_both_activities_without_writes(self):
        from app import main
        account = {"id": 7, "name": "test", "token": "fake-private-login-token",
                   "dx_token": "fake-private-device-token", "user_agent": "private-agent"}
        profile = {"growth_value": 42, "level_name": "V2", "nickname": "测试",
                   "balance": 12, "token": account["token"], "dx_token": account["dx_token"],
                   "user_agent": account["user_agent"]}
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"AUX_AUTH_FILE": str(self.path), "AUX_PUBLIC_ORIGIN": "",
                                                       "AUX_SCHEDULE_ENABLED": "0"}))
            stack.enter_context(patch.object(main.db, "init"))
            lookup = stack.enter_context(patch.object(main.db, "get_account", return_value=account))
            state = stack.enter_context(patch.object(main.longhu, "sign_state", side_effect=[True, False]))
            fetch = stack.enter_context(patch.object(main.longhu, "fetch_profile", return_value=profile))
            writes = [stack.enter_context(patch.object(owner, name)) for owner, name in (
                (main.longhu, "refresh_token"), (main.longhu, "checkin"),
                (main.runner, "run_account"), (main.runner, "run_all"),
                (main.db, "update_account"), (main.db, "create_account"), (main.db, "delete_account"),
                (main.db, "save_settings"), (main.db, "add_log"), (main.scheduler, "start"),
            )]
            with TestClient(main.app, root_path="/console") as client:
                route = "/console/api/accounts/7/status"
                self.assertEqual(client.get(route).status_code, 401)
                lookup.assert_not_called()
                response = client.get(route, auth=("operator", PASSWORD))
            self.assertEqual(response.status_code, 200)
            value = response.json()
            self.assertEqual((value["mini"], value["app"]), ("signed", "not_signed"))
            self.assertEqual(value["profile"], {"growth_value": 42, "level_name": "V2", "nickname": "测试", "balance": 12})
            self.assertEqual(value["errors"], [])
            self.assertEqual([call.args[1] for call in state.call_args_list],
                             [main.longhu.ACTIVITY_NO, main.longhu.APP_ACTIVITY_NO])
            fetch.assert_called_once_with(account)
            for write in writes:
                write.assert_not_called()
            for secret in (account["token"], account["dx_token"], account["user_agent"]):
                self.assertNotIn(secret, response.text)

    def test_readonly_status_preserves_unknown_and_redacts_business_errors(self):
        from app import main
        account = {"id": 7, "token": "fake-private-login-token", "dx_token": "fake-private-device-token"}
        with patch.dict(os.environ, {"AUX_AUTH_FILE": str(self.path), "AUX_PUBLIC_ORIGIN": "",
                                     "AUX_SCHEDULE_ENABLED": "0"}), \
                patch.object(main.db, "init"), patch.object(main.db, "get_account", return_value=account), \
                patch.object(main.longhu, "sign_state", side_effect=[main.longhu.LonghuError(account["token"]), None]), \
                patch.object(main.longhu, "fetch_profile", side_effect=main.longhu.LonghuError(account["dx_token"])):
            with TestClient(main.app, root_path="/console") as client:
                response = client.get("/console/api/accounts/7/status", auth=("operator", PASSWORD))
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json()["mini"], response.json()["app"]), ("unknown", "unknown"))
        self.assertEqual(len(response.json()["errors"]), 3)
        self.assertNotIn(account["token"], response.text)
        self.assertNotIn(account["dx_token"], response.text)

    def test_readonly_status_missing_account_never_queries_upstream(self):
        from app import main
        with patch.dict(os.environ, {"AUX_AUTH_FILE": str(self.path), "AUX_PUBLIC_ORIGIN": "",
                                     "AUX_SCHEDULE_ENABLED": "0"}), \
                patch.object(main.db, "init"), patch.object(main.db, "get_account", return_value=None), \
                patch.object(main.longhu, "sign_state") as state, patch.object(main.longhu, "fetch_profile") as fetch:
            with TestClient(main.app, root_path="/console") as client:
                response = client.get("/console/api/accounts/7/status", auth=("operator", PASSWORD))
        self.assertEqual(response.status_code, 404)
        state.assert_not_called()
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
