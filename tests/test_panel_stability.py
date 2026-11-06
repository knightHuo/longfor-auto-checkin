"""双端每日状态、时区、迁移与绑定响应性；不调用真实服务。"""

import asyncio
import sqlite3
import tempfile
import threading
import unittest
from contextlib import ExitStack, closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app import db, longhu, main


TODAY = "2026-11-07"
RUN_AT = "2026-11-07T08:30:00+08:00"


def account(**values):
    return {"id": 1, "name": "mock", "enabled": 1, "token": "mock-member-token",
            "last_status": "ok", "last_run_at": RUN_AT,
            "app_last_status": "already", "app_last_run_at": RUN_AT, **values}


class DailyPanelStatusTests(unittest.TestCase):
    def test_app_failure_is_partial_instead_of_success(self):
        for status in ("fail", "risk", "todo", "retry"):
            with self.subTest(status=status):
                result = main._daily_status(account(app_last_status=status), TODAY, "auto")
                self.assertEqual(result["today_status"], "partial")
                self.assertEqual(result["mini_today_status"], "ok")
                self.assertEqual(result["app_today_status"], status)

    def test_both_required_entries_must_be_completed_today(self):
        self.assertEqual(main._daily_status(account(), TODAY, "auto")["today_status"], "ok")
        yesterday_app = account(app_last_run_at="2026-11-06T08:30:00+08:00")
        result = main._daily_status(yesterday_app, TODAY, "auto")
        self.assertEqual(result["today_status"], "partial")
        self.assertEqual(result["app_today_status"], "pending")
        self.assertEqual(main._daily_status(account(), "2026-11-08", "auto")["today_status"], "pending")

    def test_disabled_app_is_skipped_and_does_not_prevent_completion(self):
        result = main._daily_status(account(app_last_status="fail"), TODAY, "off")
        self.assertEqual(result["today_status"], "ok")
        self.assertEqual(result["app_today_status"], "skipped")
        self.assertEqual(main._daily_status(account(app_last_status="todo"), TODAY, "manual")["today_status"], "partial")

    def test_overview_and_account_cards_use_the_same_daily_result(self):
        acc = account(app_last_status="fail")
        with patch.object(db, "list_accounts", return_value=[acc]), \
                patch.object(db, "get_settings", return_value={"app_checkin_mode": "auto"}), \
                patch.object(db, "dx_usage", return_value={}), \
                patch.object(db, "business_date", return_value=TODAY), \
                patch.object(main, "_next_run", return_value=""):
            overview = main.api_overview()
            cards = main.api_accounts()
        self.assertEqual(overview["stats"]["ok"], 0)
        self.assertEqual(overview["stats"]["partial"], 1)
        self.assertEqual(overview["today"], TODAY)
        self.assertEqual(overview["app_checkin_mode"], "auto")
        self.assertEqual(cards[0]["today_status"], "partial")
        self.assertNotIn("token", cards[0])

    def test_retry_and_busy_are_visible_without_reporting_success(self):
        for status in ("retry", "busy"):
            result = main._daily_status(account(last_status=status, app_last_status="pending"), TODAY, "auto")
            self.assertEqual(result["today_status"], status)


class BusinessDateTests(unittest.TestCase):
    def test_business_day_and_saved_time_do_not_follow_host_timezone(self):
        instant = datetime(2026, 11, 6, 16, 5, tzinfo=timezone.utc)
        with patch.object(db, "datetime") as clock:
            clock.now.side_effect = lambda tz: instant.astimezone(tz)
            self.assertEqual(db.business_date(), TODAY)
            self.assertEqual(db.now_iso(), "2026-11-07T00:05:00+08:00")

    def test_old_utc_or_naive_times_are_compared_in_beijing(self):
        self.assertEqual(db.run_date("2026-11-06T16:05:00+00:00"), TODAY)
        self.assertEqual(db.run_date("2026-11-07T00:05:00"), TODAY)
        for invalid in (None, "", "not-a-date"):
            self.assertEqual(db.run_date(invalid), "")


class AppStateMigrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = str(Path(directory.name) / "test.db")
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(db, "DB_PATH", self.path))

    def test_old_database_migrates_without_losing_account_or_app_results(self):
        old_schema = "\n".join(line for line in db.SCHEMA.splitlines() if "app_last_" not in line)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.executescript(old_schema)
            conn.execute("INSERT INTO accounts(name,token,last_status,last_run_at) VALUES(?,?,?,?)",
                         ("mock", "mock-member-token", "already", RUN_AT))
        db.init()
        saved = db.get_account(1)
        self.assertEqual(saved["last_status"], "already")
        self.assertEqual(saved["app_last_status"], "")
        db.update_account(1, app_last_status="risk", app_last_message="需要验证", app_last_run_at=RUN_AT)
        db.init()
        self.assertEqual(db.get_account(1)["app_last_status"], "risk")

    def test_only_changed_device_credentials_reset_both_entry_results(self):
        db.init()
        acc_id = db.create_account({"name": "mock", "token": "mock-token", "dx_token": "mock-device", "dx_source": "3"})
        db.update_account(acc_id, last_status="risk", last_message="需要验证", last_run_at=RUN_AT,
                          app_last_status="todo", app_last_message="待确认", app_last_run_at=RUN_AT)
        db.update_account(acc_id, dx_token="mock-device", dx_source="3")
        self.assertEqual(db.get_account(acc_id)["last_status"], "risk")
        db.update_account(acc_id, token="rotated-by-daily-refresh")
        self.assertEqual(db.get_account(acc_id)["last_status"], "risk")
        db.update_account(acc_id, dx_source="1")
        saved = db.get_account(acc_id)
        for key, value in db.execution_reset_fields().items():
            self.assertEqual(saved[key], value)


class BindingResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    def bind_patches(self, probe, refresh=None):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(main.parser, "parse", return_value={"token": "mock-token", "note": "mock"}))
        stack.enter_context(patch.object(longhu, "probe_token", side_effect=probe))
        stack.enter_context(patch.object(longhu, "refresh_token", side_effect=refresh))
        stack.enter_context(patch.object(db, "list_accounts", return_value=[]))
        stack.enter_context(patch.object(db, "create_account", return_value=7))
        stack.enter_context(patch.object(db, "add_log"))

    async def bind(self):
        return await main.api_bind(mode="token", name="mock", payload="mock", dx_token="", dx_source="3", file=None)

    async def test_slow_binding_keeps_event_loop_available(self):
        entered = threading.Event()
        release = threading.Event()

        def slow_probe(*args):
            entered.set()
            if not release.wait(1):
                raise AssertionError("绑定阻塞了事件循环，无法释放模拟网络请求")
            return {"nickname": "mock"}

        self.bind_patches(slow_probe)

        async def heartbeat():
            while not entered.is_set():
                await asyncio.sleep(0.001)
            await asyncio.sleep(0.01)
            release.set()

        result, _ = await asyncio.wait_for(asyncio.gather(self.bind(), heartbeat()), 2)
        self.assertTrue(result["ok"])

    async def test_refresh_fallback_and_reprobe_also_use_worker_thread(self):
        loop_thread = threading.get_ident()
        calls = []

        def probe(*args):
            calls.append(("probe", threading.get_ident()))
            if len(calls) == 1:
                raise longhu.LonghuError("模拟凭据需续期")
            return {"nickname": "mock"}

        def refresh(*args):
            calls.append(("refresh", threading.get_ident()))
            return True, "mock-new-token"

        self.bind_patches(probe, refresh)
        result = await self.bind()
        self.assertTrue(result["ok"])
        self.assertEqual([name for name, _ in calls], ["probe", "refresh", "probe"])
        self.assertTrue(all(thread != loop_thread for _, thread in calls))


if __name__ == "__main__":
    unittest.main()
