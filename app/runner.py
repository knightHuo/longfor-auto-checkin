"""任务编排：续期 → 签到 → 状态刷新 → 落库 → 告警。"""

import logging
import math
from datetime import datetime, timezone

import requests

from . import db, longhu, notifications, run_guard

logger = logging.getLogger(__name__)


def days_left(expire_at: str | None) -> int | None:
    if not expire_at:
        return None
    try:
        dt = datetime.fromisoformat(expire_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = dt - datetime.now(timezone.utc)
        # 向上取整：刚续期完应显示 30 天，而不是 29 天
        return max(0, math.ceil(delta.total_seconds() / 86400))
    except Exception:
        return None


def notify(text: str) -> dict:
    """统一通知入口；配置或发送异常均不阻断签到。"""
    try:
        result = notifications.send(text, db.get_settings())
    except Exception as exc:
        result = {"ok": False, "status": "failed",
                  "message": f"通知准备失败（{type(exc).__name__}）"}
    if result.get("status") == "failed":
        logger.warning("通知推送失败: %s", result["message"])
    return result


APP_MODES = ("auto", "manual", "off")


class _SubmissionDayChanged(longhu.LonghuError):
    """提交前跨日，需要重新查询当日日历。"""


def _dx_conflict(account: dict, accounts: list[dict]) -> list[dict]:
    """检查设备指纹是否与其他启用账号共用。

    配置必须来自各账号本人环境；发现共用时，后加入账号先暂停并交给用户核对。
    返回 id 更小的共用账号列表；空列表表示无冲突。
    """
    token = (account.get("dx_token") or "").strip()
    if not token:
        return []
    smaller = [
        a for a in accounts
        if a["id"] != account["id"]
        and (a.get("dx_token") or "").strip() == token
        and a["id"] < account["id"]
    ]
    return smaller


def _app_entry(account: dict, item: dict, mode: str, on_submit=None) -> tuple[str, str]:
    """APP 专享签到入口。

    auto   ：用账号自己的设备指纹真去签（每个账号必须用独立指纹）
    manual ：只查状态，不打卡；未签就记 todo 交给每日汇总提醒人工去点
    off    ：完全不处理
    """
    label = "APP "
    if mode == "off":
        return "skipped", f"{label}已关闭"
    if not (account.get("dx_token") or "").strip():
        return "todo", f"{label}未配置设备标识，未提交；请从本人独立设备环境正常采集"
    if mode == "auto":
        return longhu.checkin(account, item["activity_no"], label, on_submit=on_submit)

    state = longhu.sign_state(account, item["activity_no"])
    if state is True:
        return "already", f"{label}今日已签到"
    if state is False:
        return "todo", f"{label}未签到，需在 App 内手动签（当前为 manual 模式）"
    return "todo", f"{label}状态未知，需在 App 内确认"


def _please_in_client(is_main: bool, action: str) -> str:
    return f"请在官方微信小程序{action}" if is_main else f"请在官方 App {action}"


def _is_paused(account: dict, prefix: str, entry_date: str) -> bool:
    return (account.get(f"{prefix}last_status") in ("risk", "todo")
            and db.run_date(account.get(f"{prefix}last_run_at")) == entry_date)


def _pending_entry(account: dict, item: dict, entry_date: str) -> tuple[str, str] | None:
    """另一入口重试时，今日风控/提交未知入口只复核日历，不再次提交。"""
    is_main = item["key"] == "mini"
    prefix = "" if is_main else "app_"
    if not _is_paused(account, prefix, entry_date):
        return None
    state = None
    if (account.get("dx_token") or "").strip():
        try:
            state = longhu.sign_state(account, item["activity_no"])
        except longhu.LonghuAuthError:
            raise
        except Exception as exc:
            logger.warning("[%s] 待处理入口复核异常（%s）", account.get("name"), type(exc).__name__)
    if state is True:
        return "already", "今日已签到（只读复核）"
    message = account.get(f"{prefix}last_message") or "签到结果待人工确认"
    marker = f"本日已暂停重复提交，{_please_in_client(is_main, '确认')}"
    if "本日已暂停重复提交" not in message:
        message += "；" + marker
    return account[f"{prefix}last_status"], message


def _halt_after_mini_pause(account: dict, item: dict, mini_status: str) -> tuple[str, str]:
    """小程序当日有风控或提交结果待确认时，App 只读查日历。"""
    if not (account.get("dx_token") or "").strip():
        return "todo", "APP 未配置设备标识，未提交；请从本人独立设备环境正常采集"
    state = None
    try:
        state = longhu.sign_state(account, item["activity_no"])
    except longhu.LonghuAuthError:
        raise
    except Exception as exc:
        logger.warning("[%s] 暂停 App 后只读复核异常（%s）", account.get("name"), type(exc).__name__)
    if state is True:
        return "already", "APP 今日已签到（只读复核）"
    reason = ("APP 已暂停提交：小程序当日触发人机验证/风控" if mini_status == "risk"
              else "APP 已暂停提交：小程序当日签到结果待确认")
    if state is False:
        return mini_status, f"{reason}，请在官方 App 确认并完成签到"
    return mini_status, f"{reason}，当前日历暂未确认，请在官方 App 核对"


def run_account(account: dict, do_checkin: bool = True,
                app_mode: str | None = None) -> dict:
    """同一账号完整流程互斥；拿到锁后重读最新登录态。"""
    acc_id = account["id"]
    lock = run_guard.try_acquire(acc_id)
    if lock is None:
        return {
            "account_id": acc_id, "name": account.get("name") or "账号",
            "refreshed": None, "checkin_status": "busy",
            "checkin_message": "该账号正在执行，本次未重复提交；请稍后刷新结果",
            "extra_signs": [{"key": "app", "name": "APP", "status": "busy",
                             "message": "该账号正在执行，本次未重复提交"}],
            "profile": {}, "error": "",
        }
    try:
        latest = db.get_account(acc_id)
        if latest is None:
            return {
                "account_id": acc_id, "name": account.get("name") or "账号",
                "refreshed": None, "checkin_status": "skipped",
                "checkin_message": "账号已删除，本次未执行",
                "extra_signs": [], "profile": {}, "error": "",
            }
        return _run_account(latest, do_checkin=do_checkin, app_mode=app_mode)
    finally:
        lock.release()


def _run_account(account: dict, do_checkin: bool = True,
                 app_mode: str | None = None) -> dict:
    """执行单个账号的完整流程，返回可直接给前端用的结果。

    app_mode 为 None 时取设置里的 app_checkin_mode。
    """
    acc_id = account["id"]
    acc_name = account["name"]
    original_token = account.get("token")

    def safe(message: object) -> str:
        return longhu.redact(message, account, (original_token,))

    result = {
        "account_id": acc_id,
        "name": acc_name,
        "refreshed": False,
        "checkin_status": "skipped",
        "checkin_message": "",
        "extra_signs": [],
        "profile": {},
        "error": "",
    }

    # ---- 1. 续期 ----
    auth_error = ""
    try:
        ok, payload = longhu.refresh_token(account)
    except longhu.LonghuAuthError as exc:
        ok, payload = False, str(exc)
        auth_error = safe(exc) or "登录已失效，请重新导入"
    except longhu.LonghuError as exc:
        ok, payload = False, str(exc)

    if ok:
        result["refreshed"] = True
        if payload != account.get("token"):
            db.update_account(acc_id, token=payload)
            account["token"] = payload
        db.update_account(acc_id, expire_at=longhu.expire_at())
        db.add_log(acc_id, acc_name, "refresh", "ok", "登录续期成功；有效期按 30 天估算")
    else:
        payload = safe(payload)
        db.add_log(acc_id, acc_name, "refresh", "fail", payload)
        result["error"] = payload
        logger.warning("[%s] 续期失败: %s", acc_name, payload)

    # ---- 1.5 设备指纹冲突检查 ----
    # 共用设备配置需要人工核对，不能靠自动重试解决。
    conflict = _dx_conflict(account, db.list_accounts(only_enabled=True))
    dx_block = ""
    if conflict:
        names = "、".join(a["name"] for a in conflict)
        dx_block = (
            f"设备指纹与「{names}」共用，已暂停签到：同一指纹多账号会触发风控。"
            "请从本人独立设备环境重新采集后更新"
        )
        result["dx_conflict"] = [a["name"] for a in conflict]
        db.add_log(acc_id, acc_name, "dx", "fail", dx_block)

    # ---- 2. 签到（小程序 + APP 两个入口，同一天各签一次） ----
    if app_mode is None:
        app_mode = (db.get_settings().get("app_checkin_mode") or "auto").strip().lower()
    if app_mode not in APP_MODES:
        app_mode = "auto"

    extra = []
    if do_checkin and not dx_block:
        mini_pause_date = ""
        for item in longhu.ACTIVITIES:
            is_main = item["key"] == "mini"
            label = "" if is_main else "APP "
            prefix = "" if is_main else "app_"
            entry_at = db.now_iso()
            entry_date = db.run_date(entry_at)
            intent_stamp = None

            def record_intent():
                nonlocal intent_stamp
                submit_at = db.now_iso()
                midnight_error = "签到准备期间已跨过北京时间午夜，需重新查询当日日历后再提交"
                if db.run_date(submit_at) != entry_date:
                    raise _SubmissionDayChanged(midnight_error)
                message = "签到请求已进入提交阶段，结果待确认；若任务中断仅复核状态"
                db.update_account(acc_id, **{
                    f"{prefix}last_status": "todo",
                    f"{prefix}last_message": message,
                    f"{prefix}last_run_at": submit_at,
                })
                if db.business_date() != entry_date:
                    raise _SubmissionDayChanged(midnight_error)
                intent_stamp = submit_at

            app_off = not is_main and app_mode == "off"
            try:
                if app_off:
                    status, message = _app_entry(account, item, app_mode)
                elif auth_error:
                    status, message = "todo", auth_error + "，未提交签到"
                elif (pending := _pending_entry(account, item, entry_date)) is not None:
                    status, message = pending
                elif not is_main and app_mode == "auto" and mini_pause_date == entry_date:
                    status, message = _halt_after_mini_pause(account, item, result["checkin_status"])
                elif is_main and not (account.get("dx_token") or "").strip():
                    status, message = "todo", "未配置设备标识，未提交签到，请先导入本人设备凭据"
                else:
                    if is_main:
                        status, message = longhu.checkin(account, item["activity_no"], label,
                                                         on_submit=record_intent)
                    else:
                        status, message = _app_entry(account, item, app_mode, on_submit=record_intent)
                    if longhu.is_auth_failure(status, message):
                        auth_error = safe(message)
            except longhu.LonghuAuthError as exc:
                auth_error = safe(exc) or "登录已失效，请重新导入"
                status, message = "todo", auth_error
            except _SubmissionDayChanged as exc:
                status, message = "todo" if intent_stamp else "retry", safe(exc)
            except longhu.LonghuError as exc:
                status, message = "todo" if intent_stamp else "fail", safe(exc)
            except Exception as exc:  # 单个入口异常不影响另一个
                logger.error("[%s] %s 签到异常（%s）", acc_name, item["name"], type(exc).__name__)
                status = "todo" if intent_stamp else "fail"
                reason = "提交结果待确认" if intent_stamp else "未提交"
                message = f"签到处理异常（{type(exc).__name__}），{reason}，请检查服务日志"

            message = safe(message)
            if not (app_off and _is_paused(account, prefix, entry_date)):
                finished_at = db.now_iso()
                # 入口跨日完成时保留起始业务日，避免把旧日结果记到新一天。
                stamp = intent_stamp or (finished_at if db.run_date(finished_at) == entry_date else entry_at)
                db.update_account(acc_id, **{
                    f"{prefix}last_status": status,
                    f"{prefix}last_message": message,
                    f"{prefix}last_run_at": stamp,
                })
            db.add_log(acc_id, acc_name, "checkin" if is_main else "checkin_app",
                       status, message)
            if is_main:
                result["checkin_status"] = status
                result["checkin_message"] = message
                if status in ("risk", "todo"):
                    mini_pause_date = entry_date
            else:
                extra.append({"key": item["key"], "name": item["name"],
                              "status": status, "message": message})

        result["extra_signs"] = extra

    if do_checkin and dx_block:
        # 单独用 conflict 状态：它不是偶发失败，重试没有意义，也不该触发
        # 当天的失败重试排程，否则一天会重复告警 4 次。
        result["checkin_status"] = "conflict"
        result["checkin_message"] = dx_block
        db.update_account(acc_id, last_status="conflict", last_message=dx_block,
                          last_run_at=db.now_iso())
        app_status = "skipped" if app_mode == "off" else "conflict"
        app_message = "APP 已关闭" if app_mode == "off" else dx_block
        result["extra_signs"] = [{"key": "app", "name": "APP", "status": app_status,
                                  "message": app_message}]
        app_at = db.now_iso()
        if app_mode != "off" or not _is_paused(account, "app_", db.run_date(app_at)):
            db.update_account(acc_id, app_last_status=app_status, app_last_message=app_message,
                              app_last_run_at=app_at)

    # 明确登录失效后停止继续调用同一登录态。
    if auth_error:
        result["days_left"] = days_left((db.get_account(acc_id) or {}).get("expire_at"))
        return result

    # ---- 3. 刷新资料 ----
    try:
        profile = longhu.fetch_profile(account)
        result["profile"] = {key: value for key, value in profile.items() if key != "_warnings"}
        fields = {key: profile[key] for key in
                  ("growth_value", "level_name", "balance", "nickname") if key in profile}
        if fields:
            db.update_account(acc_id, **fields)
        warnings = [safe(message) for message in profile.get("_warnings") or []]
        if warnings:
            result["profile_warnings"] = warnings
            db.add_log(acc_id, acc_name, "query", "todo", "；".join(warnings))
    except longhu.LonghuError as exc:
        result["error"] = result["error"] or safe(exc)
        db.add_log(acc_id, acc_name, "query", "fail", safe(exc))

    days = days_left((db.get_account(acc_id) or {}).get("expire_at"))
    result["days_left"] = days
    return result


def run_all(do_checkin: bool = True) -> list[dict]:
    accounts = db.list_accounts(only_enabled=True)
    results = []
    for account in accounts:
        try:
            results.append(run_account(account, do_checkin=do_checkin))
        except Exception as exc:  # 单个账号异常不影响其他账号
            logger.error("[%s] 执行异常（%s）", account.get("name"), type(exc).__name__)
            message = f"任务处理异常（{type(exc).__name__}），请检查服务日志"
            db.add_log(account["id"], account["name"], "run", "fail", message)
            results.append({
                "account_id": account["id"], "name": account["name"],
                "checkin_status": "fail", "checkin_message": message, "error": message,
            })
    return results


def daily_summary(results: list[dict]):
    """每轮只发一条消息，双入口直观报结果，异常附下一步。"""
    if not results:
        return
    def entry_line(name: str, status: str, detail: str) -> str:
        if status in ("ok", "already"):
            return f"{name}✅已签"
        if status == "busy":
            return f"{name}⏳执行中，本次未重复提交；请稍后刷新结果"
        if status == "skipped" and "关闭" in detail:
            return f"{name}➖已关闭"
        is_main = name == "小程序"
        actions = {
            "risk": _please_in_client(is_main, "完成验证后确认签到"),
            "todo": _please_in_client(is_main, "核对今日状态，避免重复提交"),
            "retry": "尚未提交，可稍后重试",
            "conflict": "请更新为本人独立采集的设备标识",
            "fail": "请查看面板；登录失效时重新登录并更新凭据",
        }
        action = actions.get(status, "请查看面板或官方 App 确认")
        return f"{name}⚠️{detail or '未返回结果'}\n  下一步：{action}"

    lines = [f"【龙湖签到】{db.business_date()}"]
    for item in results:
        lines.append(f"\n· {item.get('name') or '账号'}")
        lines.append(entry_line("小程序", item.get("checkin_status"),
                                item.get("checkin_message") or ""))
        app = next((entry for entry in item.get("extra_signs") or []
                    if entry.get("key") == "app"), None)
        if app is None:
            lines.append("App⚠️未执行或未返回结果；请查看面板或官方 App 确认")
        else:
            lines.append(entry_line("App", app.get("status"), app.get("message") or ""))
        if item.get("refreshed") is False:
            lines.append(f"登录续期⚠️未成功：{item.get('error') or '请检查登录并更新凭据'}")
        elif item.get("error"):
            lines.append(f"资料更新⚠️{item['error']}")
        if item.get("profile_warnings"):
            lines.append("提醒⚠️" + "；".join(item["profile_warnings"]))
    return notify("\n".join(lines))
