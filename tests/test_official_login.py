"""离线采集输入测试；凭据 API 全部替换为内存中的假实现。"""

import ctypes
import importlib.util
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch


TOOLS_DIR = Path(__file__).resolve().parents[1] / 'tools'
sys.path.insert(0, str(TOOLS_DIR))
try:
    spec = importlib.util.spec_from_file_location('official_login_capture', TOOLS_DIR / 'official_login.py')
    capture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(capture)
finally:
    sys.path.pop(0)


class OfficialLoginInputTests(unittest.TestCase):
    def test_invalid_member_tokens_are_rejected_before_credential_api(self):
        for token in ('x' * 768, 'a' * 31, 'a' * 33, 'x' * 32, None):
            with self.subTest(token_type=type(token).__name__, length=len(token or '')), \
                    patch.object(capture, '_api') as api:
                with self.assertRaises(ValueError):
                    capture.save_session({'token': token, 'source': 'official-sms-lzb'})
                api.assert_not_called()

    def test_old_or_missing_source_is_not_silently_imported(self):
        for source in (None, 'official-sms-browser', 'unverified'):
            with self.subTest(source=source), patch.object(capture, '_api') as api:
                with self.assertRaises(ValueError):
                    capture.save_session({'token': '0123456789abcdef0123456789abcdef', 'source': source})
                api.assert_not_called()

    def test_valid_member_session_preserves_source_in_mock_store(self):
        recorded = {}

        def fake_write(pointer, flags):
            item = ctypes.cast(pointer, ctypes.POINTER(capture.Credential)).contents
            recorded['target'] = item.TargetName
            recorded['payload'] = json.loads(ctypes.string_at(
                item.CredentialBlob, item.CredentialBlobSize).decode('utf-16-le'))
            recorded['flags'] = flags
            return True

        api = Mock()
        api.CredWriteW.side_effect = fake_write
        value = {'token': '0123456789abcdef0123456789abcdef',
                 'source': 'official-sms-lzb', 'user_agent': 'offline-test-agent'}
        stdout = io.StringIO()
        with patch.object(capture, '_api', return_value=api), redirect_stdout(stdout):
            capture.save_session(value)

        api.CredWriteW.assert_called_once()
        self.assertEqual(recorded['target'], capture.TARGET)
        self.assertEqual(recorded['flags'], 0)
        self.assertEqual(recorded['payload'], {
            **value, 'channel': 'C2', 'bu_code': 'C20400',
        })
        self.assertEqual(stdout.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
