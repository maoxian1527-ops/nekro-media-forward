#!/usr/bin/env python3
"""重新生成 tests/fixtures/ 里的脱敏样例（需要联网；平时跑测试不需要它）。

做法：用插件自己的 _fetch_meta_html 匿名取几条公开帖子，只留下解析要用到的字段，
把 CDN 链接里带签名的参数（oh / oe / _nc_* / efg …）全部去掉，只保留 stp / cstp / ctp 这类公开的尺寸参数；
评论者的名字换成「测试用户」。所以样例里**没有任何 cookie / token / 会话信息**。

注意：
* Instagram 的样例是把真实帖子节点（字段值都是真实的）裁剪后放回一个简化的 SSR JSON 外壳里；
* Facebook 的 Relay 数据太大且嵌套很深，样例是用真实解析出来的值，按页面里同样的键名重新搭了一个简化骨架。
所以样例验证的是「解析逻辑对这些键名 / 形状正确」，**不等于**整页原样回放；真实页面格式变了要靠联网用例发现。

    python tests/fixtures/make_fixtures.py
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import test_ig_fb_resolver as T  # noqa: E402  (会先装好假的 nekro_agent 模块再导入真实 plugin.py)

P = T.P
KEEP_QUERY = ("stp", "cstp", "ctp")


def scrub(url: str) -> str:
    parts = urlsplit(url)
    pairs = [(key, value) for key, value in parse_qsl(parts.query) if key in KEEP_QUERY]
    return urlunsplit(parts._replace(query=urlencode(pairs)))


def wrap_sjs(payload: dict) -> str:
    return '<script type="application/json" data-content-len="1" data-sjs>' + json.dumps(payload, ensure_ascii=False) + "</script>"


def page(*scripts: str, title: str = "t") -> str:
    return f"<!DOCTYPE html><html><head><title>{title}</title></head><body>{''.join(scripts)}</body></html>\n"


def write(name: str, text: str) -> None:
    (HERE / name).write_text(text, encoding="utf8")
    print(f"wrote {name} ({len(text)} bytes)")


# ---------------------------------------------------------------- Instagram


def trim_ig_node(node: dict, keep_items: int = 99) -> dict:
    out = {key: node[key] for key in ("__typename", "code", "pk", "id", "media_type", "product_type", "taken_at", "like_count", "comment_count", "original_width", "original_height", "has_audio") if key in node}
    if node.get("caption"):
        out["caption"] = {"text": node["caption"].get("text")}
    if node.get("user"):
        out["user"] = {key: node["user"].get(key) for key in ("username", "full_name", "is_verified") if key in node["user"]}
    if node.get("display_uri"):
        out["display_uri"] = scrub(node["display_uri"])
    candidates = (node.get("image_versions2") or {}).get("candidates") or []
    if candidates:
        out["image_versions2"] = {"candidates": [{**{k: c[k] for k in ("width", "height") if k in c}, "url": scrub(c["url"])} for c in candidates[:2]]}
    if node.get("video_versions"):
        out["video_versions"] = [{"type": v.get("type"), "url": scrub(v["url"])} for v in node["video_versions"][:1]]
    if node.get("carousel_media"):
        out["carousel_media"] = [trim_ig_node(kid) for kid in node["carousel_media"][:keep_items]]
        out["carousel_media_count"] = node.get("carousel_media_count") or len(node["carousel_media"])
    return out


def ig_page(node: dict) -> str:
    result = {"data": {"xig_polaris_media": {"if_not_gated_logged_out": node}}}
    payload = {"require": [["ScheduledServerJS", "handle", None, [{"__bbox": {"require": [["RelayPrefetchedStreamCache", "next", [], ["adp_PolarisPostRootQueryRelayPreloader", {"__bbox": {"complete": True, "result": result}}]]]}}]]]}
    return page(wrap_sjs(payload), title="Instagram")


def ig_embed_page(live: str, max_children: int = 3) -> str:
    match = re.search(r'"contextJSON":"((?:[^"\\]|\\.)*)"', live)
    ctx = json.loads(json.loads('"' + match.group(1) + '"'))
    media = ctx["gql_data"]["shortcode_media"]
    out = {key: media.get(key) for key in ("shortcode", "is_video", "dimensions", "product_type", "taken_at_timestamp") if key in media}
    out["owner"] = {"username": media["owner"].get("username"), "full_name": media["owner"].get("full_name")}
    out["edge_media_to_caption"] = {"edges": [{"node": {"text": ((media.get("edge_media_to_caption") or {}).get("edges") or [{"node": {"text": ""}}])[0]["node"]["text"]}}]}
    out["edge_liked_by"] = {"count": (media.get("edge_liked_by") or {}).get("count")}
    out["edge_media_to_comment"] = {"count": (media.get("edge_media_to_comment") or {}).get("count")}

    def child(src: dict) -> dict:
        item = {key: src.get(key) for key in ("is_video", "dimensions") if key in src}
        item["display_resources"] = [{**res, "src": scrub(res["src"])} for res in (src.get("display_resources") or [])]
        if src.get("video_url"):
            item["video_url"] = scrub(src["video_url"])
        return item

    out.update({key: value for key, value in child(media).items() if key != "is_video" or True})
    kids = (media.get("edge_sidecar_to_children") or {}).get("edges") or []
    if kids:
        out["edge_sidecar_to_children"] = {"edges": [{"node": child(edge["node"])} for edge in kids[:max_children]]}
    ctx_out = {"gql_data": {"shortcode_media": out}}
    return page('<script>window.__additionalDataLoaded({"contextJSON":' + json.dumps(json.dumps(ctx_out, ensure_ascii=False), ensure_ascii=False) + "});</script>", title="Instagram embed")


async def build_instagram() -> None:
    for name, code, kids in (
        ("ig_single.html", "De9piNJlwES", 99),
        ("ig_reel.html", "DeTUk4sDSp-", 99),
        ("ig_carousel.html", "DJ4ZzePT6jQ", 3),
        ("ig_carousel_video.html", "DeKe6teEpya", 4),
    ):
        fetched = await P._fetch_meta_html(f"https://www.instagram.com/p/{code}/", P._META_UA, timeout=30)
        assert fetched["kind"] == "ok", (code, fetched["kind"])
        node = P._ig_find_media(fetched["text"], code)
        assert node, code
        write(name, ig_page(trim_ig_node(node, kids)))
    fetched = await P._fetch_meta_html("https://www.instagram.com/p/De5mVMikVB-/embed/captioned/", P._META_UA, timeout=30)
    assert fetched["kind"] == "ok"
    write("ig_embed_carousel.html", ig_embed_page(fetched["text"]))
    oembed = await P._ig_oembed("https://www.instagram.com/p/De9piNJlwES/")
    assert oembed["kind"] == "ok"
    data = {key: oembed["data"][key] for key in ("version", "title", "author_name", "author_url", "provider_name", "provider_url", "type", "width", "height", "thumbnail_width", "thumbnail_height") if key in oembed["data"]}
    data["thumbnail_url"] = scrub(oembed["data"]["thumbnail_url"])
    write("ig_oembed.json", json.dumps(data, ensure_ascii=False, indent=1) + "\n")
    write("ig_shell.html", page('<script>window.__shell = true;</script>', title="Instagram"))


# ---------------------------------------------------------------- Facebook


def fb_feedback(reactions, comments, shares) -> dict:
    return {"story": {"story_ufi_container": {"story": {"feedback_context": {"feedback_target_with_context": {"comet_ufi_summary_and_actions_renderer": {"feedback": {
        "i18n_reaction_count": str(reactions), "reaction_count": {"count": reactions}, "share_count": {"count": shares}, "comments": {"total_count": comments},
    }}}}}}}}


def fb_story_page(parsed: dict, source_post_url: str, *, comment_rows=None) -> str:
    attachments = []
    photos = [item for item in parsed["items"] if item["type"] == "image"]

    def photo_media(item: dict) -> dict:
        return {"__typename": "Photo", "id": item.get("id") or "1", "viewer_image": {"uri": scrub(item["url"]), "width": item.get("width"), "height": item.get("height")}}

    if len(photos) > 1:
        attachments.append({"styles": {"attachment": {"all_subattachments": {"count": parsed.get("album_count") or len(photos), "nodes": [{"media": photo_media(item)} for item in photos]}}}})
    elif photos:
        attachments.append({"styles": {"attachment": {"media": photo_media(photos[0])}}})
    if parsed.get("link"):
        attachments.append({"styles": {"attachment": {"title": parsed["link"].get("title"), "url": parsed["link"].get("url"), "source": {"text": parsed["link"].get("domain")}}}, "url": parsed["link"].get("url")})
    story = {
        "__typename": "Story", "post_id": parsed.get("post_id"), "creation_time": int(__import__("datetime").datetime.fromisoformat(parsed["created"]).timestamp()),
        "permalink_url": source_post_url, "actors": [{"name": parsed["author"], "id": parsed.get("author_id") or "1"}],
        "attachments": attachments,
        "comet_sections": {
            "content": {"story": {"comet_sections": {"message": {"story": {"message": {"text": parsed.get("text") or ""}}}}}},
            "feedback": fb_feedback(parsed.get("reactions") or 0, parsed.get("comments") or 0, parsed.get("shares") or 0),
        },
    }
    scripts = [wrap_sjs({"require": [["ScheduledServerJS", "handle", None, [{"__bbox": {"require": [["RelayPrefetchedStreamCache", "next", [], ["adp_CometSinglePostContentQueryRelayPreloader", {"__bbox": {"complete": True, "result": {"data": {"node": story}}}}]]]}}]]]})]
    if comment_rows:
        nodes = [{"id": f"c{i}", "created_time": 1700000000 + i, "author": {"name": f"测试用户{chr(65 + i)}"}, "body": {"text": text}} for i, text in enumerate(comment_rows)]
        scripts.append(wrap_sjs({"require": [["ScheduledServerJS", "handle", None, [{"__bbox": {"result": {"data": {"comments": {"edges": [{"node": node} for node in nodes]}}}}}]]]}))
    return page(*scripts, title="Facebook")


def fb_video_page(parsed: dict, live: str) -> str:
    video = next(item for item in parsed["items"] if item["type"] == "video")
    vid = str(video["id"])
    mpd = (
        '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><Period>'
        f'<AdaptationSet mimeType="video/mp4"><Representation bandwidth="2500000" codecs="avc1.64001f" mimeType="video/mp4" width="{video.get("width") or 720}" height="{video.get("height") or 1280}" FBQualityLabel="{video.get("label") or "720p"}"><BaseURL>{scrub(video["dash_video_url"]).replace("&", "&amp;")}</BaseURL></Representation>'
        '<Representation bandwidth="900000" codecs="vp09.00.31.08" mimeType="video/mp4" width="1280" height="720" FBQualityLabel="720p-vp9"><BaseURL>https://video.example.invalid/vp9.mp4</BaseURL></Representation></AdaptationSet>'
        f'<AdaptationSet mimeType="audio/mp4"><Representation bandwidth="130000" codecs="mp4a.40.2" mimeType="audio/mp4"><BaseURL>{scrub(video["dash_audio_url"]).replace("&", "&amp;")}</BaseURL></Representation></AdaptationSet>'
        "</Period></MPD>"
    )
    data = {
        "video": {"id": vid, "story": {"id": "s1"}},
        "node": {"id": vid, "creation_story": {"creation_time": int(__import__("datetime").datetime.fromisoformat(parsed["created"]).timestamp()), "message": {"text": parsed["text"]}}, "publish_time": int(__import__("datetime").datetime.fromisoformat(parsed["created"]).timestamp())},
        "media": {"id": vid, "dash_manifest_xml_string": mpd},
        "counts": {"play_count": parsed.get("views") or 0, "video_view_count": 1},
        "feedback": fb_feedback(parsed.get("reactions") or 0, parsed.get("comments") or 0, 0)["story"]["story_ufi_container"]["story"]["feedback_context"]["feedback_target_with_context"]["comet_ufi_summary_and_actions_renderer"]["feedback"],
    }
    # 主视频节点要出现在 ...result.data.video 路径上（解析器据此判断「这页的主视频」）
    payload = {"require": [["ScheduledServerJS", "handle", None, [{"__bbox": {"require": [["RelayPrefetchedStreamCache", "next", [], ["adp_CometVideoRoot", {"__bbox": {"complete": True, "result": {"data": {"video": data["video"]}}}}]]]}}]]]}
    payload2 = {"require": [["ScheduledServerJS", "handle", None, [{"__bbox": {"result": {"data": {"node": data["node"], "x": data["media"], "y": data["counts"], "z": data["feedback"]}}}}]]]}
    title = f'<meta property="og:title" content="{parsed.get("views") or 0} views · reactions | {parsed["text"][:30]} | {parsed["author"]}" />'
    return page(title, wrap_sjs(payload), wrap_sjs(payload2), title="Facebook")


async def build_facebook() -> None:
    async def parsed_of(url: str):
        fetched = await P._fetch_meta_html(url, P._META_UA, timeout=45)
        assert fetched["kind"] == "ok", (url, fetched["kind"])
        return P._fb_parse_page(fetched["text"]), fetched

    album, _ = await parsed_of("https://www.facebook.com/NASA/posts/1658876782274358/")
    write("fb_album.html", fb_story_page(album, "https://www.facebook.com/NASA/posts/1658876782274358", comment_rows=["第一条测试评论", "第二条测试评论"]))
    single, _ = await parsed_of("https://www.facebook.com/NASA/posts/1668207108007992/")
    # 单图帖：把 ctp 参数补回去（真实的单图 CDN 链接带 ctp=p600x600），用来测试「去掉 ctp 取原图」
    for item in single["items"]:
        item["url"] = scrub(item["url"]) + ("&" if "?" in scrub(item["url"]) else "?") + "ctp=p600x600"
    write("fb_single.html", fb_story_page(single, "https://www.facebook.com/NASA/posts/1668207108007992"))
    text_only, _ = await parsed_of("https://www.facebook.com/zuck/posts/pfbid0EEdDQVai4umZQwLkvGmFZnAnAucg4GMRLrphVp2SYyCUfBmtL9gmAzap22uA2jscl")
    write("fb_text_only.html", fb_story_page(text_only, "https://www.facebook.com/zuck/posts/pfbid0EEdDQVai4umZQwLkvGmFZnAnAucg4GMRLrphVp2SYyCUfBmtL9gmAzap22uA2jscl"))
    link, _ = await parsed_of("https://www.facebook.com/bbcnews/posts/1377611997736122")
    write("fb_link.html", fb_story_page(link, "https://www.facebook.com/bbcnews/posts/1377611997736122"))
    video, fetched = await parsed_of("https://www.facebook.com/watch/?v=1803909017420449")
    write("fb_video.html", fb_video_page(video, fetched["text"]))

    sources = await P._fb_video_sources("1803909017420449")
    assert sources["hd"] and sources["sd"], sources
    write("fb_video_php.html", page('<script>require("x")({"hd_src":' + json.dumps(scrub(sources["hd"])).replace("/", "\\/") + ',"sd_src":' + json.dumps(scrub(sources["sd"])).replace("/", "\\/") + "});</script>", title="Facebook"))

    href = "https://www.facebook.com/NASA/posts/1658876782274358/"
    from urllib.parse import quote
    fetched = await P._fetch_meta_html("https://www.facebook.com/plugins/post.php?href=" + quote(href, safe="") + "&show_text=true&width=500", P._META_UA, timeout=30)
    assert fetched["kind"] == "ok"
    plug = P._fb_parse_plugin_post(fetched["text"])
    ld = {"@type": "SocialMediaPosting", "dateCreated": "2026-10-16T13:05:02-0700", "headline": f"{(plug['text'] or '')[:20]} - {plug['author']}",
          "interactionStatistic": [{"interactionType": "http://schema.org/CommentAction", "userInteractionCount": plug["comments"] or 0}, {"interactionType": "http://schema.org/LikeAction", "userInteractionCount": plug["reactions"] or 0}, {"interactionType": "http://schema.org/ShareAction", "userInteractionCount": plug["shares"] or 0}],
          "comment": [{"text": "测试评论", "author": {"name": "测试用户A"}}]}
    imgs = "".join(f'<img class="scaledImageFitWidth img" src="{scrub(item["url"]).replace("&", "&amp;")}" />' for item in plug["items"])
    body = (f'<script type="application/ld+json">{json.dumps(ld, ensure_ascii=False)}</script><div class="_2_79 _50f4">{plug["author"]}<span></span></div>'
            f'<div data-testid="post_message" class="_5pbx"><p>{(plug["text"] or "").replace(chr(10) + chr(10), "</p><p>")}</p></div>{imgs}')
    write("fb_post_php.html", page(body, title="Facebook"))
    write("fb_login.html", page('<meta property="og:title" content="Log in or sign up to view" /><meta property="og:description" content="Facebook" />', title="Facebook"))
    write("fb_empty.html", page("<div>This content isn't available right now</div>", title="Facebook"))


async def main() -> None:
    await build_instagram()
    await build_facebook()


if __name__ == "__main__":
    asyncio.run(main())
