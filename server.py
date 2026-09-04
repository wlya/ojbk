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
import json # <--- 新增导入
import sys
import subprocess
import threading

from flask import Flask, jsonify, request, send_from_directory, send_file, redirect, url_for

WORK_DIR = Path(__file__).resolve().parent
DB_PATH = WORK_DIR / "mydb.sqlite"
VIDEO_DIR = WORK_DIR / "videos"
VIEWER_HTML = WORK_DIR / "viewer.html"
VENDOR_DIR = WORK_DIR / "vendor"

DEFAULT_PORT = 8787

# 确保必要的目录存在
VIDEO_DIR.mkdir(parents=True, exist_ok=True)
VENDOR_DIR.mkdir(parents=True, exist_ok=True)

# ----------------------- 数据访问 -----------------------

# --- 新增: Config 数据库操作 ---
def get_config_websites():
    """从数据库读取 websites 配置"""
    if not DB_PATH.exists():
        return []
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT value FROM config WHERE key = ?", ("websites",)).fetchone()
        if row:
            try:
                return json.loads(row[0])
            except json.JSONDecodeError:
                return []
        return []

def save_config_websites(data):
    """保存 websites 配置到数据库"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
            ("websites", json.dumps(data, ensure_ascii=False, indent=4))
        )
# ---------------------------------


def list_videos():
    """读取数据库 + 磁盘状态, 按下载时间倒序返回视频列表"""
    if not DB_PATH.exists():
        return []
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT id, url, title, video_file, downloaded_at, viewed_at
            FROM downloaded_posts WHERE deleted = 0 ORDER BY downloaded_at DESC, id DESC
        """).fetchall()
    items = []
    for id, url, title, video_file, downloaded_at, viewed_at in rows:
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
        conn.execute("UPDATE downloaded_posts SET viewed_at = CURRENT_TIMESTAMP WHERE url = ?", (url,))
        row = conn.execute(
            "SELECT viewed_at FROM downloaded_posts WHERE url = ?", (url,)).fetchone()
    return row[0] if row else None


# ----------------------- Flask 应用 -----------------------

app = Flask(__name__, static_folder='html', static_url_path="")


@app.route("/")
@app.route("/index.html")
def index():
    return redirect('/list.html')  # 302 重定向


# 保存全局进程对象与线程锁
sync_process = None
sync_lock = threading.Lock()

@app.route('/api/sync/start', methods=['POST'])
def start_sync():
    global sync_process

    with sync_lock:
        # 检查子进程是否存在且依然在运行
        if sync_process is not None and sync_process.poll() is None:
            return jsonify({
                "status": "already_running",
                "message": "同步任务正在进行中，请勿重复发起",
                "pid": sync_process.pid
            }), 400

        # 使用 sys.executable 确保调用当前 Python 环境中的解释器
        # Popen 在后台独立发起进程，当前 API 瞬间完成响应
        sync_process = subprocess.Popen([sys.executable, "crawler.py"])

        return jsonify({
            "status": "started",
            "message": "同步任务已成功启动",
            "pid": sync_process.pid
        }), 200


@app.route('/api/sync/status', methods=['GET'])
def get_sync_status():
    global sync_process

    with sync_lock:
        if sync_process is None:
            return jsonify({
                "status": "idle",
                "running": False,
                "message": "尚未启动过同步任务"
            }), 200

        # poll() 返回 None 表示进程还在运行中
        # 返回整数（如 0）表示进程已退出及其退出状态码
        return_code = sync_process.poll()

        if return_code is None:
            return jsonify({
                "status": "running",
                "running": True,
                "message": "同步任务正在运行",
                "pid": sync_process.pid
            }), 200
        else:
            return jsonify({
                "status": "finished",
                "running": False,
                "message": "同步任务已结束退出",
                "return_code": return_code,
                "pid": sync_process.pid
            }), 200
        

# --- 新增: Config 页面路由 ---
@app.route("/api/config", methods=["GET"])
def api_get_config():
    """获取 websites 配置"""
    data = get_config_websites()
    return jsonify({"ok": True, "data": data})

@app.route("/api/config", methods=["POST"])
def api_save_config():
    """保存 websites 配置"""
    if not request.is_json:
        return jsonify({"ok": False, "error": "Request must be JSON"}), 400
    
    data = request.get_json()
    if not isinstance(data, list):
        return jsonify({"ok": False, "error": "Data must be a list"}), 400
        
    try:
        save_config_websites(data)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

def clear_viewed_videos():
    """清除所有标记为已看的本地视频文件"""
    if not DB_PATH.exists():
        return {"deleted_count": 0, "freed_bytes": 0}

    deleted_count = 0
    freed_bytes = 0

    with sqlite3.connect(DB_PATH) as conn:
        # 查出所有已观看记录对应的 video_file
        rows = conn.execute("""
            SELECT d.video_file, d.url
            FROM downloaded_posts d
            WHERE d.viewed_at IS NOT NULL AND d.video_file IS NOT NULL AND d.video_file != ''
        """).fetchall()

        for (video_file,url) in rows:
            if not video_file:
                continue
            name = video_file.replace("\\", "/").rsplit("/", 1)[-1]
            path = VIDEO_DIR / name
            conn.execute("UPDATE downloaded_posts SET deleted = 1 WHERE url = ?", (url,))
            
            # 确认文件存在且安全在 VIDEO_DIR 目录下
            if path.is_file() and path.resolve().parent == VIDEO_DIR.resolve():
                try:
                    file_size = path.stat().st_size
                    path.unlink()  # 删除物理文件
                    deleted_count += 1
                    freed_bytes += file_size
                except Exception as e:
                    print(f"[警告] 删除文件失败 {path}: {e}")

    return {"deleted_count": deleted_count, "freed_bytes": freed_bytes}
# ---------------------------------


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

# --- 新增: 一键清除已看视频 API ---
@app.route("/api/clear_viewed", methods=["POST","GET"])
def post_clear_viewed():
    """清除所有已看的视频本地文件"""
    try:
        result = clear_viewed_videos()
        return jsonify({
            "ok": True,
            "deleted_count": result["deleted_count"],
            "freed_bytes": result["freed_bytes"]
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/vendor/<path:filename>")
def serve_vendor(filename):
    return send_from_directory(VENDOR_DIR, filename)


def main():
    ap = argparse.ArgumentParser(description="51cg1 媒体库服务 (Flask)")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址(默认 0.0.0.0)")
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