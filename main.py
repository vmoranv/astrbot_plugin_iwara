from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from io import BytesIO
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import aiohttp
import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from yarl import URL

try:
    import cloudscraper  # type: ignore
except Exception:
    cloudscraper = None

try:
    from PIL import Image, ImageFilter  # type: ignore
except Exception:
    Image = None
    ImageFilter = None

IWARA_SECRET = "mSvL05GfEmeEmsEYfGCnVpEjYgTJraJN"
VIDEO_ID_RE = re.compile(r"(?:https?://(?:www\.)?iwara\.tv)?/video/([A-Za-z0-9]+)")
IMAGE_ID_RE = re.compile(r"(?:https?://(?:www\.)?iwara\.tv)?/image/([A-Za-z0-9]+)")
HTML_TAG_RE = re.compile(r"<[^>]+>")


@register(
    "astrbot_plugin_iwara",
    "vmoranv",
    "Iwara 视频/图片查询与直链解析插件",
    "1.0.0",
)
class IwaraPlugin(Star):
    def __init__(self, context: Context, config: Optional[Dict[str, Any]] = None):
        super().__init__(context)
        self.config = config or {}
        self._session: Optional[aiohttp.ClientSession] = None
        self._scraper = None
        self._warmup_done = False

    async def initialize(self):
        logger.info("astrbot_plugin_iwara initialized.")

    async def terminate(self):
        await self._close_session()

    @filter.command("iwara_search")
    async def iwara_search(self, event: AstrMessageEvent):
        """
        搜索 Iwara 内容。
        用法：
        /iwara_search <关键词>
        /iwara_search video <关键词>
        /iwara_search image <关键词>
        /iwara_search all <关键词>
        """
        payload = self._extract_command_payload(event.message_str, "iwara_search")
        media_type, keyword = self._parse_search_payload(payload)

        if not keyword:
            yield event.plain_result(
                "用法：/iwara_search [video|image|all] <关键词>"
            )
            return

        limit = self._get_int_config("search_limit", 5, 1, 10)
        try:
            items = await self._search_items(keyword, media_type, limit)
        except Exception as exc:
            logger.error(f"iwara_search failed: {exc}")
            yield event.plain_result(f"Iwara 搜索失败：{exc}")
            return

        if not items:
            yield event.plain_result(f"没有找到与“{keyword}”相关的内容。")
            return

        for idx, item in enumerate(items, start=1):
            item_type = str(item.get("_media_type", media_type))
            text = self._format_search_item(idx, item, item_type)
            image_url = await self._resolve_search_item_cover_url(item, item_type)
            yield await self._make_chain_result(event, text, image_url)

    @filter.command("iwara_video")
    async def iwara_video(self, event: AstrMessageEvent):
        """
        查询视频详情（图+文）。
        用法：/iwara_video <视频ID或链接>
        """
        payload = self._extract_command_payload(event.message_str, "iwara_video")
        video_id = self._extract_video_id(payload)
        if not video_id:
            yield event.plain_result("用法：/iwara_video <视频ID或链接>")
            return

        try:
            data = await self._api_json(f"/video/{video_id}")
            text = self._format_video_detail(data, video_id)
            image_url = self._get_display_image_url(data)
            yield await self._make_chain_result(event, text, image_url)
        except Exception as exc:
            logger.error(f"iwara_video failed: {exc}")
            yield event.plain_result(f"查询视频失败：{exc}")

    @filter.command("iwara_image")
    async def iwara_image(self, event: AstrMessageEvent):
        """
        查询图片详情（图+文）。
        用法：/iwara_image <图片ID或链接>
        """
        payload = self._extract_command_payload(event.message_str, "iwara_image")
        image_id = self._extract_image_id(payload)
        if not image_id:
            yield event.plain_result("用法：/iwara_image <图片ID或链接>")
            return

        try:
            data = await self._api_json(f"/image/{image_id}")
            text = self._format_image_detail(data, image_id)
            image_url = self._get_display_image_url(data)
            yield await self._make_chain_result(event, text, image_url)
        except Exception as exc:
            logger.error(f"iwara_image failed: {exc}")
            yield event.plain_result(f"查询图片失败：{exc}")

    @filter.command("iwara_direct")
    async def iwara_direct(self, event: AstrMessageEvent):
        """
        获取视频直链（附封面图）。
        用法：/iwara_direct <视频ID或链接>
        """
        payload = self._extract_command_payload(event.message_str, "iwara_direct")
        video_id = self._extract_video_id(payload)
        if not video_id:
            yield event.plain_result("用法：/iwara_direct <视频ID或链接>")
            return

        try:
            detail = await self._api_json(f"/video/{video_id}")
            quality_list = await self._fetch_quality_list(detail)
        except Exception as exc:
            logger.error(f"iwara_direct failed: {exc}")
            yield event.plain_result(f"获取直链失败：{exc}")
            return

        title = self._get_text(detail, "title", default="未知标题")
        final_video_id = self._get_text(detail, "id", default=video_id)
        page_url = f"https://{self._site_host()}/video/{final_video_id}"

        lines: List[str] = [
            f"《{title}》直链",
            f"ID: {final_video_id}",
            f"页面: {page_url}",
        ]
        for item in quality_list:
            name = self._get_text(item, "name", default="unknown")
            view_url = self._normalize_url(self._nested_text(item, ["src", "view"]))
            download_url = self._normalize_url(
                self._nested_text(item, ["src", "download"])
            )
            if view_url:
                lines.append(f"[{name}] 播放: {view_url}")
            if download_url:
                lines.append(f"[{name}] 下载: {download_url}")

        image_url = self._get_display_image_url(detail)
        yield await self._make_chain_result(event, "\n".join(lines), image_url)

    @filter.command("iwara_diag")
    async def iwara_diag(self, event: AstrMessageEvent):
        """
        输出当前请求配置诊断信息（不回显完整敏感 Cookie）。
        """
        cookie_text = self._get_str_config("request_cookie", "")
        has_cf = "cf_clearance=" in cookie_text
        bearer = self._get_str_config("request_bearer_token", "")
        proxy = self._proxy_url()
        lines = [
            "Iwara 插件诊断",
            f"api_base_url: {self._get_str_config('api_base_url', 'https://api.iwara.tv')}",
            f"file_api_base_url: {self._get_str_config('file_api_base_url', 'https://files.iwara.tv')}",
            f"request_engine: {self._request_engine()}",
            f"cloudscraper_available: {cloudscraper is not None}",
            f"image_transport: {self._get_str_config('image_transport', 'bytes')}",
            f"proxy_url: {'已配置' if proxy else '未配置'}",
            f"warmup_homepage: {self._get_bool_config('warmup_homepage', True)}",
            f"user_agent_len: {len(self._get_str_config('request_user_agent', ''))}",
            f"referer: {self._get_str_config('request_referer', '')}",
            f"origin: {self._get_str_config('request_origin', '')}",
            f"cookie_len: {len(cookie_text)}",
            f"cookie_has_cf_clearance: {has_cf}",
            f"bearer_token: {'已配置' if bool(bearer) else '未配置'}",
            f"warmup_done: {self._warmup_done}",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("iwara_probe")
    async def iwara_probe(self, event: AstrMessageEvent):
        """
        探测 www/api/files 三个端点在当前配置下的可达性与状态码。
        用法：/iwara_probe
        """
        try:
            text = await self._run_probe()
            yield event.plain_result(text)
        except Exception as exc:
            logger.error(f"iwara_probe failed: {exc}")
            yield event.plain_result(f"Iwara 探测失败：{exc}")

    async def _search_items(
        self, keyword: str, media_type: str, limit: int
    ) -> List[Dict[str, Any]]:
        keyword = keyword.strip()
        if not keyword:
            return []

        if media_type not in {"video", "image", "all"}:
            media_type = "all"

        if media_type == "all":
            video_limit = max(1, (limit + 1) // 2)
            image_limit = max(1, limit - video_limit)
            video_exc: Optional[Exception] = None
            image_exc: Optional[Exception] = None
            videos: List[Dict[str, Any]] = []
            images: List[Dict[str, Any]] = []
            try:
                videos = await self._search_by_type(keyword, "video", video_limit)
            except Exception as exc:
                video_exc = exc
                logger.warning(f"all-search video branch failed: {exc}")
            try:
                images = await self._search_by_type(keyword, "image", image_limit)
            except Exception as exc:
                image_exc = exc
                logger.warning(f"all-search image branch failed: {exc}")

            if not videos and not images and (video_exc or image_exc):
                raise RuntimeError(
                    "all 搜索失败："
                    + "; ".join(
                        x
                        for x in [
                            f"video={video_exc}" if video_exc else "",
                            f"image={image_exc}" if image_exc else "",
                        ]
                        if x
                    )
                )
            for item in videos:
                item["_media_type"] = "video"
            for item in images:
                item["_media_type"] = "image"
            return (videos + images)[:limit]

        items = await self._search_by_type(keyword, media_type, limit)
        for item in items:
            item["_media_type"] = media_type
        return items

    async def _search_by_type(
        self, keyword: str, media_type: str, limit: int
    ) -> List[Dict[str, Any]]:
        # First try server-side search, then fallback to listing + local filter.
        try:
            data = await self._api_json(
                "/search", params={"query": keyword, "type": media_type}
            )
            items = self._extract_items(data, [f"{media_type}s", "results", "items"])
            if items:
                return items[:limit]
        except Exception as exc:
            logger.warning(f"Iwara /search fallback ({media_type}): {exc}")

        endpoint = "/videos" if media_type == "video" else "/images"
        listing = await self._api_json(
            endpoint,
            params={"sort": "date", "rating": "all", "limit": max(limit * 3, 24)},
        )
        items = self._extract_items(listing, [f"{media_type}s", "results", "items"])
        if not items:
            return []

        lowered = keyword.lower()
        filtered: List[Dict[str, Any]] = []
        for item in items:
            title = self._get_text(item, "title", default="")
            tags_text = " ".join(self._extract_tags(item))
            if lowered in title.lower() or lowered in tags_text.lower():
                filtered.append(item)
        return filtered[:limit]

    async def _fetch_quality_list(self, detail: Dict[str, Any]) -> List[Dict[str, Any]]:
        file_url = self._get_text(detail, "fileUrl", default="")
        if not file_url:
            raise RuntimeError("视频详情中没有 fileUrl，无法计算直链。")

        parsed = urlparse(file_url)
        query = parse_qs(parsed.query)
        file_id = parsed.path.rstrip("/").split("/")[-1]
        expires = query.get("expires", [None])[0]
        hash_value = query.get("hash", [None])[0]
        if not file_id or not expires or not hash_value:
            raise RuntimeError("fileUrl 缺少 fileId/expires/hash 参数。")

        x_version = self._compute_x_version(file_id, str(expires))
        data = await self._api_json(
            f"/file/{file_id}",
            params={"expires": expires, "hash": hash_value},
            headers={"X-Version": x_version},
            use_file_api=True,
        )
        items = self._extract_items(data)
        items.sort(key=self._quality_sort_key)
        return items

    def _quality_sort_key(self, item: Dict[str, Any]):
        name = self._get_text(item, "name", default="").lower()
        if name == "source":
            return (0, 0)
        match = re.search(r"(\d+)", name)
        if match:
            return (1, -int(match.group(1)))
        if "preview" in name:
            return (3, 0)
        return (2, name)

    async def _api_json(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        use_file_api: bool = False,
    ) -> Any:
        bases = self._candidate_api_bases(use_file_api)
        last_exc: Optional[Exception] = None
        for idx, base in enumerate(bases):
            url = f"{base.rstrip('/')}/{path.lstrip('/')}"
            try:
                return await self._api_json_once(
                    url=url,
                    params=params,
                    headers=headers,
                )
            except Exception as exc:
                last_exc = exc
                if idx < len(bases) - 1 and self._is_retryable_api_error(exc):
                    logger.warning(f"request failed on {base}, try next base: {exc}")
                    continue
                raise

        raise RuntimeError(f"请求失败：{last_exc}")

    async def _api_json_once(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        merged_headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "X-Site": self._site_host(),
            "User-Agent": self._get_str_config(
                "request_user_agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            ),
            "Referer": self._get_str_config(
                "request_referer", f"https://{self._site_host()}/"
            ),
            "Origin": self._get_str_config(
                "request_origin", f"https://{self._site_host()}"
            ),
        }
        cookie = self._get_str_config("request_cookie", "")
        if cookie:
            merged_headers["Cookie"] = cookie
        bearer = self._get_str_config("request_bearer_token", "")
        if bearer and "Authorization" not in merged_headers:
            merged_headers["Authorization"] = (
                bearer if bearer.lower().startswith("bearer ") else f"Bearer {bearer}"
            )
        if headers:
            merged_headers.update(headers)

        session = await self._get_session()
        await self._ensure_warmup(session, merged_headers)
        proxy = self._proxy_url()
        engine = self._request_engine()
        if engine == "cloudscraper":
            return await self._api_json_via_cloudscraper(
                url, params, merged_headers, proxy
            )

        async with session.get(
            url,
            params=params,
            headers=merged_headers,
            proxy=proxy,
        ) as resp:
            content = await resp.text()
            if resp.status >= 400:
                if self._should_try_cloudscraper_fallback(resp.status, content):
                    logger.warning(
                        "aiohttp got Cloudflare challenge, retry via cloudscraper."
                    )
                    return await self._api_json_via_cloudscraper(
                        url, params, merged_headers, proxy
                    )
                raise RuntimeError(
                    self._format_http_error(resp.status, content, proxy_used=bool(proxy))
                )
            if not content.strip():
                return {}
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                raise RuntimeError("响应不是合法 JSON。")

    def _candidate_api_bases(self, use_file_api: bool) -> List[str]:
        if use_file_api:
            primary = self._get_str_config("file_api_base_url", "https://files.iwara.tv")
            candidates = [primary, "https://files.iwara.tv", "https://filesq.iwara.tv"]
        else:
            primary = self._get_str_config("api_base_url", "https://api.iwara.tv")
            candidates = [primary, "https://api.iwara.tv", "https://apiq.iwara.tv"]
        ordered: List[str] = []
        for item in candidates:
            val = (item or "").strip().rstrip("/")
            if val and val not in ordered:
                ordered.append(val)
        return ordered

    def _is_retryable_api_error(self, exc: Exception) -> bool:
        text = str(exc).lower()
        if "cloudflare challenge" in text:
            return True
        for code in ["http 403", "http 429", "http 500", "http 502", "http 503", "http 504"]:
            if code in text:
                return True
        return False

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout_sec = self._get_int_config("request_timeout_sec", 15, 5, 60)
            timeout = aiohttp.ClientTimeout(total=timeout_sec)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                trust_env=True,
                cookie_jar=aiohttp.CookieJar(unsafe=True),
            )
            self._apply_manual_cookies(self._session)
            self._warmup_done = False
        return self._session

    def _request_engine(self) -> str:
        engine = self._get_str_config("request_engine", "auto").lower()
        if engine not in {"auto", "aiohttp", "cloudscraper"}:
            return "auto"
        return engine

    def _should_try_cloudscraper_fallback(self, status: int, content: str) -> bool:
        if self._request_engine() != "auto":
            return False
        if cloudscraper is None:
            return False
        if status != 403:
            return False
        return self._is_cf_challenge_response(content)

    def _is_cf_challenge_response(self, content: str) -> bool:
        lower = (content or "").lower()
        return (
            "just a moment" in lower
            or "cf-chl" in lower
            or "cloudflare" in lower
            or "cf-mitigated" in lower
        )

    def _get_scraper(self):
        if cloudscraper is None:
            raise RuntimeError(
                "cloudscraper 未安装，无法使用 cloudscraper 引擎。"
                "请安装：pip install cloudscraper"
            )
        if self._scraper is None:
            self._scraper = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows", "mobile": False}
            )
        return self._scraper

    async def _api_json_via_cloudscraper(
        self,
        url: str,
        params: Optional[Dict[str, Any]],
        headers: Dict[str, str],
        proxy: Optional[str],
    ) -> Any:
        timeout_sec = self._get_int_config("request_timeout_sec", 15, 5, 60)
        proxy_map = {"http": proxy, "https": proxy} if proxy else None
        req_headers = dict(headers)
        req_headers["Accept-Encoding"] = "gzip, deflate"

        def _do_request():
            scraper = self._get_scraper()
            response = scraper.get(
                url,
                params=params,
                headers=req_headers,
                proxies=proxy_map,
                timeout=timeout_sec,
            )
            return response.status_code, response.text

        status, content = await asyncio.to_thread(_do_request)
        if status >= 400:
            raise RuntimeError(
                self._format_http_error(status, content, proxy_used=bool(proxy))
            )
        if not content.strip():
            return {}
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            raise RuntimeError("响应不是合法 JSON。")

    async def _run_probe(self) -> str:
        proxy = self._proxy_url()
        engine = self._request_engine()
        api_base = self._get_str_config("api_base_url", "https://api.iwara.tv").rstrip("/")
        file_base = self._get_str_config(
            "file_api_base_url", "https://files.iwara.tv"
        ).rstrip("/")
        www_url = self._get_str_config("request_referer", "https://www.iwara.tv/")
        if not www_url:
            www_url = "https://www.iwara.tv/"

        targets = [
            ("www", www_url),
            ("api", f"{api_base}/videos?limit=1&sort=date&rating=all"),
            ("files", f"{file_base}/"),
        ]

        lines = [
            "Iwara 连通性探测",
            f"request_engine: {engine}",
            f"proxy_url: {'已配置' if proxy else '未配置'}",
            f"cloudscraper_available: {cloudscraper is not None}",
            "",
        ]
        for label, url in targets:
            result = await self._probe_single_url(url)
            cf = "yes" if result["is_cf"] else "no"
            lines.append(
                f"[{label}] {result['status']} | engine={result['engine']} | cf={cf} | {result['cost_ms']}ms"
            )
            lines.append(f"url: {url}")
            if result["preview"]:
                lines.append(f"preview: {result['preview']}")
            if result["error"]:
                lines.append(f"error: {result['error']}")
            lines.append("")

        if not proxy:
            lines.append("提示: 当前未启用代理，若持续 403 建议配置可用 HTTP 代理。")
        lines.append("提示: cf_clearance 通常绑定 IP+UA；跨机器/网络复制常失效。")
        return "\n".join(lines)

    async def _probe_single_url(self, url: str) -> Dict[str, Any]:
        engine = self._request_engine()
        if engine == "cloudscraper":
            return await self._probe_via_cloudscraper(url)
        if engine == "aiohttp":
            return await self._probe_via_aiohttp(url)

        # auto: aiohttp first, then cloudscraper if CF challenge.
        first = await self._probe_via_aiohttp(url)
        if (
            first["status"] == 403
            and first["is_cf"]
            and cloudscraper is not None
        ):
            second = await self._probe_via_cloudscraper(url)
            if second["status"] != 403:
                return second
            second["engine"] = "aiohttp->cloudscraper"
            return second
        return first

    async def _probe_via_aiohttp(self, url: str) -> Dict[str, Any]:
        start = time.perf_counter()
        session = await self._get_session()
        proxy = self._proxy_url()
        headers = self._build_probe_headers()
        timeout_sec = self._get_int_config("request_timeout_sec", 15, 5, 60)
        timeout = aiohttp.ClientTimeout(total=timeout_sec)
        try:
            async with session.get(
                url, headers=headers, proxy=proxy, timeout=timeout, allow_redirects=True
            ) as resp:
                raw = await resp.read()
                text = self._decode_preview(raw)
                return {
                    "status": resp.status,
                    "engine": "aiohttp",
                    "is_cf": self._is_cf_challenge_response(text)
                    or (resp.status == 403 and self._looks_binary_body_text(text)),
                    "preview": self._safe_preview(text),
                    "error": "",
                    "cost_ms": int((time.perf_counter() - start) * 1000),
                }
        except Exception as exc:
            return {
                "status": "ERR",
                "engine": "aiohttp",
                "is_cf": False,
                "preview": "",
                "error": str(exc),
                "cost_ms": int((time.perf_counter() - start) * 1000),
            }

    async def _probe_via_cloudscraper(self, url: str) -> Dict[str, Any]:
        start = time.perf_counter()
        proxy = self._proxy_url()
        timeout_sec = self._get_int_config("request_timeout_sec", 15, 5, 60)
        proxy_map = {"http": proxy, "https": proxy} if proxy else None
        headers = self._build_probe_headers()

        def _do_request():
            scraper = self._get_scraper()
            response = scraper.get(
                url,
                headers=headers,
                proxies=proxy_map,
                timeout=timeout_sec,
            )
            return response.status_code, response.text

        try:
            status, text = await asyncio.to_thread(_do_request)
            return {
                "status": status,
                "engine": "cloudscraper",
                "is_cf": self._is_cf_challenge_response(text)
                or (status == 403 and self._looks_binary_body_text(text)),
                "preview": self._safe_preview(text),
                "error": "",
                "cost_ms": int((time.perf_counter() - start) * 1000),
            }
        except Exception as exc:
            return {
                "status": "ERR",
                "engine": "cloudscraper",
                "is_cf": False,
                "preview": "",
                "error": str(exc),
                "cost_ms": int((time.perf_counter() - start) * 1000),
            }

    def _build_probe_headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "X-Site": self._site_host(),
            "User-Agent": self._get_str_config(
                "request_user_agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            ),
            "Referer": self._get_str_config(
                "request_referer", f"https://{self._site_host()}/"
            ),
            "Origin": self._get_str_config(
                "request_origin", f"https://{self._site_host()}"
            ),
            "Cookie": self._get_str_config("request_cookie", ""),
        }

    def _decode_preview(self, raw: bytes) -> str:
        if not raw:
            return ""
        try:
            return raw.decode("utf-8", errors="replace")
        except Exception:
            try:
                return raw.decode("latin-1", errors="replace")
            except Exception:
                return ""

    def _safe_preview(self, text: str) -> str:
        if not text:
            return ""
        if self._looks_binary_body_text(text):
            return "<binary body>"
        clean = HTML_TAG_RE.sub(" ", text)
        clean = re.sub(r"\s+", " ", clean).strip()
        if len(clean) > 160:
            clean = clean[:160] + "..."
        return clean

    async def _close_session(self):
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        if self._scraper is not None:
            try:
                self._scraper.close()
            except Exception:
                pass
        self._scraper = None
        self._warmup_done = False

    async def _make_chain_result(
        self, event: AstrMessageEvent, text: str, image_url: Optional[str]
    ):
        if not image_url:
            return event.plain_result(text)
        image_url = self._sanitize_image_url(image_url)
        if not image_url:
            return event.plain_result(text)

        image_transport = self._get_str_config("image_transport", "bytes").lower()
        if image_transport not in {"bytes", "url"}:
            image_transport = "bytes"

        try:
            if image_transport == "bytes":
                image_comp = await self._build_image_component_from_bytes(image_url)
            else:
                image_comp = Comp.Image.fromURL(image_url)
        except Exception as exc:
            logger.warning(f"skip image component for chain: {image_url} ({exc})")
            return event.plain_result(f"{text}\n封面: {image_url}")
        chain = [
            image_comp,
            Comp.Plain(text),
        ]
        return event.chain_result(chain)

    async def _build_image_component_from_bytes(self, image_url: str):
        session = await self._get_session()
        proxy = self._proxy_url()
        timeout_sec = self._get_int_config("image_fetch_timeout_sec", 8, 3, 30)
        headers = {
            "User-Agent": self._get_str_config(
                "request_user_agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            ),
            "Referer": self._get_str_config(
                "request_referer", f"https://{self._site_host()}/"
            ),
            "Origin": self._get_str_config(
                "request_origin", f"https://{self._site_host()}"
            ),
        }
        timeout = aiohttp.ClientTimeout(total=timeout_sec)
        last_exc: Optional[Exception] = None
        for attempt_url in self._image_fetch_candidates(image_url):
            try:
                async with session.get(
                    attempt_url, headers=headers, proxy=proxy, timeout=timeout
                ) as resp:
                    if resp.status >= 400:
                        body = await resp.text()
                        raise RuntimeError(
                            self._format_http_error(
                                resp.status, body, proxy_used=bool(proxy)
                            )
                        )
                    data = await resp.read()
                    if not data:
                        raise RuntimeError("图片响应为空")
                    max_bytes = self._get_int_config(
                        "image_max_kb", 2048, 256, 10240
                    ) * 1024
                    if len(data) > max_bytes:
                        raise RuntimeError(
                            f"图片过大({len(data)} bytes > {max_bytes} bytes)"
                        )
                    data = self._apply_image_censor_bytes(data)
                    if len(data) > max_bytes:
                        raise RuntimeError(
                            f"打码后图片过大({len(data)} bytes > {max_bytes} bytes)"
                        )
                    return Comp.Image.fromBytes(data)
            except Exception as exc:
                last_exc = exc
                continue
        raise RuntimeError(str(last_exc or "图片下载失败"))

    def _image_fetch_candidates(self, image_url: str) -> List[str]:
        """Return a short ordered candidate list for image fetch fallback."""
        normalized = self._sanitize_image_url(image_url)
        if not normalized:
            return [image_url]
        candidates: List[str] = [normalized]
        # i.iwara may expose either /image/thumbnail/... or /image/original/... per object.
        if "/image/thumbnail/" in normalized:
            candidates.append(normalized.replace("/image/thumbnail/", "/image/original/", 1))
        elif "/image/original/" in normalized:
            candidates.append(normalized.replace("/image/original/", "/image/thumbnail/", 1))
        # Some objects serve webp instead of jpg.
        if normalized.lower().endswith(".jpg"):
            candidates.append(normalized[:-4] + ".webp")
        elif normalized.lower().endswith(".webp"):
            candidates.append(normalized[:-5] + ".jpg")
        uniq: List[str] = []
        seen = set()
        for u in candidates:
            if u not in seen:
                uniq.append(u)
                seen.add(u)
        return uniq

    def _format_search_item(
        self, idx: int, item: Dict[str, Any], media_type: str
    ) -> str:
        item_id = self._get_text(item, "id", default="unknown")
        title = self._get_text(item, "title", default="无标题")
        author = self._extract_author(item)
        likes = self._extract_number(item, ["numLikes", "likesCount", "likeCount"])
        views = self._extract_number(item, ["numViews", "viewsCount", "viewCount"])
        comments = self._extract_number(
            item, ["numComments", "commentsCount", "commentCount"]
        )
        page_url = f"https://{self._site_host()}/{media_type}/{item_id}"
        media_cn = "视频" if media_type == "video" else "图片"
        return (
            f"[{idx}] {media_cn} | {title}\n"
            f"UP: {author}\n"
            f"ID: {item_id}\n"
            f"互动: ❤️ {likes} | 👁 {views} | 💬 {comments}\n"
            f"{page_url}"
        )

    def _format_video_detail(self, data: Dict[str, Any], fallback_id: str) -> str:
        video_id = self._get_text(data, "id", default=fallback_id)
        title = self._get_text(data, "title", default="无标题")
        author = self._extract_author(data)
        likes = self._extract_number(data, ["numLikes", "likesCount", "likeCount"])
        views = self._extract_number(data, ["numViews", "viewsCount", "viewCount"])
        comments = self._extract_number(
            data, ["numComments", "commentsCount", "commentCount"]
        )
        created = self._get_text(data, "createdAt", default="-")
        tags = ", ".join(self._extract_tags(data)[:8]) or "-"
        page_url = f"https://{self._site_host()}/video/{video_id}"
        return (
            f"视频详情\n"
            f"标题: {title}\n"
            f"UP: {author}\n"
            f"ID: {video_id}\n"
            f"发布时间: {created}\n"
            f"互动: ❤️ {likes} | 👁 {views} | 💬 {comments}\n"
            f"标签: {tags}\n"
            f"{page_url}"
        )

    def _format_image_detail(self, data: Dict[str, Any], fallback_id: str) -> str:
        image_id = self._get_text(data, "id", default=fallback_id)
        title = self._get_text(data, "title", default="无标题")
        author = self._extract_author(data)
        likes = self._extract_number(data, ["numLikes", "likesCount", "likeCount"])
        views = self._extract_number(data, ["numViews", "viewsCount", "viewCount"])
        comments = self._extract_number(
            data, ["numComments", "commentsCount", "commentCount"]
        )
        created = self._get_text(data, "createdAt", default="-")
        tags = ", ".join(self._extract_tags(data)[:8]) or "-"
        page_url = f"https://{self._site_host()}/image/{image_id}"
        return (
            f"图片详情\n"
            f"标题: {title}\n"
            f"作者: {author}\n"
            f"ID: {image_id}\n"
            f"发布时间: {created}\n"
            f"互动: ❤️ {likes} | 👁 {views} | 💬 {comments}\n"
            f"标签: {tags}\n"
            f"{page_url}"
        )

    def _extract_command_payload(self, message_str: str, command_name: str) -> str:
        text = (message_str or "").strip()
        if not text:
            return ""
        pattern = rf"^[/!#.]?{re.escape(command_name)}\s*"
        return re.sub(pattern, "", text, count=1, flags=re.IGNORECASE).strip()

    def _parse_search_payload(self, payload: str):
        text = payload.strip()
        if not text:
            return "all", ""
        parts = text.split(maxsplit=1)
        if parts[0].lower() in {"video", "image", "all"}:
            if len(parts) > 1:
                return parts[0].lower(), parts[1].strip()
            return parts[0].lower(), ""
        return "all", text

    def _extract_video_id(self, raw: str) -> Optional[str]:
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

    def _extract_image_id(self, raw: str) -> Optional[str]:
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

    def _compute_x_version(self, file_id: str, expires: str) -> str:
        payload = f"{file_id}_{expires}_{IWARA_SECRET}"
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    def _get_display_image_url(self, data: Dict[str, Any]) -> Optional[str]:
        cover_url = self._extract_cover_url(data)
        if not cover_url:
            # Prefer file-based thumbnail path (more accurate than content id path).
            file_id = self._extract_media_file_id(data)
            thumb_idx = self._extract_thumbnail_index(data)
            if file_id:
                cover_url = self._build_file_thumbnail_url(
                    file_id=file_id, thumbnail_index=thumb_idx, kind="thumbnail"
                )
        if not cover_url:
            # Last resort fallback for items without file metadata.
            content_id = self._get_text(data, "id", default="")
            if content_id:
                cover_url = (
                    f"https://i.iwara.tv/image/thumbnail/{content_id}/thumbnail-00.jpg"
                )
        if not cover_url:
            return None
        cover_url = self._sanitize_image_url(cover_url)
        if not cover_url:
            return None
        return self._apply_image_censor(cover_url)

    async def _resolve_search_item_cover_url(
        self, item: Dict[str, Any], media_type: str
    ) -> Optional[str]:
        """
        Resolve cover for search items.
        If only content-id fallback URL is available, fetch detail once and rebuild using file.id.
        """
        image_url = self._get_display_image_url(item)
        item_id = self._get_text(item, "id", default="")
        if not image_url or not item_id:
            return image_url
        if not self._is_content_id_thumbnail_url(image_url, item_id):
            return image_url

        endpoint = "/video" if media_type == "video" else "/image"
        try:
            detail = await self._api_json(f"{endpoint}/{item_id}")
            detail_url = self._get_display_image_url(detail)
            if detail_url:
                return detail_url
        except Exception as exc:
            logger.debug(f"cover detail lookup failed for {media_type}/{item_id}: {exc}")
        return image_url

    def _is_content_id_thumbnail_url(self, url: str, content_id: str) -> bool:
        normalized = self._sanitize_image_url(url)
        if not normalized or not content_id:
            return False
        try:
            path = URL(normalized).path
        except Exception:
            return False
        pattern = rf"^/image/(?:thumbnail|original)/{re.escape(content_id)}/thumbnail-\d{{2}}\.(?:jpg|jpeg|webp|png)$"
        return re.fullmatch(pattern, path, flags=re.IGNORECASE) is not None

    def _extract_media_file_id(self, data: Any) -> str:
        if not isinstance(data, dict):
            return ""
        direct_paths = [
            ["file", "id"],
            ["asset", "id"],
            ["video", "file", "id"],
            ["image", "file", "id"],
        ]
        for path in direct_paths:
            value = self._nested_text(data, path)
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

    def _extract_thumbnail_index(self, data: Any) -> int:
        if not isinstance(data, dict):
            return 0
        raw = data.get("thumbnail")
        if raw is None:
            raw = data.get("thumbnailIndex")
        try:
            idx = int(str(raw))
        except Exception:
            idx = 0
        if idx < 0 or idx > 99:
            return 0
        return idx

    def _build_file_thumbnail_url(
        self, file_id: str, thumbnail_index: int = 0, kind: str = "thumbnail"
    ) -> str:
        idx = thumbnail_index if 0 <= thumbnail_index <= 99 else 0
        segment = "thumbnail" if kind == "thumbnail" else "original"
        return f"https://i.iwara.tv/image/{segment}/{file_id}/thumbnail-{idx:02d}.jpg"

    def _extract_cover_url(self, data: Any) -> Optional[str]:
        # Prefer explicit thumbnail/cover fields before recursive scan.
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
            value = self._nested_text(data, path)
            normalized = self._sanitize_image_url(value)
            if normalized and self._looks_like_image_url(normalized):
                return normalized
        return self._recursive_find_image_url(data, depth=0)

    def _recursive_find_image_url(self, value: Any, depth: int) -> Optional[str]:
        if depth > 5:
            return None
        if isinstance(value, str):
            normalized = self._sanitize_image_url(value)
            if normalized and self._looks_like_image_url(normalized):
                return normalized
            return None
        if isinstance(value, list):
            for item in value:
                candidate = self._recursive_find_image_url(item, depth + 1)
                if candidate:
                    return candidate
            return None
        if isinstance(value, dict):
            preferred_keys = [
                "thumbnail",
                "cover",
                "preview",
                "image",
                "img",
                "avatar",
                "url",
                "src",
            ]
            for key in preferred_keys:
                if key in value:
                    candidate = self._recursive_find_image_url(value[key], depth + 1)
                    if candidate:
                        return candidate
            for sub in value.values():
                candidate = self._recursive_find_image_url(sub, depth + 1)
                if candidate:
                    return candidate
        return None

    def _looks_like_image_url(self, url: str) -> bool:
        lower = url.lower().strip()
        # Reject MIME-like strings such as "image/png".
        if re.fullmatch(r"[a-z]+/[a-z0-9.+-]+", lower):
            return False
        try:
            parsed = URL(lower)
        except Exception:
            return False
        if parsed.scheme not in {"http", "https"} or not parsed.host:
            return False
        path = parsed.path.lower()
        # Reject pseudo-paths like "/image/png" or "/image/webp".
        if re.fullmatch(r"/image/(?:png|jpe?g|webp|gif)", path):
            return False
        if any(path.endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif"]):
            return True
        if any(marker in path for marker in ("thumbnail", "cover", "preview", "avatar", "image")):
            # For non-extension URLs, require at least one id-like segment.
            return re.search(r"/[a-z]+/[a-z0-9_-]{6,}", path) is not None
        return False

    def _apply_image_censor(self, url: str) -> str:
        cleaned = self._sanitize_image_url(url)
        return cleaned or url

    def _get_image_censor_level(self) -> str:
        level = self._get_str_config("image_censor_level", "off").lower()
        if level not in {"off", "low", "medium", "high"}:
            return "off"
        return level

    def _apply_image_censor_bytes(self, data: bytes) -> bytes:
        level = self._get_image_censor_level()
        if level == "off":
            return data
        if Image is None or ImageFilter is None:
            logger.warning("Pillow unavailable; image_censor_level fallback to off.")
            return data

        radius_map = {"low": 4, "medium": 8, "high": 14}
        radius = radius_map.get(level, 0)
        if radius <= 0:
            return data

        try:
            with Image.open(BytesIO(data)) as img:
                blurred = img.filter(ImageFilter.GaussianBlur(radius=radius))
                if blurred.mode not in ("RGB", "L"):
                    blurred = blurred.convert("RGB")
                out = BytesIO()
                blurred.save(out, format="JPEG", quality=86, optimize=True)
                return out.getvalue()
        except Exception as exc:
            logger.warning(f"image censor failed, fallback raw bytes: {exc}")
            return data

    def _extract_items(
        self, data: Any, preferred_keys: Optional[List[str]] = None
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
                nested = self._extract_items(value)
                if nested:
                    return nested
        return []

    def _extract_author(self, data: Dict[str, Any]) -> str:
        user = data.get("user")
        if isinstance(user, dict):
            return (
                self._get_text(user, "name")
                or self._get_text(user, "username")
                or self._get_text(user, "id")
                or "unknown"
            )
        return (
            self._get_text(data, "username")
            or self._get_text(data, "author")
            or "unknown"
        )

    def _extract_tags(self, data: Dict[str, Any]) -> List[str]:
        raw = data.get("tags")
        if not isinstance(raw, list):
            return []
        tags: List[str] = []
        for item in raw:
            if isinstance(item, str):
                tags.append(item)
            elif isinstance(item, dict):
                name = (
                    self._get_text(item, "id")
                    or self._get_text(item, "name")
                    or self._get_text(item, "slug")
                )
                if name:
                    tags.append(name)
        return tags

    def _extract_number(self, data: Dict[str, Any], keys: List[str]) -> int:
        for key in keys:
            value = data.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                return int(value)
            if isinstance(value, str) and value.isdigit():
                return int(value)
        return 0

    def _nested_text(self, data: Any, keys: List[str]) -> str:
        cur = data
        for key in keys:
            if not isinstance(cur, dict):
                return ""
            cur = cur.get(key)
        if cur is None:
            return ""
        return str(cur).strip()

    def _get_text(self, data: Dict[str, Any], key: str, default: str = "") -> str:
        value = data.get(key)
        if value is None:
            return default
        text = str(value).strip()
        return text if text else default

    def _normalize_url(self, url: Optional[str]) -> str:
        if not url:
            return ""
        cleaned = str(url).strip()
        if not cleaned:
            return ""
        if cleaned.startswith("//"):
            return f"https:{cleaned}"
        if cleaned.startswith("/"):
            return f"https://{self._site_host()}{cleaned}"
        if re.match(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}(:\d+)?/", cleaned):
            return f"https://{cleaned}"
        if cleaned.startswith(("image/", "thumbnail/", "images/")):
            return f"https://i.iwara.tv/{cleaned}"
        return cleaned

    def _sanitize_image_url(self, url: Optional[str]) -> str:
        normalized = self._normalize_url(url)
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

    def _get_int_config(
        self, key: str, default: int, min_value: int, max_value: int
    ) -> int:
        value = self.config.get(key, default)
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = default
        return max(min_value, min(max_value, number))

    def _get_str_config(self, key: str, default: str) -> str:
        value = self.config.get(key, default)
        if value is None:
            return default
        text = str(value).strip()
        return text if text else default

    def _proxy_url(self) -> Optional[str]:
        proxy = self._get_str_config("proxy_url", "")
        return proxy or None

    def _site_host(self) -> str:
        return self._get_str_config("site_host", "www.iwara.tv")

    async def _ensure_warmup(
        self, session: aiohttp.ClientSession, headers: Dict[str, str]
    ):
        if self._warmup_done:
            return
        if not self._get_bool_config("warmup_homepage", True):
            self._warmup_done = True
            return

        warmup_url = self._get_str_config("request_referer", "https://www.iwara.tv/")
        if not warmup_url:
            warmup_url = "https://www.iwara.tv/"
        proxy = self._proxy_url()
        try:
            async with session.get(
                warmup_url,
                headers=headers,
                proxy=proxy,
                allow_redirects=True,
            ) as resp:
                # Consume body to ensure cookies in response are processed.
                await resp.text()
                logger.info(f"iwara warmup status: {resp.status}")
        except Exception as exc:
            logger.warning(f"iwara warmup failed: {exc}")
        finally:
            self._warmup_done = True

    def _apply_manual_cookies(self, session: aiohttp.ClientSession):
        cookie_text = self._get_str_config("request_cookie", "")
        if not cookie_text:
            return

        cookies: Dict[str, str] = {}
        for chunk in cookie_text.split(";"):
            part = chunk.strip()
            if not part or "=" not in part:
                continue
            key, value = part.split("=", 1)
            key = key.strip()
            value = value.strip()
            if key:
                cookies[key] = value
        if not cookies:
            return

        targets = [
            "https://www.iwara.tv/",
            "https://api.iwara.tv/",
            "https://files.iwara.tv/",
            "https://apiq.iwara.tv/",
            "https://filesq.iwara.tv/",
        ]
        for target in targets:
            session.cookie_jar.update_cookies(cookies, response_url=URL(target))

    def _get_bool_config(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            text = value.strip().lower()
            if text in {"1", "true", "yes", "on"}:
                return True
            if text in {"0", "false", "no", "off"}:
                return False
        return bool(value)

    def _format_http_error(
        self, status: int, content: str, proxy_used: bool = False
    ) -> str:
        body = (content or "").strip()
        lower = body.lower()
        is_binary = self._looks_binary_body_text(body)
        is_cf = "just a moment" in lower or "cf-chl" in lower or "cloudflare" in lower
        if status == 403 and (is_cf or is_binary):
            cookie_text = self._get_str_config("request_cookie", "")
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

        # Avoid returning full HTML page to chat; keep it concise.
        clean = HTML_TAG_RE.sub(" ", body)
        clean = re.sub(r"\s+", " ", clean).strip()
        if len(clean) > 260:
            clean = clean[:260] + "..."
        if not clean:
            clean = "empty response"
        return f"HTTP {status}: {clean}"

    def _looks_binary_body_text(self, text: str) -> bool:
        if not text:
            return False
        sample = text[:200]
        bad = 0
        for ch in sample:
            code = ord(ch)
            if (code < 9) or (13 < code < 32):
                bad += 1
            elif code == 65533:  # replacement char
                bad += 1
        return bad > max(8, len(sample) // 8)
