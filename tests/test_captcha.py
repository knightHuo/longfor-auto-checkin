"""Offline contracts only: no challenge creation, network, or real verification.

Geometry fixtures below are numeric unit inputs, not claims that any real captcha
was recognized or accepted. Real basic-image extraction still requires a sample.
"""

import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

from app import captcha
from tools import slider_match


class CaptchaModeTests(unittest.TestCase):
    def test_default_is_disabled(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(captcha.mode_from_env(), "off")

    def test_auto_means_real_local_recognition(self):
        for value in ("auto", "AUTO", " local "):
            with self.subTest(value=value), patch.dict(os.environ, {"LONGHU_CAPTCHA": value}):
                self.assertEqual(captcha.mode_from_env(), "local")

    def test_unknown_mode_does_not_start_browser(self):
        with patch.object(captcha, "available") as check:
            self.assertEqual(captcha.solve("device", mode="unknown"), (None, "off"))
        check.assert_not_called()

    def test_fixed_distance_is_not_a_silent_auto_fallback(self):
        with patch.object(captcha, "available") as check:
            token, message = captcha.solve("device", mode="local", dist=120)
        self.assertIsNone(token)
        self.assertIn("固定", message)
        check.assert_not_called()

    def test_invalid_device_or_timeout_fails_before_subprocess(self):
        for changes in ({"device": ""}, {"timeout": 1}, {"timeout": True}, {"user_agent": "x" * 4097}):
            with self.subTest(changes=changes), patch.object(captcha, "available") as check:
                token, _ = captcha.solve(**{"device": "device", "mode": "local", **changes})
                self.assertIsNone(token)
                check.assert_not_called()


class CaptchaProcessTests(unittest.TestCase):
    def completed(self, stdout, code=0, stderr=""):
        return subprocess.CompletedProcess(["node"], code, stdout, stderr)

    def test_sensitive_input_is_not_in_command_line(self):
        device, ua, token = "private-device-value", "private-user-agent", "private-result-token"
        with patch.object(captcha, "available", return_value=(True, "ok")), \
             patch.object(captcha.subprocess, "run", return_value=self.completed(json.dumps({"ok": True, "token": token}))) as run:
            self.assertEqual(captcha.solve(device, mode="auto", user_agent=ua), (token, "ok"))
        command = run.call_args.args[0]
        self.assertIn("--mode=local", command)
        self.assertNotIn(device, " ".join(command))
        self.assertNotIn(ua, " ".join(command))
        self.assertEqual(json.loads(run.call_args.kwargs["input"]), {"device": device, "user_agent": ua})

    def test_failure_never_exposes_subprocess_output(self):
        secret = "private-device-or-token"
        output = json.dumps({"ok": False, "error": "arbitrary-error-" + secret, "token": secret})
        with patch.object(captcha, "available", return_value=(True, "ok")), \
             patch.object(captcha.subprocess, "run", return_value=self.completed(output, 1, secret)):
            token, message = captcha.solve("device", mode="local")
        self.assertIsNone(token)
        self.assertNotIn(secret, message)

    def test_nonzero_exit_cannot_be_accepted_as_success(self):
        with patch.object(captcha, "available", return_value=(True, "ok")), \
             patch.object(captcha.subprocess, "run", return_value=self.completed('{"ok":true,"token":"value"}', 1)):
            self.assertIsNone(captcha.solve("device", mode="local")[0])

    def test_malformed_error_field_does_not_escape_as_exception(self):
        with patch.object(captcha, "available", return_value=(True, "ok")), \
             patch.object(captcha.subprocess, "run", return_value=self.completed('{"ok":false,"error":{"private":"value"}}', 1)):
            token, message = captcha.solve("device", mode="local")
        self.assertIsNone(token)
        self.assertNotIn("private", message)

    def test_timeout_returns_no_credential_and_does_not_retry(self):
        with patch.object(captcha, "available", return_value=(True, "ok")), \
             patch.object(captcha.subprocess, "run", side_effect=subprocess.TimeoutExpired(["node"], 120)) as run:
            token, message = captcha.solve("device", mode="local")
        self.assertIsNone(token)
        self.assertIn("未自动重试", message)
        self.assertEqual(run.call_count, 1)

    def test_dependency_check_has_no_sensitive_input_and_handles_missing_package(self):
        with patch.object(captcha.shutil, "which", return_value="node"), \
             patch.object(captcha.subprocess, "run", return_value=self.completed('{"ok":false,"error":"dependency_missing"}', 1)) as run:
            self.assertFalse(captcha.available("local")[0])
        self.assertIn("--check-deps", run.call_args.args[0])
        self.assertNotIn("input", run.call_args.kwargs)

    def test_last_json_ignores_banner_and_rejects_nonobject(self):
        self.assertEqual(captcha._last_json('banner\n{"ok":false}\n'), {"ok": False})
        self.assertIsNone(captcha._last_json('not json\n[]\n'))


class SliderGeometryTests(unittest.TestCase):
    geometry = {"background_width": 330, "piece_left": 10, "track_remaining": 280}

    def test_match_center_is_converted_to_displacement(self):
        result = slider_match.drag_distance({"target": [175, 25], "confidence": 0.91}, self.geometry, 330, 50)
        self.assertEqual(result, {"distance": 140.0, "confidence": 0.91})

    def test_rendered_width_is_used_instead_of_raw_image_pixels(self):
        result = slider_match.drag_distance({"target": [350, 50], "confidence": 0.91}, self.geometry, 660, 50)
        self.assertEqual(result["distance"], 140.0)

    def test_uncertain_match_is_rejected_before_drag(self):
        with self.assertRaises(slider_match.MatchError) as caught:
            slider_match.drag_distance({"target": [175, 25], "confidence": 0.6}, self.geometry, 330, 50)
        self.assertEqual(caught.exception.code, "low_confidence")

    def test_outside_track_or_nonfinite_values_are_rejected(self):
        for x in (0, 330, -1, math.nan, math.inf, True):
            with self.subTest(x=x), self.assertRaises(slider_match.MatchError):
                slider_match.drag_distance({"target": [x, 25], "confidence": 0.91}, self.geometry, 330, 50)

    def test_missing_confidence_is_not_treated_as_a_valid_match(self):
        with self.assertRaises(slider_match.MatchError):
            slider_match.drag_distance({"target": [175, 25]}, self.geometry, 330, 50)

    def test_short_track_is_not_overrun(self):
        with self.assertRaises(slider_match.MatchError):
            slider_match.drag_distance({"target": [175, 25], "confidence": 0.91},
                                      {**self.geometry, "track_remaining": 100}, 330, 50)

    def test_invalid_encoded_image_is_rejected(self):
        for value in (None, "not-base64!", ""):
            with self.subTest(value=value), self.assertRaises(slider_match.MatchError):
                slider_match._decode_image(value)


@unittest.skipUnless(shutil.which("node"), "Node.js not installed")
class BrowserOfflineEntryTests(unittest.TestCase):
    script = Path(__file__).resolve().parent.parent / "tools" / "captcha_browser.js"

    def test_disabled_entry_does_not_need_browser_or_network(self):
        result = subprocess.run([shutil.which("node"), str(self.script), "--mode=off"],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {"ok": False, "error": "off"})

    def test_fixed_distance_is_rejected_without_creating_a_challenge(self):
        result = subprocess.run([shutil.which("node"), str(self.script), "--mode=local", "--dist=120"],
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {"ok": False, "error": "invalid_input"})


if __name__ == "__main__":
    unittest.main()
