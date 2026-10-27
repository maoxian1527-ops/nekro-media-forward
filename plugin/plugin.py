"""Public-video forwarding for OneBot V11 / NapCat.

抖音的 web 接口现在被 Argus 风控保护：任何纯 HTTP 客户端（yt-dlp / curl）即使带上
浏览器 cookie，也会拿到 403 `Uifid Not Found` 或空响应；而且这套风控还会按 IP 信誉
拦截机房 IP。可用的做法只有一个——让真实浏览器打开视频页，由页面自己签名请求，再把
拿到的 CDN 直链交给服务器下载。

因此本插件把抖音拆成两步：

1. **解析**：交给跑在住宅网络里的本地解析器（``media-resolver``，使用无头 Edge +
   CDP）。它通过本插件的 ``/resolver/*`` 接口领取任务并回传直链——本机在 NAT 后面，
   所以由它主动轮询，服务器不需要能连回本地。
2. **下载与转发**：服务器（腾讯云）直接抓 CDN 直链完全没有问题，下载后仍旧通过短时
   链接交给本机 NapCat 以合并转发发出。

小红书继续走 yt-dlp。

X (Twitter) 和 B 站一样不需要本地解析器：读 X 嵌入组件用的公开 syndication 接口，
无需登录、无需 API token，拿到正文 / 图片 / 视频直链后云端直接下载并以合并转发发出。

Instagram 和 Facebook 同理：不登录、不带 cookie / token，默认用诚实的非浏览器 UA 匿名读公开帖子页里的
服务端渲染数据（非官方接口，随时可能失效），解析出文字 + 图片组 / 视频后云端下载并合并转发。
可通过 instagram_proxy / facebook_proxy 走代理（做法同 x_proxy，留空 = 直连）。
"""

from __future__ import annotations

import asyncio
import html
import math
import mimetypes
import os
import re
import hashlib
import json
import secrets
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urljoin, urlparse, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from nonebot import get_bot
from pydantic import BaseModel, ConfigDict, Field

from nekro_agent.api.message import ChatMessage
from nekro_agent.api.plugin import ConfigBase, NekroPlugin, SandboxMethodType
from nekro_agent.api.schemas import AgentCtx
from nekro_agent.api.signal import MsgSignal


plugin = NekroPlugin(
    name="短视频合并转发",
    module_name="video_forward",
    description="收到公开的抖音 / 小红书 / TikTok / B站 / X / Instagram / Facebook 链接后，将视频或图文以 QQ 合并转发发回原聊天。抖音与小红书由本地解析器取直链，B站走公开 API，X 读公开嵌入数据接口（含帖子文字），Instagram / Facebook 匿名读公开帖子页（不登录，非官方接口）。",
    version="0.11.0",
    author="local_video",
    url="https://github.com/KroMiose/nekro-agent",
    support_adapter=["onebot_v11"],
    allow_sleep=False,
)


@plugin.mount_config()
class VideoForwardConfig(ConfigBase):
    """公开短视频下载与合并转发设置。受保护、付费、仅好友可见或需要登录的视频不会被绕过。"""

    public_base_url: str = Field(
        default="http://127.0.0.1:8022",
        title="NekroAgent 公网地址",
        description="NapCat 用来取得视频临时链接的地址（填你自己的服务器，不要带 /webui 后缀）。",
    )
    max_file_size_mb: int = Field(
        default=60,
        ge=5,
        le=200,
        title="单个视频最大体积（MB）",
        description="超过此大小不下载，避免占满云盘或让 QQ 上传失败。",
    )
    expiry_minutes: int = Field(
        default=30,
        ge=5,
        le=120,
        title="临时视频链接有效期（分钟）",
        description="QQ 上传完成后即可失效；默认 30 分钟。",
    )
    cooldown_seconds: int = Field(
        default=15,
        ge=0,
        le=120,
        title="同一聊天冷却时间（秒）",
        description="防止同一聊天连续发链接导致重复下载。",
    )
    yt_dlp_path: str = Field(
        default="/usr/local/bin/yt-dlp",
        title="yt-dlp 命令位置",
        description="保持默认即可；服务器找不到 yt-dlp 时会给出明确提示。",
    )
    resolver_enabled: bool = Field(
        default=True,
        title="启用本地解析器（抖音 / 小红书 / TikTok；B站不需要）",
        description=(
            "抖音和小红书的 web 接口有风控且会拦截机房 IP，必须由住宅网络里的本地解析器"
            "（media-resolver）用真实浏览器取直链；TikTok 在大陆被墙、云端完全不可达，"
            "同样由本机解析器走代理下载后直接把文件路径交给 NapCat。"
            "关闭后这三个平台会回退到 yt-dlp（基本一定失败）。"
        ),
    )
    resolver_token: str = Field(
        default="",
        title="本地解析器通信密钥",
        description=(
            "本地解析器与服务器之间的共享密钥，必须与 media-resolver 的 config.json 一致；"
            "留空表示禁用解析器接口。"
        ),
    )
    resolver_timeout: int = Field(
        default=150,
        ge=30,
        le=600,
        title="等待本地解析的超时时间（秒）",
        description="本地解析器要启动无头浏览器并等待页面返回详情，通常 10~40 秒，网络慢时请调大。",
    )
    resolver_stale_seconds: int = Field(
        default=60,
        ge=15,
        le=600,
        title="解析器离线判定（秒）",
        description="超过这么久没有收到本地解析器的心跳，就认为它没在运行。",
    )
    send_timeout: int = Field(
        default=300,
        ge=30,
        le=1800,
        title="合并转发超时（秒）",
        description=(
            "NapCat 要把视频上传到 QQ 服务器才返回，家用宽带上行较慢时需要放宽；"
            "默认 300 秒。"
        ),
    )
    max_images: int = Field(
        default=9,
        ge=1,
        le=20,
        title="图文笔记最多发送张数",
        description=(
            "小红书/抖音的图文笔记（没有视频流）会按顺序下载并合并转发其图片；"
            "超过这个数量只发前几张，避免刷屏。默认 9 张。"
        ),
    )
    allow_resolver_local_path: bool = Field(
        default=False,
        title="允许直接用解析器的本机路径",
        description=(
            "NapCat 与 media-resolver 在同一台机器上时打开（例如 Windows 上同时跑 QQ/NapCat 和解析器）："
            "TikTok 这类由解析器下载好的文件直接把本机路径交给 NapCat，服务器不中转、更快。"
            "NapCat 上云后必须关闭（云端读不到本机路径），改由解析器上传文件。"
        ),
    )
    forward_nickname: str = Field(
        default="机器人",
        title="合并转发显示的昵称",
        description="合并转发卡片里显示的发送者昵称，默认「机器人」。",
    )
    bilibili_max_size_mb: int = Field(
        default=300,
        ge=10,
        le=1500,
        title="B站视频体积上限（MB）",
        description=(
            "B 站视频普遍比短视频大，单独给一个上限（默认 300MB）。"
            "超过上限不会发送，并会提示视频的实际大小。"
        ),
    )
    bilibili_cookie: str = Field(
        default="",
        title="B站 Cookie（可选，用于登录态）",
        description=(
            "留空 = 匿名访问（B 站只给到 720p，会员/登录可见视频会失败）。"
            "填浏览器里 bilibili.com 的 SESSDATA 值即可（也可以粘整行 Cookie）。"
            "注意：这相当于把账号登录态交给服务器上的插件，请自行评估风险，别用主账号。"
        ),
    )
    x_enabled: bool = Field(
        default=True,
        title="启用 X (Twitter) 帖子解析",
        description=(
            "收到 x.com / twitter.com（含 fxtwitter / vxtwitter）的帖子链接时，解析正文、图片和视频并合并转发。"
            "走 X 嵌入组件的公开数据接口，不需要登录或 API token，也不需要本地解析器；"
            "只能读公开的单条帖子，受保护 / 已删除 / 需登录的帖子会提示无法解析。关闭后 X 链接会被忽略。"
        ),
    )
    x_max_size_mb: int = Field(
        default=100,
        ge=5,
        le=500,
        title="X 视频体积上限（MB）",
        description=(
            "X 视频会挑不超过此上限的最高码率 mp4；全部档位都超过时不发送，并提示实际大小。默认 100MB。"
        ),
    )
    x_send_text: bool = Field(
        default=True,
        title="X 帖子同时发送文字",
        description=(
            "合并转发里在图片 / 视频前面附上作者、北京时间、帖子正文和被引用帖子的正文；"
            "纯文字帖子也只会发这段文字。关闭后只发图片 / 视频，纯文字帖子会提示没有可发送的内容。"
        ),
    )
    x_proxy: str = Field(
        default="",
        title="X / Twitter 代理（可选）",
        description=(
            "X 在大陆机房无法直连，需要走代理才能读取帖子与下载媒体。"
            "填形如 http://172.18.0.1:7891 的地址（注意：容器里的 127.0.0.1 是容器自己，要用宿主机网关 IP）。"
            "留空 = 直连（海外服务器或本机有全局代理时可留空）。"
        ),
    )
    x_show_stats: bool = Field(
        default=True,
        title="X 帖子文字里显示点赞 / 回复数",
        description="只在「X 帖子同时发送文字」打开时有效。",
    )
    instagram_enabled: bool = Field(
        default=True,
        title="启用 Instagram 帖子解析",
        description=(
            "收到 instagram.com 的 /p/ /reel/ /reels/ /tv/ 链接时，解析文字、图片和视频并合并转发。"
            "不登录、不带 cookie / token，用非浏览器 UA 匿名读公开帖子页（非官方接口，随时可能失效）；"
            "只能读公开帖子，私密 / 已删除 / Stories 会提示无法解析。关闭后 Instagram 链接会被忽略。"
        ),
    )
    instagram_max_size_mb: int = Field(
        default=100,
        ge=5,
        le=500,
        title="Instagram 视频体积上限（MB）",
        description="超过上限的视频不发送，并提示实际大小。默认 100MB。",
    )
    instagram_fallback_ua: bool = Field(
        default=True,
        title="Instagram 拿不到数据时改用 Googlebot UA 重试（灰色做法，默认开启，可关闭）",
        description=(
            "总是先用诚实的非浏览器 UA 请求；只有它拿不到数据（Instagram 偶尔只返回空壳）时，才会再用 Googlebot 的 UA 请求一次。"
            "这是冒充搜索引擎爬虫的灰色做法，可能违反 Meta 的服务条款，也可能随时被封。默认开启；不想用就设为 false 关闭。"
        ),
    )
    facebook_enabled: bool = Field(
        default=True,
        title="启用 Facebook 帖子解析",
        description=(
            "收到 facebook.com / fb.com 的公开帖子、视频、Reel、照片链接时，解析文字、图片和视频并合并转发。"
            "不登录、不带 cookie / token，用非浏览器 UA 匿名读公开帖子页（非官方接口，随时可能失效）；"
            "私密 / 群组 / 需要登录的内容会提示无法解析，fb.watch 短链暂不支持。关闭后 Facebook 链接会被忽略。"
        ),
    )
    facebook_max_size_mb: int = Field(
        default=100,
        ge=5,
        le=500,
        title="Facebook 视频体积上限（MB）",
        description="超过上限的视频不发送，并提示实际大小。默认 100MB。",
    )
    facebook_send_comments: bool = Field(
        default=False,
        title="Facebook 帖子文字里附上前几条评论",
        description="打开后在帖子文字末尾附上页面里能匿名读到的前 5 条评论（作者 + 内容）。默认关闭。",
    )
    instagram_proxy: str = Field(
        default="",
        title="Instagram 代理（可选）",
        description=(
            "Instagram 的帖子页请求、媒体下载（以及 yt-dlp 回退）走这个代理。"
            "填形如 http://172.18.0.1:7891 的地址（容器里的 127.0.0.1 是容器自己，要用宿主机网关 IP）。"
            "留空 = 直连。大陆机房访问 Instagram 通常需要配置（未经测试）。"
        ),
    )
    facebook_proxy: str = Field(
        default="",
        title="Facebook 代理（可选）",
        description=(
            "Facebook 的帖子页请求、媒体下载（以及 yt-dlp 回退）走这个代理。"
            "填形如 http://172.18.0.1:7891 的地址（容器里的 127.0.0.1 是容器自己，要用宿主机网关 IP）。"
            "留空 = 直连。大陆机房访问 Facebook 通常需要配置（未经测试）。"
        ),
    )
    bilibili_ffmpeg_path: str = Field(
        default="",
        title="B站 ffmpeg 路径（可选）",
        description=(
            "合并 DASH 音视频需要 ffmpeg（1080p / 4K 必须走这条路）。"
            "留空时自动查找 /home/ubuntu/srv/nekro_agent/bin/ffmpeg，再找 PATH 里的 ffmpeg；"
            "找不到就自动退回 720p 单文件 mp4。"
        ),
    )


_SUPPORTED_DOMAINS = (
    "douyin.com",
    "iesdouyin.com",
    "xiaohongshu.com",
    "xhslink.com",
    "xhslink.cn",
    "tiktok.com",
    "bilibili.com",
    "b23.tv",
)
# 需要本地解析器处理的平台
# 抖音 / 小红书：风控 + 机房 IP 拦截，必须住宅 IP + 真实浏览器取直链
# TikTok：大陆直连被 DNS 污染，云端完全不可达，只能在本机走代理下载
_RESOLVER_DOMAINS = (
    "douyin.com",
    "iesdouyin.com",
    "xiaohongshu.com",
    "xhslink.com",
    "xhslink.cn",
    "tiktok.com",
)
# X (Twitter) 及其常见镜像域名：走公开 syndication 接口，不需要本地解析器
_X_DOMAINS = (
    "x.com",
    "twitter.com",
    "fxtwitter.com",
    "vxtwitter.com",
    "fixupx.com",
    "fixvx.com",
)
_DOWNLOAD_REFERER = {
    "douyin": "https://www.douyin.com/",
    "xiaohongshu": "https://www.xiaohongshu.com/",
    "tiktok": "https://www.tiktok.com/",
    "bilibili": "https://www.bilibili.com/",
    "x": "https://x.com/",
    "instagram": "https://www.instagram.com/",
    "facebook": "https://www.facebook.com/",
}
# QQ 分享文案有时会把短链和标题拆成不同消息段，甚至省去 https://。
# 因此只找受支持站点，而不是依赖整段消息恰好是一个标准 URL。
_URL_RE = re.compile(
    r"(?:(?:https?://)?(?:[a-z0-9-]+\.)*(?:douyin\.com|iesdouyin\.com|"
    r"xiaohongshu\.com|xhslink\.(?:com|cn)|tiktok\.com|bilibili\.com|b23\.tv)"
    r"(?:/[^\s<>'\"，。！？、]*)?"
    # X：只认带 /status/<id> 的帖子链接（x.com 首页、个人主页不处理）；
    # 前面不能紧挨字母数字，否则 box.com 之类会被当成 x.com
    r"|(?<![A-Za-z0-9_.@/-])(?:https?://)?(?:[a-z0-9-]+\.)*"
    r"(?:x|twitter|fxtwitter|vxtwitter|fixupx|fixvx)\.com/"
    r"(?:i/(?:web/)?|[A-Za-z0-9_]{1,50}/)status(?:es)?/\d+[^\s<>'\"，。！？、]*"
    # Instagram / Facebook：这里只粗匹配「域名 + 路径」，是不是帖子链接由 _instagram_ref / _facebook_ref 决定
    # （首页、个人主页、l.instagram.com 跳转链接等都会在那里被丢掉）；同样不能紧挨字母数字
    r"|(?<![A-Za-z0-9_.@/-])(?:https?://)?(?:[a-z0-9-]+\.)*"
    r"(?:instagram\.com|facebook\.com|fb\.com|fb\.watch)/[^\s<>'\"，。！？、]*)",
    re.IGNORECASE,
)
_VIDEO_DIR = Path("/tmp/nekro-video-forward")
_LAST_CHAT_REQUEST: dict[str, float] = {}
_RECENT_URLS: dict[tuple[str, str], float] = {}
_IN_FLIGHT: set[tuple[str, str]] = set()

_DOWNLOAD_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)


_PLATFORM_NAMES = {
    "douyin": "抖音",
    "xiaohongshu": "小红书",
    "tiktok": "TikTok",
    "bilibili": "B站",
    "x": "X",
    "instagram": "Instagram",
    "facebook": "Facebook",
}


@dataclass
class _MediaSource:
    """一次待发送的媒体。

    要么 ``path`` 指向服务器上的文件（经临时链接交给 NapCat 下载），
    要么 ``local_path`` 是 NapCat 所在机器上的文件路径（TikTok 就是这种，
    因为大陆/云端都访问不到 TikTok CDN）。
    """

    name: str
    path: Optional[Path] = None
    local_path: Optional[str] = None
    size: int = 0
    # 图文笔记（小红书/抖音图集）：图片直链，由插件逐张下载后合成一条合并转发。
    # 非空时 path / local_path 均为空。
    images: tuple = ()
    # 放在媒体前面的文字节点（X 帖子正文等）；为空则用默认的「平台 + 视频/图文 + 链接」。
    caption: str = ""
    # 签名 / 缩放参数导致主图链接失败时的备用链接（Instagram / Facebook）：{主链接: 备用链接}
    image_fallbacks: Optional[Dict[str, str]] = None
    # 同一条帖子里的其它视频（Instagram 轮播 / Facebook 相册）：((文件路径, 文件名), ...)，主视频仍是 path
    extra_videos: tuple = ()


@dataclass
class _Lease:
    path: Path
    expires_at: float
    download_name: str


_LEASES: dict[str, _Lease] = {}

# 解析器主动上传到服务器的文件（job_id -> (路径, 过期时间)）。
# QQ/NapCat 上云后，解析器回传的 local_path 在服务器上并不存在，
# 所以由解析器先把文件 POST 上来，插件再用 /media 临时链接交付。
_UPLOADS: Dict[str, tuple[Path, float]] = {}
_UPLOAD_TTL_SECONDS = 900


# ---------------------------------------------------------------- 本地解析器队列


@dataclass
class _ResolveJob:
    """等待本地解析器回传结果的任务。"""

    id: str
    url: str
    created_at: float
    future: "asyncio.Future[Dict[str, Any]]"
    delivered_at: Optional[float] = None


_JOBS: Dict[str, _ResolveJob] = {}
_WAITERS: set["asyncio.Future[None]"] = set()
_LAST_POLL: float = 0.0
_STATS: Dict[str, int] = {"poll": 0, "jobs": 0, "ok": 0, "failed": 0}


def _resolver_token() -> str:
    return str(plugin.get_config().resolver_token or "").strip()


def _resolver_online() -> bool:
    if not _resolver_token():
        return False
    now = time.time()
    if (now - _LAST_POLL) < int(plugin.get_config().resolver_stale_seconds):
        return True
    # 本地解析器正在处理任务时不会发心跳（轮询循环被占用），这段时间不能判它离线，
    # 否则解析要几十秒的视频会收到「解析器未连接」这种误导提示。
    grace = int(plugin.get_config().resolver_timeout)
    return any(
        job.delivered_at is not None and (now - job.delivered_at) < grace
        for job in _JOBS.values()
    )


def _resolver_authorized(token: Optional[str]) -> bool:
    expected = _resolver_token()
    if not expected:
        return False
    return secrets.compare_digest(str(token or ""), expected)


def _wake_waiters() -> None:
    for waiter in list(_WAITERS):
        if not waiter.done():
            waiter.set_result(None)


def _next_pending_job() -> Optional[_ResolveJob]:
    """取出最早的未派发任务（超过 45 秒未回传的任务允许重新派发）。"""
    now = time.time()
    for job in sorted(_JOBS.values(), key=lambda item: item.created_at):
        if job.delivered_at and now - job.delivered_at < 45:
            continue
        job.delivered_at = now
        return job
    return None


async def _resolve_via_local(url: str) -> Dict[str, Any]:
    """把抖音链接交给本地解析器，等待它回传 CDN 直链。"""
    loop = asyncio.get_running_loop()
    job = _ResolveJob(
        id=uuid.uuid4().hex,
        url=url,
        created_at=time.time(),
        future=loop.create_future(),
    )
    _JOBS[job.id] = job
    _STATS["jobs"] += 1
    _wake_waiters()
    try:
        return await asyncio.wait_for(job.future, timeout=int(plugin.get_config().resolver_timeout))
    except asyncio.TimeoutError as error:
        _STATS["failed"] += 1
        raise RuntimeError(
            "本地解析器没有在限定时间内返回结果，请确认 Windows 上的 media-resolver 正在运行。"
        ) from error
    finally:
        _JOBS.pop(job.id, None)


# ---------------------------------------------------------------- 基础工具


def _find_supported_url(text: str) -> Optional[str]:
    for raw in _URL_RE.findall(text or ""):
        url = raw.rstrip(".,;:!?)]}，。！？")
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        host = (urlparse(url).hostname or "").lower().strip(".")
        if _is_x(url):
            if _x_status_id(url) and _x_enabled():
                return url
            continue
        if _is_instagram(url):
            if _instagram_ref(url) and _meta_enabled("instagram_enabled"):
                return url
            continue
        if _is_facebook(url):
            if _facebook_ref(url) and _meta_enabled("facebook_enabled"):
                return url
            continue
        if any(host == domain or host.endswith("." + domain) for domain in _SUPPORTED_DOMAINS):
            return url
    return None


def _find_message_url(message: ChatMessage) -> Optional[str]:
    """Read ordinary text first, then QQ/NapCat's raw message representation."""
    candidates = [getattr(message, "content_text", "")]
    for attribute in ("raw_message", "message", "original_message"):
        value = getattr(message, attribute, "")
        if value:
            candidates.append(str(value))
    candidates.append(str(message))
    for text in candidates:
        url = _find_supported_url(text)
        if url:
            return url
    return None


def _host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _platform_of(url: str) -> Optional[str]:
    """判断是否属于需要本地解析器处理的平台，返回 douyin / xiaohongshu / tiktok。"""
    host = _host_of(url)
    if "tiktok" in host and (host == "tiktok.com" or host.endswith(".tiktok.com")):
        return "tiktok"
    if any(host == domain or host.endswith("." + domain) for domain in _RESOLVER_DOMAINS):
        return "douyin" if "douyin" in host else "xiaohongshu"
    return None


def _download_platform(url: str) -> Optional[str]:
    """下载图片 / 视频时用来挑 Referer 的平台键：本地解析器平台 + X（X 不走解析器，所以 _platform_of 里没有它）。"""
    if _is_x(url):
        return "x"
    if _is_instagram(url):
        return "instagram"
    if _is_facebook(url):
        return "facebook"
    return _platform_of(url)


def _download_ua(platform: Optional[str]) -> str:
    """下载直链用的 UA：Instagram / Facebook 用诚实的非浏览器 UA（浏览器 UA 反而会被拦），其它平台不变。"""
    return _META_UA if platform in ("instagram", "facebook") else _DOWNLOAD_UA


def _needs_local_resolver(url: str) -> bool:
    return _platform_of(url) is not None


def _source_name(url: str) -> str:
    host = _host_of(url)
    if "douyin" in host:
        return "抖音"
    if "xiaohongshu" in host or "xhslink" in host:
        return "小红书"
    if "tiktok" in host:
        return "TikTok"
    if "bilibili" in host or "b23.tv" in host:
        return "B站"
    if _is_x(url):
        return "X"
    if _is_instagram(url):
        return "Instagram"
    if _is_facebook(url):
        return "Facebook"
    return "未知平台"


def _looks_like_local_path(value: str) -> bool:
    """本地解析器回传的 NapCat 本机文件路径（Windows 盘符 / file:// / POSIX 绝对路径）。"""
    text = str(value or "").strip()
    if not text:
        return False
    if text.startswith("file://"):
        return True
    if re.match(r"^[A-Za-z]:[\\/]", text):
        return True
    return text.startswith("/")


def _take_upload(job_id: str) -> Optional[Path]:
    """取出某任务上传的文件（取出即从登记表移除，后续生命周期交给 media lease）。"""
    if not job_id:
        return None
    entry = _UPLOADS.pop(job_id, None)
    if entry is None:
        return None
    path, _expires_at = entry
    try:
        return path if path.is_file() and path.stat().st_size > 0 else None
    except OSError:
        return None


def _cleanup_expired() -> None:
    now = time.time()
    for token, lease in list(_LEASES.items()):
        if lease.expires_at > now:
            continue
        try:
            lease.path.unlink(missing_ok=True)
            parent = lease.path.parent
            parent.rmdir()
        except OSError:
            pass
        _LEASES.pop(token, None)
    for key, expires_at in list(_RECENT_URLS.items()):
        if expires_at <= now:
            _RECENT_URLS.pop(key, None)
    for job_id, (path, expires_at) in list(_UPLOADS.items()):
        if expires_at > now:
            continue
        try:
            path.unlink(missing_ok=True)
            path.parent.rmdir()
        except OSError:
            pass
        _UPLOADS.pop(job_id, None)


def _human_size(num_bytes: Optional[float]) -> str:
    try:
        value = float(num_bytes or 0)
    except (TypeError, ValueError):
        return "未知"
    if value <= 0:
        return "未知"
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return "未知"


def _pick_stream(videos: List[Dict[str, Any]], max_bytes: int) -> Optional[Dict[str, Any]]:
    """在本地解析器返回的多个清晰度里挑一个不超过体积上限、尽量清晰的。"""
    usable = [item for item in videos if isinstance(item, dict) and item.get("url")]
    if not usable:
        return None
    sized = [item for item in usable if item.get("size")]
    within = [item for item in sized if int(item["size"]) <= max_bytes]
    if within:
        return max(within, key=lambda item: int(item["size"]))
    if sized:
        return min(sized, key=lambda item: int(item["size"]))
    return usable[0]


# ---------------------------------------------------------------- 下载


async def _download_direct(
    url: str,
    target: Path,
    max_bytes: int,
    headers: Dict[str, str],
    proxy: Optional[str] = None,
) -> Path:
    """流式下载直链，超过体积上限立即中止。"""
    limit = int(max_bytes)
    total = 0
    timeout = httpx.Timeout(30.0, read=180.0)
    use_proxy = proxy or _proxy_for_url(url)
    client_kwargs: Dict[str, Any] = {"timeout": timeout, "follow_redirects": True, "headers": headers}
    if use_proxy:
        client_kwargs["proxy"] = use_proxy
    async with httpx.AsyncClient(**client_kwargs) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            declared = int(response.headers.get("content-length") or 0)
            if declared and declared > limit:
                raise RuntimeError(
                    f"视频体积 {_human_size(declared)} 超过 {_human_size(limit)} 上限，未下载。"
                )
            with target.open("wb") as handle:
                async for chunk in response.aiter_bytes(1 << 16):
                    total += len(chunk)
                    if total > limit:
                        raise RuntimeError(f"视频超过 {_human_size(limit)} 上限，已中止下载。")
                    handle.write(chunk)
    if total <= 0:
        raise RuntimeError("下载到的视频是空文件。")
    return target


async def _run_ytdlp(url: str, task_dir: Path, proxy: Optional[str] = None) -> tuple[Path, str]:
    """用 yt-dlp 下载（小红书兜底；X 的公开接口不可用时也回退到这里）。"""
    binary = (plugin.get_config().yt_dlp_path or "yt-dlp").strip()
    if not binary or ("/" not in binary and "\\" not in binary and not shutil.which(binary)):
        raise RuntimeError("服务器尚未安装 yt-dlp，管理员需要先安装后才能解析视频。")

    output = task_dir / "video.%(ext)s"
    max_mb = int(plugin.get_config().max_file_size_mb)
    command = [
        binary,
        "--no-playlist",
        "--no-warnings",
        "--no-progress",
        "--restrict-filenames",
        "--max-filesize",
        f"{max_mb}M",
        "--format",
        "bv*+ba/b",
        "--merge-output-format",
        "mp4",
        "--output",
        str(output),
        url,
    ]
    if proxy:
        command[1:1] = ["--proxy", proxy]
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=8 * 60)
    except asyncio.TimeoutError as error:
        process.kill()
        await process.communicate()
        raise RuntimeError("下载超时，已停止本次任务。") from error
    if process.returncode != 0:
        detail = stderr.decode("utf-8", "replace").strip().splitlines()
        safe_detail = detail[-1] if detail else "视频站点拒绝了下载请求"
        raise RuntimeError(f"无法取得该视频：{safe_detail[:220]}")

    files = [path for path in task_dir.iterdir() if path.is_file() and path.stat().st_size]
    if not files:
        raise RuntimeError("下载没有产生可发送的视频文件。")
    video = max(files, key=lambda path: path.stat().st_size)
    if video.stat().st_size > max_mb * 1024 * 1024:
        raise RuntimeError(f"视频超过 {max_mb} MB 限制，未发送。")
    return video, video.name


# ---------------------------------------------------------------- B 站
# 机房 IP 访问 bilibili 网页会被风控（HTTP 412），但公开 API 正常：
#   x/web-interface/view  → 标题 / cid / 时长 / 分P
#   x/player/playurl      → fnval=1 直接返回「单文件 mp4」直链，不需要 ffmpeg 合并音视频
# 所以 B 站不需要本地解析器，云端直连即可（未登录时 B 站只会给到 720p/480p）。

# B 站 wbi 签名：非签名接口（x/player/playurl）只返回匿名画质（最高 480p 的 DASH），
# 1080p / 4K 必须走 x/player/wbi/playurl 并带上 w_rid / wts 签名。
_BILI_MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]


class _BilibiliWbi:
    """wbi 签名器：用 nav 接口里的 img_key / sub_key 按固定表混排出 mixin_key。"""

    def __init__(self) -> None:
        self.mixin_key = ""

    def refresh(self, img_url: str, sub_url: str) -> str:
        img_key = str(img_url).rsplit("/", 1)[-1].split(".")[0]
        sub_key = str(sub_url).rsplit("/", 1)[-1].split(".")[0]
        raw = img_key + sub_key
        self.mixin_key = "".join(raw[i] for i in _BILI_MIXIN_TAB if i < len(raw))[:32]
        return self.mixin_key

    def sign(self, params: Dict[str, Any]) -> Dict[str, Any]:
        signed = {key: value for key, value in params.items() if value is not None}
        signed["wts"] = int(time.time())
        query = urlencode(sorted(signed.items()), safe="")
        query = re.sub(r"[!'()*]", "", query)
        signed["w_rid"] = hashlib.md5((query + self.mixin_key).encode()).hexdigest()
        return signed


_BILI_QUALITY_LABELS = {
    6: "240p",
    16: "360p",
    32: "480p",
    64: "720p",
    74: "720p60",
    80: "1080p",
    112: "1080p+",
    116: "1080p60",
    120: "4K",
    125: "HDR",
    126: "杜比视界",
}


def _is_bilibili(url: str) -> bool:
    host = _host_of(url)
    return any(host == domain or host.endswith("." + domain) for domain in ("bilibili.com", "b23.tv"))


def _bilibili_ids(url: str) -> Dict[str, Any]:
    """从链接里取出 bvid / aid / 分P 页码。"""
    match = re.search(r"(BV[0-9A-Za-z]{10})", url)
    bvid = match.group(1) if match else ""
    match_av = re.search(r"/av(\d+)", url, re.IGNORECASE)
    aid = match_av.group(1) if match_av else ""
    page = 1
    match_page = re.search(r"[?&]p=(\d+)", url)
    if match_page:
        try:
            page = max(1, int(match_page.group(1)))
        except ValueError:
            page = 1
    return {"bvid": bvid, "aid": aid, "page": page}


def _bilibili_ffmpeg() -> str:
    """找可用的 ffmpeg：配置值 → 挂载目录 → PATH。找不到就返回空串（自动退回 720p mp4）。"""
    candidates = [
        str(getattr(plugin.get_config(), "bilibili_ffmpeg_path", "") or "").strip(),
        "/home/ubuntu/srv/nekro_agent/bin/ffmpeg",
        "ffmpeg",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        if candidate == "ffmpeg" or Path(candidate).exists():
            return candidate
    return ""


def _pick_dash_streams(videos: Any, audios: Any) -> tuple[Optional[dict], Optional[dict]]:
    """挑画质最高、且编码对 QQ 友好的视频流 + 码率最高的音频流。"""
    codec_order = {7: 0, 12: 1, 13: 2}  # AVC(h264) > HEVC > AV1，QQ 对 h264 兼容最好
    best_video: Optional[dict] = None
    best_key: Optional[tuple] = None
    for item in videos or []:
        if not item or not item.get("baseUrl"):
            continue
        try:
            quality = int(item.get("id") or 0)
        except (TypeError, ValueError):
            quality = 0
        try:
            codec = int(item.get("codecid") or 0)
        except (TypeError, ValueError):
            codec = 0
        key = (-quality, codec_order.get(codec, 9))
        if best_key is None or key < best_key:
            best_key, best_video = key, item
    best_audio: Optional[dict] = None
    for item in audios or []:
        if not item or not item.get("baseUrl"):
            continue
        if best_audio is None or int(item.get("bandwidth") or 0) > int(best_audio.get("bandwidth") or 0):
            best_audio = item
    return best_video, best_audio


async def _download_bilibili_dash(
    resolved: Dict[str, Any], task_dir: Path, max_bytes: int
) -> tuple[Path, str]:
    """下载 DASH 的视频流 + 音频流，再用 ffmpeg 无损合并成一个 mp4（1080p 及以上必须走这条路）。"""
    dash = resolved.get("dash") or {}
    video_url = str(dash.get("video_url") or "")
    audio_url = str(dash.get("audio_url") or "")
    if not video_url or not audio_url:
        raise RuntimeError("B站 DASH 流地址缺失")
    ffmpeg = _bilibili_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("没有可用的 ffmpeg（无法合并 DASH 音视频）")

    headers = {"User-Agent": _DOWNLOAD_UA, "Referer": "https://www.bilibili.com/", "Accept": "*/*"}
    video_file = await _download_direct(video_url, task_dir / "video.m4s", max_bytes, headers)
    audio_file = await _download_direct(audio_url, task_dir / "audio.m4s", max_bytes, headers)
    total = video_file.stat().st_size + audio_file.stat().st_size
    if total > max_bytes:
        raise RuntimeError(
            f"B站视频合并前总体积 {_human_size(total)} 超过 {_human_size(max_bytes)} 上限，未发送。"
        )

    output = task_dir / "video.mp4"
    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video_file),
        "-i",
        str(audio_file),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(output),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=300)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        raise RuntimeError("ffmpeg 合并超时（视频过大）") from None
    if process.returncode != 0 or not output.exists() or output.stat().st_size == 0:
        detail = (stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(f"ffmpeg 合并失败：{detail[-1][:200] if detail else '未知错误'}")

    label = str((resolved.get("videos") or [{}])[0].get("label") or "bilibili")
    media_id = str(resolved.get("aweme_id") or "bilibili")
    plugin.logger.info(f"B站 DASH 合并完成：{label} 输出={_human_size(output.stat().st_size)}")
    return output, f"B站-{label}-{media_id}.mp4"


async def _expand_bilibili_url(url: str) -> str:
    """b23.tv 短链 → 跟随跳转拿完整链接。"""
    if "b23.tv" not in _host_of(url):
        return url
    async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
        response = await client.get(
            url, headers={"User-Agent": _DOWNLOAD_UA, "Referer": "https://www.bilibili.com/"}
        )
        return str(response.url)


async def _resolve_bilibili(url: str, force_mp4: bool = False) -> Dict[str, Any]:
    """取 B 站视频的元信息 + 单文件 mp4 直链（不需要登录、不需要 ffmpeg）。"""
    cookie = str(getattr(plugin.get_config(), "bilibili_cookie", "") or "").strip()
    headers = {"User-Agent": _DOWNLOAD_UA, "Referer": "https://www.bilibili.com/"}
    if cookie:
        # 允许只填 SESSDATA 的值，也允许直接粘整行 Cookie
        headers["Cookie"] = cookie if "=" in cookie else f"SESSDATA={cookie}"
    try:
        expanded = await _expand_bilibili_url(url)
    except Exception as error:  # noqa: BLE001 - 短链跳转失败时退回原链接
        plugin.logger.warning(f"B站短链展开失败（使用原链接继续）：{error}")
        expanded = url

    ids = _bilibili_ids(expanded)
    if not ids["bvid"] and not ids["aid"]:
        if re.search(r"/(bangumi|cheese)/play/", expanded):
            return {
                "ok": False,
                "platform": "bilibili",
                "error": "番剧 / 课程需要登录 Cookie，本插件暂不支持，请发普通投稿视频链接。",
            }
        return {
            "ok": False,
            "platform": "bilibili",
            "error": f"没能从链接里认出 BV 号（链接：{expanded[:100]}）。",
        }

    params = {"bvid": ids["bvid"]} if ids["bvid"] else {"aid": ids["aid"]}
    async with httpx.AsyncClient(headers=headers, timeout=25) as client:
        view = await client.get("https://api.bilibili.com/x/web-interface/view", params=params)
        view.raise_for_status()
        payload = view.json()
        if payload.get("code") != 0:
            return {
                "ok": False,
                "platform": "bilibili",
                "error": f"B站接口返回 {payload.get('code')}：{payload.get('message')}",
            }
        data = payload.get("data") or {}
        pages = data.get("pages") or []
        page_index = min(max(1, ids["page"]), len(pages) or 1) - 1
        page_info = pages[page_index] if pages else {}
        cid = page_info.get("cid")
        if not cid:
            return {
                "ok": False,
                "platform": "bilibili",
                "error": "B站接口没有返回 cid（视频可能已删除或仅会员可见）。",
            }

        # 有 ffmpeg 就走 DASH（1080p/4K）；否则退回单文件 mp4（匿名最高 720p）
        ffmpeg_path = "" if force_mp4 else _bilibili_ffmpeg()
        fnval = 4048 if ffmpeg_path else 1

        # 取 wbi 签名 key（nav 同时会告诉我们 cookie 是否被 B 站认作已登录）
        wbi = _BilibiliWbi()
        try:
            nav = await client.get("https://api.bilibili.com/x/web-interface/nav")
            nav_data = (nav.json() or {}).get("data") or {}
            wbi_data = nav_data.get("wbi_img") or {}
            if wbi_data.get("img_url") and wbi_data.get("sub_url"):
                wbi.refresh(str(wbi_data["img_url"]), str(wbi_data["sub_url"]))
                plugin.logger.info(
                    "B站 wbi 就绪（登录态：%s）"
                    % (
                        "已登录，可用 1080p+"
                        if nav_data.get("isLogin")
                        else "未登录，最高 480p DASH / 720p mp4"
                    )
                )
        except Exception as error:  # noqa: BLE001 - 拿不到 wbi key 就退回未签名接口
            plugin.logger.warning(f"B站 wbi key 获取失败：{error}")

        play_params: Dict[str, Any] = {
            "bvid": data.get("bvid") or ids["bvid"],
            "cid": cid,
            "qn": 120,
            "fnval": fnval,
            "fnver": 0,
            "fourk": 1,
        }
        if wbi.mixin_key:
            play = await client.get(
                "https://api.bilibili.com/x/player/wbi/playurl", params=wbi.sign(play_params)
            )
        else:
            play = await client.get(
                "https://api.bilibili.com/x/player/playurl", params=play_params
            )
        play.raise_for_status()
        play_payload = play.json()
        if play_payload.get("code") != 0:
            return {
                "ok": False,
                "platform": "bilibili",
                "error": (
                    f"B站播放接口返回 {play_payload.get('code')}：{play_payload.get('message')}"
                    "（部分视频需要登录或仅限特定地区）"
                ),
            }

        play_data = play_payload.get("data") or {}

        # ---- DASH 分支：视频流 + 音频流分开下，再用 ffmpeg 无损合并 ----
        # 注意：B 站匿名用户的 DASH 只给到 480p，而单文件 mp4 能到 720p。
        # 所以只有 DASH 达到 1080p（需要登录 Cookie）时才值得走合并，否则退回 mp4。
        if fnval != 1:
            dash = play_data.get("dash") or {}
            video_stream, audio_stream = _pick_dash_streams(dash.get("video"), dash.get("audio"))
            if video_stream and audio_stream:
                try:
                    quality = int(video_stream.get("id") or 0)
                except (TypeError, ValueError):
                    quality = 0
                label = _BILI_QUALITY_LABELS.get(quality, f"quality-{quality}")
                if quality >= 80:
                    total_pages = len(pages)
                    title = str(data.get("title") or "").strip()
                    if total_pages > 1:
                        title = f"{title}（P{page_index + 1}/{total_pages}）"
                    plugin.logger.info(
                        f"B站解析：{title[:40]} 清晰度={label}（DASH，待 ffmpeg 合并）"
                    )
                    return {
                        "ok": True,
                        "platform": "bilibili",
                        "mode": "dash",
                        "title": title,
                        "aweme_id": data.get("bvid") or ids["bvid"] or ids["aid"],
                        "duration_ms": int(
                            page_info.get("duration") or data.get("duration") or 0
                        )
                        * 1000,
                        "cover": data.get("pic"),
                        "dash": {
                            "video_url": video_stream.get("baseUrl"),
                            "audio_url": audio_stream.get("baseUrl"),
                        },
                        "videos": [
                            {
                                "label": label,
                                "url": video_stream.get("baseUrl"),
                                "size": None,
                                "quality": quality,
                            }
                        ],
                    }
                plugin.logger.info(f"B站 DASH 最高只有 {label}，改走单文件 mp4（匿名可到 720p）")
            else:
                plugin.logger.warning("B站没有返回 DASH 流，改走单文件 mp4")
            fallback = await client.get(
                "https://api.bilibili.com/x/player/playurl",
                params={
                    "bvid": data.get("bvid") or ids["bvid"],
                    "cid": cid,
                    "qn": 64,
                    "fnval": 1,
                    "fourk": 1,
                },
            )
            if fallback.status_code == 200:
                fallback_payload = fallback.json()
                if fallback_payload.get("code") == 0:
                    play_data = fallback_payload.get("data") or play_data

        segments = play_data.get("durl") or []
        if not segments:
            return {
                "ok": False,
                "platform": "bilibili",
                "error": "B站没有返回单文件 mp4 直链（这类视频通常需要登录才能取到 DASH 流）。",
            }
        if len(segments) > 1:
            return {
                "ok": False,
                "platform": "bilibili",
                "error": f"这条视频被切成了 {len(segments)} 段，需要 ffmpeg 合并，暂不支持。",
            }

        segment = segments[0]
        quality = play_data.get("quality")
        try:
            label = _BILI_QUALITY_LABELS.get(int(quality), f"quality-{quality}")
        except (TypeError, ValueError):
            label = "未知清晰度"

        total_pages = len(pages)
        title = str(data.get("title") or "").strip()
        if total_pages > 1:
            title = f"{title}（P{page_index + 1}/{total_pages}）"

        plugin.logger.info(
            f"B站解析：{title[:40]} 清晰度={label} 体积={_human_size(segment.get('size'))}"
        )
        return {
            "ok": True,
            "platform": "bilibili",
            "mode": "direct_url",
            "title": title,
            "aweme_id": data.get("bvid") or ids["bvid"] or ids["aid"],
            "duration_ms": int(page_info.get("duration") or data.get("duration") or 0) * 1000,
            "cover": data.get("pic"),
            "videos": [
                {
                    "label": label,
                    "url": segment.get("url"),
                    "size": segment.get("size"),
                    "quality": quality,
                }
            ],
        }


# ---------------------------------------------------------------- X (Twitter)
# X 的网页和官方 API 都要登录 / 付费，但「嵌入帖子」用的公开接口不需要：
#   https://cdn.syndication.twimg.com/tweet-result?id=<帖子ID>&token=<任意值>
# 返回单条公开帖子的 JSON（正文 / 作者 / 图片 / 视频各档 mp4 直链 / 引用帖子等），
# 所以 X 和 B 站一样不需要本地解析器，云端直连即可。
# 注意：这是 X 嵌入组件自用的接口，**并非官方承诺的 API**，字段和限流策略可能随时变化；
# 受保护账号、已删除、年龄/敏感内容限制的帖子拿不到（拿不到时回退 yt-dlp，仍失败就报错）。

_X_STATUS_RE = re.compile(r"/status(?:es)?/(\d{1,25})(?![0-9])", re.IGNORECASE)
_X_SYNDICATION_URL = "https://cdn.syndication.twimg.com/tweet-result"
_X_MAX_TEXT_CHARS = 1500
_BEIJING_TZ = timezone(timedelta(hours=8))


def _is_x(url: str) -> bool:
    """是否 X / Twitter 链接（含 fxtwitter / vxtwitter 这类镜像域名）。"""
    value = str(url or "").strip()
    if "://" not in value:
        value = "https://" + value
    host = _host_of(value).strip(".")
    return any(host == domain or host.endswith("." + domain) for domain in _X_DOMAINS)


def _x_status_id(url: str) -> Optional[str]:
    """从 X 链接里取出帖子 ID；支持 /<user>/status/<id>、/i/status/<id>、/i/web/status/<id>、
    以及 .../photo/1、.../video/1 和 ?s=46 这类尾巴。认不出来返回 None。"""
    value = str(url or "").strip()
    if not value:
        return None
    if "://" not in value:
        value = "https://" + value
    if not _is_x(value):
        return None
    match = _X_STATUS_RE.search(urlparse(value).path or "")
    return match.group(1) if match else None


def _x_proxy() -> Optional[str]:
    """X 专用代理：大陆机房直连不通，需要走代理。"""
    value = str(getattr(plugin.get_config(), "x_proxy", "") or "").strip()
    return value or None


_X_HOSTS = ("x.com", "twitter.com", "twimg.com", "t.co", "fxtwitter.com", "vxtwitter.com", "fixupx.com", "fixvx.com")


def _proxy_for_url(url: str) -> Optional[str]:
    """X/Twitter、Instagram、Facebook 相关域名的请求 / 下载按各自的代理配置走代理；其它平台保持直连。
    fbcdn.net 这类两家都可能用的 CDN 域名不在这里按域名判断，下载媒体的调用点按平台显式传入代理（见 _meta_proxy）。"""
    host = (_host_of(url) or "").lower()
    if any(host == h or host.endswith("." + h) for h in _X_HOSTS):
        return _x_proxy()
    if any(host == h or host.endswith("." + h) for h in _IG_HOSTS):
        return _meta_proxy("instagram")
    if any(host == h or host.endswith("." + h) for h in _FB_HOSTS):
        return _meta_proxy("facebook")
    return None


def _meta_proxy(platform: Optional[str]) -> Optional[str]:
    """Instagram / Facebook 专用代理（做法同 x_proxy：配置里留空 = 直连，不影响其它平台）。"""
    if platform not in ("instagram", "facebook"):
        return None
    try:
        value = str(getattr(plugin.get_config(), f"{platform}_proxy", "") or "").strip()
    except Exception:  # noqa: BLE001 - 读不到配置时按直连
        return None
    return value or None


_IG_HOSTS = ("instagram.com", "cdninstagram.com")
_FB_HOSTS = ("facebook.com", "fb.com", "fb.watch")  # fbcdn.net 两家都可能用，下载时按平台显式传代理


def _x_enabled() -> bool:
    try:
        return bool(getattr(plugin.get_config(), "x_enabled", True))
    except Exception:  # noqa: BLE001 - 读不到配置时按默认启用
        return True


def _x_token(tweet_id: str) -> str:
    """syndication 的 token：与 X 嵌入组件相同的算法 ``(id / 1e15 * π)`` 转 36 进制并去掉 0 和小数点。
    实测接口并不校验它（任意值如 ``a`` 也行），这里仍按原算法生成，更接近浏览器行为。"""
    try:
        value = (int(tweet_id) / 1e15) * math.pi
    except (TypeError, ValueError):
        return "a"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    integer = int(value)
    fraction = value - integer
    out = ""
    while integer > 0:
        integer, rest = divmod(integer, 36)
        out = digits[rest] + out
    out = out or "0"
    tail = ""
    for _ in range(12):
        if fraction <= 0:
            break
        fraction *= 36
        digit = int(fraction)
        tail += digits[digit]
        fraction -= digit
    token = (out + tail).replace("0", "")
    return token or "a"


def _x_clean_text(data: Dict[str, Any]) -> str:
    """整理帖子正文：去掉媒体占位的 t.co 链接，把普通短链换成真实地址，还原 HTML 转义。"""
    text = str(data.get("text") or "")
    entities = data.get("entities") or {}
    for media in entities.get("media") or []:
        if isinstance(media, dict) and media.get("url"):
            text = text.replace(str(media["url"]), "")
    quoted_id = str(((data.get("quoted_tweet") or {}).get("id_str")) or "")
    for item in entities.get("urls") or []:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        expanded = str(item.get("expanded_url") or item["url"])
        # 引用帖子时，正文末尾那条指向被引用帖子的链接没有信息量，去掉
        replacement = "" if quoted_id and quoted_id in expanded else expanded
        text = text.replace(str(item["url"]), replacement)
    return html.unescape(text).strip()


def _x_beijing_time(created_at: Any) -> str:
    """ISO 时间（UTC）→ 北京时间 ``YYYY-MM-DD HH:MM``；解析不了就返回空串。"""
    try:
        moment = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(_BEIJING_TZ).strftime("%Y-%m-%d %H:%M")


def _x_tombstone_message(data: Dict[str, Any]) -> str:
    """帖子墓碑（TweetTombstone）→ 可读的中文原因。"""
    tombstone = data.get("tombstone") or {}
    raw = ""
    for key in ("text", "title"):
        part = tombstone.get(key)
        raw = str((part.get("text") if isinstance(part, dict) else part) or "") or raw
    lowered = raw.lower()
    if "suspended" in lowered:
        reason = "发帖账号已被封禁"
    elif "protected" in lowered or "limits who can view" in lowered or "approved followers" in lowered:
        reason = "账号开启了帖子保护，只有关注者能看"
    elif "sensitive" in lowered or "age-restricted" in lowered or "log in" in lowered or "sign in" in lowered:
        reason = "涉及敏感内容或年龄限制，需要登录才能查看"
    elif "deleted" in lowered or "removed" in lowered or "unavailable" in lowered:
        reason = "帖子已被删除或不可用"
    else:
        reason = "帖子不可见"
    return f"无法解析这条 X 帖子：{reason}。" + (f"（X 提示：{raw[:120]}）" if raw else "")


def _x_video_variants(media: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从 mediaDetails 的 video_info.variants 里挑出 mp4（HLS m3u8 不要），按码率从高到低排序。"""
    info = media.get("video_info") or {}
    duration_ms = int(info.get("duration_millis") or 0)
    is_gif = media.get("type") == "animated_gif"
    out: List[Dict[str, Any]] = []
    for variant in info.get("variants") or []:
        if not isinstance(variant, dict) or not variant.get("url"):
            continue
        if "mp4" not in str(variant.get("content_type") or ""):
            continue
        link = str(variant["url"])
        size_match = re.search(r"/(\d{2,5})x(\d{2,5})/", link)
        width = int(size_match.group(1)) if size_match else 0
        height = int(size_match.group(2)) if size_match else 0
        bitrate = int(variant.get("bitrate") or 0)
        label = "动图" if is_gif else (f"{height}p" if height else (f"{bitrate // 1000}k" if bitrate else "mp4"))
        item: Dict[str, Any] = {
            "label": label,
            "url": link,
            "size": None,
            "width": width or None,
            "height": height or None,
            "bitrate": bitrate or None,
        }
        if bitrate and duration_ms:
            item["estimated_size"] = int(bitrate * duration_ms / 8000)
        out.append(item)
    out.sort(key=lambda item: int(item.get("bitrate") or 0), reverse=True)
    return out


async def _x_fill_sizes(videos: List[Dict[str, Any]]) -> None:
    """用 HEAD 请求补上每档 mp4 的真实体积（供 _pick_stream 按上限挑选）；
    拿不到就退回「码率 × 时长」的估算值，估算值只作兜底。"""
    if not videos:
        return
    headers = {"User-Agent": _DOWNLOAD_UA, "Referer": _DOWNLOAD_REFERER["x"], "Accept": "*/*"}

    async def head(client: httpx.AsyncClient, item: Dict[str, Any]) -> None:
        try:
            response = await client.head(str(item["url"]))
            length = int(response.headers.get("content-length") or 0)
            if response.status_code < 400 and length > 0:
                item["size"] = length
                return
        except Exception as error:  # noqa: BLE001 - HEAD 失败不影响解析，退回估算
            plugin.logger.info(f"X 视频体积探测失败（使用估算值）：{error}")
        if item.get("estimated_size"):
            item["size"] = item["estimated_size"]
            item["size_estimated"] = True

    size_kwargs: Dict[str, Any] = {"headers": headers, "timeout": 15, "follow_redirects": True}
    if _x_proxy():
        size_kwargs["proxy"] = _x_proxy()
    async with httpx.AsyncClient(**size_kwargs) as client:
        await asyncio.gather(*(head(client, item) for item in videos))


async def _fetch_x_syndication(tweet_id: str) -> Dict[str, Any]:
    """请求 syndication 接口，少量重试。返回 ``{"kind": ..., ...}``：
    ``data`` 正常 / ``missing`` 404 / ``blocked`` 401、403 / ``retry`` 暂时性失败（限流、网络、非 JSON）。"""
    headers = {
        "User-Agent": _DOWNLOAD_UA,
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://platform.twitter.com/",
    }
    params = {"id": tweet_id, "token": _x_token(tweet_id)}
    last_error = "未知错误"
    for attempt in range(3):
        if attempt:
            await asyncio.sleep(1.5 * attempt)
        try:
            x_kwargs: Dict[str, Any] = {"headers": headers, "timeout": httpx.Timeout(15.0), "follow_redirects": True}
            if _x_proxy():
                x_kwargs["proxy"] = _x_proxy()
            async with httpx.AsyncClient(**x_kwargs) as client:
                response = await client.get(_X_SYNDICATION_URL, params=params)
        except httpx.HTTPError as error:
            last_error = f"网络错误 {type(error).__name__}"
            continue
        status = response.status_code
        if status == 404:
            return {"kind": "missing"}
        if status in (401, 403):
            return {"kind": "blocked", "status": status}
        if status == 429 or status >= 500:
            last_error = f"HTTP {status}"
            continue
        if status != 200:
            last_error = f"HTTP {status}"
            break
        try:
            payload = response.json()
        except ValueError:
            last_error = "返回的不是 JSON"
            continue
        if not isinstance(payload, dict):
            last_error = "返回内容格式异常"
            continue
        return {"kind": "data", "data": payload}
    return {"kind": "retry", "error": last_error}


async def _resolve_x(url: str) -> Dict[str, Any]:
    """取 X 帖子的正文 + 图片 + 视频直链（公开嵌入接口，不需要登录、不需要 API token）。

    返回 ``ok=False`` 且带 ``fallback=True`` 表示接口暂时不可用（限流 / 网络 / 返回异常），
    调用方可以回退 yt-dlp；没有 ``fallback`` 的失败（已删除 / 受保护 / 需要登录）是确定的，不必再试。
    """
    tweet_id = _x_status_id(url)
    if not tweet_id:
        return {"ok": False, "platform": "x", "error": f"没能从链接里认出 X 帖子 ID（链接：{str(url)[:100]}）。"}

    fetched = await _fetch_x_syndication(tweet_id)
    kind = fetched.get("kind")
    if kind == "missing":
        return {
            "ok": False,
            "platform": "x",
            "error": "没有找到这条 X 帖子：可能已被删除、发帖账号受保护，或需要登录才能查看。",
        }
    if kind == "blocked":
        return {
            "ok": False,
            "platform": "x",
            "error": f"X 拒绝了公开访问（HTTP {fetched.get('status')}），这条帖子可能需要登录才能查看。",
        }
    if kind != "data":
        return {
            "ok": False,
            "platform": "x",
            "fallback": True,
            "error": f"X 公开数据接口暂时不可用（{fetched.get('error')}）。",
        }

    data: Dict[str, Any] = fetched["data"]
    typename = str(data.get("__typename") or "")
    if typename == "TweetTombstone" or (data.get("tombstone") and not data.get("text")):
        return {"ok": False, "platform": "x", "error": _x_tombstone_message(data)}
    if not data.get("id_str") and not data.get("text") and not data.get("mediaDetails"):
        # 受保护 / 已删除的帖子偶尔会返回空对象
        return {
            "ok": False,
            "platform": "x",
            "error": "X 没有返回这条帖子的内容：可能已被删除、账号受保护或需要登录才能查看。",
        }

    user = data.get("user") or {}
    details = [item for item in (data.get("mediaDetails") or []) if isinstance(item, dict)]
    unavailable = [
        item
        for item in details
        if str((item.get("ext_media_availability") or {}).get("status") or "Available").lower() != "available"
    ]

    image_list: List[str] = []
    videos: List[Dict[str, Any]] = []
    cover: Optional[str] = None
    duration_ms = 0
    has_video_media = False
    for media in details:
        media_type = media.get("type")
        link = str(media.get("media_url_https") or "")
        if media_type == "photo" and link:
            # ?name=orig 取原图（接口默认给的是压缩过的 large）
            image_list.append(link + ("&" if "?" in link else "?") + "name=orig")
            cover = cover or link
        elif media_type in ("video", "animated_gif"):
            if has_video_media:
                continue  # 一条帖子只会有一个视频 / 动图，其余忽略
            has_video_media = True
            videos = _x_video_variants(media)
            duration_ms = int((media.get("video_info") or {}).get("duration_millis") or 0)
            if link:
                cover = link
    if not image_list and not has_video_media:
        # 个别返回只有顶层 photos 字段
        for photo in data.get("photos") or []:
            if isinstance(photo, dict) and photo.get("url"):
                image_list.append(str(photo["url"]) + "?name=orig")
        cover = cover or (image_list[0] if image_list else None)

    if has_video_media and not videos and not image_list and not str(data.get("text") or "").strip():
        return {"ok": False, "platform": "x", "error": "这条 X 视频没有可用的 mp4 直链（可能只有 HLS 流）。"}
    if unavailable and not videos and not image_list and not str(data.get("text") or "").strip():
        return {
            "ok": False,
            "platform": "x",
            "error": "这条 X 帖子的媒体不可用（可能涉及敏感内容、版权限制，或需要登录才能查看）。",
        }

    await _x_fill_sizes(videos)

    quoted_raw = data.get("quoted_tweet") if isinstance(data.get("quoted_tweet"), dict) else None
    quoted: Optional[Dict[str, Any]] = None
    if quoted_raw:
        quoted_user = quoted_raw.get("user") or {}
        quoted = {
            "id": str(quoted_raw.get("id_str") or ""),
            "author": str(quoted_user.get("name") or ""),
            "screen_name": str(quoted_user.get("screen_name") or ""),
            "text": _x_clean_text(quoted_raw),
        }

    screen_name = str(user.get("screen_name") or "")
    canonical = (
        f"https://x.com/{screen_name}/status/{tweet_id}" if screen_name else f"https://x.com/i/status/{tweet_id}"
    )
    return {
        "ok": True,
        "platform": "x",
        "mode": "direct_url",
        "note_id": str(data.get("id_str") or tweet_id),
        "title": _x_clean_text(data),
        "author": str(user.get("name") or ""),
        "screen_name": screen_name,
        "created_at": data.get("created_at"),
        "created_at_beijing": _x_beijing_time(data.get("created_at")),
        "likes": data.get("favorite_count"),
        "replies": data.get("conversation_count"),
        "lang": data.get("lang"),
        "possibly_sensitive": bool(data.get("possibly_sensitive")),
        "long_text": bool(data.get("note_tweet")),
        "canonical_url": canonical,
        "duration_ms": duration_ms,
        "cover": cover,
        "videos": videos,
        "image_list": image_list,
        "is_gif": any(item.get("type") == "animated_gif" for item in details),
        "quoted": quoted,
    }


def _format_x_caption(resolved: Dict[str, Any], kind: str, with_stats: bool) -> str:
    """把解析结果排成合并转发里放在媒体前面的文字：作者 / 北京时间 / 正文 / 引用 / 原链接。"""
    author = str(resolved.get("author") or "").strip()
    screen_name = str(resolved.get("screen_name") or "").strip()
    who = f"{author} @{screen_name}" if author and screen_name else (f"@{screen_name}" if screen_name else author)
    when = str(resolved.get("created_at_beijing") or "")
    meta = [part for part in (who, f"{when}（北京时间）" if when else "") if part]
    lines = [f"X 帖子 · {kind}"]
    if meta:
        lines.append(" · ".join(meta))
    if with_stats and (resolved.get("likes") is not None or resolved.get("replies") is not None):
        stats = []
        if resolved.get("likes") is not None:
            stats.append(f"点赞 {resolved['likes']}")
        if resolved.get("replies") is not None:
            stats.append(f"回复 {resolved['replies']}")
        lines.append(" · ".join(stats))
    body = str(resolved.get("title") or "").strip()
    if body:
        if len(body) > _X_MAX_TEXT_CHARS:
            body = body[:_X_MAX_TEXT_CHARS] + "…"
        lines.append("")
        lines.append(body)
        if resolved.get("long_text"):
            lines.append("（长文，接口只返回了开头部分，完整内容请看原帖）")
    quoted = resolved.get("quoted") or None
    if quoted and (quoted.get("text") or quoted.get("author")):
        quoted_who = f"@{quoted['screen_name']}" if quoted.get("screen_name") else (quoted.get("author") or "")
        quoted_text = str(quoted.get("text") or "").strip()
        if len(quoted_text) > _X_MAX_TEXT_CHARS:
            quoted_text = quoted_text[:_X_MAX_TEXT_CHARS] + "…"
        lines.append("")
        lines.append(f"↪ 引用 {quoted_who}：{quoted_text}" if quoted_text else f"↪ 引用 {quoted_who}")
    lines.append("")
    lines.append(f"链接：{resolved.get('canonical_url') or ''}")
    return "\n".join(lines)


async def _obtain_x(url: str, task_dir: Path) -> "_MediaSource":
    """X 帖子 → 可发送的内容：视频 / 动图下 mp4，图片按图组，纯文字只有文字节点。

    解析靠公开嵌入接口；接口暂时不可用时回退 yt-dlp（只能拿到视频，没有正文）。
    """
    config = plugin.get_config()
    x_limit = int(getattr(config, "x_max_size_mb", 100) or 100) * 1024 * 1024
    send_text = bool(getattr(config, "x_send_text", True))
    show_stats = bool(getattr(config, "x_show_stats", True))

    try:
        resolved = await _resolve_x(url)
    except Exception as error:  # noqa: BLE001 - 网络层之外的意外错误也走回退
        plugin.logger.warning(f"X 解析异常：{error}")
        resolved = {"ok": False, "platform": "x", "fallback": True, "error": f"X 解析失败：{error}"}

    if not resolved.get("ok"):
        message = str(resolved.get("error") or "X 解析失败")
        if not resolved.get("fallback"):
            raise RuntimeError(message)
        plugin.logger.warning(f"{message} 回退 yt-dlp")
        try:
            video, name = await _run_ytdlp(url, task_dir)
        except Exception as error:  # noqa: BLE001 - 把两边的原因一起报给用户
            raise RuntimeError(f"{message}yt-dlp 回退也失败：{error}") from error
        return _MediaSource(name=name, path=video, size=video.stat().st_size)

    note_id = resolved.get("note_id") or _x_status_id(url) or int(time.time())
    videos = resolved.get("videos") or []
    images = [str(item) for item in (resolved.get("image_list") or []) if item]
    limit = max(1, int(getattr(config, "max_images", 9) or 9))
    picked_images = images[:limit]

    if videos:
        kind = "动图" if resolved.get("is_gif") else "视频"
    elif images:
        kind = f"图文（{len(picked_images)} 张）"
    else:
        kind = "纯文字"
    caption = _format_x_caption(resolved, kind, show_stats) if send_text else ""
    if caption and len(images) > len(picked_images):
        caption += f"\n（共 {len(images)} 张图片，仅发送前 {len(picked_images)} 张）"

    if not videos and not images and not send_text:
        raise RuntimeError("这条 X 帖子只有文字，而配置里已关闭「X 发送帖子文字」，没有可发送的内容。")

    source = _MediaSource(name=f"X-{note_id}", caption=caption)
    if images:
        source.images = tuple(picked_images)
        plugin.logger.info(f"X 图片：共 {len(images)} 张，本次发送 {len(picked_images)} 张")
    if videos:
        picked = _pick_stream(videos, x_limit)
        if picked and picked.get("size") and int(picked["size"]) > x_limit:
            raise RuntimeError(
                f"X 视频最小也有 {_human_size(picked['size'])}，超过 {_human_size(x_limit)} 上限，未发送。"
                "可在插件配置里调大「X 视频体积上限」。"
            )
        downloaded = await _download_resolved(resolved, "x", task_dir, x_limit)
        source.name = downloaded.name
        source.path = downloaded.path
        source.size = downloaded.size
    return source



# ---------------------------------------------------------------- Instagram / Facebook（Meta）
# 两个平台都**不登录、不带 cookie / token**，只读公开帖子，和 X、B 站一样不需要本地解析器：
#
# * 实测（2026-10-27，单一东京出口）：用**诚实的非浏览器 UA** + ``Accept: text/html`` 匿名 GET 公开帖子页，
#   Meta 会返回服务端渲染（SSR）的页面，里面直接带着作者 / 全文 / 发布时间 / 点赞评论数 / 全部图片 / 视频直链；
#   反而是浏览器 UA 会被 Instagram 给 JS 空壳、被 Facebook 直接 400。所以这里**不伪装浏览器**。
# * 这些都是网页的内部数据，**不是官方 API**，字段和行为随时可能变；也可能违反 Meta 的服务条款，请自行评估，
#   只建议个人 bot、低频、只读公开内容。国内 IP / 机房 IP / 高频请求的表现未测。
# * 带签名的 CDN 直链会过期（oe= 参数），所以解析完要马上下载。
# * 唯一的灰色做法：Instagram 在诚实 UA 拿不到数据时，可选用 Googlebot UA 再试一次，
#   由配置项 ``instagram_fallback_ua`` 控制，**默认开启**（可设为 false 关闭）；只有诚实 UA 失败时才会触发。
#   Facebook 没有任何 UA 伪装。

_META_UA = "nekro-media-forward/1.0 (+personal bot; non-browser)"
_GOOGLEBOT_UA = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
_META_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
_META_MAX_TEXT_CHARS = 2200
_META_MAX_VIDEOS = 4  # 一条帖子（轮播 / 相册）里最多发送几个视频，避免合并转发过大
_FB_MAX_COMMENTS = 5
_IG_DOMAINS = ("instagram.com",)
_FB_DOMAINS = ("facebook.com", "fb.com", "fb.watch")
_IG_POST_RE = re.compile(r"^/(?:[A-Za-z0-9_.]{1,30}/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]{5,})", re.IGNORECASE)
_IG_STORY_RE = re.compile(r"^/stories/", re.IGNORECASE)
_FB_SEG = r"[^/?#\s]+"


def _host_in(url: str, domains: tuple) -> bool:
    value = str(url or "").strip()
    if "://" not in value:
        value = "https://" + value
    host = _host_of(value).strip(".")
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def _is_instagram(url: str) -> bool:
    return _host_in(url, _IG_DOMAINS)


def _is_facebook(url: str) -> bool:
    return _host_in(url, _FB_DOMAINS)


def _meta_enabled(name: str) -> bool:
    try:
        return bool(getattr(plugin.get_config(), name, True))
    except Exception:  # noqa: BLE001 - 读不到配置时按默认启用
        return True


def _instagram_ref(url: str) -> Optional[Dict[str, str]]:
    """认出 Instagram 帖子链接：/p/ /reel/ /reels/ /tv/（含 /<用户>/p/<code>/ 和 ?igsh= 之类的尾巴），
    以及 /stories/（识别出来是为了给明确的中文报错）。首页、个人主页、/explore/ 等返回 None。"""
    value = str(url or "").strip()
    if not value:
        return None
    if "://" not in value:
        value = "https://" + value
    if not _is_instagram(value):
        return None
    path = urlparse(value).path or ""
    if _IG_STORY_RE.match(path):
        return {"kind": "story", "code": ""}
    match = _IG_POST_RE.match(path)
    if not match:
        return None
    segment = re.search(r"/(p|reel|reels|tv)/", path, re.IGNORECASE)
    kind = (segment.group(1).lower() if segment else "p").replace("reels", "reel")
    return {"kind": kind, "code": match.group(1)}


def _instagram_shortcode(url: str) -> Optional[str]:
    ref = _instagram_ref(url)
    return ref["code"] if ref and ref["kind"] != "story" and ref["code"] else None


def _facebook_ref(url: str) -> Optional[Dict[str, str]]:
    """认出 Facebook 内容链接，返回 ``{"kind": ..., ...}``；认不出（首页、个人主页、登录页…）返回 None。

    kind：post（/<页面>/posts/<id>）、video（/<页面>/videos/<id>、/video.php?v=）、reel、watch（/watch/?v=）、
    photo（photo.php / photo/?fbid= / /<页面>/photos/…）、permalink（permalink.php / story.php）、
    share（/share/p|v|r/<code>）、group（/groups/…，通常要登录）、fbwatch（fb.watch 短链）。
    """
    value = str(url or "").strip()
    if not value:
        return None
    if "://" not in value:
        value = "https://" + value
    if not _is_facebook(value):
        return None
    parts = urlparse(value)
    host = (parts.hostname or "").lower().strip(".")
    path = parts.path or "/"
    query = {key: values[0] for key, values in parse_qs(parts.query).items() if values}
    if host == "fb.watch" or host.endswith(".fb.watch"):
        code = path.strip("/")
        return {"kind": "fbwatch", "code": code} if code and "/" not in code else None
    match = re.match(r"^/share/(?:([pvr])/)?([A-Za-z0-9_-]{6,})/?$", path)
    if match:
        return {"kind": "share", "sub": match.group(1) or "", "code": match.group(2)}
    if re.match(r"^/groups/[^/]+/(?:posts|permalink)/", path):
        return {"kind": "group", "path": path}
    match = re.match(r"^/reel/(\d+)", path)
    if match:
        return {"kind": "reel", "id": match.group(1)}
    if re.match(r"^/watch/?$", path) and query.get("v", "").isdigit():
        return {"kind": "watch", "id": query["v"]}
    if path.rstrip("/") == "/video.php" and query.get("v", "").isdigit():
        return {"kind": "video", "id": query["v"], "path": ""}
    if path.rstrip("/") in ("/photo.php", "/photo") and query.get("fbid"):
        return {"kind": "photo", "id": query["fbid"]}
    if path.rstrip("/") in ("/permalink.php", "/story.php") and query.get("story_fbid"):
        return {"kind": "permalink", "story_fbid": query["story_fbid"], "id": query.get("id", "")}
    match = re.match(rf"^/({_FB_SEG})/posts/({_FB_SEG}(?:/{_FB_SEG})*)/?$", path)
    if match and match.group(1) not in ("groups", "watch", "reel", "share"):
        return {"kind": "post", "path": path.rstrip("/"), "id": match.group(2).rsplit("/", 1)[-1]}
    match = re.match(rf"^/({_FB_SEG})/videos/(?:{_FB_SEG}/)*(\d+)/?$", path)
    if match:
        return {"kind": "video", "id": match.group(2), "path": path}
    match = re.match(rf"^/({_FB_SEG})/(?:photos|reels)/(?:[^/]+/)*([A-Za-z0-9_-]+)/?$", path)
    if match and match.group(1) not in ("groups", "watch", "reel", "share"):
        return {"kind": "photo", "id": match.group(2), "path": path}
    return None


# ---------------------------------------------------------------- Meta 公共：取页面 / 工具


def _meta_headers(ua: str, accept: str = _META_ACCEPT) -> Dict[str, str]:
    return {"User-Agent": ua, "Accept": accept, "Accept-Language": "en-US,en;q=0.8"}


def _meta_login_redirect(location: str) -> bool:
    path = (urlparse(location).path or "").lower()
    return path.startswith(("/login", "/checkpoint", "/accounts/login")) or path.endswith("login.php")


def _fb_rewrite_story_link(location: str) -> Optional[str]:
    """story.php / permalink.php?story_fbid=X&id=Y（/share/p/ 短链的第一跳）→ /Y/posts/X，
    这种形态匿名能直接拿到帖子页；原样跟下去会被带到登录页。"""
    parts = urlparse(location)
    if (parts.path or "").rstrip("/") not in ("/story.php", "/permalink.php"):
        return None
    query = {key: values[0] for key, values in parse_qs(parts.query).items() if values}
    if query.get("story_fbid") and query.get("id"):
        return f"https://www.facebook.com/{query['id']}/posts/{query['story_fbid']}"
    return None


async def _fetch_meta_html(
    url: str,
    ua: str = _META_UA,
    accept: str = _META_ACCEPT,
    timeout: float = 40.0,
    attempts: int = 2,
    max_hops: int = 5,
) -> Dict[str, Any]:
    """匿名取一个 Instagram / Facebook 页面（手动跟随跳转，不让它把我们带进登录页）。

    返回 ``{"kind": ...}``：``ok``（带 text / url）、``login``（被重定向到登录页）、``missing``（404 / 410）、
    ``retry``（429、5xx、网络错误、超时：暂时性失败，值得回退到别的路径）、``error``（其它 4xx / 异常跳转）。
    """
    current = url
    last_error = "未知错误"
    hops = 0
    while hops <= max_hops:
        response = None
        for attempt in range(max(1, attempts)):
            if attempt:
                await asyncio.sleep(1.5 * attempt)
            try:
                meta_kwargs: Dict[str, Any] = {
                    "headers": _meta_headers(ua, accept),
                    "timeout": httpx.Timeout(timeout),
                    "follow_redirects": False,
                }
                page_proxy = _proxy_for_url(current)
                if page_proxy:
                    meta_kwargs["proxy"] = page_proxy
                async with httpx.AsyncClient(**meta_kwargs) as client:
                    response = await client.get(current)
            except httpx.HTTPError as error:
                last_error = f"网络错误 {type(error).__name__}"
                response = None
                continue
            if response.status_code == 429 or response.status_code >= 500:
                last_error = f"HTTP {response.status_code}"
                response = None
                continue
            if response.status_code == 200 and not response.content.strip():
                last_error = "空响应"  # Facebook 偶尔对同一个页面返回 200 + 空正文，重试一次通常就好
                response = None
                continue
            break
        if response is None:
            return {"kind": "retry", "error": last_error, "url": current}
        status = response.status_code
        if status in (301, 302, 303, 307, 308):
            location = urljoin(current, response.headers.get("location", ""))
            if not location or _meta_login_redirect(location):
                return {"kind": "login", "url": current, "status": status}
            rewritten = _fb_rewrite_story_link(location)
            if rewritten:
                location = rewritten
            host = (urlparse(location).hostname or "").lower()
            if not (_host_in(location, _FB_DOMAINS + _IG_DOMAINS)) or host.endswith("fb.watch"):
                return {"kind": "error", "error": f"异常跳转到 {host or '未知地址'}", "url": current}
            current = location
            hops += 1
            continue
        if status in (404, 410):
            return {"kind": "missing", "status": status, "url": current}
        if status in (401, 403):
            return {"kind": "login", "status": status, "url": current}
        if status != 200:
            return {"kind": "error", "error": f"HTTP {status}", "status": status, "url": current}
        return {"kind": "ok", "text": response.text, "url": current, "status": status}
    return {"kind": "error", "error": "跳转次数过多", "url": current}


def _meta_json_blocks(page: str) -> List[Any]:
    """页面里 ``<script type="application/json" ... data-sjs>`` 的 JSON 块（Relay 预取数据）。"""
    blocks: List[Any] = []
    for match in re.finditer(r'<script type="application/json"[^>]*data-sjs[^>]*>(.*?)</script>', page, re.S):
        try:
            blocks.append(json.loads(match.group(1)))
        except ValueError:
            continue
    return blocks


def _meta_walk(obj: Any, path: tuple = ()):
    """深度优先遍历 JSON，产出 (路径, 节点)。"""
    yield path, obj
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from _meta_walk(value, path + (key,))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            yield from _meta_walk(value, path + (index,))


def _meta_og(page: str, prop: str) -> Optional[str]:
    match = re.search(r'<meta[^>]+property="%s"[^>]+content="([^"]*)"' % re.escape(prop), page)
    return html.unescape(match.group(1)) if match else None


def _meta_utc_iso(timestamp: Any) -> Optional[str]:
    try:
        return datetime.fromtimestamp(int(timestamp), timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _meta_sniff_ext(data: bytes) -> str:
    """按文件头判断图片扩展名（CDN 有时给 webp / png，不能一律当 jpg）。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return ".jpg"


def _meta_trim(text: str) -> str:
    text = str(text or "").strip()
    return text[:_META_MAX_TEXT_CHARS] + "…" if len(text) > _META_MAX_TEXT_CHARS else text


def _meta_post_kind(resolved: Dict[str, Any], picked_images: int) -> str:
    videos = len(resolved.get("videos") or [])
    images = len(resolved.get("image_list") or [])
    if videos and images:
        return f"图文 + 视频（{picked_images} 张图 + {videos} 个视频）"
    if videos:
        return "视频" if videos == 1 else f"视频（{videos} 个）"
    if images:
        return f"图文（{picked_images} 张）"
    return "纯文字"


# ---------------------------------------------------------------- Instagram


def _ig_find_media(page: str, code: str) -> Optional[Dict[str, Any]]:
    """从页面 SSR 数据里找帖子节点：``xig_polaris_media.if_not_gated_logged_out``（带 taken_at 和 code）。
    ``if_not_gated_logged_out`` 为 null 说明这条帖子匿名看不到（私密 / 受限）。"""
    for block in re.findall(r'<script type="application/json"[^>]*data-sjs[^>]*>(.*?)</script>', page, re.S):
        if "if_not_gated_logged_out" not in block:
            continue
        try:
            data = json.loads(block)
        except ValueError:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if "taken_at" in node and "code" in node and (not code or node.get("code") == code):
                    return node
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    return None


def _ig_page_item(node: Dict[str, Any]) -> Dict[str, Any]:
    versions = ((node.get("image_versions2") or {}).get("candidates")) or []
    # candidates[0] 没有 stp 尺寸限制，就是原图（实测 1080~4096px）；display_uri 是 640px 的备用
    image = str(versions[0].get("url")) if versions and versions[0].get("url") else str(node.get("display_uri") or "")
    fallback = str(node.get("display_uri") or "")
    video_versions = [item for item in (node.get("video_versions") or []) if isinstance(item, dict) and item.get("url")]
    width, height = node.get("original_width"), node.get("original_height")
    item: Dict[str, Any] = {
        "type": "video" if (node.get("media_type") == 2 or video_versions) else "image",
        "image_url": image,
        "image_fallback": fallback if fallback and fallback != image else "",
        "width": width,
        "height": height,
    }
    if video_versions:
        item["video_url"] = str(video_versions[0]["url"])
    return item


def _ig_post_from_page(node: Dict[str, Any]) -> Dict[str, Any]:
    kids = [item for item in (node.get("carousel_media") or []) if isinstance(item, dict)] or [node]
    user = node.get("user") or {}
    return {
        "source": "page",
        "shortcode": node.get("code"),
        "product_type": node.get("product_type"),
        "username": str(user.get("username") or ""),
        "full_name": str(user.get("full_name") or ""),
        "caption": str((node.get("caption") or {}).get("text") or ""),
        "likes": node.get("like_count"),
        "comments": node.get("comment_count"),
        "taken_at": node.get("taken_at"),
        "items": [_ig_page_item(kid) for kid in kids],
    }


def _ig_post_from_embed(page: str) -> Optional[Dict[str, Any]]:
    """备用：``/p/<code>/embed/captioned/`` 的 contextJSON → gql_data.shortcode_media。
    轮播 / 视频可用；单图帖这里是 null；没有发布时间。"""
    match = re.search(r'"contextJSON":"((?:[^"\\]|\\.)*)"', page)
    if not match:
        return None
    try:
        context = json.loads(json.loads('"' + match.group(1) + '"'))
        media = context["gql_data"]["shortcode_media"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(media, dict):
        return None
    edges = (media.get("edge_media_to_caption") or {}).get("edges") or [{}]
    kids = [edge["node"] for edge in (media.get("edge_sidecar_to_children") or {}).get("edges", []) if edge.get("node")] or [media]
    items = []
    for kid in kids:
        resources = kid.get("display_resources") or []
        if resources:
            best = max(resources, key=lambda res: int(res.get("config_width") or 0) * int(res.get("config_height") or 0))
            image, width, height = best.get("src"), best.get("config_width"), best.get("config_height")
        else:
            dims = kid.get("dimensions") or {}
            image, width, height = kid.get("display_url"), dims.get("width"), dims.get("height")
        item: Dict[str, Any] = {
            "type": "video" if kid.get("is_video") else "image",
            "image_url": str(image or ""),
            "image_fallback": "",
            "width": width,
            "height": height,
        }
        if kid.get("is_video") and kid.get("video_url"):
            item["video_url"] = str(kid["video_url"])
        items.append(item)
    owner = media.get("owner") or {}
    return {
        "source": "embed",
        "shortcode": media.get("shortcode"),
        "product_type": media.get("product_type"),
        "username": str(owner.get("username") or ""),
        "full_name": str(owner.get("full_name") or ""),
        "caption": str((edges[0].get("node") or {}).get("text") or ""),
        "likes": (media.get("edge_liked_by") or {}).get("count"),
        "comments": (media.get("edge_media_to_comment") or {}).get("count"),
        "taken_at": media.get("taken_at_timestamp"),
        "items": items,
    }


def _ig_build_resolved(post: Dict[str, Any], code: str, kind: str) -> Dict[str, Any]:
    items = post.get("items") or []
    image_list: List[str] = []
    fallbacks: Dict[str, str] = {}
    videos: List[Dict[str, Any]] = []
    cover: Optional[str] = None
    for item in items:
        if item.get("image_url"):
            cover = cover or item["image_url"]
        if item.get("type") == "video" and item.get("video_url"):
            width, height = item.get("width"), item.get("height")
            videos.append({
                "label": f"{width}x{height}" if width and height else "mp4",
                "url": item["video_url"],
                "size": None,
                "width": width,
                "height": height,
            })
        elif item.get("type") == "image" and item.get("image_url"):
            image_list.append(item["image_url"])
            if item.get("image_fallback"):
                fallbacks[item["image_url"]] = item["image_fallback"]
    shown_kind = "reel" if (post.get("product_type") == "clips" or kind in ("reel", "tv")) else "p"
    created = _meta_utc_iso(post.get("taken_at"))
    return {
        "ok": True,
        "platform": "instagram",
        "mode": "direct_url",
        "source": post.get("source"),
        "note_id": code,
        "title": str(post.get("caption") or "").strip(),
        "author": post.get("full_name") or post.get("username") or "",
        "screen_name": post.get("username") or "",
        "created_at": created,
        "created_at_beijing": _x_beijing_time(created) if created else "",
        "likes": post.get("likes"),
        "replies": post.get("comments"),
        "canonical_url": f"https://www.instagram.com/{shown_kind}/{code}/",
        "media_count": len(items),
        "cover": cover,
        "videos": videos,
        "image_list": image_list,
        "image_fallbacks": fallbacks,
    }


async def _ig_fetch_post(code: str) -> Dict[str, Any]:
    """页面 SSR → （可选）Googlebot UA → embed。成功返回 ``{"post": {...}}``，失败返回 ``{"errors": [...]}``。"""
    errors: List[str] = []
    config = plugin.get_config()
    agents = [(_META_UA, "page")]
    if bool(getattr(config, "instagram_fallback_ua", True)):
        agents.append((_GOOGLEBOT_UA, "page(Googlebot UA)"))
    for ua, label in agents:
        fetched = await _fetch_meta_html(f"https://www.instagram.com/p/{code}/", ua, timeout=30.0)
        if fetched["kind"] == "ok":
            node = await asyncio.to_thread(_ig_find_media, fetched["text"], code)
            if node:
                post = _ig_post_from_page(node)
                post["source"] = label
                return {"post": post}
            errors.append(f"{label}：页面里没有帖子数据（只拿到 JS 空壳，或帖子需要登录）")
            continue
        errors.append(f"{label}：{fetched['kind']} {fetched.get('error') or fetched.get('status') or ''}".strip())
        if fetched["kind"] == "login":
            break  # 被带去登录页就不要再换 UA 硬试了
    fetched = await _fetch_meta_html(f"https://www.instagram.com/p/{code}/embed/captioned/", _META_UA, timeout=30.0)
    if fetched["kind"] == "ok":
        post = await asyncio.to_thread(_ig_post_from_embed, fetched["text"])
        if post:
            return {"post": post}
        errors.append("embed：没有 contextJSON（单图帖 / 私密 / 已删除 / 格式变了）")
    else:
        errors.append(f"embed：{fetched['kind']} {fetched.get('error') or fetched.get('status') or ''}".strip())
    return {"errors": errors}


async def _resolve_instagram(url: str) -> Dict[str, Any]:
    """取 Instagram 帖子的作者 / 全文 / 时间 / 点赞评论数 / 全部图片 / 视频直链（匿名，不登录）。

    ``ok=False`` 且带 ``fallback=True`` 表示页面和 embed 都没拿到数据，调用方可以继续 yt-dlp → oEmbed；
    没有 ``fallback`` 的失败（Stories、认不出链接）是确定的。
    """
    ref = _instagram_ref(url)
    if not ref:
        return {"ok": False, "platform": "instagram", "error": f"没能从链接里认出 Instagram 帖子（链接：{str(url)[:100]}）。"}
    if ref["kind"] == "story":
        return {
            "ok": False,
            "platform": "instagram",
            "error": "暂不支持 Instagram Stories：Stories 必须登录才能看，本插件不使用登录态。请发 /p/ 或 /reel/ 帖子链接。",
        }
    code = ref["code"]
    fetched = await _ig_fetch_post(code)
    post = fetched.get("post")
    if not post:
        return {
            "ok": False,
            "platform": "instagram",
            "fallback": True,
            "code": code,
            "error": "Instagram 没有给出这条帖子的数据（" + "；".join(fetched.get("errors") or ["未知原因"]) + "）。",
        }
    resolved = _ig_build_resolved(post, code, ref["kind"])
    if not resolved["videos"] and not resolved["image_list"] and not resolved["title"]:
        return {"ok": False, "platform": "instagram", "fallback": True, "code": code, "error": "Instagram 返回的帖子里没有任何可用内容。"}
    return resolved


async def _ig_oembed(url: str) -> Dict[str, Any]:
    """官方公开的 oEmbed 接口（不要 token 的那个网页端点）：只有文字 + 缩略图。
    返回 ``{"kind": "ok", "data": {...}}`` / ``missing``（404：已删除、私密或不存在）/ ``error``。"""
    endpoint = "https://www.instagram.com/api/v1/oembed/?url=" + quote(url, safe="")
    fetched = await _fetch_meta_html(endpoint, _META_UA, accept="application/json,*/*;q=0.5", timeout=20.0)
    if fetched["kind"] == "missing":
        return {"kind": "missing"}
    if fetched["kind"] != "ok":
        return {"kind": "error", "error": f"{fetched['kind']} {fetched.get('error') or fetched.get('status') or ''}".strip()}
    try:
        data = json.loads(fetched["text"])
    except ValueError:
        return {"kind": "error", "error": "oEmbed 返回的不是 JSON"}
    return {"kind": "ok", "data": data} if isinstance(data, dict) else {"kind": "error", "error": "oEmbed 返回格式异常"}


def _format_ig_caption(resolved: Dict[str, Any], kind: str, picked_images: int = 0) -> str:
    """合并转发里放在媒体前面的文字：作者 / 北京时间 / 点赞评论 / 全文 / 原链接。"""
    author = str(resolved.get("author") or "").strip()
    screen_name = str(resolved.get("screen_name") or "").strip()
    who = f"{author} @{screen_name}" if author and screen_name and author != screen_name else (f"@{screen_name}" if screen_name else author)
    when = str(resolved.get("created_at_beijing") or "")
    meta = [part for part in (who, f"{when}（北京时间）" if when else "") if part]
    lines = [f"Instagram 帖子 · {kind}"]
    if meta:
        lines.append(" · ".join(meta))
    stats = []
    if resolved.get("likes") is not None:
        stats.append(f"点赞 {resolved['likes']}")
    if resolved.get("replies") is not None:
        stats.append(f"评论 {resolved['replies']}")
    if stats:
        lines.append(" · ".join(stats))
    body = str(resolved.get("title") or "").strip()
    if body:
        lines.append("")
        lines.append(_meta_trim(body))
        if len(body) > _META_MAX_TEXT_CHARS:
            lines.append("（正文过长，已截断，完整内容请看原帖）")
    if resolved.get("note"):
        lines.append("")
        lines.append(str(resolved["note"]))
    lines.append("")
    lines.append(f"链接：{resolved.get('canonical_url') or ''}")
    return "\n".join(lines)


async def _obtain_instagram(url: str, task_dir: Path) -> "_MediaSource":
    """Instagram 帖子 → 可发送内容。回退链：页面 SSR → embed（都在 _resolve_instagram 里）
    → oEmbed 探测（404 = 已删除 / 私密，直接报错）→ yt-dlp（只能拿视频）→ oEmbed（只有文字 + 缩略图）。"""
    config = plugin.get_config()
    limit = int(getattr(config, "instagram_max_size_mb", 100) or 100) * 1024 * 1024
    try:
        resolved = await _resolve_instagram(url)
    except Exception as error:  # noqa: BLE001 - 意外错误也走回退
        plugin.logger.warning(f"Instagram 解析异常：{error}")
        resolved = {"ok": False, "platform": "instagram", "fallback": True, "error": f"Instagram 解析失败：{error}"}

    if resolved.get("ok"):
        return await _obtain_meta_post(resolved, "instagram", task_dir, limit, _format_ig_caption)

    message = str(resolved.get("error") or "Instagram 解析失败")
    if not resolved.get("fallback"):
        raise RuntimeError(message)
    plugin.logger.warning(f"{message} 回退 oEmbed / yt-dlp")
    canonical = f"https://www.instagram.com/p/{resolved.get('code')}/" if resolved.get("code") else url
    oembed = await _ig_oembed(canonical)
    if oembed["kind"] == "missing":
        raise RuntimeError("没有找到这条 Instagram 帖子：可能已被删除、账号是私密的，或需要登录才能查看。")
    text_post: Optional[Dict[str, Any]] = None
    if oembed["kind"] == "ok":
        data = oembed["data"]
        text_post = {
            "title": str(data.get("title") or "").strip(),
            "author": str(data.get("author_name") or ""),
            "screen_name": str(data.get("author_name") or ""),
            "canonical_url": canonical,
            "thumbnail": str(data.get("thumbnail_url") or ""),
        }
    try:
        video, name = await _run_ytdlp(url, task_dir, _meta_proxy("instagram"))
    except Exception as error:  # noqa: BLE001 - 图片帖 yt-dlp 本来就会失败，不是异常情况
        plugin.logger.info(f"Instagram yt-dlp 回退没有拿到视频：{error}")
        if not text_post:
            raise RuntimeError(f"{message}yt-dlp 回退和 oEmbed 也都失败（{error}；oEmbed：{oembed.get('error')}）。") from error
        text_post["note"] = "（Instagram 没有给出完整数据，只取到了文字和缩略图；完整内容请看原帖）"
        caption = _format_ig_caption(text_post, "文字 + 缩略图")
        source = _MediaSource(name=f"Instagram-{resolved.get('code') or int(time.time())}", caption=caption)
        if text_post["thumbnail"]:
            source.images = (text_post["thumbnail"],)
        return source
    caption = ""
    if text_post:
        text_post["note"] = "（视频由 yt-dlp 取得；点赞 / 时间等数据没有取到）"
        caption = _format_ig_caption(text_post, "视频")
    return _MediaSource(name=name, path=video, size=video.stat().st_size, caption=caption)


# ---------------------------------------------------------------- Facebook


def _fb_drop_ctp(url: str) -> Optional[str]:
    """单图的 CDN 链接带 ``ctp=p600x600``（客户端目标尺寸），会把图压到 ~900px。
    签名（oh=）并不覆盖 ctp，去掉它就是原图（实测 2048x1365）；去掉 stp/cstp 会 403，所以只去 ctp。
    没有 ctp 参数时返回 None。这是改动 Meta 签发的 URL，属于「利用 URL 参数」，失败时调用方回退原 URL。"""
    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if not any(key == "ctp" for key, _ in pairs):
        return None
    return urlunsplit(parts._replace(query=urlencode([(key, value) for key, value in pairs if key != "ctp"])))


def _fb_photo_item(media: Dict[str, Any]) -> Dict[str, Any]:
    viewer = media.get("viewer_image") or {}
    image = media.get("image") or {}
    photo_image = media.get("photo_image") if isinstance(media.get("photo_image"), dict) else {}
    uri = viewer.get("uri") or photo_image.get("uri") or image.get("uri")
    return {
        "type": "image",
        "id": media.get("id"),
        "url": uri,
        "url_full": _fb_drop_ctp(uri) if uri else None,
        "width": viewer.get("width") or image.get("width") or photo_image.get("width"),
        "height": viewer.get("height") or image.get("height") or photo_image.get("height"),
    }


def _fb_best_dash(mpd: str) -> tuple:
    """DASH 清单里挑 H.264(avc1) 码率最高的视频流 + 码率最高的音频流（QQ 对 H.264 + AAC 兼容最好）。"""
    namespace = "{urn:mpeg:dash:schema:mpd:2011}"
    try:
        root = ET.fromstring(mpd)
    except ET.ParseError:
        return None, None
    reps = []
    for rep in root.iter(namespace + "Representation"):
        base = rep.find(namespace + "BaseURL")
        if base is None or not base.text:
            continue
        reps.append({
            "kind": "v" if str(rep.get("mimeType") or "").startswith("video") else "a",
            "bandwidth": int(rep.get("bandwidth") or 0),
            "codecs": rep.get("codecs") or "",
            "url": base.text,
            "width": int(rep.get("width") or 0),
            "height": int(rep.get("height") or 0),
            "label": rep.get("FBQualityLabel"),
        })
    videos = [rep for rep in reps if rep["kind"] == "v" and rep["codecs"].startswith("avc1")] or [
        rep for rep in reps if rep["kind"] == "v"
    ]
    audios = [rep for rep in reps if rep["kind"] == "a"]
    best_video = max(videos, key=lambda rep: rep["bandwidth"]) if videos else None
    best_audio = max(audios, key=lambda rep: rep["bandwidth"]) if audios else None
    return best_video, best_audio


def _fb_parse_page(page: str) -> Dict[str, Any]:
    """解析 Facebook 帖子页里的 Relay 数据：作者 / 全文 / 时间 / 反应评论分享数 / 图片 / 视频 / 前几条评论。
    找不到的字段留空（降级），不抛异常。视频只记 id 和 DASH 清单里挑出来的流（真正的下载链接在 _fb_video_sources）。"""
    blocks = _meta_json_blocks(page)
    result: Dict[str, Any] = {
        "author": None, "author_id": None, "text": None, "created": None, "reactions": None, "comments": None,
        "shares": None, "views": None, "items": [], "album_count": None, "link": None, "source": None,
        "post_id": None, "permalink": None, "comments_sample": [],
    }
    story = None
    for block in blocks:
        for _path, node in _meta_walk(block):
            if (
                isinstance(node, dict)
                and node.get("__typename") == "Story"
                and "creation_time" in node
                and isinstance(node.get("comet_sections"), dict)
            ):
                story = node
                break
        if story:
            break
    created = None
    if story:
        result["source"] = "relay:Story"
        result["post_id"] = story.get("post_id")
        result["permalink"] = story.get("permalink_url")
        created = story.get("creation_time")
        actor = (story.get("actors") or [{}])[0]
        result["author"], result["author_id"] = actor.get("name"), actor.get("id")
        sections = story.get("comet_sections")
        for _path, node in _meta_walk(sections):
            if isinstance(node, dict) and isinstance(node.get("message"), dict) and "text" in node["message"]:
                result["text"] = node["message"]["text"]
                break
        for _path, node in _meta_walk(sections):
            if isinstance(node, dict) and "i18n_reaction_count" in node and isinstance(node.get("reaction_count"), dict) and "share_count" in node:
                result["reactions"] = node["reaction_count"].get("count")
                result["shares"] = (node.get("share_count") or {}).get("count")
                break
        for _path, node in _meta_walk(sections):
            if isinstance(node, dict) and isinstance(node.get("comments"), dict) and "total_count" in node["comments"]:
                result["comments"] = node["comments"]["total_count"]
                break
        for attachment in story.get("attachments") or []:
            style = ((attachment.get("styles") or {}).get("attachment")) or {}
            subs = style.get("all_subattachments")
            if isinstance(subs, dict) and subs.get("nodes"):
                result["album_count"] = subs.get("count")
                for sub in subs["nodes"]:
                    media = sub.get("media") or {}
                    if media.get("__typename") == "Photo":
                        result["items"].append(_fb_photo_item(media))
                    elif media.get("__typename") == "Video":
                        result["items"].append({"type": "video", "id": media.get("id")})
                continue
            media = style.get("media") or {}
            if media.get("__typename") == "Photo":
                result["items"].append(_fb_photo_item(media))
            elif media.get("__typename") == "Video":
                result["items"].append({"type": "video", "id": media.get("id")})
            if style.get("title") or style.get("url"):
                source = style.get("source")
                result["link"] = {
                    "title": style.get("title"),
                    "url": style.get("url") or attachment.get("url"),
                    "domain": source.get("text") if isinstance(source, dict) else source,
                }
    # 视频 / Reel 页：data.video 是主视频；同页的相关视频不属于这条帖子
    primary = None
    for block in blocks:
        for path, node in _meta_walk(block):
            if path and path[-1] == "video" and isinstance(node, dict) and node.get("id") and ("creation_story" in node or "story" in node) and "result" in path:
                primary = node["id"]
                story_info = node.get("creation_story") or {}
                created = created or story_info.get("creation_time")
                result["post_id"] = result["post_id"] or primary
                break
        if primary:
            break
    if primary:
        for block in blocks:
            for _path, node in _meta_walk(block):
                if isinstance(node, dict) and node.get("id") == primary and isinstance(node.get("publish_time"), int):
                    created = node["publish_time"]
                if isinstance(node, dict) and node.get("id") == primary and isinstance(node.get("creation_story"), dict):
                    # 视频页里 creation_story.message.text 是完整正文（og:description 会被截断到 ~200 字）
                    story_message = node["creation_story"].get("message")
                    if not result["text"] and isinstance(story_message, dict) and story_message.get("text"):
                        result["text"] = story_message["text"]
                if isinstance(node, dict) and "play_count" in node and "video_view_count" in node:
                    # play_count 就是页面上显示的「xx 次观看」；video_view_count 是另一个口径（更小）
                    result["views"] = node.get("play_count") or node.get("video_view_count")
    seen: set = set()
    for block in blocks:
        for _path, node in _meta_walk(block):
            if not (isinstance(node, dict) and "dash_manifest_xml_string" in node and node.get("id") not in seen):
                continue
            known = any(item.get("id") == node.get("id") for item in result["items"])
            if primary and node.get("id") != primary and not known:
                continue  # 相关 / 下一个视频，不是这条帖子的
            if story and not known and (result["items"] or seen):
                continue  # 帖子页：只收这条帖子的附件视频
            seen.add(node["id"])
            best_video, best_audio = _fb_best_dash(node["dash_manifest_xml_string"])
            entry = next((item for item in result["items"] if item["type"] == "video" and item.get("id") == node["id"]), None)
            if entry is None:
                entry = {"type": "video", "id": node["id"]}
                result["items"].append(entry)
            if best_video:
                entry.update(
                    dash_video_url=best_video["url"], dash_audio_url=best_audio["url"] if best_audio else None,
                    width=best_video["width"], height=best_video["height"], label=best_video["label"],
                )
            result["source"] = result["source"] or "relay:video"
    if result["source"] == "relay:video":
        title = _meta_og(page, "og:title") or ""
        parts = [part.strip() for part in title.split(" | ")]
        if len(parts) >= 2 and re.search(r"views|reactions", parts[0]):
            result["author"] = parts[-1] if len(parts) >= 3 else result["author"] or parts[-1]
        for block in blocks:
            for _path, node in _meta_walk(block):
                if isinstance(node, dict) and isinstance(node.get("comments"), dict) and "total_count" in node["comments"] and result["comments"] is None:
                    result["comments"] = node["comments"]["total_count"]
                if isinstance(node, dict) and "i18n_reaction_count" in node and isinstance(node.get("reaction_count"), dict) and result["reactions"] is None:
                    result["reactions"] = node["reaction_count"].get("count")
                    result["shares"] = (node.get("share_count") or {}).get("count") if "share_count" in node else None
    seen_comments: set = set()
    for block in blocks:
        for _path, node in _meta_walk(block):
            if (
                isinstance(node, dict) and isinstance(node.get("body"), dict) and node["body"].get("text")
                and isinstance(node.get("author"), dict) and node.get("id") not in seen_comments and "created_time" in node
            ):
                seen_comments.add(node.get("id"))
                result["comments_sample"].append({"author": node["author"].get("name"), "text": node["body"]["text"]})
    result["comments_sample"] = result["comments_sample"][:_FB_MAX_COMMENTS]
    result["created"] = _meta_utc_iso(created)
    # og 标签兜底：全文被截断时至少有一点文字；没有任何附件时至少有首图
    if not result["text"]:
        result["text"] = _meta_og(page, "og:description")
        if result["text"]:
            result["text_truncated"] = True
    if not result["author"]:
        result["author"] = _meta_og(page, "og:title")
    if not result["items"]:
        og_image = _meta_og(page, "og:image")
        if og_image:
            result["items"].append({"type": "image", "url": og_image, "url_full": None, "note": "og:image"})
        result["source"] = result["source"] or "og-only"
    return result


def _fb_parse_plugin_post(page: str) -> Dict[str, Any]:
    """``/plugins/post.php``（嵌入组件）：文字 + 发布时间 + 反应评论分享数 + 前几条评论 + 小图（约 400px 级）。
    比主页面差很多，只作为回退。"""
    result: Dict[str, Any] = {
        "author": None, "text": None, "created": None, "reactions": None, "comments": None, "shares": None,
        "items": [], "comments_sample": [], "source": "plugins/post.php",
    }
    for match in re.finditer(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', page, re.S):
        try:
            data = json.loads(match.group(1))
        except ValueError:
            continue
        if not isinstance(data, dict) or data.get("@type") != "SocialMediaPosting":
            continue
        try:
            result["created"] = datetime.strptime(str(data.get("dateCreated")), "%Y-%m-%dT%H:%M:%S%z").astimezone(timezone.utc).isoformat()
        except ValueError:
            pass
        for stat in data.get("interactionStatistic") or []:
            kind = str(stat.get("interactionType") or "")
            if kind.endswith("CommentAction"):
                result["comments"] = stat.get("userInteractionCount")
            elif kind.endswith("LikeAction"):
                result["reactions"] = stat.get("userInteractionCount")
            elif kind.endswith("ShareAction"):
                result["shares"] = stat.get("userInteractionCount")
        for comment in data.get("comment") or []:
            if isinstance(comment, dict) and comment.get("text"):
                result["comments_sample"].append({"author": (comment.get("author") or {}).get("name"), "text": comment["text"]})
        headline = str(data.get("headline") or "")
        if " - " in headline:
            result["author"] = headline.rsplit(" - ", 1)[-1].strip() or None
        break
    match = re.search(r'<div class="_2_79[^"]*">([^<]+)', page)
    if match:
        result["author"] = html.unescape(match.group(1)).strip() or result["author"]
    match = re.search(r'data-testid="post_message"[^>]*>(.*?)</div>', page, re.S)
    if match:
        raw = re.sub(r"</p>\s*<p>", "\n\n", match.group(1))  # 段落之间空一行
        raw = re.sub(r"<br\s*/?>", "\n", raw)
        text = html.unescape(re.sub(r"<[^>]+>", "", raw))
        result["text"] = re.sub(r"[ \t]*\n[ \t]*", "\n", text).strip() or None
    for match in re.finditer(r'<img[^>]+class="[^"]*scaledImageFit[^"]*"[^>]+src="([^"]+)"', page):
        result["items"].append({"type": "image", "url": html.unescape(match.group(1)), "url_full": None})
    result["comments_sample"] = result["comments_sample"][:_FB_MAX_COMMENTS]
    return result


async def _fb_video_sources(video_id: str) -> Dict[str, Optional[str]]:
    """``/plugins/video.php``：嵌入播放器页，里面直接给单文件 H.264 + AAC 的 mp4（hd_src 720p / sd_src 360p），
    不需要 ffmpeg 合并，比 DASH 简单，优先用它。"""
    href = f"https://www.facebook.com/watch/?v={video_id}"
    endpoint = "https://www.facebook.com/plugins/video.php?href=" + quote(href, safe="") + "&show_text=false"
    fetched = await _fetch_meta_html(endpoint, _META_UA, timeout=30.0)
    sources: Dict[str, Optional[str]] = {"hd": None, "sd": None}
    if fetched["kind"] != "ok":
        return sources
    for key, name in (("hd", "hd_src"), ("sd", "sd_src")):
        match = re.search(r'"%s":"(https:(?:[^"\\]|\\.)*)"' % name, fetched["text"])
        if match:
            try:
                sources[key] = json.loads('"' + match.group(1) + '"')
            except ValueError:
                continue
    return sources


def _fb_fetch_url(ref: Dict[str, str], original: str) -> str:
    """规范化要抓的页面地址：一律 www.facebook.com（m. / mbasic. 会跳登录），去掉追踪参数。"""
    kind = ref["kind"]
    if kind in ("post", "video") and ref.get("path"):
        return "https://www.facebook.com" + ref["path"].rstrip("/") + "/"
    if kind == "video":
        return f"https://www.facebook.com/watch/?v={ref['id']}"
    if kind == "reel":
        return f"https://www.facebook.com/reel/{ref['id']}"
    if kind == "watch":
        return f"https://www.facebook.com/watch/?v={ref['id']}"
    if kind == "photo" and ref.get("path"):
        return "https://www.facebook.com" + ref["path"]
    if kind == "photo":
        return f"https://www.facebook.com/photo.php?fbid={ref['id']}"
    if kind == "permalink":
        if ref.get("id"):
            return f"https://www.facebook.com/{ref['id']}/posts/{ref['story_fbid']}"
        return f"https://www.facebook.com/permalink.php?story_fbid={ref['story_fbid']}"
    if kind == "share":
        sub = f"{ref['sub']}/" if ref.get("sub") else ""
        return f"https://www.facebook.com/share/{sub}{ref['code']}/"
    return original


def _fb_login_message(what: str = "这条 Facebook 内容") -> str:
    return (
        f"{what}需要登录才能查看：可能是私密 / 仅好友可见、年龄或地区受限，或这种链接形态（photo.php、permalink.php 等）匿名拿不到。"
        "本插件不登录；请发 /<主页>/posts/<id> 形式的公开帖子链接。"
    )


async def _fb_resolve_fallbacks(ref: Dict[str, str], fetch_url: str) -> Optional[Dict[str, Any]]:
    """主页面不可用时的回退：plugins/post.php（文字 + 小图 + 计数）；视频类链接再加 plugins/video.php 取视频。"""
    parsed: Optional[Dict[str, Any]] = None
    endpoint = "https://www.facebook.com/plugins/post.php?href=" + quote(fetch_url, safe="") + "&show_text=true&width=500"
    fetched = await _fetch_meta_html(endpoint, _META_UA, timeout=30.0)
    if fetched["kind"] == "ok":
        candidate = await asyncio.to_thread(_fb_parse_plugin_post, fetched["text"])
        if candidate.get("text") or candidate.get("items"):
            parsed = candidate
    video_id = ref.get("id") if ref["kind"] in ("video", "reel", "watch") else None
    if video_id:
        parsed = parsed or {
            "author": None, "text": None, "created": None, "reactions": None, "comments": None, "shares": None,
            "items": [], "comments_sample": [], "source": "plugins/video.php",
        }
        if not any(item.get("type") == "video" for item in parsed["items"]):
            parsed["items"].append({"type": "video", "id": video_id})
    return parsed


async def _fb_build_resolved(parsed: Dict[str, Any], canonical: str) -> Dict[str, Any]:
    items = parsed.get("items") or []
    image_list: List[str] = []
    fallbacks: Dict[str, str] = {}
    cover: Optional[str] = None
    for item in items:
        if item.get("type") != "image" or not item.get("url"):
            continue
        best = item.get("url_full") or item["url"]
        image_list.append(best)
        cover = cover or best
        if best != item["url"]:
            fallbacks[best] = item["url"]  # 去掉 ctp 的链接失败时回退原 URL
    video_items = [item for item in items if item.get("type") == "video"][:_META_MAX_VIDEOS]
    sources = await asyncio.gather(*(_fb_video_sources(str(item["id"])) for item in video_items)) if video_items else []
    videos: List[Dict[str, Any]] = []
    for item, source in zip(video_items, sources):
        progressive = source.get("hd") or source.get("sd")
        if not progressive and not item.get("dash_video_url"):
            continue
        height = item.get("height")
        label = item.get("label") or (f"{height}p" if height else ("HD" if source.get("hd") else "SD"))
        videos.append({
            "label": str(label),
            "url": progressive,
            "size": None,
            "width": item.get("width") or None,
            "height": height or None,
            "id": item.get("id"),
            "dash_video_url": item.get("dash_video_url"),
            "dash_audio_url": item.get("dash_audio_url"),
        })
    created = parsed.get("created")
    link = parsed.get("link") or None
    return {
        "ok": True,
        "platform": "facebook",
        "mode": "direct_url",
        "source": parsed.get("source"),
        "note_id": str(parsed.get("post_id") or ""),
        "title": str(parsed.get("text") or "").strip(),
        "text_truncated": bool(parsed.get("text_truncated")),
        "author": str(parsed.get("author") or ""),
        "screen_name": "",
        "created_at": created,
        "created_at_beijing": _x_beijing_time(created) if created else "",
        "likes": parsed.get("reactions"),
        "replies": parsed.get("comments"),
        "shares": parsed.get("shares"),
        "views": parsed.get("views"),
        "canonical_url": canonical,
        "cover": cover,
        "videos": videos,
        "video_total": len([item for item in items if item.get("type") == "video"]),
        "image_list": image_list,
        "image_fallbacks": fallbacks,
        "link_attachment": link,
        "comments_sample": parsed.get("comments_sample") or [],
        "album_count": parsed.get("album_count"),
    }


async def _resolve_facebook(url: str) -> Dict[str, Any]:
    """取 Facebook 公开帖子的作者 / 全文 / 时间 / 反应评论分享数 / 全部图片 / 视频（匿名，不登录）。

    回退链：主页面 Relay 数据 → plugins/post.php（视频类链接再加 plugins/video.php）→ og 标签。
    ``ok=False`` 且带 ``fallback=True`` 表示都没拿到、但属于暂时性失败，调用方可再试 yt-dlp。
    """
    ref = _facebook_ref(url)
    if not ref:
        return {"ok": False, "platform": "facebook", "error": f"没能从链接里认出 Facebook 帖子（链接：{str(url)[:100]}）。"}
    if ref["kind"] == "fbwatch":
        return {"ok": False, "platform": "facebook", "error": "暂不支持 fb.watch 短链，请发完整链接（在 Facebook 里点「复制链接」拿 facebook.com 开头的那条）。"}
    if ref["kind"] == "group":
        return {"ok": False, "platform": "facebook", "error": "Facebook 群组帖子通常需要登录才能查看，本插件不使用登录态，暂不支持。"}

    fetch_url = _fb_fetch_url(ref, url)
    fetched = await _fetch_meta_html(fetch_url, _META_UA, timeout=40.0)
    kind = fetched["kind"]
    page_data: Optional[Dict[str, Any]] = None
    if kind == "ok":
        page_data = await asyncio.to_thread(_fb_parse_page, fetched["text"])
        if page_data.get("source") == "og-only":
            title = str(_meta_og(fetched["text"], "og:title") or "")
            if re.search(r"log in|log into|sign up|ログイン|登录|登入", title, re.IGNORECASE) or not (page_data.get("text") or page_data["items"]):
                page_data = None
                kind = "login" if title else "missing"
        elif not (page_data.get("text") or page_data["items"] or page_data.get("source")):
            page_data = None
            kind = "missing"
    final_url = fetched.get("url") or fetch_url
    if page_data is None:
        plugin.logger.info(f"Facebook 主页面不可用（{kind}），回退 plugins/post.php：{fetch_url}")
        page_data = await _fb_resolve_fallbacks(ref, final_url if kind != "login" else fetch_url)
        if page_data is None:
            if kind == "login":
                return {"ok": False, "platform": "facebook", "error": _fb_login_message()}
            if kind == "missing":
                return {"ok": False, "platform": "facebook", "error": "没有找到这条 Facebook 内容：链接可能已失效、帖子已删除，或不是公开的。"}
            return {
                "ok": False,
                "platform": "facebook",
                "fallback": True,
                "error": f"Facebook 暂时没有给出这条内容的数据（{fetched.get('error') or kind}）（也可能是帖子已删除、或 Facebook 限流了这个 IP，稍后再试）。",
            }
    canonical = final_url if ref["kind"] != "share" else (page_data.get("permalink") or final_url)
    resolved = await _fb_build_resolved(page_data, canonical)
    if not resolved["videos"] and not resolved["image_list"] and not resolved["title"] and not resolved["link_attachment"]:
        return {"ok": False, "platform": "facebook", "fallback": True, "error": "Facebook 返回的页面里没有可用的文字、图片或视频。"}
    return resolved


def _format_fb_caption(resolved: Dict[str, Any], kind: str, with_comments: bool = False) -> str:
    author = str(resolved.get("author") or "").strip()
    when = str(resolved.get("created_at_beijing") or "")
    meta = [part for part in (author, f"{when}（北京时间）" if when else "") if part]
    lines = [f"Facebook 帖子 · {kind}"]
    if meta:
        lines.append(" · ".join(meta))
    stats = []
    for key, label in (("likes", "反应"), ("replies", "评论"), ("shares", "分享"), ("views", "观看")):
        if resolved.get(key) is not None:
            stats.append(f"{label} {resolved[key]}")
    if stats:
        lines.append(" · ".join(stats))
    body = str(resolved.get("title") or "").strip()
    if body:
        lines.append("")
        lines.append(_meta_trim(body))
        if len(body) > _META_MAX_TEXT_CHARS:
            lines.append("（正文过长，已截断，完整内容请看原帖）")
        elif resolved.get("text_truncated"):
            lines.append("（只取到了分享预览里的文字，可能不完整，完整内容请看原帖）")
    link = resolved.get("link_attachment") or None
    if link and (link.get("title") or link.get("url")):
        lines.append("")
        lines.append("🔗 " + " ".join(part for part in (str(link.get("title") or ""), str(link.get("url") or "")) if part))
    if with_comments and resolved.get("comments_sample"):
        lines.append("")
        lines.append(f"—— 前 {len(resolved['comments_sample'])} 条评论 ——")
        for comment in resolved["comments_sample"]:
            text = str(comment.get("text") or "").replace("\n", " ").strip()
            lines.append(f"{comment.get('author') or '匿名'}：{text[:200]}")
    if resolved.get("note"):
        lines.append("")
        lines.append(str(resolved["note"]))
    lines.append("")
    lines.append(f"链接：{resolved.get('canonical_url') or ''}")
    return "\n".join(lines)


async def _download_fb_dash(entry: Dict[str, Any], task_dir: Path, max_bytes: int) -> Path:
    """DASH 备用路径：下视频流 + 音频流，再用 ffmpeg 无损合并（沿用 B 站的 ffmpeg 调用方式）。"""
    video_url = str(entry.get("dash_video_url") or "")
    audio_url = str(entry.get("dash_audio_url") or "")
    if not video_url:
        raise RuntimeError("Facebook 没有给出可用的视频地址")
    headers = {"User-Agent": _META_UA, "Referer": _DOWNLOAD_REFERER["facebook"], "Accept": "*/*"}
    proxy = _meta_proxy("facebook")
    video_file = await _download_direct(video_url, task_dir / "video.m4s", max_bytes, headers, proxy)
    if not audio_url:
        return video_file  # 没有音轨的视频（动图类）直接发
    ffmpeg = _bilibili_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("没有可用的 ffmpeg（无法合并 DASH 音视频）")
    audio_file = await _download_direct(audio_url, task_dir / "audio.m4s", max_bytes, headers, proxy)
    if video_file.stat().st_size + audio_file.stat().st_size > max_bytes:
        raise RuntimeError(f"Facebook 视频合并前总体积超过 {_human_size(max_bytes)} 上限，未发送。")
    output = task_dir / "video.mp4"
    process = await asyncio.create_subprocess_exec(
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video_file), "-i", str(audio_file),
        "-c", "copy", "-movflags", "+faststart", str(output),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=300)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        raise RuntimeError("ffmpeg 合并超时（视频过大）") from None
    if process.returncode != 0 or not output.exists() or output.stat().st_size == 0:
        detail = (stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(f"ffmpeg 合并失败：{detail[-1][:200] if detail else '未知错误'}")
    return output


async def _download_meta_video(
    entry: Dict[str, Any], platform: str, resolved: Dict[str, Any], task_dir: Path, max_bytes: int
) -> "_MediaSource":
    """下载一个视频：优先单文件直链；Facebook 直链失败或没有时再走 DASH + ffmpeg。"""
    task_dir.mkdir(parents=True, exist_ok=True)
    error: Optional[Exception] = None
    if entry.get("url"):
        try:
            return await _download_resolved({**resolved, "videos": [entry]}, platform, task_dir, max_bytes)
        except Exception as exc:  # noqa: BLE001 - FB 还有 DASH 可以试
            error = exc
            if platform != "facebook" or not entry.get("dash_video_url"):
                raise
            plugin.logger.warning(f"Facebook 单文件视频下载失败，改走 DASH：{exc}")
    if platform == "facebook" and entry.get("dash_video_url"):
        try:
            merged = await _download_fb_dash(entry, task_dir, max_bytes)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Facebook 视频下载失败：{error or ''}{exc}") from exc
        return _MediaSource(name=f"Facebook-{entry.get('id') or resolved.get('note_id') or int(time.time())}.mp4", path=merged, size=merged.stat().st_size)
    raise RuntimeError(f"{_PLATFORM_NAMES.get(platform, platform)}没有可用的视频直链")


async def _obtain_meta_post(
    resolved: Dict[str, Any], platform: str, task_dir: Path, limit: int, formatter: Any
) -> "_MediaSource":
    """IG / FB 的公共收尾：挑图片（受 max_images 限制）、下载视频（最多 _META_MAX_VIDEOS 个）、拼文字节点。"""
    config = plugin.get_config()
    images = [str(item) for item in (resolved.get("image_list") or []) if item]
    max_images = max(1, int(getattr(config, "max_images", 9) or 9))
    picked_images = images[:max_images]
    videos = [item for item in (resolved.get("videos") or []) if isinstance(item, dict)][:_META_MAX_VIDEOS]
    total_videos = max(len(resolved.get("videos") or []), int(resolved.get("video_total") or 0))
    kind = _meta_post_kind({**resolved, "videos": videos[:total_videos]}, len(picked_images))
    if platform == "facebook":
        caption = formatter(resolved, kind, bool(getattr(config, "facebook_send_comments", False)))
    else:
        caption = formatter(resolved, kind)
    if len(images) > len(picked_images):
        caption += f"\n（共 {len(images)} 张图片，仅发送前 {len(picked_images)} 张）"
    if total_videos > len(videos):
        caption += f"\n（共 {total_videos} 个视频，仅发送前 {len(videos)} 个）"
    name_prefix = _PLATFORM_NAMES.get(platform, platform)
    source = _MediaSource(name=f"{name_prefix}-{resolved.get('note_id') or int(time.time())}", caption=caption)
    if picked_images:
        source.images = tuple(picked_images)
        source.image_fallbacks = {
            key: value for key, value in (resolved.get("image_fallbacks") or {}).items() if key in set(picked_images)
        }
        plugin.logger.info(f"{name_prefix}图片：共 {len(images)} 张，本次发送 {len(picked_images)} 张")
    downloaded: List["_MediaSource"] = []
    for index, entry in enumerate(videos):
        downloaded.append(await _download_meta_video(entry, platform, resolved, task_dir / f"video-{index + 1}", limit))
    if downloaded:
        source.name = downloaded[0].name
        source.path = downloaded[0].path
        source.size = downloaded[0].size
        source.extra_videos = tuple((item.path, item.name) for item in downloaded[1:])
    return source


async def _obtain_facebook(url: str, task_dir: Path) -> "_MediaSource":
    """Facebook 帖子 → 可发送内容：视频（单文件直链优先，DASH + ffmpeg 备用）、图片组、文字。
    暂时性失败时回退 yt-dlp（只能拿视频，没有正文）。"""
    config = plugin.get_config()
    limit = int(getattr(config, "facebook_max_size_mb", 100) or 100) * 1024 * 1024
    try:
        resolved = await _resolve_facebook(url)
    except Exception as error:  # noqa: BLE001
        plugin.logger.warning(f"Facebook 解析异常：{error}")
        resolved = {"ok": False, "platform": "facebook", "fallback": True, "error": f"Facebook 解析失败：{error}"}
    if resolved.get("ok"):
        return await _obtain_meta_post(resolved, "facebook", task_dir, limit, _format_fb_caption)
    message = str(resolved.get("error") or "Facebook 解析失败")
    if not resolved.get("fallback"):
        raise RuntimeError(message)
    plugin.logger.warning(f"{message} 回退 yt-dlp")
    try:
        video, name = await _run_ytdlp(url, task_dir, _meta_proxy("facebook"))
    except Exception as error:  # noqa: BLE001 - 两边的原因一起报给用户
        raise RuntimeError(f"{message}yt-dlp 回退也失败：{error}") from error
    return _MediaSource(name=name, path=video, size=video.stat().st_size)


async def _obtain_video(url: str, task_dir: Path) -> "_MediaSource":
    """拿到可发送的视频。

    - 抖音 / 小红书：本地解析器取直链 → 服务器流式下载 → 走临时链接
    - TikTok：大陆直连被墙、云端完全不可达，所以由**本机**解析器走代理下好文件，
      这里只拿到一个本机路径，直接交给同机的 NapCat 上传（不经过服务器）
    - X：公开 syndication 接口取正文 / 图片 / 视频直链（见 _obtain_x，接口不可用时回退 yt-dlp）
    - Instagram / Facebook：匿名读公开帖子页（见 _obtain_instagram / _obtain_facebook，失败时回退 yt-dlp / oEmbed）
    - 其它平台：yt-dlp 下载

    只有在**解析**这一步不可用（解析器离线 / 超时 / 解析失败）时，小红书才回退 yt-dlp；
    直链下载失败会直接报错，不去做无意义的回退。抖音、TikTok 没有回退路径。
    """
    config = plugin.get_config()
    max_bytes = int(config.max_file_size_mb) * 1024 * 1024
    platform = _platform_of(url)

    # X：公开嵌入接口，不需要本地解析器
    if _is_x(url):
        return await _obtain_x(url, task_dir)

    # Instagram / Facebook：匿名读公开帖子页，不需要本地解析器
    if _is_instagram(url):
        return await _obtain_instagram(url, task_dir)
    if _is_facebook(url):
        return await _obtain_facebook(url, task_dir)

    # B 站：走公开 API；有 ffmpeg 时用 DASH 拿 1080p/4K 并合并，否则退回单文件 mp4（匿名 720p）
    if _is_bilibili(url):
        bili_limit = int(getattr(config, "bilibili_max_size_mb", 300) or 300) * 1024 * 1024
        resolved_bili = await _resolve_bilibili(url)
        if not resolved_bili.get("ok"):
            raise RuntimeError(str(resolved_bili.get("error") or "B站解析失败"))
        if resolved_bili.get("mode") == "dash":
            try:
                dash_path, dash_name = await _download_bilibili_dash(
                    resolved_bili, task_dir, bili_limit
                )
                return _MediaSource(
                    name=dash_name, path=dash_path, size=dash_path.stat().st_size
                )
            except Exception as error:  # noqa: BLE001 - DASH 失败就退回 720p mp4
                plugin.logger.warning(f"B站 DASH 合并失败，回退单文件 mp4：{error}")
                resolved_bili = await _resolve_bilibili(url, force_mp4=True)
                if not resolved_bili.get("ok"):
                    raise RuntimeError(
                        str(resolved_bili.get("error") or "B站解析失败")
                    ) from error
        picked = _pick_stream(resolved_bili.get("videos") or [], bili_limit)
        if not picked:
            raise RuntimeError("B站没有返回可用的视频直链。")
        if picked.get("size") and int(picked["size"]) > bili_limit:
            raise RuntimeError(
                f"B站视频 {_human_size(picked['size'])} 超过 {_human_size(bili_limit)} 上限，未发送。"
                "可在插件配置里调大「B站视频体积上限」。"
            )
        return await _download_resolved(resolved_bili, "bilibili", task_dir, bili_limit)

    if platform and bool(config.resolver_enabled):
        resolved: Optional[Dict[str, Any]] = None
        if not _resolver_token():
            if platform in ("douyin", "tiktok"):
                raise RuntimeError(f"{_source_name(url)}解析需要配置「本地解析器通信密钥」，请在插件配置里填写。")
            plugin.logger.warning("未配置解析器密钥，小红书回退 yt-dlp")
        elif not _resolver_online():
            if platform in ("douyin", "tiktok"):
                raise RuntimeError(
                    "本地解析器未连接：请在 Windows 电脑上运行 media-resolver（见插件 README）。"
                    f"{_source_name(url)}无法用其它方式解析。"
                )
            plugin.logger.warning("本地解析器未连接，小红书回退 yt-dlp")
        else:
            try:
                resolved = await _resolve_via_local(url)
            except Exception as error:  # noqa: BLE001 - 超时等情况
                if platform in ("douyin", "tiktok"):
                    raise
                plugin.logger.warning(f"本地解析器超时，小红书回退 yt-dlp：{error}")

        if resolved is not None:
            if not resolved.get("ok"):
                message = str(resolved.get("error") or "本地解析器解析失败")
                if platform in ("douyin", "tiktok"):
                    raise RuntimeError(message)
                plugin.logger.warning(f"小红书解析失败，回退 yt-dlp：{message}")
            elif (resolved.get("image_list") or []) and not (resolved.get("videos") or []):
                # 图文笔记：解析器返回的是图片直链列表，没有视频流
                images = [str(item) for item in (resolved.get("image_list") or []) if item]
                limit = max(1, int(getattr(config, "max_images", 9) or 9))
                picked_images = images[:limit]
                plugin.logger.info(
                    f"{_source_name(url)}图文笔记：共 {len(images)} 张，本次发送 {len(picked_images)} 张"
                )
                if not picked_images:
                    raise RuntimeError(f"{_source_name(url)}图文笔记没有可用的图片直链")
                return _MediaSource(
                    name=(
                        f"{_PLATFORM_NAMES.get(platform or '', platform or 'media')}-"
                        f"{resolved.get('note_id') or resolved.get('aweme_id') or int(time.time())}"
                    ),
                    images=tuple(picked_images),
                )
            elif resolved.get("mode") == "local_file" and resolved.get("local_path"):
                # 解析器已在它那一侧下好文件（TikTok）。若它顺手把文件上传到了服务器
                # （NapCat 上云后必需），就用服务器侧文件走临时链接交付；
                # 否则退回旧行为：把本机路径交给同机的 NapCat。
                uploaded = _take_upload(str(resolved.get("id") or ""))
                if uploaded is not None:
                    size = uploaded.stat().st_size
                    plugin.logger.info(
                        f"使用解析器上传的文件：{uploaded.name} {_human_size(size)}"
                        f"（{(resolved.get('title') or '')[:40]}）"
                    )
                    return _MediaSource(name=uploaded.name, path=uploaded, size=size)
                local_path = str(resolved.get("local_path")).strip()
                if not _looks_like_local_path(local_path):
                    raise RuntimeError(f"本地解析器返回的文件路径不可用：{local_path[:120]}")
                allow_local = bool(getattr(config, "allow_resolver_local_path", False))
                if re.match(r"^[A-Za-z]:[\\/]", local_path) and os.name != "nt" and not allow_local:
                    # 服务器不是 Windows，却收到 Windows 本机路径：说明 NapCat 已不在解析器同一台机器上，
                    # 而解析器又没有上传文件（旧版行为）。直接给出可操作的错误，别把路径丢给 NapCat 让它失败。
                    # 若 NapCat 其实与解析器同机（家里跑 QQ/NapCat 的情况），打开配置项
                    # allow_resolver_local_path 即可按本机路径交付。
                    raise RuntimeError(
                        "TikTok 需要解析器把文件上传到服务器（NapCat 已不在解析器同一台机器上），"
                        "但本次没有收到上传。请把 media-resolver 更新到带上传能力的版本并重启它；"
                        "若 NapCat 与解析器其实是同一台机器，请在插件配置里打开「允许直接用解析器的本机路径」。"
                        f"当前收到的是本机路径：{local_path[:80]}"
                    )
                name = Path(local_path).name or "video.mp4"
                plugin.logger.info(
                    f"使用本机文件：{local_path}（{(resolved.get('title') or '')[:40]} "
                    f"{_human_size(resolved.get('size'))}）"
                )
                return _MediaSource(name=name, local_path=local_path, size=int(resolved.get("size") or 0))
            else:
                return await _download_resolved(resolved, platform, task_dir, max_bytes)

    video, name = await _run_ytdlp(url, task_dir)
    return _MediaSource(name=name, path=video, size=video.stat().st_size)


async def _download_resolved(
    resolved: Dict[str, Any], platform: str, task_dir: Path, max_bytes: int
) -> "_MediaSource":
    """按体积上限挑一条直链并下载（这里失败就直接报错，不回退）。"""
    picked = _pick_stream(resolved.get("videos") or [], max_bytes)
    if not picked:
        raise RuntimeError("本地解析器没有返回可用的视频直链")
    if picked.get("size") and int(picked["size"]) > max_bytes:
        raise RuntimeError(
            f"该视频最小体积 {_human_size(picked['size'])} 仍超过 {_human_size(max_bytes)} 上限，未发送。"
        )

    headers = {
        "User-Agent": _download_ua(platform),
        "Referer": _DOWNLOAD_REFERER.get(platform, "https://www.douyin.com/"),
        "Accept": "*/*",
    }
    source_name = _PLATFORM_NAMES.get(platform, platform)
    plugin.logger.info(
        f"{source_name}直链下载：{(resolved.get('title') or '')[:40]} 清晰度={picked.get('label')} "
        f"体积={_human_size(picked.get('size'))}"
    )
    try:
        video = await _download_direct(
            str(picked["url"]), task_dir / "video.mp4", max_bytes, headers, _meta_proxy(platform)
        )
    except Exception as error:  # noqa: BLE001 - 直链过期 / CDN 拒绝时给出可读提示
        raise RuntimeError(f"{source_name}直链下载失败：{error}") from error
    label = str(picked.get("label") or platform)
    media_id = resolved.get("aweme_id") or resolved.get("note_id") or int(time.time())
    return _MediaSource(name=f"{label}-{media_id}.mp4", path=video, size=video.stat().st_size)


# ---------------------------------------------------------------- 发送


def _lease_url(video: Path, name: str) -> str:
    _cleanup_expired()
    token = secrets.token_urlsafe(32)
    expires_at = time.time() + int(plugin.get_config().expiry_minutes) * 60
    _LEASES[token] = _Lease(path=video, expires_at=expires_at, download_name=name)
    base = plugin.get_config().public_base_url.rstrip("/")
    return f"{base}/plugins/local_video.video_forward/media/{token}"


def _target(ctx: AgentCtx) -> tuple[str, int]:
    match = re.search(r"onebot_v11-(group|private)_(\d+)$", str(ctx.chat_key))
    if not match:
        raise RuntimeError("当前聊天不是可识别的 QQ 私聊或群聊。")
    return match.group(1), int(match.group(2))


async def _send_forward(
    ctx: AgentCtx,
    file_ref: Optional[str],
    name: str,
    source_url: str,
    images: Optional[list] = None,
    caption: str = "",
    extra_videos: Optional[list] = None,
) -> None:
    """把视频或图文以合并转发发出。

    ``file_ref`` 既可以是服务器上的临时链接（http URL），
    也可以是 NapCat 所在机器上的本地文件路径（TikTok 用这种）。
    ``images`` 非空时改为发图片组（图文笔记），每个节点放一张图。
    ``caption`` 非空时作为最前面的文字节点（X 帖子正文等），替代默认的「平台 + 链接」抬头；
    图片和视频都有时（混合帖子）先发图片再发视频（``extra_videos`` 是同帖的其它视频）；两者都没有就只发文字（纯文字帖子）。
    """
    kind, target = _target(ctx)
    source = _source_name(source_url)
    if caption:
        content: list = [{"type": "text", "data": {"text": caption}}]
        for reference in images or []:
            content.append({"type": "image", "data": {"file": reference}})
        if file_ref:
            content.append({"type": "video", "data": {"file": file_ref, "name": name}})
        # 同一条帖子里的其它视频（Instagram 轮播 / Facebook 相册）：(临时链接, 文件名)
        for extra_ref, extra_name in extra_videos or []:
            content.append({"type": "video", "data": {"file": extra_ref, "name": extra_name}})
    elif images:
        content = [{
            "type": "text",
            "data": {"text": f"{source} 图文（{len(images)} 张）\n链接：{source_url}"},
        }]
        for reference in images:
            content.append({"type": "image", "data": {"file": reference}})
    else:
        content = [
            {"type": "text", "data": {"text": f"{source} 视频\n链接：{source_url}"}},
            {"type": "video", "data": {"file": file_ref, "name": name}},
        ]
    bot = get_bot()
    messages = [{
        "type": "node",
        "data": {
            # 合并转发节点必须挂在机器人自己的账号下，这里取 bot.self_id 而不是硬编码
            "user_id": str(getattr(bot, "self_id", "") or ""),
            "nickname": str(plugin.get_config().forward_nickname or "机器人"),
            "content": content,
        },
    }]
    # NapCat 需要先把视频上传到 QQ 服务器，几十 MB 的视频往往要一两分钟；
    # nonebot 的 OneBot 适配器默认 api_timeout 只有 30 秒，这里按调用放宽。
    # （适配器实现：timeout = data.get("_timeout", self.config.api_timeout)）
    call_timeout = int(plugin.get_config().send_timeout)
    if kind == "group":
        await bot.call_api(
            "send_group_forward_msg", group_id=target, messages=messages, _timeout=call_timeout
        )
    else:
        await bot.call_api(
            "send_private_forward_msg", user_id=target, messages=messages, _timeout=call_timeout
        )


async def _process(ctx: AgentCtx, url: str) -> str:
    if ctx.adapter_key != "onebot_v11":
        return "这个功能目前只支持 QQ / OneBot。"
    key = (str(ctx.chat_key), url)
    now = time.time()
    if key in _IN_FLIGHT:
        return "这条链接正在处理中，请稍候。"
    if _RECENT_URLS.get(key, 0) > now:
        return "这条链接刚处理过，未重复发送。"
    last = _LAST_CHAT_REQUEST.get(str(ctx.chat_key), 0)
    if now - last < int(plugin.get_config().cooldown_seconds):
        return "请稍等几秒再发下一条视频链接。"

    _IN_FLIGHT.add(key)
    _LAST_CHAT_REQUEST[str(ctx.chat_key)] = now
    _VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    task_dir = _VIDEO_DIR / secrets.token_urlsafe(12)
    task_dir.mkdir(parents=True, exist_ok=True)
    try:
        await ctx.send_text(f"正在解析{_source_name(url)}内容，请稍候…", record=False)
        source = await _obtain_video(url, task_dir)
        if source.images:
            # 图文笔记：逐张下载到服务器，再用临时链接合成一条合并转发
            referer = _DOWNLOAD_REFERER.get(
                _download_platform(url) or "xiaohongshu", "https://www.xiaohongshu.com/"
            )
            headers = {"User-Agent": _download_ua(_download_platform(url)), "Referer": referer, "Accept": "*/*"}
            max_bytes = int(plugin.get_config().max_file_size_mb) * 1024 * 1024
            meta_platform = _is_instagram(url) or _is_facebook(url)
            image_proxy = _meta_proxy(_download_platform(url))
            refs: list = []
            for index, image_url in enumerate(source.images, start=1):
                try:
                    image_path = await _download_direct(
                        image_url, task_dir / f"image-{index:02d}.jpg", max_bytes, headers, image_proxy
                    )
                except Exception as error:  # noqa: BLE001 - 有备用链接（缩放参数 / 小图）时先试备用
                    backup = (source.image_fallbacks or {}).get(image_url)
                    if not backup:
                        raise
                    plugin.logger.warning(f"图片 {index} 下载失败（{error}），改用备用链接")
                    image_path = await _download_direct(
                        backup, task_dir / f"image-{index:02d}.jpg", max_bytes, headers, image_proxy
                    )
                image_name = f"{source.name}-{index:02d}.jpg"
                if meta_platform:
                    # Instagram / Facebook 的 CDN 可能给 webp / png：按文件头改成真实扩展名
                    with image_path.open("rb") as handle:
                        extension = _meta_sniff_ext(handle.read(16))
                    if extension != ".jpg":
                        image_path = image_path.rename(image_path.with_suffix(extension))
                        image_name = f"{source.name}-{index:02d}{extension}"
                refs.append(_lease_url(image_path, image_name))
            plugin.logger.info(f"{_source_name(url)}图文发送：{len(refs)} 张（{source.name}）")
            # 混合帖子（图片 + 视频 / 动图）：视频也交给同一条合并转发
            video_ref = _lease_url(source.path, source.name) if source.path else None
            extra_refs = [(_lease_url(path, name), name) for path, name in source.extra_videos]
            await _send_forward(
                ctx, video_ref, source.name, url, images=refs, caption=source.caption,
                extra_videos=extra_refs,
            )
            _RECENT_URLS[key] = time.time() + 30 * 60
            if video_ref:
                return f"图文和视频已通过合并转发发回原聊天（{len(refs)} 张图片）。"
            return f"图文已通过合并转发发回原聊天（{len(refs)} 张图片）。"
        if source.caption and not source.path and not source.local_path:
            # 纯文字帖子（X）：只有文字节点
            await _send_forward(ctx, None, source.name, url, caption=source.caption)
            _RECENT_URLS[key] = time.time() + 30 * 60
            shutil.rmtree(task_dir, ignore_errors=True)
            return "帖子文字已通过合并转发发回原聊天。"
        if source.local_path:
            # 文件就在 NapCat 那台机器上（TikTok），直接把路径交给它上传，服务器不中转
            file_ref = source.local_path
            # 这条路径没用到服务器临时目录，顺手回收
            shutil.rmtree(task_dir, ignore_errors=True)
        else:
            if source.path is None:
                raise RuntimeError("没有拿到可发送的视频文件")
            file_ref = _lease_url(source.path, source.name)
        extra_refs = [(_lease_url(path, name), name) for path, name in source.extra_videos]
        await _send_forward(
            ctx, file_ref, source.name, url, caption=source.caption, extra_videos=extra_refs
        )
        _RECENT_URLS[key] = time.time() + 30 * 60
        return "视频已通过合并转发发回原聊天。"
    except Exception as error:
        # 失败时立刻回收临时目录；成功时文件由 lease 指向，交给过期清理。
        shutil.rmtree(task_dir, ignore_errors=True)
        plugin.logger.exception("短视频转发失败")
        return f"视频未能发送：{error}"
    finally:
        _IN_FLIGHT.discard(key)


@plugin.mount_on_user_message()
async def auto_forward_video(ctx: AgentCtx, message: ChatMessage) -> MsgSignal | None:
    """收到受支持站点的公开链接时自动处理，不再额外调用 AI。"""
    url = _find_message_url(message)
    if not url:
        return None
    result = await _process(ctx, url)
    if result.startswith("视频未能") or result.startswith("请稍等") or result.startswith("这条链接"):
        await ctx.send_text(result, record=False)
    return MsgSignal.BLOCK_TRIGGER


@plugin.mount_sandbox_method(
    SandboxMethodType.BEHAVIOR,
    name="解析并合并转发短视频",
    description="下载公开的抖音、小红书、TikTok、B站视频，以及 X (Twitter) / Instagram / Facebook 帖子（文字 + 图片 / 视频）链接，并以 QQ 合并转发发送到当前聊天。不支持付费、私密或受保护内容。",
)
async def forward_video(ctx: AgentCtx, url: str) -> str:
    """当用户明确要求解析短视频时调用；参数必须是公开的抖音、小红书、TikTok、B站，或 X / Instagram / Facebook 帖子链接。"""
    candidate = _find_supported_url(url)
    if not candidate:
        return "只支持公开的抖音、小红书、TikTok、B站 视频和 X / Instagram / Facebook 帖子链接。"
    return await _process(ctx, candidate)


# ---------------------------------------------------------------- 路由


class ResolverResult(BaseModel):
    """本地解析器回传的结果。

    注意：这里必须显式声明或用 ``extra="allow"``，否则 Pydantic 会静默丢掉
    解析器新增的字段（例如 TikTok 的 ``mode`` / ``local_path``）。
    """

    model_config = ConfigDict(extra="allow")

    id: str
    ok: bool = False
    error: Optional[str] = None
    platform: Optional[str] = None
    # direct_url：解析器只回传直链，由服务器下载
    # local_file：解析器已经在本机把文件下好了（TikTok），服务器直接用这个路径
    mode: Optional[str] = None
    local_path: Optional[str] = None
    size: Optional[int] = None
    aweme_id: Optional[str] = None
    note_id: Optional[str] = None
    media_id: Optional[str] = None
    title: Optional[str] = None
    cover: Optional[str] = None
    duration_ms: Optional[int] = None
    elapsed_ms: Optional[int] = None
    videos: List[Dict[str, Any]] = Field(default_factory=list)


@plugin.mount_router()
def create_router() -> APIRouter:
    """Expose media leases for NapCat plus the local resolver job API."""
    router = APIRouter()

    @router.get("/")
    async def status():
        """Small deployment check; it exposes no configuration or media."""
        return {
            "name": "video_forward",
            "version": "0.11.0",
            "supported": ["douyin", "xiaohongshu", "tiktok", "bilibili", "x", "instagram", "facebook"],
            "bilibili": {
                "cookie": bool(
                    str(getattr(plugin.get_config(), "bilibili_cookie", "") or "").strip()
                ),
                "ffmpeg": bool(_bilibili_ffmpeg()),
                "quality_hint": (
                    "cookie 已配置：DASH 可达 1080p+/4K（走 ffmpeg 合并）"
                    if str(getattr(plugin.get_config(), "bilibili_cookie", "") or "").strip()
                    else "cookie 为空：匿名只能 720p（单文件 mp4）"
                ),
            },
            "x": {
                "enabled": _x_enabled(),
                "source": "cdn.syndication.twimg.com（公开嵌入接口，无需登录 / token）",
                "max_size_mb": int(getattr(plugin.get_config(), "x_max_size_mb", 100) or 100),
                "send_text": bool(getattr(plugin.get_config(), "x_send_text", True)),
            },
            "instagram": {
                "enabled": _meta_enabled("instagram_enabled"),
                "source": "instagram.com 公开帖子页（匿名，非官方接口，无需登录 / token）",
                "max_size_mb": int(getattr(plugin.get_config(), "instagram_max_size_mb", 100) or 100),
                "fallback_ua": bool(getattr(plugin.get_config(), "instagram_fallback_ua", True)),
                "proxy_configured": bool(_meta_proxy("instagram")),
            },
            "facebook": {
                "enabled": _meta_enabled("facebook_enabled"),
                "source": "facebook.com 公开帖子页（匿名，非官方接口，无需登录 / token）",
                "max_size_mb": int(getattr(plugin.get_config(), "facebook_max_size_mb", 100) or 100),
                "send_comments": bool(getattr(plugin.get_config(), "facebook_send_comments", False)),
                "proxy_configured": bool(_meta_proxy("facebook")),
                "ffmpeg": bool(_bilibili_ffmpeg()),
            },
            "resolver": {
                "enabled": bool(plugin.get_config().resolver_enabled),
                "configured": bool(_resolver_token()),
                "online": _resolver_online(),
                "last_poll_seconds_ago": round(time.time() - _LAST_POLL, 1) if _LAST_POLL else None,
                "pending_jobs": len(_JOBS),
                "stats": dict(_STATS),
            },
        }

    @router.get("/resolver/poll")
    async def resolver_poll(token: str = "", wait: int = 20):
        """本地解析器轮询取任务（长轮询，最多等 wait 秒）。"""
        global _LAST_POLL
        if not _resolver_authorized(token):
            _STATS["poll_denied"] = _STATS.get("poll_denied", 0) + 1
            raise HTTPException(status_code=403, detail="解析器密钥不正确")
        _LAST_POLL = time.time()
        _STATS["poll"] += 1
        deadline = time.time() + max(0, min(int(wait or 0), 25))
        while True:
            job = _next_pending_job()
            if job is not None:
                return {"job": {"id": job.id, "url": job.url, "created_at": job.created_at}}
            remaining = deadline - time.time()
            if remaining <= 0:
                return {"job": None}
            waiter: "asyncio.Future[None]" = asyncio.get_running_loop().create_future()
            _WAITERS.add(waiter)
            try:
                await asyncio.wait_for(waiter, timeout=remaining)
            except asyncio.TimeoutError:
                return {"job": None}
            finally:
                _WAITERS.discard(waiter)

    @router.post("/resolver/result")
    async def resolver_result(payload: ResolverResult, token: str = ""):
        """本地解析器回传解析结果。"""
        global _LAST_POLL
        if not _resolver_authorized(token):
            raise HTTPException(status_code=403, detail="解析器密钥不正确")
        _LAST_POLL = time.time()
        job = _JOBS.get(payload.id)
        if job is None:
            return {"ok": False, "detail": "任务不存在或已超时"}
        if not job.future.done():
            job.future.set_result(payload.model_dump())
        _STATS["ok" if payload.ok else "failed"] += 1
        return {"ok": True}

    @router.get("/resolver/self-test")
    async def resolver_self_test(token: str = "", url: str = "", download: bool = False):
        """自检：让本地解析器解析一个抖音 / 小红书链接（可选顺带下载验证 CDN 是否可用）。

        浏览器直接打开也能用，确认本地解析器是否真的工作：
        ``http://<服务器>:8022/plugins/local_video.video_forward/resolver/self-test?token=<密钥>&url=<链接>&download=true``
        """
        if not _resolver_authorized(token):
            raise HTTPException(status_code=403, detail="解析器密钥不正确")
        target_url = (url or "").strip() or "https://www.douyin.com/video/7684314365076960202"
        started = time.time()
        if _is_bilibili(target_url):
            # B 站不需要本地解析器：走公开 API 拿单文件 mp4 直链
            try:
                resolved = await _resolve_bilibili(target_url)
            except Exception as error:  # noqa: BLE001 - 自检需要看到原始错误
                return {"ok": False, "error": str(error)}
        elif _is_x(target_url):
            # X 同样不需要本地解析器：走公开 syndication 接口
            try:
                resolved = await _resolve_x(target_url)
            except Exception as error:  # noqa: BLE001 - 自检需要看到原始错误
                return {"ok": False, "error": str(error)}
        elif _is_instagram(target_url):
            # Instagram：匿名读公开帖子页（不需要本地解析器）
            try:
                resolved = await _resolve_instagram(target_url)
            except Exception as error:  # noqa: BLE001 - 自检需要看到原始错误
                return {"ok": False, "error": str(error)}
        elif _is_facebook(target_url):
            # Facebook：同上
            try:
                resolved = await _resolve_facebook(target_url)
            except Exception as error:  # noqa: BLE001 - 自检需要看到原始错误
                return {"ok": False, "error": str(error)}
        else:
            if not _needs_local_resolver(target_url):
                return {"ok": False, "error": "自检只支持 抖音 / 小红书 / TikTok / B站 / X / Instagram / Facebook 链接"}
            if not _resolver_online():
                return {"ok": False, "error": "本地解析器未连接（没有收到心跳）"}
            try:
                resolved = await _resolve_via_local(target_url)
            except Exception as error:  # noqa: BLE001 - 自检需要看到原始错误
                return {"ok": False, "error": str(error)}
        report: Dict[str, Any] = {
            "ok": bool(resolved.get("ok")),
            "platform": resolved.get("platform") or _download_platform(target_url),
            "mode": resolved.get("mode") or "direct_url",
            "elapsed_seconds": round(time.time() - started, 1),
            "title": resolved.get("title"),
            "media_id": resolved.get("aweme_id") or resolved.get("note_id") or resolved.get("media_id"),
            "duration_seconds": round((resolved.get("duration_ms") or 0) / 1000, 1) or None,
            "video_count": len(resolved.get("videos") or []),
            "image_count": len(resolved.get("image_list") or []),
            "error": resolved.get("error"),
        }
        if resolved.get("mode") == "local_file":
            # 解析器已经在本机下好了文件，服务器不需要再下载
            report["local_path"] = resolved.get("local_path")
            report["local_size_mb"] = round((resolved.get("size") or 0) / 1048576, 2) or None
            job_id = str(resolved.get("id") or "")
            report["upload_received"] = job_id in _UPLOADS
            report["local_path_mode"] = (
                "hand-to-napcat"
                if bool(getattr(plugin.get_config(), "allow_resolver_local_path", False))
                else "needs-upload"
            )
            if not report["upload_received"]:
                if report["local_path_mode"] == "hand-to-napcat":
                    report["hint"] = (
                        "解析器没有上传文件，但已开启「允许直接用解析器的本机路径」，"
                        "因此该路径会直接交给同机的 NapCat（无需上传）。"
                    )
                else:
                    report["hint"] = (
                        "解析器没有上传文件（旧版行为）。NapCat 已不在解析器同一台机器时，"
                        "TikTok 需要解析器先上传：请把 media-resolver 更新到带上传能力的版本并重启它；"
                        "若两者其实是同一台机器，请在插件配置里打开「允许直接用解析器的本机路径」。"
                    )
            return report
        if download and resolved.get("ok") and resolved.get("platform") in ("instagram", "facebook"):
            # Instagram / Facebook：第一张图和第一个视频都真下载（直链会过期，所以解析完马上下）
            platform_key = str(resolved["platform"])
            limit_name = f"{platform_key}_max_size_mb"
            max_bytes = int(getattr(plugin.get_config(), limit_name, 100) or 100) * 1024 * 1024
            task_dir = _VIDEO_DIR / f"selftest-meta-{int(time.time())}"
            task_dir.mkdir(parents=True, exist_ok=True)
            headers = {
                "User-Agent": _download_ua(platform_key),
                "Referer": _DOWNLOAD_REFERER[platform_key],
                "Accept": "*/*",
            }
            test_proxy = _meta_proxy(platform_key)
            try:
                images = resolved.get("image_list") or []
                if images:
                    try:
                        image = await _download_direct(
                            str(images[0]), task_dir / "image-01.jpg", max_bytes, headers, test_proxy
                        )
                    except Exception:  # noqa: BLE001 - 与正式流程一致：有备用链接就试备用
                        backup = (resolved.get("image_fallbacks") or {}).get(images[0])
                        if not backup:
                            raise
                        image = await _download_direct(backup, task_dir / "image-01.jpg", max_bytes, headers, test_proxy)
                        report["first_image_used_fallback"] = True
                    report["first_image_mb"] = round(image.stat().st_size / 1048576, 2)
                if resolved.get("videos"):
                    downloaded = await _download_meta_video(
                        resolved["videos"][0], platform_key, resolved, task_dir / "video-1", max_bytes
                    )
                    report["downloaded_mb"] = round((downloaded.size or 0) / 1048576, 2)
            except Exception as error:  # noqa: BLE001 - 自检要把错误原样报出来
                report["ok"] = False
                report["error"] = f"下载失败：{error}"
            finally:
                shutil.rmtree(task_dir, ignore_errors=True)
            return report
        if download and resolved.get("ok") and (resolved.get("image_list") or []):
            # 图文笔记：只验证第一张能否下载，足以证明图片链路可用
            max_bytes = int(plugin.get_config().max_file_size_mb) * 1024 * 1024
            task_dir = _VIDEO_DIR / f"selftest-img-{int(time.time())}"
            task_dir.mkdir(parents=True, exist_ok=True)
            try:
                image = await _download_direct(
                    str((resolved.get("image_list") or [])[0]),
                    task_dir / "image-01.jpg",
                    max_bytes,
                    {
                        "User-Agent": _DOWNLOAD_UA,
                        "Referer": _DOWNLOAD_REFERER.get(
                            _download_platform(target_url) or "xiaohongshu", "https://www.xiaohongshu.com/"
                        ),
                    },
                )
                report["first_image_mb"] = round(image.stat().st_size / 1048576, 2)
            except Exception as error:  # noqa: BLE001 - 自检要把错误原样报出来
                report["ok"] = False
                report["error"] = f"图片下载失败：{error}"
            finally:
                shutil.rmtree(task_dir, ignore_errors=True)
            return report
        if download and resolved.get("ok") and resolved.get("mode") == "dash":
            # B 站 DASH：真下载两路流并用 ffmpeg 合并，才能证明 1080p 链路可用
            dash_limit = (
                int(getattr(plugin.get_config(), "bilibili_max_size_mb", 300) or 300)
                * 1024
                * 1024
            )
            task_dir = _VIDEO_DIR / f"selftest-dash-{int(time.time())}"
            task_dir.mkdir(parents=True, exist_ok=True)
            try:
                merged_path, _merged_name = await _download_bilibili_dash(
                    resolved, task_dir, dash_limit
                )
                report["downloaded_mb"] = round(merged_path.stat().st_size / 1048576, 2)
                report["merged"] = True
            except Exception as error:  # noqa: BLE001 - 自检要把错误原样报出来
                report["ok"] = False
                report["error"] = f"DASH 合并失败：{error}"
            finally:
                shutil.rmtree(task_dir, ignore_errors=True)
            return report
        if download and resolved.get("ok"):
            platform_key = str(resolved.get("platform") or _download_platform(target_url) or "douyin")
            max_bytes = int(plugin.get_config().max_file_size_mb) * 1024 * 1024
            if platform_key == "bilibili":
                max_bytes = (
                    int(getattr(plugin.get_config(), "bilibili_max_size_mb", 300) or 300)
                    * 1024
                    * 1024
                )
            if platform_key == "x":
                max_bytes = int(getattr(plugin.get_config(), "x_max_size_mb", 100) or 100) * 1024 * 1024
            picked = _pick_stream(resolved.get("videos") or [], max_bytes)
            if picked:
                task_dir = _VIDEO_DIR / f"selftest-{int(time.time())}"
                task_dir.mkdir(parents=True, exist_ok=True)
                try:
                    video = await _download_direct(
                        str(picked["url"]),
                        task_dir / "video.mp4",
                        max_bytes,
                        {
                            "User-Agent": _DOWNLOAD_UA,
                            "Referer": _DOWNLOAD_REFERER.get(
                                platform_key, "https://www.douyin.com/"
                            ),
                        },
                    )
                    report["downloaded_mb"] = round(video.stat().st_size / 1048576, 2)
                except Exception as error:  # noqa: BLE001 - 自检要把错误原样报出来
                    report["ok"] = False
                    report["error"] = f"直链下载失败：{error}"
                finally:
                    shutil.rmtree(task_dir, ignore_errors=True)
        return report

    @router.post("/resolver/upload")
    async def resolver_upload(request: Request, job: str = "", name: str = "", token: str = ""):
        """本地解析器把已下载好的文件推给服务器。

        QQ/NapCat 上云后不再与解析器同机，``local_path`` 无法使用，
        因此解析器在回报 ``mode=local_file`` 之前先调用本接口上传文件；
        插件随后用 ``/media/{token}`` 临时链接交付给 NapCat。
        """
        global _LAST_POLL
        if not _resolver_authorized(token):
            raise HTTPException(status_code=403, detail="解析器密钥不正确")
        _LAST_POLL = time.time()
        job_id = (job or "").strip()
        if not job_id:
            raise HTTPException(status_code=400, detail="缺少 job 参数")

        config = plugin.get_config()
        max_bytes = int(config.max_file_size_mb) * 1024 * 1024
        safe_name = Path(name or "video.mp4").name or "video.mp4"
        task_dir = _VIDEO_DIR / f"upload-{secrets.token_urlsafe(8)}"
        task_dir.mkdir(parents=True, exist_ok=True)
        target = task_dir / safe_name
        written = 0
        try:
            with target.open("wb") as handle:
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > max_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=f"文件超过 {config.max_file_size_mb}MB 上限，已中止接收",
                        )
                    handle.write(chunk)
        except HTTPException:
            shutil.rmtree(task_dir, ignore_errors=True)
            raise
        except Exception as error:  # noqa: BLE001 - 网络中断等
            shutil.rmtree(task_dir, ignore_errors=True)
            raise HTTPException(status_code=500, detail=f"接收上传失败：{error}") from error

        _cleanup_expired()
        _UPLOADS[job_id] = (target, time.time() + _UPLOAD_TTL_SECONDS)
        plugin.logger.info(f"解析器上传完成（job={job_id}）：{safe_name} {_human_size(written)}")
        return {"ok": True, "size": written, "name": safe_name}

    @router.get("/media/{token}")
    async def get_media(token: str):
        _cleanup_expired()
        lease = _LEASES.get(token)
        if lease is None or not lease.path.is_file():
            raise HTTPException(status_code=404, detail="视频已过期或不存在")
        media_type = mimetypes.guess_type(lease.download_name)[0] or "application/octet-stream"
        return FileResponse(
            lease.path,
            media_type=media_type,
            filename=lease.download_name,
            content_disposition_type="inline",
        )

    return router
