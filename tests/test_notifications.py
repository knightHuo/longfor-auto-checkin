"""通知协议、双入口摘要与凭据保护；只使用虚构密钥及 mock 网络。"""

import unittest
from unittest.mock import Mock, patch

import requests

from app import notifications, runner


TOKEN = "synthetic-pushplus-private-token"
SETTINGS = {"notification_provider": "pushplus", "pushplus_token": TOKEN}


class NotificationTests(unittest.TestCase):
    def send_with_response(self, *, status=200, data=None, settings=None):
        response = Mock(status_code=status)
        response.json.return_value = {"code": 200} if data is None else data
        session = Mock()
        session.post.return_value = response
        with patch.object(notifications.requests, "Session", return_value=session):
            result = notifications.send("虚构的签到结果", settings or SETTINGS)
        return result, session

    def test_pushplus_uses_fixed_https_json_and_only_reports_queued(self):
        result, session = self.send_with_response(data={"code": 200, "data": "private-serial"})
        session.post.assert_called_once_with(
            "https://www.pushplus.plus/send",
            json={"token": TOKEN, "title": "龙湖签到", "content": "虚构的签到结果",
                  "template": "txt", "channel": "wechat"},
            timeout=10, allow_redirects=False,
        )
        self.assertFalse(session.trust_env)
        session.close.assert_called_once()
        self.assertEqual(result, {"ok": True, "status": "queued", "message": "已受理，等待投递"})
        self.assertNotIn(TOKEN, str(result))
        self.assertNotIn("private-serial", str(result))

    def test_pushplus_requires_both_http_2xx_and_numeric_code_200(self):
        cases = [(500, {"code": 200}), (302, {"code": 200}),
                 (200, {"code": 0}), (200, {"code": "200"}),
                 (200, {"code": True}), (200, {}), (200, [])]
        for status, data in cases:
            with self.subTest(status=status, data=data):
                result, session = self.send_with_response(status=status, data=data)
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], "failed")
                self.assertEqual(session.post.call_count, 1)

    def test_error_body_and_unknown_error_code_never_reveal_secrets(self):
        result, _ = self.send_with_response(data={"code": TOKEN, "msg": TOKEN, "data": TOKEN})
        self.assertEqual(result["status"], "failed")
        self.assertNotIn(TOKEN, str(result))
        result, _ = self.send_with_response(data={"code": 600, "msg": TOKEN})
        self.assertIn("600", result["message"])
        self.assertNotIn(TOKEN, str(result))

    def test_invalid_json_is_failure_without_response_echo(self):
        session = Mock()
        session.post.return_value.status_code = 200
        session.post.return_value.json.side_effect = ValueError(TOKEN)
        with patch.object(notifications.requests, "Session", return_value=session):
            result = notifications.send("测试", SETTINGS)
        self.assertEqual(result["status"], "failed")
        self.assertNotIn(TOKEN, str(result))
        session.close.assert_called_once()

    def test_network_exception_is_safe_and_never_retried(self):
        session = Mock()
        session.post.side_effect = requests.Timeout("private-url?token=" + TOKEN)
        with patch.object(notifications.requests, "Session", return_value=session):
            result = notifications.send("测试", SETTINGS)
        self.assertEqual(result["status"], "failed")
        self.assertIn("Timeout", result["message"])
        self.assertNotIn(TOKEN, str(result))
        self.assertNotIn("private-url", str(result))
        self.assertEqual(session.post.call_count, 1)
        session.close.assert_called_once()

    def test_disabled_provider_does_not_fall_back_or_start_network(self):
        for settings in ({}, {"notification_provider": "pushplus", "webhook_url": "https://example.invalid/key"}):
            with self.subTest(settings=settings), patch.object(notifications.requests, "Session") as factory:
                result = notifications.send("测试", settings)
                self.assertEqual(result["status"], "disabled")
                factory.assert_not_called()

    def test_invalid_provider_does_not_start_network(self):
        with patch.object(notifications.requests, "Session") as factory:
            result = notifications.send("测试", {"notification_provider": "invalid"})
        self.assertEqual(result["status"], "failed")
        factory.assert_not_called()

    def test_legacy_bark_and_robot_webhooks_keep_their_payloads(self):
        cases = [
            ("https://api.day.app/synthetic-key", "https://api.day.app/push",
             {"device_key": "synthetic-key", "title": "龙湖签到", "body": "虚构的签到结果", "group": "龙湖签到"}, {"code": 200}),
            ("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=synthetic",
             "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=synthetic",
             {"msgtype": "text", "text": {"content": "虚构的签到结果"}}, {"errcode": 0}),
            ("https://oapi.dingtalk.com/robot/send?access_token=synthetic",
             "https://oapi.dingtalk.com/robot/send?access_token=synthetic",
             {"msgtype": "text", "text": {"content": "虚构的签到结果"}}, {"errcode": 0}),
        ]
        for url, endpoint, payload, response in cases:
            with self.subTest(endpoint=endpoint):
                result, session = self.send_with_response(
                    data=response, settings={"webhook_url": url})
                self.assertTrue(result["ok"])
                self.assertEqual(session.post.call_args.args, (endpoint,))
                self.assertEqual(session.post.call_args.kwargs["json"], payload)

    def test_runner_preparation_failure_is_safe(self):
        with patch.object(runner.db, "get_settings", side_effect=RuntimeError(TOKEN)), \
                self.assertLogs(runner.logger, "WARNING") as logs:
            result = runner.notify("测试")
        self.assertEqual(result["status"], "failed")
        self.assertNotIn(TOKEN, str(result) + str(logs.output))


class SummaryTests(unittest.TestCase):
    def test_already_signed_entries_still_send_one_complete_summary(self):
        with patch.object(runner, "notify", return_value={"ok": True}) as send:
            result = runner.daily_summary([{
                "name": "虚构账号", "refreshed": True,
                "checkin_status": "already", "checkin_message": "今日已签到",
                "extra_signs": [{"key": "app", "status": "already", "message": "App 今日已签到"}],
            }])
        send.assert_called_once()
        text = send.call_args.args[0]
        self.assertIn("小程序✅已签", text)
        self.assertIn("App✅已签", text)
        self.assertNotIn("登录续期", text)
        self.assertEqual(result, {"ok": True})

    def test_validation_and_expired_credentials_are_visible_for_each_entry(self):
        with patch.object(runner, "notify") as send:
            runner.daily_summary([{
                "name": "虚构账号", "refreshed": False, "error": "登录已过期，需要重新导入",
                "checkin_status": "fail", "checkin_message": "鉴权失败：token 已失效，需要重新导入",
                "extra_signs": [{"key": "app", "status": "risk", "message": "请在 App 完成验证"}],
            }])
        send.assert_called_once()
        text = send.call_args.args[0]
        for expected in ("小程序⚠️鉴权失败", "App⚠️", "登录续期⚠️未成功", "登录已过期", "请在 App 完成验证", "下一步"):
            self.assertIn(expected, text)

    def test_missing_app_result_is_explicit_and_empty_batch_sends_nothing(self):
        with patch.object(runner, "notify") as send:
            runner.daily_summary([])
            send.assert_not_called()
            runner.daily_summary([{"name": "虚构账号", "checkin_status": "fail"}])
        self.assertIn("App⚠️未执行或未返回结果", send.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
