import sqlite3, json
DB = "mydb.sqlite"
cfg = [
    {
        "domain": "https://yvl3e.ibeeuzscf.cc/order/today/page/{page}//",
        "page_parse": r"archives/{\d+}/",
        "include_keyword": ["内射", "巨乳", "大奶", "无套", "乘骑", "美乳", "吊钟"],
        "exclude_keyword": ["日本"],
        "max_page": 4
    },
    {
        "domain": "https://hy2pz9.lgokurmfe.cc/category/wpcz/{page}/",
        "page_parse": r"archives/{\d+}/",
        "include_keyword": ["内射", "巨乳", "大奶", "无套", "乘骑", "美乳", "吊钟"],
        "exclude_keyword": ["日本"],
        "max_page": 4
    }
]
with sqlite3.connect(DB) as conn:
    # 新建config表 & 初始化
    conn.execute("""
        CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL)
    """)
    conn.execute(
        "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
        ("websites", json.dumps(cfg, ensure_ascii=False))
    )

    # 新建cdownloaded_posts表 & 初始化
    conn.execute("""
        CREATE TABLE IF NOT EXISTS downloaded_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT UNIQUE NOT NULL,
            title TEXT,
            video_file TEXT,
            viewed_at TEXT DEFAULT NULL,
            downloaded_at TEXT DEFAULT (datetime('now', 'localtime')),
            deleted INTEGER DEFAULT 0
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