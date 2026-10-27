# nekro-media-forward

> bot媒体转发 · QQ 短视频 / 图文自动合并转发（NekroAgent + OneBot v11 / NapCat）

[![ci](https://github.com/maoxian1527-ops/nekro-media-forward/actions/workflows/ci.yml/badge.svg)](https://github.com/maoxian1527-ops/nekro-media-forward/actions/workflows/ci.yml)
[English](README.en.md) · 中文


把 抖音 / 小红书 / TikTok / **B 站** / **X (Twitter)** / **Instagram** / **Facebook** 的公开视频或图文链接，自动解析并以 QQ **合并转发**发回原聊天。
为 [NekroAgent](https://github.com/KroMiose/nekro-agent) + OneBot v11 / NapCat 设计，附带一个可选的
**本地解析器（media-resolver）**，用来处理机房 IP 拿不到的抖音/小红书直链，以及必须走代理的 TikTok；
B 站不需要解析器 —— 云端直连公开 API 即可（支持 1080p / 4K，见下文）。

- **零第三方依赖**：插件是单文件 Python（只用 NekroAgent 自带能力），解析器是单文件 Node.js（只用标准库 + 系统里的 Edge）
- **不打断聊天**：链接消息由插件直接处理并阻断 AI 触发，不会让机器人"再回一句"
- **住宅 IP 解析**：机房 IP 解析抖音容易被风控，解析器跑在你家里的电脑上，用真实无头浏览器取直链
- **主动轮询**：解析器在家宽 NAT 后面也能用，服务器不需要能连回本地

---

## 功能一览

| 平台 | 视频 | 图文（图集） | 说明 |
|---|---|---|---|
| 抖音 | ✅ | ✅ | 直链由本地解析器取；图文作品按图片组发出 |
| 小红书 | ✅ | ✅ | 需登录/风控的笔记会失败，属于平台限制 |
| TikTok | ✅ | — | 解析器**经代理**下载到本机，再把文件交给 NapCat |
| B站 | ✅ | — | 走 B 站公开 API 取**单文件 mp4** 直链，云端直接下载（**不需要解析器、不需要 ffmpeg**） |
| X (Twitter) | ✅（含动图） | ✅ | 读 X 嵌入组件的**公开数据接口**，**不需要登录 / API token / 解析器**；合并转发里附带帖子文字（作者、北京时间、正文、被引用帖子）。纯文字帖也能发 |
| Instagram | ✅（Reel / 视频帖） | ✅（单图 / 轮播） | 匿名读公开帖子页，**不登录、不用 Cookie / token、不需要解析器**；合并转发里附带作者、北京时间、点赞 / 评论数和正文。**非官方接口，可能随时失效**；不支持 Stories、私密账号 |
| Facebook | ✅ | ✅（单图 / 相册） | 匿名读公开帖子页，**不登录、不用 Cookie / token、不需要解析器**；附带作者、北京时间、反应 / 评论 / 分享数和正文。**非官方接口，可能随时失效**；不支持 `fb.watch` 短链、群组帖、私密内容 |


- 视频以**合并转发**发出（一条卡片，附原链接）
- 图文笔记以**一条合并转发**装多张图（`max_images` 控制上限，默认 9 张）
### B 站的画质说明

| 情况 | 拿到什么 | 走哪条路 |
|---|---|---|
| 匿名（`bilibili_cookie` 留空） | **720p** | 单文件 mp4（`fnval=1`），不需要 ffmpeg |
| 已登录（填了 `SESSDATA`） | **1080p / 4K** | DASH（`fnval=4048`）+ ffmpeg 合并音视频 |

**登录怎么弄（推荐）**：跑一次 `sudo ./tools/bilibili-qr-login.sh` —— 它会在服务器上生成登录二维码，
手机 B 站 App 扫码确认后，自动把 `SESSDATA` / `bili_jct` / `DedeUserID` / `DedeUserID__ckMd5`
写进插件配置并重启容器。比手动从浏览器复制 Cookie 可靠得多（少字段、YAML 折行都会导致登录失效）。

> 有意思的细节：B 站**匿名用户的 DASH 只给 480p**，而单文件 mp4 能到 720p。
> 所以插件会先看 DASH 能到多高，**只有达到 1080p 才走 DASH 合并**，否则退回 mp4 —— 保证匿名也有 720p。

拿 `SESSDATA`（手动方式）：浏览器登录 bilibili.com → F12 → Application → Storage → Cookies →
`https://www.bilibili.com` → 复制 `SESSDATA` 的值（建议用小号，登录态会交给服务器上的插件）。

> 技术细节：B 站的非签名接口 `x/player/playurl` **只返回匿名画质**（DASH 最高 480p、单文件 mp4 最高 720p），
> 1080p+ 必须走 `x/player/wbi/playurl` 并带 `w_rid` / `wts` 签名。插件已内置 wbi 签名（见 `_BilibiliWbi`）。

实测（云端、同一视频 BV1GJ411x7h7）：

| 状态 | 解析结果 |
|---|---|
| 匿名 | `mode=direct_url`，720p，49.57 MB |
| 登录后 | `mode=dash`，**1080p**，`merged=true`，71.75 MB |

- X (Twitter)：网页和官方 API 都要登录 / 付费，所以改读 X 嵌入组件自己用的公开接口
  `https://cdn.syndication.twimg.com/tweet-result?id=<帖子ID>`（返回正文、作者、图片、各档 mp4 直链、引用帖子），
  因此 **X 不需要本地解析器、不需要登录**。支持 `x.com` / `twitter.com` / `mobile.twitter.com` / `fxtwitter.com` /
  `vxtwitter.com` 的 `/<用户>/status/<ID>`、`/i/status/<ID>`（可带 `?s=46`、`/photo/1`、`/video/1`）。
  视频自动挑**不超过 `x_max_size_mb` 的最高码率 mp4**，图片取 `?name=orig` 原图（受 `max_images` 限制）。
  **限制**：该接口不是 X 官方承诺的 API，字段 / 限流可能随时变化；只能读**公开的单条帖子**，受保护账号、已删除、
  敏感内容或需登录的帖子无法解析；长文（note tweet）只能拿到开头部分；暂不支持 `t.co` 短链和被引用帖子里的图片 / 视频。
  接口暂时不可用（限流 / 网络）时会回退 yt-dlp（只能发视频、没有文字）。详见 [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) 第 9 条。
- Instagram / Facebook：两者都**不登录、不用 Cookie / token**，插件默认以诚实的 `nekro-media-forward/1.0` User-Agent 匿名请求公开帖子页并解析页面里内嵌的 JSON（Instagram 仅在诚实 UA 失败时才会按下面的开关回退 Googlebot UA）
  （Instagram 的 SSR 数据、Facebook 的 Relay 数据），都是**非官方接口，页面结构 / 限流 / 是否开放可能随时变化**。
  - Instagram：支持 `/p/` `/reel/` `/reels/` `/tv/` 和 `/<用户>/p/<code>/`（可带 `?igsh=`）。页面拿不到时依次回退 embed 页、yt-dlp（仅视频）、oEmbed（只有文字 + 缩略图）。
    轮播取每张的全尺寸图；**图片和视频混合的轮播会先发所有图片再发视频，原有顺序不保留**。
    Stories、私密账号、需登录的内容会提示无法解析。`instagram_fallback_ua` 是**灰色做法（伪装成搜索引擎爬虫的 UA）、默认开启**：只有诚实 UA 拿不到数据（页面是壳）时才会再用 Googlebot UA 重试一次；介意的话设为 `false` 关闭。
  - Facebook：支持 `/<主页>/posts/<id>`、`/<主页>/videos/<id>`、`/reel/<id>`、`/watch/?v=`、`/share/p|v|r/<码>`（会还原成真实帖子地址）、`photo.php?fbid=`、`permalink.php?story_fbid=`（后两种匿名通常会被带去登录页，只能尽力回退）。
    视频优先取 `plugins/video.php` 给出的**单文件 mp4（H.264 + AAC）**；拿不到时用页面里的 DASH 视频流 + 音频流，**用 ffmpeg 合并**（和 B 站同一个 ffmpeg，见 `bilibili_ffmpeg_path`；只有这条回退路径需要 ffmpeg）。
    单张图片会去掉 CDN 链接里的 `ctp` 缩略图参数以取原图，失败再用原链接。`fb.watch` 短链、群组帖、`m.facebook.com` 的 `photo.php` 等需登录的内容不支持，会给出明确提示。
  - 代理：配置 `instagram_proxy` / `facebook_proxy`（做法同 `x_proxy`，留空 = 直连）后，对应站点的帖子页请求、图片 / 视频下载和 yt-dlp 回退都走该代理；两项互相独立，不会借用 `x_proxy`。
  - 通用：签名的 CDN 链接很快过期，所以解析后**立即下载**；中国大陆 IP 访问这两个站**未测试**，经代理访问也只做了离线的参数传递验证、没有真实代理实测（以上都是在海外服务器直连验证的）。详见 [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) 第 10 条。
- B 站：机房 IP 访问网页会被风控（HTTP 412），所以改走 `api.bilibili.com` 的公开接口
  （`view` 拿 cid + `playurl` 的 `fnval=1` 直接返回单文件 mp4），因此 **B 站不需要本地解析器**；
  未登录时 B 站只给到 720p，番剧/课程需要登录 Cookie（会给出明确提示）
- 提供 `/resolver/self-test` 自检接口，一条命令验证「解析 → 下载 → 图片 → 上传」全链路
- 内置去重与冷却（同一条链接 30 分钟内不重复发送）

---

## 架构

```
QQ 群/私聊消息（含链接）
        │
        ▼
┌─────────────────────────────┐
│ NekroAgent + 本插件          │  ① 识别链接 → ② 解析 → ③ 下载/取文件 → ④ 合并转发
│  /resolver/* 任务队列接口     │
└──────────────┬──────────────┘
               │ 解析器主动轮询（长轮询，含密钥）
               ▼
┌─────────────────────────────┐
│ media-resolver（家里电脑）    │  无头 Edge + CDP：打开真实页面，抓 __INITIAL_STATE__
│  抖音/小红书直链、TikTok 下载  │  住宅 IP + 真实浏览器，避开机房 IP 风控
└─────────────────────────────┘
```

任务流转：

1. 插件发现链接 → 若是抖音/小红书/TikTok，把任务放进队列并**长轮询等待**结果
2. 解析器轮询取任务 → 用浏览器解析（TikTok 则直接代理下载文件）→ 回报直链/文件
3. 插件下载直链（TikTok 用解析器给的文件）→ 生成临时链接 `/media/{token}` → `send_group_forward_msg`
4. `NapCat` 从临时链接取文件并上传到 QQ（NapCat 与解析器同机时可直接读本机文件，见下）

---

## 安装

### 1) 安装插件（NekroAgent）

把 `plugin/plugin.py` 放到 NekroAgent 的插件目录，重启容器：

```bash
# 形如：
docker cp plugin/plugin.py nekro_agent:/app/plugins/workdir/video_forward/plugin.py
docker restart nekro_agent
```

然后在 NekroAgent 的插件配置里填写（见下面的配置表），至少需要：

- `public_base_url`：NapCat 能访问到的 NekroAgent 地址（如 `http://1.2.3.4:8022`）
- `resolver_token`：自己随便生成一串足够长的随机字符，解析器要用同一个值

### 2) 安装本地解析器（可选，但抖音/小红书/TikTok 需要）

需要 Windows + Node.js ≥ 18 + Edge（系统自带即可）。

```powershell
mkdir C:\media-resolver
# 复制 resolver/ 下的三个文件进去：resolver.js、agent.js、start.bat
copy resolver\config.example.json C:\media-resolver\config.json
# 编辑 config.json：server 填你的 NekroAgent 地址，token 与插件里的 resolver_token 一致
```

双击 `start.bat` 启动（或用 `resolver/install-autostart.ps1` 配开机自启）。
启动日志里出现 `解析器已启动，服务器： ...` 即成功；插件状态接口会显示 `"online": true`。

> 解析器首次启动会预热一次无头 Edge，若报 `浏览器预热失败`，见
> [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) 里的「浏览器 profile 被占用」。

---

## 两种部署形态

TikTok 这类"由解析器在本机下好文件"的平台，交付方式取决于 NapCat 装在哪：

| 形态 | NapCat 位置 | 需要的配置 | 交付方式 |
|---|---|---|---|
| **同机**（推荐家用） | 与解析器同一台 Windows | `allow_resolver_local_path: true` | 解析器回报 `local_path`，插件直接把该路径交给 NapCat（**零中转、最快**） |
| **分离**（NapCat 上云） | 云服务器上 | `allow_resolver_local_path: false` | 解析器先把文件 `POST` 到插件的 `/resolver/upload`，插件再用 `/media/{token}` 交付 |

抖音/小红书视频两种形态都一样：插件在服务器侧下载直链，再经临时链接交付。

---

## 配置（插件）

| 键 | 默认 | 说明 |
|---|---|---|
| `public_base_url` | `http://127.0.0.1:8022` | NapCat 访问 NekroAgent 的地址（不带 `/webui`） |
| `max_file_size_mb` | `60` | 单个视频/图片体积上限 |
| `expiry_minutes` | `30` | `/media/{token}` 临时链接有效期 |
| `cooldown_seconds` | `3` | 同一聊天两次处理的间隔 |
| `yt_dlp_path` | `/usr/local/bin/yt-dlp` | 小红书兜底（被重定向到登录页时）用的 yt-dlp 路径 |
| `resolver_enabled` | `true` | 是否启用本地解析器 |
| `resolver_token` | `""` | 解析器通信密钥（**必填**，与解析器 `config.json` 一致） |
| `resolver_timeout` | `120` | 等解析器结果的最长时间（秒） |
| `resolver_stale_seconds` | `60` | 多久没心跳算离线 |
| `send_timeout` | `300` | 合并转发调用的超时（大视频上传慢，别太小） |
| `max_images` | `9` | 图文笔记最多发送张数（1~20） |
| `bilibili_max_size_mb` | `300` | B 站视频体积上限（MB），超过不发并提示实际大小 |
| `bilibili_cookie` | `""` | B 站登录态（填 `SESSDATA`）：留空=匿名 720p；填了才能拿到 1080p/会员视频 |
| `bilibili_ffmpeg_path` | `""` | 合并 DASH 音视频用的 ffmpeg，留空自动查找挂载目录再找 PATH |
| `x_enabled` | `true` | 是否解析 X (Twitter) 帖子（公开嵌入接口，无需登录 / 解析器）；关闭后 X 链接被忽略 |
| `x_max_size_mb` | `100` | X 视频体积上限（MB），自动挑不超过上限的最高码率 mp4，超限则不发并提示实际大小 |
| `x_send_text` | `true` | 合并转发里在图片 / 视频前附上作者、北京时间、正文和被引用帖子；关闭后只发媒体（纯文字帖会提示无内容） |
| `x_proxy` | `""` | X 走代理的地址（大陆机房必填，如 `http://172.18.0.1:7891`） |
| `x_show_stats` | `true` | 文字里是否带点赞 / 回复数 |
| `instagram_enabled` | `true` | 是否解析 Instagram 帖子（匿名公开页，无需登录 / 解析器）；关闭后 Instagram 链接被忽略 |
| `instagram_max_size_mb` | `100` | Instagram 视频体积上限（MB），超限则不发并提示实际大小 |
| `instagram_fallback_ua` | `true` | **灰色做法（伪装爬虫 UA），默认开启，可设为 `false` 关闭**：先用诚实 UA，只有它拿不到数据时才再用 Googlebot UA 重试一次 |
| `instagram_proxy` | `""` | Instagram 走代理的地址（帖子页 / 媒体下载 / yt-dlp 回退；留空 = 直连，如 `http://172.18.0.1:7891`） |
| `facebook_proxy` | `""` | Facebook 走代理的地址（同上，两者互相独立） |
| `facebook_enabled` | `true` | 是否解析 Facebook 帖子（匿名公开页，无需登录 / 解析器）；关闭后 Facebook 链接被忽略 |
| `facebook_max_size_mb` | `100` | Facebook 视频体积上限（MB），超限则不发并提示实际大小 |
| `facebook_send_comments` | `false` | 文字里是否附最多 5 条页面上匿名可见的热门评论 |
| `allow_resolver_local_path` | `false` | NapCat 与解析器同机时设为 `true` |
| `forward_nickname` | `机器人` | 合并转发卡片里显示的昵称（发送者账号取自 `bot.self_id`） |

## 配置（解析器）

| 键 | 说明 |
|---|---|
| `server` | NekroAgent 地址，如 `http://1.2.3.4:8022` |
| `token` | 与插件 `resolver_token` 一致 |
| `browser` | Edge 可执行文件路径；留空自动查找 |
| `proxy` | 浏览器代理（可选） |
| `tiktokProxy` | TikTok 下载用的代理，如 `http://127.0.0.1:7897` |
| `ytDlpPath` | yt-dlp 可执行文件路径（TikTok 下载用） |
| `downloadDir` | 下载目录，默认脚本同目录 `downloads` |
| `maxDownloadMb` / `keepHours` | 单文件上限 / 下载文件保留小时数 |
| `maxUploadMb` | 上传到服务器的上限（分离形态用） |

其余为超时与预热参数，默认值适用于家用宽带。

---

## 自检

一条命令验证全链路（`<token>` 换成 `resolver_token`，`<url>` 换成目标链接）：

```bash
curl "http://<你的服务器>:8022/plugins/local_video.video_forward/resolver/self-test?token=<token>&url=<url>&download=true"
```

返回示例：

```json
{"ok":true,"platform":"douyin","video_count":11,"image_count":0,"downloaded_mb":4.9}
{"ok":true,"platform":"xiaohongshu","mode":"images","image_count":5,"first_image_mb":0.05}
{"ok":true,"platform":"tiktok","mode":"local_file","local_path":"C:\\media-resolver\\downloads\\tt_xxx.mp4",
 "local_path_mode":"hand-to-napcat"}
```

---

## 常见问题

见 [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)，包括：

- `浏览器进程在启动阶段就退出了`（profile 被残留 Edge 占用）
- 抖音/小红书明明有直链却发不出（机房 IP 风控 → 必须走本地解析器）
- `TikTok 需要解析器把文件上传到服务器`（部署形态配置错）
- `正向Websocket消息` 看不到（NapCat 的 debug 开关）
- TikTok 短链大小写导致 `Unsupported URL`

---

## 免责声明

- 本项目仅供**个人学习与自用**，请遵守各平台的服务条款与所在地法律。
- 请只处理**公开**内容；不支持也不应绕过付费、私密、仅好友可见或需要登录的内容。
- 下载内容的使用需尊重原作者版权；转发到公开群聊前请自行确认授权与合规性。
- 平台接口随时可能变化导致失效，作者不提供任何可用性保证。

## 参与开发

见 [CONTRIBUTING.md](CONTRIBUTING.md)。提交前请确认没有把 `config.yaml` / `config.json` / token 带进来（CI 有一道扫描会拦）。

## License

**PolyForm Noncommercial 1.0.0** —— 个人使用、修改、再分发都允许；**禁止任何商业用途**（包括把基于本项目的东西作为商品或服务提供给他人）。全文见 [LICENSE](LICENSE)。

> ⚠️ 这不是 OSI 定义的开源许可，属于 source-available（源码可见）。如需商业授权，请联系作者。
> **大陆机房部署注意（X）**：X 在大陆网络下不可直连，`cdn.syndication.twimg.com` 与 `video.twimg.com` 都会超时。
> 请配置 `x_proxy` 指向你的代理（容器内要用宿主机在 docker 网桥上的地址，例如 `http://172.18.0.1:7891`，
> 不能用 `127.0.0.1`——那是容器自己）。B 站 / 抖音 / 小红书 / TikTok 不需要这个代理。
