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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL)
    """)
    conn.execute(
        "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
        ("websites", json.dumps(cfg, ensure_ascii=False))
    )