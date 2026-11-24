"""账号并发、独立入口状态及部分资料更新；不访问真实账号或数据库。"""

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from pathlib import Path
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
        database.business_date.side_effect = lambda: actual_db.run_date(database.now_iso.return_value)
        database.run_date.side_effect = actual_db.run_date
        database.update_account.side_effect = lambda account_id, **fields: store.update(fields)
        stack.enter_context(patch.object(longhu, "db", database))
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

    def test_mini_clock_http_401_stops_app_and_profile(self):
        real_checkin = longhu.checkin
        session = Mock()
        session.post.return_value = Mock(status_code=401, text="Bearer fake-unknown-login-token")
        with runner_env() as env, patch.object(longhu.requests, "Session", return_value=session):
            env.checkin.side_effect = real_checkin
            result = runner.run_account(dict(ACCOUNT))
            self.assertEqual(result["checkin_status"], "todo")
            self.assertTrue(result["checkin_message"].startswith("鉴权失败："))
            self.assertEqual(result["extra_signs"][0]["status"], "todo")
            self.assertNotIn("fake-unknown-login-token", str(result) + str(env.db.add_log.call_args_list))
            env.checkin.assert_called_once()
            env.state.assert_called_once_with(unittest.mock.ANY, longhu.ACTIVITY_NO)
            session.post.assert_called_once()
            self.assertTrue(session.post.call_args.args[0].endswith("/signature/clock"))
            self.assertEqual(session.post.call_args.kwargs["json"]["activity_no"], longhu.ACTIVITY_NO)
            env.fetch.assert_not_called()

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

    def test_mini_pause_halts_same_round_app_submission_and_uses_readonly_check(self):
        for mini_status in ("risk", "todo"):
            for app_signed in (False, None, True):
                with self.subTest(mini_status=mini_status, app_signed=app_signed), runner_env() as env:
                    env.checkin.return_value = (mini_status, "小程序待确认")
                    env.state.return_value = app_signed
                    result = runner.run_account(dict(ACCOUNT))
                    expected_status = "already" if app_signed is True else mini_status
                    self.assertEqual(result["checkin_status"], mini_status)
                    self.assertEqual(result["extra_signs"][0]["status"], expected_status)
                    self.assertEqual(env.store["app_last_status"], expected_status)
                    self.assertEqual(env.store["app_last_run_at"], NOW)
                    self.assertEqual([call.args[1] for call in env.checkin.call_args_list], [longhu.ACTIVITY_NO])
                    env.state.assert_called_once_with(unittest.mock.ANY, longhu.APP_ACTIVITY_NO)

    def test_risk_in_mini_keeps_app_off_as_skipped_without_extra_query(self):
        with runner_env() as env:
            env.checkin.return_value = ("risk", "需要人机验证")
            result = runner.run_account(dict(ACCOUNT), app_mode="off")
            self.assertEqual(result["checkin_status"], "risk")
            self.assertEqual(result["extra_signs"][0]["status"], "skipped")
            env.state.assert_not_called()

    def test_existing_mini_risk_also_blocks_app_retry_from_submitting(self):
        with runner_env({"last_status": "risk", "last_message": "请完成验证", "last_run_at": NOW,
                         "app_last_status": "fail", "app_last_message": "App 临时故障",
                         "app_last_run_at": NOW}) as env:
            result = runner.run_account(dict(ACCOUNT))
            self.assertEqual(result["checkin_status"], "risk")
            self.assertEqual(result["extra_signs"][0]["status"], "risk")
            env.checkin.assert_not_called()
            self.assertEqual([call.args[1] for call in env.state.call_args_list],
                             [longhu.ACTIVITY_NO, longhu.APP_ACTIVITY_NO])

    def test_app_can_submit_if_mini_pause_recheck_confirms_signed_and_app_is_not_paused(self):
        for paused in ("risk", "todo"):
            with self.subTest(paused=paused), runner_env({"last_status": paused, "last_run_at": NOW}) as env:
                env.state.return_value = True
                result = runner.run_account(dict(ACCOUNT))
                self.assertEqual(result["checkin_status"], "already")
                self.assertEqual(result["extra_signs"][0]["status"], "ok")
                self.assertEqual(env.checkin.call_count, 1)
                self.assertEqual(env.checkin.call_args.args[1], longhu.APP_ACTIVITY_NO)

    def test_off_does_not_erase_app_pause_before_switching_back_to_auto(self):
        for paused in ("risk", "todo"):
            with self.subTest(paused=paused), runner_env() as env:
                env.checkin.side_effect = [("ok", "小程序已签"), (paused, "App 待人工确认"),
                                           ("already", "小程序已签"), ("already", "小程序已签")]
                runner.run_account(dict(ACCOUNT), app_mode="auto")
                before = {key: value for key, value in env.store.items() if key.startswith("app_last_")}
                off = runner.run_account(dict(ACCOUNT), app_mode="off")
                self.assertEqual(off["extra_signs"][0]["status"], "skipped")
                self.assertEqual({key: env.store[key] for key in before}, before)
                env.state.assert_not_called()

                resumed = runner.run_account(dict(ACCOUNT), app_mode="auto")
                self.assertEqual(resumed["extra_signs"][0]["status"], paused)
                env.state.assert_called_once_with(unittest.mock.ANY, longhu.APP_ACTIVITY_NO)
                self.assertEqual([call.args[1] for call in env.checkin.call_args_list].count(
                    longhu.APP_ACTIVITY_NO), 1)

    def test_mini_risk_finishing_after_midnight_does_not_freeze_new_day_app(self):
        yesterday = TODAY + "T23:59:59+08:00"
        after_midnight = "2026-11-08T00:00:01+08:00"
        for recover_pause in (False, True):
            account = {"last_status": "risk", "last_run_at": yesterday} if recover_pause else {}
            with self.subTest(recover_pause=recover_pause), runner_env(account) as env:
                env.db.now_iso.return_value = yesterday

                def mini_review_crosses_midnight(*args):
                    env.db.now_iso.return_value = after_midnight
                    return False

                def checkin(account, activity, label, on_submit):
                    on_submit()
                    if activity == longhu.ACTIVITY_NO:
                        mini_review_crosses_midnight()
                        return "risk", "小程序需要验证"
                    return "ok", "App 已按次日日历确认"

                env.state.side_effect = mini_review_crosses_midnight
                env.checkin.side_effect = checkin
                result = runner.run_account(dict(ACCOUNT))
                self.assertEqual(result["extra_signs"][0]["status"], "ok")
                self.assertEqual((env.store["last_status"], env.store["last_run_at"]), ("risk", yesterday))
                self.assertEqual((env.store["app_last_status"], env.store["app_last_run_at"]), ("ok", after_midnight))
                self.assertEqual(env.checkin.call_args.args[1], longhu.APP_ACTIVITY_NO)
                self.assertEqual(env.state.call_count, int(recover_pause))

    def test_calendar_from_before_midnight_cannot_authorize_a_clock_after_midnight(self):
        real_checkin = longhu.checkin
        yesterday = TODAY + "T23:59:59+08:00"
        with runner_env() as env, patch.object(longhu, "_request") as request:
            env.checkin.side_effect = real_checkin
            env.db.now_iso.return_value = yesterday

            def calendar(*args):
                env.db.now_iso.return_value = "2026-11-08T00:00:01+08:00"
                return False

            env.state.side_effect = calendar
            result = runner.run_account(dict(ACCOUNT), app_mode="off")
            request.assert_not_called()
            self.assertEqual(result["checkin_status"], "retry")
            self.assertIn("重新查询当日日历", result["checkin_message"])
            self.assertEqual(env.store["last_run_at"], yesterday)

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
            env.checkin.assert_not_called()
            self.assertEqual(result["extra_signs"][0]["status"], "todo")

    def test_pending_recheck_auth_error_stops_all_later_upstream_calls(self):
        for prefix in ("", "app_"):
            previous = {f"{prefix}last_status": "todo", f"{prefix}last_run_at": NOW}
            with self.subTest(prefix=prefix), runner_env(previous) as env:
                env.state.side_effect = longhu.LonghuAuthError(
                    "登录已失效 Authorization: Bearer fake-auth-echo")
                result = runner.run_account(dict(ACCOUNT))
                self.assertEqual(env.store[f"{prefix}last_status"], "todo")
                self.assertEqual(result["extra_signs"][0]["status"], "todo")
                self.assertIn("登录已失效", result["extra_signs"][0]["message"])
                self.assertNotIn("fake-auth-echo", str(result))
                self.assertEqual(env.checkin.call_count, int(prefix == "app_"))
                self.assertEqual(env.state.call_count, 1)
                env.fetch.assert_not_called()

    def test_app_risk_halt_auth_error_stops_profile_refresh(self):
        with runner_env() as env:
            env.checkin.return_value = ("risk", "小程序需要验证")
            env.state.side_effect = longhu.LonghuAuthError("登录已失效")
            result = runner.run_account(dict(ACCOUNT))
            self.assertEqual(result["checkin_status"], "risk")
            self.assertEqual(result["extra_signs"][0]["status"], "todo")
            self.assertEqual(env.store["app_last_status"], "todo")
            env.checkin.assert_called_once()
            env.state.assert_called_once_with(unittest.mock.ANY, longhu.APP_ACTIVITY_NO)
            env.fetch.assert_not_called()

    def test_app_off_is_persisted_as_skipped(self):
        with runner_env() as env:
            result = runner.run_account(dict(ACCOUNT), app_mode="off")
            self.assertEqual(result["extra_signs"][0]["status"], "skipped")
            self.assertEqual(env.store["app_last_status"], "skipped")
            self.assertEqual(env.store["app_last_run_at"], NOW)
            self.assertEqual(env.checkin.call_count, 1)


class PersistedPauseTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        directory = stack.enter_context(tempfile.TemporaryDirectory())
        stack.enter_context(patch.object(actual_db, "DB_PATH", str(Path(directory) / "test.db")))
        self.clock = stack.enter_context(patch.object(actual_db, "now_iso", return_value=NOW))
        stack.enter_context(patch.object(actual_db, "business_date", side_effect=lambda: actual_db.run_date(self.clock.return_value)))
        actual_db.init()
        account_id = actual_db.create_account(ACCOUNT)
        self.account = actual_db.get_account(account_id)
        stack.enter_context(patch.object(longhu, "refresh_token", return_value=(True, ACCOUNT["token"])))
        self.fetch = stack.enter_context(patch.object(longhu, "fetch_profile", return_value={}))
        self.state = stack.enter_context(patch.object(longhu, "sign_state", return_value=False))
        self.request = stack.enter_context(patch.object(longhu, "_request", return_value={"code": "801810"}))

    def test_restart_after_mini_risk_is_saved_does_not_submit_app(self):
        class SimulatedProcessExit(BaseException):
            pass

        with patch.object(runner, "_halt_after_mini_pause", side_effect=SimulatedProcessExit):
            with self.assertRaises(SimulatedProcessExit):
                runner.run_account(self.account)
        saved = actual_db.get_account(self.account["id"])
        self.assertEqual((saved["last_status"], saved["last_run_at"]), ("risk", NOW))
        self.assertIsNone(saved["app_last_run_at"])
        self.fetch.assert_not_called()

        self.state.reset_mock()
        result = runner.run_account(self.account)
        saved = actual_db.get_account(self.account["id"])
        self.assertEqual(result["extra_signs"][0]["status"], "risk")
        self.assertEqual((saved["app_last_status"], saved["app_last_run_at"]), ("risk", NOW))
        self.assertEqual([call.args[1] for call in self.state.call_args_list],
                         [longhu.ACTIVITY_NO, longhu.APP_ACTIVITY_NO])
        self.request.assert_called_once()
        self.assertTrue(self.request.call_args.args[1].endswith("/signature/clock"))
        self.assertEqual(self.request.call_args.args[3]["activity_no"], longhu.ACTIVITY_NO)

    def test_restart_during_mini_post_clock_risk_review_only_reads_both_calendars(self):
        class SimulatedProcessExit(BaseException):
            pass

        self.state.side_effect = [False, SimulatedProcessExit]
        with self.assertRaises(SimulatedProcessExit):
            runner.run_account(self.account)
        saved = actual_db.get_account(self.account["id"])
        self.assertEqual((saved["last_status"], saved["last_run_at"]), ("todo", NOW))
        self.assertIn("提交阶段", saved["last_message"])
        self.assertIsNone(saved["app_last_run_at"])

        self.state.reset_mock(side_effect=True)
        result = runner.run_account(self.account)
        saved = actual_db.get_account(self.account["id"])
        self.assertEqual(result["checkin_status"], "todo")
        self.assertEqual(result["extra_signs"][0]["status"], "todo")
        self.assertEqual((saved["app_last_status"], saved["app_last_run_at"]), ("todo", NOW))
        self.assertEqual([call.args[1] for call in self.state.call_args_list],
                         [longhu.ACTIVITY_NO, longhu.APP_ACTIVITY_NO])
        self.request.assert_called_once()
        self.assertEqual(self.request.call_args.args[3]["activity_no"], longhu.ACTIVITY_NO)

    def test_midnight_during_intent_write_aborts_clock_and_preserves_next_day_execution(self):
        yesterday = TODAY + "T23:59:57+08:00"
        midnight = "2026-11-08T00:00:00+08:00"
        self.clock.return_value = yesterday
        write_account = actual_db.update_account

        def update_then_cross(account_id, **fields):
            write_account(account_id, **fields)
            if fields.get("last_status") == "todo":
                self.clock.return_value = midnight

        with patch.object(actual_db, "update_account", side_effect=update_then_cross):
            result = runner.run_account(self.account, app_mode="off")
        self.request.assert_not_called()
        self.assertEqual(result["checkin_status"], "retry")
        saved = actual_db.get_account(self.account["id"])
        self.assertEqual((saved["last_status"], saved["last_run_at"]), ("retry", yesterday))

        self.request.return_value = {"code": "0000"}
        self.state.side_effect = [False, True, False, True]
        next_day = runner.run_account(self.account)
        self.assertEqual(next_day["checkin_status"], "ok")
        self.assertEqual(next_day["extra_signs"][0]["status"], "ok")
        self.assertEqual([call.args[3]["activity_no"] for call in self.request.call_args_list],
                         [longhu.ACTIVITY_NO, longhu.APP_ACTIVITY_NO])
        saved = actual_db.get_account(self.account["id"])
        self.assertEqual((saved["last_run_at"], saved["app_last_run_at"]), (midnight, midnight))

    def test_clock_response_after_midnight_preserves_submission_timestamp_for_each_entry(self):
        entry_start = TODAY + "T23:59:55+08:00"
        intent_stamp = TODAY + "T23:59:59+08:00"
        midnight = "2026-11-08T00:00:00+08:00"
        for key, activity_no in (("mini", longhu.ACTIVITY_NO), ("app", longhu.APP_ACTIVITY_NO)):
            with self.subTest(key=key):
                self.clock.return_value = entry_start

                def preflight(account, activity):
                    if activity != activity_no:
                        return True
                    self.clock.return_value = intent_stamp
                    return False

                def delayed_response(*args):
                    self.clock.return_value = midnight
                    return {"code": "0000"}

                self.state.reset_mock()
                self.request.reset_mock()
                self.state.side_effect = preflight
                self.request.side_effect = delayed_response
                runner.run_account(self.account, app_mode="off" if key == "mini" else "auto")
                self.request.assert_called_once()
                self.assertEqual(self.request.call_args.args[3]["activity_no"], activity_no)
                self.assertEqual(sum(call.args[1] == activity_no for call in self.state.call_args_list), 1)
                saved = actual_db.get_account(self.account["id"])
                prefix = "" if key == "mini" else "app_"
                self.assertEqual((saved[f"{prefix}last_status"], saved[f"{prefix}last_run_at"]),
                                 ("todo", intent_stamp))
                actual_db.update_account(self.account["id"], last_status="", app_last_status="")

    def test_mini_and_app_risk_timestamps_are_saved_before_profile_crash(self):
        def crash_after_checking_results(account):
            saved = actual_db.get_account(account["id"])
            self.assertEqual((saved["last_status"], saved["last_run_at"]), ("risk", NOW))
            self.assertEqual((saved["app_last_status"], saved["app_last_run_at"]), ("risk", NOW))
            raise RuntimeError("synthetic unexpected profile error")

        self.fetch.side_effect = crash_after_checking_results
        with self.assertRaisesRegex(RuntimeError, "synthetic unexpected profile error"):
            runner.run_account(self.account)
        self.request.assert_called_once()
        self.assertEqual(self.request.call_args.args[3]["activity_no"], longhu.ACTIVITY_NO)


class ProfileAndSummaryTests(unittest.TestCase):
    def test_refresh_and_profile_rejections_do_not_expose_upstream_text_in_logs(self):
        real_refresh = longhu.refresh_token
        real_fetch = longhu.fetch_profile
        echo = "Bearer fake-unknown-bearer 0rYTQ6JpNw2Z"
        with runner_env() as env, patch.object(longhu, "_request", side_effect=[
            {"code": "5001", "message": echo}, {"code": "0rYTQ6JpNw2Z", "message": echo},
        ]), self.assertLogs(runner.logger, "WARNING") as logs:
            env.refresh.side_effect = real_refresh
            env.fetch.side_effect = real_fetch
            result = runner.run_account(dict(ACCOUNT), do_checkin=False)
            messages = [call.args[4] for call in env.db.add_log.call_args_list]
        self.assertEqual(len(messages), 2)
        self.assertIn("token2key", messages[0])
        self.assertIn("code=5001", messages[0])
        self.assertIn("用户信息查询失败（code=invalid）", messages[1])
        self.assertEqual(result["error"], messages[0])
        output = str(messages) + str(logs.output) + str(result)
        self.assertNotIn("fake-unknown-bearer", output)
        self.assertNotIn("0rYTQ6JpNw2Z", output)

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
            env.checkin.side_effect = [("ok", "小程序已按日历确认"), ("risk", "App 请完成验证")]
            env.fetch.side_effect = RuntimeError("synthetic unexpected profile error")
            with self.assertRaises(RuntimeError):
                runner.run_account(dict(ACCOUNT))
            self.assertEqual((env.store["last_status"], env.store["last_run_at"]), ("ok", NOW))
            self.assertEqual((env.store["app_last_status"], env.store["app_last_run_at"]), ("risk", NOW))
            app_write = next(call.kwargs for call in env.db.update_account.call_args_list
                             if call.kwargs.get("app_last_status") == "risk")
            self.assertEqual(app_write["app_last_run_at"], NOW)

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

    def test_summary_points_mini_and_app_verification_to_their_own_clients(self):
        with patch.object(runner.db, "business_date", return_value=TODAY), patch.object(runner, "notify") as notify:
            runner.daily_summary([{
                "name": "虚构账号",
                "checkin_status": "risk", "checkin_message": "需要人机验证",
                "extra_signs": [{"key": "app", "status": "risk", "message": "已暂停提交"}],
            }])
        text = notify.call_args.args[0]
        self.assertIn("请在官方微信小程序完成验证后确认签到", text)
        self.assertIn("请在官方 App 完成验证后确认签到", text)


if __name__ == "__main__":
    unittest.main()
