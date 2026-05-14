from __future__ import annotations
import os

from .iwara_ui_render import render_search_ui
import astrbot.api.message_components as Comp

from typing import Any, Dict, List, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .iwara_api import IwaraAPI
from .iwara_helpers import (
    HTML_TAG_RE,
    extract_command_payload,
    extract_image_id,
    extract_items,
    extract_video_id,
    get_int_config,
    get_str_config,
    parse_search_payload,
    proxy_url,
    site_host,
    get_text,
    normalize_url,
)
from .iwara_format import (
    format_search_item,
    format_video_detail,
    format_image_detail,
    format_user_profile,
)
from .iwara_image import (
    get_display_image_url,
    extract_avatar_url,
)
from .iwara_commands import (
    search_items,
    fetch_quality_list,
    resolve_cover_url,
    make_chain,
    cloudscraper_available,
)


class IwaraPlugin(Star):
    def __init__(self, context: Context, config: Optional[Dict[str, Any]] = None):
        super().__init__(context)
        self.config = config or {}
        self._api = IwaraAPI(config)
        self.use_image_ui = config.get("use_image_ui", True)

        self.background_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "backgrounds"
        )
        os.makedirs(self.background_dir, exist_ok=True)

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
        limit = get_int_config(self.config, "search_limit", 6, 1, 10)
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

        if self.use_image_ui:
            yield event.plain_result("正在渲染 UI 界面，请稍候...")
            try:

                bg_path = os.path.join(self.background_dir, "bg.png")

                image_bytes = await render_search_ui(
                    items=items,
                    keyword=keyword,
                    config=self.config,
                    resolve_cover_func=resolve_cover_url,
                    extract_avatar_func=extract_avatar_url,
                    api=self._api,
                    bg_path=bg_path,
                )
                yield event.chain_result([Comp.Image.fromBytes(image_bytes)])
            except Exception as e:
                logger.error(f"渲染 UI 失败: {e}")
                yield event.plain_result(
                    f"UI 渲染失败，请检查环境 (例如是否缺少Pillow库): {e}"
                )

        else:

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

    @filter.command("iwara_diag")
    async def iwara_diag(self, event: AstrMessageEvent):
        """输出当前请求配置诊断信息。"""
        cookie_text = get_str_config(self.config, "request_cookie", "")
        bearer = get_str_config(self.config, "request_bearer_token", "")
        px = proxy_url(self.config)
        yield event.plain_result(
            "\n".join(
                [
                    "Iwara 插件诊断",
                    f"api_base_url: {get_str_config(self.config, 'api_base_url', 'https://api.iwara.tv')}",
                    f"file_api_base_url: {get_str_config(self.config, 'file_api_base_url', 'https://files.iwara.tv')}",
                    f"request_engine: {get_str_config(self.config, 'request_engine', 'auto')}",
                    f"cloudscraper_available: {cloudscraper_available()}",
                    f"image_transport: {get_str_config(self.config, 'image_transport', 'bytes')}",
                    f"proxy_url: {'已配置' if px else '未配置'}",
                    f"warmup_homepage: {get_str_config(self.config, 'warmup_homepage', 'true')}",
                    f"user_agent_len: {len(get_str_config(self.config, 'request_user_agent', ''))}",
                    f"cookie_len: {len(cookie_text)}",
                    f"cookie_has_cf_clearance: {'cf_clearance=' in cookie_text}",
                    f"bearer_token: {'已配置' if bearer else '未配置'}",
                    f"warmup_done: {self._api.warmup_done}",
                ]
            )
        )

    @filter.command("iwara_probe")
    async def iwara_probe(self, event: AstrMessageEvent):
        """探测端点可达性。"""
        from .iwara_probe import run_probe

        try:
            yield event.plain_result(await run_probe(self._api))
        except Exception as exc:
            logger.error(f"iwara_probe failed: {exc}")
            yield event.plain_result(f"Iwara 探测失败：{exc}")

    @filter.command("iwara_related")
    async def iwara_related(self, event: AstrMessageEvent):
        """查询相关视频。/iwara_related <视频ID或链接>"""
        video_id = extract_video_id(
            extract_command_payload(event.message_str, "iwara_related")
        )
        if not video_id:
            yield event.plain_result("用法：/iwara_related <视频ID或链接>")
            return
        try:
            items = extract_items(
                await self._api.get_json(f"/video/{video_id}/related")
            )
            if not items:
                yield event.plain_result("未找到相关视频。")
                return
            limit = get_int_config(self.config, "search_limit", 5, 1, 10)
            host = site_host(self.config)
            for idx, item in enumerate(items[:limit], start=1):
                yield await make_chain(
                    event,
                    self.config,
                    self._api,
                    format_search_item(idx, item, "video", host),
                    get_display_image_url(item),
                )
        except Exception as exc:
            logger.error(f"iwara_related failed: {exc}")
            yield event.plain_result(f"查询相关视频失败：{exc}")

    @filter.command("iwara_comments")
    async def iwara_comments(self, event: AstrMessageEvent):
        """查询视频评论。/iwara_comments <视频ID或链接>"""
        video_id = extract_video_id(
            extract_command_payload(event.message_str, "iwara_comments")
        )
        if not video_id:
            yield event.plain_result("用法：/iwara_comments <视频ID或链接>")
            return
        try:
            from .iwara_helpers import extract_author

            items = extract_items(
                await self._api.get_json(
                    f"/video/{video_id}/comments", params={"page": 0}
                )
            )
            if not items:
                yield event.plain_result("该视频暂无评论。")
                return
            limit = get_int_config(self.config, "search_limit", 5, 1, 10)
            lines = [f"视频 {video_id} 的评论："]
            for idx, item in enumerate(items[:limit], start=1):
                body = (
                    get_text(item, "body")
                    or get_text(item, "content")
                    or get_text(item, "text")
                )
                body = HTML_TAG_RE.sub(" ", body).strip()
                lines.append(
                    f"[{idx}] {extract_author(item)} ({get_text(item, 'createdAt', '-')}): {body[:120]}"
                )
            yield event.plain_result("\n".join(lines))
        except Exception as exc:
            logger.error(f"iwara_comments failed: {exc}")
            yield event.plain_result(f"查询评论失败：{exc}")

    @filter.command("iwara_likes")
    async def iwara_likes(self, event: AstrMessageEvent):
        """查询视频点赞用户。/iwara_likes <视频ID或链接>"""
        video_id = extract_video_id(
            extract_command_payload(event.message_str, "iwara_likes")
        )
        if not video_id:
            yield event.plain_result("用法：/iwara_likes <视频ID或链接>")
            return
        try:
            from .iwara_helpers import extract_author

            display_limit = get_int_config(self.config, "search_limit", 5, 1, 10)
            items = extract_items(
                await self._api.get_json(
                    f"/video/{video_id}/likes", params={"page": 0, "limit": 20}
                )
            )
            if not items:
                yield event.plain_result("该视频暂无点赞。")
                return
            lines = [f"视频 {video_id} 的点赞用户："]
            for idx, item in enumerate(items[:display_limit], start=1):
                lines.append(f"[{idx}] {extract_author(item)}")
            yield event.plain_result("\n".join(lines))
        except Exception as exc:
            logger.error(f"iwara_likes failed: {exc}")
            yield event.plain_result(f"查询点赞失败：{exc}")

    @filter.command("iwara_trending")
    async def iwara_trending(self, event: AstrMessageEvent):
        """查询热门内容。/iwara_trending [video|image|all]"""
        payload = (
            extract_command_payload(event.message_str, "iwara_trending").strip().lower()
        )
        media_type = payload if payload in {"video", "image", "all"} else "video"
        limit = get_int_config(self.config, "search_limit", 5, 1, 10)
        types: List[str] = ["video", "image"] if media_type == "all" else [media_type]
        buckets: Dict[str, List[Dict[str, Any]]] = {}
        errors: List[str] = []
        for t in types:
            try:
                items = extract_items(
                    await self._api.get_json(
                        f"/trending/{t}", params={"rating": "all", "limit": limit}
                    )
                )
                for item in items:
                    item["_media_type"] = t
                buckets[t] = items
            except Exception as exc:
                errors.append(f"{t}={exc}")
        if not any(buckets.values()) and errors:
            yield event.plain_result(f"获取热门内容失败：{'; '.join(errors)}")
            return
        if not any(buckets.values()):
            yield event.plain_result("暂无热门内容。")
            return
        # interleave: video, image, video, image, ...
        interleaved: List[Dict[str, Any]] = []
        max_len = max((len(v) for v in buckets.values()), default=0)
        for i in range(max_len):
            for t in types:
                if t in buckets and i < len(buckets[t]):
                    interleaved.append(buckets[t][i])
        host = site_host(self.config)
        for idx, item in enumerate(interleaved[:limit], start=1):
            t = str(item.get("_media_type", media_type))
            yield await make_chain(
                event,
                self.config,
                self._api,
                format_search_item(idx, item, t, host),
                get_display_image_url(item),
            )

    @filter.command("iwara_user")
    async def iwara_user(self, event: AstrMessageEvent):
        """查询用户信息。/iwara_user <用户名>"""
        query = extract_command_payload(event.message_str, "iwara_user").strip()
        if not query:
            yield event.plain_result("用法：/iwara_user <用户名>")
            return
        try:
            data = await self._api.get_json(f"/profile/{query}")
            yield await make_chain(
                event,
                self.config,
                self._api,
                format_user_profile(data, query, site_host(self.config)),
                extract_avatar_url(data),
            )
        except Exception as exc:
            logger.error(f"iwara_user failed: {exc}")
            yield event.plain_result(f"查询用户失败：{exc}")

    @filter.command("iwara_ui")
    async def iwara_ui_toggle(self, event: AstrMessageEvent):
        """切换 Iwara 搜索的图文 UI 模式。"""
        self.use_image_ui = not self.use_image_ui
        status = (
            "已开启 (图片海报模式)" if self.use_image_ui else "已关闭 (经典图文模式)"
        )
        self.config["use_image_ui"] = self.use_image_ui
        yield event.plain_result(f"Iwara UI 界面 {status}")
