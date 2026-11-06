"""通知发送适配器；返回安全状态，失败不影响签到流程。"""

from urllib.parse import urlparse

import requests


PUSHPLUS_ENDPOINT = "https://www.pushplus.plus/send"
TITLE = "龙湖签到"
BARK_DEFAULT_HOSTS = ("day.app",)
BOT_HOSTS = ("qyapi.weixin.qq.com", "oapi.dingtalk.com")


def _is_bark(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in BARK_DEFAULT_HOSTS):
        return True
    if any(host == h or host.endswith("." + h) for h in BOT_HOSTS):
        return False
    segments = [part for part in parsed.path.split("/") if part]
    return len(segments) == 1 and not parsed.query


def _build_webhook(url: str, text: str) -> tuple[str, dict]:
    if _is_bark(url):
        parsed = urlparse(url)
        key = [part for part in parsed.path.split("/") if part][0]
        return f"{parsed.scheme or 'https'}://{parsed.netloc}/push", {
            "device_key": key,
            "title": TITLE,
            "body": text,
            "group": TITLE,
        }
    return url, {"msgtype": "text", "text": {"content": text}}


def _result(ok: bool, status: str, message: str) -> dict:
    return {"ok": ok, "status": status, "message": message}


def send(text: str, settings: dict) -> dict:
    """PushPlus 的成功仅表示接口受理，不能据此声称微信已收到。"""
    session = None
    try:
        provider = (settings.get("notification_provider") or "webhook").strip().lower()
        if provider == "pushplus":
            token = (settings.get("pushplus_token") or "").strip()
            if not token:
                return _result(False, "disabled", "尚未配置 PushPlus token")
            endpoint = PUSHPLUS_ENDPOINT
            payload = {
                "token": token,
                "title": TITLE,
                "content": text,
                "template": "txt",
                "channel": "wechat",
            }
        elif provider == "webhook":
            url = (settings.get("webhook_url") or "").strip()
            if not url:
                return _result(False, "disabled", "尚未配置通知地址")
            endpoint, payload = _build_webhook(url, text)
        else:
            return _result(False, "failed", "通知方式配置无效")

        session = requests.Session()
        session.trust_env = False
        # 不跟随重定向，避免把请求体中的通知密钥发送到其他地址。
        response = session.post(endpoint, json=payload, timeout=10, allow_redirects=False)
        if not 200 <= response.status_code < 300:
            return _result(False, "failed", f"通知请求失败（HTTP {response.status_code}）")
        try:
            data = response.json()
        except ValueError:
            return _result(False, "failed", "通知服务响应格式无法识别")
        if not isinstance(data, dict):
            return _result(False, "failed", "通知服务响应格式无法识别")

        if provider == "pushplus":
            code = data.get("code")
            accepted = type(code) is int and code == 200
        else:
            code = data.get("code", data.get("errcode"))
            accepted = type(code) is int and code in (0, 200)
        if not accepted:
            # 上游 msg/响应/URL 可能回显密钥；只保留数值错误码。
            suffix = f"（code={code}）" if type(code) is int else ""
            return _result(False, "failed", "通知服务未受理" + suffix)
        return _result(True, "queued", "已受理，等待投递")
    except Exception as exc:
        # 请求异常常含完整 URL 或请求体，不能回显异常正文。
        return _result(False, "failed", f"通知发送失败（{type(exc).__name__}）")
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
