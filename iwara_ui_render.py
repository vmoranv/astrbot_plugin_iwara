import asyncio
import datetime
import textwrap
import os
from io import BytesIO
import aiohttp
from PIL import Image, ImageDraw, ImageFont, ImageChops

# ==================== 图像与排版辅助函数 ====================


def load_font(size: int) -> ImageFont.ImageFont:
    """加载字体，优先使用本地字体文件，失败则回退到默认字体"""

    font_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "fonts", "wqy-microhei.ttc"
    )
    if os.path.exists(font_path):
        try:
            return ImageFont.truetype(font_path, size)
        except Exception:
            pass
    return ImageFont.load_default()


def crop_center(img: Image.Image, crop_width: int, crop_height: int) -> Image.Image:
    """等比例缩放并居中裁剪图片"""
    img_width, img_height = img.size
    ratio = max(crop_width / img_width, crop_height / img_height)
    new_w, new_h = int(img_width * ratio), int(img_height * ratio)
    img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    left = (new_w - crop_width) // 2
    top = (new_h - crop_height) // 2
    return img.crop((left, top, left + crop_width, top + crop_height))


def mask_all_corners(img: Image.Image, radius: int) -> Image.Image:
    """给图片添加平滑圆角（同时保留图片原有的半透明度）"""
    mask = Image.new("L", img.size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle([(0, 0), img.size], radius=radius, fill=255)
    img = img.convert("RGBA")

    r, g, b, a = img.split()
    new_a = ImageChops.darker(a, mask)
    img.putalpha(new_a)
    return img


def create_gradient_bg(
    width: int, height: int, color1: tuple, color2: tuple
) -> Image.Image:
    """创建垂直渐变背景"""
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
    """创建主背景，优先使用本地背景图并添加深色遮罩"""
    if bg_path and os.path.exists(bg_path):
        try:
            bg_img = Image.open(bg_path).convert("RGBA")
            bg_img = crop_center(bg_img, width, height)
            # 添加半透明的深色遮罩，保证文字可读性
            overlay = Image.new(
                "RGBA", (width, height), (13, 17, 26, 200)
            )  # 200为透明度
            return Image.alpha_composite(bg_img, overlay)
        except Exception:
            pass
    return create_gradient_bg(width, height, (13, 17, 26, 255), (26, 34, 53, 255))


def format_number(num: int) -> str:
    """格式化数字 (例如 128600 -> 12.8万)"""
    if num >= 10000:
        return f"{num / 10000:.1f}万"
    return str(num)


def get_time_diff_str(iso_str: str) -> str:
    """计算发布时间距离现在的直观表达"""
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


# ==================== 手绘图标 (完美替代字体符号) ====================


def draw_play_icon(
    draw: ImageDraw.ImageDraw, x: float, y: float, size: float, fill: tuple
):
    """绘制饱满、比例干净的播放图标 (经典圆润比例)"""
    draw.polygon(
        [
            (x + size * 0.2, y + size * 0.12),
            (x + size * 0.2, y + size * 0.88),
            (x + size * 0.88, y + size * 0.5),
        ],
        fill=fill,
    )


def draw_heart_icon(
    draw: ImageDraw.ImageDraw, x: float, y: float, size: float, fill: tuple
):
    """绘制圆润的爱心图标"""
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
    session: aiohttp.ClientSession, url: str, proxy: str = None
) -> Image.Image:
    if not url:
        return None
    try:
        async with session.get(url, proxy=proxy, timeout=8) as resp:
            if resp.status == 200:
                data = await resp.read()
                return Image.open(BytesIO(data)).convert("RGBA")
    except Exception:
        pass
    return None


# ==================== 核心 UI 渲染引擎 ====================


async def render_search_ui(
    items: list,
    keyword: str,
    config: dict,
    resolve_cover_func,
    extract_avatar_func,
    api,
    bg_path: str = None,
) -> bytes:
    CANVAS_W = 1080
    PAD_EDGE = 40
    PAD_TOP = 140
    GAP = 40
    COL_W = (CANVAS_W - PAD_EDGE * 2 - GAP) // 2

    COVER_H = 300  # 270
    # ⭐ 修改一：卡片高度进一步压缩，缩短标题和头像的间距
    CARD_H = 520  # 480

    rows = (len(items) + 1) // 2
    canvas_h = PAD_TOP + rows * CARD_H + (rows - 1) * GAP + PAD_EDGE + 40

    canvas = create_background(CANVAS_W, canvas_h, bg_path)
    draw = ImageDraw.Draw(canvas)

    font_title_main = load_font(48)
    font_card_title = load_font(36)
    font_stats = load_font(28)
    font_author = load_font(28)
    font_time = load_font(24)

    draw.text(
        (PAD_EDGE, 50),
        f"搜索结果: {keyword}",
        font=font_title_main,
        fill=(255, 255, 255),
    )

    proxy = config.get("proxy_url", "") or None
    async with aiohttp.ClientSession() as session:
        cover_url_tasks = [
            resolve_cover_func(api, item, str(item.get("_media_type", "video")))
            for item in items
        ]
        cover_urls = await asyncio.gather(*cover_url_tasks)
        avatar_urls = [extract_avatar_func(item) for item in items]

        cover_tasks = [fetch_image(session, u, proxy) for u in cover_urls]
        avatar_tasks = [fetch_image(session, u, proxy) for u in avatar_urls]

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

        # ⭐ 修改二：精准调节 Y 轴坐标，让图标与文字处于同一水平线中心对齐
        icon_y = COVER_H - 36
        text_y = COVER_H - 42

        # 播放数
        draw_play_icon(cover_draw, 24, icon_y, 20, fill=(230, 240, 255))
        cover_draw.text((50, text_y), views, font=font_stats, fill=(230, 240, 255))

        # 点赞数 (调整 X 轴距离让它更紧凑，微调 Y 轴让爱心视觉上居中)
        draw_heart_icon(cover_draw, 144, icon_y + 2, 22, fill=(230, 240, 255))
        cover_draw.text((172, text_y), likes, font=font_stats, fill=(230, 240, 255))

        # =========================================================
        # 绘制右下角的【播放时长 / 图片角标】
        # =========================================================
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
            font_badge = load_font(24)
            if hasattr(cover_draw, "textlength"):
                tw = int(cover_draw.textlength(badge_text, font=font_badge))
                th = 20
            else:
                tw, th = cover_draw.textsize(badge_text, font=font_badge)[0], 20

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
        # =========================================================

        cover_layer = mask_all_corners(cover_layer, 24)
        card.paste(cover_layer, (0, 0), cover_layer)

        # ==================== 绘制文字区 ====================
        title = item.get("title", "无标题")
        title_lines = []
        temp_line = ""
        max_text_w = COL_W - 48

        for char in title:
            if hasattr(card_draw, "textlength"):
                w = card_draw.textlength(temp_line + char, font=font_card_title)
            else:
                w = card_draw.textsize(temp_line + char, font=font_card_title)[0]

            if w <= max_text_w:
                temp_line += char
            else:
                title_lines.append(temp_line)
                temp_line = char
        if temp_line:
            title_lines.append(temp_line)

        if len(title_lines) > 2:
            title_lines = title_lines[:2]
            while len(title_lines[1]) > 0:
                if hasattr(card_draw, "textlength"):
                    w = card_draw.textlength(
                        title_lines[1] + "...", font=font_card_title
                    )
                else:
                    w = card_draw.textsize(
                        title_lines[1] + "...", font=font_card_title
                    )[0]
                if w <= max_text_w:
                    break
                title_lines[1] = title_lines[1][:-1]
            title_lines[1] += "..."

        # ⭐ 修改三：减小行距，向上微调位置
        for i, line in enumerate(title_lines):
            card_draw.text(
                (24, COVER_H + 20 + i * 38),
                line,
                font=font_card_title,
                fill=(255, 255, 255),
            )

        # ==================== 绘制作者信息区 ====================
        avatar_img = fetched_avatars[idx]
        avatar_size = 64

        # ⭐ 修改四：匹配缩小后的卡片总高度，头像整体上移
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
        if len(author_name) > 8:
            author_name = author_name[:7] + "..."
        card_draw.text(
            (110, avatar_y), author_name, font=font_author, fill=(226, 232, 240)
        )

        time_str = get_time_diff_str(item.get("createdAt", ""))
        card_draw.text(
            (110, avatar_y + 35), time_str, font=font_time, fill=(148, 163, 184)
        )

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
