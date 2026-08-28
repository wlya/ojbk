#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
51cg1 媒体库服务 (Flask 版)
==========================
- 读取 downloaded.db, 按下载时间倒序列出所有已下载视频
- 使用 Flask send_from_directory 托管视频与静态资源

用法:
    python server_flask.py
    python server_flask.py --port 9000
    python server_flask.py --host 0.0.0.0 --no-open
"""

import argparse
import sqlite3
import sys
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, send_file

WORK_DIR = Path(__file__).resolve().parent
DB_PATH = WORK_DIR / "downloaded.db"
VIDEO_DIR = WORK_DIR / "videos"
VIEWER_HTML = WORK_DIR / "viewer.html"
VENDOR_DIR = WORK_DIR / "vendor"

DEFAULT_PORT = 8787

# 确保必要的目录存在
VIDEO_DIR.mkdir(parents=True, exist_ok=True)
VENDOR_DIR.mkdir(parents=True, exist_ok=True)

# ----------------------- 数据访问 -----------------------

def _ensure_view_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS viewed_posts (
            url TEXT PRIMARY KEY,
            viewed_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)


def list_videos():
    """读取数据库 + 磁盘状态, 按下载时间倒序返回视频列表"""
    if not DB_PATH.exists():
        return []
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_view_table(conn)
        rows = conn.execute("""
            SELECT d.url, d.title, d.video_file, d.downloaded_at, v.viewed_at
            FROM downloaded_posts d
            LEFT JOIN viewed_posts v ON v.url = d.url
            ORDER BY d.downloaded_at DESC, d.id DESC
        """).fetchall()
    items = []
    for url, title, video_file, downloaded_at, viewed_at in rows:
        name = ""
        if video_file:
            name = video_file.replace("\\", "/").rsplit("/", 1)[-1]
        path = VIDEO_DIR / name if name else None
        exists = bool(path and path.is_file())
        size = path.stat().st_size if exists else 0
        items.append({
            "url": url,
            "title": title or name,
            "file": name,
            "size": size,
            "exists": exists,
            "downloaded_at": downloaded_at,
            "viewed": viewed_at is not None,
            "viewed_at": viewed_at,
        })
    return items


def mark_viewed(url: str):
    """标记已看, 返回 viewed_at; url 无效返回 None"""
    url = (url or "").strip()
    if not url:
        return None
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_view_table(conn)
        conn.execute("INSERT OR IGNORE INTO viewed_posts (url) VALUES (?)", (url,))
        row = conn.execute(
            "SELECT viewed_at FROM viewed_posts WHERE url = ?", (url,)).fetchone()
    return row[0] if row else None


# ----------------------- Flask 应用 -----------------------

app = Flask(__name__, static_folder='html', static_url_path="")


@app.route("/")
@app.route("/index.html")
def index():
    if VIEWER_HTML.is_file():
        return send_file(VIEWER_HTML)
    return "<h1>缺少 viewer.html</h1><p>请把它放在 server.py 同目录</p>", 500


@app.route("/api/videos", methods=["GET"])
def get_videos():
    return jsonify({"ok": True, "items": list_videos()})


@app.route("/api/mark_viewed", methods=["POST"])
def post_mark_viewed():
    data = request.get_json(silent=True) or {}
    url = data.get("url")
    viewed_at = mark_viewed(url)
    if viewed_at is None:
        return jsonify({"ok": False, "error": "url required"}), 400
    return jsonify({"ok": True, "viewed": True, "viewed_at": viewed_at})


@app.route("/videos/<path:filename>")
def serve_video(filename):
    # conditional=True 会启用 Range Header 断点续传支持
    return send_from_directory(VIDEO_DIR, filename, conditional=True)


@app.route("/vendor/<path:filename>")
def serve_vendor(filename):
    return send_from_directory(VENDOR_DIR, filename)


def main():
    ap = argparse.ArgumentParser(description="51cg1 媒体库服务 (Flask)")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址(默认 127.0.0.1)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    url = f"http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{args.port}/"
    print(f"媒体库服务 (Flask) 已启动: {url} (Ctrl+C 停止)")
    print(f"  数据库: {DB_PATH}")
    print(f"  视频目录: {VIDEO_DIR}")
    sys.stdout.flush()

    if not args.no_open:
        webbrowser.open(url)

    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()