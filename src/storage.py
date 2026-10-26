"""SQLite数据存储：用户档案、复习记录、答题记录、测试结果、聊天历史"""
import sqlite3
import os
from datetime import datetime

_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "learning.db")


def _get_conn():
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _get_conn()
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS user_profile (
        id INTEGER PRIMARY KEY,
        name TEXT DEFAULT '我',
        learning_style TEXT DEFAULT '',
        daily_review_limit INTEGER DEFAULT 5,
        daily_minutes INTEGER DEFAULT 30,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS review_records (
        kp_id TEXT PRIMARY KEY,
        first_learned_at TEXT,
        last_reviewed_at TEXT,
        stage INTEGER DEFAULT 0,
        ef REAL DEFAULT 2.5,
        interval INTEGER DEFAULT 0,
        repetitions INTEGER DEFAULT 0,
        next_review_at TEXT,
        mastery REAL DEFAULT 0.0,
        review_count INTEGER DEFAULT 0,
        content_type TEXT DEFAULT 'conceptual'
    );
    CREATE TABLE IF NOT EXISTS recall_answers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kp_id TEXT,
        question TEXT,
        answer TEXT,
        score REAL,
        quality INTEGER,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS style_test (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        result_json TEXT,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS chat_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        role TEXT,
        content TEXT,
        mode TEXT,
        kp_id TEXT,
        created_at TEXT
    );
    """)
    if not c.execute("SELECT COUNT(*) FROM user_profile").fetchone()[0]:
        c.execute("INSERT INTO user_profile(id, created_at) VALUES(1, ?)",
                  (datetime.now().isoformat(),))
    conn.commit()
    conn.close()


def get_profile():
    conn = _get_conn()
    row = conn.execute("SELECT * FROM user_profile WHERE id=1").fetchone()
    conn.close()
    return dict(row) if row else None


def update_profile(name=None, learning_style=None, daily_review_limit=None, daily_minutes=None):
    p = get_profile() or {}
    conn = _get_conn()
    conn.execute("""
        INSERT OR REPLACE INTO user_profile
        (id, name, learning_style, daily_review_limit, daily_minutes, created_at)
        VALUES (1, ?, ?, ?, ?, ?)
    """, (
        name if name is not None else p.get("name", "我"),
        learning_style if learning_style is not None else p.get("learning_style", ""),
        daily_review_limit if daily_review_limit is not None else p.get("daily_review_limit", 5),
        daily_minutes if daily_minutes is not None else p.get("daily_minutes", 30),
        p.get("created_at", datetime.now().isoformat()),
    ))
    conn.commit()
    conn.close()


def get_all_review_records():
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM review_records").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_review_record(kp_id):
    conn = _get_conn()
    row = conn.execute("SELECT * FROM review_records WHERE kp_id=?", (kp_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def upsert_review_record(kp_id, first_learned_at, last_reviewed_at, stage, ef,
                         interval, repetitions, next_review_at, mastery,
                         review_count, content_type):
    conn = _get_conn()
    conn.execute("""
        INSERT OR REPLACE INTO review_records
        (kp_id, first_learned_at, last_reviewed_at, stage, ef, interval,
         repetitions, next_review_at, mastery, review_count, content_type)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (kp_id, first_learned_at, last_reviewed_at, stage, ef, interval,
          repetitions, next_review_at, mastery, review_count, content_type))
    conn.commit()
    conn.close()


def add_recall_answer(kp_id, question, answer, score, quality):
    conn = _get_conn()
    conn.execute("""
        INSERT INTO recall_answers (kp_id, question, answer, score, quality, created_at)
        VALUES (?,?,?,?,?,?)
    """, (kp_id, question, answer, score, quality, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def get_recall_answers(kp_id=None, limit=50):
    conn = _get_conn()
    if kp_id:
        rows = conn.execute(
            "SELECT * FROM recall_answers WHERE kp_id=? ORDER BY id DESC LIMIT ?",
            (kp_id, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM recall_answers ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_style_test(result_json):
    conn = _get_conn()
    conn.execute("INSERT INTO style_test (result_json, created_at) VALUES (?,?)",
                 (result_json, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def get_latest_style_test():
    conn = _get_conn()
    row = conn.execute("SELECT * FROM style_test ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    return dict(row) if row else None


def add_chat_message(role, content, mode="chat", kp_id=None):
    conn = _get_conn()
    conn.execute("""
        INSERT INTO chat_history (role, content, mode, kp_id, created_at)
        VALUES (?,?,?,?,?)
    """, (role, content, mode, kp_id, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def get_chat_history(mode=None, kp_id=None, limit=100):
    conn = _get_conn()
    sql = "SELECT * FROM chat_history WHERE 1=1"
    params = []
    if mode:
        sql += " AND mode=?"
        params.append(mode)
    if kp_id:
        sql += " AND kp_id=?"
        params.append(kp_id)
    sql += " ORDER BY id ASC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def clear_chat_history(mode=None):
    conn = _get_conn()
    if mode:
        conn.execute("DELETE FROM chat_history WHERE mode=?", (mode,))
    else:
        conn.execute("DELETE FROM chat_history")
    conn.commit()
    conn.close()