# 51cg1 每日增量爬虫使用说明

## 功能概述

- 爬取 `https://51cg1.com/category/wpcz/` 分页（第 1~4 页，新内容在第一页最前面）
- 筛选标题含关键词（内射 / 巨乳 / 大奶 / 无套 / 乘骑）的帖子
- 提取帖子页面（含 iframe 播放页）中的 m3u8 地址，下载并合并为本地视频
- 已下载的帖子 URL 写入 `downloaded.db`（SQLite），重复运行自动跳过，不重复下载
- 使用 curl_cffi 模拟真实浏览器 TLS/HTTP2 指纹（过 Cloudflare 盾），自动在多个 Chrome/Safari/Firefox 指纹间切换
- 自动检测系统代理（Windows 注册表 / 环境变量），也可在 `PROXIES` 手动指定
- 自动处理 AES-128 加密 HLS 流（站点视频流为加密流，已实测解密正常）
- 请求间隔 2 秒 + 失败重试；本页命中帖子全部已下载过时提前停止翻页

## 目录结构

```
crawler-51cg/
├── crawler.py          # 主脚本(爬取+下载)
├── server.py           # 媒体库服务(列表+播放+已看标记)
├── viewer.html         # 媒体库前端页面
├── vendor/mpegts.min.js# .ts 浏览器软解组件(本地内置, 无需联网)
├── requirements.txt    # Python 依赖
├── videos/             # 视频保存目录（自动创建）
├── downloaded.db       # SQLite（去重 + 会话状态 + 已看记录）
└── crawler.log         # 运行日志
```

## 媒体库：本地播放服务

```bash
python server.py               # 启动后自动打开 http://127.0.0.1:8787
python server.py --port 9000   # 换端口
python server.py --host 0.0.0.0 --no-open   # 允许局域网访问(注意隐私)
```

- 视频按下载时间**倒序**排列（最新在最上面），点击即播
- 播放过的视频标题前自动加 ✅，记录持久化在 `viewed_posts` 表，重开不丢
- 支持 HTTP Range，可随意拖动进度条
- `.mp4` 原生播放；`.ts` 用内置的 mpegts.js 软解播放（已本地化，无需联网）
- 与爬虫共用同一个 `downloaded.db`，爬虫下载完点「刷新列表」即可看到

## 安装依赖

```bash
cd crawler-51cg
pip install -r requirements.txt
```

依赖说明：

- **curl_cffi**（必需）：模拟浏览器 TLS 指纹。本站开了 Cloudflare 盾，普通 requests/urllib 会被 403 拦截，实测 chrome133a 指纹可稳定通过
- **pycryptodome**（必需）：站点视频流为 AES-128 加密 HLS，纯 Python 下载模式靠它逐段解密
- **ffmpeg**（可选但推荐）：安装后走 ffmpeg 拉流合并为 mp4（更快、兼容性更好）；未安装时自动降级为纯 Python 模式（拼接为 .ts，绝大多数播放器可直接播）

ffmpeg 安装（可选）：

- Windows: `winget install Gyan.FFmpeg`，装完重开终端
- 未安装不影响运行（已实测纯 Python 模式可完整下载加密流）

## 手动运行

```bash
python crawler.py                  # 正式执行一轮
python crawler.py --dry-run        # 只列出命中帖子，不下载（先测试）
python crawler.py --max-pages 4    # 指定爬取页数
```

## 每天定时运行

### 方案 A：Windows 任务计划程序（推荐，关机后不补跑）

以管理员身份运行一次下面这条命令（每天 09:00 执行，路径按实际修改）：

```bat
schtasks /Create /TN "51cg1爬虫" /TR "cmd /c cd /d C:\path\to\crawler-51cg && python crawler.py >> run.log 2>&1" /SC DAILY /ST 09:00
```

删除任务：`schtasks /Delete /TN "51cg1爬虫" /F`

### 方案 B：脚本常驻模式

```bash
python crawler.py --loop                   # 每 24 小时一轮
python crawler.py --loop --interval-hours 12
```

缺点：需要一直开着终端/电脑。

## 常用修改位置（crawler.py 顶部配置区）

| 想改什么 | 改哪里 |
|---|---|
| 关键词 | `KEYWORDS` 列表 |
| 爬取页数 | `MAX_PAGES`（或运行时加 `--max-pages`） |
| 请求间隔 | `DELAY`（秒，被反爬时调大） |
| 代理 | `PROXIES = {"http": "...", "https": "..."}`（`None`=自动检测系统代理，`{}`=禁用） |
| 浏览器指纹 | `IMPERSONATE_CANDIDATES` 列表（按优先级） |
| 视频保存目录 | `VIDEO_DIR` |
| 视频文件名长度 | `safe_filename(name, max_len=80)` |

## 数据库操作备忘

```bash
sqlite3 downloaded.db
SELECT title, video_file, downloaded_at FROM downloaded_posts ORDER BY id DESC;  -- 查看已下载
DELETE FROM downloaded_posts WHERE url = '...';   -- 删除某条记录（下次会重新下载）
SELECT COUNT(*) FROM downloaded_posts;            -- 统计总数
```

## 常见问题

- **所有指纹都被 Cloudflare 拦截**：站点临时调高了防护等级。先重试几次；仍不行换网络/代理出口 IP（在 `PROXIES` 手动指定），并在 `IMPERSONATE_CANDIDATES` 里补充 curl_cffi 支持的新指纹。
- **直连超时 / 连不上**：本机网络无法直达该站点，需要代理。脚本会自动读系统代理（注册表/环境变量）；无系统代理时手动配 `PROXIES`。
- **下载的 .ts 播放不了**：装 ffmpeg 后重跑（会输出 mp4）；或用 PotPlayer/VLC 直接播 .ts。
- **日志里出现 SSL warning**：脚本已自动降级为不校验证书，不影响使用。
- **想知道跑了什么**：看 `crawler.log`，每页发现数、命中数、每个帖子的下载状态都有记录。

## 实测记录（2026-08-28）

- chrome133a 指纹通过 Cloudflare 验证（chrome124/131 会被拦，脚本会自动切换）
- 第 1 页 46 帖命中 5~6 个，第 2 页命中 5 个，关键词筛选正常
- 视频流确认为 AES-128 加密 HLS（约 190~200 分段/部），纯 Python 模式完整下载并解密成功（121.6 MB）
- SQLite 去重验证：已下载帖子 `is_downloaded => True`，新帖 `False`
