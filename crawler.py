#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多站点每日增量爬虫
==================
功能:
  1. 从 SQLite config 表读取 key='websites' 的 JSON 配置
  2. 按每个站点的 domain / page_parse / include_keyword / exclude_keyword / max_page 爬取
  3. 提取帖子页面中的 m3u8(HLS) 地址并下载合并到本地 videos/ 目录
  4. 已下载帖子写入本地 SQLite(downloaded.db), 重复运行自动跳过
  5. 过盾后的 Cookie(含 cf_clearance)与浏览器指纹持久化到 SQLite session_state 表
  6. 全程模拟浏览器请求头与 TLS 指纹, 带请求间隔与重试

配置示例 (config 表 key='websites' 的 value):
  domain 支持直接写 {page} 占位, 例如:
  [{"domain":"https://yvl3e.ibeeuzscf.cc/order/today/page/{page}/",
    "page_parse":"archives/{\\d+}/",
    "include_keyword":["内射", "巨乳", "大奶", "无套", "乘骑","美乳","吊钟"],
    "exclude_keyword":["日本"],
    "max_page":4}]

用法:
  python crawler.py                  # 单次执行
  python crawler.py --dry-run        # 只列出命中帖子, 不下载(测试用)
  python crawler.py --loop           # 常驻模式, 每 24 小时自动执行一次
  python crawler.py --loop --interval-hours 12
"""

import argparse
import json
import logging
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from http.cookiejar import Cookie
from pathlib import Path
from urllib.parse import urljoin, urlparse

try:
    from curl_cffi import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("缺少依赖, 请先执行: pip install -r requirements.txt")
    sys.exit(1)

# curl_cffi 支持的浏览器指纹(按优先级尝试, 任一拿到非挑战页即固定使用)
IMPERSONATE_CANDIDATES = ["chrome133a", "chrome131", "safari18_0", "firefox133"]

# ==================== 可按需修改的配置 ====================
WORK_DIR = Path(__file__).resolve().parent
VIDEO_DIR = WORK_DIR / "videos"        # 视频保存目录
DB_PATH = WORK_DIR / "downloaded.db"   # SQLite 数据库(去重 + 配置 + 会话)
LOG_PATH = WORK_DIR / "crawler.log"

TIMEOUT = 30          # 单请求超时(秒)
DELAY = 2.0           # 两次请求间隔(秒), 太小容易触发反爬
SEG_RETRIES = 3       # 下载重试次数
PROXIES = None        # None = 自动检测系统代理
                      # 需要手动指定时改为, 例如:
                      # {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
                      # 不想用任何代理则改为 {}

# =========================================================
FALLBACK_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36")

M3U8_RE = re.compile(r"https?://[^\s\"'<>\\]+?\.m3u8(?:\?[^\s\"'<>\\]*)?", re.I)
# 明显不是帖子正文的链接
SKIP_HREF_PARTS = (
    "/category/", "/tag/", "/page/", "/wp-content/", "/wp-admin/",
    "/wp-login", "/wp-json", "#", "mailto:", "javascript:", "tg://", "t.me",
)


def setup_logging():
    """日志同时输出到控制台和 crawler.log"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt_short = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    fmt_full = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt_short)
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt_full)
    root.addHandler(sh)
    root.addHandler(fh)


# ----------------------- 数据库 -----------------------

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS downloaded_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE NOT NULL,
                title TEXT,
                video_file TEXT,
                downloaded_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        """)
        # Cloudflare/站点会话状态: 指纹 + Cookie(含 cf_clearance) + 请求头
        conn.execute("""
            CREATE TABLE IF NOT EXISTS session_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                impersonate TEXT NOT NULL,
                cookies TEXT NOT NULL,
                headers TEXT,
                user_agent TEXT,
                saved_at TEXT DEFAULT (datetime('now', 'localtime')),
                last_ok_at TEXT
            )
        """)
        # 配置表: key-value, 其中 key='websites' 存放站点列表 JSON
        conn.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY NOT NULL,
                value TEXT NOT NULL
            )
        """)


def load_websites_config():
    """
    从 config 表读取 key='websites' 的 value (JSON 数组).
    返回 list[dict], 每项含 domain / page_parse / include_keyword /
    exclude_keyword / max_page.
    """
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT value FROM config WHERE key = ?", ("websites",)
        ).fetchone()
    if not row or not row[0]:
        logging.error("config 表中未找到 key='websites', 请先写入配置")
        return []
    try:
        data = json.loads(row[0])
    except json.JSONDecodeError as e:
        logging.error("websites 配置 JSON 解析失败: %s", e)
        return []
    if not isinstance(data, list):
        logging.error("websites 配置应为 JSON 数组")
        return []
    sites = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            logging.warning("跳过第 %d 项(非对象): %s", i, item)
            continue
        domain = (item.get("domain") or "").strip()
        if not domain:
            logging.warning("跳过第 %d 项(缺少 domain)", i)
            continue
        page_parse = (item.get("page_parse") or "").strip()
        include_kw = item.get("include_keyword") or []
        exclude_kw = item.get("exclude_keyword") or []
        max_page = int(item.get("max_page") or 4)
        if not isinstance(include_kw, list):
            include_kw = [str(include_kw)]
        if not isinstance(exclude_kw, list):
            exclude_kw = [str(exclude_kw)]
        sites.append({
            "domain": domain,
            "page_parse": page_parse,
            "include_keyword": [str(k) for k in include_kw if k],
            "exclude_keyword": [str(k) for k in exclude_kw if k],
            "max_page": max(1, max_page),
        })
    return sites


def is_downloaded(conn, url):
    cur = conn.execute("SELECT 1 FROM downloaded_posts WHERE url = ?", (url,))
    return cur.fetchone() is not None


def mark_downloaded(conn, url, title, video_file):
    conn.execute(
        "INSERT OR IGNORE INTO downloaded_posts (url, title, video_file) VALUES (?, ?, ?)",
        (url, title, video_file),
    )
    conn.commit()


# ----------------- 会话状态持久化(Cookie/指纹) -----------------

def cookie_to_dict(c):
    """http.cookiejar.Cookie -> 可序列化 dict"""
    return {
        "name": c.name, "value": c.value,
        "domain": c.domain, "path": c.path,
        "secure": bool(c.secure), "expires": c.expires,
    }


def dict_to_cookie(d):
    """dict -> http.cookiejar.Cookie(还原回 curl_cffi 会话的 cookiejar)"""
    return Cookie(
        version=0, name=d["name"], value=d["value"],
        port=None, port_specified=False,
        domain=d.get("domain") or "", domain_specified=bool(d.get("domain")),
        domain_initial_dot=d.get("domain", "").startswith("."),
        path=d.get("path") or "/", path_specified=True,
        secure=bool(d.get("secure")),
        expires=d.get("expires"), discard=d.get("expires") is None,
        comment=None, comment_url=None, rest={}, rfc2109=False,
    )


def save_session_state(impersonate, session, extra_headers=None):
    """把过盾后的指纹 + 全部 Cookie 写入 session_state 表(单行覆盖)"""
    try:
        cookies = [cookie_to_dict(c) for c in session.cookies.jar]
    except AttributeError:
        cookies = []
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO session_state (id, impersonate, cookies, headers, user_agent, saved_at, last_ok_at)
            VALUES (1, ?, ?, ?, '', datetime('now','localtime'), datetime('now','localtime'))
            ON CONFLICT(id) DO UPDATE SET
                impersonate=excluded.impersonate,
                cookies=excluded.cookies,
                headers=excluded.headers,
                user_agent=excluded.user_agent,
                saved_at=excluded.saved_at,
                last_ok_at=excluded.last_ok_at
        """, (
            impersonate,
            json.dumps(cookies, ensure_ascii=False),
            json.dumps(extra_headers or {}, ensure_ascii=False),
        ))
    logging.info("会话状态已入库: 指纹=%s, Cookie %d 个(含 cf_clearance=%s)",
                 impersonate, len(cookies),
                 "有" if any(c["name"] == "cf_clearance" for c in cookies) else "无")


def load_session_state():
    """读取已保存的会话状态, 无则返回 None"""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT impersonate, cookies, headers FROM session_state WHERE id = 1"
            ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    try:
        return {
            "impersonate": row[0],
            "cookies": json.loads(row[1]),
            "headers": json.loads(row[2] or "{}"),
        }
    except (json.JSONDecodeError, TypeError):
        return None


def mark_session_ok():
    """会话验证有效时刷新 last_ok_at"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE session_state SET last_ok_at = datetime('now','localtime') WHERE id = 1")


# ----------------------- HTTP -----------------------

def detect_system_proxies():
    """自动检测系统代理: 先看环境变量, 再读 Windows 注册表 Internet Settings"""
    import os
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY"):
        val = os.environ.get(key)
        if val:
            return {"http": val, "https": val}
    if sys.platform == "win32":
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                               r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
            enabled = winreg.QueryValueEx(k, "ProxyEnable")[0]
            server = winreg.QueryValueEx(k, "ProxyServer")[0]
            winreg.CloseKey(k)
            if enabled and server:
                if "=" in server:  # 形如 http=...;https=...
                    for part in server.split(";"):
                        if part.startswith(("https=", "http=")):
                            server = part.split("=", 1)[1]
                            break
                url = server if "://" in server else f"http://{server}"
                logging.info("检测到系统代理: %s", url)
                return {"http": url, "https": url}
        except Exception:
            pass
    return {}


def challenge_blocked(resp):
    """判断响应是否为 Cloudflare 挑战页"""
    if resp.status_code == 403 and "just a moment" in resp.text.lower():
        return True
    if resp.headers.get("Cf-Mitigated", "").lower() == "challenge":
        return True
    return False


def inject_cookies(session, cookie_dicts):
    """把入库的 cookie 列表还原进会话"""
    for d in cookie_dicts:
        try:
            session.cookies.jar.set_cookie(dict_to_cookie(d))
        except Exception:
            try:
                session.cookies.set(d["name"], d["value"],
                                    domain=d.get("domain") or "",
                                    path=d.get("path") or "/")
            except Exception:
                pass


def make_headers(base_url):
    """按站点生成请求头"""
    return {
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,*/*;q=0.8"),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": base_url.rstrip("/") + "/",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
    }


def try_restore_session(base_url, headers):
    """用库里保存的指纹+Cookie 重建会话并验证; 有效返回会话, 无效返回 None"""
    state = load_session_state()
    if not state or state["impersonate"] not in IMPERSONATE_CANDIDATES:
        return None
    imp = state["impersonate"]
    if not state["cookies"]:
        return None
    proxies = PROXIES if PROXIES is not None else detect_system_proxies()
    s = requests.Session(impersonate=imp, proxies=proxies or None, timeout=TIMEOUT)
    inject_cookies(s, state["cookies"])
    for k, v in state["headers"].items():
        s.headers[k] = v
    try:
        r = s.get(base_url.rstrip("/") + "/", headers=headers)
        if not challenge_blocked(r) and r.status_code == 200:
            logging.info("复用入库会话成功: 指纹=%s, Cookie %d 个, 免过盾直连",
                         imp, len(state["cookies"]))
            mark_session_ok()
            save_session_state(imp, s, state["headers"])
            return s
        logging.info("入库会话已失效(状态码 %s), 重新协商指纹", r.status_code)
    except Exception as e:
        logging.info("入库会话验证失败: %s, 重新协商指纹", str(e)[:100])
    return None


def build_session(base_url, headers):
    """构建会话: 优先复用库里的 Cookie+指纹, 失效再逐个试指纹, 成功后入库"""
    restored = try_restore_session(base_url, headers)
    if restored is not None:
        return restored
    proxies = PROXIES if PROXIES is not None else detect_system_proxies()
    for imp in IMPERSONATE_CANDIDATES:
        try:
            s = requests.Session(impersonate=imp, proxies=proxies or None, timeout=TIMEOUT)
            r = s.get(base_url.rstrip("/") + "/", headers=headers)
            if challenge_blocked(r):
                logging.info("指纹 %s 被 Cloudflare 拦截, 换下一个", imp)
                continue
            logging.info("浏览器指纹选定: %s (状态码 %d)", imp, r.status_code)
            save_session_state(imp, s, dict(headers))
            return s
        except Exception as e:
            logging.warning("指纹 %s 测试失败: %s", imp, e)
    logging.warning("所有浏览器指纹均未通过测试, 使用 %s 继续尝试", IMPERSONATE_CANDIDATES[0])
    return requests.Session(impersonate=IMPERSONATE_CANDIDATES[0],
                            proxies=proxies or None, timeout=TIMEOUT)


def request_with_retry(session, url, *, retries=SEG_RETRIES, headers=None):
    """带重试的 GET; 遇到 Cloudflare 挑战页也计入重试"""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, timeout=TIMEOUT, headers=headers)
            if challenge_blocked(resp):
                raise RuntimeError("Cloudflare 挑战页(指纹可能已失效)")
            resp.raise_for_status()
            return resp
        except Exception as e:
            last_err = e
            wait = 2 * attempt
            logging.warning("请求失败(%d/%d) %s: %s -> %ds 后重试",
                            attempt, retries, url, str(e)[:120], wait)
            time.sleep(wait)
    raise RuntimeError(f"重试 {retries} 次仍失败: {url} ({last_err})")


# ----------------------- URL / 分页辅助 -----------------------

def parse_site_base(domain_url):
    """
    从配置的 domain 解析出:
      - origin: 协议+主机, 如 https://yvl3e.ibeeuzscf.cc
      - list_template: 列表页 URL 模板, 用 {page} 占位

    优先使用配置里已写的 {page}:
      https://xxx/order/today/page/{page}/
      https://xxx/category/wpcz/{page}/
    若无 {page}, 再尝试从 .../page/1/ 或末尾数字推导, 否则追加 /page/{page}/
    """
    domain_url = (domain_url or "").strip()
    # 清理多余斜杠: page/{page}// -> page/{page}/
    domain_url = re.sub(r"(?<!:)/{2,}", "/", domain_url)

    parsed = urlparse(domain_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    if "{page}" in domain_url:
        return origin, domain_url

    path = parsed.path or "/"
    # 匹配 /page/数字 或 路径末尾纯数字
    m = re.search(r"(.*?/page/)(\d+)(/?)$", path, re.I)
    if m:
        prefix, _, suffix = m.group(1), m.group(2), m.group(3)
        template = origin + prefix + "{page}" + (suffix or "/")
        return origin, template
    m2 = re.search(r"(.*?/)(\d+)(/?)$", path)
    if m2 and m2.group(2).isdigit():
        prefix, _, suffix = m2.group(1), m2.group(2), m2.group(3)
        template = origin + prefix + "{page}" + (suffix or "/")
        return origin, template
    # 无页码: 在 path 后追加 page/{n}/
    base_path = path.rstrip("/")
    template = origin + base_path + "/page/{page}/"
    return origin, template


def build_list_url(template, page):
    """用 page 填充模板; 兼容 {page} 与多余斜杠"""
    url = template.replace("{page}", str(page))
    # 再次折叠多余斜杠(保留 https://)
    url = re.sub(r"(?<!:)/{2,}", "/", url)
    return url


def compile_page_parse(pattern):
    """
    把配置里的 page_parse 转成正则.
    支持常见写法:
      archives/{\d+}/   -> archives/\d+/
      archives/(\d+)/   原样使用
      纯字符串则 re.escape 后匹配
    """
    if not pattern:
        return None
    # 把 {\d+} 这类占位转成 \d+
    p = pattern.replace(r"{\d+}", r"\d+")
    p = p.replace("{d+}", r"\d+")
    # 若用户已写了正则元字符, 尽量保持; 否则做宽松匹配
    try:
        return re.compile(p, re.I)
    except re.error:
        return re.compile(re.escape(pattern), re.I)


# ----------------------- 页面解析 -----------------------

def extract_posts(html, base_url, origin, page_parse_re=None):
    """
    从列表页提取 (帖子URL, 标题) 列表, 保持页面出现顺序.
    若提供 page_parse_re, 则只保留 href 匹配该正则的链接.
    """
    soup = BeautifulSoup(html, "html.parser")
    found = {}
    for a in soup.find_all("a", href=True):
        title = a.get_text(strip=True)
        if not title or len(title) < 4:
            continue
        href = urljoin(base_url, a["href"].strip())
        if not href.startswith("http"):
            continue
        if any(s in href for s in SKIP_HREF_PARTS):
            continue
        # 只收录本站链接
        if not href.startswith(origin):
            continue
        path = href[len(origin):]
        if path in ("", "/"):
            continue
        # 若配置了 page_parse, 用正则过滤帖子链接
        if page_parse_re is not None:
            if not page_parse_re.search(href) and not page_parse_re.search(path):
                continue
        found[href] = title
    return list(found.items())


def title_matches(title, include_kw, exclude_kw):
    """包含任一 include 关键词, 且不包含任一 exclude 关键词"""
    if include_kw and not any(k in title for k in include_kw):
        return False
    if exclude_kw and any(k in title for k in exclude_kw):
        return False
    return True


def extract_m3u8s(html):
    """从页面源码(含内联 JS)提取 m3u8 地址"""
    text = html.replace("\\/", "/")  # 还原 JS 转义
    return list(dict.fromkeys(M3U8_RE.findall(text)))


def extract_iframes(html, base_url):
    """提取页面中的 iframe 播放器地址(m3u8 可能藏在第三方播放页里)"""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tag in soup.find_all("iframe", src=True):
        src = urljoin(base_url, tag["src"].strip())
        if src.startswith("http"):
            out.append(src)
    return out


def safe_filename(name, max_len=80):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip(" ._")
    return name[:max_len]


# ----------------------- 视频下载 -----------------------

def has_ffmpeg():
    return False
    return shutil.which("ffmpeg") is not None


def download_hls_ffmpeg(m3u8_url, out_path, referer_origin):
    """首选方案: ffmpeg 直接拉流合并为 mp4(自动处理加密/多码率)"""
    header = f"Referer: {referer_origin}/\r\nUser-Agent: {FALLBACK_UA}"
    cmd = [
        "ffmpeg", "-y",
        "-loglevel", "error", "-stats",
        "-user_agent", FALLBACK_UA,
        "-headers", header,
        "-i", m3u8_url,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        str(out_path),
    ]
    logging.info("ffmpeg 拉流: %s -> %s", m3u8_url, out_path.name)
    subprocess.run(cmd, check=True, timeout=7200)


def download_hls_python(session, m3u8_url, out_base, referer_origin):
    """无 ffmpeg 时的回退: 解析 m3u8 -> 逐段下载(AES-128 自动解密) -> 拼接为 .ts"""
    resp = request_with_retry(session, m3u8_url, headers={"Referer": referer_origin + "/"})
    text = resp.text

    # master 播放列表 -> 取第一个子播放列表
    if "#EXT-X-STREAM-INF" in text:
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        sub_url = None
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF"):
                for j in range(i + 1, len(lines)):
                    if not lines[j].startswith("#"):
                        sub_url = urljoin(m3u8_url, lines[j])
                        break
                if sub_url:
                    break
        if not sub_url:
            raise RuntimeError("master m3u8 中未找到子播放列表")
        logging.info("检测到多码率列表, 切换到: %s", sub_url)
        m3u8_url = sub_url
        text = request_with_retry(session, sub_url,
                                  headers={"Referer": referer_origin + "/"}).text

    # AES-128 加密流: 取密钥, 逐段解密(需要 pycryptodome)
    key, default_iv, seq0 = None, None, 0
    aes = re.search(r'#EXT-X-KEY:METHOD=AES-128,URI="([^"]+)"(?:,IV=0x([0-9A-Fa-f]+))?', text)
    if aes:
        try:
            from Crypto.Cipher import AES
        except ImportError:
            raise RuntimeError("HLS 为 AES-128 加密, 请安装 ffmpeg 或执行 "
                               "pip install pycryptodome 后重试")
        key_url = urljoin(m3u8_url, aes.group(1))
        key = request_with_retry(session, key_url,
                                 headers={"Referer": m3u8_url}).content
        if aes.group(2):
            default_iv = bytes.fromhex(aes.group(2))
        ms = re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", text)
        seq0 = int(ms.group(1)) if ms else 0
        logging.info("HLS 为 AES-128 加密, 已获取密钥(%d 字节), 将逐段解密", len(key))

    segs = [l.strip() for l in text.splitlines()
            if l.strip() and not l.startswith("#")]
    if not segs:
        raise RuntimeError("m3u8 中未解析到视频分段")

    total = len(segs)
    logging.info("纯 Python 拉流, 共 %d 个分段", total)
    tmp_path = out_base.with_suffix(".ts.part")
    final_path = out_base.with_suffix(".ts")
    with open(tmp_path, "wb") as f:
        for i, seg in enumerate(segs, 1):
            seg_url = urljoin(m3u8_url, seg)
            data = request_with_retry(
                session, seg_url, headers={"Referer": m3u8_url}).content
            if key:
                from Crypto.Cipher import AES
                iv = default_iv or (seq0 + i - 1).to_bytes(16, "big")
                data = AES.new(key, AES.MODE_CBC, iv).decrypt(data)
                pad = data[-1]
                if 1 <= pad <= 16:
                    data = data[:-pad]
            f.write(data)
            if i % 10 == 0 or i == total:
                logging.info("  分段进度 %d/%d", i, total)
    tmp_path.replace(final_path)
    return final_path


# ----------------------- 主流程 -----------------------

def process_post(conn, session, url, title, origin, headers, dry_run=False):
    """处理单个命中帖子, 成功下载返回 True"""
    if dry_run:
        logging.info("[dry-run] 命中: %s | %s", title, url)
        return False

    logging.info("▶️处理帖子: %s | %s", title, url)
    html = request_with_retry(session, url, headers=headers).text

    m3u8s = extract_m3u8s(html)
    if not m3u8s:
        for ifr in extract_iframes(html, url)[:3]:
            try:
                logging.info("检查 iframe 播放器: %s", ifr)
                ihtml = request_with_retry(session, ifr, headers=headers).text
                found = extract_m3u8s(ihtml)
                if found:
                    m3u8s = found
                    break
            except Exception as e:
                logging.warning("iframe 抓取失败 %s: %s", ifr, e)
            time.sleep(DELAY)

    if not m3u8s:
        logging.warning("未找到 m3u8 地址, 跳过: %s", url)
        return False

    m3u8_url = m3u8s[0]
    logging.info("📌找到 m3u8: %s", m3u8_url)

    VIDEO_DIR.mkdir(exist_ok=True)
    stem = safe_filename(title) or f"post_{int(time.time())}"
    out_base = VIDEO_DIR / stem
    try:
        if has_ffmpeg():
            out_path = out_base.with_suffix(".mp4")
            download_hls_ffmpeg(m3u8_url, out_path, origin)
        else:
            out_path = Path(download_hls_python(session, m3u8_url, out_base, origin))
        size_mb = out_path.stat().st_size / 1048576
        if size_mb < 0.05:
            raise RuntimeError(f"文件过小({size_mb:.2f} MB), 可能不完整")
        mark_downloaded(conn, url, title, str(out_path))
        logging.info("✅完成: %s (%.1f MB)", out_path.name, size_mb)
        return True
    except Exception as e:
        logging.error("❌下载失败 %s: %s", url, e)
        for p in VIDEO_DIR.glob(stem + ".*"):
            if p.suffix == ".part":
                try:
                    p.unlink()
                except OSError:
                    pass
        return False


def run_site(conn, site, dry_run=False):
    """爬取单个站点配置, 返回新增下载数"""
    domain = site["domain"]
    origin, list_template = parse_site_base(domain)
    page_parse_re = compile_page_parse(site["page_parse"])
    include_kw = site["include_keyword"]
    exclude_kw = site["exclude_keyword"]
    max_pages = site["max_page"]
    headers = make_headers(origin)

    logging.info("=" * 60)
    logging.info("站点: %s", origin)
    logging.info("列表模板: %s", list_template)
    logging.info("page_parse: %s", site["page_parse"] or "(无, 宽松匹配)")
    logging.info("include: %s | exclude: %s | max_page: %d",
                 "/".join(include_kw) or "(全部)",
                 "/".join(exclude_kw) or "(无)",
                 max_pages)

    session = build_session(origin, headers)
    new_count = 0
    try:
        for page in range(1, max_pages + 1):
            list_url = build_list_url(list_template, page)
            logging.info("== 列表页 %d/%d: %s", page, max_pages, list_url)
            html = request_with_retry(session, list_url, headers=headers).text
            posts = extract_posts(html, list_url, origin, page_parse_re)
            hits = [(u, t) for u, t in posts
                    if title_matches(t, include_kw, exclude_kw)]
            logging.info("   发现帖子 %d 个, 命中关键词 %d 个", len(posts), len(hits))
            if not hits:
                time.sleep(DELAY)
                continue

            all_seen = True
            for u, t in hits:
                if is_downloaded(conn, u):
                    continue
                all_seen = False
                if process_post(conn, session, u, t, origin, headers, dry_run=dry_run):
                    new_count += 1
                time.sleep(DELAY)

            if all_seen and not dry_run:
                logging.info("   本页命中帖子全部已下载过, 新内容在最前页, 提前停止翻页")
                break
            time.sleep(DELAY)
    finally:
        try:
            save_session_state(
                getattr(session, "impersonate", IMPERSONATE_CANDIDATES[0]),
                session, dict(headers))
        except Exception as e:
            logging.warning("会话状态入库失败: %s", e)
    return new_count


def run_once(args):
    """执行一轮完整爬取(所有配置站点), 返回新增下载数"""
    init_db()
    sites = load_websites_config()
    if not sites:
        logging.error("无可用站点配置, 退出")
        return 0

    conn = sqlite3.connect(DB_PATH)
    total_new = 0
    try:
        for site in sites:
            try:
                n = run_site(conn, site, dry_run=args.dry_run)
                total_new += n
                logging.info("本站新增 %d 个", n)
            except Exception as e:
                logging.error("站点 %s 执行异常: %s", site.get("domain"), e)
    finally:
        conn.close()
    return total_new


def main():
    parser = argparse.ArgumentParser(description="多站点每日增量爬虫 (配置来自 SQLite config 表)")
    parser.add_argument("--dry-run", action="store_true",
                        help="只列出命中的帖子, 不下载")
    parser.add_argument("--loop", action="store_true",
                        help="常驻模式, 每隔一段时间自动执行")
    parser.add_argument("--interval-hours", type=float, default=24.0,
                        help="常驻模式执行间隔(小时, 默认 24)")
    args = parser.parse_args()

    setup_logging()
    init_db()
    sites = load_websites_config()
    logging.info("已加载 %d 个站点配置 | ffmpeg: %s",
                 len(sites),
                 "已安装" if has_ffmpeg() else "未安装(将用纯 Python 模式)")
    for s in sites:
        logging.info("  - %s  max_page=%d  include=%s  exclude=%s",
                     s["domain"], s["max_page"],
                     s["include_keyword"], s["exclude_keyword"])

    if args.loop:
        logging.info("常驻模式: 每 %.1f 小时执行一次, Ctrl+C 退出", args.interval_hours)
        while True:
            try:
                n = run_once(args)
                logging.info("本轮完成, 新增下载 %d 个", n)
            except KeyboardInterrupt:
                logging.info("手动退出")
                break
            except Exception as e:
                logging.error("本轮执行异常: %s", e)
            next_ts = time.time() + args.interval_hours * 3600
            logging.info("下次执行: %s",
                         datetime.fromtimestamp(next_ts).strftime("%Y-%m-%d %H:%M:%S"))
            try:
                time.sleep(args.interval_hours * 3600)
            except KeyboardInterrupt:
                logging.info("手动退出")
                break
    else:
        n = run_once(args)
        logging.info("全部完成, 新增下载 %d 个", n)


if __name__ == "__main__":
    main()
