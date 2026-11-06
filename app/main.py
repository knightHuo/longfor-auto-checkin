"""龙湖签到服务：Web 前端 + REST API + 内置定时任务。"""

import logging
import re
from html import escape
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import config, db, longhu, parser, run_guard, runner, scheduler, security
from . import __version__

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
MAX_IMPORT_BYTES = 5 * 1024 * 1024


@asynccontextmanager
async def lifespan(application: FastAPI):
    application.state.console_credentials = security.load_credentials()
    db.init()
    if _schedule_enabled():
        scheduler.start()
        logger.info("服务已启动，签到窗口 %s", scheduler.window_text())
    else:
        logger.info("服务已启动，自动调度已暂停")
    try:
        yield
    finally:
        if scheduler.scheduler.running:
            scheduler.scheduler.shutdown(wait=False)


def _schedule_enabled() -> bool:
    return config.schedule_enabled()


def _next_run() -> str:
    return scheduler.next_run_text() if _schedule_enabled() else ""


app = FastAPI(title="Longfor Auto Check-in", version=__version__, lifespan=lifespan)
app.add_middleware(security.ConsoleAuthMiddleware)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    # Validation errors can otherwise echo credentials supplied in form fields.
    return JSONResponse({"detail": "请求参数格式不正确，请检查输入内容"}, status_code=422)


# --------------------------------------------------------------------------
# 页面
# --------------------------------------------------------------------------

@app.get("/")
def index(request: Request):
    page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    prefix = escape(request.scope.get("root_path", "").rstrip("/"), quote=True)
    return HTMLResponse(page.replace("__APP_ROOT__", prefix))


# --------------------------------------------------------------------------
# 账号
# --------------------------------------------------------------------------

def _daily_status(account: dict, today: str, app_mode: str) -> dict:
    mini = (account.get("last_status") or "pending") if db.run_date(account.get("last_run_at")) == today else "pending"
    app_status = (account.get("app_last_status") or "pending") if db.run_date(account.get("app_last_run_at")) == today else "pending"
    if app_mode == "off":
        app_status = "skipped"
    required = [mini] if app_mode == "off" else [mini, app_status]
    completed = [status in ("ok", "already") for status in required]
    if not account.get("enabled", True):
        combined = "pending"
    elif all(completed):
        combined = "ok" if "ok" in required else "already"
    elif any(completed):
        combined = "partial"
    else:
        combined = next((status for status in ("busy", "risk", "conflict", "fail", "retry", "todo") if status in required), "pending")
    return {"mini_today_status": mini, "app_today_status": app_status, "today_status": combined}


def _decorate(account: dict, usage: dict[str, list[dict]] | None = None,
              today: str | None = None, app_mode: str = "auto") -> dict:
    item = dict(account)
    for key in ("token", "user_agent", "api_key", "dx_token"):  # 敏感字段不外发
        item.pop(key, None)
    token = account.get("token") or ""
    item["token_mask"] = f"{token[:6]}...{token[-4:]}" if len(token) > 12 else "****"

    # 设备指纹状态。同一指纹用于多个账号会触发风控（2026-11-03 实测），
    # 指纹归 id 最小的账号所有，其余账号会被标记为冲突并暂停签到。
    dx = (account.get("dx_token") or "").strip()
    item["has_dx"] = bool(dx)
    source = account.get("dx_source") or longhu.DEFAULT_DX_SOURCE
    item["dx_source"] = source if source in longhu.DX_SOURCE_LABELS else None
    item["dx_source_label"] = longhu.DX_SOURCE_LABELS.get(source, "未知")
    # 只看启用中的账号，与运行时 _dx_conflict 的口径保持一致
    shared = [
        a for a in (usage or {}).get(dx, [])
        if a["id"] != account["id"] and a.get("enabled")
    ] if dx else []
    item["dx_shared"] = bool(shared)
    item["dx_shared_with"] = [a["name"] for a in shared]
    item["dx_conflict"] = any(a["id"] < account["id"] for a in shared)
    item["days_left"] = runner.days_left(account.get("expire_at"))
    account_id = account.get("id")
    item["is_busy"] = run_guard.is_busy(account_id) if account_id is not None else False
    item.update(_daily_status(account, today or db.business_date(), app_mode))
    return item


@app.get("/api/accounts")
def api_accounts():
    usage = db.dx_usage()
    today = db.business_date()
    app_mode = db.get_settings().get("app_checkin_mode", "auto")
    return [_decorate(acc, usage, today, app_mode) for acc in db.list_accounts()]


@app.post("/api/accounts/bind")
async def api_bind(
    mode: str = Form(...),
    name: str = Form("", max_length=128),
    payload: str = Form("", max_length=MAX_IMPORT_BYTES),
    dx_token: str = Form("", max_length=2048),
    dx_source: str | None = Form(None),
    file: UploadFile | None = File(None),
    account_id: Annotated[int | None, Form()] = None,
):
    previous = db.get_account(account_id) if account_id is not None else None
    if account_id is not None and previous is None:
        raise HTTPException(404, "账号不存在，请刷新后重试")
    if mode == "har":
        if file is None:
            raise HTTPException(400, "HAR 模式需要上传文件")
        raw = await file.read(MAX_IMPORT_BYTES + 1)
        if len(raw) > MAX_IMPORT_BYTES:
            raise HTTPException(413, "导入文件过大，请只导出龙湖会员相关请求（最多5 MiB）")
    else:
        raw = payload

    try:
        parsed = parser.parse(mode, raw)
    except parser.ParseError as exc:
        raise HTTPException(400, str(exc)) from exc
    incoming_dx = dx_token.strip() or (parsed.get("dx_token") or "").strip()
    if previous and not incoming_dx and dx_source not in (None, "", previous.get("dx_source")):
        raise HTTPException(400, "更改设备来源时，请同时提供该环境实际采集的设备标识")
    try:
        source_value = longhu.validate_dx_source(
            dx_source if dx_source not in (None, "") else
            previous.get("dx_source") if previous and (not incoming_dx or not parsed.get("dx_source_explicit")) else parsed.get("dx_source"))
    except longhu.LonghuError as exc:
        raise HTTPException(400, str(exc)) from None

    account = {
        "token": parsed["token"],
        "user_agent": previous.get("user_agent") if previous and not incoming_dx else parsed.get("user_agent") or (previous or {}).get("user_agent") or longhu.DEFAULT_UA,
        "api_key": parsed.get("api_key"),
        "bu_code": parsed.get("bu_code", "C20400"),
        "channel": parsed.get("channel", "C2"),
        "dx_source": source_value,
    }
    if previous and (mode == "token" or not incoming_dx):
        for field in ("api_key", "bu_code", "channel"):
            account[field] = previous.get(field) or account[field]

    # 一账号一指纹：共用指纹会触发风控，提前拦掉，别等跑起来才暴露。
    dx_value = incoming_dx or (previous or {}).get("dx_token") or ""
    if dx_value:
        others = db.accounts_with_dx(dx_value, exclude_id=account_id) if account_id is not None else db.accounts_with_dx(dx_value)
        if others:
            names = "、".join(a["name"] for a in others)
            raise HTTPException(
                400,
                f"这个指纹已被「{names}」占用。同一指纹用于多个账号会触发风控，"
                "请从该账号的独立设备环境重新采集。",
            )

    # 校验 token：先直接查资料，失败则尝试续期后重试
    profile = None
    verify_error = ""
    try:
        profile = await run_in_threadpool(longhu.probe_token,
            account["token"], account["user_agent"], account["bu_code"], account["channel"]
        )
    except longhu.LonghuError as exc:
        verify_error = str(exc)
        try:
            ok, new_token = await run_in_threadpool(longhu.refresh_token, account)
        except longhu.LonghuError as refresh_error:
            ok, new_token = False, ""
            verify_error = longhu.redact(refresh_error, account)
        if ok:
            account["token"] = new_token
            try:
                profile = await run_in_threadpool(longhu.probe_token,
                    account["token"], account["user_agent"],
                    account["bu_code"], account["channel"],
                )
                verify_error = ""
            except longhu.LonghuError as exc2:
                verify_error = str(exc2)

    if profile is None:
        raise HTTPException(400, f"凭据校验失败：{verify_error or '未知原因'}，请重新抓取")

    final_name = (name or "").strip() or (previous or {}).get("name") or profile.get("nickname") or f"龙湖账号 {len(db.list_accounts()) + 1}"

    fields = {
        "name": final_name,
        "token": account["token"],
        "user_agent": account["user_agent"],
        "api_key": account["api_key"],
        "dx_token": dx_value or None,
        "dx_source": source_value,
        "bu_code": account["bu_code"],
        "channel": account["channel"],
        "growth_value": profile.get("growth_value", 0),
        "level_name": profile.get("level_name", ""),
        "balance": profile.get("balance", 0),
        "nickname": profile.get("nickname", ""),
        "expire_at": longhu.expire_at(),
    }
    if previous:
        # Credential replacement shares the execution lock, so an in-flight run
        # cannot overwrite the newly verified session with an older refresh.
        lock = run_guard.try_acquire(account_id)
        if lock is None:
            raise HTTPException(409, "该账号正在执行，请等待结束后更新登录")
        try:
            current = db.get_account(account_id)
            if current is None:
                raise HTTPException(404, "账号已删除，请刷新后重试")
            changed = any(fields.get(key) != current.get(key) for key in ("token", "user_agent", "dx_token", "dx_source"))
            # A partial profile refresh must not erase previously known data.
            for field in ("growth_value", "level_name", "balance", "nickname"):
                if field not in profile:
                    fields.pop(field, None)
            if changed:
                fields.update(db.execution_reset_fields())
            db.update_account(account_id, **fields)
        except ValueError:
            raise HTTPException(400, "该登录凭据已绑定其他账号，请更新原记录") from None
        finally:
            lock.release()
    else:
        try:
            account_id = db.create_account(fields)
        except ValueError:
            raise HTTPException(400, "该账号已经存在，请选择更新原账号登录") from None
    db.add_log(account_id, final_name, "bind", "ok", f"{parsed['note']}，凭据校验通过")

    return {"ok": True, "id": account_id, "name": final_name, "profile": profile, "updated": bool(previous)}


@app.post("/api/accounts/{account_id}/dx")
def api_set_dx(account_id: int, dx_token: str = Form(""), dx_source: str | None = Form(None)):
    lock = run_guard.try_acquire(account_id)
    if lock is None:
        raise HTTPException(409, "该账号正在执行，请等待结束后更新设备")
    try:
        return _set_device(account_id, dx_token, dx_source)
    finally:
        lock.release()


def _set_device(account_id: int, dx_token: str, dx_source: str | None):
    """保存实际采集的设备标识及来源；保存本身不代表通过风控。"""
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(404, "账号不存在")
    try:
        source_value = longhu.validate_dx_source(dx_source if dx_source not in (None, "") else account.get("dx_source"))
    except longhu.LonghuError as exc:
        raise HTTPException(400, str(exc)) from None
    value = dx_token.strip()
    if value and (len(value) < 20 or " " in value):
        raise HTTPException(400, "设备标识格式无效，请粘贴当前设备真实采集的值")

    # 一账号一指纹：共用指纹会被上游判成同一设备多账号，直接触发风控。
    if value:
        others = db.accounts_with_dx(value, exclude_id=account_id)
        if others:
            names = "、".join(a["name"] for a in others)
            raise HTTPException(
                400,
                f"这个指纹已被「{names}」占用。同一指纹用于多个账号会触发风控，"
                "请从该账号的独立设备环境重新采集。",
            )

    db.update_account(account_id, dx_token=value or None, dx_source=source_value)
    db.add_log(account_id, account["name"], "dx",
               "ok" if value else "fail",
               f"已保存设备标识（{longhu.DX_SOURCE_LABELS[source_value]}），风控有效性待业务验证" if value else "已清除设备标识")
    return {"ok": True, "has_dx": bool(value), "dx_source": source_value,
            "dx_source_label": longhu.DX_SOURCE_LABELS[source_value]}


@app.delete("/api/accounts/{account_id}")
def api_delete(account_id: int):
    if not db.get_account(account_id):
        raise HTTPException(404, "账号不存在")
    db.delete_account(account_id)
    return {"ok": True}


@app.get("/api/accounts/{account_id}/status")
def api_account_status(account_id: int):
    """只读核对两路日历与资料，不续期、不签到，也不写执行记录。"""
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(404, "账号不存在")
    result = {"account_id": account_id, "mini": "unknown", "app": "unknown",
              "profile": {}, "errors": []}
    for activity in longhu.ACTIVITIES:
        try:
            state = longhu.sign_state(account, activity["activity_no"])
            result[activity["key"]] = "signed" if state is True else "not_signed" if state is False else "unknown"
            if state is not True and state is not False:
                result["errors"].append(f"{activity['name']}签到状态暂时无法确认")
        except longhu.LonghuError as exc:
            result["errors"].append(f"{activity['name']}：{longhu.redact(exc, account)}")
        except Exception:
            result["errors"].append(f"{activity['name']}签到状态查询异常，请稍后确认")
    try:
        profile = longhu.fetch_profile(account)
        allowed = ("growth_value", "level_name", "nickname", "balance", "expiring_lz")
        result["profile"] = {key: profile[key] for key in allowed if key in profile}
    except longhu.LonghuError as exc:
        result["errors"].append(f"资料查询：{longhu.redact(exc, account)}")
    except Exception:
        result["errors"].append("资料查询异常，请稍后确认")
    return result


@app.post("/api/accounts/{account_id}/run")
def api_run(account_id: int, app: str | None = None):
    """立即跑一轮。app=auto 强制尝试 APP 入口，app=off 跳过，默认按设置。"""
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(404, "账号不存在")
    mode = (app or "").strip().lower() or None
    if mode and mode not in runner.APP_MODES:
        raise HTTPException(400, "app 只能是 auto / manual / off")
    result = runner.run_account(account, do_checkin=True, app_mode=mode)
    runner.daily_summary([result])
    return result


@app.get("/api/accounts/{account_id}/logs")
def api_account_logs(account_id: int, limit: int = 20):
    return db.list_logs(account_id, limit)


# --------------------------------------------------------------------------
# 概览 / 设置 / 日志
# --------------------------------------------------------------------------

@app.get("/api/overview")
def api_overview():
    accounts = db.list_accounts()
    today = db.business_date()
    app_mode = db.get_settings().get("app_checkin_mode", "auto")
    stats = {key: 0 for key in ("ok", "already", "fail", "pending", "conflict", "risk", "partial", "retry", "busy")}
    min_days = None

    for acc in accounts:
        days = runner.days_left(acc.get("expire_at"))
        if days is not None:
            min_days = days if min_days is None else min(min_days, days)
        status = _daily_status(acc, today, app_mode)["today_status"]
        stats[status if status in stats else "pending"] += 1

    return {
        "total": len(accounts),
        "today": today,
        "app_checkin_mode": app_mode,
        "stats": stats,
        "min_days": min_days,
        "next_run": _next_run(),
        "schedule_enabled": _schedule_enabled(),
        "checkin_time": db.get_settings().get("checkin_time", "08:05"),
    }


@app.get("/api/logs")
def api_logs(limit: int = 50):
    return db.list_logs(None, limit)


@app.get("/api/settings")
def api_get_settings():
    data = db.get_settings()
    return {
        "checkin_time": data.get("checkin_time", "08:00"),
        "checkin_jitter": data.get("checkin_jitter", "60"),
        "window": scheduler.window_text(),
        "webhook_configured": bool(data.get("webhook_url")),
        "notification_provider": data.get("notification_provider", "webhook"),
        "pushplus_configured": bool(data.get("pushplus_token")),
        "app_checkin_mode": data.get("app_checkin_mode", "manual"),
        "next_run": _next_run(),
        "schedule_enabled": _schedule_enabled(),
    }


@app.post("/api/settings")
def api_save_settings(
    checkin_time: str = Form(...),
    webhook_url: str | None = Form(None, max_length=2048),
    checkin_jitter: str | None = Form(None),
    app_checkin_mode: str | None = Form(None),
    notification_provider: str | None = Form(None),
    pushplus_token: str | None = Form(None),
    clear_pushplus_token: bool = Form(False),
    clear_webhook_url: bool = Form(False),
):
    try:
        hour, minute = checkin_time.split(":")
        assert 0 <= int(hour) <= 23 and 0 <= int(minute) <= 59
    except Exception:
        raise HTTPException(400, "时间格式应为 HH:MM") from None

    values = {"checkin_time": checkin_time}
    if clear_webhook_url:
        values["webhook_url"] = ""
    elif webhook_url is not None and webhook_url.strip():
        value = webhook_url.strip()
        try:
            parsed = urlsplit(value)
            if (parsed.scheme not in ("https", "http") or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or any(c.isspace() for c in value)):
                raise ValueError("invalid webhook")
        except ValueError:
            raise HTTPException(400, "推送地址格式不正确") from None
        values["webhook_url"] = value
    if checkin_jitter is not None and checkin_jitter.strip() != "":
        try:
            jitter = int(float(checkin_jitter))
            assert 0 <= jitter <= 1439
        except Exception:
            raise HTTPException(400, "随机浮动应为 0–1439 之间的分钟数") from None
        values["checkin_jitter"] = str(jitter)

    if app_checkin_mode is not None and app_checkin_mode.strip() != "":
        mode = app_checkin_mode.strip().lower()
        if mode not in runner.APP_MODES:
            raise HTTPException(400, "APP 入口方式只能是 auto / manual / off")
        values["app_checkin_mode"] = mode

    if notification_provider is not None:
        if notification_provider not in ("webhook", "pushplus"):
            raise HTTPException(400, "通知方式只能是 webhook 或 pushplus")
        values["notification_provider"] = notification_provider

    if clear_pushplus_token:
        values["pushplus_token"] = ""
    elif pushplus_token is not None and pushplus_token.strip():
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", pushplus_token):
            raise HTTPException(400, "PushPlus token 格式不正确，请填写完整 token，不要包含空白或链接")
        values["pushplus_token"] = pushplus_token

    schedule_changed = False
    if _schedule_enabled():
        previous = db.get_settings()
        schedule_changed = any(
            key in values and values[key] != previous.get(key, db.DEFAULT_SETTINGS[key])
            for key in ("checkin_time", "checkin_jitter")
        )
    db.save_settings(values)
    if _schedule_enabled() and schedule_changed:
        scheduler.reschedule()
    return {
        "ok": True,
        "window": scheduler.window_text(),
        "next_run": _next_run(),
        "schedule_enabled": _schedule_enabled(),
    }


@app.post("/api/notifications/test")
def api_test_notification():
    """仅测试已保存的通知配置，不执行续期、查询或签到。"""
    settings = db.get_settings()
    if settings.get("notification_provider", "webhook") == "pushplus" and not settings.get("pushplus_token"):
        return JSONResponse({"ok": False, "status": "disabled", "message": "请先保存 PushPlus token"}, status_code=400)
    result = runner.notify("【龙湖自动签到】通知测试\n这是一条测试消息，未执行签到。")
    status = result.get("status") if isinstance(result, dict) else "failed"
    if status == "queued":
        return {"ok": True, "status": "queued", "message": "已受理，等待投递；请在微信中确认"}
    if status == "disabled":
        return JSONResponse({"ok": False, "status": "disabled", "message": "通知尚未配置，请先保存通知设置"}, status_code=400)
    return JSONResponse({"ok": False, "status": "failed", "message": "测试通知未受理，请检查通知配置或稍后重试"}, status_code=502)


@app.get("/api/health")
def api_health():
    return {"ok": True}
