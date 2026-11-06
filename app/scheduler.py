"""定时调度：每天在一个时间窗口内随机取一个时刻跑一轮签到，每天都不一样。

窗口 = [签到窗口起点, 起点 + 随机浮动分钟)。默认 08:00 起浮动 60 分钟，
即每天落在 08:00–09:00 之间的某一分某一秒。
浮动填 0 就退化成原来的固定时间。

实现上不用 cron（cron 只支持固定值），而是每天排一个一次性任务，
再由一个每 5 分钟的巡场任务兜底；运行结束后立即按结果排下一轮。
"""

import logging
import random
import threading
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from . import db, runner

logger = logging.getLogger(__name__)

JOB_ID = "daily_checkin"
PLANNER_ID = "daily_planner"
DEFAULT_TIME = "08:00"
DEFAULT_JITTER = 60

# 失败重试：签到被上游临时拒绝（如「网络故障，请稍后再试」）时，
# 当天内再补几次，而不是直接放弃到第二天。
# 退避 10/20/30/40 分钟，若 08:25 首跑失败，最后一次重试落在 10 点后，
# 足以覆盖「活动开放时间比预期晚」这一类情况。
MAX_RETRY = 4
RETRY_MINUTES = 10
RETRYABLE_STATUSES = frozenset(("fail", "retry", "busy"))
CATCHUP_SECONDS = 30

scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
_state_lock = threading.RLock()
_running = False


def _now() -> datetime:
    return datetime.now(scheduler.timezone)


def _today_text() -> str:
    return _now().strftime("%Y-%m-%d")


def _parse_hhmm(text: str) -> tuple[int, int]:
    try:
        hour, minute = text.split(":")
        hour, minute = int(hour), int(minute)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except Exception:
        pass
    return 8, 0


def _config(settings: dict | None = None) -> tuple[int, int, int]:
    """返回 (窗口起点小时, 分钟, 随机浮动分钟)。"""
    settings = settings if settings is not None else db.get_settings()
    hour, minute = _parse_hhmm(settings.get("checkin_time") or DEFAULT_TIME)
    try:
        jitter = int(float(settings.get("checkin_jitter") or 0))
    except (TypeError, ValueError):
        jitter = DEFAULT_JITTER
    return hour, minute, max(0, min(jitter, 1439))


def window_text() -> str:
    """给前端显示的窗口描述，如 08:00–09:00。"""
    hour, minute, jitter = _config()
    start = f"{hour:02d}:{minute:02d}"
    if jitter <= 0:
        return start
    end = _now().replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(minutes=jitter)
    return f"{start}–{end.strftime('%H:%M')}"


def _parse_iso(text: str | None) -> datetime | None:
    try:
        dt = datetime.fromisoformat(text) if text else None
    except (TypeError, ValueError):
        return None
    if dt is not None and dt.tzinfo is None:
        zone = scheduler.timezone
        dt = zone.localize(dt) if hasattr(zone, "localize") else dt.replace(tzinfo=zone)
    return dt.astimezone(scheduler.timezone) if dt is not None else None


def _pick_next() -> datetime:
    """算出下一次签到的具体时刻。

    优先级：当天待执行的重试 > 当天正常首跑/补跑 > 明天窗口内随机时刻。
    """
    settings = db.get_settings()
    hour, minute, jitter = _config(settings)
    now = _now()

    # 1) 今天还有重试没跑完，先跑重试
    retry_at = _parse_iso(settings.get("retry_at"))
    today = now.date().isoformat()
    retry_date = settings.get("retry_date") or (retry_at.date().isoformat() if retry_at else "")
    if retry_at and retry_at.date() == now.date() and retry_date == today:
        return retry_at if retry_at > now else _catchup_time(now)

    start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    end = start + timedelta(minutes=jitter)

    if settings.get("last_run_date") == today:
        start += timedelta(days=1)
        end += timedelta(days=1)
        base = start
    elif now >= end:
        # 重启或停机错过随机窗口时仍补查当天；业务层先查已签状态再决定提交。
        return _catchup_time(now)
    elif now <= start:
        base = start
    else:
        base = now

    span = int((end - base).total_seconds())
    if span <= 0:
        return base
    return base + timedelta(seconds=random.randint(0, span - 1))


def _catchup_time(now: datetime) -> datetime:
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return min(now + timedelta(seconds=CATCHUP_SECONDS), midnight - timedelta(microseconds=1))


def _record_result(ok_all: bool, run_date: str | None = None):
    """记录本轮结果。全成功 → 记为今天已跑；有失败 → 排当天重试。"""
    now = _now()
    run_date = run_date or now.date().isoformat()
    settings = db.get_settings()
    retry_at = _parse_iso(settings.get("retry_at"))
    retry_date = settings.get("retry_date") or (retry_at.date().isoformat() if retry_at else "")
    try:
        retries = max(0, int(settings.get("retry_count") or 0)) if retry_date == run_date else 0
    except (TypeError, ValueError):
        retries = 0

    finished = {"last_run_date": run_date, "retry_at": "", "retry_count": "0", "retry_date": ""}
    if ok_all or now.date().isoformat() != run_date:
        # 跨午夜完成只结算启动那天，不能把新一天误记为已完成或沿用旧重试次数。
        db.save_settings(finished)
        return

    if retries < MAX_RETRY:
        delay = RETRY_MINUTES * (retries + 1)
        when = now + timedelta(minutes=delay)
        if when.date() != now.date():
            db.save_settings(finished)
            logger.warning("重试时间将跨午夜，结束 %s 的重试，次日按正常排程执行", run_date)
            return
        db.save_settings({"retry_at": when.isoformat(), "retry_count": str(retries + 1),
                          "retry_date": run_date})
        logger.warning(
            "本轮有账号失败，%d 分钟后重试（第 %d/%d 次）：%s",
            delay, retries + 1, MAX_RETRY, when.strftime("%H:%M:%S"),
        )
    else:
        db.save_settings(finished)
        logger.error("已重试 %d 次仍失败，今天不再重试", MAX_RETRY)


def _job():
    global _running
    with _state_lock:
        if _running:
            return
        _running = True
        run_date = _today_text()
    ok_all = True
    results = []
    try:
        logger.info("定时任务开始执行")
        results = runner.run_all()
        failed = [r for r in results if _needs_retry(r)]
        ok_all = not failed
        logger.info("定时任务完成，共 %d 个账号，失败 %d 个", len(results), len(failed))
    except Exception:
        logger.exception("定时任务执行失败")
        ok_all = False
    finally:
        with _state_lock:
            try:
                _record_result(ok_all, run_date)
            except Exception:
                logger.exception("记录执行结果失败")
            finally:
                _running = False
            try:
                _ensure_scheduled(force=True)
            except Exception:
                logger.exception("重排签到任务失败，等待巡场恢复")
    # 通知故障不能更改已落库的任务结果或把成功签到变成重试。
    try:
        runner.daily_summary(results)
    except Exception:
        logger.exception("每日汇总通知失败")


def _needs_retry(result: dict) -> bool:
    return result.get("checkin_status") in RETRYABLE_STATUSES or any(
        entry.get("status") in RETRYABLE_STATUSES for entry in result.get("extra_signs") or []
    )


def _ensure_scheduled(force: bool = False) -> datetime | None:
    """保证总有一个待执行的签到任务；force=True 时按当前设置重建。"""
    with _state_lock:
        if _running:
            return None
        job = scheduler.get_job(JOB_ID)
        if job and job.next_run_time and not force:
            return job.next_run_time

        when = _pick_next()
        scheduler.add_job(
            _job,
            DateTrigger(run_date=when),
            id=JOB_ID,
            replace_existing=True,
            misfire_grace_time=1800,
            coalesce=True,
            max_instances=1,
        )
        logger.info("下次签到：%s", when.strftime("%Y-%m-%d %H:%M:%S"))
        return when


def _planner():
    """巡场：签到任务是一次性的，跑完就被移除了，这里负责补上下一次。"""
    try:
        _ensure_scheduled()
    except Exception:
        logger.exception("重排签到任务失败")


def reschedule():
    """按当前设置重建排程，改完立即生效。"""
    scheduler.add_job(
        _planner,
        CronTrigger(minute="*/5"),
        id=PLANNER_ID,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    when = _ensure_scheduled(force=True)
    hour, minute, jitter = _config()
    logger.info(
        "签到窗口 %02d:%02d 起浮动 %d 分钟，下次 %s",
        hour, minute, jitter,
        when.strftime("%Y-%m-%d %H:%M:%S") if when else "—",
    )


def start():
    if not scheduler.running:
        scheduler.start()
        reschedule()


def next_run_text() -> str:
    job = scheduler.get_job(JOB_ID)
    if job and job.next_run_time:
        return job.next_run_time.strftime("%Y-%m-%d %H:%M")
    return ""
