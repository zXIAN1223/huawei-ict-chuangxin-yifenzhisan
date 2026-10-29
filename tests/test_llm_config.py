"""``C-LLM-CONFIG@1`` 配置读取与幂等迁移测试（plan 2.2.1 / 2.2.3 / 2.3.3，T5）。

覆盖范围
--------
1. **三态优先级**：仅 ``LLM_*`` / 仅 ``PANGU_*`` / 两者均设置（取 ``LLM_*``），
   以及两者都不设置时的默认值；空白值视为缺失并回退；
2. **向后兼容填充**：``Settings.pangu_api_url/api_key/model/timeout`` 等于对应
   ``llm_*`` 有效值（R6.3，供 ``routers/quiz.py`` 零改动受益）；
3. **校验**：``llm_temperature`` clamp 至 ``[0.0, 2.0]``、``llm_max_tokens`` 非法或
   ``< 1`` 回退默认、``llm_timeout`` 非法回退默认、``llm_model`` 空串回退默认；
4. **旧库幂等迁移**（R6.2）：手工建立仅允许 ``('pangu','local')`` 的旧
   ``chat_message`` 表并写入历史行 → ``init_db()`` → 可写入 ``provider='llm'``、
   旧行（含 id）完整保留、索引重建、二次 ``init_db()`` 无副作用。

隔离约束（AC-R8-3）：所有用例只操作 ``tmp_path`` 下的临时库文件，**绝不触碰**
运行库 ``backend/app/data/app.db``（``runtime_db_path`` 夹具仅用于断言未被写入）。
配置读取通过 monkeypatch 清空进程环境变量并把 ``.env`` 指向不存在的文件，保证
结果与运行机环境无关。
"""

from __future__ import annotations

import sqlite3
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from backend.app import config as config_module
from backend.app.config import Settings, get_settings
from backend.app.db import init_db
from backend.app.models import TABLE_NAMES

#: 影响 LLM 配置读取的环境变量（``C-LLM-CONFIG@1` 优先级映射表）。
_CONFIG_ENV_KEYS = (
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "LLM_TIMEOUT",
    "LLM_TEMPERATURE",
    "LLM_MAX_TOKENS",
    "PANGU_API_URL",
    "PANGU_API_KEY",
    "PANGU_MODEL",
    "PANGU_TIMEOUT",
)


@pytest.fixture(autouse=True)
def _isolate_config_environment(monkeypatch, tmp_path):
    """清空外部配置来源：进程环境变量 + ``.env``（指向不存在文件）。"""
    monkeypatch.setattr(config_module, "ENV_FILE", tmp_path / "absent.env")
    for key in _CONFIG_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


# ---------------------------------------------------------------------------
# 1) 三态优先级
# ---------------------------------------------------------------------------


def test_settings_defaults_without_credentials(_isolate_config_environment) -> None:
    """两者都不设置 → 契约默认值，``llm_enabled`` 为 False。"""
    settings = get_settings()

    assert settings.llm_base_url is None
    assert settings.llm_api_key is None
    assert settings.llm_model == "pangu"
    assert settings.llm_timeout == 30.0
    assert settings.llm_temperature == 0.7
    assert settings.llm_max_tokens == 1024
    assert settings.llm_enabled is False
    assert settings.pangu_enabled is False


def test_settings_only_llm_keys(_isolate_config_environment) -> None:
    """仅设置 ``LLM_*`` → 全部取自 ``LLM_*``，且回填到 ``pangu_*``。"""
    env = _isolate_config_environment
    env.setenv("LLM_BASE_URL", "https://llm.example/v1")
    env.setenv("LLM_API_KEY", "llm-key")
    env.setenv("LLM_MODEL", "llm-model")
    env.setenv("LLM_TIMEOUT", "12")
    env.setenv("LLM_TEMPERATURE", "0.3")
    env.setenv("LLM_MAX_TOKENS", "256")

    settings = get_settings()

    assert settings.llm_base_url == "https://llm.example/v1"
    assert settings.llm_api_key == "llm-key"
    assert settings.llm_model == "llm-model"
    assert settings.llm_timeout == 12.0
    assert settings.llm_temperature == 0.3
    assert settings.llm_max_tokens == 256
    assert settings.llm_enabled is True


def test_settings_only_pangu_keys_fall_back(_isolate_config_environment) -> None:
    """仅设置 ``PANGU_*`` → ``llm_*`` 回退读取旧别名（兼容旧部署）。"""
    env = _isolate_config_environment
    env.setenv("PANGU_API_URL", "https://pangu.example/v1")
    env.setenv("PANGU_API_KEY", "pangu-key")
    env.setenv("PANGU_MODEL", "pangu-model")
    env.setenv("PANGU_TIMEOUT", "18.5")

    settings = get_settings()

    assert settings.llm_base_url == "https://pangu.example/v1"
    assert settings.llm_api_key == "pangu-key"
    assert settings.llm_model == "pangu-model"
    assert settings.llm_timeout == 18.5
    assert settings.llm_enabled is True


def test_settings_both_keys_prefer_llm(_isolate_config_environment) -> None:
    """两者均设置 → 取 ``LLM_*``（高优先级）。"""
    env = _isolate_config_environment
    env.setenv("LLM_BASE_URL", "https://llm.example/v1")
    env.setenv("LLM_API_KEY", "llm-key")
    env.setenv("LLM_MODEL", "llm-model")
    env.setenv("LLM_TIMEOUT", "5")
    env.setenv("PANGU_API_URL", "https://pangu.example/v1")
    env.setenv("PANGU_API_KEY", "pangu-key")
    env.setenv("PANGU_MODEL", "pangu-model")
    env.setenv("PANGU_TIMEOUT", "99")

    settings = get_settings()

    assert settings.llm_base_url == "https://llm.example/v1"
    assert settings.llm_api_key == "llm-key"
    assert settings.llm_model == "llm-model"
    assert settings.llm_timeout == 5.0
    # 兼容填充同样以 LLM 有效值为准
    assert settings.pangu_api_url == "https://llm.example/v1"
    assert settings.pangu_api_key == "llm-key"
    assert settings.pangu_model == "llm-model"
    assert settings.pangu_timeout == 5.0


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_blank_llm_value_falls_back_to_pangu(
    _isolate_config_environment, blank: str
) -> None:
    """空白 ``LLM_*`` 视为缺失 → 回退 ``PANGU_*``（若也无则默认）。"""
    env = _isolate_config_environment
    env.setenv("LLM_BASE_URL", blank)
    env.setenv("LLM_API_KEY", blank)
    env.setenv("LLM_MODEL", blank)
    env.setenv("PANGU_API_URL", "https://pangu.example/v1")
    env.setenv("PANGU_API_KEY", "pangu-key")

    settings = get_settings()

    assert settings.llm_base_url == "https://pangu.example/v1"
    assert settings.llm_api_key == "pangu-key"
    assert settings.llm_model == "pangu"  # 别名也为空白 → 默认


# ---------------------------------------------------------------------------
# 2) 向后兼容填充（pangu_* == llm_*）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env_setup",
    [
        pytest.param({"LLM_BASE_URL": "https://llm/v1", "LLM_API_KEY": "k"}, id="llm"),
        pytest.param({"PANGU_API_URL": "https://pangu/v1", "PANGU_API_KEY": "k"}, id="pangu"),
        pytest.param(
            {
                "LLM_BASE_URL": "https://llm/v1",
                "LLM_API_KEY": "k",
                "PANGU_API_URL": "https://pangu/v1",
                "PANGU_API_KEY": "k2",
            },
            id="both",
        ),
    ],
)
def test_pangu_fields_mirror_llm_effective_values(
    _isolate_config_environment, env_setup: dict[str, str]
) -> None:
    """``pangu_*`` 必须等于 ``llm_*`` 有效值（R6.3）。"""
    env = _isolate_config_environment
    for key, value in env_setup.items():
        env.setenv(key, value)

    settings = get_settings()

    assert settings.pangu_api_url == settings.llm_base_url
    assert settings.pangu_api_key == settings.llm_api_key
    assert settings.pangu_model == settings.llm_model
    assert settings.pangu_timeout == settings.llm_timeout
    assert settings.pangu_enabled == settings.llm_enabled


# ---------------------------------------------------------------------------
# 3) 校验与 clamp
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("5", 2.0, id="clamp-high"),
        pytest.param("2.5", 2.0, id="clamp-high-2.5"),
        pytest.param("-1", 0.0, id="clamp-low"),
        pytest.param("0", 0.0, id="zero-kept"),
        pytest.param("1.5", 1.5, id="in-range"),
        pytest.param("abc", 0.7, id="invalid-default"),
        pytest.param("nan", 0.7, id="nan-default"),
        pytest.param("inf", 0.7, id="inf-default"),
    ],
)
def test_llm_temperature_parsing(
    _isolate_config_environment, raw: str, expected: float
) -> None:
    """``LLM_TEMPERATURE`` clamp 至 [0,2]，非法/非有限值回退默认 0.7。"""
    _isolate_config_environment.setenv("LLM_TEMPERATURE", raw)
    assert get_settings().llm_temperature == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("512", 512, id="valid"),
        pytest.param("1", 1, id="minimum-valid"),
        pytest.param("0", 1024, id="zero-default"),
        pytest.param("-3", 1024, id="negative-default"),
        pytest.param("abc", 1024, id="invalid-default"),
        pytest.param(" 256 ", 256, id="whitespace-trimmed"),
    ],
)
def test_llm_max_tokens_parsing(
    _isolate_config_environment, raw: str, expected: int
) -> None:
    """``LLM_MAX_TOKENS`` 非法或 < 1 时回退默认 1024。"""
    _isolate_config_environment.setenv("LLM_MAX_TOKENS", raw)
    assert get_settings().llm_max_tokens == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("12.5", 12.5, id="valid"),
        pytest.param("abc", 30.0, id="invalid-default"),
        pytest.param("nan", 30.0, id="nan-default"),
    ],
)
def test_llm_timeout_parsing(
    _isolate_config_environment, raw: str, expected: float
) -> None:
    """``LLM_TIMEOUT`` 非法数值回退默认 30.0。"""
    _isolate_config_environment.setenv("LLM_TIMEOUT", raw)
    assert get_settings().llm_timeout == expected


def test_settings_is_frozen_snapshot() -> None:
    """``Settings`` 为 frozen dataclass（配置快照不可变）。"""
    settings = Settings(
        db_path=Path("a.db"),
        data_dir=Path("data"),
        frontend_dir=Path("frontend"),
        pangu_api_url=None,
        pangu_api_key=None,
        pangu_model="pangu",
        pangu_timeout=30.0,
    )
    with pytest.raises(FrozenInstanceError):
        settings.llm_model = "changed"  # type: ignore[misc]


def test_db_path_env_is_still_honoured(_isolate_config_environment, tmp_path) -> None:
    """``ALA_DB_PATH`` 仍生效（测试隔离依赖它切换到临时库）。"""
    db_file = tmp_path / "custom.db"
    _isolate_config_environment.setenv("ALA_DB_PATH", str(db_file))
    assert get_settings().db_path == db_file.resolve()


# ---------------------------------------------------------------------------
# 4) 旧 CHECK 表 → init_db 幂等迁移（R6.2）
# ---------------------------------------------------------------------------

#: 旧版 `chat_message` 建表语句（provider CHECK 仅允许 pangu/local）。
_LEGACY_CHAT_MESSAGE_DDL = """
CREATE TABLE chat_message (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content    TEXT NOT NULL,
    provider   TEXT CHECK (provider IS NULL OR provider IN ('pangu', 'local')),
    created_at TEXT NOT NULL
);
"""

_LEGACY_ROWS: tuple[tuple[Any, ...], ...] = (
    ("sess-legacy", "user-legacy", "assistant", "历史回答 A", "pangu", "2026-01-01T00:00:00Z"),
    ("sess-legacy", "user-legacy", "user", "历史提问 B", None, "2026-01-01T00:00:01Z"),
)

_CHAT_MESSAGE_COLUMNS = ("id", "session_id", "user_id", "role", "content", "provider", "created_at")


def _query(db_file: Path, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    """以独立连接执行查询并返回全部行（查询后立即关闭连接）。"""
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _create_legacy_database(db_file: Path) -> None:
    """构造旧库：仅 `chat_message` 为旧 CHECK 结构，并预置历史行。"""
    conn = sqlite3.connect(str(db_file))
    try:
        conn.executescript(_LEGACY_CHAT_MESSAGE_DDL)
        conn.executemany(
            "INSERT INTO chat_message "
            "(session_id, user_id, role, content, provider, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            _LEGACY_ROWS,
        )
        conn.commit()
    finally:
        conn.close()


def test_legacy_check_rejects_llm_before_migration(tmp_path) -> None:
    """前置事实：旧 CHECK 表**拒绝**写入 ``provider='llm'``（迁移的必要性证据）。"""
    db_file = tmp_path / "legacy_reject.db"
    _create_legacy_database(db_file)

    conn = sqlite3.connect(str(db_file))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO chat_message "
                "(session_id, user_id, role, content, provider, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("sess-x", "u", "assistant", "新回答", "llm", "2026-01-02T00:00:00Z"),
            )
        conn.rollback()
    finally:
        conn.close()


def test_init_db_migrates_provider_domain_and_keeps_legacy_rows(
    tmp_path, runtime_db_path
) -> None:
    """旧 CHECK 表经 ``init_db()`` 放宽，历史行完整保留且可写 ``llm``。"""
    db_file = tmp_path / "app.db"
    _create_legacy_database(db_file)

    init_db(db_file)

    # (1) 建表 SQL 已含 'llm'
    sql_rows = _query(
        db_file,
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'chat_message'",
    )
    assert len(sql_rows) == 1
    table_sql = str(sql_rows[0]["sql"])
    assert "'llm'" in table_sql
    assert "'pangu'" in table_sql and "'local'" in table_sql

    # (2) 旧行（含 id 与 provider）完整保留
    rows = _query(
        db_file,
        "SELECT id, session_id, user_id, role, content, provider, created_at "
        "FROM chat_message ORDER BY id",
    )
    assert [tuple(row[column] for column in _CHAT_MESSAGE_COLUMNS) for row in rows] == [
        (1, *_LEGACY_ROWS[0]),
        (2, *_LEGACY_ROWS[1]),
    ]

    # (3) 迁移后可写入新值域 'llm'
    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute(
            "INSERT INTO chat_message "
            "(session_id, user_id, role, content, provider, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("sess-legacy", "user-legacy", "assistant", "新回答 C", "llm", "2026-01-02T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()
    assert [row["provider"] for row in _query(db_file, "SELECT provider FROM chat_message ORDER BY id")] == [
        "pangu",
        None,
        "llm",
    ]

    # (4) 索引在迁移之后重建；旧表已清理；其余 6 张表齐备
    index_names = {
        row["name"]
        for row in _query(
            db_file,
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'chat_message'",
        )
    }
    assert "idx_chat_message_session" in index_names
    tables = {row["name"] for row in _query(db_file, "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "chat_message__old" not in tables
    assert set(TABLE_NAMES) <= tables

    # (5) 绝不触碰运行库（AC-R8-3）
    assert runtime_db_path != db_file


def test_init_db_is_idempotent_on_migrated_database(tmp_path) -> None:
    """已迁移库再次 ``init_db()`` 无副作用（幂等）。"""
    db_file = tmp_path / "app.db"
    _create_legacy_database(db_file)
    init_db(db_file)

    before = _query(db_file, "SELECT COUNT(*) AS total FROM chat_message")[0]["total"]
    init_db(db_file)  # 第二次
    init_db(db_file)  # 第三次（新库路径也应无副作用）

    after = _query(db_file, "SELECT COUNT(*) AS total FROM chat_message")[0]["total"]
    assert after == before == len(_LEGACY_ROWS)

    sql_rows = _query(
        db_file,
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'chat_message'",
    )
    assert "'llm'" in str(sql_rows[0]["sql"])
    index_names = {
        row["name"]
        for row in _query(
            db_file,
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'chat_message'",
        )
    }
    assert "idx_chat_message_session" in index_names


def test_init_db_on_fresh_database_allows_llm_without_migration(tmp_path) -> None:
    """全新库直接由新 DDL 建立，可写入 ``llm``；``_migrate_*`` 跳过（无副作用）。"""
    db_file = tmp_path / "fresh.db"
    init_db(db_file)

    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute(
            "INSERT INTO chat_message "
            "(session_id, user_id, role, content, provider, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("sess-new", "u", "assistant", "新库回答", "llm", "2026-01-03T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()

    assert _query(db_file, "SELECT provider FROM chat_message")[0]["provider"] == "llm"
    tables = {row["name"] for row in _query(db_file, "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "chat_message__old" not in tables

# ---------------------------------------------------------------------------
# 5) .env.example 配置示例（C-ENV-LLM@1，静态读取断言）
# ---------------------------------------------------------------------------

#: `.env.example` 必须文档化的键（plan 2.2.4）。
_REQUIRED_ENV_EXAMPLE_KEYS = (
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "LLM_TIMEOUT",
    "LLM_TEMPERATURE",
    "LLM_MAX_TOKENS",
    "PANGU_API_URL",
    "PANGU_API_KEY",
    "PANGU_MODEL",
    "PANGU_TIMEOUT",
    "ASR_ENABLED",
    "ASR_MODEL_DIR",
    "ASR_LANG",
    "ASR_TIMEOUT",
    "ASR_MAX_DURATION_MS",
    "ASR_MAX_BYTES",
)

#: 密钥行允许的最大占位长度（超过即视为疑似真实密钥）。
_MAX_PLACEHOLDER_SECRET_LENGTH = 8


def test_env_example_documents_llm_and_asr_keys() -> None:
    """``.env.example`` 含 ``LLM_*`` / ``PANGU_*`` / ``ASR_*`` 段且注明优先级。"""
    env_example_path = config_module.REPO_ROOT / ".env.example"
    assert env_example_path.is_file(), ".env.example 缺失（C-ENV-LLM@1）"
    text = env_example_path.read_text(encoding="utf-8")

    for key in _REQUIRED_ENV_EXAMPLE_KEYS:
        assert any(
            line.strip().startswith(f"{key}=") for line in text.splitlines()
        ), f".env.example 缺少配置项 {key}"

    # 优先级说明必须存在（LLM_* 优先于 PANGU_*）
    assert "LLM_*" in text and "PANGU_*" in text
    assert "优先" in text


def test_env_example_contains_no_real_secrets() -> None:
    """``.env.example`` 中的 ``*_API_KEY`` 行不得填入真实密钥（R5.2/R5.4）。"""
    env_example_path = config_module.REPO_ROOT / ".env.example"
    text = env_example_path.read_text(encoding="utf-8")

    checked = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if not key.strip().endswith("API_KEY"):
            continue
        checked += 1
        assert len(value.strip()) <= _MAX_PLACEHOLDER_SECRET_LENGTH, (
            f".env.example 的 {key.strip()} 疑似填入真实密钥"
        )

    assert checked >= 2  # 至少涵盖 LLM_API_KEY 与 PANGU_API_KEY