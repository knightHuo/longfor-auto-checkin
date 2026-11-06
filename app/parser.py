"""凭据解析：支持粘贴 cURL、上传 HAR、直接填 token 三种方式。

统一输出：{token, dx_token, dx_source, user_agent, api_key, bu_code, channel, note}
"""

import json
import re
import shlex
from urllib.parse import urlparse

from .longhu import LonghuError, validate_dx_source

DEFAULT_BU_CODE = "C20400"
DEFAULT_CHANNEL = "C2"
DEFAULT_API_KEY = "c06753f1-3e68-437d-b592-b94656ea5517"


class ParseError(Exception):
    """解析失败，消息可直接展示给用户"""


def _pick_token(headers: dict) -> str | None:
    """按可信度取 token：签到用的 X-LF-UserToken 优先，其次 token/lmToken。"""
    for name in ("x-lf-usertoken", "token", "lmtoken", "authtoken"):
        value = headers.get(name)
        if (isinstance(value, str) and 16 <= len(value) <= 4096
                and not any(c.isspace() for c in value)
                and value.lower() not in ("undefined", "null")):
            return value.strip()
    return None


def _official_url(url: object) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        return parsed.scheme == "https" and parsed.username is None and parsed.password is None and (
            host == "longfor.com" or host.endswith(".longfor.com")
        )
    except ValueError:
        return False


def _score_url(url: str) -> int:
    """给不同接口打分，越像签到请求分越高。"""
    path = urlparse(url).path
    if "signature/clock" in path:
        return 100
    if "signature/page-info" in path:
        return 90
    if "user-info" in path or "my-info" in path:
        return 80
    if "user-lz-balance" in path:
        return 70
    if "longfor.com" in url:
        return 40
    return 0


def _finish(headers: dict, source: str, score: int) -> dict:
    token = _pick_token(headers)
    if not token:
        raise ParseError("没找到 token，请确认抓的是签到或会员相关请求")

    api_key = headers.get("x-gaia-api-key") or headers.get("x-gaia-apikey")
    dx_token = (headers.get("x-lf-dxrisk-token") or "").strip()
    if dx_token.lower() in ("undefined", "null"):
        dx_token = ""
    try:
        dx_source = validate_dx_source(headers.get("x-lf-dxrisk-source"))
    except LonghuError as exc:
        raise ParseError(str(exc)) from None
    return {
        "token": token,
        "dx_token": dx_token or None,
        "dx_source": dx_source,
        "dx_source_explicit": "x-lf-dxrisk-source" in headers,
        "user_agent": headers.get("user-agent"),
        "api_key": api_key if api_key and api_key.lower() != "undefined" else DEFAULT_API_KEY,
        "bu_code": headers.get("x-lf-bucode") or headers.get("x-lf-bu-code") or DEFAULT_BU_CODE,
        "channel": headers.get("x-lf-channel") or DEFAULT_CHANNEL,
        "source": source,
        "note": f"从 {source} 解析，接口匹配度 {score}",
    }


# --------------------------------------------------------------------------
# cURL
# --------------------------------------------------------------------------

def parse_curl(text: str) -> dict:
    if "curl" not in text.lower():
        raise ParseError("这段内容里没有 curl 命令")

    # 合并续行：反斜杠续行、Windows 的 ^ 续行
    flat = text.replace("\\\n", " ").replace("\\\r\n", " ").replace("^\n", " ")

    headers = {}
    # -H 'k: v' / --header "k: v" / --header='k: v'
    pattern = re.compile(r"(?:-H|--header)[\s=]+(['\"])(.*?)\1", re.S)
    for match in pattern.finditer(flat):
        raw = match.group(2)
        if ":" in raw:
            key, value = raw.split(":", 1)
            headers[key.strip().lower()] = value.strip()

    body = ""
    body_match = re.search(r"(?:--data-raw|--data|-d)[\s=]+(['\"])(.*?)\1", flat, re.S)
    if body_match:
        body = body_match.group(2)

    # Read argument boundaries; a Referer/header/body URL is not the request target.
    try:
        arguments = shlex.split(flat)
    except ValueError:
        raise ParseError("cURL 引号或参数格式不完整，请重新复制浏览器请求") from None
    if not arguments or arguments[0].lower() not in ("curl", "curl.exe"):
        raise ParseError("请直接粘贴浏览器复制的 cURL 请求")
    values = {"-H", "--header", "-d", "--data", "--data-raw", "--data-binary", "--data-urlencode",
              "-X", "--request", "-A", "--user-agent", "-e", "--referer", "-b", "--cookie",
              "-u", "--user", "-x", "--proxy", "-F", "--form", "--form-string", "-o", "--output",
              "-w", "--write-out", "--connect-timeout", "--max-time", "--url"}
    flags = {"--compressed", "-k", "--insecure", "-L", "--location", "-s", "--silent", "-S",
             "--show-error", "--globoff", "--http1.1", "--http2", "-I", "--head", "--no-progress-meter"}
    urls = []
    index = 1
    while index < len(arguments):
        item = arguments[index]
        key, separator, inline = item.partition("=")
        if key in values:
            if separator:
                value = inline
            else:
                index += 1
                if index >= len(arguments):
                    raise ParseError("cURL 参数缺少内容")
                value = arguments[index]
            if key == "--url":
                urls.append(value)
        elif item in flags:
            pass
        elif item.startswith("http://") or item.startswith("https://"):
            urls.append(item)
        else:
            raise ParseError("此 cURL 参数格式暂不支持，请使用浏览器的 Copy as cURL")
        index += 1
    if len(urls) != 1:
        raise ParseError("请每次导入一个完整的龙湖会员请求")
    url = urls[0]
    if not _official_url(url):
        raise ParseError("请导入 HTTPS 龙湖会员请求；其他站点的凭据不能用于本服务")
    score = _score_url(url)

    result = _finish(headers, "cURL", score)
    if body:
        result["note"] += f"，请求体 {len(body)} 字节"
    return result


# --------------------------------------------------------------------------
# HAR
# --------------------------------------------------------------------------

def parse_har(raw: str | bytes) -> dict:
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        data = json.loads(raw)
    except (ValueError, TypeError, UnicodeError):
        raise ParseError("HAR 文件不是合法 JSON") from None

    if not isinstance(data, dict) or not isinstance(data.get("log"), dict):
        raise ParseError("HAR 文件缺少有效的 log 对象")
    entries = data["log"].get("entries")
    if not isinstance(entries, list):
        raise ParseError("HAR 文件缺少有效的请求列表")
    if not entries:
        raise ParseError("HAR 里没有任何请求记录")

    best_headers = None
    best_rank = (-1, "")

    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("request"), dict):
            continue
        request = entry["request"]
        url = request.get("url") or ""
        if not _official_url(url):
            continue
        headers = {}
        raw_headers = request.get("headers")
        for item in raw_headers if isinstance(raw_headers, list) else []:
            if not isinstance(item, dict):
                continue
            name, value = item.get("name"), item.get("value")
            if isinstance(name, str) and isinstance(value, str):
                headers[name.lower()] = value
        if not _pick_token(headers):
            continue
        score = _score_url(url)
        stamp = entry.get("startedDateTime")
        rank = (score, stamp if isinstance(stamp, str) else "")
        if rank >= best_rank:
            best_rank = rank
            best_headers = headers

    if not best_headers:
        raise ParseError("HAR 里没有带 token 的 longfor 请求，请确认抓到了签到或会员页面")

    return _finish(best_headers, "HAR", best_rank[0])


# --------------------------------------------------------------------------
# 直接填 token
# --------------------------------------------------------------------------

def parse_token(token: str) -> dict:
    token = (token or "").strip()
    if not token:
        raise ParseError("token 不能为空")
    if not 16 <= len(token) <= 4096:
        raise ParseError("token 长度不对，正常是 32 位十六进制")
    if not re.fullmatch(r"[0-9a-zA-Z]+", token):
        raise ParseError("token 只能是字母和数字")
    return {
        "token": token,
        "dx_token": None,
        "dx_source": validate_dx_source(),
        "user_agent": None,
        "api_key": DEFAULT_API_KEY,
        "bu_code": DEFAULT_BU_CODE,
        "channel": DEFAULT_CHANNEL,
        "source": "手动填写",
        "note": "手动填写 token，已使用默认 UA",
    }


def parse(mode: str, payload: str | bytes) -> dict:
    if mode == "curl":
        text = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else payload
        return parse_curl(text)
    if mode == "har":
        return parse_har(payload)
    if mode == "token":
        text = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else payload
        return parse_token(text)
    raise ParseError(f"不支持的导入方式：{mode}")
