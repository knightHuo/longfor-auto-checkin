"""Fresh install and live SQLite/WAL backup contracts; no external service calls."""

from contextlib import closing, redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tools import manage


class InitializationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_fresh_install_has_hash_only_and_scheduling_off(self):
        password = "synthetic-password-for-tests"
        result = manage.initialize(self.root, "tester", password)
        value = json.loads((self.root / "secrets/panel-auth.json").read_text())
        environment = (self.root / ".env").read_text()
        self.assertEqual(value["username"], "tester")
        self.assertEqual(value["hash"], hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(value["salt"]), value["iterations"]).hex())
        self.assertNotIn(password, json.dumps(value) + environment + json.dumps(result))
        self.assertIn("LONGFOR_SCHEDULE_ENABLED=0", environment)
        self.assertNotIn("LONGHU_CAPTCHA=local", environment)
        self.assertTrue((self.root / "data").is_dir())
        if os.name == "posix":
            self.assertEqual((self.root / "secrets/panel-auth.json").stat().st_mode & 0o777, 0o400)

    def test_existing_installation_is_not_overwritten(self):
        manage.initialize(self.root, "tester", "synthetic-password-one")
        auth = self.root / "secrets/panel-auth.json"
        before = auth.read_bytes()
        with self.assertRaises(ValueError):
            manage.initialize(self.root, "replacement", "synthetic-password-two")
        self.assertEqual(auth.read_bytes(), before)

    def test_invalid_credentials_do_not_create_files(self):
        for username, password in (("bad:name", "long-enough-password"), ("ok", "short")):
            with self.subTest(username=username):
                with self.assertRaises(ValueError):
                    manage.initialize(self.root, username, password)
                self.assertFalse((self.root / "secrets").exists())

    def test_noninteractive_setup_requires_explicit_generation(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(manage.sys.stdin, "isatty", return_value=False), redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(manage.main(["init", "--root", str(self.root)]), 1)
        self.assertFalse((self.root / "secrets").exists())

    def test_generated_password_is_only_in_explicit_cli_output(self):
        password = "synthetic-generated-value"
        stdout = io.StringIO()
        with patch.object(manage.secrets, "token_urlsafe", return_value=password), redirect_stdout(stdout):
            self.assertEqual(manage.main(["init", "--root", str(self.root), "--generate-password"]), 0)
        self.assertEqual(stdout.getvalue().count(password), 1)
        self.assertNotIn(password, (self.root / "secrets/panel-auth.json").read_text())


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "data").mkdir()
        self.source = sqlite3.connect(self.root / "data/longhu.db")
        self.addCleanup(self.source.close)
        self.source.execute("PRAGMA journal_mode=WAL")
        self.source.execute("CREATE TABLE records(value TEXT)")
        self.source.commit()

    def test_backup_includes_committed_wal_and_is_a_consistent_snapshot(self):
        self.source.execute("INSERT INTO records VALUES ('committed')")
        self.source.commit()
        self.source.execute("INSERT INTO records VALUES ('uncommitted')")
        output = manage.backup_database(self.root)
        with closing(sqlite3.connect(output)) as backup:
            self.assertEqual(backup.execute("SELECT value FROM records").fetchall(), [("committed",)])
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone(), ("ok",))
        if os.name == "posix":
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_existing_backup_is_preserved(self):
        destination = self.root / "existing.db"
        destination.write_bytes(b"keep-this-file")
        with self.assertRaises(ValueError):
            manage.backup_database(self.root, destination)
        self.assertEqual(destination.read_bytes(), b"keep-this-file")

    def test_missing_database_never_creates_an_empty_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                manage.backup_database(root)
            self.assertFalse((root / "data/longhu.db").exists())


if __name__ == "__main__":
    unittest.main()
