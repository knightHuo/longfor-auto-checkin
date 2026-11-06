"""查询失败与提交不确定的边界，全部使用虚构响应。"""

import unittest
from unittest.mock import Mock, patch

from app import longhu


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


if __name__ == "__main__":
    unittest.main()
