#!/usr/bin/env python3
"""X (Twitter) 帖子解析的自检脚本，不依赖 NekroAgent 运行环境。

做法：给 ``nekro_agent.*`` 和 ``nonebot`` 塞最小的假模块，再直接导入 plugin/plugin.py，
测试里调用的就是插件里真实的 _is_x / _x_status_id / _resolve_x / _obtain_x / _process。

运行（需要 httpx、pydantic、fastapi，这三个本来就是 NekroAgent 自带的）：

    pip install httpx pydantic fastapi        # 只在本机没有时
    python tests/test_x_resolver.py           # 全部（含真实联网）
    NMF_X_OFFLINE=1 python tests/test_x_resolver.py   # 只跑离线用例

联网用例只访问 X 的公开嵌入接口（cdn.syndication.twimg.com）和 twimg CDN，
不使用任何登录态 / token；下载的文件只写到临时目录，测完即删。
"""

from __future__ import annotations

import asyncio
import enum
import importlib.util
import logging
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any, List

ROOT = Path(__file__).resolve().parent.parent
OFFLINE = bool(os.environ.get("NMF_X_OFFLINE"))

# ---------------------------------------------------------------- 假的 NekroAgent / nonebot


def _install_stubs() -> None:
    from pydantic import BaseModel

    class _Plugin:
        def __init__(self, **_kwargs: Any) -> None:
            self.logger = logging.getLogger("nmf-test")
            self._config_cls: Any = None
            self.config_override: dict = {}

        def mount_config(self):
            def deco(cls):
                self._config_cls = cls
                return cls

            return deco

        def get_config(self):
            return self._config_cls(**self.config_override)

        def _identity(self, *_a: Any, **_k: Any):
            return lambda fn: fn

        mount_on_user_message = mount_router = _identity
        mount_sandbox_method = _identity

    class _SandboxMethodType(enum.Enum):
        BEHAVIOR = "behavior"

    class _MsgSignal(enum.Enum):
        BLOCK_TRIGGER = "block"

    def module(name: str, **attrs: Any) -> None:
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        sys.modules[name] = mod

    module("nonebot", get_bot=lambda: None)
    module("nekro_agent")
    module("nekro_agent.api")
    module("nekro_agent.api.message", ChatMessage=type("ChatMessage", (), {}))
    module(
        "nekro_agent.api.plugin",
        ConfigBase=BaseModel,
        NekroPlugin=_Plugin,
        SandboxMethodType=_SandboxMethodType,
    )
    module("nekro_agent.api.schemas", AgentCtx=type("AgentCtx", (), {}))
    module("nekro_agent.api.signal", MsgSignal=_MsgSignal)


def _load_plugin():
    _install_stubs()
    spec = importlib.util.spec_from_file_location("video_forward_plugin", ROOT / "plugin" / "plugin.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["video_forward_plugin"] = mod
    spec.loader.exec_module(mod)
    return mod


P = _load_plugin()

# 真实样例与公开帖子（都只用公开嵌入接口读取）
REAL_VIDEO_ID = "2114209839190618413"  # 视频帖「好吃！更好听！」
REAL_VIDEO_URL = f"https://x.com/xiaoyaodez51072/status/{REAL_VIDEO_ID}"
MULTI_PHOTO_ID = "2050301878521536874"  # @SpaceX 三张图
TEXT_ONLY_ID = "1585841080431321088"  # @elonmusk「the bird is freed」
TEXT_ONLY_ID_2 = "20"  # @jack 第一条
QUOTE_ID = "2098789109980332057"  # @elonmusk 引用帖（纯文字引用）
QUOTE_PHOTO_ID = "2089176557747413105"  # @elonmusk 引用了带图帖子
MISSING_ID = "99999999999999999"


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- (a) URL 识别 / ID 提取


class UrlParsing(unittest.TestCase):
    CASES = [
        ("https://x.com/xiaoyaodez51072/status/2114209839190618413", "2114209839190618413"),
        ("https://x.com/xiaoyaodez51072/status/2114209839190618413?s=46&t=abc", "2114209839190618413"),
        ("https://twitter.com/jack/status/20", "20"),
        ("https://mobile.twitter.com/jack/status/20?s=20", "20"),
        ("https://www.twitter.com/jack/status/20", "20"),
        ("https://fxtwitter.com/jack/status/20", "20"),
        ("https://vxtwitter.com/jack/status/20", "20"),
        ("https://fixupx.com/jack/status/20", "20"),
        ("https://x.com/i/status/2114209839190618413", "2114209839190618413"),
        ("https://x.com/i/web/status/2114209839190618413", "2114209839190618413"),
        ("https://x.com/xiaoyaodez51072/status/2114209839190618413/photo/1", "2114209839190618413"),
        ("https://x.com/xiaoyaodez51072/status/2114209839190618413/video/1", "2114209839190618413"),
        ("https://twitter.com/jack/statuses/20", "20"),
        ("x.com/jack/status/20", "20"),  # 没写 https://
        ("HTTPS://X.COM/Jack/Status/20", "20"),
    ]

    def test_status_id(self):
        for url, expected in self.CASES:
            with self.subTest(url=url):
                self.assertTrue(P._is_x(url))
                self.assertEqual(P._x_status_id(url), expected)

    def test_find_supported_url_in_chat_text(self):
        for url, expected in self.CASES:
            with self.subTest(url=url):
                found = P._find_supported_url(f"帮我看看这个{url}，谢谢！")
                self.assertIsNotNone(found)
                self.assertEqual(P._x_status_id(found), expected)
        # 链接后面紧跟中文标点 / 括号时，标点不能被吞进去
        found = P._find_supported_url("看（https://x.com/jack/status/20）")
        self.assertEqual(P._x_status_id(found), "20")
        self.assertTrue(found.startswith("https://"))

    def test_not_x(self):
        for text in [
            "https://x.com/home",
            "https://x.com/jack",  # 个人主页
            "https://x.com/jack/status/abc",
            "https://twitter.com/search?q=status/20",
            "https://box.com/foo/status/20",  # box.com 结尾带 x.com，不能误判
            "https://dropbox.com/s/status/123",
            "https://notx.com/foo/status/20",
            "https://example.com/x.com/foo/status/20",
            "随便聊聊 x.com 这个网站",
        ]:
            with self.subTest(text=text):
                self.assertIsNone(P._find_supported_url(text))
        self.assertFalse(P._is_x("https://box.com/a/status/1"))
        self.assertIsNone(P._x_status_id("https://box.com/a/status/1"))
        self.assertIsNone(P._x_status_id("https://x.com/jack"))

    def test_other_platforms_unaffected(self):
        self.assertEqual(
            P._find_supported_url("https://www.douyin.com/video/7684314365076960202"),
            "https://www.douyin.com/video/7684314365076960202",
        )
        self.assertIsNotNone(P._find_supported_url("https://www.bilibili.com/video/BV1GJ411x7h7"))
        self.assertIsNotNone(P._find_supported_url("看 xhslink.com/abc 这个"))
        self.assertIsNotNone(P._find_supported_url("https://b23.tv/abcdef"))
        # X 不走本地解析器
        self.assertFalse(P._needs_local_resolver(REAL_VIDEO_URL))
        self.assertIsNone(P._platform_of(REAL_VIDEO_URL))
        self.assertEqual(P._download_platform(REAL_VIDEO_URL), "x")
        self.assertEqual(P._source_name(REAL_VIDEO_URL), "X")
        self.assertEqual(P._PLATFORM_NAMES["x"], "X")
        self.assertIn("x", P._DOWNLOAD_REFERER)

    def test_disabled_by_config(self):
        P.plugin.config_override = {"x_enabled": False}
        try:
            self.assertIsNone(P._find_supported_url(REAL_VIDEO_URL))
            self.assertIsNotNone(P._find_supported_url("https://www.bilibili.com/video/BV1GJ411x7h7"))
        finally:
            P.plugin.config_override = {}
        self.assertIsNotNone(P._find_supported_url(REAL_VIDEO_URL))

    def test_token(self):
        for tweet_id in (REAL_VIDEO_ID, "20", MULTI_PHOTO_ID):
            token = P._x_token(tweet_id)
            self.assertTrue(token and token.isalnum(), token)
        self.assertEqual(P._x_token("not-a-number"), "a")


# ---------------------------------------------------------------- 纯函数（离线、合成数据）


class PureFunctions(unittest.TestCase):
    def test_beijing_time(self):
        self.assertEqual(P._x_beijing_time("2026-10-25T04:18:00.000Z"), "2026-10-25 12:18")
        self.assertEqual(P._x_beijing_time("2026-10-25T20:30:00.000Z"), "2026-10-26 04:30")  # 跨日
        self.assertEqual(P._x_beijing_time("garbage"), "")
        self.assertEqual(P._x_beijing_time(None), "")

    def test_clean_text(self):
        data = {
            "text": "好吃！\n更好听！ https://t.co/md7Lt96HWj A &amp; B https://t.co/xyz https://t.co/quoted",
            "entities": {
                "media": [{"url": "https://t.co/md7Lt96HWj"}],
                "urls": [
                    {"url": "https://t.co/xyz", "expanded_url": "https://example.com/page"},
                    {"url": "https://t.co/quoted", "expanded_url": "https://x.com/a/status/777"},
                ],
            },
            "quoted_tweet": {"id_str": "777"},
        }
        self.assertEqual(P._x_clean_text(data), "好吃！\n更好听！  A & B https://example.com/page")

    def test_video_variants_sorted_and_labeled(self):
        media = {
            "type": "video",
            "video_info": {
                "duration_millis": 5000,
                "variants": [
                    {"content_type": "application/x-mpegURL", "url": "https://v/pl/a.m3u8"},
                    {"content_type": "video/mp4", "bitrate": 256000, "url": "https://v/vid/avc1/480x270/a.mp4"},
                    {"content_type": "video/mp4", "bitrate": 2176000, "url": "https://v/vid/avc1/1280x720/c.mp4"},
                    {"content_type": "video/mp4", "bitrate": 832000, "url": "https://v/vid/avc1/640x360/b.mp4"},
                ],
            },
        }
        out = P._x_video_variants(media)
        self.assertEqual([v["label"] for v in out], ["720p", "360p", "270p"])
        self.assertEqual(out[0]["bitrate"], 2176000)
        self.assertEqual((out[0]["width"], out[0]["height"]), (1280, 720))
        self.assertEqual(out[0]["estimated_size"], 2176000 * 5000 // 8000)
        gif = P._x_video_variants(
            {"type": "animated_gif", "video_info": {"variants": [{"content_type": "video/mp4", "bitrate": 0, "url": "https://v/tweet_video/x.mp4"}]}}
        )
        self.assertEqual(len(gif), 1)
        self.assertEqual(gif[0]["label"], "动图")

    def test_pick_stream_prefers_best_within_limit(self):
        videos = [
            {"label": "720p", "url": "u3", "size": 80 * 1024 * 1024, "bitrate": 3},
            {"label": "360p", "url": "u2", "size": 20 * 1024 * 1024, "bitrate": 2},
            {"label": "270p", "url": "u1", "size": 5 * 1024 * 1024, "bitrate": 1},
        ]
        self.assertEqual(P._pick_stream(videos, 100 * 1024 * 1024)["label"], "720p")
        self.assertEqual(P._pick_stream(videos, 30 * 1024 * 1024)["label"], "360p")
        self.assertEqual(P._pick_stream(videos, 1024)["label"], "270p")  # 都超限：取最小，由调用方报错

    def test_caption_layout(self):
        resolved = {
            "author": "天宮 秋水",
            "screen_name": "xiaoyaodez51072",
            "created_at_beijing": "2026-10-25 12:18",
            "likes": 18,
            "replies": 2,
            "title": "好吃！\n更好听！",
            "canonical_url": "https://x.com/xiaoyaodez51072/status/1",
            "quoted": {"author": "Q", "screen_name": "qq", "text": "被引用的话"},
            "long_text": True,
        }
        text = P._format_x_caption(resolved, "视频", True)
        self.assertEqual(
            text.splitlines()[:4],
            ["X 帖子 · 视频", "天宮 秋水 @xiaoyaodez51072 · 2026-10-25 12:18（北京时间）", "点赞 18 · 回复 2", ""],
        )
        self.assertIn("好吃！\n更好听！", text)
        self.assertIn("↪ 引用 @qq：被引用的话", text)
        self.assertIn("长文", text)
        self.assertTrue(text.endswith("链接：https://x.com/xiaoyaodez51072/status/1"))
        self.assertNotIn("点赞", P._format_x_caption(resolved, "视频", False))

    def test_tombstone_messages(self):
        cases = {
            "This Post is from a suspended account.": "封禁",
            "You’re unable to view this Post because this account owner limits who can view their Posts.": "保护",
            "This Post may contain sensitive content. Log in to view": "敏感",
            "This Post was deleted by the Post author.": "删除",
            "something else": "不可见",
        }
        for raw, keyword in cases.items():
            with self.subTest(raw=raw):
                message = P._x_tombstone_message({"tombstone": {"text": {"text": raw}}})
                self.assertIn(keyword, message)


# ---------------------------------------------------------------- _resolve_x / _obtain_x 的错误路径（离线，打补丁）


class ErrorPathsOffline(unittest.TestCase):
    def setUp(self):
        self._orig = P._fetch_x_syndication

    def tearDown(self):
        P._fetch_x_syndication = self._orig

    def _patch(self, result):
        async def fake(_tweet_id):
            return result

        P._fetch_x_syndication = fake

    def test_bad_url(self):
        r = run(P._resolve_x("https://x.com/jack"))
        self.assertFalse(r["ok"])
        self.assertIn("帖子 ID", r["error"])

    def test_404(self):
        self._patch({"kind": "missing"})
        r = run(P._resolve_x("https://x.com/a/status/123"))
        self.assertFalse(r["ok"])
        self.assertFalse(r.get("fallback"))
        self.assertIn("删除", r["error"])
        self.assertIn("受保护", r["error"])

    def test_blocked(self):
        self._patch({"kind": "blocked", "status": 403})
        r = run(P._resolve_x("https://x.com/a/status/123"))
        self.assertFalse(r["ok"])
        self.assertIn("登录", r["error"])

    def test_tombstone(self):
        self._patch({"kind": "data", "data": {"__typename": "TweetTombstone", "tombstone": {"text": {"text": "This Post is from a suspended account."}}}})
        r = run(P._resolve_x("https://x.com/a/status/123"))
        self.assertFalse(r["ok"])
        self.assertIn("封禁", r["error"])
        self.assertFalse(r.get("fallback"))

    def test_empty_object(self):
        self._patch({"kind": "data", "data": {}})
        r = run(P._resolve_x("https://x.com/a/status/123"))
        self.assertFalse(r["ok"])
        self.assertIn("没有返回", r["error"])

    def test_retryable_marks_fallback_and_ytdlp_is_used(self):
        self._patch({"kind": "retry", "error": "HTTP 429"})
        r = run(P._resolve_x("https://x.com/a/status/123"))
        self.assertFalse(r["ok"])
        self.assertTrue(r["fallback"])
        self.assertIn("429", r["error"])

        called: List[str] = []
        orig = P._run_ytdlp

        async def fake_ytdlp(url, task_dir):
            called.append(url)
            f = task_dir / "video.mp4"
            f.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 32)
            return f, f.name

        P._run_ytdlp = fake_ytdlp
        tmp = Path(tempfile.mkdtemp())
        try:
            source = run(P._obtain_x("https://x.com/a/status/123", tmp))
            self.assertEqual(called, ["https://x.com/a/status/123"])
            self.assertIsNotNone(source.path)
            self.assertEqual(source.caption, "")  # 回退路径拿不到正文
            # yt-dlp 也失败：两边原因都要报
            async def failing(_u, _d):
                raise RuntimeError("服务器尚未安装 yt-dlp")

            P._run_ytdlp = failing
            with self.assertRaises(RuntimeError) as ctx:
                run(P._obtain_x("https://x.com/a/status/123", tmp))
            self.assertIn("429", str(ctx.exception))
            self.assertIn("yt-dlp", str(ctx.exception))
        finally:
            P._run_ytdlp = orig
            shutil.rmtree(tmp, ignore_errors=True)

    def test_definitive_error_does_not_call_ytdlp(self):
        self._patch({"kind": "missing"})
        orig = P._run_ytdlp

        async def boom(_u, _d):
            raise AssertionError("不应回退 yt-dlp")

        P._run_ytdlp = boom
        tmp = Path(tempfile.mkdtemp())
        try:
            with self.assertRaises(RuntimeError) as ctx:
                run(P._obtain_x("https://x.com/a/status/123", tmp))
            self.assertIn("没有找到", str(ctx.exception))
        finally:
            P._run_ytdlp = orig
            shutil.rmtree(tmp, ignore_errors=True)

    def test_gif_and_mixed_synthetic(self):
        """合成数据：动图（animated_gif）与「图片 + 视频」混合帖子的解析结构。"""
        data = {
            "__typename": "Tweet",
            "id_str": "5",
            "text": "gif and pics https://t.co/aa",
            "created_at": "2026-01-01T00:00:00.000Z",
            "user": {"name": "N", "screen_name": "n"},
            "entities": {"media": [{"url": "https://t.co/aa"}]},
            "mediaDetails": [
                {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/a.jpg"},
                {
                    "type": "animated_gif",
                    "media_url_https": "https://pbs.twimg.com/tweet_video_thumb/t.jpg",
                    "video_info": {"variants": [{"content_type": "video/mp4", "bitrate": 0, "url": "https://video.twimg.com/tweet_video/x.mp4"}]},
                },
            ],
            "note_tweet": {"id": "1"},
        }
        self._patch({"kind": "data", "data": data})

        async def no_head(videos):
            for v in videos:
                v["size"] = 123456

        orig = P._x_fill_sizes
        P._x_fill_sizes = no_head
        try:
            r = run(P._resolve_x("https://x.com/n/status/5"))
        finally:
            P._x_fill_sizes = orig
        self.assertTrue(r["ok"])
        self.assertEqual(r["title"], "gif and pics")
        self.assertEqual(r["image_list"], ["https://pbs.twimg.com/media/a.jpg?name=orig"])
        self.assertEqual(len(r["videos"]), 1)
        self.assertTrue(r["is_gif"])
        self.assertTrue(r["long_text"])
        self.assertEqual(r["cover"], "https://pbs.twimg.com/tweet_video_thumb/t.jpg")



# ---------------------------------------------------------------- 合并转发节点组装（离线）


class SendForwardOffline(unittest.TestCase):
    def setUp(self):
        self.bot = _FakeBot()
        self._old = P.get_bot
        P.get_bot = lambda: self.bot

    def tearDown(self):
        P.get_bot = self._old

    def test_mixed_images_and_video_with_caption(self):
        run(P._send_forward(_FakeCtx(), "http://h/v.mp4", "v.mp4", "https://x.com/a/status/1", images=["http://h/1.jpg", "http://h/2.jpg"], caption="X 帖子 · 视频\n正文"))
        content = self.bot.calls[0]["messages"][0]["data"]["content"]
        self.assertEqual([s["type"] for s in content], ["text", "image", "image", "video"])
        self.assertEqual(content[0]["data"]["text"], "X 帖子 · 视频\n正文")

    def test_text_only_private_chat(self):
        run(P._send_forward(_FakeCtx("onebot_v11-private_555"), None, "n", "https://x.com/a/status/1", caption="只有文字"))
        call = self.bot.calls[0]
        self.assertEqual(call["api"], "send_private_forward_msg")
        self.assertEqual(call["user_id"], 555)
        self.assertEqual([s["type"] for s in call["messages"][0]["data"]["content"]], ["text"])

    def test_other_platforms_unchanged(self):
        """没有 caption 时，输出必须与改动前完全一致（抖音/小红书/B站/TikTok 的老行为）。"""
        run(P._send_forward(_FakeCtx(), "http://h/v.mp4", "v.mp4", "https://www.douyin.com/video/1"))
        content = self.bot.calls[-1]["messages"][0]["data"]["content"]
        self.assertEqual(content, [
            {"type": "text", "data": {"text": "抖音 视频\n链接：https://www.douyin.com/video/1"}},
            {"type": "video", "data": {"file": "http://h/v.mp4", "name": "v.mp4"}},
        ])
        run(P._send_forward(_FakeCtx(), None, "n", "https://www.xiaohongshu.com/explore/1", images=["http://h/1.jpg"]))
        content = self.bot.calls[-1]["messages"][0]["data"]["content"]
        self.assertEqual(content, [
            {"type": "text", "data": {"text": "小红书 图文（1 张）\n链接：https://www.xiaohongshu.com/explore/1"}},
            {"type": "image", "data": {"file": "http://h/1.jpg"}},
        ])


# ---------------------------------------------------------------- 联网：真实帖子（(b)(c)(d)(e)）


def _sniff_mp4(path: Path) -> bool:
    head = path.read_bytes()[:16]
    return len(head) >= 12 and head[4:8] == b"ftyp"


@unittest.skipIf(OFFLINE, "NMF_X_OFFLINE=1：跳过联网用例")
class LiveSyndication(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="nmf-x-test-"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # (b)
    def test_b_real_video_post(self):
        r = run(P._resolve_x(REAL_VIDEO_URL + "?s=46"))
        print("\n[b] 解析结果摘要：", {k: r.get(k) for k in ("ok", "platform", "title", "author", "screen_name", "created_at_beijing", "likes", "replies", "cover")})
        print("[b] videos：", [(v["label"], v["bitrate"], v["size"]) for v in r["videos"]])
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["platform"], "x")
        self.assertEqual(r["title"], "好吃！\n更好听！")  # t.co 媒体占位链接已去掉
        self.assertEqual(r["screen_name"], "xiaoyaodez51072")
        self.assertTrue(r["author"])
        self.assertRegex(r["created_at_beijing"], r"^\d{4}-\d\d-\d\d \d\d:\d\d$")
        self.assertGreaterEqual(len(r["videos"]), 3)
        self.assertTrue(all(v["url"].endswith(".mp4") for v in r["videos"]))
        self.assertEqual(r["videos"][0]["bitrate"], max(v["bitrate"] for v in r["videos"]))
        self.assertEqual(r["videos"][0]["height"], 720)
        self.assertTrue(all(v["size"] and v["size"] > 0 for v in r["videos"]), "HEAD 应拿到真实体积")
        self.assertTrue(r["cover"].startswith("https://pbs.twimg.com/"))
        self.assertEqual(r["image_list"], [])
        self.assertEqual(r["note_id"], REAL_VIDEO_ID)

    # (c)
    def test_c_download_best_mp4_and_cover(self):
        r = run(P._resolve_x(REAL_VIDEO_URL))
        self.assertTrue(r["ok"], r)
        out = self.tmp / "c"
        out.mkdir()
        limit = 60 * 1024 * 1024
        source = run(P._download_resolved(r, "x", out, limit))
        print("\n[c] 下载：", source.name, source.size, "bytes")
        self.assertTrue(source.path.is_file())
        self.assertGreater(source.path.stat().st_size, 10_000)
        self.assertTrue(_sniff_mp4(source.path), "mp4 头部应含 ftyp")
        self.assertEqual(source.size, r["videos"][0]["size"])  # 挑中的是最高清那档
        headers = {"User-Agent": P._DOWNLOAD_UA, "Referer": P._DOWNLOAD_REFERER["x"]}
        cover = run(P._download_direct(r["cover"], out / "cover.jpg", limit, headers))
        data = cover.read_bytes()
        print("[c] 封面：", cover.name, len(data), "bytes")
        self.assertGreater(len(data), 1000)
        self.assertTrue(data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n", "封面应是 JPEG / PNG")

    def test_c2_size_limit_enforced(self):
        r = run(P._resolve_x(REAL_VIDEO_URL))
        out = self.tmp / "c2"
        out.mkdir()
        with self.assertRaises(RuntimeError) as ctx:
            run(P._download_resolved(r, "x", out, 1024))  # 1KB：所有档位都超限
        self.assertIn("上限", str(ctx.exception))

    # (d)
    def test_d_multi_photo_post(self):
        r = run(P._resolve_x(f"https://twitter.com/SpaceX/status/{MULTI_PHOTO_ID}"))
        print("\n[d] 多图帖：", r.get("title"), len(r.get("image_list") or []), "张")
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(r["image_list"]), 3)
        self.assertTrue(all(u.endswith("?name=orig") for u in r["image_list"]))
        self.assertEqual(r["videos"], [])
        self.assertIn("Falcon Heavy", r["title"])
        out = self.tmp / "d"
        out.mkdir()
        headers = {"User-Agent": P._DOWNLOAD_UA, "Referer": P._DOWNLOAD_REFERER["x"]}
        first = run(P._download_direct(r["image_list"][0], out / "i1.jpg", 60 * 1024 * 1024, headers))
        data = first.read_bytes()
        print("[d] 第 1 张原图：", len(data), "bytes")
        self.assertGreater(len(data), 10_000)
        self.assertTrue(data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n")

    def test_d_text_only_posts(self):
        for tweet_id, needle, who in ((TEXT_ONLY_ID, "the bird is freed", "elonmusk"), (TEXT_ONLY_ID_2, "just setting up my twttr", "jack")):
            with self.subTest(tweet_id=tweet_id):
                r = run(P._resolve_x(f"https://x.com/i/status/{tweet_id}"))
                self.assertTrue(r["ok"], r)
                self.assertEqual(r["title"], needle)
                self.assertEqual(r["screen_name"], who)
                self.assertEqual(r["videos"], [])
                self.assertEqual(r["image_list"], [])
                self.assertIsNone(r["cover"])
        r = run(P._resolve_x(f"https://x.com/i/status/{TEXT_ONLY_ID_2}"))
        self.assertEqual(r["created_at_beijing"], "2006-03-22 04:50")  # 2006-03-21 20:50 UTC → +8h，跨日

    def test_d_quote_tweet(self):
        r = run(P._resolve_x(f"https://x.com/elonmusk/status/{QUOTE_ID}"))
        print("\n[d] 引用帖：", r.get("title"), "| 被引用：", (r.get("quoted") or {}).get("screen_name"), (r.get("quoted") or {}).get("text", "")[:40])
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["title"], "Dario is right")
        self.assertTrue(r["quoted"] and r["quoted"]["text"])
        caption = P._format_x_caption(r, "纯文字", True)
        self.assertIn("↪ 引用 @", caption)
        self.assertIn(r["quoted"]["text"][:20], caption)

    # (e)
    def test_e_missing_post(self):
        r = run(P._resolve_x(f"https://x.com/nobody/status/{MISSING_ID}"))
        print("\n[e] 不存在的帖子：", r)
        self.assertFalse(r["ok"])
        self.assertFalse(r.get("fallback"))
        self.assertIn("没有找到", r["error"])
        # 走 _obtain_x：给用户的是可读中文，而不是回退 yt-dlp
        tmp = self.tmp / "e"
        tmp.mkdir()
        with self.assertRaises(RuntimeError) as ctx:
            run(P._obtain_x(f"https://x.com/nobody/status/{MISSING_ID}", tmp))
        self.assertIn("已被删除", str(ctx.exception))


# ---------------------------------------------------------------- 联网 + 假 NapCat：整条 _process 链路


class _FakeCtx:
    adapter_key = "onebot_v11"

    def __init__(self, chat_key: str = "onebot_v11-group_10001"):
        self.chat_key = chat_key
        self.sent: List[str] = []

    async def send_text(self, text: str, record: bool = True):
        self.sent.append(text)


class _FakeBot:
    self_id = "424242"

    def __init__(self):
        self.calls: List[dict] = []

    async def call_api(self, api: str, **kwargs: Any):
        self.calls.append({"api": api, **kwargs})
        return {"message_id": 1}


@unittest.skipIf(OFFLINE, "NMF_X_OFFLINE=1：跳过联网用例")
class EndToEndProcess(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nmf-x-e2e-"))
        self._old_dir = P._VIDEO_DIR
        P._VIDEO_DIR = self.tmp
        P.plugin.config_override = {"cooldown_seconds": 0, "public_base_url": "http://napcat.test:8022"}
        P._RECENT_URLS.clear()
        P._LAST_CHAT_REQUEST.clear()
        P._LEASES.clear()
        self.bot = _FakeBot()
        self._old_get_bot = P.get_bot
        P.get_bot = lambda: self.bot

    def tearDown(self):
        P.get_bot = self._old_get_bot
        P._VIDEO_DIR = self._old_dir
        P.plugin.config_override = {}
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _forward(self):
        self.assertEqual(len(self.bot.calls), 1, self.bot.calls)
        call = self.bot.calls[0]
        self.assertEqual(call["api"], "send_group_forward_msg")
        self.assertEqual(call["group_id"], 10001)
        (node,) = call["messages"]
        self.assertEqual(node["type"], "node")
        self.assertEqual(node["data"]["user_id"], "424242")
        return node["data"]["content"]

    def test_video_post(self):
        ctx = _FakeCtx()
        result = run(P._process(ctx, REAL_VIDEO_URL))
        content = self._forward()
        print("\n[e2e] 视频帖返回：", result)
        print("[e2e] 节点段类型：", [seg["type"] for seg in content])
        print("[e2e] 文字节点：\n" + content[0]["data"]["text"])
        self.assertEqual(result, "视频已通过合并转发发回原聊天。")
        self.assertEqual([seg["type"] for seg in content], ["text", "video"])  # 文字在媒体前面
        text = content[0]["data"]["text"]
        self.assertIn("X 帖子 · 视频", text)
        self.assertIn("@xiaoyaodez51072", text)
        self.assertIn("（北京时间）", text)
        self.assertIn("好吃！\n更好听！", text)
        self.assertIn(f"https://x.com/xiaoyaodez51072/status/{REAL_VIDEO_ID}", text)
        self.assertNotIn("t.co/", text)
        video = content[1]["data"]
        self.assertTrue(video["file"].startswith("http://napcat.test:8022/plugins/local_video.video_forward/media/"))
        token = video["file"].rsplit("/", 1)[1]
        lease = P._LEASES[token]
        self.assertGreater(lease.path.stat().st_size, 10_000)
        self.assertTrue(_sniff_mp4(lease.path))
        self.assertIn("正在解析X内容", ctx.sent[0])

    def test_multi_photo_post(self):
        ctx = _FakeCtx()
        result = run(P._process(ctx, f"https://x.com/SpaceX/status/{MULTI_PHOTO_ID}?s=46"))
        content = self._forward()
        print("\n[e2e] 多图帖返回：", result, [seg["type"] for seg in content])
        self.assertEqual([seg["type"] for seg in content], ["text", "image", "image", "image"])
        self.assertIn("图文（3 张）", content[0]["data"]["text"])
        self.assertIn("Falcon Heavy", content[0]["data"]["text"])
        for seg in content[1:]:
            lease = P._LEASES[seg["data"]["file"].rsplit("/", 1)[1]]
            self.assertGreater(lease.path.stat().st_size, 10_000)

    def test_multi_photo_respects_max_images(self):
        P.plugin.config_override["max_images"] = 2
        ctx = _FakeCtx()
        run(P._process(ctx, f"https://x.com/SpaceX/status/{MULTI_PHOTO_ID}"))
        content = self._forward()
        self.assertEqual([seg["type"] for seg in content], ["text", "image", "image"])
        self.assertIn("仅发送前 2 张", content[0]["data"]["text"])

    def test_text_only_post(self):
        ctx = _FakeCtx()
        result = run(P._process(ctx, f"https://x.com/elonmusk/status/{TEXT_ONLY_ID}"))
        content = self._forward()
        print("\n[e2e] 纯文字帖返回：", result, "\n" + content[0]["data"]["text"])
        self.assertEqual(result, "帖子文字已通过合并转发发回原聊天。")
        self.assertEqual([seg["type"] for seg in content], ["text"])
        self.assertIn("the bird is freed", content[0]["data"]["text"])
        self.assertIn("X 帖子 · 纯文字", content[0]["data"]["text"])

    def test_text_only_with_text_disabled(self):
        P.plugin.config_override["x_send_text"] = False
        ctx = _FakeCtx()
        result = run(P._process(ctx, f"https://x.com/elonmusk/status/{TEXT_ONLY_ID}"))
        self.assertTrue(result.startswith("视频未能发送："), result)
        self.assertIn("没有可发送的内容", result)
        self.assertEqual(self.bot.calls, [])

    def test_video_with_text_disabled_uses_default_header(self):
        P.plugin.config_override["x_send_text"] = False
        ctx = _FakeCtx()
        run(P._process(ctx, REAL_VIDEO_URL))
        content = self._forward()
        self.assertEqual([seg["type"] for seg in content], ["text", "video"])
        self.assertTrue(content[0]["data"]["text"].startswith("X 视频\n链接："))

    def test_missing_post_message(self):
        ctx = _FakeCtx()
        result = run(P._process(ctx, f"https://x.com/nobody/status/{MISSING_ID}"))
        print("\n[e2e] 不存在帖子返回：", result)
        self.assertTrue(result.startswith("视频未能发送："))
        self.assertIn("已被删除", result)
        self.assertEqual(self.bot.calls, [])

    def test_status_endpoint_lists_x(self):
        router = P.create_router() if callable(getattr(P, "create_router", None)) else None
        self.assertIsNotNone(router)
        endpoint = next(r.endpoint for r in router.routes if getattr(r, "path", "") == "/")
        data = run(endpoint())
        self.assertIn("x", data["supported"])
        self.assertTrue(data["x"]["enabled"])
        self.assertEqual(data["version"], "0.11.0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
