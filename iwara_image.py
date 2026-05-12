from __future__ import annotations

import asyncio
from io import BytesIO
from typing import Any, Dict, List, Optional

from yarl import URL

try:
    from PIL import Image, ImageFilter  # type: ignore
except Exception:
    Image = None
    ImageFilter = None

from .iwara_helpers import (
    get_text,
    nested_text,
    sanitize_image_url,
    get_str_config,
)


def get_display_image_url(data: Dict[str, Any]) -> Optional[str]:
    cover_url = extract_cover_url(data)
    if not cover_url:
        file_id = extract_media_file_id(data)
        thumb_idx = extract_thumbnail_index(data)
        if file_id:
            cover_url = build_file_thumbnail_url(file_id, thumb_idx, "thumbnail")
    if not cover_url:
        # For image items, thumbnail is an object {id, name}
        thumbnail = data.get("thumbnail")
        if isinstance(thumbnail, dict):
            tid = str(thumbnail.get("id", "")).strip()
            tname = str(thumbnail.get("name", "")).strip()
            if tid and tname:
                cover_url = f"https://i.iwara.tv/image/thumbnail/{tid}/{tname}"
    if not cover_url:
        content_id = get_text(data, "id", default="")
        if content_id:
            cover_url = (
                f"https://i.iwara.tv/image/thumbnail/{content_id}/thumbnail-00.jpg"
            )
    if not cover_url:
        return None
    cover_url = sanitize_image_url(cover_url)
    return cover_url or None


def extract_cover_url(data: Any) -> Optional[str]:
    priority_paths = [
        ["thumbnailUrl"],
        ["previewUrl"],
        ["coverUrl"],
        ["imageUrl"],
        ["thumbnail", "url"],
        ["thumbnail", "src"],
        ["thumbnail", "large"],
        ["thumbnail", "small"],
        ["cover", "url"],
        ["file", "thumbnailUrl"],
    ]
    for path in priority_paths:
        value = nested_text(data, path)
        normalized = sanitize_image_url(value)
        if normalized and looks_like_image_url(normalized):
            return normalized
    return _recursive_find_image_url(data, depth=0)


def _recursive_find_image_url(value: Any, depth: int) -> Optional[str]:
    if depth > 5:
        return None
    if isinstance(value, str):
        normalized = sanitize_image_url(value)
        if normalized and looks_like_image_url(normalized):
            return normalized
        return None
    if isinstance(value, list):
        for item in value:
            candidate = _recursive_find_image_url(item, depth + 1)
            if candidate:
                return candidate
        return None
    if isinstance(value, dict):
        for key in [
            "thumbnail",
            "cover",
            "preview",
            "image",
            "img",
            "avatar",
            "url",
            "src",
        ]:
            if key in value:
                candidate = _recursive_find_image_url(value[key], depth + 1)
                if candidate:
                    return candidate
        for sub in value.values():
            candidate = _recursive_find_image_url(sub, depth + 1)
            if candidate:
                return candidate
    return None


def looks_like_image_url(url: str) -> bool:
    import re

    lower = url.lower().strip()
    if re.fullmatch(r"[a-z]+/[a-z0-9.+-]+", lower):
        return False
    try:
        parsed = URL(lower)
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.host:
        return False
    path = parsed.path.lower()
    if re.fullmatch(r"/image/(?:png|jpe?g|webp|gif)", path):
        return False
    if any(path.endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif"]):
        return True
    if any(m in path for m in ("thumbnail", "cover", "preview", "avatar", "image")):
        return re.search(r"/[a-z]+/[a-z0-9_-]{6,}", path) is not None
    return False


def extract_media_file_id(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    for path in [
        ["file", "id"],
        ["asset", "id"],
        ["video", "file", "id"],
        ["image", "file", "id"],
    ]:
        value = nested_text(data, path)
        if value:
            return value
    for key in ("fileId", "assetId"):
        value = data.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    files = data.get("files")
    if isinstance(files, list):
        for item in files:
            if isinstance(item, dict):
                value = item.get("id")
                if value is not None and str(value).strip():
                    return str(value).strip()
    return ""


def extract_thumbnail_index(data: Any) -> int:
    if not isinstance(data, dict):
        return 0
    raw = data.get("thumbnail")
    if raw is None:
        raw = data.get("thumbnailIndex")
    try:
        idx = int(str(raw))
    except Exception:
        idx = 0
    return max(0, min(99, idx))


def build_file_thumbnail_url(
    file_id: str, thumbnail_index: int = 0, kind: str = "thumbnail"
) -> str:
    idx = max(0, min(99, thumbnail_index))
    segment = "thumbnail" if kind == "thumbnail" else "original"
    return f"https://i.iwara.tv/image/{segment}/{file_id}/thumbnail-{idx:02d}.jpg"


def is_content_id_thumbnail_url(url: str, content_id: str) -> bool:
    import re

    normalized = sanitize_image_url(url)
    if not normalized or not content_id:
        return False
    try:
        path = URL(normalized).path
    except Exception:
        return False
    pattern = rf"^/image/(?:thumbnail|original)/{re.escape(content_id)}/thumbnail-\d{{2}}\.(?:jpg|jpeg|webp|png)$"
    return re.fullmatch(pattern, path, flags=re.IGNORECASE) is not None


def image_fetch_candidates(image_url: str) -> List[str]:
    normalized = sanitize_image_url(image_url)
    if not normalized:
        return [image_url]
    candidates: List[str] = [normalized]
    if "/image/thumbnail/" in normalized:
        candidates.append(
            normalized.replace("/image/thumbnail/", "/image/original/", 1)
        )
    elif "/image/original/" in normalized:
        candidates.append(
            normalized.replace("/image/original/", "/image/thumbnail/", 1)
        )
    if normalized.lower().endswith(".jpg"):
        candidates.append(normalized[:-4] + ".webp")
    elif normalized.lower().endswith(".webp"):
        candidates.append(normalized[:-5] + ".jpg")
    seen = set()
    return [u for u in candidates if u not in seen and not seen.add(u)]  # type: ignore[func-returns-value]


def extract_avatar_url(data: Dict[str, Any]) -> Optional[str]:
    user = data.get("user", data)
    avatar = user.get("avatar") if isinstance(user, dict) else None
    if isinstance(avatar, dict):
        avatar_id = get_text(avatar, "id", default="")
        avatar_name = get_text(avatar, "name", default="")
        if avatar_id and avatar_name:
            return f"https://i.iwara.tv/image/original/{avatar_id}/{avatar_name}"
    for path in [
        ["avatarUrl"],
        ["user", "avatarUrl"],
        ["avatar", "url"],
        ["user", "avatar", "url"],
    ]:
        value = nested_text(data, path)
        normalized = sanitize_image_url(value)
        if normalized and looks_like_image_url(normalized):
            return normalized
    return None


# ---- async image censor (CPU-bound → thread) ----


async def apply_image_censor_bytes(data: bytes, config: Dict[str, Any]) -> bytes:
    level = get_str_config(config, "image_censor_level", "off").lower()
    if level not in {"low", "medium", "high"}:
        return data
    if Image is None or ImageFilter is None:
        return data
    radius_map = {"low": 4, "medium": 8, "high": 14}
    radius = radius_map.get(level, 0)
    if radius <= 0:
        return data
    return await asyncio.to_thread(_pil_blur, data, radius)


def _pil_blur(data: bytes, radius: int) -> bytes:
    try:
        with Image.open(BytesIO(data)) as img:
            blurred = img.filter(ImageFilter.GaussianBlur(radius=radius))
            if blurred.mode not in ("RGB", "L"):
                blurred = blurred.convert("RGB")
            out = BytesIO()
            blurred.save(out, format="JPEG", quality=86, optimize=True)
            return out.getvalue()
    except Exception:
        return data
