# 常见问题与排查

这些都是实际踩过的坑，按现象查最快。

---

## 1. 机器人完全没反应（消息都不进）

先分清"没进"还是"进了没发"：

```bash
# 云端：近 30 分钟有没有事件
docker logs --since 30m nekro_agent 2>&1 | grep -c 'OneBot V11'
```

- **0** → NapCat / OneBot 没连上。查 NapCat 是否在跑（WebUI 默认 `http://127.0.0.1:6099`）、
  云端日志里有没有 `WebSocket /onebot/v11/ws [accepted]` 与 `Bot <你的QQ> connected`。
- **>0** → 消息进来了，看第 2、3 条。

> 经验：家里电脑重启后 NapCat 不会自动起来。可用本项目 README 里的
> 「开机自启」思路（等网络就绪 → 调启动器）。

---

## 2. 抖音/小红书"解析失败"，但链接明明是对的

机房 IP 会触发风控：抖音 web 接口对纯 HTTP 客户端（yt-dlp / curl）一律 403 或空响应，
按 IP 信誉还会拦数据中心。**唯一可靠做法是让住宅网络里跑一个真实浏览器**，也就是本项目
的本地解析器。

确认解析器在线：

```bash
curl "http://<你的服务器>:8022/plugins/local_video.video_forward/"
# 期望看到 "resolver":{"enabled":true,"online":true,...}
```

若 `online:false`：解析器没跑 / token 不一致 / 服务器地址填错 / 防火墙挡了出站。

---

## 3. `浏览器进程在启动阶段就退出了`（解析器）

无头 Edge 起不来。最常见原因是 **`.browser-profile` 被上一次残留的 `msedge.exe` 占着**：
Windows 下用同一个 `--user-data-dir` 再启动时，新进程会"交接后立即退出"。

**处置**：

1. 结束残留的 Edge（只结束**解析器**启动的那些，别看错把自己的浏览器关了）。
   在任务管理器里按 PID 结束，或：

   ```powershell
   # 找出占着调试端口的进程（解析器随机用 9222-9821）
   netstat -ano | Select-String 'LISTENING' | Select-String ':9[2-8][0-9][0-9]'
   Stop-Process -Id <PID> -Force
   ```

2. 或让解析器换一个全新 profile 目录重试（本项目 `resolver.js` 已内置：
   首次启动失败会自动改用 `<profile>-alt-<时间戳>` 再试一次）。

3. 仍不行时，把 `.browser-profile` 改名后重启解析器，让它重建。

> 另一个诱因：**Edge 自动更新后旧 profile 不兼容**。改名 profile 即可。

---

## 4. `TikTok 需要解析器把文件上传到服务器`

TikTok 是由解析器在本机下好文件再交给 NapCat 的，交付方式取决于 NapCat 在哪：

- **NapCat 与解析器同机** → 插件配置打开 `allow_resolver_local_path: true`，
  直接按本机路径交付（最快）。
- **NapCat 在云端** → 保持 `false`，解析器必须支持上传（`POST /resolver/upload`），
  插件再用 `/media/{token}` 交付。

自检里 `local_path_mode` 会直接告诉你当前是哪种：`hand-to-napcat` / `needs-upload`。

---

## 5. `https://www.tiktok.com/?_r=1` / `Unsupported URL`

TikTok 短链**区分大小写**。`vm.tiktok.com/ZPLSqPDfQ/` 与 `vm.tiktok.com/ZPLsqPDfQ/` 是两个不同的链接，
后者常被展开到首页。请照抄原始大小写；短链过期也会这样。

---

## 6. 视频发出去了但群里打不开/卡片是空

- `/media/{token}` 过期（`expiry_minutes`）：NapCat 取文件太晚会 404。适当调大。
- 直链有防盗链：确认下载时带了对应平台的 `Referer`（本项目已内置）。
- 大视频上传超时：调大 `send_timeout`（默认 300 秒）。

---

## 7. 想看 NapCat / 插件到底发了什么

- OneBot 客户端默认 `debug: false`，**不会**记录 `正向Websocket消息`（agent → NapCat 的 API 调用）。
  排查发送问题时要看结果侧（群里的回复、`notice.group_msg_emoji_like`、/media 访问日志），
  或临时把该客户端的 `debug` 打开。
- 插件日志关键字：`解析器上传完成`、`使用本机文件`、`抖音直链下载`、`图文发送`、`短视频转发失败`。

---

## 8. 容器重建后某些能力消失了

任何用 `pip install` / `apt install` 临时装进容器的东西，都会在 `docker compose up -d`（重建）后丢失。
请装到**宿主挂载目录**（容器内同路径可见），并把插件配置指向那里。

---

## 9. X (Twitter) 帖子发不出来 / 提示「无法解析」

X 走的是 **X 嵌入组件的公开数据接口**（`https://cdn.syndication.twimg.com/tweet-result?id=<帖子ID>&token=<任意值>`），
**不需要登录、不需要 API token、也不需要本地解析器**，和 B 站一样云端直连。所以：

- 只能读**公开的单条帖子**；**受保护账号、已删除、被封禁账号、需登录才能看的敏感 / 年龄限制内容**拿不到，
  插件会直接提示「没有找到这条 X 帖子：可能已被删除、发帖账号受保护，或需要登录才能查看」之类的中文原因。
  插件**不会**也不应该去绕过这些限制（不用登录态 / Cookie）。
- 这个接口**不是 X 官方承诺的 API**：字段、限流、是否继续开放都可能变化。接口返回 429 / 5xx / 网络错误时，
  插件会重试 3 次，仍失败就**回退 yt-dlp**（只能发视频，没有帖子文字；yt-dlp 对 X 的可用性取决于它的版本和 X 当时的策略，
  没装或失败时报错里会同时写出两边的原因）。
- 长文（note tweet）接口只给开头部分，文字节点末尾会注明；被引用帖子里的图片 / 视频不会发，只附正文；
  `t.co` 短链暂不识别，请发 `x.com/<用户>/status/<ID>` 形式的链接。
- 视频超过 `x_max_size_mb`（默认 100MB）时不发送，并提示实际大小，可在配置里调大。
- 不想要文字节点：把 `x_send_text` 设为 `false`（纯文字帖这时会提示没有可发送的内容）。
- 想完全关掉：`x_enabled: false`，X 链接会被忽略（机器人不会回应）。

**快速自检**（把 `<ID>` 换成帖子 ID，应返回 JSON，里面有 `text` 和 `mediaDetails`）：

```bash
curl -s -A "Mozilla/5.0" "https://cdn.syndication.twimg.com/tweet-result?id=<ID>&token=a" | head -c 400
```

返回 404（HTML「Nothing to see here」）= 帖子不存在 / 已删 / 受保护；服务器上这条能通而插件仍失败，再看日志里的
`X 公开数据接口暂时不可用`、`X 直链下载失败` 等关键字。也可以用 `/resolver/self-test?token=<密钥>&url=<X 帖子链接>&download=true`
验证「解析 → 下载」。

---

## 10. Instagram / Facebook 帖子发不出来 / 提示「无法解析」

Instagram 和 Facebook 走的都是**匿名读公开帖子页**：插件默认用诚实的 `nekro-media-forward/1.0` User-Agent（不冒充浏览器；Instagram 唯一的例外见下面的「灰色做法」）
请求帖子页，解析页面里内嵌的 JSON，**不登录、不用 Cookie / token、不需要本地解析器**。所以：

- **这是非官方做法，随时可能失效**：Meta 改页面结构、加强限流 / 风控、或不再给匿名客户端返回数据，插件都会跟着失败。
  失败时报错里会写出每条回退路径各自的原因（Instagram：页面 → embed 页 → yt-dlp → oEmbed；Facebook：帖子页 → `plugins/post.php` → `plugins/video.php` → yt-dlp）。
- **签名的 CDN 链接会过期**（Instagram `oe=` / Facebook `oe=` 之类的参数），所以插件解析后立刻下载；如果解析和下载之间隔了很久（例如被限流重试）
  可能遇到 HTTP 403，重发一次链接即可。
- **灰色做法**：`instagram_fallback_ua`（**默认 `true`，开启**）——总是先用诚实 UA 请求；只有它拿不到数据（Instagram 页面是 JS 空壳）时，
  才会再用 Googlebot 的 User-Agent 重试一次，成功时日志 / 自检里的来源会标成 `page(Googlebot UA)`。
  这属于伪装成搜索引擎爬虫的灰色做法，可能不符合 Instagram 的使用条款，也可能随时被封；介意就把它设为 `false` 关闭
  （关闭后只用诚实 UA，失败走 embed → yt-dlp → oEmbed 回退）。被带去登录页时不会换 UA 硬试。Facebook 没有任何 UA 伪装。
  **不要**换成浏览器 UA：实测 Facebook 对浏览器 UA 直接返回 HTTP 400，Instagram 返回一个没有数据的 JS 壳页面。
- **不支持**（会给出明确中文提示）：Instagram Stories / 高亮；私密账号和仅好友可见的帖子；需要登录的内容（年龄 / 地区限制）；
  Facebook 群组帖（`/groups/.../posts/...`）；**`fb.watch` 短链**（请在 Facebook 里「复制链接」发 `facebook.com` 开头的完整链接，
  报错就是「暂不支持 fb.watch 短链，请发完整链接」）；`m.facebook.com` / `photo.php` / `permalink.php` 匿名常被带去登录页。
  插件**不会**也不应该去绕过这些限制。
- **代理**：大陆机房多半直连不通这两个站，可配置 `instagram_proxy` / `facebook_proxy`（做法同 `x_proxy`，如 `http://172.18.0.1:7891`；
  容器里的 `127.0.0.1` 是容器自己，要填宿主机在 docker 网桥上的地址）。留空 = 直连，行为和以前一样；两项互相独立，**不会借用 `x_proxy`**。
  配置后，该站点的帖子页请求、图片 / 视频下载、yt-dlp 回退都会走它。注意：代理这条路径只做过离线验证（确认代理参数传给了 httpx / yt-dlp），
  **没有用真实代理、也没有在大陆网络下实测过**；如果配了代理仍失败，先用 `curl -x <代理> -A "nekro-media-forward/1.0" https://www.instagram.com/p/<code>/` 看代理本身通不通。
- **中国大陆 IP 未测试**：以上能力都是在海外机房 IP 直连下验证的。
  机房 IP 被 Meta 限流时会看到 HTTP 429 / 500 / 空响应，插件会重试几次，仍失败就走回退；这时换个出口 IP 或稍后再试。
- Instagram 混合轮播（图片 + 视频）会先发所有图片再发视频，原有顺序不保留；图片数量受 `max_images` 限制，单个视频超过
  `instagram_max_size_mb` / `facebook_max_size_mb`（默认 100MB）不发送并提示实际大小。
- Facebook 视频优先用单文件 mp4；它不可用时才用 DASH 视频流 + 音频流并**用 ffmpeg 合并**（需要服务器上有 ffmpeg，
  路径同 `bilibili_ffmpeg_path`，留空自动查找）。缺 ffmpeg 时会提示。
- 想关掉：`instagram_enabled: false` / `facebook_enabled: false`，对应链接会被忽略（机器人不会回应）。

**快速自检**（把链接换成公开帖子；应返回 JSON，`ok` 为 `true`，并有 `image_count` / `video_count`）：

```bash
curl -s "http://<server>:8022/plugins/local_video.video_forward/resolver/self-test?token=<密钥>&url=<Instagram 或 Facebook 帖子链接>&download=true"
```

也可以手动看页面能不能匿名取到（诚实 UA；Facebook 返回 400 / Instagram 返回只有壳的页面，说明当前 IP 被限制了）：

```bash
curl -s -o /dev/null -w "%{http_code} %{size_download}\n" -A "nekro-media-forward/1.0 (+personal bot; non-browser)" -H "Accept: text/html" "https://www.instagram.com/p/<code>/"
```
