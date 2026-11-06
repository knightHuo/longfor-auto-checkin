"""一次本地滑块定位。stdin/stdout JSON；无网络、无凭据、无图片落盘。

仅支持已提取的 basic 拼图：背景是浏览器渲染截图，target 是原图，
target_view 描述其 CSS 裁剪。真实龙湖图片提取和阈值仍待样本验证。
ddddocr 1.6.1 的 slide_match 返回模板中心坐标与 confidence。
"""

import base64
import contextlib
import io
import json
import math
import sys

MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_INPUT_BYTES = 6 * 1024 * 1024


class MatchError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def number(value, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MatchError("invalid_geometry")
    value = float(value)
    if not math.isfinite(value) or not low <= value <= high:
        raise MatchError("invalid_geometry")
    return value


def drag_distance(match: dict, geometry: dict, image_width: float,
                  piece_width: float, min_confidence: float = 0.75) -> dict:
    """图片中心坐标 -> CSS 水平位移；SDK basic 的拼图与手柄等距移动。"""
    threshold = number(min_confidence, 0.5, 1)
    confidence = number(match.get("confidence"), 0, 1)
    if confidence < threshold:
        raise MatchError("low_confidence")
    center = match.get("target")
    if not isinstance(center, (list, tuple)) or len(center) != 2:
        raise MatchError("invalid_geometry")
    pixel_width = number(image_width, 1, 4096)
    x = number(center[0], 0, pixel_width)
    width = number(geometry.get("background_width"), 50, 1000)
    start = number(geometry.get("piece_left"), -1, width)
    piece_width = number(piece_width, 5, min(width, 200))
    track_remaining = number(geometry.get("track_remaining"), 1, 1000)
    distance = x * width / pixel_width - (start + piece_width / 2)
    if not 3 <= distance <= min(track_remaining, width - start - piece_width) + 1:
        raise MatchError("invalid_geometry")
    return {"distance": round(distance, 3), "confidence": round(confidence, 4)}


def _decode_image(value: str) -> bytes:
    if not isinstance(value, str) or len(value) > MAX_IMAGE_BYTES * 4 / 3 + 16:
        raise MatchError("unsupported_images")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        raise MatchError("unsupported_images") from None
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise MatchError("unsupported_images")
    return raw


def match_images(payload: dict) -> dict:
    try:
        # Optional dependencies stay lazy, so default service/tests need no OCR wheels.
        import ddddocr
        from PIL import Image
    except ImportError:
        raise MatchError("dependency_missing") from None

    with Image.open(io.BytesIO(_decode_image(payload.get("background")))) as source:
        if not 50 <= source.width <= 1000 or not 20 <= source.height <= 500:
            raise MatchError("unsupported_images")
        background = source.convert("RGB")
    view = payload.get("target_view") or {}
    piece_w = round(number(view.get("width"), 5, 200))
    piece_h = round(number(view.get("height"), 5, 200))
    rendered_w = round(number(view.get("image_width"), 1, 4096))
    rendered_h = round(number(view.get("image_height"), 1, 4096))
    offset_x = round(number(view.get("offset_x"), -4096, 200))
    offset_y = round(number(view.get("offset_y"), -4096, 200))
    with Image.open(io.BytesIO(_decode_image(payload.get("target")))) as source:
        if source.width > 4096 or source.height > 4096:
            raise MatchError("unsupported_images")
        target = source.convert("RGBA").resize((rendered_w, rendered_h), Image.Resampling.LANCZOS)
    cropped = Image.new("RGBA", (piece_w, piece_h))
    cropped.paste(target, (offset_x, offset_y))
    geometry = payload.get("geometry") or {}
    ratio = background.width / number(geometry.get("background_width"), 50, 1000)
    y = round(number(geometry.get("piece_top"), 0, 500) * ratio)
    height = round(piece_h * ratio)
    width = round(piece_w * ratio)
    if y + height > background.height + 1 or width >= background.width:
        raise MatchError("invalid_geometry")
    cropped = cropped.resize((width, height), Image.Resampling.LANCZOS)
    row = background.crop((0, y, background.width, y + height))
    template_data, row_data = io.BytesIO(), io.BytesIO()
    cropped.save(template_data, format="PNG")
    row.save(row_data, format="PNG")
    with contextlib.redirect_stdout(io.StringIO()):
        engine = ddddocr.DdddOcr(ocr=False, det=False, show_ad=False)
        result = engine.slide_match(template_data.getvalue(), row_data.getvalue())
    return drag_distance(result, geometry, background.width, piece_w,
                         payload.get("min_confidence", 0.75))


def main() -> int:
    if "--check-deps" in sys.argv:
        try:
            import ddddocr
            import cv2  # noqa: F401
            from PIL import Image  # noqa: F401
            with contextlib.redirect_stdout(io.StringIO()):
                ddddocr.DdddOcr(ocr=False, det=False, show_ad=False)
            print(json.dumps({"ok": True}))
            return 0
        except Exception:
            print(json.dumps({"ok": False, "error": "dependency_missing"}))
            return 1
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise MatchError("unsupported_images")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise MatchError("unsupported_images")
        result = match_images(payload)
        print(json.dumps({"ok": True, **result}))
        return 0
    except MatchError as exc:
        print(json.dumps({"ok": False, "error": exc.code}))
    except Exception:
        print(json.dumps({"ok": False, "error": "unsupported_images"}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
