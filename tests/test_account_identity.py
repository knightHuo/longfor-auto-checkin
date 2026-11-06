"""相同登录凭据的并发写入保护；只操作临时 SQLite。"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from app import db


class AccountIdentityTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        override = patch.object(db, "DB_PATH", str(Path(directory.name) / "identity.db"))
        override.start()
        self.addCleanup(override.stop)
        db.init()

    def create(self, token="synthetic-member-token", name="虚构账号"):
        return db.create_account({"name": name, "token": token})

    def test_lookup_can_exclude_the_account_being_updated(self):
        account_id = self.create()
        self.assertEqual(db.account_with_token("synthetic-member-token")["id"], account_id)
        self.assertIsNone(db.account_with_token("synthetic-member-token", exclude_id=account_id))
        self.assertIsNone(db.account_with_token("synthetic-unknown-token"))

    def test_concurrent_imports_of_the_same_token_create_only_one_account(self):
        gate = threading.Barrier(4)
        def create_concurrently(number):
            gate.wait(timeout=5)
            try:
                return "created", self.create(name=f"虚构账号{number}")
            except ValueError as exc:
                return "duplicate", str(exc)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(create_concurrently, range(4)))
        self.assertEqual(sum(status == "created" for status, _ in results), 1)
        self.assertEqual(len(db.list_accounts()), 1)
        for status, message in results:
            if status == "duplicate":
                self.assertEqual(message, "该账号已经存在，请更新原账号凭据")
                self.assertNotIn("synthetic-member-token", message)

    def test_conflicting_update_keeps_all_existing_fields(self):
        first = self.create("synthetic-first-token", "第一账号")
        second = self.create("synthetic-second-token", "第二账号")
        with self.assertRaises(ValueError):
            db.update_account(second, token="synthetic-first-token", name="不得保存的名称")
        saved = db.get_account(second)
        self.assertEqual(saved["token"], "synthetic-second-token")
        self.assertEqual(saved["name"], "第二账号")
        self.assertEqual(db.get_account(first)["name"], "第一账号")

    def test_updating_its_own_unchanged_token_is_allowed(self):
        account_id = self.create()
        db.update_account(account_id, token="synthetic-member-token", name="已更新名称")
        self.assertEqual(db.get_account(account_id)["name"], "已更新名称")

    def test_concurrent_updates_cannot_assign_one_token_to_two_accounts(self):
        accounts = [self.create("synthetic-token-" + str(number)) for number in range(2)]
        gate = threading.Barrier(2)
        def update_concurrently(account_id):
            gate.wait(timeout=5)
            try:
                db.update_account(account_id, token="synthetic-shared-new-token")
                return "updated"
            except ValueError:
                return "duplicate"
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(update_concurrently, accounts))
        self.assertEqual(outcomes.count("updated"), 1)
        self.assertEqual(outcomes.count("duplicate"), 1)
        self.assertEqual(sum(account["token"] == "synthetic-shared-new-token"
                             for account in db.list_accounts()), 1)

    def test_legacy_duplicates_remain_migratable_without_a_unique_constraint(self):
        with db.connect() as connection:
            connection.executemany("INSERT INTO accounts(name, token) VALUES (?, ?)", [
                ("旧记录一", "synthetic-legacy-token"), ("旧记录二", "synthetic-legacy-token"),
            ])
        db.init()
        self.assertEqual(len(db.list_accounts()), 2)
        with self.assertRaises(ValueError):
            self.create("synthetic-legacy-token")
        self.assertEqual(len(db.list_accounts()), 2)


if __name__ == "__main__":
    unittest.main()
