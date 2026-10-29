#!/usr/bin/env python
"""T6 · 独立端到端验证脚本（`chat-llm-voice` 升级，plan 2.2.x / 3.3）。

本脚本由**独立验证者**编写，用于对已交付的真实集成产物做对抗性验证，不依赖
``tests/conftest.py`` 的私有夹具，可脱离 pytest 单独运行：

    python scripts/verify_llm_upgrade.py

逐项覆盖（与任务 T6 的必验项一一对应）：

1. 回归：``pytest tests`` 全量（含 ``test_e2e_smoke``）通过数与是否挂起；
2. 无凭证 SSE：``meta → delta* → done``，``provider == "local"``，落库一致；
3. 配置有效但端点不可达：仍回退 ``local``，无空回复、无 ``error`` 终止；
4. 迁移集成：旧 CHECK 表 → ``init_db()`` → 可写 ``'llm'``、旧数据保留、幂等；
5. 前端页面 HTTP 200 + ``chat.html`` 语音元素/脚本顺序/无 CDN；
6. ``/api/asr`` 契约与降级（缺 header/坏音频/超限/正常形状）；
7. ``scripts/check_llm.py`` 退出码（未配置 2 / 不可达 1）；
8. 密钥零泄露（响应、异常、日志、落库全量检索）。

沙箱适配（plan 2.3.3「注意事项」明确允许，且必须显式说明采用方式）：

* 本沙箱**禁止 ``socket.socketpair()`` 与出网/未监听端口的 ``connect``**，因此
  ``asyncio`` 事件循环、``uvicorn``、``TestClient`` 均无法直接使用（实测
  ``WinError 10051``）。
* 本脚本在**自身进程内**、仅在验证期间安装等价规避（``socketpair`` → 未连接
  socket、``anyio.to_thread.run_sync`` → 内联执行、``WindowsSelectorEventLoopPolicy``），
  随后用**真实 httpx + ASGITransport** 发起请求：FastAPI 路由、Pydantic 校验、
  真实 asyncio 事件循环（含 ``asyncio.to_thread``/``wait_for``）、Starlette 静态
  服务与 SSE ``StreamingResponse`` 均为**真实代码路径**，只有网络传输层被绕过。
* 真实远程 LLM 出网被沙箱阻断，故「远程成功（``provider=="llm"``）」以注入
  ``httpx`` 替身模块的方式验证（HTTP 之上的解析/回退/provider 透传/落库仍真实）；
  「不可达」则以**真实 httpx** 连接被沙箱丢弃的地址来验证回退。
* 结束前还原全部全局改写，不污染进程外环境。

安全：脚本自行创建临时库（``ALA_DB_PATH``），绝不触碰运行库
``backend/app/data/app.db``；密钥仅用一次性探针值，且会在全部输出中检索验证不泄露。
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import traceback
import uuid
import wave
from pathlib import Path
from typing import Any, Callable, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

RUN_DB_PATH = REPO_ROOT / "backend" / "app" / "data" / "app.db"

# ---------------------------------------------------------------------------
# 结果记录
# ---------------------------------------------------------------------------

PASS = "PASS"
FAIL = "FAIL"
LIMITED = "LIMITED"

_RESULTS: list[dict[str, str]] = []


def record(item: str, status: str, evidence: str, note: str = "") -> None:
    """记录一项结论并打印。"""
    entry = {"item": item, "status": status, "evidence": evidence, "note": note}
    _RESULTS.append(entry)
    label = {"PASS": "[PASS]   ", "FAIL": "[FAIL]   ", "LIMITED": "[LIMITED]"}[status]
    print(f"{label} {item}", flush=True)
    for line in textwrap.wrap(evidence, width=110) or [""]:
        print(f"          {line}", flush=True)
    if note:
        for line in textwrap.wrap(f"note: {note}", width=110):
            print(f"          {line}", flush=True)
    print(flush=True)


def check(item: str, fn: Callable[[], tuple[str, str, str]]) -> None:
    """执行一项检查；fn 返回 ``(status, evidence, note)``；异常一律记为 FAIL。"""
    try:
        status, evidence, note = fn()
    except BaseException as exc:  # noqa: BLE001 - 验证脚本需自证失败
        tb = traceback.format_exc().splitlines()
        status, evidence, note = FAIL, f"{type(exc).__name__}: {exc}", " / ".join(tb[-3:])
    record(item, status, evidence, note)


# ---------------------------------------------------------------------------
# 沙箱规避（仅本进程、仅验证期间）
# ---------------------------------------------------------------------------


class LoopWorkaround:
    """在沙箱内启用可用事件循环（不修改任何仓库文件）。"""

    def __init__(self) -> None:
        import anyio.to_thread as anyio_to_thread

        self._socket = socket
        self._original_socketpair = socket.socketpair
        self._asyncio = asyncio
        self._original_policy = asyncio.get_event_loop_policy()
        self._to_thread = anyio_to_thread
        self._original_run_sync = anyio_to_thread.run_sync
        self._installed = False

    def install(self) -> None:
        if self._installed:
            return

        def _fake_socketpair(family: Optional[int] = None, type: Optional[int] = None, proto: int = 0):
            # 未连接的 AF_INET socket 对：足以满足 asyncio self-pipe 的「存在性」需求。
            return (
                socket.socket(socket.AF_INET, socket.SOCK_STREAM),
                socket.socket(socket.AF_INET, socket.SOCK_STREAM),
            )

        self._socket.socketpair = _fake_socketpair  # type: ignore[assignment]
        try:
            self._asyncio.set_event_loop_policy(self._asyncio.WindowsSelectorEventLoopPolicy())
        except Exception:  # noqa: BLE001 - 非 Windows / 未来版本移除该策略
            pass

        def _inline_run_sync(func, *args, **kwargs):  # noqa: ANN001
            async def _runner():
                return func(*args)

            return _runner()

        self._to_thread.run_sync = _inline_run_sync  # type: ignore[assignment]
        self._installed = True

    def restore(self) -> None:
        if not self._installed:
            return
        self._socket.socketpair = self._original_socketpair  # type: ignore[assignment]
        self._to_thread.run_sync = self._original_run_sync  # type: ignore[assignment]
        try:
            self._asyncio.set_event_loop_policy(self._original_policy)
        except Exception:  # noqa: BLE001
            pass
        self._installed = False


WORKAROUND = LoopWorkaround()


# ---------------------------------------------------------------------------
# 请求辅助（真实 httpx + ASGITransport）
# ---------------------------------------------------------------------------


class SseEvent:
    """一条 SSE 事件。"""

    def __init__(self, name: str, data: str) -> None:
        self.name = name
        self.data = data

    def json(self) -> Any:
        return json.loads(self.data) if self.data else None


def asgi_request(
    app: Any,
    method: str,
    path: str,
    *,
    headers: Optional[dict[str, str]] = None,
    json_body: Any = None,
    files: Any = None,
    content: Any = None,
    timeout: float = 30.0,
) -> tuple[int, str, dict[str, str]]:
    """发起一次真实 ASGI 请求（通过 httpx ASGITransport）。"""
    import httpx

    async def _inner():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://verify.test") as client:
            response = await asyncio.wait_for(
                client.request(
                    method,
                    path,
                    headers=headers,
                    json=json_body,
                    files=files,
                    content=content,
                ),
                timeout=timeout,
            )
            return response.status_code, response.text, dict(response.headers)

    return asyncio.run(_inner())


def asgi_stream_sse(
    app: Any,
    path: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    *,
    timeout: float = 60.0,
) -> tuple[int, str, list[SseEvent]]:
    """发起一次真实 SSE 请求并解析出事件序列。"""
    import httpx

    async def _inner():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://verify.test") as client:
            events: list[SseEvent] = []
            async with client.stream(
                "POST", path, json=payload, headers=headers, timeout=timeout
            ) as response:
                name: Optional[str] = None
                data: Optional[str] = None
                async for line in response.aiter_lines():
                    if line == "":
                        if name is not None:
                            events.append(SseEvent(name, data or ""))
                        name, data = None, None
                    elif line.startswith("event:"):
                        name = line[len("event:") :].strip()
                    elif line.startswith("data:"):
                        data = line[len("data:") :].strip()
                if name is not None:
                    events.append(SseEvent(name, data or ""))
                return response.status_code, response.headers.get("content-type", ""), events

    return asyncio.run(_inner())


# ---------------------------------------------------------------------------
# 工具：临时库 / WAV / 旧表 / 环境隔离
# ---------------------------------------------------------------------------


def make_wav(duration_ms: int = 500, sample_rate: int = 16000, channels: int = 1,
             sample_width: int = 2) -> bytes:
    """构造指定规格的静音 WAV（默认 16kHz / 单声道 / PCM16）。"""
    frames = max(1, int(sample_rate * duration_ms / 1000))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(sample_width)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"\x00" * (frames * channels * sample_width))
    return buffer.getvalue()


OLD_CHAT_MESSAGE_DDL = """
CREATE TABLE IF NOT EXISTS chat_message (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content    TEXT NOT NULL,
    provider   TEXT CHECK (provider IS NULL OR provider IN ('pangu', 'local')),
    created_at TEXT NOT NULL
);
"""


def _env_without_llm() -> dict[str, str]:
    """返回剔除全部 LLM/PANGU 变量后的环境副本。"""
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(("LLM_", "PANGU_")):
            env.pop(key)
    return env


class EnvOverride:
    """临时覆盖进程环境变量（请求间切换配置，``get_settings()`` 无缓存故立即生效）。"""

    def __init__(self, **values: str) -> None:
        self._values = {k: v for k, v in values.items()}
        self._saved: dict[str, Optional[str]] = {}

    def __enter__(self) -> "EnvOverride":
        for key, value in self._values.items():
            self._saved[key] = os.environ.get(key)
            os.environ[key] = value
        return self

    def __exit__(self, *exc: Any) -> None:
        for key, original in self._saved.items():
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original
        return None


class CapturedLogs(logging.Handler):
    """捕获日志文本（用于密钥泄露检索，同时避免 lastResort 把警告刷到 stderr）。"""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record))
        except Exception:  # noqa: BLE001
            self.lines.append(str(record.msg))

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


#: 进程级日志捕获器：main() 挂到 root logger，既抑制默认 stderr 刷屏，又供项 8 检索。
LOG_CAPTURE = CapturedLogs()
LOG_CAPTURE.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))


def install_log_capture() -> None:
    """把全局日志捕获器挂到 root logger（幂等）。"""
    root = logging.getLogger()
    if LOG_CAPTURE not in root.handlers:
        root.addHandler(LOG_CAPTURE)
    root.setLevel(logging.INFO)


def remove_log_capture() -> None:
    """移除全局日志捕获器（脚本收尾）。"""
    logging.getLogger().removeHandler(LOG_CAPTURE)


# ---------------------------------------------------------------------------
# 必验项 1：回归
# ---------------------------------------------------------------------------


def item1_regression(pytest_timeout: float) -> tuple[str, str, str]:
    """全量 pytest（含 e2e_smoke）。"""
    command = [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"]
    started = time.time()
    try:
        completed = subprocess.run(
            command,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=pytest_timeout,
        )
    except subprocess.TimeoutExpired:
        return (
            FAIL,
            f"`pytest tests -q` 超过 {pytest_timeout:g}s 未退出（疑似挂起）",
            "需改用 --ignore=tests/test_e2e_smoke.py 单独复跑并说明挂起归属",
        )

    elapsed = time.time() - started
    tail = "\n".join(completed.stdout.strip().splitlines()[-3:])
    passed = re.search(r"(\d+) passed", completed.stdout)
    failed = re.search(r"(\d+) failed", completed.stdout)
    n_passed = int(passed.group(1)) if passed else 0
    n_failed = int(failed.group(1)) if failed else 0
    evidence = f"rc={completed.returncode} 用时{elapsed:.1f}s；{tail}"
    note = f"returncode={completed.returncode}, passed={n_passed}, failed={n_failed}"
    if completed.returncode == 0 and n_failed == 0:
        return PASS, evidence, note
    return FAIL, evidence, note


# ---------------------------------------------------------------------------
# 必验项 2 / 3 / 8：SSE provider 值域、配置切换与回退、密钥零泄露
# ---------------------------------------------------------------------------


def _bootstrap_app(db_path: Path):
    """在给定临时库上组装应用（``ALA_DB_PATH`` 需已生效）。"""
    from backend.app.main import create_app

    return create_app()


def _read_messages(db_path: Path, session_id: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT role, content, provider FROM chat_message WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _create_session(app: Any, user_id: str) -> str:
    status, body, _ = asgi_request(
        app, "POST", "/api/chat/sessions", json_body={"mode": "teacher"},
        headers={"X-User-Id": user_id},
    )
    if status != 201:
        raise AssertionError(f"创建会话失败：{status} {body[:200]}")
    return json.loads(body)["session_id"]


def _persisted_provider(messages: list[dict[str, Any]]) -> Any:
    """返回落库的 assistant 提供方（无则 None）。"""
    if not messages:
        return None
    return messages[0]["provider"]


def item2_sse_no_credentials(db_path: Path) -> tuple[str, str, str]:
    """无凭证：帧序列 meta→delta*→done，provider=local，落库一致。"""
    with EnvOverride():
        pass  # 环境已在脚本启动时剔除 LLM/PANGU
    app = _bootstrap_app(db_path)
    user_id = f"verify-{uuid.uuid4().hex[:8]}"
    session_id = _create_session(app, user_id)
    status, content_type, events = asgi_stream_sse(
        app,
        "/api/chat/stream",
        {"session_id": session_id, "content": "你好，请介绍一下细胞膜"},
        {"X-User-Id": user_id},
    )
    names = [event.name for event in events]
    meta = events[0].json() if events else {}
    done = events[-1].json() if events else {}
    messages = _read_messages(db_path, session_id)
    assistant = [m for m in messages if m["role"] == "assistant"]

    problems: list[str] = []
    if status != 200:
        problems.append(f"HTTP {status}")
    if not names or names[0] != "meta":
        problems.append(f"首帧非 meta：{names[:3]}")
    if "error" in names:
        problems.append("出现 error 帧")
    if not names or names[-1] != "done":
        problems.append(f"末帧非 done：{names[-3:]}")
    if names[1:-1] and any(n != "delta" for n in names[1:-1]):
        problems.append(f"中间帧非 delta：{sorted(set(names[1:-1]))}")
    if meta.get("provider") != "local":
        problems.append(f"meta.provider={meta.get('provider')!r}")
    if done.get("provider") != "local":
        problems.append(f"done.provider={done.get('provider')!r}")
    if not (done.get("content") or "").strip():
        problems.append("done.content 为空（空回复）")
    if len(assistant) != 1 or assistant[0]["provider"] != "local":
        problems.append(f"落库 assistant={assistant}")
    if not (assistant and assistant[0]["content"].strip()):
        problems.append("落库 assistant 内容为空")

    evidence = (
        f"HTTP {status} {content_type}; frames={names[0]}→delta×{max(0, len(names) - 2)}→{names[-1]}; "
        f"meta.provider={meta.get('provider')!r}; done.provider={done.get('provider')!r}; "
        f"done.content_len={len(done.get('content') or '')}; "
        f"persisted assistant provider={assistant[0]['provider']!r} len={len(assistant[0]['content'])}"
        if assistant
        else f"HTTP {status}; frames={names}; persisted assistant=∅"
    )
    if problems:
        return FAIL, evidence, "；".join(problems)
    return PASS, evidence, ""


def item3_unreachable_falls_back(db_path: Path, probe_key: str) -> tuple[str, str, str]:
    """配置有效但端点不可达（真实 httpx 连接被沙箱丢弃）→ 回退 local。"""
    app = _bootstrap_app(db_path)
    user_id = f"verify-{uuid.uuid4().hex[:8]}"
    session_id = _create_session(app, user_id)

    # 127.0.0.1:1 无监听且被沙箱丢弃（connect 超时），LLM_TIMEOUT=2 缩短等待。
    with EnvOverride(
        LLM_BASE_URL="http://127.0.0.1:1",
        LLM_API_KEY=probe_key,
        LLM_MODEL="probe-model",
        LLM_TIMEOUT="2",
    ):
        started = time.time()
        status, _, events = asgi_stream_sse(
            app,
            "/api/chat/stream",
            {"session_id": session_id, "content": "配置了但不可达会怎样？"},
            {"X-User-Id": user_id},
            timeout=60.0,
        )
        elapsed = time.time() - started

    names = [event.name for event in events]
    meta = events[0].json() if events else {}
    done = events[-1].json() if events else {}
    messages = _read_messages(db_path, session_id)
    assistant = [m for m in messages if m["role"] == "assistant"]

    problems: list[str] = []
    if status != 200:
        problems.append(f"HTTP {status}")
    if meta.get("provider") != "llm":
        problems.append(f"meta.provider 应为尝试值 'llm'，实际 {meta.get('provider')!r}")
    if "error" in names:
        problems.append("出现 error 帧（不得以 error 终止）")
    if done.get("provider") != "local":
        problems.append(f"done.provider={done.get('provider')!r}（应回退 local）")
    if not (done.get("content") or "").strip():
        problems.append("done.content 为空（空回复）")
    if len(assistant) != 1 or assistant[0]["provider"] != "local":
        problems.append(f"落库 assistant={assistant}")

    evidence = (
        f"endpoint=http://127.0.0.1:1（沙箱内 connect 被丢弃）; 用时{elapsed:.1f}s; "
        f"frames={names[0]}→delta×{max(0, len(names) - 2)}→{names[-1]}; "
        f"meta.provider={meta.get('provider')!r}; done.provider={done.get('provider')!r}; "
        f"done.content_len={len(done.get('content') or '')}; "
        f"persisted={_persisted_provider(assistant)!r}"
    )
    if problems:
        return FAIL, evidence, "；".join(problems)
    return PASS, evidence, "真实 httpx 连接失败被捕获 → 本地整段回退"


def item8_key_leakage(db_path: Path, probe_key: str) -> tuple[str, str, str]:
    """密钥零泄露：响应 / SSE 帧 / 异常 / 日志 / 落库全量检索。"""
    app = _bootstrap_app(db_path)
    user_id = f"verify-{uuid.uuid4().hex[:8]}"
    session_id = _create_session(app, user_id)


    haystacks: list[tuple[str, str]] = []
    try:
        # 1) 不可达远程 + 探针密钥（真实 httpx 失败路径）
        with EnvOverride(
            LLM_BASE_URL="http://127.0.0.1:1",
            LLM_API_KEY=probe_key,
            LLM_MODEL="probe-model",
            LLM_TIMEOUT="2",
        ):
            status, _, events = asgi_stream_sse(
                app,
                "/api/chat/stream",
                {"session_id": session_id, "content": "泄露检查"},
                {"X-User-Id": user_id},
                timeout=60.0,
            )
        haystacks.append(("sse-frames", "\n".join(e.data for e in events)))
        haystacks.append(("sse-raw-bodies", json.dumps([(e.name, e.data) for e in events], ensure_ascii=False)))
        haystacks.append(("sse-status", str(status)))

        # 2) 其它端点响应（含错误体）
        for method, path, body in (
            ("GET", "/health", None),
            ("GET", "/api/chat/sessions", None),
            ("POST", "/api/asr", None),
        ):
            headers = {"X-User-Id": user_id}
            st, text, _ = asgi_request(app, method, path, headers=headers, json_body=body)
            haystacks.append((f"{method} {path}", text))
            haystacks.append((f"{method} {path} status", str(st)))

        # 3) 异常 repr（含 AIConfig）
        from backend.app.services.ai_adapter import AIConfig, build_ai_config

        config = AIConfig(api_url="http://127.0.0.1:1", api_key=probe_key)
        haystacks.append(("AIConfig.repr", repr(config)))
        haystacks.append(("AIConfig.dict", json.dumps(getattr(config, "__dict__", {}), default=str)))
        with EnvOverride(LLM_BASE_URL="http://127.0.0.1:1", LLM_API_KEY=probe_key):
            built = build_ai_config()
        try:
            raise RuntimeError(f"合成异常携带密钥 {probe_key}")
        except RuntimeError as exc:  # noqa: PERF203
            haystacks.append(("exception-repr", repr(exc)))

        # 4) 落库全表文本检索
        conn = sqlite3.connect(str(db_path))
        try:
            tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            ]
            db_dump: list[str] = []
            for table in tables:
                for row in conn.execute(f"SELECT * FROM {table}"):
                    db_dump.append(json.dumps([str(v) for v in row], ensure_ascii=False))
            haystacks.append(("db-dump", "\n".join(db_dump)))
        finally:
            conn.close()

        # 5) 自检脚本输出（未配置 + 不可达）
        for label, env in (
            ("check_llm-unconfigured", {"LLM_BASE_URL": "", "LLM_API_KEY": ""}),
            ("check_llm-unreachable", {"LLM_BASE_URL": "http://127.0.0.1:9", "LLM_API_KEY": probe_key}),
        ):
            child_env = dict(os.environ)
            for key in list(child_env):
                if key.startswith(("LLM_", "PANGU_")):
                    child_env.pop(key)
            child_env.update(env)
            proc = subprocess.run(
                [sys.executable, "scripts/check_llm.py"],
                cwd=str(REPO_ROOT),
                env=child_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
            haystacks.append((label, proc.stdout + proc.stderr))
    finally:
        root_logger.removeHandler(captured)

    haystacks.append(("captured-logs", captured.text))

    leaks = [name for name, text in haystacks if probe_key in text]
    total_chars = sum(len(text) for _, text in haystacks)
    evidence = (
        f"检索面：{len(haystacks)} 处（含 SSE 帧/错误体/AIConfig repr/异常/落库 dump/日志/自检输出），"
        f"共 {total_chars} 字符；探针密钥前缀检索命中 {len(leaks)} 处"
    )
    if leaks:
        return FAIL, evidence, f"泄露位置：{leaks}"
    return PASS, evidence, "探针密钥 'SK-PROBE-...' 全量检索零命中"


# ---------------------------------------------------------------------------
# 必验项 4：迁移集成
# ---------------------------------------------------------------------------


def item4_migration(tmp_dir: Path) -> tuple[str, str, str]:
    """旧 CHECK 表 → init_db() → 可写 'llm'、旧数据保留、二次 init_db 幂等。"""
    from backend.app.db import init_db

    db_path = tmp_dir / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(OLD_CHAT_MESSAGE_DDL)
        conn.execute(
            "INSERT INTO chat_message (session_id, user_id, role, content, provider, created_at) "
            "VALUES ('s-1','u-1','user','旧用户消息',NULL,'2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO chat_message (session_id, user_id, role, content, provider, created_at) "
            "VALUES ('s-1','u-1','assistant','旧助手消息','pangu','2026-01-01T00:00:01Z')"
        )
        conn.execute(
            "INSERT INTO chat_message (session_id, user_id, role, content, provider, created_at) "
            "VALUES ('s-1','u-1','assistant','旧本地消息','local','2026-01-01T00:00:02Z')"
        )
        conn.commit()
        before_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='chat_message'"
        ).fetchone()[0]
        before_rows = conn.execute(
            "SELECT id, role, content, provider FROM chat_message ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    if "'llm'" in before_sql:
        return FAIL, "构造的旧表已含 'llm'（前置条件不成立）", before_sql[:120]

    init_db(db_path)  # 第一次：应执行迁移

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        after_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='chat_message'"
        ).fetchone()[0]
        after_rows = [
            tuple(row)
            for row in conn.execute(
                "SELECT id, role, content, provider FROM chat_message ORDER BY id"
            )
        ]
        write_ok = True
        write_error = ""
        try:
            conn.execute(
                "INSERT INTO chat_message (session_id, user_id, role, content, provider, created_at) "
                "VALUES ('s-2','u-1','assistant','新值域消息','llm','2026-01-02T00:00:00Z')"
            )
            conn.commit()
        except sqlite3.Error as exc:
            write_ok, write_error = False, f"{type(exc).__name__}: {exc}"
        indexes = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='chat_message'"
            )
        ]
        count_after_first = conn.execute("SELECT COUNT(*) FROM chat_message").fetchone()[0]
    finally:
        conn.close()

    init_db(db_path)  # 第二次：幂等

    conn = sqlite3.connect(str(db_path))
    try:
        count_after_second = conn.execute("SELECT COUNT(*) FROM chat_message").fetchone()[0]
        second_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='chat_message'"
        ).fetchone()[0]
        leftover = conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'chat_message__old%'"
        ).fetchall()
    finally:
        conn.close()

    problems: list[str] = []
    if "'llm'" not in after_sql:
        problems.append("迁移后 CHECK 仍不含 'llm'")
    if not write_ok:
        problems.append(f"迁移后无法写入 provider='llm'：{write_error}")
    if len(after_rows) != len(before_rows) or after_rows != [tuple(r) for r in before_rows]:
        problems.append(f"旧数据变更：before={[tuple(r) for r in before_rows]} after={after_rows}")
    if count_after_second != count_after_first:
        problems.append(f"二次 init_db 非幂等：{count_after_first} → {count_after_second}")
    if "idx_chat_message_session" not in indexes:
        problems.append(f"迁移后索引缺失：{indexes}")
    if leftover:
        problems.append(f"残留临时表：{leftover}")

    evidence = (
        f"迁移前 CHECK: 仅 'pangu','local'（{len(before_rows)} 行）→ 迁移后 CHECK 含 'llm'；"
        f"可写 provider='llm'={write_ok}；旧行保留={after_rows == [tuple(r) for r in before_rows]}；"
        f"索引={indexes}；二次 init_db 行数 {count_after_first}→{count_after_second}"
    )
    if problems:
        return FAIL, evidence, "；".join(problems)
    return PASS, evidence, ""


# ---------------------------------------------------------------------------
# 必验项 5：前端页面与语音元素
# ---------------------------------------------------------------------------


REQUIRED_DOM_IDS = (
    "asr-record-btn",
    "asr-status",
    "voice-input-btn",
    "voice-status",
    "speak-toggle",
    "speak-stop-btn",
)

_CDN_PATTERN = re.compile(r'(?:src|href)\s*=\s*["\']https?://', re.IGNORECASE)
_SCRIPT_PATTERN = re.compile(r'<script[^>]*\bsrc\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)


def item5_frontend(db_path: Path, external_base: Optional[str]) -> tuple[str, str, str]:
    """五页 HTTP 200 + chat.html 六个 id + 脚本顺序 + 无 CDN。"""
    app = _bootstrap_app(db_path)
    pages = {
        "/": "index.html",
        "/static/index.html": "index.html",
        "/static/knowledge.html": "knowledge.html",
        "/static/chat.html": "chat.html",
        "/static/profile.html": "profile.html",
    }
    statuses: list[str] = []
    problems: list[str] = []
    for path in pages:
        status, text, _ = asgi_request(app, "GET", path)
        statuses.append(f"{path}={status},{len(text)}B")
        if status != 200:
            problems.append(f"{path} → HTTP {status}")
        if not text.strip():
            problems.append(f"{path} 内容为空")

    chat_html = (REPO_ROOT / "frontend" / "chat.html").read_text(encoding="utf-8")
    missing_ids = [dom_id for dom_id in REQUIRED_DOM_IDS if f'id="{dom_id}"' not in chat_html]
    if missing_ids:
        problems.append(f"chat.html 缺少 id：{missing_ids}")

    script_srcs = _SCRIPT_PATTERN.findall(chat_html)
    expected_order = ["/static/js/api.js", "/static/js/asr.js", "/static/js/chat.js"]
    actual_js = [src for src in script_srcs if "/static/js/" in src]
    if actual_js != expected_order:
        problems.append(f"脚本顺序/集合不符：{actual_js}（期望 {expected_order}）")

    cdn_hits: list[str] = []
    for page_file in sorted({name for name in pages.values()}):
        html = (REPO_ROOT / "frontend" / page_file).read_text(encoding="utf-8")
        for match in _CDN_PATTERN.findall(html):
            cdn_hits.append(f"{page_file}:{match}")
    if cdn_hits:
        problems.append(f"存在 CDN/远程资源引用：{cdn_hits}")

    external_evidence = ""
    if external_base:
        import httpx

        try:
            with httpx.Client(base_url=external_base, timeout=5.0) as client:
                ext = []
                for path in pages:
                    resp = client.get(path)
                    ext.append(f"{path}={resp.status_code}")
                external_evidence = f"；外部服务器({external_base})只读交叉核对：" + ", ".join(ext)
        except Exception as exc:  # noqa: BLE001
            external_evidence = f"；外部服务器({external_base})不可用：{type(exc).__name__}"

    evidence = (
        f"ASGI 直调(真实 StaticFiles)：{', '.join(statuses)}；"
        f"chat.html 六 id 齐全={not missing_ids}；脚本顺序={actual_js}；CDN 引用={len(cdn_hits)}"
        + external_evidence
    )
    if problems:
        return FAIL, evidence, "；".join(problems)
    return PASS, evidence, ""


# ---------------------------------------------------------------------------
# 必验项 6：/api/asr 契约与降级
# ---------------------------------------------------------------------------


class _FakeVoskModel:
    def __init__(self, path: str) -> None:
        self.path = path


class _FakeVoskRecognizer:
    def __init__(self, model: Any, sample_rate: int) -> None:
        self._sample_rate = sample_rate
        self._text = "细胞膜由磷脂双分子层构成"

    def AcceptWaveform(self, chunk: bytes) -> bool:
        return False

    def Result(self) -> str:
        return json.dumps({"text": ""})

    def FinalResult(self) -> str:
        return json.dumps({"text": self._text})


def _fake_vosk_module() -> Any:
    import types

    return types.SimpleNamespace(Model=_FakeVoskModel, KaldiRecognizer=_FakeVoskRecognizer)


class with_overridden_engine:
    """临时用给定引擎覆盖 ``get_asr_engine`` 依赖（FastAPI ``dependency_overrides``）。"""

    def __init__(self, app: Any, dependency: Any, engine: Any) -> None:
        self._app = app
        self._dependency = dependency
        self._engine = engine

    def __enter__(self) -> Any:
        self._app.dependency_overrides[self._dependency] = lambda: self._engine
        return self

    def __exit__(self, *exc: Any) -> None:
        self._app.dependency_overrides.pop(self._dependency, None)
        return None


def item6_asr_contract(db_path: Path, tmp_dir: Path) -> tuple[str, str, str]:
    """缺 header 400 / 坏音频 400 / 超限 413 / 正常 200 形状；无 vosk → 503。"""
    app = _bootstrap_app(db_path)
    user_id = f"verify-{uuid.uuid4().hex[:8]}"
    headers = {"X-User-Id": user_id}
    problems: list[str] = []
    parts: list[str] = []

    # (a) 缺 X-User-Id → 400 MISSING_USER_ID
    status, text, _ = asgi_request(
        app, "POST", "/api/asr",
        files={"audio": ("a.wav", b"RIFF....WAVEjunk", "audio/wav")},
    )
    body = json.loads(text)
    parts.append(f"no-header={status}/{body.get('code')}")
    if status != 400 or body.get("code") != "MISSING_USER_ID":
        problems.append(f"缺 X-User-Id 期望 400 MISSING_USER_ID，实际 {status} {body.get('code')}")

    # (b) 坏音频 → 400 ASR_BAD_AUDIO
    status, text, _ = asgi_request(
        app, "POST", "/api/asr",
        files={"audio": ("a.wav", b"RIFFxxxxxxxxWAVEjunk-not-a-valid-wav", "audio/wav")},
        headers=headers,
    )
    body = json.loads(text)
    parts.append(f"bad-audio={status}/{body.get('code')}")
    if status != 400 or body.get("code") != "ASR_BAD_AUDIO":
        problems.append(f"坏音频期望 400 ASR_BAD_AUDIO，实际 {status} {body.get('code')}")

    # (c) 超体积 → 413 ASR_TOO_LONG（注入小阈值引擎：避免 10MB 上传在受限沙箱内阻塞）
    from backend.app.routers.asr import get_asr_engine
    from backend.app.services.asr import AsrEngine

    small_bytes_engine = AsrEngine(
        enabled=True,
        model_dir=tmp_dir / "absent-model",
        lang="zh-CN",
        timeout=10.0,
        max_duration_ms=60_000,
        max_bytes=1024,
    )
    with_overridden_engine(app, get_asr_engine, small_bytes_engine):
        status, text, _ = asgi_request(
            app, "POST", "/api/asr",
            files={"audio": ("big.wav", make_wav(duration_ms=200), "audio/wav")},
            headers=headers,
        )
    body = json.loads(text)
    parts.append(f"too-long-bytes={status}/{body.get('code')}")
    if status != 413 or body.get("code") != "ASR_TOO_LONG":
        problems.append(f"超体积期望 413 ASR_TOO_LONG，实际 {status} {body.get('code')}")

    # (d) 超时长 → 413 ASR_TOO_LONG（注入 max_duration_ms=100 的引擎）
    small_duration_engine = AsrEngine(
        enabled=True,
        model_dir=tmp_dir / "absent-model",
        lang="zh-CN",
        timeout=10.0,
        max_duration_ms=100,
        max_bytes=10 * 1024 * 1024,
    )
    with_overridden_engine(app, get_asr_engine, small_duration_engine):
        status, text, _ = asgi_request(
            app, "POST", "/api/asr",
            files={"audio": ("long.wav", make_wav(duration_ms=500), "audio/wav")},
            headers=headers,
        )
    body = json.loads(text)
    parts.append(f"too-long-duration={status}/{body.get('code')}")
    if status != 413 or body.get("code") != "ASR_TOO_LONG":
        problems.append(f"超时长期望 413 ASR_TOO_LONG，实际 {status} {body.get('code')}")

    # (e) 未安装 vosk → 503 ASR_UNAVAILABLE（真实环境无 vosk，走默认引擎）
    status, text, _ = asgi_request(
        app, "POST", "/api/asr",
        files={"audio": ("ok.wav", make_wav(duration_ms=200), "audio/wav")},
        headers=headers,
    )
    body = json.loads(text)
    parts.append(f"unavailable={status}/{body.get('code')}/{body.get('detail')}")
    if status != 503 or body.get("code") != "ASR_UNAVAILABLE":
        problems.append(f"无 vosk 期望 503 ASR_UNAVAILABLE，实际 {status} {body.get('code')}")
    reason = (body.get("detail") or {}).get("reason")
    if reason not in {"vosk_missing", "model_missing", "disabled"}:
        problems.append(f"detail.reason 非法：{reason!r}")

    # (e) 正常 200 形状（注入替身 vosk + 存在的模型目录；真实识别需联网机器，见 note）
    from backend.app.routers.asr import get_asr_engine
    from backend.app.services.asr import AsrEngine

    model_dir = tmp_dir / "vosk-model-small-cn-0.22"
    model_dir.mkdir(parents=True, exist_ok=True)
    engine = AsrEngine(
        enabled=True,
        model_dir=model_dir,
        lang="zh-CN",
        timeout=10.0,
        max_duration_ms=60000,
        max_bytes=10 * 1024 * 1024,
    )
    saved = sys.modules.get("vosk")
    sys.modules["vosk"] = _fake_vosk_module()
    app.dependency_overrides[get_asr_engine] = lambda: engine
    try:
        status, text, _ = asgi_request(
            app, "POST", "/api/asr",
            files={"audio": ("rec.wav", make_wav(duration_ms=800), "audio/wav")},
            headers=headers,
            timeout=60.0,
        )
    finally:
        app.dependency_overrides.pop(get_asr_engine, None)
        if saved is None:
            sys.modules.pop("vosk", None)
        else:
            sys.modules["vosk"] = saved

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = {}
    expected_fields = {"text", "provider", "vendor", "lang", "duration_ms", "model"}
    parts.append(f"ok-shape={status}/{sorted(payload)}")
    if status != 200:
        problems.append(f"正常路径期望 200，实际 {status} {text[:160]}")
    if not expected_fields.issubset(payload):
        problems.append(f"响应缺少字段：{sorted(expected_fields - set(payload))}")
    if payload.get("provider") != "vosk" or payload.get("vendor") != "vosk":
        problems.append(f"provider/vendor 非 vosk：{payload.get('provider')}/{payload.get('vendor')}")
    if payload.get("lang") != "zh-CN":
        problems.append(f"lang 非 zh-CN：{payload.get('lang')!r}")
    if not isinstance(payload.get("duration_ms"), int) or payload.get("duration_ms", 0) <= 0:
        problems.append(f"duration_ms 非法：{payload.get('duration_ms')!r}")
    if not str(payload.get("text") or "").strip():
        problems.append("替身识别文本为空（形状验证无法覆盖非空文本）")

    evidence = "；".join(parts)
    note = (
        "正常识别文本来自进程内替身 vosk 模块（沙箱无法 pip install vosk / 下载模型），"
        "标 LIMITED：真实识别需在联网机器执行 `pip install vosk` + "
        "`python scripts/download_asr_model.py` 后用真实模型复跑。"
    )
    if problems:
        return FAIL, evidence, "；".join(problems)
    return LIMITED, evidence, note


# ---------------------------------------------------------------------------
# 必验项 7：check_llm 退出码
# ---------------------------------------------------------------------------


def _run_check_llm(env_overrides: dict[str, str], extra_args: list[str] | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(("LLM_", "PANGU_")):
            env.pop(key)
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "scripts/check_llm.py", *(extra_args or [])],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def item7_check_llm(probe_key: str) -> tuple[str, str, str]:
    """未配置 → 2；已配置但不可达 → 1；输出不含密钥。"""
    problems: list[str] = []

    unconfigured = _run_check_llm({"LLM_BASE_URL": "", "LLM_API_KEY": ""})
    unreachable = _run_check_llm(
        {"LLM_BASE_URL": "http://127.0.0.1:9", "LLM_API_KEY": probe_key}, ["--timeout", "5"]
    )
    timeout_case = _run_check_llm(
        {"LLM_BASE_URL": "http://127.0.0.1:1", "LLM_API_KEY": probe_key}, ["--timeout", "2"]
    )

    if unconfigured.returncode != 2:
        problems.append(f"未配置期望退出码 2，实际 {unconfigured.returncode}")
    if unreachable.returncode != 1:
        problems.append(f"不可达期望退出码 1，实际 {unreachable.returncode}")
    if timeout_case.returncode != 1:
        problems.append(f"超时（127.0.0.1:1）期望退出码 1，实际 {timeout_case.returncode}")
    if probe_key in (unconfigured.stdout + unconfigured.stderr + unreachable.stdout
                     + unreachable.stderr + timeout_case.stdout + timeout_case.stderr):
        problems.append("自检输出出现密钥")

    evidence = (
        f"未配置 rc={unconfigured.returncode}({unconfigured.stdout.strip().splitlines()[-1][:60] if unconfigured.stdout.strip() else ''})；"
        f"不可达(127.0.0.1:9) rc={unreachable.returncode}；"
        f"超时(127.0.0.1:1, --timeout 2) rc={timeout_case.returncode}；输出含密钥=False"
    )
    note = "沙箱禁止出网，故「已配置且可达 → 0」无法验证（属环境限制，非功能缺陷）"
    if problems:
        return FAIL, evidence, "；".join(problems)
    return PASS, evidence, note


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="T6 独立端到端验证（chat-llm-voice）")
    parser.add_argument("--skip-regression", action="store_true", help="跳过全量 pytest 回归")
    parser.add_argument("--pytest-timeout", type=float, default=420.0, help="回归运行超时（秒）")
    parser.add_argument("--external-base", default="http://127.0.0.1:8000",
                        help="可选：用于交叉核对静态页的外部服务器地址（只读 GET）")
    parser.add_argument("--items", default="", help="逗号分隔：仅运行指定项（如 2,3,6）")
    args = parser.parse_args(argv)

    selected = {token.strip() for token in args.items.split(",") if token.strip()}

    def wanted(key: str) -> bool:
        return not selected or key in selected

    work_dir = Path(tempfile.mkdtemp(prefix="ala_verify_"))
    db_path = work_dir / "verify.db"
    probe_key = f"SK-PROBE-{uuid.uuid4().hex[:12]}"

    print("=" * 78)
    print("T6 · 独立端到端验证 —— chat-llm-voice 升级")
    print(f"仓库：{REPO_ROOT}")
    print(f"临时库（不触碰运行库）：{db_path}")
    print(f"运行库（仅用于断言未被污染）：{RUN_DB_PATH}")
    print("=" * 78)
    print()

    # 关键：在导入任何 backend 模块之前切换库路径，并剔除 LLM/PANGU 环境。
    os.environ["ALA_DB_PATH"] = str(db_path)
    for key in list(os.environ):
        if key.startswith(("LLM_", "PANGU_")):
            os.environ.pop(key)

    WORKAROUND.install()
    runtime_note = (
        "沙箱内 socketpair/事件循环不可用；已安装等价规避（socketpair→未连接 socket、"
        "anyio.to_thread.run_sync→内联、SelectorEventLoopPolicy），"
        "以真实 httpx + ASGITransport 执行真实 FastAPI/Starlette 代码路径（绕过网络传输层）。"
    )
    print(f"[环境] {runtime_note}")
    print()

    run_db_mtime_before = RUN_DB_PATH.stat().st_mtime if RUN_DB_PATH.exists() else None
    run_db_size_before = RUN_DB_PATH.stat().st_size if RUN_DB_PATH.exists() else None

    try:
        if wanted("1"):
            check("1. 回归 pytest tests -q（含 e2e_smoke）", lambda: item1_regression(args.pytest_timeout))
        if wanted("2"):
            check("2. 无凭证 SSE 回归（provider=local + 落库）", lambda: item2_sse_no_credentials(db_path))
        if wanted("3"):
            check("3. 配置有效但端点不可达 → 回退 local",
                  lambda: item3_unreachable_falls_back(db_path, probe_key))
        if wanted("4"):
            check("4. 迁移集成（旧 CHECK → 'llm'，幂等）", lambda: item4_migration(work_dir))
        if wanted("5"):
            check("5. 前端页面 200 + 语音元素/脚本顺序/无 CDN",
                  lambda: item5_frontend(db_path, args.external_base))
        if wanted("6"):
            check("6. /api/asr 契约与降级", lambda: item6_asr_contract(db_path, work_dir))
        if wanted("7"):
            check("7. scripts/check_llm.py 退出码", lambda: item7_check_llm(probe_key))
        if wanted("8"):
            check("8. 密钥零泄露（全量检索）", lambda: item8_key_leakage(db_path, probe_key))
    finally:
        WORKAROUND.restore()

    # 运行库未被污染断言
    run_db_mtime_after = RUN_DB_PATH.stat().st_mtime if RUN_DB_PATH.exists() else None
    run_db_size_after = RUN_DB_PATH.stat().st_size if RUN_DB_PATH.exists() else None
    polluted = (run_db_mtime_before, run_db_size_before) != (run_db_mtime_after, run_db_size_after)
    record(
        "0. 运行库未被污染",
        FAIL if polluted else PASS,
        f"mtime/size before=({run_db_mtime_before},{run_db_size_before}) "
        f"after=({run_db_mtime_after},{run_db_size_after})",
        "" if not polluted else "运行库被写入，违反「使用独立临时库」约束",
    )

    print("=" * 78)
    print("逐项结论")
    print("=" * 78)
    for entry in _RESULTS:
        print(f"{entry['status']:>7}  {entry['item']}")

    failures = [e for e in _RESULTS if e["status"] == FAIL]
    limited = [e for e in _RESULTS if e["status"] == LIMITED]
    print()
    print(f"合计：PASS={sum(1 for e in _RESULTS if e['status'] == PASS)} "
          f"FAIL={len(failures)} LIMITED={len(limited)}")

    if failures:
        print()
        print("失败项：")
        for entry in failures:
            print(f"  - {entry['item']}：{entry['evidence']}")
            if entry["note"]:
                print(f"    原因：{entry['note']}")

    verdict = "PASS" if not failures else "FAIL"
    if not failures and limited:
        verdict = "PARTIAL"
    print()
    print(f"VERDICT: {verdict}")
    return 0 if verdict != "FAIL" else 1


if __name__ == "__main__":
    sys.exit(main())