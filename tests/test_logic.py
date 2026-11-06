"""业务状态和凭据保护回归测试；全部使用虚构数据，不连接龙湖。"""

import json
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import requests

from app import longhu, parser, runner


ACCOUNT = {
    "id": 1, "name": "测试账号", "token": "fake-login-token-0123456789",
    "dx_token": "fake-device-token-0123456789", "user_agent": "test-agent",
    "api_key": "fake-api-key-0123456789",
}


class CheckinTests(unittest.TestCase):
    def call_checkin(self, states, response, activity=None):
        with patch.object(longhu, "sign_state", side_effect=states) as state, \
                patch.object(longhu, "_request", return_value=response) as request:
            result = longhu.checkin(ACCOUNT, activity or longhu.ACTIVITY_NO)
        return result, state, request

    def test_unknown_before_never_submits(self):
        result, _, request = self.call_checkin([None], {})
        self.assertEqual(result[0], "retry")
        request.assert_not_called()

    def test_already_signed_never_submits(self):
        result, _, request = self.call_checkin([True], {})
        self.assertEqual(result[0], "already")
        request.assert_not_called()

    def test_missing_calendar_after_submit_is_not_success(self):
        for popup in (0, 1):
            with self.subTest(popup=popup):
                result, _, request = self.call_checkin(
                    [False, None], {"code": "0000", "data": {"is_popup": popup}})
                self.assertEqual(result[0], "todo")
                self.assertEqual(request.call_count, 1)

    def test_calendar_still_unsigned_after_success_is_pending(self):
        result, _, _ = self.call_checkin([False, False], {"code": "0000", "data": {}})
        self.assertEqual(result[0], "todo")

    def test_each_activity_uses_its_own_calendar(self):
        for activity in (longhu.ACTIVITY_NO, longhu.APP_ACTIVITY_NO):
            with self.subTest(activity=activity):
                result, state, request = self.call_checkin(
                    [False, True], {"code": "0000", "data": {}}, activity)
                self.assertEqual(result[0], "ok")
                for call in state.call_args_list:
                    self.assertEqual(call.args[1], activity)
                self.assertEqual(request.call_args.args[3], {"activity_no": activity})

    def test_authentication_failure_is_not_already(self):
        result, _, _ = self.call_checkin([False], {"code": "801001", "message": "未登录"})
        self.assertEqual(result[0], "todo")
        self.assertIn("鉴权失败", result[1])

    def test_risk_does_not_repeat_without_solver(self):
        with patch.object(longhu, "_solve_captcha", return_value=(None, "off")):
            result, _, request = self.call_checkin(
                [False, False], {"code": "8040012", "message": "需要验证"})
        self.assertEqual(result[0], "risk")
        self.assertEqual(request.call_count, 1)

    def test_network_failure_after_submit_is_pending_and_redacted(self):
        with patch.object(longhu, "sign_state", return_value=False), \
                patch.object(longhu, "_request", side_effect=longhu.LonghuError(ACCOUNT["token"])):
            result = longhu.checkin(ACCOUNT)
        self.assertEqual(result[0], "todo")
        self.assertNotIn(ACCOUNT["token"], result[1])

    def test_unparseable_submit_response_is_pending(self):
        result, _, request = self.call_checkin(
            [False, False], {"code": "parse_error", "message": "格式异常"})
        self.assertEqual(result[0], "todo")
        self.assertEqual(request.call_count, 1)

    def test_captcha_retry_unknown_is_pending(self):
        with patch.object(longhu, "sign_state", side_effect=[False, False, None]), \
                patch.object(longhu, "_solve_captcha", return_value=("fake-captcha-token", "ok")), \
                patch.object(longhu, "_request", side_effect=[
                    {"code": "8040012"}, {"code": "0000", "data": {"is_popup": 1}},
                ]) as request:
            result = longhu.checkin(ACCOUNT)
        self.assertEqual(result[0], "todo")
        self.assertEqual(request.call_count, 2)

    def test_calendar_unknown_status_is_unknown(self):
        now = datetime.now(timezone.utc)
        china = now.astimezone(longhu.timezone(longhu.timedelta(hours=8)))
        payload = {"code": "0000", "data": {
            "current_date_time": int(now.timestamp() * 1000),
            "seven_days_signs": [{"sign_year": china.year, "sign_month": china.month,
                                  "sign_day": china.day, "sign_status": 999}],
        }}
        with patch.object(longhu, "_request", return_value=payload):
            self.assertIsNone(longhu.sign_state(ACCOUNT))

    def test_requests_exception_does_not_echo_private_url(self):
        session = Mock()
        session.post.side_effect = requests.ConnectionError(
            "https://example.invalid/?token=" + ACCOUNT["token"])
        with patch.object(longhu.requests, "Session", return_value=session):
            with self.assertRaises(longhu.LonghuError) as raised:
                longhu._request("POST", "https://example.invalid", {}, {})
        self.assertNotIn(ACCOUNT["token"], str(raised.exception))
        self.assertIn("ConnectionError", str(raised.exception))

    def test_decode_never_echoes_raw_response(self):
        decoded = longhu._decode(Mock(text="token=" + ACCOUNT["token"]))
        self.assertEqual(decoded["code"], "parse_error")
        self.assertNotIn(ACCOUNT["token"], str(decoded))


class ParserTests(unittest.TestCase):
    def test_curl_preserves_device_fingerprint(self):
        parsed = parser.parse_curl(
            "curl 'https://gw2c-hw-open.longfor.com/signature/clock' "
            "-H 'X-LF-UserToken: fake-login-token-0123456789' "
            "-H 'X-LF-DXRisk-Token: fake-device-token-0123456789'")
        self.assertEqual(parsed["token"], ACCOUNT["token"])
        self.assertEqual(parsed["dx_token"], ACCOUNT["dx_token"])

    def test_har_preserves_newest_device_and_rejects_lookalike_host(self):
        def entry(host, stamp, device):
            return {"startedDateTime": stamp, "request": {
                "url": "https://" + host + "/signature/clock",
                "headers": [{"name": "X-LF-UserToken", "value": ACCOUNT["token"]},
                            {"name": "X-LF-DXRisk-Token", "value": device}],
            }}
        payload = {"log": {"entries": [
            entry("gw2c-hw-open.longfor.com", "2026-01-01T00:00:00Z", "old-device"),
            entry("gw2c-hw-open.longfor.com", "2026-01-02T00:00:00Z", ACCOUNT["dx_token"]),
            entry("longfor.com.example.invalid", "2026-01-03T00:00:00Z", "wrong-device"),
        ]}}
        self.assertEqual(parser.parse_har(json.dumps(payload))["dx_token"], ACCOUNT["dx_token"])

    def test_token_only_has_no_device_fingerprint(self):
        self.assertIsNone(parser.parse_token("0123456789abcdef0123456789abcdef")["dx_token"])


class RunnerTests(unittest.TestCase):
    def mocked_runner(self, stack, account=None, refresh=None, checkin=None):
        account = dict(ACCOUNT if account is None else account)
        db_mock = stack.enter_context(patch.object(runner, "db"))
        db_mock.list_accounts.return_value = [account]
        db_mock.get_settings.return_value = {"app_checkin_mode": "auto"}
        db_mock.get_account.side_effect = lambda account_id: {**account, "expire_at": None}
        db_mock.now_iso.return_value = "2026-01-01T00:00:00+08:00"
        stack.enter_context(patch.object(longhu, "refresh_token",
                                        return_value=refresh or (True, account["token"])))
        stack.enter_context(patch.object(longhu, "fetch_profile", return_value={}))
        check = stack.enter_context(patch.object(longhu, "checkin",
                                                side_effect=checkin or [("ok", "已核实"), ("risk", "需验证")] ))
        notify = stack.enter_context(patch.object(runner, "notify"))
        return account, db_mock, check, notify

    def test_successful_refresh_never_sends_token_in_failure_notice(self):
        new_token = "fake-new-login-token-0123456789"
        with ExitStack() as stack:
            account, db_mock, _, notify = self.mocked_runner(
                stack, refresh=(True, new_token),
                checkin=[("fail", "上游回显 " + new_token), ("ok", "已核实")])
            result = runner.run_account(account)
            notify.assert_not_called()
            runner.daily_summary([result])
        self.assertEqual(result["checkin_status"], "fail")
        self.assertEqual(result["extra_signs"][0]["status"], "ok")
        logged = str(db_mock.add_log.call_args_list) + str(notify.call_args_list) + str(result)
        self.assertNotIn(new_token, logged)
        self.assertNotIn(ACCOUNT["token"], logged)
        self.assertNotIn("登录续期", notify.call_args.args[0])

    def test_missing_fingerprint_never_submits_either_entry(self):
        with ExitStack() as stack:
            account, _, check, _ = self.mocked_runner(stack, {**ACCOUNT, "dx_token": None})
            result = runner.run_account(account)
        check.assert_not_called()
        self.assertEqual(result["checkin_status"], "todo")
        self.assertEqual(result["extra_signs"][0]["status"], "todo")

    def test_two_entries_keep_separate_statuses_and_activity_numbers(self):
        with ExitStack() as stack:
            account, _, check, _ = self.mocked_runner(stack)
            result = runner.run_account(account)
        self.assertEqual(result["checkin_status"], "ok")
        self.assertEqual(result["extra_signs"][0]["status"], "risk")
        self.assertEqual([call.args[1] for call in check.call_args_list],
                         [longhu.ACTIVITY_NO, longhu.APP_ACTIVITY_NO])

    def test_pending_state_is_in_daily_notice(self):
        with patch.object(runner, "notify") as notify:
            runner.daily_summary([{"name": "测试账号", "checkin_status": "todo",
                                   "checkin_message": "请人工确认"}])
        notify.assert_called_once()
        self.assertIn("请人工确认", notify.call_args.args[0])

    def test_webhook_errors_do_not_log_access_url(self):
        private_url = "https://example.invalid/send?access_token=fake-webhook-secret"
        session = Mock()
        session.post.side_effect = requests.ConnectionError(private_url)
        with patch.object(runner.db, "get_settings", return_value={"webhook_url": private_url}), \
                patch.object(runner.requests, "Session", return_value=session), \
                self.assertLogs(runner.logger, "WARNING") as logs:
            runner.notify("测试通知")
        self.assertNotIn("fake-webhook-secret", str(logs.output))


if __name__ == "__main__":
    unittest.main()
