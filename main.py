from __future__ import annotations

from typing import Any, Dict, List, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .iwara_api import IwaraAPI
from .iwara_helpers import (
    extract_command_payload,
    extract_image_id,
    extract_video_id,
    get_int_config,
    parse_search_payload,
    site_host,
    normalize_url,
)
from .iwara_format import (
    format_search_item,
    format_video_detail,
    format_image_detail,
)
from .iwara_image import get_display_image_url
from .iwara_commands import (
    search_items,
    fetch_quality_list,
    resolve_cover_url,
    make_chain,
)
from .iwara_secondary import SecondaryCommands


@register(
    "astrbot_plugin_iwara", "vmoranv", "Iwara 视频/图片查询与直链解析插件", "1.1.0"
)
class IwaraPlugin(SecondaryCommands, Star):
    def __init__(self, context: Context, config: Optional[Dict[str, Any]] = None):
        super().__init__(context)
        self.config = config or {}
        self._api = IwaraAPI(config)

    async def initialize(self):
        logger.info("astrbot_plugin_iwara initialized.")

    async def terminate(self):
        await self._api.close()

    @filter.command("iwara_search")
    async def iwara_search(self, event: AstrMessageEvent):
        """搜索 Iwara 内容。/iwara_search [video|image|all] <关键词>"""
        payload = extract_command_payload(event.message_str, "iwara_search")
        media_type, keyword = parse_search_payload(payload)
        if not keyword:
            yield event.plain_result("用法：/iwara_search [video|image|all] <关键词>")
            return
        limit = get_int_config(self.config, "search_limit", 5, 1, 10)
        try:
            items = await search_items(
                self._api, self.config, keyword, media_type, limit
            )
        except Exception as exc:
            logger.error(f"iwara_search failed: {exc}")
            yield event.plain_result(f"Iwara 搜索失败：{exc}")
            return
        if not items:
            yield event.plain_result(f'没有找到与"{keyword}"相关的内容。')
            return
        host = site_host(self.config)
        for idx, item in enumerate(items, start=1):
            text = format_search_item(
                idx, item, str(item.get("_media_type", media_type)), host
            )
            image_url = await resolve_cover_url(
                self._api, item, str(item.get("_media_type", media_type))
            )
            yield await make_chain(event, self.config, self._api, text, image_url)

    @filter.command("iwara_video")
    async def iwara_video(self, event: AstrMessageEvent):
        """查询视频详情。/iwara_video <视频ID或链接>"""
        video_id = extract_video_id(
            extract_command_payload(event.message_str, "iwara_video")
        )
        if not video_id:
            yield event.plain_result("用法：/iwara_video <视频ID或链接>")
            return
        try:
            data = await self._api.get_json(f"/video/{video_id}")
            yield await make_chain(
                event,
                self.config,
                self._api,
                format_video_detail(data, video_id, site_host(self.config)),
                get_display_image_url(data),
            )
        except Exception as exc:
            logger.error(f"iwara_video failed: {exc}")
            yield event.plain_result(f"查询视频失败：{exc}")

    @filter.command("iwara_image")
    async def iwara_image(self, event: AstrMessageEvent):
        """查询图片详情。/iwara_image <图片ID或链接>"""
        image_id = extract_image_id(
            extract_command_payload(event.message_str, "iwara_image")
        )
        if not image_id:
            yield event.plain_result("用法：/iwara_image <图片ID或链接>")
            return
        try:
            data = await self._api.get_json(f"/image/{image_id}")
            yield await make_chain(
                event,
                self.config,
                self._api,
                format_image_detail(data, image_id, site_host(self.config)),
                get_display_image_url(data),
            )
        except Exception as exc:
            logger.error(f"iwara_image failed: {exc}")
            yield event.plain_result(f"查询图片失败：{exc}")

    @filter.command("iwara_direct")
    async def iwara_direct(self, event: AstrMessageEvent):
        """获取视频直链。/iwara_direct <视频ID或链接>"""
        video_id = extract_video_id(
            extract_command_payload(event.message_str, "iwara_direct")
        )
        if not video_id:
            yield event.plain_result("用法：/iwara_direct <视频ID或链接>")
            return
        try:
            detail = await self._api.get_json(f"/video/{video_id}")
            quality_list = await fetch_quality_list(self._api, self.config, detail)
        except Exception as exc:
            logger.error(f"iwara_direct failed: {exc}")
            yield event.plain_result(f"获取直链失败：{exc}")
            return
        host = site_host(self.config)
        title, final_id = (
            str(detail.get("title", "未知标题")),
            str(detail.get("id", video_id)),
        )
        lines: List[str] = [
            f"《{title}》直链",
            f"ID: {final_id}",
            f"页面: https://{host}/video/{final_id}",
        ]
        for item in quality_list:
            name = str(item.get("name", "unknown"))
            src = item.get("src", {}) if isinstance(item.get("src"), dict) else {}
            if view := normalize_url(src.get("view", "")):
                lines.append(f"[{name}] 播放: {view}")
            if dl := normalize_url(src.get("download", "")):
                lines.append(f"[{name}] 下载: {dl}")
        yield await make_chain(
            event,
            self.config,
            self._api,
            "\n".join(lines),
            get_display_image_url(detail),
        )
