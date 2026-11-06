"""Public setup and malformed import boundaries; all credentials are synthetic."""

import io
import json
import os
import unittest
from contextlib import ExitStack
from unittest.mock import patch
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import config, main, parser, security


class EnvironmentTests(unittest.TestCase):
    def test_new_install_is_paused_and_explicit_values_are_required(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(config.schedule_enabled())
        for value in ("1", "true", "on", "yes"):
            with patch.dict(os.environ, {"LONGFOR_SCHEDULE_ENABLED": value}, clear=True):
                self.assertTrue(config.schedule_enabled())

    def test_canonical_setting_overrides_legacy_without_breaking_existing_installs(self):
        with patch.dict(os.environ, {"AUX_SCHEDULE_ENABLED": "1"}, clear=True):
            self.assertTrue(config.schedule_enabled())
        with patch.dict(os.environ, {"LONGFOR_SCHEDULE_ENABLED": "0", "AUX_SCHEDULE_ENABLED": "1"}, clear=True):
            self.assertFalse(config.schedule_enabled())


class ImportTests(unittest.TestCase):
    def test_import_does_not_accept_credentials_for_lookalike_or_other_sites(self):
        for host in ("example.com", "longfor.com.evil.invalid", "longfor.com@evil.invalid"):
            with self.subTest(host=host), self.assertRaises(parser.ParseError):
                parser.parse_curl("curl 'https://" + host + "/user-info' -H 'lmToken: synthetic-token-123456789'")

    def test_bad_har_shapes_have_safe_errors(self):
        for value in ([], {"log": []}, {"log": {"entries": {}}},
                      {"log": {"entries": [None, 4, {"request": []}]}}):
            with self.subTest(value=value), self.assertRaises(parser.ParseError):
                parser.parse_har(json.dumps(value))

    def test_referer_url_cannot_disguise_the_actual_curl_target(self):
        with self.assertRaises(parser.ParseError):
            parser.parse_curl("curl -H 'Referer: https://longfor.com/' -H 'token: synthetic-other-service-token' 'https://other.example/api'")

    def test_malformed_entries_do_not_hide_a_usable_member_query(self):
        valid = {"request": {"url": "https://gw2c-hw-open.longfor.com/user-info", "headers": [
            None, {"name": "lmToken", "value": "synthetic-token-123456789"},
        ]}}
        self.assertEqual(parser.parse_har(json.dumps({"log": {"entries": [None, valid]}}))["token"],
                         "synthetic-token-123456789")


class FormBoundaryTests(unittest.TestCase):
    def application(self, stack):
        stack.enter_context(patch.object(security, "load_credentials", return_value=SimpleNamespace(public_origin=None)))
        stack.enter_context(patch.object(security, "_authenticated", return_value=True))
        stack.enter_context(patch.object(main.db, "init"))
        stack.enter_context(patch.object(main, "_schedule_enabled", return_value=False))
        return stack.enter_context(TestClient(main.app))

    def test_webhook_secret_is_not_returned_and_blank_preserves_it(self):
        settings = {"webhook_url": "https://api.day.app/synthetic-private-device", "checkin_time": "08:00"}
        with ExitStack() as stack:
            stack.enter_context(patch.object(main.db, "get_settings", side_effect=lambda: dict(settings)))
            stack.enter_context(patch.object(main.db, "save_settings", side_effect=lambda fields: settings.update(fields)))
            client = self.application(stack)
            response = client.get("/api/settings")
            self.assertTrue(response.json()["webhook_configured"])
            self.assertNotIn(settings["webhook_url"], response.text)
            client.post("/api/settings", data={"checkin_time": "08:00", "webhook_url": ""})
            self.assertTrue(settings["webhook_url"])
            client.post("/api/settings", data={"checkin_time": "08:00", "clear_webhook_url": "true"})
            self.assertEqual(settings["webhook_url"], "")

    def test_reauthentication_updates_original_account_and_preserves_device(self):
        account = {"id": 1, "name": "演示", "token": "oldSyntheticMemberToken123", "dx_token": "synthetic-device",
                   "dx_source": "3", "user_agent": "observed-agent", "balance": 12.5}
        with ExitStack() as stack:
            stack.enter_context(patch.object(main.db, "get_account", return_value=account))
            stack.enter_context(patch.object(main.db, "accounts_with_dx", return_value=[]))
            stack.enter_context(patch.object(main.db, "add_log"))
            update = stack.enter_context(patch.object(main.db, "update_account"))
            create = stack.enter_context(patch.object(main.db, "create_account"))
            stack.enter_context(patch.object(main.longhu, "probe_token", return_value={"growth_value": 100}))
            client = self.application(stack)
            response = client.post("/api/accounts/bind", data={"account_id": 1, "mode": "curl", "payload":
                "curl 'https://gw2c-hw-open.longfor.com/user-info' -H 'lmToken: newSyntheticMemberToken123' "
                "-H 'User-Agent: different-browser-agent' -H 'X-LF-DXRisk-Source: 5'"})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["updated"])
            self.assertEqual(response.json()["id"], 1)
            create.assert_not_called()
            fields = update.call_args.kwargs
            self.assertEqual(fields["dx_source"], "3")
            self.assertEqual(fields["dx_token"], "synthetic-device")
            self.assertEqual(fields["user_agent"], "observed-agent")
            self.assertNotIn("balance", fields)
            self.assertEqual(fields["last_status"], "")

    def test_validation_does_not_echo_secret_form_values(self):
        application = main.app
        with ExitStack() as stack:
            stack.enter_context(patch.object(security, "load_credentials", return_value=SimpleNamespace(public_origin=None)))
            stack.enter_context(patch.object(security, "_authenticated", return_value=True))
            stack.enter_context(patch.object(main.db, "init"))
            stack.enter_context(patch.object(main, "_schedule_enabled", return_value=False))
            probe = stack.enter_context(patch.object(main.longhu, "probe_token"))
            with TestClient(application) as client:
                secret = "synthetic-private-member-token"
                response = client.post("/api/accounts/bind", data={"mode": "token", "name": secret * 10,
                                                                     "payload": secret})
                self.assertEqual(response.status_code, 422)
                self.assertNotIn(secret, response.text)
                probe.assert_not_called()

    def test_oversize_file_is_refused_before_upstream_validation(self):
        with ExitStack() as stack:
            stack.enter_context(patch.object(security, "load_credentials", return_value=SimpleNamespace(public_origin=None)))
            stack.enter_context(patch.object(security, "_authenticated", return_value=True))
            stack.enter_context(patch.object(main.db, "init"))
            stack.enter_context(patch.object(main, "_schedule_enabled", return_value=False))
            stack.enter_context(patch.object(main, "MAX_IMPORT_BYTES", 16))
            probe = stack.enter_context(patch.object(main.longhu, "probe_token"))
            with TestClient(main.app) as client:
                response = client.post("/api/accounts/bind", data={"mode": "har"},
                                       files={"file": ("member.har", io.BytesIO(b"x" * 17))})
                self.assertEqual(response.status_code, 413)
                probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
