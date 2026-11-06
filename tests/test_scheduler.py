"""调度恢复与竞态回归；使用虚拟时钟和内存设置，不启动真实任务。"""

import threading
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app import scheduler as subject


class MemoryScheduler:
    timezone = ZoneInfo("Asia/Shanghai")

    def __init__(self):
        self.jobs = {}

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def add_job(self, function, trigger, **options):
        self.jobs[options["id"]] = SimpleNamespace(
            next_run_time=getattr(trigger, "run_date", None), function=function,
        )


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.settings = {
            "checkin_time": "08:00", "checkin_jitter": "60", "last_run_date": "",
            "retry_at": "", "retry_count": "0",
        }
        self.now = datetime(2026, 11, 7, 8, 46, tzinfo=MemoryScheduler.timezone)
        self.scheduler = MemoryScheduler()
        self.stack.enter_context(patch.object(subject, "scheduler", self.scheduler))
        self.stack.enter_context(patch.object(subject, "_running", False))
        self.stack.enter_context(patch.object(subject, "_now", side_effect=lambda: self.now))
        self.stack.enter_context(patch.object(subject.random, "randint", return_value=0))
        self.stack.enter_context(patch.object(subject.db, "get_settings",
                                             side_effect=lambda: dict(self.settings)))
        self.stack.enter_context(patch.object(subject.db, "save_settings",
                                             side_effect=self.settings.update))
        self.run_all = self.stack.enter_context(patch.object(subject.runner, "run_all", return_value=[]))
        self.summary = self.stack.enter_context(patch.object(subject.runner, "daily_summary"))

    def test_normal_window_and_completed_day(self):
        self.now = self.now.replace(hour=7)
        self.assertEqual(subject._pick_next(), self.now.replace(hour=8, minute=0))
        self.now = self.now.replace(hour=8)
        self.assertEqual(subject._pick_next(), self.now)
        self.settings["last_run_date"] = "2026-11-07"
        self.assertEqual(subject._pick_next(), self.now.replace(hour=8, minute=0) + timedelta(days=1))

    def test_restart_after_window_catches_up_today(self):
        self.now = self.now.replace(hour=9, minute=10)
        self.assertEqual(subject._pick_next(), self.now + timedelta(seconds=30))
        self.settings["checkin_jitter"] = "0"
        self.assertEqual(subject._pick_next(), self.now + timedelta(seconds=30))

    def test_late_catchup_does_not_roll_into_next_day(self):
        self.now = self.now.replace(hour=23, minute=59, second=50)
        when = subject._pick_next()
        self.assertGreater(when, self.now)
        self.assertEqual(when.date(), self.now.date())

    def test_due_retry_is_recovered_and_future_retry_is_preserved(self):
        self.settings.update(retry_date="2026-11-07", retry_at=self.now.isoformat(), retry_count="1")
        self.assertEqual(subject._pick_next(), self.now + timedelta(seconds=30))
        later = self.now + timedelta(minutes=10)
        self.settings["retry_at"] = later.isoformat()
        self.assertEqual(subject._pick_next(), later)

    def test_legacy_naive_retry_uses_local_timezone(self):
        self.settings["retry_at"] = "2026-11-07T08:56:00"
        self.assertEqual(subject._pick_next(), self.now.replace(minute=56))

    def test_retry_date_comparison_uses_local_calendar_day(self):
        self.now = self.now.replace(hour=0, minute=10)
        self.settings["retry_at"] = "2026-11-06T16:30:00+00:00"
        self.assertEqual(subject._pick_next(), self.now.replace(minute=30))

    def test_new_day_does_not_inherit_old_retry_count(self):
        self.settings.update(retry_date="2026-11-06", retry_at="2026-11-06T23:00:00+08:00",
                             retry_count="4")
        subject._record_result(False)
        self.assertEqual(self.settings["retry_count"], "1")
        self.assertEqual(self.settings["retry_date"], "2026-11-07")
        self.assertEqual(subject._parse_iso(self.settings["retry_at"]), self.now + timedelta(minutes=10))

    def test_legacy_retry_count_continues_for_same_day(self):
        self.settings.update(retry_at=self.now.isoformat(), retry_count="2")
        subject._record_result(False)
        self.assertEqual(self.settings["retry_count"], "3")
        self.assertEqual(subject._parse_iso(self.settings["retry_at"]), self.now + timedelta(minutes=30))

    def test_retry_exhaustion_stops_today(self):
        self.settings.update(retry_date="2026-11-07", retry_at=self.now.isoformat(), retry_count="4")
        subject._record_result(False)
        self.assertEqual(self.settings["last_run_date"], "2026-11-07")
        self.assertEqual(self.settings["retry_at"], "")
        self.assertEqual(subject._pick_next().date(), (self.now + timedelta(days=1)).date())

    def test_retry_does_not_cross_midnight(self):
        self.now = self.now.replace(hour=23, minute=55)
        subject._record_result(False)
        self.assertEqual(self.settings["last_run_date"], "2026-11-07")
        self.assertEqual(self.settings["retry_at"], "")
        self.assertEqual(self.settings["retry_count"], "0")

    def test_task_finishing_after_midnight_settles_only_its_start_day(self):
        self.now = self.now.replace(hour=23, minute=59)

        def finish_next_day():
            self.now = self.now.replace(hour=0, minute=2) + timedelta(days=1)
            return [{"checkin_status": "already"}]

        self.run_all.side_effect = finish_next_day
        subject._job()
        self.assertEqual(self.settings["last_run_date"], "2026-11-07")
        self.assertEqual(self.scheduler.jobs[subject.JOB_ID].next_run_time,
                         self.now.replace(hour=8, minute=0))

    def test_failed_task_after_midnight_does_not_spend_new_day_retries(self):
        self.now = self.now.replace(hour=0, minute=2) + timedelta(days=1)
        self.settings.update(retry_date="2026-11-07", retry_count="4")
        subject._record_result(False, "2026-11-07")
        self.assertEqual(self.settings["last_run_date"], "2026-11-07")
        self.assertEqual(self.settings["retry_count"], "0")
        subject._record_result(False, "2026-11-08")
        self.assertEqual(self.settings["retry_count"], "1")
        self.assertEqual(self.settings["retry_date"], "2026-11-08")

    def test_both_entries_include_safe_retry_and_busy_statuses(self):
        for status in ("fail", "retry", "busy"):
            with self.subTest(status=status):
                self.assertTrue(subject._needs_retry({"checkin_status": status}))
                self.assertTrue(subject._needs_retry({"checkin_status": "already",
                    "extra_signs": [{"key": "app", "status": status}]}))
        for status in ("ok", "already", "todo", "risk", "conflict", "skipped"):
            with self.subTest(status=status):
                self.assertFalse(subject._needs_retry({"checkin_status": status,
                    "extra_signs": [{"key": "app", "status": status}]}))

    def test_app_only_failure_gets_retry(self):
        self.run_all.return_value = [{"checkin_status": "already", "extra_signs": [
            {"key": "app", "status": "fail"}]}]
        subject._job()
        self.assertEqual(self.settings["retry_count"], "1")
        self.assertEqual(self.scheduler.jobs[subject.JOB_ID].next_run_time,
                         self.now + timedelta(minutes=10))

    def test_planner_during_run_cannot_queue_stale_schedule(self):
        self.now = self.now.replace(hour=9, minute=0)

        def fail_after_planner():
            subject._planner()
            self.assertNotIn(subject.JOB_ID, self.scheduler.jobs)
            subject.reschedule()
            self.assertNotIn(subject.JOB_ID, self.scheduler.jobs)
            return [{"checkin_status": "retry"}]

        self.run_all.side_effect = fail_after_planner
        subject._job()
        self.assertEqual(self.scheduler.jobs[subject.JOB_ID].next_run_time,
                         self.now + timedelta(minutes=10))
        self.assertFalse(subject._running)

    def test_success_replaces_any_preexisting_stale_job(self):
        self.scheduler.jobs[subject.JOB_ID] = SimpleNamespace(next_run_time=self.now + timedelta(seconds=10))
        self.run_all.return_value = [{"checkin_status": "already"}]
        subject._job()
        self.assertEqual(self.scheduler.jobs[subject.JOB_ID].next_run_time,
                         self.now.replace(hour=8, minute=0) + timedelta(days=1))

    def test_inflight_run_is_not_started_again(self):
        entered, release = threading.Event(), threading.Event()

        def block():
            entered.set()
            if not release.wait(3):
                raise AssertionError("test did not release worker")
            return [{"checkin_status": "ok"}]

        self.run_all.side_effect = block
        worker = threading.Thread(target=subject._job)
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            subject._job()
            subject._planner()
            self.assertNotIn(subject.JOB_ID, self.scheduler.jobs)
            self.run_all.assert_called_once()
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())

    def test_notification_failure_cannot_change_recorded_result(self):
        self.run_all.return_value = [{"checkin_status": "ok"}]

        def notify_after_commit(results):
            self.assertEqual(self.settings["last_run_date"], "2026-11-07")
            self.assertIn(subject.JOB_ID, self.scheduler.jobs)
            raise RuntimeError("fake notification failure")

        self.summary.side_effect = notify_after_commit
        with self.assertLogs(subject.logger, level="ERROR"):
            subject._job()
        self.assertEqual(self.settings["retry_at"], "")
        self.assertEqual(self.settings["last_run_date"], "2026-11-07")


if __name__ == "__main__":
    unittest.main()
