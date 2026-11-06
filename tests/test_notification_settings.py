import os
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import main


class NotificationSettingsTests(unittest.TestCase):
    def setUp(self):
        self.settings = dict(main.db.DEFAULT_SETTINGS)
        self.settings["pushplus_token"] = "private-pushplus-token-sentinel"
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"AUX_SCHEDULE_ENABLED": "0"}))
        self.stack.enter_context(patch.object(main.db, "get_settings", side_effect=lambda: dict(self.settings)))
        self.save = self.stack.enter_context(patch.object(main.db, "save_settings", side_effect=self.settings.update))
        self.stack.enter_context(patch.object(main.scheduler, "window_text", return_value="08:00–09:00"))
        self.stack.enter_context(patch.object(main, "_next_run", return_value=""))
        self.notify = self.stack.enter_context(patch.object(main.runner, "notify", return_value={
            "ok": True, "status": "queued", "message": "已受理，等待投递"}))
        self.run = self.stack.enter_context(patch.object(main.runner, "run_account"))
        self.summary = self.stack.enter_context(patch.object(main.runner, "daily_summary"))
        application = FastAPI()
        application.add_api_route("/api/settings", main.api_get_settings, methods=["GET"])
        application.add_api_route("/api/settings", main.api_save_settings, methods=["POST"])
        application.add_api_route("/api/notifications/test", main.api_test_notification, methods=["POST"])
        application.add_api_route("/api/accounts/{account_id}/run", main.api_run, methods=["POST"])
        self.client = TestClient(application)
        self.addCleanup(self.client.close)

    def save_settings(self, **values):
        return self.client.post("/api/settings", data={"checkin_time": "08:00", **values})

    def test_settings_never_return_saved_pushplus_token(self):
        response = self.client.get("/api/settings")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["pushplus_configured"])
        self.assertEqual(response.json()["notification_provider"], "webhook")
        self.assertNotIn("pushplus_token", response.json())
        self.assertNotIn(self.settings["pushplus_token"], response.text)

    def test_omitted_empty_and_blank_token_keep_saved_value(self):
        previous = self.settings["pushplus_token"]
        for values in ({}, {"pushplus_token": ""}, {"pushplus_token": "  \t  "}):
            with self.subTest(values=values):
                response = self.save_settings(notification_provider="pushplus", **values)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.settings["pushplus_token"], previous)
                self.assertNotIn(previous, response.text)

    def test_replacement_and_explicit_clear(self):
        token = "new-pushplus-token-1234567890"
        response = self.save_settings(notification_provider="pushplus", pushplus_token=token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.settings["pushplus_token"], token)
        self.assertNotIn(token, response.text)
        response = self.save_settings(clear_pushplus_token="true")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.settings["pushplus_token"], "")
        self.assertFalse(self.client.get("/api/settings").json()["pushplus_configured"])

    def test_notification_changes_preserve_existing_schedule(self):
        with patch.object(main, "_schedule_enabled", return_value=True), \
                patch.object(main.scheduler, "reschedule") as reschedule:
            response = self.save_settings(notification_provider="pushplus", checkin_jitter="60")
        self.assertEqual(response.status_code, 200)
        reschedule.assert_not_called()

    def test_schedule_field_changes_reschedule(self):
        with patch.object(main, "_schedule_enabled", return_value=True), \
                patch.object(main.scheduler, "reschedule") as reschedule:
            response = self.client.post("/api/settings", data={"checkin_time": "08:30"})
        self.assertEqual(response.status_code, 200)
        reschedule.assert_called_once_with()

    def test_invalid_provider_and_tokens_do_not_save_or_echo(self):
        invalid = [{"notification_provider": "other"}]
        invalid.extend({"pushplus_token": token} for token in (
            "short", "x" * 129, "has whitespace-in-token", "line\nbreak-sentinel-1234",
            "https://pushplus.example/token"))
        for values in invalid:
            with self.subTest(field=next(iter(values))):
                self.save.reset_mock()
                response = self.save_settings(**values)
                self.assertEqual(response.status_code, 400)
                self.save.assert_not_called()
                for value in values.values():
                    self.assertNotIn(value, response.text)

    def test_test_notification_uses_saved_provider_without_running_checkin(self):
        self.settings["notification_provider"] = "pushplus"
        response = self.client.post("/api/notifications/test")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "queued")
        self.assertIn("已受理", response.json()["message"])
        self.assertNotIn("已送达", response.json()["message"])
        self.notify.assert_called_once()
        self.run.assert_not_called()
        self.summary.assert_not_called()
        self.save.assert_not_called()
        self.assertNotIn(self.settings["pushplus_token"], response.text)

    def test_missing_pushplus_token_does_not_send(self):
        self.settings.update(notification_provider="pushplus", pushplus_token="")
        response = self.client.post("/api/notifications/test")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["status"], "disabled")
        self.notify.assert_not_called()
        self.run.assert_not_called()

    def test_notification_failures_have_safe_status_and_message(self):
        for status, code in (("disabled", 400), ("failed", 502)):
            with self.subTest(status=status):
                self.notify.return_value = {"status": status, "message": "private-response-sentinel"}
                response = self.client.post("/api/notifications/test")
                self.assertEqual(response.status_code, code)
                self.assertEqual(response.json()["status"], status)
                self.assertNotIn("private-response-sentinel", response.text)
                self.run.assert_not_called()

    def test_manual_run_sends_summary_of_both_results(self):
        result = {"checkin_status": "already", "extra_signs": [{"key": "app", "status": "todo"}]}
        self.run.return_value = result
        with patch.object(main.db, "get_account", return_value={"id": 1, "name": "mock-account"}):
            response = self.client.post("/api/accounts/1/run")
        self.assertEqual(response.status_code, 200)
        self.summary.assert_called_once_with([result])
        self.notify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
