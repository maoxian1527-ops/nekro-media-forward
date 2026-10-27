#!/usr/bin/env python3
"""Instagram / Facebook 帖子解析的自检脚本，风格和 tests/test_x_resolver.py 一致，不依赖 NekroAgent 运行环境。

做法：复用 test_x_resolver 里「给 nekro_agent.* / nonebot 塞假模块再导入真实 plugin/plugin.py」的装置，
所以这里调用的就是插件里真实的 _instagram_ref / _facebook_ref / _resolve_instagram / _resolve_facebook /
_obtain_* / _process。

运行（需要 httpx、pydantic、fastapi；联网用例里的 ffprobe 需要 ffmpeg）：

    python tests/test_ig_fb_resolver.py                    # 全部（含真实联网）
    NMF_IGFB_OFFLINE=1 python tests/test_ig_fb_resolver.py # 只跑离线用例（用 tests/fixtures/ 里脱敏的样例）

联网用例只匿名访问 Instagram / Facebook 的公开帖子页和它们的 CDN，不使用任何 cookie / token / 登录态；
下载的文件只写到临时目录，测完即删。Meta 会对频繁请求限流，联网的解析步骤失败（429 / 空响应）时会重试几次，
重试后仍然失败就让用例失败，不会悄悄当成通过。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402
import test_x_resolver as X  # noqa: E402  (装好假的 nekro_agent 模块，并导入真实的 plugin.py)

P = X.P
run = X.run
_FakeCtx = X._FakeCtx
_FakeBot = X._FakeBot
_sniff_mp4 = X._sniff_mp4
OFFLINE = bool(os.environ.get("NMF_IGFB_OFFLINE"))
FIXTURES = Path(__file__).resolve().parent / "fixtures"
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

# 真实公开帖子（只读；帖子被删 / 改了的话联网用例会失败，到时更新样例即可）
IG_SINGLE = "https://www.instagram.com/p/De9piNJlwES/"  # NASA 单图
IG_REEL = "https://www.instagram.com/reel/DeTUk4sDSp-/"  # NASA Reel
IG_CAROUSEL = "https://www.instagram.com/p/DJ4ZzePT6jQ/?igsh=MTIzNDU2"  # NatGeo 6 图轮播
IG_CAROUSEL_VIDEO = "https://www.instagram.com/p/DeKe6teEpya/"  # Google 20 项轮播（含 1 个视频）
IG_EMBED_ONLY = "https://www.instagram.com/p/De5mVMikVB-/"  # 4 图轮播，用来测 embed 回退
IG_MISSING = "https://www.instagram.com/p/ZZZZZZZZZZZ/"
FB_ALBUM = "https://www.facebook.com/NASA/posts/1658876782274358"  # 3 图相册
FB_SINGLE = "https://www.facebook.com/NASA/posts/1668207108007992"  # 单图
FB_TEXT = "https://www.facebook.com/zuck/posts/pfbid0EEdDQVai4umZQwLkvGmFZnAnAucg4GMRLrphVp2SYyCUfBmtL9gmAzap22uA2jscl"
FB_LINK = "https://www.facebook.com/bbcnews/posts/1377611997736122"  # 链接帖（带预览图）
FB_VIDEO = "https://www.facebook.com/NASA/videos/1803909017420449/"
FB_WATCH = "https://www.facebook.com/watch/?v=1803909017420449"
FB_REEL = "https://www.facebook.com/reel/1513894760512927"
FB_SHARE = "https://www.facebook.com/share/p/14pj5DHbCZG/"
FB_MISSING = "https://www.facebook.com/NASA/posts/1"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf8")


def pr(*args: Any) -> None:
    print(*args, flush=True)


def resolve_retry(resolver, url: str, attempts: int = 3, delay: float = 4.0) -> Dict[str, Any]:
    """联网解析：Meta 偶尔限流 / 返回空壳，``fallback=True`` 的暂时性失败重试几次；仍失败就原样返回，由用例判失败。"""
    result: Dict[str, Any] = {}
    for attempt in range(1, attempts + 1):
        result = run(resolver(url))
        if result.get("ok") or not result.get("fallback"):
            if attempt > 1:
                pr(f"    （第 {attempt} 次尝试才成功 / 得到确定结果：{url}）")
            return result
        pr(f"    （第 {attempt} 次暂时性失败：{str(result.get('error'))[:100]}，{delay}s 后重试）")
        time.sleep(delay)
    return result


def image_size(data: bytes) -> Optional[tuple]:
    """从文件头解析 JPEG / PNG / WebP 的真实宽高（不依赖 PIL）。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", data[16:24])
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        kind = data[12:16]
        if kind == b"VP8X":
            return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
        if kind == b"VP8 ":
            return struct.unpack("<HH", data[26:30])[0] & 0x3FFF, struct.unpack("<HH", data[26:30])[1] & 0x3FFF
        if kind == b"VP8L":
            bits = int.from_bytes(data[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if data[:3] == b"\xff\xd8\xff":
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                height, width = struct.unpack(">HH", data[index + 5 : index + 9])
                return width, height
            index += 2 + struct.unpack(">H", data[index + 2 : index + 4])[0]
    return None


def is_image(data: bytes) -> bool:
    return data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n" or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")


def ffprobe_streams(path: Path) -> Dict[str, Dict[str, Any]]:
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    streams: Dict[str, Dict[str, Any]] = {}
    for stream in json.loads(out)["streams"]:
        streams.setdefault(stream["codec_type"], stream)
    return streams


# ---------------------------------------------------------------- (a) URL 识别


class UrlParsing(unittest.TestCase):
    IG_POSITIVE = [
        ("https://www.instagram.com/p/DeKe6teEpya/", ("p", "DeKe6teEpya")),
        ("https://instagram.com/p/DeKe6teEpya", ("p", "DeKe6teEpya")),
        ("https://www.instagram.com/p/DeKe6teEpya/?igsh=MTIzNDU2&utm_source=qr", ("p", "DeKe6teEpya")),
        ("https://www.instagram.com/reel/DeTUk4sDSp-/", ("reel", "DeTUk4sDSp-")),
        ("https://www.instagram.com/reels/DeTUk4sDSp-/", ("reel", "DeTUk4sDSp-")),
        ("https://www.instagram.com/reel/DeTUk4sDSp-/?igsh=abc", ("reel", "DeTUk4sDSp-")),
        ("https://www.instagram.com/tv/CxYz12AbCdE/", ("tv", "CxYz12AbCdE")),
        ("https://www.instagram.com/nasa/p/De9piNJlwES/", ("p", "De9piNJlwES")),
        ("https://www.instagram.com/nasa/reel/DeTUk4sDSp-/", ("reel", "DeTUk4sDSp-")),
        ("https://m.instagram.com/p/DeKe6teEpya/", ("p", "DeKe6teEpya")),
        ("instagram.com/p/DeKe6teEpya/", ("p", "DeKe6teEpya")),  # 没写 https://
        ("HTTPS://WWW.INSTAGRAM.COM/P/DeKe6teEpya/", ("p", "DeKe6teEpya")),
        ("https://www.instagram.com/p/DeKe6teEpya/embed/", ("p", "DeKe6teEpya")),
        ("https://www.instagram.com/stories/nasa/3801234567890123456/", ("story", "")),
        ("https://www.instagram.com/stories/highlights/17912345678901234/", ("story", "")),
    ]
    IG_NEGATIVE = [
        "https://www.instagram.com/",
        "https://www.instagram.com/nasa/",
        "https://www.instagram.com/nasa/reels/",
        "https://www.instagram.com/reels/",
        "https://www.instagram.com/explore/",
        "https://www.instagram.com/explore/tags/space/",
        "https://www.instagram.com/accounts/login/",
        "https://l.instagram.com/?u=https%3A%2F%2Fexample.com",
        "https://notinstagram.com/p/DeKe6teEpya/",
        "https://example.com/instagram.com/p/DeKe6teEpya/",
        "https://www.instagram.com/p/",
        "随便聊聊 instagram.com 这个网站",
    ]
    FB_POSITIVE = [
        ("https://www.facebook.com/NASA/posts/1658876782274358", "post"),
        ("https://www.facebook.com/NASA/posts/1658876782274358?ref=embed_post", "post"),
        ("https://www.facebook.com/unicef/posts/what-does-school-mean-for-9-year-old-khadijaits-a-place-to-learna-place-to-play-/856094429888477/", "post"),
        ("https://www.facebook.com/zuck/posts/pfbid0EEdDQVai4umZQwLkvGmFZnAnAucg4GMRLrphVp2SYyCUfBmtL9gmAzap22uA2jscl", "post"),
        ("https://m.facebook.com/NASA/posts/1658876782274358", "post"),
        ("https://web.facebook.com/NASA/posts/1658876782274358", "post"),
        ("https://fb.com/NASA/posts/1658876782274358", "post"),
        ("https://www.facebook.com/NASA/videos/1803909017420449/", "video"),
        ("https://www.facebook.com/NASA/videos/artemis-acceleration/1803909017420449/", "video"),
        ("https://www.facebook.com/video.php?v=1803909017420449", "video"),
        ("https://www.facebook.com/reel/1513894760512927", "reel"),
        ("https://www.facebook.com/reel/1513894760512927?s=fb_shorts_profile", "reel"),
        ("https://www.facebook.com/watch/?v=1803909017420449", "watch"),
        ("https://www.facebook.com/watch?v=1803909017420449&ref=sharing", "watch"),
        ("https://www.facebook.com/photo.php?fbid=10158123456789012&set=a.1234", "photo"),
        ("https://www.facebook.com/photo/?fbid=10158123456789012&set=a.1234", "photo"),
        ("https://www.facebook.com/NASA/photos/a.1234567890/10158123456789012/", "photo"),
        ("https://www.facebook.com/permalink.php?story_fbid=pfbid02abc&id=100044440000000", "permalink"),
        ("https://www.facebook.com/story.php?story_fbid=1658876782274358&id=100044440000000", "permalink"),
        ("https://www.facebook.com/share/p/14pj5DHbCZG/", "share"),
        ("https://www.facebook.com/share/v/1AbCdEfGhI/", "share"),
        ("https://www.facebook.com/share/r/1AbCdEfGhI/", "share"),
        ("https://www.facebook.com/groups/123456789/posts/987654321/", "group"),
        ("https://fb.watch/Ipm19Wy4Le/", "fbwatch"),
        ("facebook.com/NASA/posts/1658876782274358", "post"),
    ]
    FB_NEGATIVE = [
        "https://www.facebook.com/",
        "https://www.facebook.com/NASA",
        "https://www.facebook.com/NASA/",
        "https://www.facebook.com/login/",
        "https://www.facebook.com/l.php?u=https%3A%2F%2Fexample.com",
        "https://www.facebook.com/watch/",
        "https://www.facebook.com/groups/123456789/",
        "https://developers.facebook.com/docs/",
        "https://notfacebook.com/NASA/posts/1658876782274358",
        "https://example.com/facebook.com/NASA/posts/1658876782274358",
        "https://box.com/s/abc",
        "https://dropbox.com/s/abc",
        "随便聊聊 facebook.com 这个网站",
    ]

    def test_instagram_ref(self):
        for url, (kind, code) in self.IG_POSITIVE:
            with self.subTest(url=url):
                self.assertTrue(P._is_instagram(url))
                ref = P._instagram_ref(url)
                self.assertEqual((ref["kind"], ref["code"]), (kind, code))
                found = P._find_supported_url(f"帮我看看这个{url}，谢谢！")
                self.assertIsNotNone(found)
                self.assertEqual(P._instagram_ref(found)["code"], code)
        self.assertEqual(P._instagram_shortcode("https://www.instagram.com/reel/DeTUk4sDSp-/?igsh=x"), "DeTUk4sDSp-")
        self.assertIsNone(P._instagram_shortcode("https://www.instagram.com/stories/nasa/1/"))

    def test_instagram_negative(self):
        for text in self.IG_NEGATIVE:
            with self.subTest(text=text):
                self.assertIsNone(P._find_supported_url(text))
                self.assertIsNone(P._instagram_ref(text) if P._is_instagram(text) else None)

    def test_facebook_ref(self):
        for url, kind in self.FB_POSITIVE:
            with self.subTest(url=url):
                self.assertTrue(P._is_facebook(url))
                ref = P._facebook_ref(url)
                self.assertIsNotNone(ref)
                self.assertEqual(ref["kind"], kind)
                found = P._find_supported_url(f"看看这个：{url}，好看吗")
                self.assertIsNotNone(found, url)
                self.assertEqual(P._facebook_ref(found)["kind"], kind)

    def test_facebook_ref_details(self):
        self.assertEqual(P._facebook_ref("https://www.facebook.com/NASA/posts/1658876782274358")["id"], "1658876782274358")
        self.assertEqual(P._facebook_ref("https://www.facebook.com/reel/1513894760512927?x=1")["id"], "1513894760512927")
        self.assertEqual(P._facebook_ref("https://www.facebook.com/watch/?v=18039&t=3")["id"], "18039")
        self.assertEqual(P._facebook_ref("https://www.facebook.com/photo.php?fbid=123&set=a.1")["id"], "123")
        share = P._facebook_ref("https://www.facebook.com/share/v/1AbCdEfGhI/")
        self.assertEqual((share["sub"], share["code"]), ("v", "1AbCdEfGhI"))
        perm = P._facebook_ref("https://www.facebook.com/permalink.php?story_fbid=555&id=777")
        self.assertEqual((perm["story_fbid"], perm["id"]), ("555", "777"))

    def test_facebook_negative(self):
        for text in self.FB_NEGATIVE:
            with self.subTest(text=text):
                self.assertIsNone(P._find_supported_url(text))
                self.assertIsNone(P._facebook_ref(text) if P._is_facebook(text) else None)

    def test_fetch_url_normalization(self):
        cases = {
            "https://m.facebook.com/NASA/posts/1658876782274358?ref=x": "https://www.facebook.com/NASA/posts/1658876782274358/",
            "https://web.facebook.com/NASA/videos/1803909017420449/": "https://www.facebook.com/NASA/videos/1803909017420449/",
            "https://www.facebook.com/reel/1513894760512927?s=x": "https://www.facebook.com/reel/1513894760512927",
            "https://www.facebook.com/watch?v=18039&t=3": "https://www.facebook.com/watch/?v=18039",
            "https://www.facebook.com/permalink.php?story_fbid=555&id=777": "https://www.facebook.com/777/posts/555",
            "https://www.facebook.com/photo/?fbid=123&set=a.1": "https://www.facebook.com/photo.php?fbid=123",
            "https://www.facebook.com/share/p/14pj5DHbCZG/": "https://www.facebook.com/share/p/14pj5DHbCZG/",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(P._fb_fetch_url(P._facebook_ref(url), url), expected)

    def test_other_platforms_unaffected(self):
        self.assertEqual(
            P._find_supported_url("https://www.douyin.com/video/7684314365076960202"),
            "https://www.douyin.com/video/7684314365076960202",
        )
        self.assertIsNotNone(P._find_supported_url("https://www.bilibili.com/video/BV1GJ411x7h7"))
        self.assertIsNotNone(P._find_supported_url("看 xhslink.com/abc 这个"))
        self.assertIsNotNone(P._find_supported_url("https://b23.tv/abcdef"))
        self.assertEqual(P._x_status_id(P._find_supported_url("https://x.com/jack/status/20")), "20")
        self.assertIsNone(P._find_supported_url("https://x.com/jack"))
        self.assertIsNone(P._find_supported_url("https://box.com/foo/status/20"))
        for url in (IG_REEL, FB_ALBUM):
            self.assertFalse(P._needs_local_resolver(url))
            self.assertIsNone(P._platform_of(url))
        self.assertEqual(P._download_platform(IG_REEL), "instagram")
        self.assertEqual(P._download_platform(FB_ALBUM), "facebook")
        self.assertEqual(P._download_platform("https://x.com/a/status/1"), "x")
        self.assertEqual(P._source_name(IG_REEL), "Instagram")
        self.assertEqual(P._source_name(FB_ALBUM), "Facebook")
        self.assertEqual(P._PLATFORM_NAMES["instagram"], "Instagram")
        self.assertEqual(P._PLATFORM_NAMES["facebook"], "Facebook")
        self.assertEqual(P._DOWNLOAD_REFERER["instagram"], "https://www.instagram.com/")
        self.assertEqual(P._DOWNLOAD_REFERER["facebook"], "https://www.facebook.com/")
        # 下载 UA：IG / FB 用诚实 UA，其它平台沿用浏览器 UA
        self.assertEqual(P._download_ua("instagram"), P._META_UA)
        self.assertEqual(P._download_ua("facebook"), P._META_UA)
        for platform in ("x", "douyin", "xiaohongshu", "bilibili", "tiktok", None):
            self.assertEqual(P._download_ua(platform), P._DOWNLOAD_UA)
        self.assertNotIn("Mozilla", P._META_UA)

    def test_disabled_by_config(self):
        for key, url in (("instagram_enabled", IG_REEL), ("facebook_enabled", FB_ALBUM)):
            P.plugin.config_override = {key: False}
            try:
                self.assertIsNone(P._find_supported_url(url))
                self.assertIsNotNone(P._find_supported_url("https://www.bilibili.com/video/BV1GJ411x7h7"))
                self.assertIsNotNone(P._find_supported_url("https://x.com/jack/status/20"))
            finally:
                P.plugin.config_override = {}
            self.assertIsNotNone(P._find_supported_url(url))

    def test_config_defaults(self):
        config = P.plugin.get_config()
        self.assertTrue(config.instagram_enabled)
        self.assertTrue(config.facebook_enabled)
        self.assertEqual(config.instagram_max_size_mb, 100)
        self.assertEqual(config.facebook_max_size_mb, 100)
        self.assertFalse(config.facebook_send_comments)
        self.assertTrue(config.instagram_fallback_ua)  # 灰色做法，但用户明确决定默认开启（可设为 false 关闭）
        self.assertEqual((config.instagram_proxy, config.facebook_proxy, config.x_proxy), ("", "", ""))  # 代理默认不启用
        with self.assertRaises(Exception):
            P.plugin._config_cls(instagram_max_size_mb=1)  # 低于下限 5
        with self.assertRaises(Exception):
            P.plugin._config_cls(facebook_max_size_mb=501)  # 高于上限 500


# ---------------------------------------------------------------- (b) 纯函数 / 用脱敏样例离线解析


class PureFunctions(unittest.TestCase):
    def test_drop_ctp(self):
        url = "https://scontent.xx.fbcdn.net/v/t39/a.jpg?stp=dst-jpg_tt6&cstp=mx2048x1365&ctp=p600x600&_nc_cat=1&oh=00_abc&oe=6AE00000"
        out = P._fb_drop_ctp(url)
        self.assertNotIn("ctp=", out.replace("cstp=", ""))
        self.assertIn("stp=dst-jpg_tt6", out)
        self.assertIn("cstp=mx2048x1365", out)  # 只去 ctp，stp / cstp 去了会 403
        self.assertIn("oh=00_abc", out)  # 签名原样保留
        self.assertIsNone(P._fb_drop_ctp("https://scontent.xx.fbcdn.net/v/t39/a.jpg?stp=dst-jpg"))

    def test_sniff_ext(self):
        self.assertEqual(P._meta_sniff_ext(b"\xff\xd8\xff\xe0" + b"0" * 12), ".jpg")
        self.assertEqual(P._meta_sniff_ext(b"\x89PNG\r\n\x1a\n" + b"0" * 8), ".png")
        self.assertEqual(P._meta_sniff_ext(b"RIFF\x00\x00\x00\x00WEBPVP8 "), ".webp")
        self.assertEqual(P._meta_sniff_ext(b"GIF89a" + b"0" * 10), ".gif")
        self.assertEqual(P._meta_sniff_ext(b"unknown-bytes!!!!"), ".jpg")

    def test_best_dash_prefers_h264_and_best_audio(self):
        mpd = (
            '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><Period>'
            '<AdaptationSet><Representation bandwidth="3000000" codecs="vp09.00" mimeType="video/mp4" width="1280" height="720"><BaseURL>https://v/vp9.mp4</BaseURL></Representation>'
            '<Representation bandwidth="1000000" codecs="avc1.4d401f" mimeType="video/mp4" width="640" height="360" FBQualityLabel="360p"><BaseURL>https://v/low.mp4</BaseURL></Representation>'
            '<Representation bandwidth="2500000" codecs="avc1.64001f" mimeType="video/mp4" width="1280" height="720" FBQualityLabel="720p"><BaseURL>https://v/hi.mp4</BaseURL></Representation></AdaptationSet>'
            '<AdaptationSet><Representation bandwidth="64000" codecs="mp4a.40.2" mimeType="audio/mp4"><BaseURL>https://a/low.mp4</BaseURL></Representation>'
            '<Representation bandwidth="130000" codecs="mp4a.40.2" mimeType="audio/mp4"><BaseURL>https://a/hi.mp4</BaseURL></Representation></AdaptationSet>'
            "</Period></MPD>"
        )
        video, audio = P._fb_best_dash(mpd)
        self.assertEqual(video["url"], "https://v/hi.mp4")  # 同为 H.264 里码率最高；VP9 码率更高也不选
        self.assertEqual(video["label"], "720p")
        self.assertEqual(audio["url"], "https://a/hi.mp4")
        self.assertEqual(P._fb_best_dash("<not-xml"), (None, None))

    def test_post_kind_labels(self):
        self.assertEqual(P._meta_post_kind({"videos": [1], "image_list": []}, 0), "视频")
        self.assertEqual(P._meta_post_kind({"videos": [1, 2], "image_list": []}, 0), "视频（2 个）")
        self.assertEqual(P._meta_post_kind({"videos": [], "image_list": [1, 2, 3]}, 3), "图文（3 张）")
        self.assertEqual(P._meta_post_kind({"videos": [1], "image_list": [1, 2]}, 2), "图文 + 视频（2 张图 + 1 个视频）")
        self.assertEqual(P._meta_post_kind({"videos": [], "image_list": []}, 0), "纯文字")

    def test_ig_caption_layout(self):
        resolved = {
            "author": "NASA", "screen_name": "nasa", "created_at_beijing": "2026-10-10 23:15",
            "likes": 749562, "replies": 7515, "title": "POV: you're watching the Earth set",
            "canonical_url": "https://www.instagram.com/reel/DeTUk4sDSp-/",
        }
        text = P._format_ig_caption(resolved, "视频")
        self.assertEqual(
            text.splitlines()[:4],
            ["Instagram 帖子 · 视频", "NASA @nasa · 2026-10-10 23:15（北京时间）", "点赞 749562 · 评论 7515", ""],
        )
        self.assertIn("POV: you're watching the Earth set", text)
        self.assertTrue(text.endswith("链接：https://www.instagram.com/reel/DeTUk4sDSp-/"))
        long_text = P._format_ig_caption({**resolved, "title": "字" * 3000}, "视频")
        self.assertIn("已截断", long_text)
        self.assertNotIn("字" * 2300, long_text)

    def test_fb_caption_layout_and_comments(self):
        resolved = {
            "author": "NASA", "created_at_beijing": "2026-10-17 04:05", "likes": 242038, "replies": 2727, "shares": 14596,
            "views": 336684, "title": "Float into the weekend",
            "canonical_url": "https://www.facebook.com/NASA/posts/1/",
            "comments_sample": [{"author": "测试用户A", "text": "第一条\n评论"}],
            "link_attachment": {"title": "BBC", "url": "https://bbc.co.uk/x"},
        }
        text = P._format_fb_caption(resolved, "图文（3 张）")
        self.assertEqual(
            text.splitlines()[:4],
            ["Facebook 帖子 · 图文（3 张）", "NASA · 2026-10-17 04:05（北京时间）", "反应 242038 · 评论 2727 · 分享 14596 · 观看 336684", ""],
        )
        self.assertIn("🔗 BBC https://bbc.co.uk/x", text)
        self.assertNotIn("第一条", text)  # 评论默认不附
        with_comments = P._format_fb_caption(resolved, "图文（3 张）", True)
        self.assertIn("测试用户A：第一条 评论", with_comments)
        self.assertTrue(with_comments.endswith("链接：https://www.facebook.com/NASA/posts/1/"))

    def test_x_caption_unchanged(self):
        """X 的文字节点格式不能被这次改动影响。"""
        resolved = {
            "author": "天宮 秋水", "screen_name": "xiaoyaodez51072", "created_at_beijing": "2026-10-25 12:18",
            "likes": 18, "replies": 2, "title": "好吃！\n更好听！", "canonical_url": "https://x.com/xiaoyaodez51072/status/1",
        }
        self.assertEqual(
            P._format_x_caption(resolved, "视频", True),
            "X 帖子 · 视频\n天宮 秋水 @xiaoyaodez51072 · 2026-10-25 12:18（北京时间）\n点赞 18 · 回复 2\n\n好吃！\n更好听！\n\n链接：https://x.com/xiaoyaodez51072/status/1",
        )


class FixtureParsing(unittest.TestCase):
    """用 tests/fixtures/ 里脱敏的真实帖子样例走真实解析函数（不联网）。"""

    def test_ig_single_image(self):
        node = P._ig_find_media(fixture("ig_single.html"), "De9piNJlwES")
        self.assertIsNotNone(node)
        r = P._ig_build_resolved(P._ig_post_from_page(node), "De9piNJlwES", "p")
        self.assertTrue(r["ok"])
        self.assertEqual((r["platform"], r["screen_name"], r["note_id"]), ("instagram", "nasa", "De9piNJlwES"))
        self.assertEqual(r["author"], "NASA")
        self.assertEqual(len(r["image_list"]), 1)
        self.assertEqual(r["videos"], [])
        self.assertTrue(r["title"].startswith("A close-knit family of stars"))
        self.assertRegex(r["created_at_beijing"], r"^\d{4}-\d\d-\d\d \d\d:\d\d$")
        self.assertIsInstance(r["likes"], int)
        self.assertIsInstance(r["replies"], int)
        self.assertEqual(r["canonical_url"], "https://www.instagram.com/p/De9piNJlwES/")
        self.assertEqual(r["cover"], r["image_list"][0])

    def test_ig_reel(self):
        node = P._ig_find_media(fixture("ig_reel.html"), "DeTUk4sDSp-")
        r = P._ig_build_resolved(P._ig_post_from_page(node), "DeTUk4sDSp-", "p")
        self.assertEqual(len(r["videos"]), 1)
        self.assertEqual(r["image_list"], [])  # 视频的封面不当图片发
        self.assertTrue(r["videos"][0]["url"].startswith("https://"))
        self.assertEqual(r["canonical_url"], "https://www.instagram.com/reel/DeTUk4sDSp-/")  # product_type=clips → reel
        self.assertEqual(r["created_at_beijing"], "2026-10-10 23:15")  # 15:15 UTC → +8h
        self.assertEqual((r["videos"][0]["width"], r["videos"][0]["height"]), (718, 1276))
        self.assertEqual(r["cover"].split("?")[0].rsplit(".", 1)[-1], "jpg")

    def test_ig_carousel_and_mixed(self):
        node = P._ig_find_media(fixture("ig_carousel.html"), "DJ4ZzePT6jQ")
        r = P._ig_build_resolved(P._ig_post_from_page(node), "DJ4ZzePT6jQ", "p")
        self.assertEqual(len(r["image_list"]), 3)
        self.assertEqual(r["videos"], [])
        mixed = P._ig_find_media(fixture("ig_carousel_video.html"), "DeKe6teEpya")
        r = P._ig_build_resolved(P._ig_post_from_page(mixed), "DeKe6teEpya", "p")
        self.assertEqual(len(r["videos"]) + len(r["image_list"]), 4)
        self.assertGreaterEqual(len(r["image_list"]), 1)
        self.assertEqual(r["media_count"], 4)
        # 备用链接（display_uri）只在和主链接不同时记录
        for primary, backup in r["image_fallbacks"].items():
            self.assertNotEqual(primary, backup)

    def test_ig_wrong_code_and_shell(self):
        self.assertIsNone(P._ig_find_media(fixture("ig_single.html"), "OtherCode123"))
        self.assertIsNone(P._ig_find_media(fixture("ig_shell.html"), "De9piNJlwES"))
        self.assertIsNone(P._ig_post_from_embed(fixture("ig_shell.html")))

    def test_ig_embed(self):
        post = P._ig_post_from_embed(fixture("ig_embed_carousel.html"))
        self.assertIsNotNone(post)
        self.assertEqual(post["source"], "embed")
        r = P._ig_build_resolved(post, "De5mVMikVB-", "p")
        self.assertEqual(len(r["image_list"]), 3)
        self.assertEqual(r["created_at_beijing"], "")  # embed 没有发布时间
        self.assertTrue(r["screen_name"])
        self.assertTrue(r["title"])

    def test_ig_oembed_fixture_shape(self):
        data = json.loads(fixture("ig_oembed.json"))
        self.assertTrue(data["title"] and data["author_name"] and data["thumbnail_url"].startswith("https://"))

    def test_fb_album(self):
        parsed = P._fb_parse_page(fixture("fb_album.html"))
        self.assertEqual(parsed["source"], "relay:Story")
        self.assertEqual(len([i for i in parsed["items"] if i["type"] == "image"]), 3)
        self.assertEqual(parsed["album_count"], 3)
        self.assertTrue(parsed["text"].startswith("Float into the weekend"))
        self.assertEqual(parsed["author"], "NASA - National Aeronautics and Space Administration")
        self.assertIsInstance(parsed["reactions"], int)
        self.assertIsInstance(parsed["comments"], int)
        self.assertIsInstance(parsed["shares"], int)
        self.assertEqual([c["text"] for c in parsed["comments_sample"]], ["第一条测试评论", "第二条测试评论"])
        r = run(P._fb_build_resolved(parsed, "https://www.facebook.com/NASA/posts/1658876782274358/"))
        self.assertEqual(len(r["image_list"]), 3)
        self.assertEqual(r["videos"], [])
        self.assertEqual(r["created_at_beijing"], "2026-10-17 04:05")  # 2026-10-16 20:05 UTC → +8h

    def test_fb_single_photo_drops_ctp_and_keeps_fallback(self):
        parsed = P._fb_parse_page(fixture("fb_single.html"))
        (item,) = [i for i in parsed["items"] if i["type"] == "image"]
        self.assertIn("ctp=p600x600", item["url"])
        self.assertNotIn("ctp=p600x600", item["url_full"])
        r = run(P._fb_build_resolved(parsed, "https://www.facebook.com/NASA/posts/1668207108007992/"))
        (best,) = r["image_list"]
        self.assertEqual(best, item["url_full"])
        self.assertEqual(r["image_fallbacks"], {best: item["url"]})  # 失败时回退带 ctp 的原链接

    def test_fb_text_only_and_link(self):
        r = run(P._fb_build_resolved(P._fb_parse_page(fixture("fb_text_only.html")), "https://www.facebook.com/zuck/posts/x/"))
        self.assertEqual(r["author"], "Mark Zuckerberg")
        self.assertTrue(r["title"])
        self.assertEqual((r["image_list"], r["videos"], r["cover"]), ([], [], None))
        r = run(P._fb_build_resolved(P._fb_parse_page(fixture("fb_link.html")), "https://www.facebook.com/bbcnews/posts/1/"))
        self.assertEqual(r["author"], "BBC News")
        self.assertEqual(len(r["image_list"]), 1)  # 链接预览图

    def test_fb_video_page(self):
        parsed = P._fb_parse_page(fixture("fb_video.html"))
        self.assertEqual(parsed["source"], "relay:video")
        (video,) = [i for i in parsed["items"] if i["type"] == "video"]
        self.assertTrue(video["dash_video_url"] and video["dash_audio_url"])
        self.assertNotIn("vp9", video["dash_video_url"])  # 选的是 H.264
        self.assertTrue(parsed["text"].startswith("We are not slowing down"))
        self.assertFalse(parsed.get("text_truncated"))  # 取到的是完整正文，不是 og 截断版
        self.assertEqual(parsed["created"], "2026-10-23T23:37:19+00:00")
        self.assertGreater(parsed["views"], 1000)

        async def fake_sources(_id):
            return {"hd": "https://v.example/hd.mp4", "sd": "https://v.example/sd.mp4"}

        orig = P._fb_video_sources
        P._fb_video_sources = fake_sources
        try:
            r = run(P._fb_build_resolved(parsed, "https://www.facebook.com/NASA/videos/1803909017420449/"))
        finally:
            P._fb_video_sources = orig
        self.assertEqual(len(r["videos"]), 1)
        self.assertEqual(r["videos"][0]["url"], "https://v.example/hd.mp4")  # 单文件直链优先
        self.assertTrue(r["videos"][0]["dash_video_url"])  # DASH 留作备用
        self.assertEqual(r["video_total"], 1)

    def test_fb_video_php_parsing(self):
        async def fake_fetch(url, *a, **k):
            return {"kind": "ok", "text": fixture("fb_video_php.html"), "url": url}

        orig = P._fetch_meta_html
        P._fetch_meta_html = fake_fetch
        try:
            sources = run(P._fb_video_sources("1803909017420449"))
        finally:
            P._fetch_meta_html = orig
        self.assertTrue(sources["hd"].startswith("https://") and sources["sd"].startswith("https://"))
        self.assertNotIn("\\", sources["hd"])  # \/ 转义已还原

    def test_fb_plugin_post_fallback_parsing(self):
        parsed = P._fb_parse_plugin_post(fixture("fb_post_php.html"))
        self.assertEqual(parsed["author"], "NASA - National Aeronautics and Space Administration")
        self.assertTrue(parsed["text"].startswith("Float into the weekend"))
        self.assertIn("\n\n", parsed["text"])  # <p> 段落还原成空行
        self.assertEqual(len(parsed["items"]), 3)
        self.assertEqual(parsed["created"], "2026-10-16T20:05:02+00:00")
        self.assertIsInstance(parsed["reactions"], int)
        self.assertEqual(parsed["comments_sample"][0]["author"], "测试用户A")

    def test_fb_garbage_page_does_not_raise(self):
        parsed = P._fb_parse_page("<html><body>nothing here</body></html>")
        self.assertEqual(parsed["items"], [])
        self.assertIsNone(parsed["text"])
        self.assertEqual(P._fb_parse_page(fixture("fb_empty.html"))["items"], [])


# ---------------------------------------------------------------- (c) 抓取层与回退链（离线，打补丁）


class _MockHttpx:
    """把 plugin.httpx.AsyncClient 换成 MockTransport，验证 _fetch_meta_html 的跳转 / 重试 / 错误分类。"""

    def __init__(self, handler):
        self.handler = handler
        self.requests: List[httpx.Request] = []
        self.client_kwargs: List[Dict[str, Any]] = []  # 每个 AsyncClient 的构造参数（用来验证 proxy 真的传进了 httpx）
        self._orig = None

    def __enter__(self):
        outer = self
        real = httpx.AsyncClient

        class Client(real):  # type: ignore[misc, valid-type]
            def __init__(self, *args: Any, **kwargs: Any):
                def handle(request: httpx.Request) -> httpx.Response:
                    outer.requests.append(request)
                    return outer.handler(request)

                outer.client_kwargs.append({k: v for k, v in kwargs.items() if k != "transport"})
                kwargs.pop("proxy", None)  # 已记录；真传给 httpx 会让它绕过 MockTransport 去连代理
                kwargs["transport"] = httpx.MockTransport(handle)
                super().__init__(*args, **kwargs)

        self._orig = real
        P.httpx.AsyncClient = Client
        self._orig_sleep = P.asyncio.sleep

        async def fast_sleep(_s):
            return None

        P.asyncio.sleep = fast_sleep
        return self

    def __exit__(self, *exc):
        P.httpx.AsyncClient = self._orig
        P.asyncio.sleep = self._orig_sleep


class FetchLayerOffline(unittest.TestCase):
    def test_honest_headers_and_ok(self):
        with _MockHttpx(lambda req: httpx.Response(200, text="<html>hello</html>")) as m:
            r = run(P._fetch_meta_html("https://www.instagram.com/p/abcde/"))
        self.assertEqual(r["kind"], "ok")
        request = m.requests[0]
        self.assertTrue(request.headers["user-agent"].startswith("nekro-media-forward/1.0"))
        self.assertNotIn("Mozilla", request.headers["user-agent"])
        self.assertEqual(request.headers["accept"], P._META_ACCEPT)
        self.assertNotIn("cookie", request.headers)
        self.assertNotIn("authorization", request.headers)

    def test_share_link_rewritten_to_posts(self):
        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path.startswith("/share/p/"):
                return httpx.Response(302, headers={"location": "https://www.facebook.com/story.php?story_fbid=1016&id=5337&rdid=x&share_url=y"})
            if req.url.path == "/5337/posts/1016":
                return httpx.Response(200, text="<html>post page</html>")
            return httpx.Response(302, headers={"location": "https://www.facebook.com/login/?next=x"})

        with _MockHttpx(handler) as m:
            r = run(P._fetch_meta_html("https://www.facebook.com/share/p/14pj5DHbCZG/"))
        self.assertEqual(r["kind"], "ok")
        self.assertEqual(r["url"], "https://www.facebook.com/5337/posts/1016")
        self.assertEqual(len(m.requests), 2)  # 没有先去请求 story.php（会被带去登录页）

    def test_login_redirects(self):
        for location in ("https://www.facebook.com/login/?next=x", "https://www.instagram.com/accounts/login/?next=x", "/login.php?next=x", "https://www.facebook.com/checkpoint/1/"):
            with self.subTest(location=location):
                with _MockHttpx(lambda req, loc=location: httpx.Response(302, headers={"location": loc})):
                    r = run(P._fetch_meta_html("https://www.facebook.com/photo.php?fbid=1"))
                self.assertEqual(r["kind"], "login")

    def test_status_classification(self):
        cases = {404: "missing", 410: "missing", 401: "login", 403: "login", 400: "error", 429: "retry", 500: "retry", 503: "retry"}
        for status, kind in cases.items():
            with self.subTest(status=status):
                with _MockHttpx(lambda req, s=status: httpx.Response(s, text="x")):
                    self.assertEqual(run(P._fetch_meta_html("https://www.facebook.com/a/posts/1"))["kind"], kind)

    def test_retry_then_success_and_empty_body(self):
        counter = {"n": 0}

        def flaky(req: httpx.Request) -> httpx.Response:
            counter["n"] += 1
            if counter["n"] == 1:
                return httpx.Response(200, text="")  # Facebook 偶发的 200 + 空正文
            return httpx.Response(200, text="<html>ok</html>")

        with _MockHttpx(flaky):
            r = run(P._fetch_meta_html("https://www.facebook.com/a/posts/1"))
        self.assertEqual((r["kind"], counter["n"]), ("ok", 2))
        counter["n"] = 0

        def always_empty(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="")

        with _MockHttpx(always_empty):
            self.assertEqual(run(P._fetch_meta_html("https://www.facebook.com/a/posts/1"))["kind"], "retry")

    def test_network_error_is_retry(self):
        def boom(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("t")

        with _MockHttpx(boom):
            r = run(P._fetch_meta_html("https://www.facebook.com/a/posts/1"))
        self.assertEqual(r["kind"], "retry")
        self.assertIn("ConnectTimeout", r["error"])

    def test_external_redirect_refused(self):
        with _MockHttpx(lambda req: httpx.Response(302, headers={"location": "https://evil.example.com/x"})):
            r = run(P._fetch_meta_html("https://www.facebook.com/a/posts/1"))
        self.assertEqual(r["kind"], "error")

    def test_redirect_loop_bounded(self):
        with _MockHttpx(lambda req: httpx.Response(302, headers={"location": "https://www.facebook.com/a/posts/2"})) as m:
            r = run(P._fetch_meta_html("https://www.facebook.com/a/posts/1"))
        self.assertEqual(r["kind"], "error")
        self.assertLessEqual(len(m.requests), 8)

    def test_rewrite_story_link(self):
        self.assertEqual(
            P._fb_rewrite_story_link("https://www.facebook.com/story.php?story_fbid=1&id=2&x=3"),
            "https://www.facebook.com/2/posts/1",
        )
        self.assertEqual(
            P._fb_rewrite_story_link("https://www.facebook.com/permalink.php?story_fbid=pfbid0a&id=2"),
            "https://www.facebook.com/2/posts/pfbid0a",
        )
        self.assertIsNone(P._fb_rewrite_story_link("https://www.facebook.com/story.php?story_fbid=1"))
        self.assertIsNone(P._fb_rewrite_story_link("https://www.facebook.com/NASA/posts/1"))


class _Router:
    """按 URL 片段返回不同的 _fetch_meta_html 结果，并记录每次调用的 (url, ua)。"""

    def __init__(self, routes: Dict[str, Any]):
        self.routes = routes
        self.calls: List[tuple] = []

    async def __call__(self, url: str, ua: str = P._META_UA, *a: Any, **k: Any) -> Dict[str, Any]:
        self.calls.append((url, ua))
        for needle, result in self.routes.items():
            if needle in url:
                value = result(ua) if callable(result) else result
                return {"url": url, **value}
        return {"kind": "error", "error": "no route", "url": url}


def ok(text: str) -> Dict[str, Any]:
    return {"kind": "ok", "text": text}


class FallbackChainOffline(unittest.TestCase):
    def setUp(self):
        self._orig_fetch = P._fetch_meta_html
        self._orig_ytdlp = P._run_ytdlp
        self.tmp = Path(tempfile.mkdtemp(prefix="nmf-igfb-chain-"))

    def tearDown(self):
        P._fetch_meta_html = self._orig_fetch
        P._run_ytdlp = self._orig_ytdlp
        P.plugin.config_override = {}
        shutil.rmtree(self.tmp, ignore_errors=True)

    def use(self, routes) -> _Router:
        router = _Router(routes)
        P._fetch_meta_html = router
        return router

    def fake_ytdlp(self, ok_result: bool = True):
        calls: List[str] = []
        self.ytdlp_proxies: List[Optional[str]] = []

        async def fake(url, task_dir, proxy=None):
            calls.append(url)
            self.ytdlp_proxies.append(proxy)
            if not ok_result:
                raise RuntimeError("服务器尚未安装 yt-dlp")
            f = task_dir / "video.mp4"
            f.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 32)
            return f, f.name

        P._run_ytdlp = fake
        return calls

    # ---- Instagram

    def test_ig_page_ok_is_first_choice(self):
        router = self.use({"/p/De9piNJlwES/embed": {"kind": "error"}, "/p/De9piNJlwES/": ok(fixture("ig_single.html"))})
        r = run(P._resolve_instagram(IG_SINGLE))
        self.assertTrue(r["ok"])
        self.assertEqual(r["source"], "page")
        self.assertEqual(len(router.calls), 1)  # 页面成功就不去碰 embed
        self.assertEqual(router.calls[0][1], P._META_UA)

    def test_ig_page_shell_then_embed(self):
        router = self.use({"/embed/captioned/": ok(fixture("ig_embed_carousel.html")), "/p/De5mVMikVB-/": ok(fixture("ig_shell.html"))})
        r = run(P._resolve_instagram("https://www.instagram.com/p/De5mVMikVB-/"))
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["source"], "embed")
        self.assertEqual(len(r["image_list"]), 3)
        # 默认开启 Googlebot 回退：诚实 UA 空壳 → Googlebot UA 也是空壳 → embed
        self.assertEqual(
            [url.split("/p/")[1] for url, _ in router.calls],
            ["De5mVMikVB-/", "De5mVMikVB-/", "De5mVMikVB-/embed/captioned/"],
        )

    def test_ig_page_shell_then_embed_with_fallback_ua_off(self):
        P.plugin.config_override = {"instagram_fallback_ua": False}
        router = self.use({"/embed/captioned/": ok(fixture("ig_embed_carousel.html")), "/p/De5mVMikVB-/": ok(fixture("ig_shell.html"))})
        r = run(P._resolve_instagram("https://www.instagram.com/p/De5mVMikVB-/"))
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["source"], "embed")
        self.assertEqual([url.split("/p/")[1] for url, _ in router.calls], ["De5mVMikVB-/", "De5mVMikVB-/embed/captioned/"])

    def test_ig_googlebot_ua_can_be_turned_off(self):
        """instagram_fallback_ua=false 时：只用诚实 UA，绝不发 Googlebot UA（关闭路径）。"""
        P.plugin.config_override = {"instagram_fallback_ua": False}
        router = self.use({"/embed/captioned/": ok(fixture("ig_shell.html")), "/p/": ok(fixture("ig_shell.html"))})
        r = run(P._resolve_instagram(IG_SINGLE))
        self.assertFalse(r["ok"])
        self.assertTrue(r["fallback"])
        self.assertTrue(all(ua == P._META_UA for _, ua in router.calls), router.calls)
        self.assertFalse(any("Googlebot" in ua for _, ua in router.calls))

    def test_ig_googlebot_ua_default_on_but_only_after_honest_ua_fails(self):
        """默认（不设任何配置）开启：先诚实 UA，失败（空壳）才换 Googlebot UA 再试一次。"""
        self.assertEqual(P.plugin.config_override, {})

        def by_ua(ua: str) -> Dict[str, Any]:
            return ok(fixture("ig_single.html")) if "Googlebot" in ua else ok(fixture("ig_shell.html"))

        router = self.use({"/embed/captioned/": {"kind": "error"}, "/p/De9piNJlwES/": by_ua})
        r = run(P._resolve_instagram(IG_SINGLE))
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["source"], "page(Googlebot UA)")
        self.assertEqual([ua == P._META_UA for _, ua in router.calls], [True, False])  # 先诚实 UA，失败才换

    def test_ig_googlebot_not_used_when_honest_ua_works(self):
        """默认开启也不会无故伪装：诚实 UA 成功时一次 Googlebot 请求都不发。"""
        router = self.use({"/p/De9piNJlwES/": ok(fixture("ig_single.html"))})
        r = run(P._resolve_instagram(IG_SINGLE))
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["source"], "page")
        self.assertEqual(len(router.calls), 1)
        self.assertFalse(any("Googlebot" in ua for _, ua in router.calls))

    def test_ig_googlebot_default_on_still_fails_cleanly(self):
        """默认开启时 Googlebot 也拿不到：会各试一次（诚实 UA + Googlebot），然后走 embed 回退，最终报 fallback。"""
        router = self.use({"/embed/captioned/": ok(fixture("ig_shell.html")), "/p/": ok(fixture("ig_shell.html"))})
        r = run(P._resolve_instagram(IG_SINGLE))
        self.assertFalse(r["ok"])
        self.assertTrue(r["fallback"])
        page_uas = [ua for url, ua in router.calls if "/embed/" not in url]
        self.assertEqual([("Googlebot" in ua) for ua in page_uas], [False, True])
        self.assertTrue(all(ua == P._META_UA for url, ua in router.calls if "/embed/" in url))  # embed 仍只用诚实 UA

    def test_ig_googlebot_not_used_after_login_redirect(self):
        router = self.use({"/embed/captioned/": {"kind": "error"}, "/p/": {"kind": "login"}})
        r = run(P._resolve_instagram(IG_SINGLE))
        self.assertFalse(r["ok"])
        self.assertFalse(any("Googlebot" in ua for _, ua in router.calls))

    def test_ig_story_clear_error_without_network(self):
        router = self.use({})
        r = run(P._resolve_instagram("https://www.instagram.com/stories/nasa/3801234567890123456/"))
        self.assertFalse(r["ok"])
        self.assertFalse(r.get("fallback"))
        self.assertIn("Stories", r["error"])
        self.assertIn("登录", r["error"])
        self.assertEqual(router.calls, [])
        with self.assertRaises(RuntimeError) as ctx:
            run(P._obtain_instagram("https://www.instagram.com/stories/nasa/3801234567890123456/", self.tmp))
        self.assertIn("Stories", str(ctx.exception))

    def test_ig_chain_oembed_404_is_definitive(self):
        self.use({"/api/v1/oembed/": {"kind": "missing"}, "/embed/captioned/": ok(fixture("ig_shell.html")), "/p/": ok(fixture("ig_shell.html"))})
        calls = self.fake_ytdlp()
        with self.assertRaises(RuntimeError) as ctx:
            run(P._obtain_instagram(IG_MISSING, self.tmp))
        self.assertIn("已被删除", str(ctx.exception))
        self.assertIn("私密", str(ctx.exception))
        self.assertEqual(calls, [])  # 确定不存在，就不去跑 yt-dlp

    def test_ig_chain_ytdlp_then_oembed_caption(self):
        self.use({"/api/v1/oembed/": ok(fixture("ig_oembed.json")), "/embed/captioned/": ok(fixture("ig_shell.html")), "/p/": ok(fixture("ig_shell.html"))})
        calls = self.fake_ytdlp()
        source = run(P._obtain_instagram(IG_REEL, self.tmp))
        self.assertEqual(calls, [IG_REEL])
        self.assertIsNotNone(source.path)
        self.assertIn("Instagram 帖子 · 视频", source.caption)  # oEmbed 的文字配上 yt-dlp 的视频
        self.assertIn("A close-knit family of stars", source.caption)
        self.assertIn("yt-dlp", source.caption)

    def test_ig_chain_oembed_only_text_and_thumbnail(self):
        self.use({"/api/v1/oembed/": ok(fixture("ig_oembed.json")), "/embed/captioned/": ok(fixture("ig_shell.html")), "/p/": ok(fixture("ig_shell.html"))})
        self.fake_ytdlp(ok_result=False)
        source = run(P._obtain_instagram(IG_SINGLE, self.tmp))
        self.assertIsNone(source.path)
        self.assertEqual(len(source.images), 1)
        self.assertTrue(source.images[0].startswith("https://"))
        self.assertIn("只取到了文字和缩略图", source.caption)
        self.assertIn("NASA", source.caption)

    def test_ig_chain_everything_fails(self):
        self.use({"/api/v1/oembed/": {"kind": "retry", "error": "HTTP 429"}, "/embed/captioned/": {"kind": "retry", "error": "HTTP 429"}, "/p/": {"kind": "retry", "error": "HTTP 429"}})
        self.fake_ytdlp(ok_result=False)
        with self.assertRaises(RuntimeError) as ctx:
            run(P._obtain_instagram(IG_SINGLE, self.tmp))
        message = str(ctx.exception)
        self.assertIn("429", message)
        self.assertIn("yt-dlp", message)

    # ---- Facebook

    def test_fb_main_page_ok(self):
        router = self.use({"/NASA/posts/1658876782274358": ok(fixture("fb_album.html"))})
        r = run(P._resolve_facebook(FB_ALBUM))
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["source"], "relay:Story")
        self.assertEqual(len(r["image_list"]), 3)
        self.assertEqual(len(router.calls), 1)
        self.assertEqual(router.calls[0][1], P._META_UA)

    def test_fb_no_ua_spoofing_anywhere(self):
        router = self.use({"/plugins/post.php": ok(fixture("fb_post_php.html")), "/NASA/posts/": {"kind": "retry", "error": "HTTP 500"}})
        run(P._resolve_facebook(FB_ALBUM))
        self.assertTrue(all(ua == P._META_UA for _, ua in router.calls))

    def test_fb_main_login_then_plugin_post(self):
        router = self.use({"/plugins/post.php": ok(fixture("fb_post_php.html")), "/photo.php": {"kind": "login"}})
        r = run(P._resolve_facebook("https://www.facebook.com/photo.php?fbid=1658876782274358"))
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["source"], "plugins/post.php")
        self.assertEqual(len(r["image_list"]), 3)
        self.assertTrue(r["title"].startswith("Float into the weekend"))
        self.assertEqual([("/plugins/post.php" in url) for url, _ in router.calls], [False, True])

    def test_fb_video_url_falls_back_to_video_php(self):
        self.use({"/plugins/video.php": ok(fixture("fb_video_php.html")), "/plugins/post.php": {"kind": "retry", "error": "HTTP 500"}, "/NASA/videos/": {"kind": "retry", "error": "HTTP 500"}})
        r = run(P._resolve_facebook(FB_VIDEO))
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(r["videos"]), 1)
        self.assertTrue(r["videos"][0]["url"].startswith("https://"))
        self.assertEqual(r["title"], "")  # 只拿到视频，没有正文

    def test_fb_transient_everything_marks_fallback_and_uses_ytdlp(self):
        self.use({"/": {"kind": "retry", "error": "HTTP 500"}})
        r = run(P._resolve_facebook(FB_ALBUM))
        self.assertFalse(r["ok"])
        self.assertTrue(r["fallback"])
        calls = self.fake_ytdlp()
        source = run(P._obtain_facebook(FB_ALBUM, self.tmp))
        self.assertEqual(calls, [FB_ALBUM])
        self.assertEqual(source.caption, "")  # 回退路径拿不到正文
        self.fake_ytdlp(ok_result=False)
        with self.assertRaises(RuntimeError) as ctx:
            run(P._obtain_facebook(FB_ALBUM, self.tmp))
        self.assertIn("500", str(ctx.exception))
        self.assertIn("yt-dlp", str(ctx.exception))

    def test_fb_login_is_definitive_and_skips_ytdlp(self):
        self.use({"/": {"kind": "login"}})
        calls = self.fake_ytdlp()
        with self.assertRaises(RuntimeError) as ctx:
            run(P._obtain_facebook("https://www.facebook.com/photo.php?fbid=1", self.tmp))
        self.assertIn("需要登录", str(ctx.exception))
        self.assertIn("不登录", str(ctx.exception))
        self.assertEqual(calls, [])

    def test_fb_og_login_prompt_is_login_error(self):
        self.use({"/": ok(fixture("fb_login.html"))})
        r = run(P._resolve_facebook(FB_ALBUM))
        self.assertFalse(r["ok"])
        self.assertFalse(r.get("fallback"))
        self.assertIn("需要登录", r["error"])

    def test_fb_empty_page_is_not_found(self):
        self.use({"/": ok(fixture("fb_empty.html"))})
        r = run(P._resolve_facebook(FB_ALBUM))
        self.assertFalse(r["ok"])
        self.assertFalse(r.get("fallback"))
        self.assertIn("没有找到", r["error"])

    def test_fb_watch_short_link_exact_message_and_no_network(self):
        router = self.use({})
        r = run(P._resolve_facebook("https://fb.watch/Ipm19Wy4Le/"))
        self.assertFalse(r["ok"])
        self.assertTrue(r["error"].startswith("暂不支持 fb.watch 短链，请发完整链接"))
        self.assertEqual(router.calls, [])
        with self.assertRaises(RuntimeError) as ctx:
            run(P._obtain_facebook("https://fb.watch/Ipm19Wy4Le/", self.tmp))
        self.assertTrue(str(ctx.exception).startswith("暂不支持 fb.watch 短链，请发完整链接"))

    def test_fb_group_error(self):
        router = self.use({})
        r = run(P._resolve_facebook("https://www.facebook.com/groups/123456789/posts/987654321/"))
        self.assertFalse(r["ok"])
        self.assertIn("群组", r["error"])
        self.assertIn("登录", r["error"])
        self.assertEqual(router.calls, [])

    def test_fb_unrecognized_url(self):
        r = run(P._resolve_facebook("https://www.facebook.com/NASA"))
        self.assertFalse(r["ok"])
        self.assertIn("认出", r["error"])


# ---------------------------------------------------------------- (d) 下载 / 发送链路（离线，假下载 + 假 NapCat）


def _jpeg(width: int = 8, height: int = 8) -> bytes:
    # 最小的带 SOF0 的 JPEG 头（只用来过魔数 / 尺寸解析，不要求能解码）
    return b"\xff\xd8\xff\xc0\x00\x11\x08" + struct.pack(">HH", height, width) + b"\x03" + b"\x01\x22\x00\x02\x11\x01\x03\x11\x01" + b"\xff\xd9"


class _FakeDownloads:
    """替换 plugin._download_direct：按 URL 决定写什么 / 失败。"""

    def __init__(self, fail_substrings=(), payloads: Optional[Dict[str, bytes]] = None):
        self.fail = tuple(fail_substrings)
        self.payloads = payloads or {}
        self.calls: List[tuple] = []
        self.proxies: List[Optional[str]] = []

    async def __call__(self, url: str, target: Path, max_bytes: int, headers: Dict[str, str], proxy: Optional[str] = None) -> Path:
        self.calls.append((url, target.name, dict(headers)))
        self.proxies.append(proxy)
        if any(token in url for token in self.fail):
            raise RuntimeError("HTTP 403")
        data = next((payload for needle, payload in self.payloads.items() if needle in url), None)
        if data is None:
            data = b"\x00\x00\x00\x18ftypmp42" + b"0" * 64 if url.split("?")[0].endswith(".mp4") else _jpeg()
        target.write_bytes(data)
        return target


class ProcessOffline(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nmf-igfb-proc-"))
        self._old = (P._VIDEO_DIR, P.get_bot, P._download_direct, P._fetch_meta_html, P._fb_video_sources)
        P._VIDEO_DIR = self.tmp
        P.plugin.config_override = {"cooldown_seconds": 0, "public_base_url": "http://napcat.test:8022"}
        P._RECENT_URLS.clear()
        P._LAST_CHAT_REQUEST.clear()
        P._LEASES.clear()
        self.bot = _FakeBot()
        P.get_bot = lambda: self.bot

        async def fake_sources(video_id):
            return {"hd": f"https://video.example/{video_id}-hd.mp4", "sd": f"https://video.example/{video_id}-sd.mp4"}

        P._fb_video_sources = fake_sources

    def tearDown(self):
        P._VIDEO_DIR, P.get_bot, P._download_direct, P._fetch_meta_html, P._fb_video_sources = self._old
        P.plugin.config_override = {}
        shutil.rmtree(self.tmp, ignore_errors=True)

    def content(self) -> list:
        self.assertEqual(len(self.bot.calls), 1, self.bot.calls)
        call = self.bot.calls[0]
        self.assertEqual(call["api"], "send_group_forward_msg")
        (node,) = call["messages"]
        return node["data"]["content"]

    def test_ig_carousel_text_then_images(self):
        P._fetch_meta_html = _Router({"/p/": ok(fixture("ig_carousel.html"))})
        downloads = P._download_direct = _FakeDownloads()
        ctx = _FakeCtx()
        result = run(P._process(ctx, "https://www.instagram.com/p/DJ4ZzePT6jQ/?igsh=x"))
        content = self.content()
        self.assertEqual(result, "图文已通过合并转发发回原聊天（3 张图片）。")
        self.assertEqual([seg["type"] for seg in content], ["text", "image", "image", "image"])
        self.assertIn("Instagram 帖子 · 图文（3 张）", content[0]["data"]["text"])
        self.assertIn("@natgeo", content[0]["data"]["text"])
        self.assertIn("链接：https://www.instagram.com/p/DJ4ZzePT6jQ/", content[0]["data"]["text"])
        self.assertIn("正在解析Instagram内容", ctx.sent[0])
        # 下载图片用的是诚实 UA + instagram Referer
        self.assertTrue(all(call[2]["User-Agent"] == P._META_UA for call in downloads.calls))
        self.assertTrue(all(call[2]["Referer"] == "https://www.instagram.com/" for call in downloads.calls))

    def test_ig_reel_text_then_video(self):
        P._fetch_meta_html = _Router({"/p/": ok(fixture("ig_reel.html"))})
        P._download_direct = _FakeDownloads()
        result = run(P._process(_FakeCtx(), IG_REEL))
        content = self.content()
        self.assertEqual(result, "视频已通过合并转发发回原聊天。")
        self.assertEqual([seg["type"] for seg in content], ["text", "video"])
        self.assertIn("Instagram 帖子 · 视频", content[0]["data"]["text"])
        lease = P._LEASES[content[1]["data"]["file"].rsplit("/", 1)[1]]
        self.assertTrue(_sniff_mp4(lease.path))

    def test_ig_mixed_carousel_images_then_video(self):
        P._fetch_meta_html = _Router({"/p/": ok(fixture("ig_carousel_video.html"))})
        P._download_direct = _FakeDownloads()
        result = run(P._process(_FakeCtx(), "https://www.instagram.com/p/DeKe6teEpya/"))
        types = [seg["type"] for seg in self.content()]
        self.assertEqual(types[0], "text")
        self.assertEqual(types[-1], "video")
        self.assertTrue(all(t == "image" for t in types[1:-1]))
        self.assertIn("图文和视频已通过合并转发发回原聊天", result)

    def test_image_cap_and_note(self):
        P.plugin.config_override["max_images"] = 2
        P._fetch_meta_html = _Router({"/p/": ok(fixture("ig_carousel.html"))})
        P._download_direct = _FakeDownloads()
        run(P._process(_FakeCtx(), IG_CAROUSEL))
        content = self.content()
        self.assertEqual([seg["type"] for seg in content], ["text", "image", "image"])
        self.assertIn("共 3 张图片，仅发送前 2 张", content[0]["data"]["text"])

    def test_image_extension_sniffed(self):
        P._fetch_meta_html = _Router({"/p/": ok(fixture("ig_carousel.html"))})
        P._download_direct = _FakeDownloads(payloads={"": b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"0" * 32})
        run(P._process(_FakeCtx(), IG_CAROUSEL))
        names = [lease.download_name for lease in P._LEASES.values()]
        self.assertEqual(len(names), 3)
        self.assertTrue(all(name.endswith(".webp") for name in names), names)
        self.assertTrue(all(lease.path.suffix == ".webp" for lease in P._LEASES.values()))

    def test_fb_single_ctp_failure_falls_back_to_original_url(self):
        P._fetch_meta_html = _Router({"/NASA/posts/1668207108007992": ok(fixture("fb_single.html"))})
        downloads = P._download_direct = _FakeDownloads(fail_substrings=[], payloads={})
        parsed_item = [i for i in P._fb_parse_page(fixture("fb_single.html"))["items"] if i["type"] == "image"][0]
        # 去掉 ctp 的链接让它 403：必须改用带 ctp 的原链接，而不是整条失败
        original_call = downloads.__call__

        async def flaky(url, target, max_bytes, headers, proxy=None):
            if "ctp=" not in url:
                downloads.calls.append((url, target.name, dict(headers)))
                raise RuntimeError("HTTP 403")
            return await original_call(url, target, max_bytes, headers, proxy)

        P._download_direct = flaky
        result = run(P._process(_FakeCtx(), FB_SINGLE))
        content = self.content()
        self.assertEqual([seg["type"] for seg in content], ["text", "image"])
        self.assertIn("已通过合并转发", result)
        self.assertTrue(any("ctp=" in call[0] for call in downloads.calls))
        self.assertEqual(parsed_item["url_full"] in [c[0] for c in downloads.calls], True)

    def test_fb_album_order_and_caption(self):
        P._fetch_meta_html = _Router({"/NASA/posts/1658876782274358": ok(fixture("fb_album.html"))})
        P._download_direct = _FakeDownloads()
        run(P._process(_FakeCtx(), FB_ALBUM))
        content = self.content()
        self.assertEqual([seg["type"] for seg in content], ["text", "image", "image", "image"])
        text = content[0]["data"]["text"]
        self.assertIn("Facebook 帖子 · 图文（3 张）", text)
        self.assertIn("NASA - National Aeronautics and Space Administration", text)
        self.assertIn("（北京时间）", text)
        self.assertNotIn("测试评论", text)  # facebook_send_comments 默认关闭

    def test_fb_send_comments_option(self):
        P.plugin.config_override["facebook_send_comments"] = True
        P._fetch_meta_html = _Router({"/NASA/posts/1658876782274358": ok(fixture("fb_album.html"))})
        P._download_direct = _FakeDownloads()
        run(P._process(_FakeCtx(), FB_ALBUM))
        text = self.content()[0]["data"]["text"]
        self.assertIn("第一条测试评论", text)
        self.assertIn("测试用户A", text)

    def test_fb_text_only(self):
        P._fetch_meta_html = _Router({"/zuck/posts/": ok(fixture("fb_text_only.html"))})
        downloads = P._download_direct = _FakeDownloads()
        result = run(P._process(_FakeCtx(), FB_TEXT))
        content = self.content()
        self.assertEqual(result, "帖子文字已通过合并转发发回原聊天。")
        self.assertEqual([seg["type"] for seg in content], ["text"])
        self.assertIn("Facebook 帖子 · 纯文字", content[0]["data"]["text"])
        self.assertEqual(downloads.calls, [])

    def test_fb_video_prefers_progressive_hd(self):
        P._fetch_meta_html = _Router({"/watch/": ok(fixture("fb_video.html"))})
        downloads = P._download_direct = _FakeDownloads()
        result = run(P._process(_FakeCtx(), FB_WATCH))
        content = self.content()
        self.assertEqual(result, "视频已通过合并转发发回原聊天。")
        self.assertEqual([seg["type"] for seg in content], ["text", "video"])
        self.assertEqual([c[0] for c in downloads.calls], ["https://video.example/1803909017420449-hd.mp4"])
        self.assertIn("Facebook 帖子 · 视频", content[0]["data"]["text"])
        self.assertIn("观看", content[0]["data"]["text"])

    def test_multi_video_post_sends_all_videos(self):
        resolved = {
            "ok": True, "platform": "instagram", "note_id": "ABCDE", "title": "两个视频", "author": "a", "screen_name": "a",
            "canonical_url": "https://www.instagram.com/p/ABCDE/", "created_at_beijing": "", "likes": None, "replies": None,
            "image_list": [], "videos": [{"label": "v1", "url": "https://v/1.mp4"}, {"label": "v2", "url": "https://v/2.mp4"}],
        }

        async def fake_resolve(_url):
            return resolved

        old = P._resolve_instagram
        P._resolve_instagram = fake_resolve
        P._download_direct = _FakeDownloads()
        try:
            run(P._process(_FakeCtx(), "https://www.instagram.com/p/ABCDE/"))
        finally:
            P._resolve_instagram = old
        content = self.content()
        self.assertEqual([seg["type"] for seg in content], ["text", "video", "video"])
        files = [seg["data"]["file"] for seg in content[1:]]
        self.assertEqual(len(set(files)), 2)  # 两个视频是两个独立的临时链接，没有互相覆盖
        paths = [P._LEASES[f.rsplit("/", 1)[1]].path for f in files]
        self.assertEqual(len(set(paths)), 2)

    def test_failure_messages_are_chinese_and_nothing_sent(self):
        P._fetch_meta_html = _Router({"/": {"kind": "login"}})
        result = run(P._process(_FakeCtx(), "https://www.facebook.com/photo.php?fbid=123"))
        self.assertTrue(result.startswith("视频未能发送："), result)
        self.assertIn("需要登录", result)
        result = run(P._process(_FakeCtx(), "https://www.instagram.com/stories/nasa/1/"))
        self.assertIn("Stories", result)
        P._RECENT_URLS.clear()
        result = run(P._process(_FakeCtx(), "https://fb.watch/abc123/"))
        self.assertIn("暂不支持 fb.watch 短链，请发完整链接", result)
        self.assertEqual(self.bot.calls, [])

    def test_download_failure_reported(self):
        P._fetch_meta_html = _Router({"/p/": ok(fixture("ig_reel.html"))})
        P._download_direct = _FakeDownloads(fail_substrings=[".mp4"])
        result = run(P._process(_FakeCtx(), IG_REEL))
        self.assertTrue(result.startswith("视频未能发送："), result)
        self.assertIn("Instagram直链下载失败", result)
        self.assertEqual(self.bot.calls, [])

    def test_video_size_limit_is_platform_specific(self):
        seen: List[int] = []

        async def capture(url, target, max_bytes, headers, proxy=None):
            seen.append(max_bytes)
            target.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 64)
            return target

        P.plugin.config_override.update({"instagram_max_size_mb": 7, "facebook_max_size_mb": 9})
        P._download_direct = capture
        P._fetch_meta_html = _Router({"/p/": ok(fixture("ig_reel.html")), "/watch/": ok(fixture("fb_video.html"))})
        run(P._process(_FakeCtx(), IG_REEL))
        self.bot.calls.clear()
        P._RECENT_URLS.clear()
        run(P._process(_FakeCtx(), FB_WATCH))
        self.assertEqual(seen, [7 * 1024 * 1024, 9 * 1024 * 1024])

    @unittest.skipUnless(FFMPEG and FFPROBE, "需要 ffmpeg / ffprobe")
    def test_fb_dash_merge_with_real_ffmpeg(self):
        """FB 单文件直链不可用时走 DASH：下两路流，用 ffmpeg（沿用 B 站的调用）合并，结果要同时有视频流和音频流。"""
        src = self.tmp / "src"
        src.mkdir()
        vfile, afile = src / "v.mp4", src / "a.mp4"
        subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(vfile)], check=True)
        subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "aac", str(afile)], check=True)
        P._download_direct = _FakeDownloads(payloads={"v-stream": vfile.read_bytes(), "a-stream": afile.read_bytes()})
        entry = {"url": None, "id": "42", "dash_video_url": "https://v/v-stream.mp4", "dash_audio_url": "https://v/a-stream.mp4"}
        source = run(P._download_meta_video(entry, "facebook", {"note_id": "42"}, self.tmp / "dash", 50 * 1024 * 1024))
        streams = ffprobe_streams(source.path)
        self.assertEqual(streams["video"]["codec_name"], "h264")
        self.assertEqual(streams["audio"]["codec_name"], "aac")
        self.assertEqual((streams["video"]["width"], streams["video"]["height"]), (320, 240))

    def test_fb_progressive_failure_falls_back_to_dash(self):
        calls: List[str] = []
        entry = {"url": "https://v/prog.mp4", "id": "42", "dash_video_url": "https://v/v-stream.mp4", "dash_audio_url": None}

        async def fake(url, target, max_bytes, headers, proxy=None):
            calls.append(url)
            if "prog" in url:
                raise RuntimeError("HTTP 403")
            target.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 64)
            return target

        P._download_direct = fake
        source = run(P._download_meta_video(entry, "facebook", {"note_id": "42"}, self.tmp / "d2", 10 * 1024 * 1024))
        self.assertEqual(calls, ["https://v/prog.mp4", "https://v/v-stream.mp4"])  # 没有音轨：直接发视频流，不需要 ffmpeg
        self.assertTrue(source.path.exists())
        # Instagram 没有 DASH 备用：直链失败就直接报错
        with self.assertRaises(RuntimeError) as ctx:
            run(P._download_meta_video({"url": "https://v/prog.mp4"}, "instagram", {"note_id": "x"}, self.tmp / "d3", 10 * 1024 * 1024))
        self.assertIn("Instagram直链下载失败", str(ctx.exception))


# ---------------------------------------------------------------- (e) 回归：别的平台 / 状态 / 自检路由（离线）


class RegressionOffline(unittest.TestCase):
    def setUp(self):
        self.bot = _FakeBot()
        self._old = P.get_bot
        P.get_bot = lambda: self.bot

    def tearDown(self):
        P.get_bot = self._old
        P.plugin.config_override = {}

    def test_other_platforms_send_forward_byte_identical(self):
        run(P._send_forward(_FakeCtx(), "http://h/v.mp4", "v.mp4", "https://www.douyin.com/video/1"))
        self.assertEqual(self.bot.calls[-1]["messages"][0]["data"]["content"], [
            {"type": "text", "data": {"text": "抖音 视频\n链接：https://www.douyin.com/video/1"}},
            {"type": "video", "data": {"file": "http://h/v.mp4", "name": "v.mp4"}},
        ])
        run(P._send_forward(_FakeCtx(), None, "n", "https://www.xiaohongshu.com/explore/1", images=["http://h/1.jpg"]))
        self.assertEqual(self.bot.calls[-1]["messages"][0]["data"]["content"], [
            {"type": "text", "data": {"text": "小红书 图文（1 张）\n链接：https://www.xiaohongshu.com/explore/1"}},
            {"type": "image", "data": {"file": "http://h/1.jpg"}},
        ])
        run(P._send_forward(_FakeCtx(), "http://h/b.mp4", "b.mp4", "https://www.bilibili.com/video/BV1GJ411x7h7"))
        self.assertEqual(self.bot.calls[-1]["messages"][0]["data"]["content"][0]["data"]["text"], "B站 视频\n链接：https://www.bilibili.com/video/BV1GJ411x7h7")
        # X：带 caption 的老用法（图 + 视频）不变；不传 extra_videos 就和以前一样
        run(P._send_forward(_FakeCtx(), "http://h/v.mp4", "v.mp4", "https://x.com/a/status/1", images=["http://h/1.jpg"], caption="X 帖子 · 视频\n正文"))
        self.assertEqual([s["type"] for s in self.bot.calls[-1]["messages"][0]["data"]["content"]], ["text", "image", "video"])

    def test_extra_videos_appended_after_main_video(self):
        run(P._send_forward(_FakeCtx(), "http://h/1.mp4", "1.mp4", "https://www.instagram.com/p/ABCDE/", images=["http://h/i.jpg"], caption="cap", extra_videos=[("http://h/2.mp4", "2.mp4"), ("http://h/3.mp4", "3.mp4")]))
        content = self.bot.calls[-1]["messages"][0]["data"]["content"]
        self.assertEqual([s["type"] for s in content], ["text", "image", "video", "video", "video"])
        self.assertEqual([s["data"]["name"] for s in content[2:]], ["1.mp4", "2.mp4", "3.mp4"])

    def test_default_header_when_no_caption(self):
        run(P._send_forward(_FakeCtx(), "http://h/v.mp4", "v.mp4", "https://www.instagram.com/reel/ABCDE/"))
        self.assertEqual(self.bot.calls[-1]["messages"][0]["data"]["content"][0]["data"]["text"], "Instagram 视频\n链接：https://www.instagram.com/reel/ABCDE/")

    def test_status_endpoint_lists_instagram_facebook(self):
        router = P.create_router()
        endpoint = next(r.endpoint for r in router.routes if getattr(r, "path", "") == "/")
        data = run(endpoint())
        self.assertEqual(data["version"], "0.11.0")
        for key in ("douyin", "xiaohongshu", "tiktok", "bilibili", "x", "instagram", "facebook"):
            self.assertIn(key, data["supported"])
        self.assertTrue(data["instagram"]["enabled"])
        self.assertTrue(data["facebook"]["enabled"])
        self.assertTrue(data["instagram"]["fallback_ua"])  # 默认开启
        self.assertFalse(data["instagram"]["proxy_configured"])
        self.assertFalse(data["facebook"]["proxy_configured"])
        self.assertEqual(data["instagram"]["max_size_mb"], 100)
        self.assertFalse(data["facebook"]["send_comments"])
        self.assertTrue(data["x"]["enabled"])  # X 的状态项还在
        P.plugin.config_override = {"instagram_enabled": False}
        self.assertFalse(run(endpoint())["instagram"]["enabled"])

    def _self_test(self):
        router = P.create_router()
        return next(r.endpoint for r in router.routes if getattr(r, "path", "") == "/resolver/self-test")

    def test_self_test_supports_instagram_and_facebook_offline(self):
        P.plugin.config_override = {"resolver_token": "unit-test-secret"}
        endpoint = self._self_test()
        old = (P._fetch_meta_html, P._download_direct, P._fb_video_sources)
        P._fetch_meta_html = _Router({"/p/": ok(fixture("ig_carousel_video.html")), "/NASA/posts/1658876782274358": ok(fixture("fb_album.html"))})
        P._download_direct = _FakeDownloads()

        async def fake_sources(_id):
            return {"hd": "https://v/hd.mp4", "sd": None}

        P._fb_video_sources = fake_sources
        try:
            ig = run(endpoint(token="unit-test-secret", url="https://www.instagram.com/p/DeKe6teEpya/", download=True))
            fb = run(endpoint(token="unit-test-secret", url=FB_ALBUM, download=True))
            no_download = run(endpoint(token="unit-test-secret", url=FB_ALBUM, download=False))
        finally:
            P._fetch_meta_html, P._download_direct, P._fb_video_sources = old
        self.assertTrue(ig["ok"], ig)
        self.assertEqual(ig["platform"], "instagram")
        self.assertEqual(ig["image_count"] + ig["video_count"], 4)
        self.assertIn("downloaded_mb", ig)  # 视频真下了
        self.assertIn("first_image_mb", ig)  # 图也真下了
        self.assertTrue(fb["ok"], fb)
        self.assertEqual((fb["platform"], fb["image_count"]), ("facebook", 3))
        self.assertIn("first_image_mb", fb)
        self.assertTrue(no_download["ok"])
        self.assertNotIn("first_image_mb", no_download)

    def test_self_test_rejects_bad_token_and_unsupported(self):
        P.plugin.config_override = {"resolver_token": "unit-test-secret"}
        endpoint = self._self_test()
        with self.assertRaises(Exception):
            run(endpoint(token="wrong", url=IG_REEL, download=False))
        r = run(endpoint(token="unit-test-secret", url="https://example.com/x", download=False))
        self.assertFalse(r["ok"])
        self.assertIn("Instagram", r["error"])


# ---------------------------------------------------------------- 代理（离线：验证参数真的传入 httpx / yt-dlp，不需要真实代理）

IG_PROXY = "http://proxy-ig.test:7891"
FB_PROXY = "http://proxy-fb.test:7892"
X_PROXY = "http://proxy-x.test:7893"


class ProxyOffline(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nmf-igfb-proxy-"))
        self._old = (P._VIDEO_DIR, P.get_bot, P._fetch_meta_html, P._fb_video_sources, P._download_direct)
        P._VIDEO_DIR = self.tmp
        self.base = {"cooldown_seconds": 0, "public_base_url": "http://napcat.test:8022"}
        P.plugin.config_override = dict(self.base)
        P._RECENT_URLS.clear()
        P._LAST_CHAT_REQUEST.clear()
        P._LEASES.clear()
        self.bot = _FakeBot()
        P.get_bot = lambda: self.bot

    def tearDown(self):
        P._VIDEO_DIR, P.get_bot, P._fetch_meta_html, P._fb_video_sources, P._download_direct = self._old
        P.plugin.config_override = {}
        shutil.rmtree(self.tmp, ignore_errors=True)

    def configure(self, **cfg):
        P.plugin.config_override = {**self.base, **cfg}

    # ---- 页面请求（_fetch_meta_html）

    def fetch_kwargs(self, url: str, **cfg) -> Dict[str, Any]:
        self.configure(**cfg)
        with _MockHttpx(lambda req: httpx.Response(200, text="<html>ok</html>")) as m:
            self.assertEqual(run(P._fetch_meta_html(url))["kind"], "ok")
        (kwargs,) = m.client_kwargs
        return kwargs

    def test_page_fetch_uses_instagram_proxy(self):
        kwargs = self.fetch_kwargs("https://www.instagram.com/p/abcde/", instagram_proxy=f"  {IG_PROXY}  ", facebook_proxy=FB_PROXY, x_proxy=X_PROXY)
        self.assertEqual(kwargs["proxy"], IG_PROXY)  # 去掉首尾空白，且不会用成 Facebook / X 的代理

    def test_page_fetch_uses_facebook_proxy(self):
        kwargs = self.fetch_kwargs("https://www.facebook.com/NASA/posts/1", instagram_proxy=IG_PROXY, facebook_proxy=FB_PROXY, x_proxy=X_PROXY)
        self.assertEqual(kwargs["proxy"], FB_PROXY)

    def test_page_fetch_without_proxy_is_unchanged(self):
        for url in ("https://www.instagram.com/p/abcde/", "https://www.facebook.com/NASA/posts/1"):
            with self.subTest(url=url):
                self.assertNotIn("proxy", self.fetch_kwargs(url))  # 默认：不传 proxy，行为和以前一样
                # 只配了别的平台的代理，也不影响这一家
                self.assertNotIn("proxy", self.fetch_kwargs(url, x_proxy=X_PROXY, **({"facebook_proxy": FB_PROXY} if "instagram" in url else {"instagram_proxy": IG_PROXY})))

    def test_page_fetch_redirect_hops_keep_proxy(self):
        self.configure(facebook_proxy=FB_PROXY)

        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path.startswith("/share/p/"):
                return httpx.Response(302, headers={"location": "https://www.facebook.com/NASA/posts/1"})
            return httpx.Response(200, text="<html>ok</html>")

        with _MockHttpx(handler) as m:
            self.assertEqual(run(P._fetch_meta_html("https://www.facebook.com/share/p/abc/"))["kind"], "ok")
        self.assertEqual(len(m.client_kwargs), 2)
        self.assertTrue(all(k.get("proxy") == FB_PROXY for k in m.client_kwargs))

    # ---- 映射规则

    def test_proxy_routing_rules(self):
        self.configure(instagram_proxy=IG_PROXY, facebook_proxy=FB_PROXY, x_proxy=X_PROXY)
        self.assertEqual(P._proxy_for_url("https://scontent.cdninstagram.com/v/a.jpg"), IG_PROXY)
        self.assertEqual(P._proxy_for_url("https://www.instagram.com/p/x/"), IG_PROXY)
        self.assertEqual(P._proxy_for_url("https://www.facebook.com/a/posts/1"), FB_PROXY)
        self.assertEqual(P._proxy_for_url("https://video.twimg.com/a.mp4"), X_PROXY)  # 上游 X 的规则不变
        self.assertIsNone(P._proxy_for_url("https://api.bilibili.com/x"))  # 其它平台保持直连
        self.assertIsNone(P._proxy_for_url("https://example.com/"))
        self.assertEqual((P._meta_proxy("instagram"), P._meta_proxy("facebook"), P._meta_proxy("x"), P._meta_proxy(None)), (IG_PROXY, FB_PROXY, None, None))
        self.configure(x_proxy=X_PROXY)  # 只配了 x_proxy：IG / FB 不能被偷偷代理
        for url in ("https://www.instagram.com/p/x/", "https://scontent.cdninstagram.com/a.jpg", "https://www.facebook.com/a/posts/1", "https://scontent.xx.fbcdn.net/a.jpg"):
            self.assertIsNone(P._proxy_for_url(url), url)
        self.assertEqual(P._proxy_for_url("https://x.com/jack/status/20"), X_PROXY)

    # ---- 媒体下载（真实 _download_direct）

    def download_kwargs(self, url: str, **kw) -> Dict[str, Any]:
        with _MockHttpx(lambda req: httpx.Response(200, content=_jpeg())) as m:
            run(P._download_direct(url, self.tmp / "a.jpg", 10 * 1024 * 1024, {"User-Agent": P._META_UA}, **kw))
        (kwargs,) = m.client_kwargs
        return kwargs

    def test_download_direct_passes_proxy_to_httpx(self):
        self.configure(instagram_proxy=IG_PROXY, facebook_proxy=FB_PROXY)
        self.assertEqual(self.download_kwargs("https://scontent.cdninstagram.com/a.jpg")["proxy"], IG_PROXY)
        self.assertEqual(self.download_kwargs("https://scontent.xx.fbcdn.net/a.jpg", proxy=FB_PROXY)["proxy"], FB_PROXY)  # fbcdn：调用点按平台显式传
        self.assertEqual(self.download_kwargs("https://scontent.xx.fbcdn.net/a.jpg", proxy=IG_PROXY)["proxy"], IG_PROXY)
        self.configure()
        self.assertNotIn("proxy", self.download_kwargs("https://scontent.cdninstagram.com/a.jpg"))  # 未配置：行为不变
        self.assertNotIn("proxy", self.download_kwargs("https://scontent.xx.fbcdn.net/a.jpg", proxy=P._meta_proxy("facebook")))

    def run_process(self, url: str, routes: Dict[str, Any], **cfg) -> _MockHttpx:
        """真实的 _process + 真实的 _download_direct，只把 httpx 换成离线的 MockTransport。"""
        self.configure(**cfg)
        P._fetch_meta_html = _Router(routes)
        P._RECENT_URLS.clear()

        async def fake_sources(video_id):
            return {"hd": f"https://video.fbcdn.example/{video_id}-hd.mp4", "sd": None}

        P._fb_video_sources = fake_sources

        def handler(req: httpx.Request) -> httpx.Response:
            if str(req.url).endswith(".mp4") or ".mp4?" in str(req.url):
                return httpx.Response(200, content=b"\x00\x00\x00\x18ftypmp42" + b"0" * 64)
            return httpx.Response(200, content=_jpeg())

        with _MockHttpx(handler) as m:
            result = run(P._process(_FakeCtx(), url))
        self.assertTrue("合并转发" in result, result)
        return m

    def test_process_instagram_downloads_use_instagram_proxy(self):
        m = self.run_process(IG_CAROUSEL, {"/p/": ok(fixture("ig_carousel.html"))}, instagram_proxy=IG_PROXY, facebook_proxy=FB_PROXY)
        self.assertGreaterEqual(len(m.client_kwargs), 2)  # 轮播里每张图一次下载
        self.assertTrue(all(k.get("proxy") == IG_PROXY for k in m.client_kwargs), m.client_kwargs)

    def test_process_facebook_images_use_facebook_proxy(self):
        m = self.run_process(FB_ALBUM, {"/NASA/posts/1658876782274358": ok(fixture("fb_album.html"))}, instagram_proxy=IG_PROXY, facebook_proxy=FB_PROXY)
        self.assertEqual(len(m.client_kwargs), 3)
        self.assertTrue(all(k.get("proxy") == FB_PROXY for k in m.client_kwargs), m.client_kwargs)

    def test_process_facebook_video_uses_facebook_proxy(self):
        m = self.run_process(FB_WATCH, {"/watch/": ok(fixture("fb_video.html"))}, instagram_proxy=IG_PROXY, facebook_proxy=FB_PROXY)
        self.assertTrue(m.client_kwargs)
        self.assertTrue(all(k.get("proxy") == FB_PROXY for k in m.client_kwargs), m.client_kwargs)

    def test_process_instagram_reel_uses_instagram_proxy(self):
        m = self.run_process(IG_REEL, {"/p/": ok(fixture("ig_reel.html"))}, instagram_proxy=IG_PROXY, facebook_proxy=FB_PROXY)
        self.assertTrue(m.client_kwargs)
        self.assertTrue(all(k.get("proxy") == IG_PROXY for k in m.client_kwargs), m.client_kwargs)

    def test_process_without_proxy_config_sends_no_proxy(self):
        for url, routes in (
            (IG_CAROUSEL, {"/p/": ok(fixture("ig_carousel.html"))}),
            (FB_ALBUM, {"/NASA/posts/1658876782274358": ok(fixture("fb_album.html"))}),
            (FB_WATCH, {"/watch/": ok(fixture("fb_video.html"))}),
        ):
            with self.subTest(url=url):
                m = self.run_process(url, routes, x_proxy=X_PROXY)  # 只有 x_proxy 也不影响 IG / FB
                self.assertTrue(m.client_kwargs)
                self.assertTrue(all("proxy" not in k for k in m.client_kwargs), m.client_kwargs)

    # ---- yt-dlp 回退

    def ytdlp_command(self, proxy: Optional[str]) -> List[str]:
        captured: List[str] = []
        orig = P.asyncio.create_subprocess_exec

        class _Proc:
            returncode = 0

            async def communicate(self):
                return b"", b""

        async def fake_exec(*command, **_kw):
            captured.extend(command)
            (self.tmp / "ytdlp" / "video.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 32)
            return _Proc()

        (self.tmp / "ytdlp").mkdir(exist_ok=True)
        P.asyncio.create_subprocess_exec = fake_exec
        try:
            run(P._run_ytdlp("https://www.instagram.com/reel/ABCDE/", self.tmp / "ytdlp", proxy))
        finally:
            P.asyncio.create_subprocess_exec = orig
        return captured

    def test_ytdlp_gets_proxy_only_when_given(self):
        with_proxy = self.ytdlp_command(IG_PROXY)
        self.assertEqual(with_proxy[with_proxy.index("--proxy") + 1], IG_PROXY)
        self.assertEqual(with_proxy[-1], "https://www.instagram.com/reel/ABCDE/")
        self.assertNotIn("--proxy", self.ytdlp_command(None))  # 未配置：命令和以前一样

    def test_obtain_fallbacks_pass_platform_proxy_to_ytdlp(self):
        seen: List[tuple] = []

        async def fake(url, task_dir, proxy=None):
            seen.append((url, proxy))
            f = task_dir / "video.mp4"
            f.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 32)
            return f, f.name

        orig = P._run_ytdlp
        P._run_ytdlp = fake
        try:
            self.configure(instagram_proxy=IG_PROXY, facebook_proxy=FB_PROXY)
            P._fetch_meta_html = _Router({"/api/v1/oembed/": {"kind": "error"}, "/embed/captioned/": ok(fixture("ig_shell.html")), "/p/": ok(fixture("ig_shell.html"))})
            run(P._obtain_instagram(IG_REEL, self.tmp))
            P._fetch_meta_html = _Router({"/plugins/": {"kind": "retry", "error": "HTTP 429"}, "/": {"kind": "retry", "error": "HTTP 429"}})
            run(P._obtain_facebook(FB_ALBUM, self.tmp))
        finally:
            P._run_ytdlp = orig
        self.assertEqual(seen, [(IG_REEL, IG_PROXY), (FB_ALBUM, FB_PROXY)])


# ---------------------------------------------------------------- 联网：真实帖子


@unittest.skipIf(OFFLINE, "NMF_IGFB_OFFLINE=1：跳过联网用例")
class LiveInstagram(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="nmf-ig-live-"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def headers(self):
        return {"User-Agent": P._META_UA, "Referer": P._DOWNLOAD_REFERER["instagram"], "Accept": "*/*"}

    def fetch_image(self, url: str, name: str, fallback: Optional[str] = None) -> bytes:
        limit = 60 * 1024 * 1024
        try:
            path = run(P._download_direct(url, self.tmp / name, limit, self.headers()))
        except Exception:
            if not fallback:
                raise
            path = run(P._download_direct(fallback, self.tmp / name, limit, self.headers()))
        return path.read_bytes()

    def test_single_image(self):
        r = resolve_retry(P._resolve_instagram, IG_SINGLE)
        pr("\n[IG 单图]", {k: r.get(k) for k in ("ok", "source", "author", "screen_name", "created_at_beijing", "likes", "replies", "canonical_url")}, "| 标题：", (r.get("title") or "")[:50])
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["screen_name"], "nasa")
        self.assertEqual(r["source"], "page")
        self.assertTrue(r["title"])
        self.assertRegex(r["created_at_beijing"], r"^\d{4}-\d\d-\d\d \d\d:\d\d$")
        self.assertGreater(r["likes"], 0)
        self.assertEqual(len(r["image_list"]), 1)
        data = self.fetch_image(r["image_list"][0], "single.jpg", r["image_fallbacks"].get(r["image_list"][0]))
        size = image_size(data)
        pr(f"    图片 {len(data)}B 魔数 {data[:4].hex()} 尺寸 {size}")
        self.assertTrue(is_image(data))
        self.assertGreaterEqual(min(size), 1000)  # 全尺寸图（实测 1440x1440），不是 640 缩略图

    def test_carousel_all_images(self):
        r = resolve_retry(P._resolve_instagram, IG_CAROUSEL)
        pr("\n[IG 6 图轮播]", {k: r.get(k) for k in ("ok", "source", "screen_name", "likes", "replies")}, "图片数：", len(r.get("image_list") or []))
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(r["image_list"]), 6)
        self.assertEqual(r["videos"], [])
        sizes = []
        for index, url in enumerate(r["image_list"], 1):
            data = self.fetch_image(url, f"c{index}.jpg", r["image_fallbacks"].get(url))
            self.assertTrue(is_image(data), f"第 {index} 张不是图片")
            sizes.append(image_size(data))
        pr("    全部 6 张已下载，尺寸：", sizes)
        self.assertTrue(all(min(s) >= 1000 for s in sizes), sizes)

    def test_reel_video_download_and_probe(self):
        r = resolve_retry(P._resolve_instagram, IG_REEL)
        pr("\n[IG Reel]", {k: r.get(k) for k in ("ok", "source", "screen_name", "created_at_beijing", "likes", "replies", "canonical_url")}, "videos：", [(v["label"], v["width"], v["height"]) for v in r.get("videos") or []])
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(r["videos"]), 1)
        self.assertEqual(r["canonical_url"], "https://www.instagram.com/reel/DeTUk4sDSp-/")
        out = self.tmp / "reel"
        out.mkdir()
        source = run(P._download_resolved(r, "instagram", out, 100 * 1024 * 1024))
        self.assertTrue(_sniff_mp4(source.path))
        self.assertGreater(source.size, 100_000)
        if FFPROBE:
            streams = ffprobe_streams(source.path)
            pr("    ffprobe：", {k: (v.get("codec_name"), v.get("width"), v.get("height")) for k, v in streams.items()}, f"{source.size}B")
            self.assertEqual(streams["video"]["codec_name"], "h264")
            self.assertIn("audio", streams)
            self.assertGreaterEqual(min(streams["video"]["width"], streams["video"]["height"]), 700)

    def test_carousel_with_video_post(self):
        r = resolve_retry(P._resolve_instagram, IG_CAROUSEL_VIDEO)
        pr("\n[IG 轮播含视频]", "图片：", len(r.get("image_list") or []), "视频：", len(r.get("videos") or []), "总项数：", r.get("media_count"))
        self.assertTrue(r["ok"], r)
        self.assertGreaterEqual(len(r["image_list"]), 10)
        self.assertGreaterEqual(len(r["videos"]), 1)
        self.assertEqual(len(r["image_list"]) + len(r["videos"]), r["media_count"])
        out = self.tmp / "mixed"
        out.mkdir()
        source = run(P._download_resolved(r, "instagram", out, 100 * 1024 * 1024))
        self.assertTrue(_sniff_mp4(source.path))

    def test_embed_fallback_live(self):
        """页面 SSR 之外的 embed 路径也要真的能用（单独调用，不是靠打补丁）。"""
        fetched = None
        for _ in range(3):
            fetched = run(P._fetch_meta_html("https://www.instagram.com/p/De5mVMikVB-/embed/captioned/", P._META_UA, timeout=30))
            if fetched["kind"] == "ok":
                break
            time.sleep(4)
        self.assertEqual(fetched["kind"], "ok", fetched)
        post = P._ig_post_from_embed(fetched["text"])
        self.assertIsNotNone(post, "embed 里没有 contextJSON（格式变了？）")
        r = P._ig_build_resolved(post, "De5mVMikVB-", "p")
        pr("\n[IG embed 回退]", "图片：", len(r["image_list"]), "作者：", r["screen_name"])
        self.assertEqual(len(r["image_list"]), 4)
        data = self.fetch_image(r["image_list"][0], "embed1.jpg")
        self.assertTrue(is_image(data))
        pr("    embed 第 1 张：", image_size(data))

    def test_oembed_live(self):
        for _ in range(3):
            result = run(P._ig_oembed(IG_SINGLE))
            if result["kind"] == "ok":
                break
            time.sleep(4)
        pr("\n[IG oEmbed]", result["kind"], (result.get("data") or {}).get("author_name"))
        self.assertEqual(result["kind"], "ok", result)
        self.assertTrue(result["data"]["title"])
        self.assertTrue(result["data"]["thumbnail_url"].startswith("https://"))
        data = self.fetch_image(result["data"]["thumbnail_url"], "oembed-thumb.jpg")
        self.assertTrue(is_image(data))
        missing = run(P._ig_oembed(IG_MISSING))
        self.assertEqual(missing["kind"], "missing")

    def test_missing_post_message(self):
        tmp = self.tmp / "missing"
        tmp.mkdir()
        orig = P._run_ytdlp

        async def no_ytdlp(_u, _d):
            raise AssertionError("确定不存在的帖子不应回退 yt-dlp")

        P._run_ytdlp = no_ytdlp
        try:
            with self.assertRaises(RuntimeError) as ctx:
                run(P._obtain_instagram(IG_MISSING, tmp))
        finally:
            P._run_ytdlp = orig
        pr("\n[IG 不存在的帖子]", ctx.exception)
        self.assertIn("没有找到这条 Instagram 帖子", str(ctx.exception))

    def test_profile_and_story_not_handled(self):
        self.assertIsNone(P._find_supported_url("https://www.instagram.com/nasa/"))
        r = run(P._resolve_instagram("https://www.instagram.com/stories/nasa/1/"))
        self.assertIn("Stories", r["error"])


@unittest.skipIf(OFFLINE, "NMF_IGFB_OFFLINE=1：跳过联网用例")
class LiveFacebook(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="nmf-fb-live-"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def headers(self):
        return {"User-Agent": P._META_UA, "Referer": P._DOWNLOAD_REFERER["facebook"], "Accept": "*/*"}

    def fetch_image(self, url: str, name: str, fallback: Optional[str] = None) -> bytes:
        limit = 60 * 1024 * 1024
        try:
            path = run(P._download_direct(url, self.tmp / name, limit, self.headers()))
        except Exception as error:
            if not fallback:
                raise
            pr(f"    （去 ctp 的链接失败：{error}，改用原链接）")
            path = run(P._download_direct(fallback, self.tmp / name, limit, self.headers()))
        return path.read_bytes()

    def test_album_all_images(self):
        r = resolve_retry(P._resolve_facebook, FB_ALBUM)
        pr("\n[FB 3 图相册]", {k: r.get(k) for k in ("ok", "source", "author", "created_at_beijing", "likes", "replies", "shares")}, "图片数：", len(r.get("image_list") or []))
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["source"], "relay:Story")
        self.assertEqual(len(r["image_list"]), 3)
        self.assertIn("NASA", r["author"])
        self.assertTrue(r["title"])
        self.assertRegex(r["created_at_beijing"], r"^\d{4}-\d\d-\d\d \d\d:\d\d$")
        for key in ("likes", "replies", "shares"):
            self.assertIsInstance(r[key], int)
        sizes = []
        for index, url in enumerate(r["image_list"], 1):
            data = self.fetch_image(url, f"a{index}.jpg", r["image_fallbacks"].get(url))
            self.assertTrue(is_image(data), f"第 {index} 张不是图片")
            sizes.append(image_size(data))
        pr("    3 张已下载，尺寸：", sizes)
        self.assertTrue(all(min(s) >= 700 for s in sizes), sizes)

    def test_single_photo_full_size(self):
        r = resolve_retry(P._resolve_facebook, FB_SINGLE)
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(r["image_list"]), 1)
        data = self.fetch_image(r["image_list"][0], "single.jpg", r["image_fallbacks"].get(r["image_list"][0]))
        pr("\n[FB 单图]", f"{len(data)}B", image_size(data), "去 ctp 备用链接数：", len(r["image_fallbacks"]))
        self.assertTrue(is_image(data))
        self.assertGreaterEqual(max(image_size(data)), 1000)

    def test_text_only(self):
        r = resolve_retry(P._resolve_facebook, FB_TEXT)
        pr("\n[FB 纯文字]", {k: r.get(k) for k in ("ok", "author", "created_at_beijing", "likes", "replies", "shares")}, "| 正文：", (r.get("title") or "")[:50].replace("\n", " "))
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["author"], "Mark Zuckerberg")
        self.assertTrue(r["title"])
        self.assertEqual((r["image_list"], r["videos"]), ([], []))

    def test_link_post(self):
        r = resolve_retry(P._resolve_facebook, FB_LINK)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["author"], "BBC News")
        self.assertTrue(r["title"])
        pr("\n[FB 链接帖]", "图片数：", len(r["image_list"]), "link：", r.get("link_attachment"))

    def test_video_progressive_download_and_probe(self):
        r = resolve_retry(P._resolve_facebook, FB_VIDEO)
        pr("\n[FB 视频]", {k: r.get(k) for k in ("ok", "source", "author", "created_at_beijing", "likes", "replies", "views")}, [(v["label"], bool(v["url"]), bool(v["dash_video_url"])) for v in r.get("videos") or []])
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(r["videos"]), 1)
        self.assertTrue(r["videos"][0]["url"], "没有拿到单文件直链")
        out = self.tmp / "video"
        out.mkdir()
        source = run(P._download_meta_video(r["videos"][0], "facebook", r, out, 100 * 1024 * 1024))
        self.assertTrue(_sniff_mp4(source.path))
        if FFPROBE:
            streams = ffprobe_streams(source.path)
            pr("    单文件直链 ffprobe：", {k: (v.get("codec_name"), v.get("width"), v.get("height")) for k, v in streams.items()}, f"{source.size}B")
            self.assertEqual(streams["video"]["codec_name"], "h264")
            self.assertEqual(streams["audio"]["codec_name"], "aac")

    @unittest.skipUnless(FFMPEG and FFPROBE, "需要 ffmpeg / ffprobe")
    def test_video_dash_merge_live(self):
        """强制走 DASH（去掉单文件直链）：真实下载视频流 + 音频流并用 ffmpeg 合并，要同时有 h264 视频流和 aac 音频流。"""
        r = resolve_retry(P._resolve_facebook, FB_WATCH)
        self.assertTrue(r["ok"], r)
        entry = {**r["videos"][0], "url": None}
        self.assertTrue(entry["dash_video_url"] and entry["dash_audio_url"], "页面没有给出 DASH 流")
        out = self.tmp / "dash"
        out.mkdir()
        source = run(P._download_meta_video(entry, "facebook", r, out, 100 * 1024 * 1024))
        streams = ffprobe_streams(source.path)
        pr("\n[FB DASH 合并]", {k: (v.get("codec_name"), v.get("width"), v.get("height")) for k, v in streams.items()}, f"{source.size}B")
        self.assertEqual(streams["video"]["codec_name"], "h264")
        self.assertEqual(streams["audio"]["codec_name"], "aac")
        self.assertGreaterEqual(streams["video"]["height"], 480)

    def test_reel(self):
        r = resolve_retry(P._resolve_facebook, FB_REEL)
        pr("\n[FB Reel]", {k: r.get(k) for k in ("ok", "source", "author", "created_at_beijing")}, "videos：", len(r.get("videos") or []))
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(r["videos"]), 1)
        out = self.tmp / "reel"
        out.mkdir()
        source = run(P._download_meta_video(r["videos"][0], "facebook", r, out, 100 * 1024 * 1024))
        self.assertTrue(_sniff_mp4(source.path))

    def test_share_link(self):
        r = resolve_retry(P._resolve_facebook, FB_SHARE)
        pr("\n[FB /share/p/]", {k: r.get(k) for k in ("ok", "source", "author", "canonical_url")}, "videos：", len(r.get("videos") or []))
        self.assertTrue(r["ok"], r)
        self.assertIn("/posts/", r["canonical_url"])  # 短链被还原成真实帖子地址
        self.assertTrue(r["title"] or r["videos"] or r["image_list"])

    def test_missing_post(self):
        tmp = self.tmp / "missing"
        tmp.mkdir()
        orig = P._run_ytdlp

        async def no_ytdlp(_u, _d):
            raise RuntimeError("yt-dlp 也不行（测试里不真的去跑）")

        P._run_ytdlp = no_ytdlp
        try:
            with self.assertRaises(RuntimeError) as ctx:
                run(P._obtain_facebook(FB_MISSING, tmp))
        finally:
            P._run_ytdlp = orig
        pr("\n[FB 不存在的帖子]", ctx.exception)
        self.assertRegex(str(ctx.exception), r"Facebook")

    def test_login_only_link_message(self):
        r = run(P._resolve_facebook("https://m.facebook.com/photo.php?fbid=10150000000000000"))
        pr("\n[FB m./photo.php 无效链接]", {k: r.get(k) for k in ("ok", "fallback", "error")})
        self.assertFalse(r["ok"])
        self.assertTrue(r["error"])


class _LiveProcessBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nmf-igfb-e2e-"))
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

    def process(self, url: str, attempts: int = 3) -> tuple:
        """整条 _process 链路；Meta 限流造成的失败重试，最后一次的结果原样返回。"""
        result = ""
        for attempt in range(1, attempts + 1):
            self.bot.calls.clear()
            P._RECENT_URLS.clear()
            result = run(P._process(_FakeCtx(), url))
            if not result.startswith("视频未能发送："):
                break
            pr(f"    （第 {attempt} 次失败：{result[:120]}）")
            time.sleep(5)
        return result, (self.bot.calls[0]["messages"][0]["data"]["content"] if self.bot.calls else [])

    def lease_path(self, seg: dict) -> Path:
        return P._LEASES[seg["data"]["file"].rsplit("/", 1)[1]].path


@unittest.skipIf(OFFLINE, "NMF_IGFB_OFFLINE=1：跳过联网用例")
class LiveProcess(_LiveProcessBase):
    def test_ig_carousel(self):
        result, content = self.process(IG_CAROUSEL)
        pr("\n[e2e IG 轮播]", result, [seg["type"] for seg in content])
        pr(content[0]["data"]["text"] if content else "(没有发出)")
        self.assertIn("图文已通过合并转发", result)
        self.assertEqual([seg["type"] for seg in content], ["text"] + ["image"] * 6)
        self.assertIn("Instagram 帖子 · 图文（6 张）", content[0]["data"]["text"])
        for seg in content[1:]:
            data = self.lease_path(seg).read_bytes()
            self.assertTrue(is_image(data))
            self.assertGreaterEqual(min(image_size(data)), 1000)

    def test_ig_reel(self):
        result, content = self.process(IG_REEL)
        pr("\n[e2e IG Reel]", result, [seg["type"] for seg in content])
        pr(content[0]["data"]["text"] if content else "(没有发出)")
        self.assertEqual(result, "视频已通过合并转发发回原聊天。")
        self.assertEqual([seg["type"] for seg in content], ["text", "video"])
        self.assertIn("（北京时间）", content[0]["data"]["text"])
        self.assertTrue(_sniff_mp4(self.lease_path(content[1])))

    def test_ig_single(self):
        result, content = self.process(IG_SINGLE)
        self.assertIn("图文已通过合并转发", result)
        self.assertEqual([seg["type"] for seg in content], ["text", "image"])

    def test_fb_album(self):
        result, content = self.process(FB_ALBUM)
        pr("\n[e2e FB 相册]", result, [seg["type"] for seg in content])
        pr(content[0]["data"]["text"] if content else "(没有发出)")
        self.assertEqual([seg["type"] for seg in content], ["text", "image", "image", "image"])
        self.assertIn("Facebook 帖子 · 图文（3 张）", content[0]["data"]["text"])
        for seg in content[1:]:
            self.assertTrue(is_image(self.lease_path(seg).read_bytes()))

    def test_fb_video(self):
        result, content = self.process(FB_WATCH)
        pr("\n[e2e FB 视频]", result, [seg["type"] for seg in content])
        pr(content[0]["data"]["text"] if content else "(没有发出)")
        self.assertEqual([seg["type"] for seg in content], ["text", "video"])
        path = self.lease_path(content[1])
        self.assertTrue(_sniff_mp4(path))
        if FFPROBE:
            streams = ffprobe_streams(path)
            self.assertIn("video", streams)
            self.assertIn("audio", streams)

    def test_fb_text_only(self):
        result, content = self.process(FB_TEXT)
        self.assertEqual(result, "帖子文字已通过合并转发发回原聊天。")
        self.assertEqual([seg["type"] for seg in content], ["text"])
        self.assertIn("Mark Zuckerberg", content[0]["data"]["text"])

    def test_fb_share_link(self):
        result, content = self.process(FB_SHARE)
        pr("\n[e2e FB /share/p/]", result, [seg["type"] for seg in content])
        self.assertFalse(result.startswith("视频未能发送"), result)
        self.assertTrue(content)

    def test_missing_posts_give_chinese_error(self):
        result, content = self.process(IG_MISSING, attempts=1)
        pr("\n[e2e IG 不存在]", result)
        self.assertTrue(result.startswith("视频未能发送："))
        self.assertIn("没有找到这条 Instagram 帖子", result)
        self.assertEqual(content, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
