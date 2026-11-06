"""账号并发、独立入口状态及部分资料更新；不访问真实账号或数据库。"""

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import db as actual_db, longhu, run_guard, runner


TODAY = "2026-11-07"
NOW = TODAY + "T08:46:06+08:00"
ACCOUNT = {"id": 101, "name": "虚构账号", "token": "synthetic-member-token",
           "dx_token": "synthetic-device-token", "dx_source": "3",
           "balance": 26.8, "last_status": "", "app_last_status": ""}


@contextmanager
def runner_env(account=None, profile=None):
    store = {**ACCOUNT, **(account or {})}
    with ExitStack() as stack:
        database = stack.enter_context(patch.object(runner, "db"))
        database.get_account.side_effect = lambda account_id: dict(store)
        database.list_accounts.side_effect = lambda **kwargs: [dict(store)]
        database.get_settings.return_value = {"app_checkin_mode": "auto"}
        database.now_iso.return_value = NOW
        database.business_date.return_value = TODAY
        database.run_date.side_effect = actual_db.run_date
        database.update_account.side_effect = lambda account_id, **fields: store.update(fields)
        refresh = stack.enter_context(patch.object(longhu, "refresh_token", return_value=(True, store["token"])))
        checkin = stack.enter_context(patch.object(longhu, "checkin", return_value=("ok", "日历已确认")))
        state = stack.enter_context(patch.object(longhu, "sign_state", return_value=False))
        fetch = stack.enter_context(patch.object(longhu, "fetch_profile", return_value=dict(profile or {})))
        notify = stack.enter_context(patch.object(runner, "notify"))
        yield SimpleNamespace(store=store, db=database, refresh=refresh, checkin=checkin,
                              state=state, fetch=fetch, notify=notify)


class AccountGuardTests(unittest.TestCase):
    def test_manual_and_scheduled_overlap_only_one_full_flow_reaches_upstream(self):
        entered = threading.Event()
        release = threading.Event()
        with runner_env() as env, ThreadPoolExecutor(max_workers=2) as pool:
            def hold_refresh(account):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("synthetic gate timeout")
                return True, account["token"]

            env.refresh.side_effect = hold_refresh
            first = pool.submit(runner.run_account, dict(ACCOUNT))
            try:
                self.assertTrue(entered.wait(2))
                second = pool.submit(runner.run_account, dict(ACCOUNT))
                busy = second.result(timeout=2)
                self.assertEqual(busy["checkin_status"], "busy")
                self.assertIsNone(busy["refreshed"])
                self.assertEqual(env.refresh.call_count, 1)
                env.checkin.assert_not_called()
                env.fetch.assert_not_called()
                self.assertNotIn(ACCOUNT["token"], str(busy))
                self.assertNotIn(ACCOUNT["dx_token"], str(busy))
            finally:
                release.set()
            result = first.result(timeout=2)
            self.assertEqual(result["checkin_status"], "ok")
            self.assertEqual(env.checkin.call_count, 2)
            self.assertEqual(env.fetch.call_count, 1)
            env.notify.assert_not_called()

    def test_acquired_flow_uses_latest_database_token_instead_of_stale_input(self):
        with runner_env({"token": "synthetic-newer-token"}) as env:
            result = runner.run_account(dict(ACCOUNT))
            self.assertEqual(result["checkin_status"], "ok")
            self.assertEqual(env.refresh.call_args.args[0]["token"], "synthetic-newer-token")
            for call in env.checkin.call_args_list:
                self.assertEqual(call.args[0]["token"], "synthetic-newer-token")

    def test_exception_releases_guard_for_the_next_run(self):
        with runner_env() as env:
            with patch.object(runner, "_run_account", side_effect=RuntimeError("synthetic")):
                with self.assertRaises(RuntimeError):
                    runner.run_account(dict(ACCOUNT))
            result = runner.run_account(dict(ACCOUNT), do_checkin=False)
            self.assertEqual(result["checkin_status"], "skipped")
            env.refresh.assert_called_once()

    def test_different_accounts_use_independent_locks(self):
        first = run_guard.try_acquire(10001)
        second = run_guard.try_acquire(10002)
        try:
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            self.assertIsNone(run_guard.try_acquire(10001))
        finally:
            if first:
                first.release()
            if second:
                second.release()

    def test_deleted_account_never_calls_upstream(self):
        with runner_env() as env:
            env.db.get_account.side_effect = None
            env.db.get_account.return_value = None
            result = runner.run_account(dict(ACCOUNT))
            self.assertEqual(result["checkin_status"], "skipped")
            env.refresh.assert_not_called()
            env.checkin.assert_not_called()
            env.fetch.assert_not_called()


class PersistedEntryTests(unittest.TestCase):
    def test_crash_after_clock_keeps_an_intent_that_survives_the_next_run(self):
        class SimulatedProcessExit(BaseException):
            pass
        real_checkin = longhu.checkin
        with runner_env() as env, patch.object(longhu, "_request", side_effect=SimulatedProcessExit) as request:
            env.checkin.side_effect = real_checkin
            with self.assertRaises(SimulatedProcessExit):
                runner.run_account(dict(ACCOUNT), app_mode="off")
            self.assertEqual(env.store["last_status"], "todo")
            self.assertEqual(env.store["last_run_at"], NOW)
            self.assertIn("提交阶段", env.store["last_message"])
            result = runner.run_account(dict(ACCOUNT), app_mode="off")
            self.assertEqual(result["checkin_status"], "todo")
            self.assertEqual(request.call_count, 1)

    def test_app_crash_also_persists_its_own_submission_intent(self):
        class SimulatedProcessExit(BaseException):
            pass
        real_checkin = longhu.checkin
        with runner_env() as env, patch.object(longhu, "_request", side_effect=SimulatedProcessExit) as request:
            env.checkin.side_effect = real_checkin
            env.state.side_effect = lambda account, activity: activity == longhu.ACTIVITY_NO
            with self.assertRaises(SimulatedProcessExit):
                runner.run_account(dict(ACCOUNT))
            self.assertEqual(env.store["last_status"], "already")
            self.assertEqual(env.store["app_last_status"], "todo")
            self.assertEqual(env.store["app_last_run_at"], NOW)
            result = runner.run_account(dict(ACCOUNT))
            self.assertEqual(result["extra_signs"][0]["status"], "todo")
            self.assertEqual(request.call_count, 1)

    def test_unexpected_exception_after_clock_cannot_clear_the_submission_pause(self):
        real_checkin = longhu.checkin
        with runner_env() as env, patch.object(longhu, "_request", side_effect=RuntimeError("synthetic")):
            env.checkin.side_effect = real_checkin
            with self.assertLogs(runner.logger, "ERROR"):
                result = runner.run_account(dict(ACCOUNT), app_mode="off")
            self.assertEqual(result["checkin_status"], "todo")
            self.assertEqual(env.store["last_status"], "todo")

    def test_definite_refresh_authentication_failure_stops_remaining_upstream_calls(self):
        with runner_env() as env:
            env.refresh.side_effect = longhu.LonghuAuthError("登录已失效，请重新导入")
            with self.assertLogs(runner.logger, "WARNING"):
                result = runner.run_account(dict(ACCOUNT))
            env.checkin.assert_not_called()
            env.state.assert_not_called()
            env.fetch.assert_not_called()
            self.assertEqual(result["checkin_status"], "todo")
            self.assertEqual(result["extra_signs"][0]["status"], "todo")
            self.assertEqual(env.store["last_run_at"], NOW)

    def test_app_failure_and_mini_success_persist_independently(self):
        with runner_env() as env:
            env.checkin.side_effect = [("ok", "小程序完成"), ("fail", "App 临时故障")]
            runner.run_account(dict(ACCOUNT))
            self.assertEqual(env.store["last_status"], "ok")
            self.assertEqual(env.store["last_message"], "小程序完成")
            self.assertEqual(env.store["app_last_status"], "fail")
            self.assertEqual(env.store["app_last_message"], "App 临时故障")
            self.assertEqual(env.store["app_last_run_at"], NOW)
            self.assertEqual(env.store["last_run_at"], NOW)

    def test_risk_in_mini_is_not_resubmitted_when_app_failure_retries_account(self):
        with runner_env() as env:
            env.checkin.side_effect = [("risk", "请完成验证"), ("fail", "App 临时故障"),
                                      ("ok", "App 恢复成功")]
            runner.run_account(dict(ACCOUNT))
            result = runner.run_account(dict(ACCOUNT))
            self.assertEqual(result["checkin_status"], "risk")
            self.assertEqual(result["extra_signs"][0]["status"], "ok")
            activities = [call.args[1] for call in env.checkin.call_args_list]
            self.assertEqual(activities, [longhu.ACTIVITY_NO, longhu.APP_ACTIVITY_NO, longhu.APP_ACTIVITY_NO])
            env.state.assert_called_once_with(unittest.mock.ANY, longhu.ACTIVITY_NO)

    def test_today_unknown_app_submission_only_rechecks_state_and_can_confirm_signed(self):
        account = {"app_last_status": "todo", "app_last_message": "请求结果未知",
                   "app_last_run_at": NOW}
        for confirmed in (False, None, True):
            with self.subTest(confirmed=confirmed), runner_env(account) as env:
                env.state.return_value = confirmed
                result = runner.run_account(dict(ACCOUNT))
                self.assertEqual(env.checkin.call_count, 1)
                self.assertEqual(env.checkin.call_args.args[1], longhu.ACTIVITY_NO)
                self.assertEqual(result["extra_signs"][0]["status"], "already" if confirmed else "todo")

    def test_previous_day_pending_does_not_block_todays_signin(self):
        with runner_env({"last_status": "risk", "last_run_at": "2026-11-06T08:00:00+08:00"}) as env:
            runner.run_account(dict(ACCOUNT))
            self.assertEqual(env.checkin.call_count, 2)
            env.state.assert_not_called()

    def test_readonly_pending_recheck_exception_preserves_submission_pause(self):
        with runner_env({"last_status": "todo", "last_run_at": NOW}) as env:
            env.state.side_effect = RuntimeError("synthetic")
            with self.assertLogs(runner.logger, "WARNING"):
                result = runner.run_account(dict(ACCOUNT))
            self.assertEqual(result["checkin_status"], "todo")
            self.assertEqual(env.store["last_status"], "todo")
            self.assertEqual(env.checkin.call_count, 1)

    def test_app_off_is_persisted_as_skipped(self):
        with runner_env() as env:
            result = runner.run_account(dict(ACCOUNT), app_mode="off")
            self.assertEqual(result["extra_signs"][0]["status"], "skipped")
            self.assertEqual(env.store["app_last_status"], "skipped")
            self.assertEqual(env.store["app_last_run_at"], NOW)
            self.assertEqual(env.checkin.call_count, 1)


class ProfileAndSummaryTests(unittest.TestCase):
    def test_refresh_only_preserves_both_sign_dates_and_statuses(self):
        previous = "2026-11-06T08:00:00+08:00"
        account = {"last_status": "ok", "last_run_at": previous,
                   "app_last_status": "already", "app_last_run_at": previous}
        with runner_env(account) as env:
            runner.run_account(dict(ACCOUNT), do_checkin=False)
            self.assertEqual(env.store["last_run_at"], previous)
            self.assertEqual(env.store["app_last_run_at"], previous)
            self.assertEqual(env.store["last_status"], "ok")
            self.assertEqual(env.store["app_last_status"], "already")
            env.checkin.assert_not_called()
            self.assertFalse(any("last_run_at" in call.kwargs or "app_last_run_at" in call.kwargs
                                 for call in env.db.update_account.call_args_list))

    def test_risk_timestamp_is_saved_even_if_later_profile_processing_crashes(self):
        with runner_env() as env:
            env.checkin.side_effect = [("risk", "请完成验证"), ("todo", "App 请求结果未知")]
            env.fetch.side_effect = RuntimeError("synthetic unexpected profile error")
            with self.assertRaises(RuntimeError):
                runner.run_account(dict(ACCOUNT))
            self.assertEqual((env.store["last_status"], env.store["last_run_at"]), ("risk", NOW))
            self.assertEqual((env.store["app_last_status"], env.store["app_last_run_at"]), ("todo", NOW))
            mini_write = next(call.kwargs for call in env.db.update_account.call_args_list
                              if "last_status" in call.kwargs)
            self.assertEqual(mini_write["last_run_at"], NOW)

    def test_partial_profile_preserves_old_balance_and_logs_warning(self):
        profile = {"growth_value": 3379, "_warnings": ["珑珠余额暂未刷新，保留上次记录"]}
        with runner_env(profile=profile) as env:
            result = runner.run_account(dict(ACCOUNT), do_checkin=False)
            self.assertEqual(env.store["balance"], 26.8)
            self.assertEqual(env.store["growth_value"], 3379)
            self.assertNotIn("balance", result["profile"])
            self.assertNotIn("_warnings", result["profile"])
            self.assertEqual(result["profile_warnings"], profile["_warnings"])
            self.assertTrue(any(call.args[2:4] == ("query", "todo") for call in env.db.add_log.call_args_list))
            self.assertFalse(any("balance" in call.kwargs for call in env.db.update_account.call_args_list))

    def test_real_zero_balance_is_saved(self):
        with runner_env(profile={"balance": 0}) as env:
            runner.run_account(dict(ACCOUNT), do_checkin=False)
            self.assertEqual(env.store["balance"], 0)

    def test_summary_is_one_dated_message_with_both_entries_and_no_successful_renewal_noise(self):
        with patch.object(runner.db, "business_date", return_value=TODAY), patch.object(runner, "notify") as notify:
            runner.daily_summary([{"name": "虚构账号", "refreshed": True, "checkin_status": "already",
                                  "extra_signs": [{"key": "app", "status": "ok"}]}])
        notify.assert_called_once()
        text = notify.call_args.args[0]
        self.assertIn(TODAY, text)
        self.assertIn("小程序✅已签", text)
        self.assertIn("App✅已签", text)
        self.assertNotIn("登录续期", text)

    def test_retry_busy_and_profile_warning_have_clear_next_steps(self):
        with patch.object(runner.db, "business_date", return_value=TODAY), patch.object(runner, "notify") as notify:
            runner.daily_summary([{"name": "虚构账号", "checkin_status": "retry", "checkin_message": "查询临时失败",
                                  "extra_signs": [{"key": "app", "status": "busy"}],
                                  "profile_warnings": ["余额暂未刷新"]}])
        text = notify.call_args.args[0]
        self.assertIn("小程序⚠️查询临时失败", text)
        self.assertIn("尚未提交，可稍后重试", text)
        self.assertIn("App⏳执行中", text)
        self.assertIn("提醒⚠️余额暂未刷新", text)


if __name__ == "__main__":
    unittest.main()
