"""查询失败与提交不确定的边界，全部使用虚构响应。"""

import unittest
from unittest.mock import Mock, patch

from app import longhu, scheduler


ACCOUNT = {"token": "fake-review-token", "dx_token": "fake-review-device", "dx_source": "3"}


class ReadFailureTests(unittest.TestCase):
    def test_expired_login_is_manual_work_instead_of_an_automatic_retry(self):
        with patch.object(longhu, "_request", return_value={"code": "801001", "message": "未登录"}) as request:
            status, message = longhu.checkin(ACCOUNT)
        self.assertEqual(status, "todo")
        self.assertIn("重新导入", message)
        self.assertEqual(request.call_count, 1)
        self.assertTrue(request.call_args.args[1].endswith("/signature/page-info"))

    def test_failed_intent_write_prevents_the_submission(self):
        callback = Mock(side_effect=RuntimeError("synthetic storage unavailable"))
        with patch.object(longhu, "sign_state", return_value=False), patch.object(longhu, "_request") as request:
            with self.assertRaises(RuntimeError):
                longhu.checkin(ACCOUNT, on_submit=callback)
        callback.assert_called_once()
        request.assert_not_called()

    def test_persisted_intent_happens_before_the_http_submission(self):
        order = []
        def request(*args, **kwargs):
            order.append("clock")
            return {"code": "0000"}
        with patch.object(longhu, "sign_state", side_effect=[False, True]), \
                patch.object(longhu, "_request", side_effect=request):
            status, _ = longhu.checkin(ACCOUNT, on_submit=lambda: order.append("persist"))
        self.assertEqual(order, ["persist", "clock"])
        self.assertEqual(status, "ok")

    def test_query_failure_can_retry_without_any_submission(self):
        with patch.object(longhu, "_request", side_effect=longhu.LonghuError("查询超时")) as request:
            status, _ = longhu.checkin(ACCOUNT)
        self.assertEqual(status, "retry")
        self.assertEqual(request.call_count, 1)
        self.assertTrue(request.call_args.args[1].endswith("/signature/page-info"))

    def test_failure_after_submission_remains_unknown(self):
        with patch.object(longhu, "sign_state", return_value=False), \
                patch.object(longhu, "_request", side_effect=longhu.LonghuError("超时")) as request:
            status, _ = longhu.checkin(ACCOUNT)
        self.assertEqual(status, "todo")
        self.assertEqual(request.call_count, 1)

    def test_auth_failure_after_submission_uses_authentication_result(self):
        error = longhu.LonghuAuthError("登录已失效，请重新导入")
        with patch.object(longhu, "sign_state", side_effect=[False, error]), \
                patch.object(longhu, "_request", return_value={"code": "0000"}) as request:
            status, message = longhu.checkin(ACCOUNT)
        self.assertTrue(longhu.is_auth_failure(status, message))
        self.assertTrue(message.startswith("鉴权失败："))
        request.assert_called_once()


class PartialProfileTests(unittest.TestCase):
    def test_malformed_profile_does_not_raise_an_exception_with_response_content(self):
        with patch.object(longhu, "_request", return_value={"code": "0000", "data": [ACCOUNT["token"]]}):
            with self.assertRaises(longhu.LonghuError) as raised:
                longhu.fetch_profile(ACCOUNT)
        self.assertNotIn(ACCOUNT["token"], str(raised.exception))

    def test_missing_or_invalid_growth_is_not_converted_to_zero(self):
        for raw in (None, "private-response-value", [], True, "NaN", 1.5):
            with self.subTest(raw=raw), patch.object(longhu, "_request", side_effect=[
                {"code": "0000", "data": {"growthValue": raw}},
                {"code": "0000", "data": {"balance": 5}},
            ]):
                profile = longhu.fetch_profile(ACCOUNT)
                self.assertNotIn("growth_value", profile)
                self.assertEqual(profile["balance"], 5)
                self.assertIn("_warnings", profile)

    def profile(self, balance_response):
        with patch.object(longhu, "_request", side_effect=[
            {"code": "0000", "data": {"growthValue": 100, "levelName": "V1"}},
            balance_response,
        ]):
            return longhu.fetch_profile(ACCOUNT)

    def test_balance_failure_does_not_invent_a_zero(self):
        for response in (longhu.LonghuError("超时"), {"code": "500"},
                         {"code": "0000", "data": {}},
                         {"code": "0000", "data": {"balance": None}}):
            with self.subTest(response=type(response).__name__):
                profile = self.profile(response)
                self.assertNotIn("balance", profile)
                self.assertIn("_warnings", profile)
                self.assertEqual(profile["growth_value"], 100)

    def test_legitimate_zero_is_retained(self):
        profile = self.profile({"code": "0000", "data": {"balance": 0, "expiringLz": "0"}})
        self.assertEqual(profile["balance"], 0)
        self.assertNotIn("_warnings", profile)

    def test_invalid_balance_does_not_replace_a_saved_value(self):
        for raw in ("invalid", "NaN", "Infinity", True, [], {}):
            with self.subTest(raw=raw):
                self.assertNotIn("balance", self.profile({"code": "0000", "data": {"balance": raw}}))

    def test_definite_balance_auth_failure_is_exposed_to_status_queries(self):
        for response in (longhu.LonghuAuthError("登录已失效"), {"code": "801001"}):
            with self.subTest(response=response), self.assertRaises(longhu.LonghuAuthError):
                self.profile(response)


class UpstreamMessageTests(unittest.TestCase):
    def test_refresh_returns_safe_hints_for_both_steps(self):
        echo = "Bearer fake-unknown-bearer 0rYTQ6JpNw2Z"
        key_exchange = {"code": "0000", "data": {"key": "fake-key-exchange"}}
        for responses, action, code in (
            ([{"code": "5001", "message": echo}], "token2key", "5001"),
            ([key_exchange, {"code": "0rYTQ6JpNw2Z", "message": echo}], "key2token", "invalid"),
            ([key_exchange, {"code": "123456", "message": "验证码 123456"}], "key2token", "invalid"),
            ([{"data": {"key": "5001"}}, {"code": "5001"}], "key2token", "invalid"),
        ):
            with self.subTest(action=action), patch.object(longhu, "_request", side_effect=responses):
                ok, message = longhu.refresh_token(ACCOUNT)
            self.assertFalse(ok)
            self.assertIn(action, message)
            self.assertIn(f"code={code}", message)
            self.assertNotIn("Bearer", message)
            self.assertNotIn("0rYTQ6JpNw2Z", message)
            self.assertNotIn("fake-key-exchange", message)
            self.assertNotIn("123456", message)

    def test_profile_failure_uses_safe_hint_and_diagnostic_code(self):
        for code, diagnostic in (("5001", "5001"), ("0rYTQ6JpNw2Z", "invalid"), ("123456", "invalid")):
            response = {"code": code, "message": "Bearer fake-unknown-bearer 0rYTQ6JpNw2Z"}
            with self.subTest(code=code), patch.object(longhu, "_request", return_value=response):
                with self.assertRaisesRegex(longhu.LonghuError, f"^用户信息查询失败（code={diagnostic}）") as raised:
                    longhu.fetch_profile(ACCOUNT)
            self.assertNotIn("Bearer", str(raised.exception))
            self.assertNotIn("0rYTQ6JpNw2Z", str(raised.exception))
            self.assertNotIn("123456", str(raised.exception))


class TransportTests(unittest.TestCase):
    def test_credential_headers_are_not_forwarded_after_redirects(self):
        session = Mock()
        session.get.return_value = Mock(status_code=302, text="sensitive-redirect-response")
        with patch.object(longhu.requests, "Session", return_value=session):
            with self.assertRaises(longhu.LonghuError) as raised:
                longhu._request("GET", "https://example.invalid/api", {"token": ACCOUNT["token"]})
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
        self.assertIn("302", str(raised.exception))
        self.assertNotIn("sensitive", str(raised.exception))
        session.close.assert_called_once()

    def test_http_authentication_failure_stops_query_retries(self):
        session = Mock()
        session.post.return_value = Mock(status_code=401, text=ACCOUNT["token"])
        with patch.object(longhu.requests, "Session", return_value=session):
            status, message = longhu.checkin(ACCOUNT)
        self.assertEqual(status, "todo")
        self.assertEqual(session.post.call_count, 1)
        self.assertNotIn(ACCOUNT["token"], message)

    def test_calendar_and_clock_diagnostics_are_logged_and_redacted(self):
        echo = "synthetic upstream text Authorization: Bearer fake-unknown-bearer"
        with patch.object(longhu, "_request",
                          return_value={"code": "5001", "message": f"{echo} {ACCOUNT['token']}"}), \
                self.assertLogs(longhu.logger, "WARNING") as logs:
            self.assertIsNone(longhu.sign_state(ACCOUNT, longhu.APP_ACTIVITY_NO))
        joined = "\n".join(logs.output)
        self.assertIn("entry=app", joined)
        self.assertIn("code=5001", joined)
        self.assertIn("elapsed_ms=", joined)
        self.assertNotIn(ACCOUNT["token"], joined)
        self.assertNotIn("fake-unknown-bearer", joined)
        self.assertNotIn("synthetic upstream text", joined)
        self.assertNotIn("message=", joined)

        with patch.object(longhu, "sign_state", side_effect=[False, False]), \
                patch.object(longhu, "_solve_captcha", return_value=(None, "off")), \
                patch.object(longhu, "_request",
                             return_value={"code": "8040012", "message": f"{echo} {ACCOUNT['dx_token']}"}), \
                self.assertLogs(longhu.logger, "WARNING") as logs:
            status, message = longhu.checkin(ACCOUNT, longhu.ACTIVITY_NO)
        joined = "\n".join(logs.output)
        self.assertEqual(status, "risk")
        self.assertIn("entry=mini", joined)
        self.assertIn("code=8040012", joined)
        self.assertIn("calendar_after=False", joined)
        self.assertNotIn(ACCOUNT["dx_token"], joined + message)
        self.assertNotIn("fake-unknown-bearer", joined + message)
        self.assertNotIn("synthetic upstream text", joined + message)

    def test_redact_removes_entire_authorization_credentials(self):
        for header, secret in (
            ("Authorization: Bearer {}", "fake-bearer-credential"),
            ("authorization: Basic {}\r\n", "ZmFrZTpzZWNyZXQ="),
            ('"Authorization": "bEaReR {}"', "fake-json-credential"),
            ("Proxy-Authorization: Token {}", "fake-proxy-credential"),
        ):
            with self.subTest(header=header):
                sanitized = longhu.redact(header.format(secret))
                self.assertIn("[已隐藏]", sanitized)
                self.assertNotIn(secret, sanitized)
                self.assertEqual(sanitized, longhu.redact(sanitized))

    def test_diagnostics_reject_unsafe_business_codes(self):
        for code in ("Authorization: Basic ZmFrZTpzZWNyZXQ=", "line\nbreak", "代码", "x" * 25,
                     "0rYTQ6JpNw2Z", ACCOUNT["token"]):
            with self.subTest(code=code):
                payload = {"code": code}
                with patch.object(longhu, "_request", return_value=payload), \
                        self.assertLogs(longhu.logger, "WARNING") as logs:
                    self.assertIsNone(longhu.sign_state(ACCOUNT))
                joined = "\n".join(logs.output)
                self.assertIn("code=invalid", joined)
                self.assertNotIn(code, joined)

                with patch.object(longhu, "_request", return_value=payload), \
                        patch.object(longhu, "sign_state", return_value=False), \
                        self.assertLogs(longhu.logger, "WARNING") as logs:
                    status, message = longhu.checkin(ACCOUNT)
                self.assertEqual(status, "todo")
                self.assertFalse(scheduler._needs_retry({"checkin_status": status}))
                joined = "\n".join(logs.output)
                self.assertIn("code=invalid", joined)
                self.assertNotIn(code, joined + message)

    def test_unexplained_submit_response_uses_only_calendar_confirmation(self):
        for code in (None, "", "parse_error", "unknown-c0de"):
            for confirmed in (False, None, True):
                with self.subTest(code=code, confirmed=confirmed), \
                        patch.object(longhu, "_request", return_value={"code": code}) as request, \
                        patch.object(longhu, "sign_state", side_effect=[False, confirmed]):
                    status, _ = longhu.checkin(ACCOUNT)
                self.assertEqual(status, "ok" if confirmed is True else "todo")
                self.assertFalse(scheduler._needs_retry({"checkin_status": "ok", "extra_signs": [
                    {"key": "app", "status": status},
                ]}))
                request.assert_called_once()

    def test_request_failure_diagnostics_do_not_echo_exception_text(self):
        secret = "fake-basic-credential"
        error = longhu.LonghuError(f"synthetic upstream text Authorization: Basic {secret}")
        with patch.object(longhu, "_request", side_effect=error), \
                self.assertLogs(longhu.logger, "WARNING") as logs:
            self.assertIsNone(longhu.sign_state(ACCOUNT))
            with patch.object(longhu, "sign_state", return_value=False):
                status, message = longhu.checkin(ACCOUNT)
        joined = "\n".join(logs.output)
        self.assertIn("日历查询失败", joined)
        self.assertIn("签到提交异常", joined)
        self.assertNotIn("synthetic upstream text", joined)
        self.assertNotIn(secret, joined + message)
        self.assertEqual(status, "todo")


if __name__ == "__main__":
    unittest.main()
