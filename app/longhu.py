"""龙湖天街 API 客户端。

封装了三件事：
1. refresh_token —— 使用 token2key + key2token 尝试续期
2. checkin       —— 每日签到，严格区分「成功 / 今日已签 / 鉴权失败」
3. fetch_profile —— 拉取成长值、等级、珑珠余额

所有请求强制直连（trust_env=False），避免宿主机的抓包代理干扰。
"""

import base64
import hashlib
import json
import math
import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import requests

BASE = "https://gw2c-hw-open.longfor.com"

# 小程序前端写死的签名密钥（来自 sign-in-prize 的 JS）
SIGN_SECRET_MICRO_APP = "Q74eKtH5LePYfSjIiflUbCL2gxjTa7rF"

API_KEY_MAIN = "c06753f1-3e68-437d-b592-b94656ea5517"
API_KEY_MEMBER = "98717e7a-a039-46af-8143-be7558a089c0"
API_KEY_WALLET = "f4a2290e-c382-45b7-949c-3253d07e9b5b"

# 小程序：日日签 日日赚
ACTIVITY_NO = "11111111111686241863606037740000"
# APP：APP签到 奖励翻倍（会员端「赚珑珠」里的 App 专享入口）
APP_ACTIVITY_NO = "11111111111736501868255956070000"

# 两个活动分别查询、执行与记录结果；服务端是否允许领取以实际响应为准。
ACTIVITIES = (
    {"key": "mini", "name": "小程序", "activity_no": ACTIVITY_NO},
    {"key": "app", "name": "APP", "activity_no": APP_ACTIVITY_NO},
)

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/144.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Mac MacWechat/WMPF MacWechat/3.8.7(0x13080712) "
    "UnifiedPCMacWechat(0xf2641d55) XWEB/25561"
)

THIRTY_DAYS = 30

# 官方客户端来源；记录采集环境，不能把普通浏览器标识冒充为 App 标识。
DX_SOURCE_LABELS = {"1": "Android App", "2": "iOS App", "3": "普通浏览器", "5": "微信小程序"}
DEFAULT_DX_SOURCE = "5"  # 兼容此前已经保存的配置；新采集必须保留实际来源。


class LonghuError(Exception):
    """业务层可读错误"""


class LonghuAuthError(LonghuError):
    """明确的登录失效，不能当作临时网络错误自动重试。"""


def _check_auth(response: dict):
    message = str(response.get("message") or "")
    if str(response.get("code") or "") == "801001" or any(
        hint in message for hint in ("未登录", "登录已过期", "登录失效")
    ):
        raise LonghuAuthError("鉴权失败：登录已失效，请重新导入本人登录凭据")


def _data_object(response: dict) -> dict:
    data = response.get("data")
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise LonghuError("上游响应格式已变化，请稍后重查或更新客户端")
    return data


def validate_dx_source(value: object = None) -> str:
    if value is None or value == "":
        return DEFAULT_DX_SOURCE
    source = value.strip() if isinstance(value, str) else None
    if source not in DX_SOURCE_LABELS:
        raise LonghuError("设备标识来源无效，仅支持 Android App、iOS App、普通浏览器或微信小程序")
    return source


def redact(message: object, account: dict | None = None,
           extra_secrets: tuple = ()) -> str:
    """日志和通知只保留业务提示，不回显账号凭据。"""
    text = str(message or "")
    secrets = [
        (account or {}).get(key)
        for key in ("token", "dx_token", "api_key", "cookie")
    ] + list(extra_secrets)
    for secret in sorted({str(v) for v in secrets if v and len(str(v)) >= 8},
                         key=len, reverse=True):
        text = text.replace(secret, "[已隐藏]")
    # 处理上游回显的未知/更新后令牌，以及带凭据的 URL 参数。
    return re.sub(
        r"(?i)(\b(?:[\w-]*token|api[-_]?key|cookie|authorization)\b"
        r"[\s\"']*[:=][\s\"']*)([^\s,;\"'&}\]]+)",
        r"\1[已隐藏]", text,
    )[:500]


# --------------------------------------------------------------------------
# 底层请求
# --------------------------------------------------------------------------

def _decode(resp: requests.Response) -> dict:
    """接口有时返回裸 JSON，有时返回 base64 包裹的 JSON，两种都兼容。"""
    text = resp.text or ""
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    try:
        data = json.loads(base64.b64decode(text).decode("utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"code": "parse_error", "message": "上游响应格式无法识别"}


def _request(method: str, url: str, headers: dict, body: dict | None = None,
             timeout: int = 20) -> dict:
    session = requests.Session()
    session.trust_env = False  # 忽略系统代理，直连
    clean = {k: v for k, v in headers.items() if v not in (None, "")}
    try:
        if method.upper() == "GET":
            resp = session.get(url, headers=clean, timeout=timeout, allow_redirects=False)
        else:
            resp = session.post(url, headers=clean, json=body, timeout=timeout, allow_redirects=False)
        if resp.status_code == 401:
            raise LonghuAuthError("鉴权失败：上游拒绝登录凭据，请重新导入")
        if not 200 <= resp.status_code < 300:
            raise LonghuError(f"上游请求失败（HTTP {resp.status_code}）")
    except requests.RequestException as exc:
        # requests 异常可能包含 URL 查询参数或请求头，不向页面/日志回显。
        raise LonghuError(f"网络请求失败（{type(exc).__name__}）") from None
    finally:
        session.close()
    return _decode(resp)


def sign_headers(body: dict | None = None) -> dict:
    """复刻小程序签名：md5(排序参数 + '&' + 毫秒时间戳 + '&' + secret)"""
    parts = []
    for key in sorted((body or {}).keys()):
        value = body[key]
        if isinstance(value, (dict, list)):
            continue
        text = str(value)
        if text.strip() and text != "null":
            parts.append(f"{key}={text}")
    joined = "|".join(parts)
    ts = str(int(time.time() * 1000))
    raw = (joined + "&" if joined else "") + ts + "&" + SIGN_SECRET_MICRO_APP
    return {
        "X-LONGZHU-TimeStamp": ts,
        "X-Client-Type": "microApp",
        "X-LONGZHU-Sign": hashlib.md5(raw.encode("utf-8")).hexdigest(),
    }


# --------------------------------------------------------------------------
# 业务接口
# --------------------------------------------------------------------------

def refresh_token(account: dict) -> tuple[bool, str]:
    """滚动续期。成功返回 (True, 新token)，失败返回 (False, 原因)。

    接口不保证返回实际到期时间；面板的 30 天仅用于估算，不是有效期承诺。
    """
    token = account.get("token") or ""
    if not token:
        return False, "缺少 token"

    headers = {
        "lmToken": token,
        "X-LF-Bucode": account.get("bu_code", "C20400"),
        "X-LF-Api-Version": "v2_6_0",
        "X-LF-Channel": account.get("channel", "C2"),
        "X-LF-App-Version": "2.6.0",
        "Content-Type": "application/json",
        "User-Agent": account.get("user_agent") or DEFAULT_UA,
        "Origin": "https://longzhu.longfor.com",
        "Referer": "https://longzhu.longfor.com/",
    }
    headers.update(sign_headers({}))

    res = _request("POST", f"{BASE}/supera/mine/v2_6_0/token/token2key", headers, {})
    _check_auth(res)
    key = _data_object(res).get("key")
    if not isinstance(key, str) or not key.strip():
        return False, redact(res.get("message") or "token2key 未返回 key（token 可能已失效）", account)

    wallet_headers = {
        "X-Gaia-Api-Key": API_KEY_WALLET,
        "systype": "WALLET",
        "Content-Type": "application/json",
        "User-Agent": account.get("user_agent") or DEFAULT_UA,
        "Origin": "https://longball.longhu.net",
        "Referer": "https://longball.longhu.net/",
    }
    res2 = _request(
        "POST",
        f"{BASE}/lmember-lpns-api-prod/api/lpns/auth/v1/key2token",
        wallet_headers,
        {"key": key, "buCode": account.get("bu_code", "C20400"), "channel": account.get("channel", "C2")},
    )
    _check_auth(res2)
    data = _data_object(res2)
    if (res2.get("code") == "0000" and isinstance(data.get("token"), str)
            and data["token"] and not any(char.isspace() for char in data["token"])):
        return True, data["token"]
    return False, redact(res2.get("message") or "key2token 未返回 token", account, (key,))


def _task_headers(account: dict) -> dict:
    """活动接口（page-info / clock）共用的请求头。"""
    headers = {
        "User-Agent": account.get("user_agent") or DEFAULT_UA,
        "Origin": "https://longzhu.longfor.com",
        "Referer": "https://longzhu.longfor.com/",
        "X-LF-DXRisk-Source": validate_dx_source(account.get("dx_source")),
        "X-LF-Bu-Code": account.get("bu_code", "C20400"),
        # 签到必须用签到专用 key（前端写死的常量）。若沿用抓包里的 key，
        # 用户粘贴的是会员接口 cURL 时会拿到 member key，导致 401。
        "X-GAIA-API-KEY": API_KEY_MAIN,
        "X-LF-UserToken": account.get("token") or "",
        "X-LF-Channel": account.get("channel", "C2"),
        # 真实 App 未过滑块时，前端就是把这个字符串原样发出去的（抓包可见）。
        # 过了滑块会变成 "<token>:<指纹>"，两个值用冒号拼接。
        "X-LF-DXRisk-Captcha-Token": "undefined",
        "token": account.get("token") or "",
        "Content-Type": "application/json;charset=UTF-8",
    }
    # 只发送账号已保存的真实采集标识，并保留其来源，不用临时生成的值兜底。
    fingerprint = (account.get("dx_token") or "").strip()
    if fingerprint:
        headers["X-LF-DXRisk-Token"] = fingerprint
    return headers


def sign_state(account: dict, activity_no: str = ACTIVITY_NO) -> bool | None:
    """查询「今天是否已签到」。True=已签，False=未签，None=查不到。

    以服务端返回的日历为准，比依赖 clock 接口的 is_popup 可靠：
    clock 在「已签到」和「签到被拒」两种情况下都可能不返回奖励明细。
    """
    headers = _task_headers(account)  # 配置错误明确抛出，不当成一次网络失败。
    try:
        res = _request(
            "POST",
            f"{BASE}/lmarketing-task-api-mvc-prod/openapi/task/v1/signature/page-info",
            headers,
            {"activity_no": activity_no},
        )
    except LonghuAuthError:
        raise
    except LonghuError:
        return None
    _check_auth(res)
    if res.get("code") != "0000":
        return None

    data = res.get("data") or {}
    if not isinstance(data, dict):
        return None
    # 用服务端时间判断“今天”，避免容器时区与龙湖不一致
    business_tz = timezone(timedelta(hours=8))
    today = datetime.now(business_tz)
    raw = data.get("current_date_time")
    if raw:
        try:
            today = datetime.fromtimestamp(int(raw) / 1000, business_tz)
        except (TypeError, ValueError, OverflowError, OSError):
            pass

    for item in data.get("seven_days_signs") or []:
        if not isinstance(item, dict):
            continue
        try:
            day = (int(item.get("sign_year")), int(item.get("sign_month")), int(item.get("sign_day")))
        except (TypeError, ValueError):
            continue
        if day == (today.year, today.month, today.day):
            # 20 = 已签到，10 = 未签到
            try:
                status = int(item.get("sign_status"))
            except (TypeError, ValueError):
                return None
            return {20: True, 10: False}.get(status)
    return None


CODE_HINT = {
    # 验证频率及设备是否被接受由上游决定，不能承诺人工验证一次后永久可用。
    "801810": "风控拒绝：请在官方客户端确认状态并完成必要验证",
    "801811": "需要滑块验证：请在 App 内完成验证后，再检查签到状态",
    "801812": "风控拒绝：请在官方客户端确认状态并完成必要验证",
    "8040012": "需要人机验证：请在 App 内确认并完成验证；验证频率由服务端决定",
    "8040016": "需要人机验证：请在 App 内确认并完成验证；验证频率由服务端决定",
}

# 风控拒绝转为人工处理，避免把需要验证的响应当作普通临时错误不断重试。
RISK_CODES = {"801810", "801811", "801812", "8040012", "8040016"}


def _solve_captcha(account: dict) -> tuple[str | None, str]:
    """过滑块换 captcha token。没开启（默认）就直接返回 off。"""
    try:
        from . import captcha as captcha_mod
    except Exception as exc:  # 缺依赖不应该影响正常签到
        return None, f"求解器不可用（{type(exc).__name__}）"
    mode = captcha_mod.mode_from_env()
    if mode == "off":
        return None, "off"
    device = (account.get("dx_token") or "").strip()
    if not device:
        return None, "缺少设备指纹"
    return captcha_mod.solve(device, mode=mode,
                             user_agent=account.get("user_agent") or DEFAULT_UA)


def _captcha_header(token: str, device: str) -> str:
    """接口要求的格式：token 在前，设备指纹在后，冒号拼接。"""
    return f"{token}:{device}" if device else token


def checkin(account: dict, activity_no: str = ACTIVITY_NO,
            label: str = "", on_submit: Callable[[], None] | None = None) -> tuple[str, str]:
    """每日签到。返回 (status, message)

    status: ok（签到成功）/ already（今日已签）/ retry（未提交，可重试）/ todo（待确认）/
            risk（风控拒绝）/ fail（其他失败）
    """
    tag = f"{label}" if label else ""
    try:
        validate_dx_source(account.get("dx_source"))
    except LonghuError as exc:
        return "todo", f"{tag}{exc}，未提交签到，请修正配置"
    # 先查状态：已签就不要再打签到接口，免得无谓地触发风控
    try:
        before = sign_state(account, activity_no)
    except LonghuAuthError as exc:
        return "todo", f"{tag}{exc}，未提交签到"
    if before is True:
        return "already", f"{tag}今日已签到"
    if before is None:
        return "retry", f"{tag}暂时无法查询今日状态，尚未提交，将稍后重查；持续失败请检查登录"

    headers = _task_headers(account)
    # 先持久化提交意图，再发请求；写库失败必须阻止请求。
    # 若进程在上游执行后退出，重启仍会把该入口视为待确认而只读复核。
    if on_submit is not None:
        on_submit()
    try:
        res = _request(
            "POST",
            f"{BASE}/lmarketing-task-api-mvc-prod/openapi/task/v1/signature/clock",
            headers,
            {"activity_no": activity_no},
        )
    except LonghuError as exc:
        # 提交发生网络异常时，服务端可能已执行；禁止自动重发。
        return "todo", f"{tag}签到请求结果未知：{redact(exc, account)}，请在 App 内确认"

    code = redact(res.get("code") or "", account)
    message = redact(res.get("message") or "", account)

    if code == "0000":
        # 奖励弹窗不能替代日历复核；成长值和珑珠也不能混加成“积分”。
        try:
            after = sign_state(account, activity_no)
        except LonghuAuthError:
            after = None
        if after is True:
            return "ok", f"{tag}签到成功（已按日历确认）"
        if after is False:
            return "todo", f"{tag}签到接口已返回，但日历尚未确认；请稍后只读核对，未自动重发"
        return "todo", f"{tag}签到已提交，但日历状态查询失败，请在 App 内确认"

    try:
        _check_auth(res)
    except LonghuAuthError as exc:
        return "todo", str(exc)

    # 兜底：接口报错但可能实际上已经签上（历史上出现过报错但状态已更新）
    try:
        confirmed = sign_state(account, activity_no)
    except LonghuAuthError:
        confirmed = None
    if confirmed is True:
        return "ok", f"{tag}签到成功（接口返回 {code}，已按日历确认）"
    if code in ("", "parse_error"):
        return "todo", f"{tag}签到返回内容无法确认，请在 App 内核实，未自动重试"

    hint = CODE_HINT.get(code, "")
    detail = f"{message or '未知错误'}（code={code or '无'}{'，' + hint if hint else ''}）"
    if code in RISK_CODES:
        # 仅显式启用实验求解器时，尝试取得验证凭证并追加一次提交。
        if code in ("8040012", "8040016"):
            token, why = _solve_captcha(account)
            if token:
                device = (account.get("dx_token") or "").strip()
                retry_headers = _task_headers(account)
                retry_headers["X-LF-DXRisk-Captcha-Token"] = _captcha_header(token, device)
                if on_submit is not None:
                    on_submit()
                try:
                    res2 = _request(
                        "POST",
                        f"{BASE}/lmarketing-task-api-mvc-prod/openapi/task/v1/signature/clock",
                        retry_headers,
                        {"activity_no": activity_no},
                    )
                except LonghuError as exc:
                    return "todo", f"{tag}过滑块后签到结果未知：{redact(exc, account, (token,))}，请在 App 内确认"
                code2 = redact(res2.get("code") or "", account, (token,))
                try:
                    after = sign_state(account, activity_no)
                except LonghuAuthError:
                    after = None
                if after is True:
                    return "ok", f"{tag}签到成功（已过滑块，按日历确认）"
                if after is None:
                    return "todo", f"{tag}过滑块后签到已提交，但日历状态查询失败，请在 App 内确认"
                detail = f"{detail}；过滑块后仍失败（code={code2 or '无'}）"
            elif why and why != "off":
                detail = f"{detail}；滑块求解未完成：{redact(why, account)}"
        # 风控拒绝：当天重试无意义，标 risk 不进入失败重试排程
        return "risk", f"{tag}签到失败：{detail}"
    return "fail", f"{tag}签到失败：{detail}"


def fetch_profile(account: dict) -> dict:
    """拉取成长值 / 等级 / 珑珠。token 无效时抛 LonghuError。"""
    headers = {
        "User-Agent": account.get("user_agent") or DEFAULT_UA,
        "lmToken": account.get("token") or "",
        "X-Gaia-Api-Key": API_KEY_MEMBER,
        "X-LF-Bucode": account.get("bu_code", "C20400"),
        "X-LF-Channel": account.get("channel", "C2"),
        "X-LF-Api-Version": "v2_6_0",
        "Content-Type": "application/json",
    }
    res = _request(
        "GET",
        f"{BASE}/supera/member/api/bff/pages/v2_6_0/v1/user-info",
        headers,
    )
    _check_auth(res)
    if res.get("code") != "0000":
        raise LonghuError(redact(res.get("message") or "用户信息查询失败", account))

    data = _data_object(res)
    profile = {}
    warnings = []
    raw_growth = data.get("growthValue")
    try:
        if raw_growth is None or isinstance(raw_growth, bool):
            raise ValueError
        growth = int(raw_growth)
        if isinstance(raw_growth, float) and raw_growth != growth:
            raise ValueError
        profile["growth_value"] = growth
    except (TypeError, ValueError, OverflowError):
        warnings.append("成长值暂未刷新，保留上次记录")
    for source, target in (("levelName", "level_name"), ("nickName", "nickname")):
        raw = data.get(source)
        if target == "nickname" and raw is None:
            raw = data.get("nickname")
        if isinstance(raw, str):
            profile[target] = raw

    balance_headers = dict(headers)
    balance_available = False
    try:
        res2 = _request(
            "GET",
            f"{BASE}/supera/member/api/bff/pages/v2_6_0/v2/user-lz-balance",
            balance_headers,
        )
        if res2.get("code") == "0000":
            bdata = res2.get("data") or {}
            if isinstance(bdata, dict):
                for source, target in (("balance", "balance"), ("expiringLz", "expiring_lz")):
                    raw = bdata.get(source)
                    if raw is None or isinstance(raw, bool):
                        continue
                    try:
                        value = float(raw)
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if math.isfinite(value):
                        profile[target] = value
                balance_available = "balance" in profile
    except LonghuError:
        pass
    if not balance_available:
        warnings.append("珑珠余额暂未刷新，保留上次记录")
    if warnings:
        profile["_warnings"] = warnings
    return profile


def probe_token(token: str, user_agent: str | None = None,
                bu_code: str = "C20400", channel: str = "C2") -> dict:
    """校验一个 token 是否可用，返回 profile。失败抛 LonghuError。"""
    return fetch_profile({
        "token": token,
        "user_agent": user_agent or DEFAULT_UA,
        "api_key": API_KEY_MAIN,
        "bu_code": bu_code,
        "channel": channel,
    })


def expire_at(seconds: int = THIRTY_DAYS * 86400) -> str:
    """按当前时间估算到期时刻；上游可提前失效，不能视作服务端保证。"""
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
