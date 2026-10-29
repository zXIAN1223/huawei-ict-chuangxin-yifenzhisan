"""SQLite 连接、建表与事务，以及 UTC 时间与「本地日界」工具。

约定（plan 2.3.4）：
- 每请求独立连接：`sqlite3.connect(db_path, check_same_thread=False)`；
- 启用 `PRAGMA journal_mode=WAL` 与 `PRAGMA busy_timeout=5000`；
- 写操作使用事务包裹；
- 库路径由环境变量 `ALA_DB_PATH` 决定（经 `config.get_settings()`）。
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from backend.app.config import get_settings
from backend.app.models import DDL_CHAT_MESSAGE, DDL_INDEXES, SCHEMA_STATEMENTS

ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


# ---------------------------------------------------------------------------
# 时间工具
# ---------------------------------------------------------------------------


def utc_now() -> datetime:
    """当前 UTC 时间（带时区）。"""
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    """将 datetime 规范化为 UTC ISO-8601 字符串 `YYYY-MM-DDTHH:MM:SSZ`。

    无时区的输入按 UTC 解释。
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime(ISO_FORMAT)


def parse_iso(value: str) -> datetime:
    """解析 UTC ISO-8601 字符串为带 UTC 时区的 datetime。

    兼容结尾为 `Z` 或带偏移量的形式。
    """
    if not isinstance(value, str):
        raise ValueError(f"期望 ISO-8601 字符串，实际为 {type(value)!r}")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def local_day_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    """返回服务器**本地日界**的半开区间 `[start, end)`（均为 UTC）。

    以本地时区的当日 `00:00:00` 起，至次日 `00:00:00` 止（等价于当日
    `00:00:00` ~ `23:59:59.999`）。用于「今日到期」判定（plan 2.3.2）。
    """
    if now is None:
        now = datetime.now().astimezone()
    elif now.tzinfo is None:
        now = now.astimezone()

    local_start = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    local_end = local_start + timedelta(days=1)
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


def end_of_local_day(now: datetime | None = None) -> datetime:
    """返回服务器本地当日结束（次日 0 点，UTC）的时刻。"""
    return local_day_bounds(now)[1]


def is_due_by_end_of_local_day(due_at: str, now: datetime | None = None) -> bool:
    """判断 `due_at`（UTC ISO 字符串）是否在「今日」日界之前到期。"""
    try:
        due = parse_iso(due_at)
    except ValueError:
        return False
    return due < end_of_local_day(now)


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------


def _resolve_path(db_path: str | Path | None) -> Path:
    if db_path is None:
        return get_settings().db_path
    path = Path(db_path)
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """创建并配置一个 SQLite 连接。

    - `check_same_thread=False`：允许跨线程使用（FastAPI 线程池）；
    - `isolation_level=None`：交由显式事务控制（见 `transaction`）；
    - `journal_mode=WAL`、`busy_timeout=5000`、`foreign_keys=ON`。
    """
    path = _resolve_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_connection(db_path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    """连接上下文管理器：退出时确保关闭连接。"""
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式事务上下文：正常结束提交，异常回滚后重新抛出。

    要求连接为 `isolation_level=None`（由 `connect()` 保证）。
    """
    conn.execute("BEGIN")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:  # 回滚失败不掩盖原始异常
            pass
        raise


# ---------------------------------------------------------------------------
# 建表
# ---------------------------------------------------------------------------

#: `chat_message` 迁移时按此列顺序整表搬运（列名/顺序与 DDL 一致）。
_CHAT_MESSAGE_COLUMNS: tuple[str, ...] = (
    "id",
    "session_id",
    "user_id",
    "role",
    "content",
    "provider",
    "created_at",
)

#: 判断建表 SQL 是否已包含 `'llm'` 值域（新 DDL 的唯一特征）。
_CHAT_MESSAGE_PROVIDER_LLM_PATTERN = re.compile(r"['\"]llm['\"]")


def _migrate_chat_message_provider_domain(conn: sqlite3.Connection) -> None:
    """把旧库 `chat_message.provider` 的 CHECK 约束放宽到含 ``'llm'``（幂等）。

    背景（契约 C-LLM-PROVIDER@1 / R6.2）：旧库的 `provider` CHECK 仅允许
    ``'pangu'``/``'local'``，无法写入新值域中的 ``'llm'``。SQLite 不支持直接修改
    CHECK，故采用「改名 → 按新 DDL 建表 → 整列搬运 → 删除旧表」重建法。

    * 表不存在（新库已由新 DDL 建好）或建表 SQL 已含 ``'llm'`` → 直接跳过；
    * 调用方须保证：同一事务内、`SCHEMA_STATEMENTS` 之后、`DDL_INDEXES` 之前调用，
      以便旧表索引随 ``DROP`` 消失后由 `DDL_INDEXES` 统一重建；
    * 数据保留：原行（含 ``id``）完整搬运，`chat_message` 无指向它的外键与触发器。
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'chat_message'"
    ).fetchone()
    if row is None:
        return
    table_sql = str(row[0] or "")
    if _CHAT_MESSAGE_PROVIDER_LLM_PATTERN.search(table_sql):
        return

    # 正常路径不会残留 `chat_message__old`（迁移与重建同事务，失败整体回滚）；
    # 此处先清理异常残留，避免 RENAME 失败导致初始化彻底不可用。
    conn.execute("DROP TABLE IF EXISTS chat_message__old")
    conn.execute("ALTER TABLE chat_message RENAME TO chat_message__old")
    conn.execute(DDL_CHAT_MESSAGE)
    columns = ", ".join(_CHAT_MESSAGE_COLUMNS)
    conn.execute(
        f"INSERT INTO chat_message ({columns}) SELECT {columns} FROM chat_message__old"
    )
    conn.execute("DROP TABLE chat_message__old")


def init_db(db_path: str | Path | None = None) -> None:
    """初始化数据库：建立全部 7 张表（含索引），幂等。

    DDL 唯一来源为 `models.SCHEMA_STATEMENTS`；其余任务不得另建表。

    执行顺序（plan 2.3.3，不可颠倒）：
    建表 → `chat_message.provider` 值域幂等迁移 → 建索引（同一事务）。
    """
    conn = connect(db_path)
    try:
        conn.execute("BEGIN")
        for statement in SCHEMA_STATEMENTS:
            conn.execute(statement)
        # 旧库在此放宽 provider CHECK；必须在建索引之前，索引随后统一重建。
        _migrate_chat_message_provider_domain(conn)
        for statement in DDL_INDEXES:
            conn.execute(statement)
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def list_tables(conn: sqlite3.Connection) -> list[str]:
    """返回当前库中的用户表名（按名称排序）。"""
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [row[0] for row in rows]