from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional

from yarl import URL

IWARA_SECRET = "mSvL05GfEmeEmsEYfGCnVpEjYgTJraJN"
VIDEO_ID_RE = re.compile(r"(?:https?://(?:www\.)?iwara\.tv)?/video/([A-Za-z0-9]+)")
IMAGE_ID_RE = re.compile(r"(?:https?://(?:www\.)?iwara\.tv)?/image/([A-Za-z0-9]+)")
HTML_TAG_RE = re.compile(r"<[^>]+>")


# ---- config ----


def get_int_config(
    config: Dict[str, Any], key: str, default: int, min_value: int, max_value: int
) -> int:
    value = config.get(key, default)
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(min_value, min(max_value, number))


def get_str_config(config: Dict[str, Any], key: str, default: str) -> str:
    value = config.get(key, default)
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def get_bool_config(config: Dict[str, Any], key: str, default: bool) -> bool:
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def proxy_url(config: Dict[str, Any]) -> Optional[str]:
    p = get_str_config(config, "proxy_url", "")
    return p or None


def site_host(config: Dict[str, Any]) -> str:
    return get_str_config(config, "site_host", "www.iwara.tv")


def build_request_headers(config: Dict[str, Any]) -> Dict[str, str]:
    host = site_host(config)
    return {
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "X-Site": host,
        "User-Agent": get_str_config(
            config,
            "request_user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        ),
        "Referer": get_str_config(config, "request_referer", f"https://{host}/"),
        "Origin": get_str_config(config, "request_origin", f"https://{host}"),
    }


# ---- URL / ID ----


def extract_video_id(raw: str) -> Optional[str]:
    text = (raw or "").strip()
    if not text:
        return None
    match = VIDEO_ID_RE.search(text)
    if match:
        return match.group(1)
    token = text.split("?")[0].strip("/").split("/")[-1]
    if re.fullmatch(r"[A-Za-z0-9]{6,}", token):
        return token
    return None


def extract_image_id(raw: str) -> Optional[str]:
    text = (raw or "").strip()
    if not text:
        return None
    match = IMAGE_ID_RE.search(text)
    if match:
        return match.group(1)
    token = text.split("?")[0].strip("/").split("/")[-1]
    if re.fullmatch(r"[A-Za-z0-9]{6,}", token):
        return token
    return None


def normalize_url(url: Optional[str], host: str = "www.iwara.tv") -> str:
    if not url:
        return ""
    cleaned = str(url).strip()
    if not cleaned:
        return ""
    if cleaned.startswith("//"):
        return f"https:{cleaned}"
    if cleaned.startswith("/"):
        return f"https://{host}{cleaned}"
    if re.match(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}(:\d+)?/", cleaned):
        return f"https://{cleaned}"
    if cleaned.startswith(("image/", "thumbnail/", "images/")):
        return f"https://i.iwara.tv/{cleaned}"
    return cleaned


def sanitize_image_url(url: Optional[str]) -> str:
    normalized = normalize_url(url)
    if not normalized:
        return ""
    try:
        parsed = URL(normalized)
    except Exception:
        return ""
    if parsed.scheme not in {"http", "https"}:
        return ""
    if not parsed.host:
        return ""
    return str(parsed)


def compute_x_version(file_id: str, expires: str) -> str:
    payload = f"{file_id}_{expires}_{IWARA_SECRET}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


# ---- text / data extraction ----


def extract_command_payload(message_str: str, command_name: str) -> str:
    text = (message_str or "").strip()
    if not text:
        return ""
    pattern = rf"^[/!#.]?{re.escape(command_name)}\s*"
    return re.sub(pattern, "", text, count=1, flags=re.IGNORECASE).strip()


def parse_search_payload(payload: str):
    text = payload.strip()
    if not text:
        return "all", ""
    parts = text.split(maxsplit=1)
    if parts[0].lower() in {"video", "image", "all"}:
        if len(parts) > 1:
            return parts[0].lower(), parts[1].strip()
        return parts[0].lower(), ""
    return "all", text


def get_text(data: Dict[str, Any], key: str, default: str = "") -> str:
    value = data.get(key)
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def nested_text(data: Any, keys: List[str]) -> str:
    cur = data
    for key in keys:
        if not isinstance(cur, dict):
            return ""
        cur = cur.get(key)
    if cur is None:
        return ""
    return str(cur).strip()


def extract_items(
    data: Any, preferred_keys: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []
    keys_to_try = list(preferred_keys or []) + ["items", "results", "data", "list"]
    for key in keys_to_try:
        value = data.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
        if isinstance(value, dict):
            nested = extract_items(value)
            if nested:
                return nested
    return []


def extract_author(data: Dict[str, Any]) -> str:
    user = data.get("user")
    if isinstance(user, dict):
        return (
            get_text(user, "name")
            or get_text(user, "username")
            or get_text(user, "id")
            or "unknown"
        )
    return get_text(data, "username") or get_text(data, "author") or "unknown"


def extract_tags(data: Dict[str, Any]) -> List[str]:
    raw = data.get("tags")
    if not isinstance(raw, list):
        return []
    tags: List[str] = []
    for item in raw:
        if isinstance(item, str):
            tags.append(item)
        elif isinstance(item, dict):
            name = (
                get_text(item, "id") or get_text(item, "name") or get_text(item, "slug")
            )
            if name:
                tags.append(name)
    return tags


def extract_number(data: Dict[str, Any], keys: List[str]) -> int:
    for key in keys:
        value = data.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return 0


def looks_binary_body_text(text: str) -> bool:
    if not text:
        return False
    sample = text[:200]
    bad = 0
    for ch in sample:
        code = ord(ch)
        if (code < 9) or (13 < code < 32):
            bad += 1
        elif code == 65533:
            bad += 1
    return bad > max(8, len(sample) // 8)


def format_http_error(
    status: int,
    content: str,
    proxy_used: bool = False,
    config: Optional[Dict[str, Any]] = None,
) -> str:
    config = config or {}
    body = (content or "").strip()
    lower = body.lower()
    is_binary = looks_binary_body_text(body)
    is_cf = "just a moment" in lower or "cf-chl" in lower or "cloudflare" in lower
    if status == 403 and (is_cf or is_binary):
        cookie_text = get_str_config(config, "request_cookie", "")
        has_cookie = bool(cookie_text)
        tips = [
            "HTTP 403 (Cloudflare challenge).",
            "请配置可用代理（proxy_url）或填写 request_cookie（含 cf_clearance）。",
            f"当前 Cookie 状态: {'已配置' if has_cookie else '未配置'}。",
            "可同时检查 request_user_agent / request_referer 是否使用默认浏览器值。",
            "也可将 request_engine 设为 auto/cloudscraper（需安装 cloudscraper）。",
            "注意：cf_clearance 通常与获取时的 IP/UA 绑定，换网络/机器后会失效。",
            f"当前代理状态: {'已启用' if proxy_used else '未启用'}。",
        ]
        return " ".join(tips)
    clean = HTML_TAG_RE.sub(" ", body)
    clean = re.sub(r"\s+", " ", clean).strip()
    if len(clean) > 260:
        clean = clean[:260] + "..."
    if not clean:
        clean = "empty response"
    return f"HTTP {status}: {clean}"
