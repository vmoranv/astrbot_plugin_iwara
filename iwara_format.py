from __future__ import annotations

from typing import Any, Dict

from .iwara_helpers import get_text, extract_author, extract_tags, extract_number


def format_search_item(
    idx: int, item: Dict[str, Any], media_type: str, host: str
) -> str:
    item_id = get_text(item, "id", default="unknown")
    title = get_text(item, "title", default="无标题")
    author = extract_author(item)
    likes = extract_number(item, ["numLikes", "likesCount", "likeCount"])
    views = extract_number(item, ["numViews", "viewsCount", "viewCount"])
    comments = extract_number(item, ["numComments", "commentsCount", "commentCount"])
    page_url = f"https://{host}/{media_type}/{item_id}"
    media_cn = "视频" if media_type == "video" else "图片"
    return (
        f"[{idx}] {media_cn} | {title}\n"
        f"UP: {author}\n"
        f"ID: {item_id}\n"
        f"互动: ❤️ {likes} | 👁 {views} | 💬 {comments}\n"
        f"{page_url}"
    )


def format_video_detail(data: Dict[str, Any], fallback_id: str, host: str) -> str:
    video_id = get_text(data, "id", default=fallback_id)
    title = get_text(data, "title", default="无标题")
    author = extract_author(data)
    likes = extract_number(data, ["numLikes", "likesCount", "likeCount"])
    views = extract_number(data, ["numViews", "viewsCount", "viewCount"])
    comments = extract_number(data, ["numComments", "commentsCount", "commentCount"])
    created = get_text(data, "createdAt", default="-")
    tags = ", ".join(extract_tags(data)[:8]) or "-"
    page_url = f"https://{host}/video/{video_id}"
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


def format_image_detail(data: Dict[str, Any], fallback_id: str, host: str) -> str:
    image_id = get_text(data, "id", default=fallback_id)
    title = get_text(data, "title", default="无标题")
    author = extract_author(data)
    likes = extract_number(data, ["numLikes", "likesCount", "likeCount"])
    views = extract_number(data, ["numViews", "viewsCount", "viewCount"])
    comments = extract_number(data, ["numComments", "commentsCount", "commentCount"])
    created = get_text(data, "createdAt", default="-")
    tags = ", ".join(extract_tags(data)[:8]) or "-"
    page_url = f"https://{host}/image/{image_id}"
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


def format_user_profile(data: Dict[str, Any], fallback_query: str, host: str) -> str:
    from .iwara_helpers import HTML_TAG_RE

    user = data.get("user", data)
    username = get_text(user, "name") or get_text(user, "username") or fallback_query
    user_id = get_text(user, "id", default="-")
    role = get_text(user, "role", default="-")
    status = get_text(user, "status", default="-")
    created = get_text(user, "createdAt", default="-")
    followers = extract_number(user, ["followerCount", "numFollowers"])
    following = extract_number(user, ["followingCount", "numFollowing"])
    likes_received = extract_number(user, ["numLikesReceived", "likesReceived"])
    bio = get_text(data, "body", default="")
    if bio:
        bio = HTML_TAG_RE.sub(" ", bio).strip()
        if len(bio) > 100:
            bio = bio[:100] + "..."
    page_url = f"https://{host}/profile/{username}"
    lines = [
        "用户信息",
        f"用户名: {username}",
        f"ID: {user_id}",
        f"角色: {role}",
        f"状态: {status}",
        f"注册时间: {created}",
        f"粉丝: {followers} | 关注: {following} | 获赞: {likes_received}",
    ]
    if bio:
        lines.append(f"简介: {bio}")
    lines.append(page_url)
    return "\n".join(lines)


def quality_sort_key(item: Dict[str, Any]):
    import re

    name = get_text(item, "name", default="").lower()
    if name == "source":
        return (0, 0)
    match = re.search(r"(\d+)", name)
    if match:
        return (1, -int(match.group(1)))
    if "preview" in name:
        return (3, 0)
    return (2, name)
