"""视觉识别。"""

import base64
import logging
import os
import re

log = logging.getLogger("meiguan.grab")

_warned: set[str] = set()


def warn_once(code: str, text: str) -> None:
    if code in _warned:
        return
    _warned.add(code)
    log.info("%s", text)


def vision_settings() -> tuple[str, str, str]:
    key = os.environ.get("VISION_API_KEY", "").strip()
    base = os.environ.get(
        "VISION_BASE_URL",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    ).strip().rstrip("/")
    model = os.environ.get("VISION_MODEL", "qwen3.5-flash").strip() or "qwen3.5-flash"
    return key, base, model


def vision_models() -> list[tuple[str, str]]:
    found = []
    for item in os.environ.get("VISION_MODELS", "").split(","):
        item = item.strip()
        if not item:
            continue
        model, _, tag = item.partition(":")
        model = model.strip()
        if model:
            found.append((model, tag.strip() or model))
    if found:
        return found
    return [("qwen3.5-flash", "flash"), ("qwen3.5-plus", "plus")]


def _safe_error(text: str) -> str:
    return re.sub(r"sk-\S+", "sk-***", text or "")[:160]


async def vision_chat(image: bytes, prompt: str, mime: str, model: str, max_tokens: int) -> str:
    key, base, default_model = vision_settings()
    if not key:
        return ""
    import httpx

    body = {
        "model": model or default_model,
        "max_tokens": max_tokens,
        "temperature": 0,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(image).decode()}"}},
                {"type": "text", "text": prompt},
            ],
        }],
    }
    try:
        async with httpx.AsyncClient(timeout=40) as http:
            resp = await http.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json=body,
            )
    except Exception as exc:
        log.info("节点：视觉识别失败 %s", type(exc).__name__)
        return ""
    if resp.status_code != 200:
        log.info("节点：视觉识别失败 HTTP %s %s", resp.status_code, _safe_error(resp.text))
        return ""
    try:
        return str(resp.json()["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError, ValueError):
        log.info("节点：视觉识别返回无法解析")
        return ""


def glyph_prompt(want_digit: bool | None, keep_case: bool = False) -> str:
    if want_digit is True:
        return "图片上是一个阿拉伯数字（0到9之间）。只回答这一个数字，不要任何其他内容。"
    if want_digit is False:
        return "图片上是一个英文大写字母（A到Z之间）。只回答这一个字母，不要任何其他内容。"
    if keep_case:
        return "图片上是一个字符，可能是数字、英文字母或汉字。英文字母保留大小写。只回答这一个字符，不要任何其他内容。"
    return "图片上是一个字符，可能是数字、大写英文字母或汉字。英文字母用大写。只回答这一个字符，不要任何其他内容。"


def parse_glyph(text: str, want_digit: bool | None, keep_case: bool = False) -> str:
    raw = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL).strip()
    if want_digit is True:
        chars = "".join(ch for ch in raw if ch.isdigit())
        return chars if chars and len(chars) <= 2 else ""
    if want_digit is False:
        chars = "".join(ch for ch in raw.upper() if ch.isascii() and ch.isalpha())
        return chars if chars and len(chars) <= 2 else ""
    compact = "".join(ch for ch in raw if not ch.isspace() and ch not in "\"'`“”‘’。，,.:：")
    if len(compact) != 1:
        return ""
    if not (compact.isascii() and compact.isalnum() or "\u4e00" <= compact <= "\u9fff"):
        return ""
    if not keep_case and compact.isascii() and compact.isalpha():
        return compact.upper()
    return compact
