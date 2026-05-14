import asyncio
import datetime
import os
import re
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from PIL import Image, ImageDraw, ImageFont, ImageChops

# 从内部模块导入工具函数
from .iwara_helpers import build_request_headers, get_int_config

# ==================== 文本与字体增强辅助 ====================


def remove_emojis(text: str) -> str:
    """
    清除文本中的 Emoji 和特殊增补字符。

    Pillow 默认的 ImageFont 无法很好地处理 Color Emoji，遇到时可能会抛错或渲染成极丑的黑边残块。
    通过去除 supplementary planes 的字符来保护渲染引擎。

    Args:
        text: 待处理的原始文本。

    Returns:
        处理后的纯文本，移除了 Emoji 并将换行替换为空格。
    """
    if not text:
        return ""
    # 去掉绝大多数 Emoji 所在的 Unicode 增补平面字符
    return re.sub(r"[\U00010000-\U0010ffff]", "", text).replace("\n", " ").strip()


def load_font(size: int) -> ImageFont.ImageFont:
    """
    加载字体，优先使用本地插件字体，失败时智能回退到操作系统内置中文字体。

    Args:
        size: 字体大小。

    Returns:
        ImageFont 对象，如果所有路径都失败则返回 Pillow 默认字体。
    """
    plugin_font = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "fonts", "wqy-microhei.ttc"
    )

    # 字体回退序列（涵盖 Windows, Linux, MacOS 的常用默认中文字体）
    fallback_paths = [
        plugin_font,
        "C:/Windows/Fonts/msyh.ttc",  # Windows 微软雅黑
        "C:/Windows/Fonts/simhei.ttf",  # Windows 黑体
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",  # Linux 文泉驿
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",  # Linux Noto
        "/System/Library/Fonts/PingFang.ttc",  # macOS 苹方
        "/System/Library/Fonts/STHeiti Light.ttc",  # macOS 华文黑体
    ]

    for path in fallback_paths:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue

    print(
        "WARNING (Iwara Plugin): 未找到任何可用的中文字体，UI 渲染将出现豆腐块！请在 fonts 目录下补充 wqy-microhei.ttc"
    )
    return ImageFont.load_default()


def get_text_size(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont
) -> Tuple[int, int]:
    """
    获取文本的真实宽高（适配 Pillow 10+ 的 textbbox，替代已废弃的 textsize）。

    Args:
        draw: PIL ImageDraw 对象。
        text: 要测量长度的字符串。
        font: 使用的字体对象。

    Returns:
        (宽度, 高度) 的元组。
    """
    if not text:
        return 0, 0
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def get_text_width(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont
) -> int:
    """
    获取文本宽度，优先使用高性能的 textlength。

    Args:
        draw: PIL ImageDraw 对象。
        text: 要测量长度的字符串。
        font: 使用的字体对象。

    Returns:
        文本所占的像素宽度。
    """
    if hasattr(draw, "textlength"):
        return int(draw.textlength(text, font=font))
    return get_text_size(draw, text, font)[0]


def wrap_text_with_ellipsis(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
    max_lines: int = 2,
) -> List[str]:
    """
    高效的文本换行与省略号截断算法。

    Args:
        draw: PIL ImageDraw 对象。
        text: 待换行的文本。
        font: 字体对象。
        max_width: 单行最大像素宽度。
        max_lines: 最大允许显示的行数。

    Returns:
        包含每一行字符串的列表。
    """
    if not text:
        return []

    lines = []
    current_line = ""

    for char in text:
        test_line = current_line + char
        if get_text_width(draw, test_line, font) <= max_width:
            current_line = test_line
        else:
            if current_line:
                lines.append(current_line)
            current_line = char
            if len(lines) == max_lines:
                break

    if current_line and len(lines) < max_lines:
        lines.append(current_line)

    # 检查是否溢出，如果原始文本没有被完全消耗完，或者强制到了最后一行且仍然超宽
    consumed_len = sum(len(line) for line in lines)
    if consumed_len < len(text) and len(lines) == max_lines:
        ellipsis_w = get_text_width(draw, "...", font)
        last_line = lines[-1]
        while (
            len(last_line) > 0
            and (get_text_width(draw, last_line, font) + ellipsis_w) > max_width
        ):
            last_line = last_line[:-1]
        lines[-1] = last_line + "..."

    return lines


# ==================== 图像与排版辅助函数 ====================


def crop_center(img: Image.Image, crop_width: int, crop_height: int) -> Image.Image:
    """
    等比例缩放并居中裁剪图片。

    Args:
        img: 原始 Image 对象。
        crop_width: 目标宽度。
        crop_height: 目标高度。

    Returns:
        裁剪并缩放后的新 Image 对象。
    """
    img_width, img_height = img.size
    ratio = max(crop_width / img_width, crop_height / img_height)
    new_w, new_h = int(img_width * ratio), int(img_height * ratio)
    img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    left = (new_w - crop_width) // 2
    top = (new_h - crop_height) // 2
    return img.crop((left, top, left + crop_width, top + crop_height))


def mask_all_corners(img: Image.Image, radius: int) -> Image.Image:
    """
    给图片添加平滑圆角（修复 Alpha 通道污染问题）。

    使用 multiply 替代 darker，确保不会产生奇怪的黑色毛边。

    Args:
        img: 原始 Image 对象。
        radius: 圆角半径。

    Returns:
        带有圆角遮罩的 RGBA Image 对象。
    """
    mask = Image.new("L", img.size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle([(0, 0), img.size], radius=radius, fill=255)

    img = img.convert("RGBA")
    r, g, b, a = img.split()
    new_a = ImageChops.multiply(a, mask)
    img.putalpha(new_a)
    return img


def create_gradient_bg(
    width: int, height: int, color1: Tuple[int, ...], color2: Tuple[int, ...]
) -> Image.Image:
    """
    创建垂直渐变背景。

    Args:
        width: 背景宽度。
        height: 背景高度。
        color1: 顶部颜色 (R, G, B, [A])。
        color2: 底部颜色 (R, G, B, [A])。

    Returns:
        渐变色的 Image 对象。
    """
    base = Image.new("RGBA", (1, height), color1)
    draw = ImageDraw.Draw(base)
    for y in range(height):
        r = int(color1[0] + (color2[0] - color1[0]) * y / height)
        g = int(color1[1] + (color2[1] - color1[1]) * y / height)
        b = int(color1[2] + (color2[2] - color1[2]) * y / height)
        a = (
            int(color1[3] + (color2[3] - color1[3]) * y / height)
            if len(color1) == 4
            else 255
        )
        draw.point((0, y), fill=(r, g, b, a))
    return base.resize((width, height))


def create_background(width: int, height: int, bg_path: str = None) -> Image.Image:
    """
    创建主背景，优先使用本地背景图并添加深色遮罩。

    Args:
        width: 画布宽度。
        height: 画布高度。
        bg_path: 本地背景图片路径。

    Returns:
        作为底层背景的 Image 对象。
    """
    if bg_path and os.path.exists(bg_path):
        try:
            bg_img = Image.open(bg_path).convert("RGBA")
            bg_img = crop_center(bg_img, width, height)
            overlay = Image.new("RGBA", (width, height), (13, 17, 26, 200))
            return Image.alpha_composite(bg_img, overlay)
        except Exception:
            pass
    return create_gradient_bg(width, height, (13, 17, 26, 255), (26, 34, 53, 255))


def format_number(num: int) -> str:
    """
    格式化数字 (例如 128600 -> 12.8万)。

    Args:
        num: 原始整数。

    Returns:
        缩写后的字符串。
    """
    if num >= 10000:
        return f"{num / 10000:.1f}万"
    return str(num)


def get_time_diff_str(iso_str: str) -> str:
    """
    计算发布时间距离现在的直观表达。

    Args:
        iso_str: ISO 格式的时间字符串。

    Returns:
        如“3天前”、“1年前”等描述。
    """
    if not iso_str:
        return "未知时间"
    try:
        dt = datetime.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        now = datetime.datetime.now(datetime.timezone.utc)
        diff = now - dt
        if diff.days > 365:
            return f"{diff.days // 365}年前"
        elif diff.days > 30:
            return f"{diff.days // 30}个月前"
        elif diff.days > 0:
            return f"{diff.days}天前"
        elif diff.seconds > 3600:
            return f"{diff.seconds // 3600}小时前"
        elif diff.seconds > 60:
            return f"{diff.seconds // 60}分钟前"
        else:
            return "刚刚"
    except Exception:
        return iso_str[:10]


# ==================== 手绘图标 ====================


def draw_play_icon(
    draw: ImageDraw.ImageDraw, x: float, y: float, size: float, fill: Tuple[int, ...]
):
    """绘制播放按钮小图标"""
    draw.polygon(
        [
            (x + size * 0.2, y + size * 0.12),
            (x + size * 0.2, y + size * 0.88),
            (x + size * 0.88, y + size * 0.5),
        ],
        fill=fill,
    )


def draw_heart_icon(
    draw: ImageDraw.ImageDraw, x: float, y: float, size: float, fill: Tuple[int, ...]
):
    """绘制爱心小图标"""
    d = size * 0.55
    draw.ellipse((x, y, x + d, y + d), fill=fill)
    draw.ellipse((x + size - d, y, x + size, y + d), fill=fill)
    draw.polygon(
        [
            (x + d * 0.15, y + d * 0.7),
            (x + size - d * 0.15, y + d * 0.7),
            (x + size * 0.5, y + size * 0.95),
        ],
        fill=fill,
    )


# ==================== 网络请求 ====================


async def fetch_image(
    session: aiohttp.ClientSession,
    url: str,
    headers: Dict[str, str],
    proxy: Optional[str] = None,
    timeout: int = 8,
) -> Optional[Image.Image]:
    """
    抓取远程图片并转换为 PIL Image 对象。

    Args:
        session: aiohttp 会话对象。
        url: 图片 URL。
        headers: 请求头，包含 UA 和 Referer 以避免 403。
        proxy: 代理地址。
        timeout: 超时时间。

    Returns:
        Image 对象或 None（抓取失败时）。
    """
    if not url:
        return None
    try:
        async with session.get(
            url, headers=headers, proxy=proxy, timeout=timeout
        ) as resp:
            if resp.status == 200:
                data = await resp.read()
                return Image.open(BytesIO(data)).convert("RGBA")
    except Exception:
        pass
    return None


# ==================== 核心 UI 渲染引擎 ====================


async def render_search_ui(
    items: List[Dict[str, Any]],
    keyword: str,
    config: Dict[str, Any],
    resolve_cover_func: Any,
    extract_avatar_func: Any,
    api: Any,
    bg_path: str = None,
) -> bytes:
    """
    核心 UI 渲染引擎：将搜索结果列表渲染为长图。

    Args:
        items: Iwara 搜索结果列表。
        keyword: 搜索关键词。
        config: 插件配置字典。
        resolve_cover_func: 处理封面 URL 的异步函数。
        extract_avatar_func: 提取头像 URL 的函数。
        api: IwaraAPI 实例。
        bg_path: 背景图片路径。

    Returns:
        JPEG 格式的图片字节流。
    """
    CANVAS_W = 1080
    PAD_EDGE = 40
    PAD_TOP = 140
    GAP = 40
    COL_W = (CANVAS_W - PAD_EDGE * 2 - GAP) // 2

    COVER_H = 300
    CARD_H = 520

    rows = (len(items) + 1) // 2
    canvas_h = PAD_TOP + rows * CARD_H + (rows - 1) * GAP + PAD_EDGE + 40

    canvas = create_background(CANVAS_W, canvas_h, bg_path)
    draw = ImageDraw.Draw(canvas)

    font_title_main = load_font(48)
    font_card_title = load_font(36)
    font_stats = load_font(28)
    font_author = load_font(28)
    font_time = load_font(24)
    font_badge = load_font(24)

    safe_keyword = remove_emojis(keyword)
    if len(safe_keyword) > 15:
        safe_keyword = safe_keyword[:15] + "..."
    draw.text(
        (PAD_EDGE, 50),
        f"搜索结果: {safe_keyword}",
        font=font_title_main,
        fill=(255, 255, 255),
    )

    # --- 修改部分：复用 API Session 并构建请求头 ---
    session = await api._get_session()
    headers = build_request_headers(config)
    proxy = config.get("proxy_url", "") or None
    fetch_timeout = get_int_config(config, "image_fetch_timeout_sec", 8, 3, 30)

    cover_url_tasks = [
        resolve_cover_func(api, item, str(item.get("_media_type", "video")))
        for item in items
    ]
    cover_urls = await asyncio.gather(*cover_url_tasks)
    avatar_urls = [extract_avatar_func(item) for item in items]

    # 将请求头传入 fetch_image
    cover_tasks = [
        fetch_image(session, u, headers, proxy, fetch_timeout) for u in cover_urls
    ]
    avatar_tasks = [
        fetch_image(session, u, headers, proxy, fetch_timeout) for u in avatar_urls
    ]

    fetched_covers = await asyncio.gather(*cover_tasks)
    fetched_avatars = await asyncio.gather(*avatar_tasks)

    for idx, item in enumerate(items):
        col = idx % 2
        row = idx // 2
        x_offset = PAD_EDGE + col * (COL_W + GAP)
        y_offset = PAD_TOP + row * (CARD_H + GAP)

        card = Image.new("RGBA", (COL_W, CARD_H), (27, 36, 61, 0))
        card_draw = ImageDraw.Draw(card)

        cover_layer = Image.new("RGBA", (COL_W, COVER_H), (0, 0, 0, 0))
        cover_draw = ImageDraw.Draw(cover_layer)

        cover_img = fetched_covers[idx]
        if cover_img:
            cover_img = crop_center(cover_img, COL_W, COVER_H)
            cover_layer.paste(cover_img, (0, 0))
        else:
            cover_draw.rectangle([(0, 0), (COL_W, COVER_H)], fill=(40, 50, 70, 255))

        shadow = create_gradient_bg(COL_W, 60, (0, 0, 0, 0), (0, 0, 0, 100))
        cover_layer.paste(shadow, (0, COVER_H - 60), shadow)

        views = format_number(item.get("numViews", 0))
        likes = format_number(item.get("numLikes", 0))

        icon_y = COVER_H - 36
        text_y = COVER_H - 42

        # 播放数
        draw_play_icon(cover_draw, 24, icon_y, 20, fill=(230, 240, 255))
        cover_draw.text((50, text_y), views, font=font_stats, fill=(230, 240, 255))

        # 点赞数
        draw_heart_icon(cover_draw, 144, icon_y + 2, 22, fill=(230, 240, 255))
        cover_draw.text((172, text_y), likes, font=font_stats, fill=(230, 240, 255))

        # 绘制角标
        media_type = item.get("_media_type", "video")
        badge_text = ""
        if media_type == "video":
            duration = item.get("file", {}).get("duration")
            if isinstance(duration, (int, float)) and duration > 0:
                m, s = divmod(int(duration), 60)
                badge_text = f"{m:02d}:{s:02d}"
            else:
                badge_text = "Video"
        else:
            num_images = item.get("numImages", 0)
            badge_text = f"{num_images} P" if num_images > 0 else "Image"

        if badge_text:
            tw, th = get_text_size(cover_draw, badge_text, font_badge)
            pad_x, pad_y = 10, 6
            box_w = tw + pad_x * 2
            box_h = th + pad_y * 2
            box_x = COL_W - 16 - box_w
            box_y = COVER_H - 16 - box_h
            cover_draw.text(
                (box_x + pad_x, box_y + pad_y - 2),
                badge_text,
                font=font_badge,
                fill=(255, 255, 255),
            )

        cover_layer = mask_all_corners(cover_layer, 24)
        card.paste(cover_layer, (0, 0), cover_layer)

        # 绘制标题 (使用安全截断和 Emoji 过滤)
        title = remove_emojis(item.get("title", "无标题"))
        max_text_w = COL_W - 48
        title_lines = wrap_text_with_ellipsis(
            card_draw, title, font_card_title, max_text_w, max_lines=2
        )

        for i, line in enumerate(title_lines):
            card_draw.text(
                (24, COVER_H + 20 + i * 38),
                line,
                font=font_card_title,
                fill=(255, 255, 255),
            )

        # 绘制作者区
        avatar_img = fetched_avatars[idx]
        avatar_size = 64
        avatar_y = CARD_H - 85

        if avatar_img:
            avatar_img = crop_center(avatar_img, avatar_size, avatar_size)
            avatar_img = mask_all_corners(avatar_img, avatar_size // 2)
            card.paste(avatar_img, (24, avatar_y), avatar_img)
        else:
            card_draw.ellipse(
                [24, avatar_y, 24 + avatar_size, avatar_y + avatar_size],
                fill=(100, 110, 130),
            )

        author_name = (
            item.get("user", {}).get("name", "未知")
            if isinstance(item.get("user"), dict)
            else "未知"
        )
        author_name = remove_emojis(author_name)

        # 简单截断作者过长名称（预留空间给右侧）
        if len(author_name) > 8:
            author_name = author_name[:7] + "..."

        card_draw.text(
            (110, avatar_y), author_name, font=font_author, fill=(226, 232, 240)
        )

        time_str = get_time_diff_str(item.get("createdAt", ""))
        card_draw.text(
            (110, avatar_y + 35), time_str, font=font_time, fill=(148, 163, 184)
        )

        # 绘制右下角菜单点点占位
        dot_x = COL_W - 40
        dot_y = avatar_y + 32
        card_draw.ellipse(
            [dot_x, dot_y - 14, dot_x + 6, dot_y - 8], fill=(200, 210, 220)
        )
        card_draw.ellipse(
            [dot_x, dot_y - 2, dot_x + 6, dot_y + 4], fill=(200, 210, 220)
        )
        card_draw.ellipse(
            [dot_x, dot_y + 10, dot_x + 6, dot_y + 16], fill=(200, 210, 220)
        )

        card = mask_all_corners(card, 24)
        canvas.paste(card, (x_offset, y_offset), card)

    out = BytesIO()
    canvas.convert("RGB").save(out, format="JPEG", quality=90)
    return out.getvalue()
