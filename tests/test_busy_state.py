"""运行中展示只读互斥快照，不覆盖签到状态或暴露凭据。"""

from contextlib import ExitStack
import hashlib
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main, run_guard
from app.security import Credentials


class BusyLookupTests(unittest.TestCase):
    def test_readonly_lookup_does_not_create_a_lock(self):
        account_id = 910291
        before = set(run_guard._registry)
        self.assertFalse(run_guard.is_busy(account_id))
        self.assertEqual(set(run_guard._registry), before)

    def test_lookup_follows_the_existing_execution_or_update_lock(self):
        account_id = 910292
        self.assertFalse(run_guard.is_busy(account_id))
        lock = run_guard.try_acquire(account_id)
        self.assertIsNotNone(lock)
        try:
            self.assertTrue(run_guard.is_busy(account_id))
        finally:
            lock.release()
        self.assertFalse(run_guard.is_busy(account_id))


class BusyAccountApiTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        password = "synthetic-console-password"
        salt = b"synthetic-salt-16"
        credentials = Credentials(b"tester", salt,
            hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200000), 200000)
        self.auth = ("tester", password)
        self.account = {"id": 910293, "name": "虚构账号", "enabled": 1,
                        "token": "synthetic-private-member-token", "api_key": "synthetic-private-api-key",
                        "dx_token": "synthetic-private-device-token", "user_agent": "synthetic-private-agent",
                        "dx_source": "3", "last_status": "todo", "last_message": "签到请求已进入提交阶段",
                        "last_run_at": "2026-01-02T08:00:00+08:00",
                        "app_last_status": "pending", "app_last_run_at": None}
        self.stack.enter_context(patch.object(main, "_schedule_enabled", return_value=False))
        self.stack.enter_context(patch.object(main.security, "load_credentials", return_value=credentials))
        self.stack.enter_context(patch.object(main.db, "init"))
        self.stack.enter_context(patch.object(main.db, "list_accounts", side_effect=lambda: [dict(self.account)]))
        self.stack.enter_context(patch.object(main.db, "get_settings", return_value={"app_checkin_mode": "auto"}))
        self.stack.enter_context(patch.object(main.db, "business_date", return_value="2026-01-02"))
        self.stack.enter_context(patch.object(main.db, "dx_usage", return_value={}))
        self.write = self.stack.enter_context(patch.object(main.db, "update_account"))
        self.log = self.stack.enter_context(patch.object(main.db, "add_log"))
        self.client = self.stack.enter_context(TestClient(main.app))

    def accounts(self):
        response = self.client.get("/api/accounts", auth=self.auth)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_api_exposes_only_a_busy_boolean_and_keeps_persisted_status_unchanged(self):
        lock = run_guard.try_acquire(self.account["id"])
        self.assertIsNotNone(lock)
        try:
            busy = self.accounts()[0]
            self.assertIs(busy["is_busy"], True)
            self.assertEqual(busy["mini_today_status"], "todo")
            self.assertEqual(busy["today_status"], "todo")
        finally:
            lock.release()
        released = self.accounts()[0]
        self.assertIs(released["is_busy"], False)
        self.assertEqual(released["mini_today_status"], "todo")
        self.assertEqual(self.account["last_status"], "todo")
        for output in (busy, released):
            serialized = json.dumps(output)
            for field in ("token", "api_key", "dx_token", "user_agent"):
                self.assertNotIn(field, output)
                self.assertNotIn(self.account[field], serialized)
        self.write.assert_not_called()
        self.log.assert_not_called()


if __name__ == "__main__":
    unittest.main()
