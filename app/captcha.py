"""可选验证码适配器；默认关闭，本地识图尚需真实挑战样本验证。

manual 使用可见浏览器让用户处理；local（兼容 auto）使用 ddddocr 定位，
只拖动一次。工具只负责拿验证码凭据，业务成功仍须由签到接口复核。
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
SCRIPT = TOOLS_DIR / "captcha_browser.js"
MATCHER = TOOLS_DIR / "slider_match.py"


def _mode(value: str) -> str:
    value = (value or "off").strip().lower()
    return "local" if value == "auto" else value if value in ("off", "manual", "local") else "off"


def _node_env() -> dict:
    env = os.environ.copy()
    env["LONGHU_PYTHON"] = sys.executable
    locations = [TOOLS_DIR / "node_modules", TOOLS_DIR.parent / "node_modules"]
    found = [str(path) for path in locations if path.is_dir()]
    if found:
        previous = env.get("NODE_PATH")
        env["NODE_PATH"] = os.pathsep.join(found + ([previous] if previous else []))
    return env


def _last_json(output: str) -> dict | None:
    for line in reversed((output or "").splitlines()):
        try:
            value = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    return None


def available(mode: str = "manual") -> tuple[bool, str]:
    """只检查本机依赖，不打开网络、创建挑战或启动浏览器。"""
    mode = _mode(mode)
    if mode == "off":
        return False, "off"
    node = shutil.which("node")
    if node is None:
        return False, "没有 Node.js；请使用可选验证码镜像"
    if not SCRIPT.is_file() or not MATCHER.is_file():
        return False, "验证码工具文件不完整"
    try:
        proc = subprocess.run(
            [node, str(SCRIPT), "--check-deps", f"--mode={mode}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20, env=_node_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, "验证码依赖检查失败"
    result = _last_json(proc.stdout)
    if result and result.get("ok") is True and proc.returncode == 0:
        return True, "ok"
    # 不返回原始 stderr，避免子进程错误携带设备值、URL或文件内容。
    return False, "验证码依赖不完整；需 playwright-core、Chromium，local 另需 requirements-captcha.txt"


def solve(device: str, mode: str = "manual", dist: int | None = None,
          timeout: int = 120, user_agent: str | None = None) -> tuple[str | None, str]:
    """执行一次挑战；local 必须真实识图，不接受固定距离兜底。

    设备标识与 UA 通过 stdin 传入，不出现在进程命令行。失败不重复挑战。
    dist 只为旧调用保留参数位置；指定固定距离时明确拒绝。
    """
    mode = _mode(mode)
    if mode == "off":
        return None, "off"
    if dist is not None:
        return None, "不接受固定拖动距离，请使用 local 识图或 manual 人工模式"
    if not isinstance(device, str) or not device.strip() or len(device) > 4096:
        return None, "缺少有效设备标识"
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 10 <= timeout <= 180:
        return None, "验证码等待时间应为 10–180 秒"
    if user_agent is not None and (not isinstance(user_agent, str) or len(user_agent) > 4096):
        return None, "无效浏览器标识"
    ok, why = available(mode)
    if not ok:
        return None, why
    try:
        proc = subprocess.run(
            [shutil.which("node") or "node", str(SCRIPT), f"--mode={mode}", f"--timeout={timeout}"],
            input=json.dumps({"device": device.strip(), "user_agent": user_agent}),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout + 15, env=_node_env(),
        )
    except subprocess.TimeoutExpired:
        return None, "验证码处理超时，未自动重试"
    except OSError:
        return None, "验证码工具启动失败"
    result = _last_json(proc.stdout)
    if not result:
        return None, "验证码工具没有返回可识别结果"
    token = result.get("token")
    if proc.returncode == 0 and result.get("ok") is True and isinstance(token, str) and token:
        return token, "ok"
    # 工具只发固定错误码，错误详情不串入日志/通知。
    messages = {
        "unsupported_images": "当前验证码图片结构未适配，需要人工处理",
        "low_confidence": "本地识图置信度不足，未拖动",
        "invalid_geometry": "验证码图像与页面坐标不匹配，未拖动",
        "challenge_changed": "验证码在识图期间已变化，未拖动",
        "verification_rejected": "验证码验证未通过，未自动重试",
        "high_risk": "验证码服务拒绝本次验证，已停止",
        "timeout": "验证码处理超时，未自动重试",
        "dependency_missing": "验证码运行依赖缺失",
        "browser_start_failed": "验证码浏览器启动失败",
    }
    error_code = result.get("error")
    return None, messages.get(error_code if isinstance(error_code, str) else "", "验证码处理未完成，需人工确认")


def captcha_header(token: str, device: str) -> str:
    return f"{token}:{device}" if device else token


def mode_from_env() -> str:
    """LONGHU_CAPTCHA=off / manual / local；auto 为 local 的兼容别名。"""
    return _mode(os.environ.get("LONGHU_CAPTCHA", "off"))
