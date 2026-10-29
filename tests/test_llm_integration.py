"""T6 · LLM/ASR 升级的独立集成验证（``chat-llm-voice``，plan 2.2.x / 3.3）。

本文件由**独立验证者**编写，不复用实现者同思路的断言，而是从「升级后是否真的
可用、是否真的能降级」的角度做对抗性验证：

1. ``test_chat_stream_without_credentials_*`` —— 无凭证时必须回退 ``local``：
   帧序 ``meta → delta* → done``、``meta.provider == done.provider == "local"``、
   落库 assistant 消息 ``provider == "local"``（独立临时库，直接查 SQLite 交叉验证）；
2. ``test_chat_stream_unreachable_endpoint_*`` —— **配置有效但端点不可达**：
   ``provider`` 判定为 ``llm``（证明配置链生效），网络层抛真实
   ``httpx.ConnectError`` 后必须整段回退 ``local``，且**不得**出现 ``error`` 终帧；
3. ``test_chat_message_provider_migration_*`` —— 旧 CHECK（仅 ``'pangu','local'``）
   旧库经 ``init_db()`` 后可写 ``'llm'``、旧行完整保留、二次调用幂等、非法值仍被拒；
4. 前端：``chat.html`` 六个语音元素 id、脚本顺序 ``api.js → asr.js → chat.js``、
   无 CDN/远程资源、ES5 风格；五个页面/脚本文件存在；
5. ``POST /api/asr``：缺 ``X-User-Id`` → 400 ``MISSING_USER_ID``；无 vosk/模型 →
   503 ``ASR_UNAVAILABLE``；坏音频 → 400 ``ASR_BAD_AUDIO``；超限 → 413；正常 200 形状
   （本机未安装 vosk，200 形状以替身引擎验证并标注 LIMITED）；
6. ``scripts/check_llm.py`` 退出码：未配置 → 2、不可达 → 1（子进程 ``timeout`` 保护）；
7. 密钥零泄露：SSE 文本、落库全文、日志、``AIConfig`` repr 中均不得出现探针密钥。

环境约束（与 ``tests/conftest.py`` 一致）：pytest 进程被网络沙箱限制，无法创建
asyncio 事件循环，故 HTTP 层使用 ``DirectClient``（进程内直调路由）；SSE 与协程由
``send(None)`` 手动驱动。**任何可能阻塞的外部探测一律放在带 ``timeout`` 的子进程
里执行**，超时即 ``pytest.skip``（LIMITED），绝不挂起测试进程。
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import sqlite3
import subprocess
import sys
import types
import uuid
import wave
from pathlib import Path
from typing import Any, Optional

import pytest

from backend.app import config as config_module
from backend.app.routers.asr import post_asr
from backend.app.services.asr import ASR_PROVIDER, AsrEngine, AsrTranscription, build_asr_engine
from backend.app.services import ai_adapter as ai_adapter_module

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = REPO_ROOT / "frontend"
CHAT_HTML = FRONTEND_DIR / "chat.html"

CELL_KP = "cell-membrane"

#: 无凭证隔离时需清空的配置键（含 ``PANGU_*`` 兼容别名）。
_LLM_ENV_KEYS = (
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

#: 语音相关 DOM id（前端契约，plan 2.2.11）。
_VOICE_IDS = (
    "asr-record-btn",
    "asr-status",
    "voice-input-btn",
    "voice-status",
    "speak-toggle",
    "speak-stop-btn",
)

#: 期望存在的 5 个前端页面/脚本文件。
_EXPECTED_FRONTEND_FILES = (
    "index.html",
    "knowledge.html",
    "chat.html",
    "profile.html",
    "js/asr.js",
)


# ---------------------------------------------------------------------------
# 辅助：配置隔离 / SSE 解析 / 网络不可达替身 / WAV 与 ASR 调用
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_llm_env(monkeypatch, tmp_path):
    """隔离 LLM 配置来源：清空进程环境变量并指向不存在的 ``.env``。

    仅删除环境变量是不够的——``config.get_settings()`` 会回退读取仓库根 ``.env``
    （本仓库该文件含真实 ``LLM_API_KEY``）。因此同时把 ``config.ENV_FILE`` 指向
    临时目录下的不存在文件，确保「无凭证」是**真实的无凭证**。
    """
    monkeypatch.setattr(config_module, "ENV_FILE", tmp_path / "absent.env")
    for key in _LLM_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def parse_sse_frames(raw_text: str) -> list[tuple[str, dict[str, Any]]]:
    """把 SSE 原文解析为 ``[(event, data_json), ...]``。"""
    frames: list[tuple[str, dict[str, Any]]] = []
    event_name: Optional[str] = None
    data_buffer: list[str] = []

    def _flush() -> None:
        nonlocal event_name, data_buffer
        if event_name is None:
            return
        payload_text = "\n".join(data_buffer).strip()
        try:
            payload = json.loads(payload_text) if payload_text else {}
        except json.JSONDecodeError:
            payload = {"__raw__": payload_text}
        frames.append((event_name, payload))
        event_name, data_buffer = None, []

    for line in raw_text.splitlines():
        if not line.strip():
            _flush()
            continue
        if line.startswith("event:"):
            event_name = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_buffer.append(line[len("data:") :].strip())
    _flush()
    return frames


def install_connect_refused(monkeypatch) -> dict[str, int]:
    """把适配层的 ``httpx`` 换为「连接必定被拒」的替身（含真实异常类型）。

    注入的只是**网络传输层**：``AIAdapter.stream`` 的 provider 判定、payload 构建、
    异常捕获、回退编排与落库全部为真实代码路径。

    :returns: 调用计数器；测试用它证明「确实走到了远程尝试」而非 provider 一开始
        就是 ``local``（否则断言会退化为空壳）。
    """
    import httpx as real_httpx

    counters = {"stream_calls": 0, "post_calls": 0}

    class _RefusedAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.timeout = kwargs.get("timeout")

        async def __aenter__(self) -> "_RefusedAsyncClient":
            return self

        async def __aexit__(self, *exc_info: Any) -> bool:
            return False

        def stream(self, *args: Any, **kwargs: Any):
            counters["stream_calls"] += 1
            raise real_httpx.ConnectError("[WinError 10061] 目标计算机积极拒绝连接")

        async def post(self, *args: Any, **kwargs: Any):
            counters["post_calls"] += 1
            raise real_httpx.ConnectError("[WinError 10061] 目标计算机积极拒绝连接")

    monkeypatch.setattr(
        ai_adapter_module,
        "httpx",
        types.SimpleNamespace(AsyncClient=_RefusedAsyncClient, ConnectError=real_httpx.ConnectError),
    )
    return counters


def run_subprocess(code: str, *, env_overrides: dict[str, str], timeout: float = 60.0):
    """在独立子进程中执行一段 Python 代码（带超时，绝不阻塞测试进程）。

    :returns: ``subprocess.CompletedProcess``；超时时返回 ``None``（调用方标 LIMITED）。
    """
    import os

    env = dict(os.environ)
    env.update(env_overrides)
    env["PYTHONUNBUFFERED"] = "1"
    try:
        return subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None


def make_wav(
    *,
    duration_ms: int = 500,
    sample_rate: int = 16000,
    channels: int = 1,
    sample_width: int = 2,
) -> bytes:
    """构造指定规格的静音 WAV（默认 PCM16 / 16kHz / 单声道）。"""
    frame_count = max(1, int(sample_rate * duration_ms / 1000))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(sample_width)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"\x00" * (frame_count * channels * sample_width))
    return buffer.getvalue()


class FakeUploadFile:
    """``UploadFile`` 替身：仅实现端点使用的异步 ``read()``。"""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._offset = 0

    async def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunk = self._payload[self._offset :]
        else:
            chunk = self._payload[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


class StubAsrEngine:
    """替身引擎：不加载 vosk，返回固定识别结果（仅用于 200 响应形状断言）。"""

    def __init__(
        self,
        *,
        text: str = "细胞膜是选择透过性膜",
        max_bytes: int = 10 * 1024 * 1024,
        timeout: float = 5.0,
        lang: str = "zh-CN",
    ) -> None:
        self.text = text
        self.max_bytes = max_bytes
        self.timeout = timeout
        self.lang = lang
        self.calls: list[Optional[str]] = []

    def transcribe(self, audio: bytes, *, lang: Optional[str] = None) -> AsrTranscription:
        self.calls.append(lang)
        return AsrTranscription(
            text=self.text, lang=lang or self.lang, duration_ms=1200, model="stub-model"
        )


@pytest.fixture()
def sync_asr_asyncio(monkeypatch):
    """把 ``routers.asr`` 的 ``asyncio`` 换成同步替身（沙箱无法创建事件循环）。"""
    from backend.app import routers as routers_package

    async def _to_thread(func, /, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    async def _wait_for(awaitable: Any, timeout: Optional[float] = None) -> Any:
        return await awaitable

    monkeypatch.setattr(
        routers_package.asr,
        "asyncio",
        types.SimpleNamespace(
            to_thread=_to_thread, wait_for=_wait_for, TimeoutError=asyncio.TimeoutError
        ),
    )


def call_post_asr(
    *,
    engine: Any,
    payload: Optional[bytes] = None,
    lang: Optional[str] = None,
    user_id: str = "test-user-0001",
) -> tuple[int, dict[str, Any]]:
    """在无事件循环环境下驱动 ``post_asr`` 端点协程，返回 ``(status, body)``。

    依赖（``user_id`` / ``engine``）以显式入参注入，等价于 FastAPI 的 ``Depends``
    解析结果；``File``/``Form`` 字段亦按解析结果注入，从而绕开 ``DirectClient``
    不支持 multipart 的限制（HTTP 层之外的校验、错误映射与装配仍为真实代码）。
    """
    upload = FakeUploadFile(payload) if payload is not None else None
    coroutine = post_asr(audio=upload, lang=lang, user_id=user_id, engine=engine)
    try:
        try:
            coroutine.send(None)
        except StopIteration as stop:
            result = stop.value
        else:  # pragma: no cover - 同步替身生效时不会走到这里
            raise RuntimeError("端点协程未在无事件循环下完成")
    except Exception as exc:  # noqa: BLE001 - 按 main.py 的统一错误体还原
        from fastapi import HTTPException

        from backend.app.deps import ApiError

        if isinstance(exc, ApiError):
            body: dict[str, Any] = {"code": exc.code, "message": exc.message}
            if exc.detail is not None:
                body["detail"] = exc.detail
            return exc.status_code, body
        if isinstance(exc, HTTPException) and isinstance(exc.detail, dict):
            return exc.status_code, dict(exc.detail)
        raise
    return 200, dict(result)


def dump_database_text(db_path: Path) -> str:
    """把临时库全部表的全部行转成文本，用于密钥泄露全量检索。"""
    conn = sqlite3.connect(str(db_path))
    try:
        chunks: list[str] = []
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for table in tables:
            try:
                rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
            except sqlite3.Error:  # pragma: no cover - 表结构异常时跳过
                continue
            chunks.append(f"[{table}] {len(rows)} rows")
            for row in rows:
                chunks.append(repr(tuple(row)))
        return "\n".join(chunks)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1) 无凭证：SSE 帧序 + provider=local + 落库一致
# ---------------------------------------------------------------------------


def test_chat_stream_without_credentials_falls_back_to_local(
    client, auth_headers, temp_db_path, isolated_llm_env
) -> None:
    """无凭证 → 帧序 ``meta→delta*→done``，meta/done/落库 provider 均为 local。"""
    # 前提核实：确实是「无凭证」环境（而非恰好 .env 无值）
    assert config_module.get_settings().llm_enabled is False, "无凭证前提不成立"

    created = client.post("/api/chat/sessions", json={"mode": "teacher"}, headers=auth_headers)
    assert created.status_code == 201, created.text
    session_id = created.json()["session_id"]

    response = client.post(
        "/api/chat/stream",
        json={"session_id": session_id, "content": "请讲讲细胞膜的结构", "mode": "teacher"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")

    frames = parse_sse_frames(response.text)
    events = [name for name, _ in frames]
    assert events, "SSE 未产生任何帧"
    assert events[0] == "meta", f"首帧应为 meta，实际 {events[0]}"
    assert events[-1] == "done", f"末帧应为 done，实际 {events[-1]}"
    assert "error" not in events, f"无凭证路径不应出现 error 帧：{events}"
    assert all(name in ("meta", "delta", "done") for name in events), events

    meta = frames[0][1]
    done = frames[-1][1]
    deltas = [payload["text"] for name, payload in frames if name == "delta"]
    assert len(deltas) >= 1, "本地模拟应产生至少一个 delta"
    assert meta["provider"] == "local", f"meta.provider={meta['provider']}"
    assert done["provider"] == "local", f"done.provider={done['provider']}"
    assert done["content"] == "".join(deltas), "done.content 与 delta 拼接不一致"
    assert done["content"].strip(), "done.content 为空"

    # 落库交叉验证：API 与 SQLite 直查双证据
    messages = client.get(f"/api/chat/sessions/{session_id}/messages", headers=auth_headers)
    assert messages.status_code == 200
    assistant = next(m for m in messages.json() if m["role"] == "assistant")
    assert assistant["provider"] == "local"
    assert assistant["content"] == done["content"]

    conn = sqlite3.connect(str(temp_db_path))
    try:
        stored = conn.execute(
            "SELECT provider, content FROM chat_message WHERE role = 'assistant'"
        ).fetchall()
    finally:
        conn.close()
    assert stored, "assistant 消息未落库"
    assert [row[0] for row in stored] == ["local"], f"落库 provider 异常：{stored}"
    assert stored[0][1] == done["content"]


# ---------------------------------------------------------------------------
# 2) 配置有效但端点不可达 → 回退 local，无 error 终止
# ---------------------------------------------------------------------------


def test_chat_stream_unreachable_endpoint_falls_back_to_local(
    client, auth_headers, temp_db_path, monkeypatch, isolated_llm_env, caplog
) -> None:
    """配置有效（provider=llm）但连接被拒 → 整段回退 local，仍以 done 正常结束。"""
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("LLM_API_KEY", "sk-integration-probe-unreachable")
    monkeypatch.setenv("LLM_TIMEOUT", "2")

    settings = config_module.get_settings()
    assert settings.llm_enabled is True, "配置有效前提不成立"
    assert ai_adapter_module.build_ai_config(settings).resolve_endpoint() == (
        "http://127.0.0.1:1/chat/completions"
    ), "endpoint 归一化不符合 plan 2.2.2"

    install_connect_refused_counters = install_connect_refused(monkeypatch)

    created = client.post("/api/chat/sessions", json={"mode": "teacher"}, headers=auth_headers)
    session_id = created.json()["session_id"]
    with caplog.at_level("WARNING"):
        response = client.post(
            "/api/chat/stream",
            json={"session_id": session_id, "content": "端点不可达时也要有回答", "mode": "teacher"},
            headers=auth_headers,
        )
    assert response.status_code == 200, response.text

    # 证据：确实走到了远程尝试（否则本用例会退化为「provider 本来就是 local」的空壳）
    assert install_connect_refused_counters["stream_calls"] == 1, (
        f"远程流式尝试未被触发：{install_connect_refused_counters}"
    )
    assert any("远程 LLM stream 调用失败" in record.getMessage() for record in caplog.records), (
        "回退未经过异常捕获路径（日志缺失）"
    )

    frames = parse_sse_frames(response.text)
    events = [name for name, _ in frames]
    assert "error" not in events, f"不可达不应以 error 帧终止：{events}"
    assert events[-1] == "done"
    assert frames[0][1]["provider"] == "llm", "meta 应反映本次**尝试**的提供方 llm"
    done = frames[-1][1]
    assert done["provider"] == "local", f"回退后 done.provider 应为 local，实际 {done['provider']}"
    assert done["content"].strip(), "回退后正文不得为空"

    conn = sqlite3.connect(str(temp_db_path))
    try:
        providers = [
            row[0]
            for row in conn.execute(
                "SELECT provider FROM chat_message WHERE role = 'assistant'"
            ).fetchall()
        ]
    finally:
        conn.close()
    assert providers == ["local"], f"落库 provider 应记录实际提供方 local，实际 {providers}"


def test_unreachable_baseline_really_refuses_connection() -> None:
    """[LIMITED 保护] 真实网络栈下 ``127.0.0.1:1`` 确实不可达（子进程 + 超时）。"""
    code = (
        "import httpx\n"
        "try:\n"
        "    httpx.Client(timeout=2.0).post('http://127.0.0.1:1/chat/completions', json={})\n"
        "except Exception as exc:\n"
        "    print('EXC=' + type(exc).__name__)\n"
        "else:\n"
        "    print('EXC=NONE')\n"
    )
    proc = run_subprocess(code, env_overrides={}, timeout=30.0)
    if proc is None:
        pytest.skip("[LIMITED] 真实回环探测超时（沙箱限制），改用注入的 ConnectError 作为等价证据")
    if proc.returncode != 0:
        pytest.skip(f"[LIMITED] 真实回环探测不可执行（沙箱限制）：{proc.stderr.strip()[-200:]}")
    assert "EXC=" in proc.stdout
    assert "EXC=NONE" not in proc.stdout, "127.0.0.1:1 竟然可连，不可达前提不成立"
    assert any(
        name in proc.stdout for name in ("ConnectError", "ConnectTimeout", "TimeoutException")
    ), proc.stdout.strip()


# ---------------------------------------------------------------------------
# 3) 旧库 provider CHECK 迁移
# ---------------------------------------------------------------------------

#: 升级前（旧库）的 ``chat_message`` DDL：CHECK 仅允许 'pangu','local'。
_OLD_CHAT_MESSAGE_DDL = """
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


def test_chat_message_provider_migration_is_idempotent(tmp_path) -> None:
    """旧 CHECK 库 → ``init_db()`` → 可写 'llm'、旧行保留、二次幂等、非法值仍被拒。"""
    from backend.app.db import init_db

    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_OLD_CHAT_MESSAGE_DDL)
        conn.execute(
            "INSERT INTO chat_message (session_id, user_id, role, content, provider, created_at) "
            "VALUES ('s-legacy', 'u-legacy', 'assistant', '旧版本回复', 'pangu', '2026-01-01T00:00:00Z')"
        )
        conn.commit()
        # 前提核实：旧 CHECK 确实拒绝 'llm'（否则本测试毫无意义）
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO chat_message (session_id, user_id, role, content, provider, created_at) "
                "VALUES ('s-legacy', 'u-legacy', 'assistant', '不应写入', 'llm', '2026-01-01T00:00:01Z')"
            )
        conn.rollback()
    finally:
        conn.close()

    init_db(db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'chat_message'"
        ).fetchone()[0]
        assert "'llm'" in table_sql, f"迁移后 CHECK 未包含 llm：{table_sql}"

        legacy_rows = conn.execute(
            "SELECT id, content, provider FROM chat_message WHERE session_id = 's-legacy'"
        ).fetchall()
        assert legacy_rows == [(1, "旧版本回复", "pangu")], f"旧行未完整保留：{legacy_rows}"

        # 新值域可写：llm / local / pangu 全部允许
        for index, provider in enumerate(("llm", "local", "pangu")):
            conn.execute(
                "INSERT INTO chat_message (session_id, user_id, role, content, provider, created_at) "
                "VALUES (?, ?, 'assistant', ?, ?, ?)",
                ("s-new", "u-new", f"{provider} 回复", provider, f"2026-01-02T00:00:0{index}Z"),
            )
        conn.commit()
        stored = [
            row[0]
            for row in conn.execute(
                "SELECT provider FROM chat_message WHERE session_id = 's-new' ORDER BY id"
            )
        ]
        assert stored == ["llm", "local", "pangu"]

        # 对抗：CHECK 未被整体移除，未知值仍必须被拒
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO chat_message (session_id, user_id, role, content, provider, created_at) "
                "VALUES ('s-new', 'u-new', 'assistant', 'x', 'openai', '2026-01-02T00:00:09Z')"
            )
        conn.rollback()
    finally:
        conn.close()

    # 二次迁移必须幂等：不重复建表、不丢数据、SQL 仍含 llm
    init_db(db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        table_sql_again = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'chat_message'"
        ).fetchone()[0]
        assert table_sql_again == table_sql, "二次 init_db 改变了表结构（不幂等）"
        counts = conn.execute("SELECT COUNT(*) FROM chat_message").fetchone()[0]
        assert counts == 4, f"二次迁移丢失/重复了行（期望 4，实际 {counts}）"
        assert conn.execute(
            "SELECT COUNT(*) FROM chat_message WHERE provider = 'pangu' AND session_id = 's-legacy'"
        ).fetchone()[0] == 1, "旧行在二次迁移后丢失"

        leftover = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'chat_message__old'"
        ).fetchone()[0]
        assert leftover == 0, "迁移残留了临时表 chat_message__old"

        migrated_indexes = sorted(
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'chat_message' "
                "AND name NOT LIKE 'sqlite_autoindex%'"
            )
        )
    finally:
        conn.close()

    # 对抗：重建表会丢失原表索引——迁移必须把索引恢复到与全新库一致
    fresh_path = tmp_path / "fresh.db"
    init_db(fresh_path)
    conn = sqlite3.connect(str(fresh_path))
    try:
        fresh_indexes = sorted(
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'chat_message' "
                "AND name NOT LIKE 'sqlite_autoindex%'"
            )
        )
    finally:
        conn.close()
    assert migrated_indexes == fresh_indexes, (
        f"迁移后 chat_message 索引与全新库不一致：迁移={migrated_indexes} 全新={fresh_indexes}"
    )


# ---------------------------------------------------------------------------
# 4) 前端：语音元素 / 脚本顺序 / 无 CDN / 页面文件
# ---------------------------------------------------------------------------


def test_frontend_voice_ui_script_order_and_no_cdn() -> None:
    """``chat.html`` 含 6 个语音 id、脚本次序正确、无 CDN；5 个文件均存在。"""
    for relative in _EXPECTED_FRONTEND_FILES:
        target = FRONTEND_DIR / relative
        assert target.is_file(), f"缺少前端文件：{relative}"
        assert target.stat().st_size > 0, f"前端文件为空：{relative}"

    html = CHAT_HTML.read_text(encoding="utf-8")
    for element_id in _VOICE_IDS:
        assert f'id="{element_id}"' in html, f"chat.html 缺少元素 id='{element_id}'"

    script_sources = re.findall(r'<script\s+[^>]*src="([^"]+)"', html)
    assert script_sources, "chat.html 未引用任何脚本"
    expected_tail = ["/static/js/api.js", "/static/js/asr.js", "/static/js/chat.js"]
    positions = []
    for expected in expected_tail:
        assert expected in script_sources, f"未找到脚本 {expected}（实际 {script_sources}）"
        positions.append(script_sources.index(expected))
    assert positions == sorted(positions), f"脚本次序错误：{script_sources}"

    # 无 CDN / 远程资源（离线可用）
    for relative in ("chat.html", "js/asr.js", "js/chat.js", "js/api.js"):
        text = (FRONTEND_DIR / relative).read_text(encoding="utf-8")
        assert not re.search(r"(?:src|href)\s*=\s*[\"'](?:https?:)?//", text), (
            f"{relative} 引用了远程资源（CDN 违规）"
        )
        assert "cdn." not in text, f"{relative} 出现 CDN 域名"

    # ES5 风格：剔除注释行后不得出现 let/const/箭头函数（用户硬约束）
    for relative in ("js/asr.js", "js/chat.js"):
        text = (FRONTEND_DIR / relative).read_text(encoding="utf-8")
        code_lines = [
            line
            for line in text.splitlines()
            if not line.strip().startswith(("*", "//", "/*"))
        ]
        code = "\n".join(code_lines)
        assert not re.search(r"(?:^|[^\w$])(?:let|const)\s+[A-Za-z_$]", code), (
            f"{relative} 使用了 ES6 声明（应为 ES5）"
        )
        assert "=>" not in code, f"{relative} 使用了箭头函数（应为 ES5）"

    asr_js = (FRONTEND_DIR / "js" / "asr.js").read_text(encoding="utf-8")
    assert "ALA" in asr_js and "asr" in asr_js, "asr.js 未挂载 window.ALA.asr 命名空间"
    assert "/api/asr" in asr_js, "asr.js 未调用后端 /api/asr"


# ---------------------------------------------------------------------------
# 5) POST /api/asr 契约与降级
# ---------------------------------------------------------------------------


def test_post_asr_missing_user_id_returns_400(client) -> None:
    """缺 ``X-User-Id`` → 400 ``MISSING_USER_ID``（鉴权先于音频校验）。"""
    response = client.post("/api/asr")
    assert response.status_code == 400, f"实际 {response.status_code}: {response.text[:120]}"
    assert response.json()["code"] == "MISSING_USER_ID"


def test_post_asr_unavailable_when_no_vosk_or_model(sync_asr_asyncio) -> None:
    """无 vosk / 无模型 → 503 ``ASR_UNAVAILABLE``，``detail.reason`` 属固定集合。"""
    engine = build_asr_engine()  # 真实配置构造，本机未安装 vosk
    assert engine.available() is False

    status, body = call_post_asr(engine=engine, payload=make_wav())
    assert status == 503, f"{status} {body}"
    assert body["code"] == "ASR_UNAVAILABLE"
    assert body["message"]
    assert body["detail"]["reason"] in ("vosk_missing", "model_missing", "disabled")


def test_post_asr_bad_audio_returns_400(sync_asr_asyncio, tmp_path) -> None:
    """非 WAV 字节 → 400 ``ASR_BAD_AUDIO``（格式校验先于引擎可用性）。"""
    engine = AsrEngine(
        enabled=True,
        model_dir=tmp_path / "absent-model",
        lang="zh-CN",
        timeout=5.0,
        max_duration_ms=60000,
        max_bytes=10 * 1024 * 1024,
    )
    status, body = call_post_asr(engine=engine, payload=b"definitely-not-a-wav-file")
    assert status == 400, f"{status} {body}"
    assert body["code"] == "ASR_BAD_AUDIO"


def test_post_asr_oversized_returns_413(sync_asr_asyncio, tmp_path) -> None:
    """超过 ``ASR_MAX_BYTES`` → 413 ``ASR_TOO_LONG``（体积优先判定）。"""
    engine = AsrEngine(
        enabled=True,
        model_dir=tmp_path / "absent-model",
        lang="zh-CN",
        timeout=5.0,
        max_duration_ms=60000,
        max_bytes=200,
    )
    payload = make_wav(duration_ms=500)
    assert len(payload) > 200

    status, body = call_post_asr(engine=engine, payload=payload)
    assert status == 413, f"{status} {body}"
    assert body["code"] == "ASR_TOO_LONG"


def test_post_asr_time_over_limit_returns_413(sync_asr_asyncio, tmp_path) -> None:
    """[对抗] 体积合规但时长超限 → 413（避免仅凭体积判定漏检长音频）。"""
    engine = AsrEngine(
        enabled=True,
        model_dir=tmp_path / "absent-model",
        lang="zh-CN",
        timeout=5.0,
        max_duration_ms=1000,
        max_bytes=10 * 1024 * 1024,
    )
    status, body = call_post_asr(engine=engine, payload=make_wav(duration_ms=2000))
    assert status == 413, f"{status} {body}"
    assert body["code"] == "ASR_TOO_LONG"


def test_post_asr_success_shape_with_stub_engine(sync_asr_asyncio) -> None:
    """[LIMITED] 正常 200 形状：本机无 vosk，用替身引擎验证响应契约。"""
    import importlib.util

    if importlib.util.find_spec("vosk") is not None:  # pragma: no cover - 本机未安装
        pytest.skip("vosk 已安装，该替身用例不再需要")

    engine = StubAsrEngine()
    status, body = call_post_asr(engine=engine, payload=make_wav(), lang=" en-US ")
    assert status == 200, f"{status} {body}"
    assert set(body) == {"text", "provider", "vendor", "lang", "duration_ms", "model"}
    assert body["provider"] == ASR_PROVIDER == "vosk"
    assert body["vendor"] == "vosk"
    assert body["lang"] == "en-US" and engine.calls == ["en-US"]
    assert isinstance(body["duration_ms"], int) and body["duration_ms"] > 0
    assert body["model"] == "stub-model"

    blank_lang_status, blank_body = call_post_asr(engine=engine, payload=make_wav(), lang="   ")
    assert blank_lang_status == 200 and blank_body["lang"] == "zh-CN"


# ---------------------------------------------------------------------------
# 6) scripts/check_llm.py 退出码（子进程，带超时）
# ---------------------------------------------------------------------------

_CHECK_LLM_TIMEOUT = 60.0


def test_check_llm_exit_code_2_when_not_configured() -> None:
    """未配置 LLM → 退出码 2，且输出中不含任何密钥字样。"""
    proc = run_subprocess(
        "import runpy, sys; sys.argv = ['check_llm.py']; runpy.run_path('scripts/check_llm.py', run_name='__main__')",
        env_overrides={
            "LLM_BASE_URL": "",
            "LLM_API_KEY": "",
            "PANGU_API_URL": "",
            "PANGU_API_KEY": "",
        },
        timeout=_CHECK_LLM_TIMEOUT,
    )
    assert proc is not None, "[LIMITED] check_llm.py 未配置场景超时（>60s）"
    combined = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 2, f"期望退出码 2，实际 {proc.returncode}\n{combined[-800:]}"
    assert "未配置 LLM" in combined
    assert "sk-" not in combined, "输出出现疑似密钥片段"


def test_check_llm_exit_code_1_when_unreachable() -> None:
    """配置有效但端点不可达 → 退出码 1，且探针密钥被脱敏。"""
    probe_key = f"sk-check-probe-{uuid.uuid4().hex[:8]}"
    proc = run_subprocess(
        "import runpy, sys; sys.argv = ['check_llm.py']; runpy.run_path('scripts/check_llm.py', run_name='__main__')",
        env_overrides={
            "LLM_BASE_URL": "http://127.0.0.1:1",
            "LLM_API_KEY": probe_key,
            "PANGU_API_URL": "",
            "PANGU_API_KEY": "",
            "LLM_TIMEOUT": "2",
        },
        timeout=_CHECK_LLM_TIMEOUT,
    )
    assert proc is not None, "[LIMITED] check_llm.py 不可达场景超时（>60s）"
    combined = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 1, f"期望退出码 1，实际 {proc.returncode}\n{combined[-800:]}"
    assert "连通性测试失败" in combined
    assert probe_key not in combined, "check_llm.py 输出了明文密钥"
    assert "http://127.0.0.1:1/chat/completions" in combined, "未打印归一化后的脱敏 endpoint"


# ---------------------------------------------------------------------------
# 7) 密钥零泄露（响应 / 落库 / 日志 / repr 全量检索）
# ---------------------------------------------------------------------------


def test_api_key_never_leaks(client, auth_headers, temp_db_path, monkeypatch, isolated_llm_env, caplog) -> None:
    """探针密钥不得出现在 SSE、落库全文、日志与配置对象 repr 中。"""
    probe_key = f"sk-leak-canary-{uuid.uuid4().hex[:10]}"
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("LLM_API_KEY", probe_key)

    ai_config = ai_adapter_module.build_ai_config()
    assert ai_config.api_key == probe_key, "探针密钥未生效"
    assert probe_key not in repr(ai_config), "AIConfig repr 泄露密钥"
    adapter = ai_adapter_module.AIAdapter(ai_config)
    assert probe_key not in repr(adapter), "AIAdapter repr 泄露密钥"

    install_connect_refused(monkeypatch)

    created = client.post("/api/chat/sessions", json={"mode": "teacher"}, headers=auth_headers)
    session_id = created.json()["session_id"]
    with caplog.at_level("DEBUG"):
        response = client.post(
            "/api/chat/stream",
            json={"session_id": session_id, "content": "密钥泄露检查", "mode": "teacher"},
            headers=auth_headers,
        )
        messages = client.get(f"/api/chat/sessions/{session_id}/messages", headers=auth_headers)

    assert response.status_code == 200
    assert probe_key not in response.text, "SSE 响应体泄露密钥"
    assert probe_key not in messages.text, "消息接口泄露密钥"
    assert probe_key not in dump_database_text(temp_db_path), "数据库落库内容泄露密钥"
    assert probe_key not in caplog.text, "日志泄露密钥"

    # 对抗：check_llm 的脱敏函数对含密钥的异常文本同样有效
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "check_llm_module", REPO_ROOT / "scripts" / "check_llm.py"
    )
    assert spec is not None and spec.loader is not None
    check_llm_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(check_llm_module)
    scrubbed = check_llm_module.scrub_secret(
        f"POST failed Bearer {probe_key} url=http://x/?key={probe_key}", probe_key
    )
    assert probe_key not in scrubbed and "***" in scrubbed