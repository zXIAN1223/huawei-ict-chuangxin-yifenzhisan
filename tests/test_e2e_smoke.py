"""T14 · 端到端集成冒烟测试（独立验证，plan 2.3.3 / 3.3）。

以真实服务器为优先：默认尝试以子进程 ``python run.py`` 启动真实服务器并走真实
HTTP；若沙箱限制 127.0.0.1 回环（``WinError 10051``）导致无法启动，则回退为
**进程内 ASGI 直调**（httpx.ASGITransport），仍完整经过 FastAPI 路由、Pydantic
校验与全部业务逻辑，仅绕过网络栈。

运行：``pytest tests/test_e2e_smoke.py -q``。

回退可信性：见 :func:`_install_loop_workaround`（socketpair→未连接 socket、
WindowsSelectorEventLoop、anyio 线程池内联），该规避仅在**本测试执行期间**生效，
测试结束后立即还原，不影响其它测试模块。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PRIMARY_KP = "cell-membrane"
SECONDARY_KP = "genetics-gene-trait"

if not shutil.which("python"):  # pragma: no cover
    pass


# ---------------------------------------------------------------------------
# 沙箱规避层（仅本测试执行期间生效，测试结束还原）
# ---------------------------------------------------------------------------
class _SandboxWorkaround:
    """保存并在退出时还原被改写的 asyncio / anyio / socket 全局项。"""

    def __init__(self) -> None:
        import asyncio as _asyncio
        import anyio.to_thread as _to_thread

        self._socket_module = socket
        self._original_socketpair = socket.socketpair
        self._asyncio = _asyncio
        self._original_policy = _asyncio.get_event_loop_policy()
        self._to_thread = _to_thread
        self._original_run_sync = _to_thread.run_sync

    def install(self) -> None:
        def _fake_socketpair(family: Optional[int] = None, type: Optional[int] = None, proto: int = 0):
            return (
                socket.socket(socket.AF_INET, socket.SOCK_STREAM),
                socket.socket(socket.AF_INET, socket.SOCK_STREAM),
            )

        self._socket_module.socketpair = _fake_socketpair  # type: ignore[assignment]
        try:
            self._asyncio.set_event_loop_policy(self._asyncio.WindowsSelectorEventLoopPolicy())
        except Exception:  # noqa: BLE001
            pass

        def _inline_run_sync(func, *args, **kwargs):  # noqa: ANN001
            async def _runner():
                return func(*args)

            return _runner()

        self._to_thread.run_sync = _inline_run_sync  # type: ignore[assignment]

    def restore(self) -> None:
        self._socket_module.socketpair = self._original_socketpair  # type: ignore[assignment]
        try:
            self._asyncio.set_event_loop_policy(self._original_policy)
        except Exception:  # noqa: BLE001
            pass
        self._to_thread.run_sync = self._original_run_sync  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 轻量响应与测试夹具
# ---------------------------------------------------------------------------
class SimpleResponse:
    """统一响应载体（屏蔽 httpx.Response 与测试断言的差异）。"""

    def __init__(self, status_code: int, text: str, data: Any, headers: dict) -> None:
        self.status_code = status_code
        self.text = text
        self._data = data
        self.headers = headers

    def json(self) -> Any:
        if self._data is None:
            raise ValueError("响应体不是 JSON")
        return self._data


def _pick_free_port() -> int:
    for _ in range(10):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", 0))
                return int(sock.getsockname()[1])
        except OSError:
            time.sleep(0.2)
    return 8123


class Harness:
    """端到端测试夹具：真实服务器或进程内 ASGI 直调。"""

    def __init__(self, mode: str, db_path: Path, base_url: Optional[str] = None, app: Any = None) -> None:
        self.mode = mode
        self.db_path = db_path
        self.base_url = base_url
        self.app = app
        self.user_id = f"pytest-e2e-{uuid.uuid4().hex[:8]}"
        self.headers = {"X-User-Id": self.user_id}

    @property
    def is_live(self) -> bool:
        return self.mode != "asgi"

    async def _client(self):
        import httpx

        if self.is_live:
            return httpx.AsyncClient(base_url=self.base_url, timeout=15.0)
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://e2e.test"
        )

    def request(self, method: str, path: str, **kwargs: Any) -> SimpleResponse:
        """同步执行一次请求（每次独立事件循环，避免跨 loop 复用问题）。"""

        async def _do() -> SimpleResponse:
            client = await self._client()
            try:
                resp = await client.request(method, path, **kwargs)
                try:
                    data = resp.json()
                except ValueError:
                    data = None
                return SimpleResponse(resp.status_code, resp.text, data, dict(resp.headers))
            finally:
                await client.aclose()

        return asyncio.run(_do())

    def stream_sse(self, path: str, payload: dict, headers: dict) -> dict:
        """同步收集一次 SSE 流。"""

        async def _do() -> dict:
            client = await self._client()
            try:
                events: list[tuple[str, str]] = []
                async with client.stream("POST", path, json=payload, headers=headers) as resp:
                    status = resp.status_code
                    content_type = resp.headers.get("content-type", "")
                    event: Optional[str] = None
                    data: Optional[str] = None
                    async for line in resp.aiter_lines():
                        if line == "":
                            if event is not None:
                                events.append((event, data or ""))
                            event, data = None, None
                        elif line.startswith("event:"):
                            event = line[len("event:") :].strip()
                        elif line.startswith("data:"):
                            data = line[len("data:") :].strip()
                    if event is not None:
                        events.append((event, data or ""))
                return {"status": status, "content_type": content_type, "events": events}
            finally:
                await client.aclose()

        return asyncio.run(_do())


def _start_live_server(db_path: Path, port: int, timeout: float = 8.0) -> Optional[subprocess.Popen]:
    """尝试以子进程启动真实服务器；成功返回进程，失败返回 None。"""
    env = dict(os.environ)
    env["ALA_DB_PATH"] = str(db_path)
    env["ALA_HOST"] = "127.0.0.1"
    env["ALA_PORT"] = str(port)
    env["PYTHONUNBUFFERED"] = "1"
    log_path = db_path.parent / "run_py.log"
    handle = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [sys.executable, "run.py"],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return proc
        except OSError:
            time.sleep(0.25)
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    handle.close()
    return None


@pytest.fixture(scope="module")
def e2e_harness():
    """模块级夹具：优先真实服务器，回退进程内 ASGI 直调。"""
    workaround = _SandboxWorkaround()
    workaround.install()

    workdir = Path(tempfile.mkdtemp(prefix="ala_pytest_e2e_"))
    db_path = workdir / "e2e.db"
    os.environ["ALA_DB_PATH"] = str(db_path)

    proc: Optional[subprocess.Popen] = None
    harness: Optional[Harness] = None
    try:
        port = _pick_free_port()
        proc = _start_live_server(db_path, port)
        if proc is not None:
            print(f"\n[T14] 真实服务器模式：http://127.0.0.1:{port}")
            harness = Harness("live-subprocess", db_path, base_url=f"http://127.0.0.1:{port}")
        else:
            print("\n[T14] 回退进程内 ASGI 直调模式（沙箱限制 127.0.0.1）")
            os.environ["ALA_DB_PATH"] = str(db_path)
            from backend.app.main import create_app

            app = create_app()
            harness = Harness("asgi", db_path, app=app)
        yield harness
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(workdir, ignore_errors=True)
        workaround.restore()


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------
def test_home_and_static_pages(e2e_harness: Harness) -> None:
    """GET / 与四个前端页面均返回 200（AC-R8-2 / R7）。"""
    home = e2e_harness.request("GET", "/")
    assert home.status_code == 200, f"GET / -> {home.status_code}"
    assert "AI 学习助手" in home.text, "首页未命中 index.html"

    for page in ("index.html", "knowledge.html", "chat.html", "profile.html"):
        resp = e2e_harness.request("GET", f"/static/{page}")
        assert resp.status_code == 200, f"/static/{page} -> {resp.status_code}"
        assert resp.text.strip(), f"/static/{page} 内容为空"

    health = e2e_harness.request("GET", "/health")
    assert health.status_code == 200
    body = health.json()
    assert body["content_loaded"] is True
    assert body["chapters"] == 2


def test_knowledge_chapters_and_kp(e2e_harness: Harness) -> None:
    """章节 2 章、总知识点 ≥16；知识点详情/导图/配图结构完整（C1）。"""
    chapters = e2e_harness.request("GET", "/api/knowledge/chapters")
    assert chapters.status_code == 200
    data = chapters.json()
    assert len(data) == 2, f"章节数应为 2，实际 {len(data)}"
    total = sum(ch["kp_count"] for ch in data)
    assert total >= 16, f"总知识点应 ≥16，实际 {total}"

    detail = e2e_harness.request("GET", f"/api/knowledge/kps/{PRIMARY_KP}")
    assert detail.status_code == 200
    kp = detail.json()
    assert len(kp["content_blocks"]) >= 1
    assert len(kp["images"]) >= 1
    assert len(kp["mindmap"]["children"]) >= 1
    assert len(kp["quiz"]) >= 2
    assert any(q["type"] != "single_choice" for q in kp["quiz"])

    mindmap = e2e_harness.request("GET", f"/api/knowledge/kps/{PRIMARY_KP}/mindmap")
    assert mindmap.status_code == 200
    node = mindmap.json()
    assert node["id"] and node["label"] and len(node["children"]) >= 1

    images = e2e_harness.request("GET", f"/api/knowledge/kps/{PRIMARY_KP}/images")
    assert images.status_code == 200
    img = images.json()
    assert len(img) >= 1
    assert {"src", "caption", "alt"}.issubset(img[0].keys())
    assert img[0]["src"].startswith("/static/img/biology/")

    # 对抗：未知 kp / 章节 -> 404 结构化错误体
    unknown_kp = e2e_harness.request("GET", "/api/knowledge/kps/no-such-kp")
    assert unknown_kp.status_code == 404 and unknown_kp.json()["code"] == "KP_NOT_FOUND"
    unknown_ch = e2e_harness.request("GET", "/api/knowledge/chapters/no-such")
    assert unknown_ch.status_code == 404 and unknown_ch.json()["code"] == "CHAPTER_NOT_FOUND"


def test_profile_questionnaire_strategy(e2e_harness: Harness) -> None:
    """默认 profile → 问卷 → 策略 主链路（C5 / AC-R5-1/2/4）。"""
    headers = e2e_harness.headers

    profile = e2e_harness.request("GET", "/api/profile", headers=headers)
    assert profile.status_code == 200
    assert profile.json()["daily_minutes"] == 30
    assert profile.json()["questionnaire_completed"] is False

    submitted = e2e_harness.request(
        "POST", "/api/profile/questionnaire", json={"answers": {"q6": ["v"]}}, headers=headers
    )
    assert submitted.status_code == 200
    assessment = submitted.json()
    assert "learning_style" in assessment
    assert set(assessment["style_scores"].keys()) >= {"visual", "auditory", "reading", "kinesthetic"}
    assert "feynman" in assessment["recommended_modes"]

    strategy = e2e_harness.request("GET", "/api/profile/strategy", headers=headers)
    assert strategy.status_code == 200
    body = strategy.json()
    for field in ("recommended_modes", "session_length_min", "review_intensity_multiplier", "rationale"):
        assert field in body
    assert len(body["rationale"]) >= 2


def test_review_add_due_and_quiz_writeback(e2e_harness: Harness) -> None:
    """加入复习 → 今日清单 → 作答回写 review_state 主链路（C2+C3）。"""
    import sqlite3

    headers = e2e_harness.headers

    added = e2e_harness.request("POST", "/api/review/items", json={"kp_id": PRIMARY_KP}, headers=headers)
    assert added.status_code == 201
    item = added.json()
    assert item["status"] == "active"

    # 幂等
    again = e2e_harness.request("POST", "/api/review/items", json={"kp_id": PRIMARY_KP}, headers=headers)
    assert again.status_code == 201 and again.json()["due_at"] == item["due_at"]

    # 契约：新项首次间隔≥1 天，未到期前不在今日清单
    before = e2e_harness.request("GET", "/api/review/today", headers=headers)
    assert PRIMARY_KP not in [it["kp_id"] for it in before.json()["items"]]

    # 模拟跨日到期（独立临时库）
    conn = sqlite3.connect(str(e2e_harness.db_path))
    try:
        past_due = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "UPDATE review_state SET due_at = ? WHERE kp_id = ? AND user_id = ?",
            (past_due, PRIMARY_KP, e2e_harness.user_id),
        )
        conn.commit()
    finally:
        conn.close()

    today = e2e_harness.request("GET", "/api/review/today", headers=headers)
    assert today.status_code == 200
    plan = today.json()
    ids = [it["kp_id"] for it in plan["items"]]
    assert PRIMARY_KP in ids, f"到期项未进入今日清单：{ids}"
    included = next(it for it in plan["items"] if it["kp_id"] == PRIMARY_KP)
    for field in ("chapter_id", "title", "due_at", "overdue_days", "est_minutes",
                  "level", "interval_days", "mastery", "priority_score"):
        assert field in included

    # 对抗：预算 0 至少保留 1 项（AC-R2-5）
    zero_budget = e2e_harness.request("GET", "/api/review/today", params={"budget_minutes": 0}, headers=headers)
    assert zero_budget.status_code == 200
    assert len(zero_budget.json()["items"]) >= 1

    # 作答（正确）→ review_state 更新
    quiz = e2e_harness.request("GET", f"/api/quiz/{PRIMARY_KP}", headers=headers)
    assert quiz.status_code == 200
    items = quiz.json()
    target = next((q for q in items if q["type"] in ("single_choice", "true_false")), items[0])
    before_item = next(
        it for it in e2e_harness.request("GET", "/api/review/items", headers=headers).json()
        if it["kp_id"] == PRIMARY_KP
    )
    attempt = e2e_harness.request(
        "POST",
        f"/api/quiz/{PRIMARY_KP}/attempt",
        json={"qid": target["qid"], "user_answer": target["answer"]},
        headers=headers,
    )
    assert attempt.status_code == 200
    body = attempt.json()
    assert body["grade"]["is_correct"] is True and body["grade"]["score"] == 100
    state = body["review_state"]
    assert state is not None
    assert state["review_count"] == before_item["review_count"] + 1
    assert state["level"] == before_item["level"] + 1

    # 对抗：错误作答 / 未知 qid / 未复习 kp
    wrong = e2e_harness.request(
        "POST",
        f"/api/quiz/{PRIMARY_KP}/attempt",
        json={"qid": target["qid"], "user_answer": "__WRONG__"},
        headers=headers,
    )
    assert wrong.status_code == 200
    assert wrong.json()["grade"]["score"] == 0 and wrong.json()["grade"]["is_correct"] is False

    bad_qid = e2e_harness.request(
        "POST", f"/api/quiz/{PRIMARY_KP}/attempt", json={"qid": "nope", "user_answer": "x"}, headers=headers
    )
    assert bad_qid.status_code == 404 and bad_qid.json()["code"] == "QUIZ_ITEM_NOT_FOUND"

    other_quiz = e2e_harness.request("GET", f"/api/quiz/{SECONDARY_KP}", headers=headers).json()
    other = next((q for q in other_quiz if q["type"] in ("single_choice", "true_false")), other_quiz[0])
    not_reviewed = e2e_harness.request(
        "POST",
        f"/api/quiz/{SECONDARY_KP}/attempt",
        json={"qid": other["qid"], "user_answer": other["answer"]},
        headers=headers,
    )
    assert not_reviewed.status_code == 200
    assert not_reviewed.json()["review_state"] is None


def test_review_reminder(e2e_harness: Harness) -> None:
    """提醒读取/更新语义 + 非法时间 422（C2）。"""
    headers = e2e_harness.headers
    got = e2e_harness.request("GET", "/api/review/reminder", headers=headers)
    assert got.status_code == 200
    assert set(got.json().keys()) == {"enabled", "time"}

    updated = e2e_harness.request(
        "PUT", "/api/review/reminder", json={"enabled": False, "time": "07:30"}, headers=headers
    )
    assert updated.json() == {"enabled": False, "time": "07:30"}

    keep = e2e_harness.request("PUT", "/api/review/reminder", json={"enabled": True}, headers=headers)
    assert keep.json()["time"] == "07:30", "enabled=false 时应保留 time"

    invalid = e2e_harness.request(
        "PUT", "/api/review/reminder", json={"enabled": True, "time": "25:00"}, headers=headers
    )
    assert invalid.status_code == 422


def test_chat_sse_stream(e2e_harness: Harness) -> None:
    """聊天会话 → SSE 流（provider=local，帧序列 meta→delta*→done）→ 落库（C4）。"""
    headers = e2e_harness.headers

    session = e2e_harness.request("POST", "/api/chat/sessions", json={"mode": "teacher"}, headers=headers)
    assert session.status_code == 201
    session_id = session.json()["session_id"]

    streamed = e2e_harness.stream_sse(
        "/api/chat/stream",
        {"session_id": session_id, "content": "请讲讲细胞膜", "mode": "teacher"},
        headers,
    )
    assert streamed["status"] == 200
    assert "text/event-stream" in streamed["content_type"]
    names = [name for name, _ in streamed["events"]]
    assert names[0] == "meta" and names[-1] == "done"
    assert all(n in ("meta", "delta", "done") for n in names)
    deltas = [json_loads(data)["text"] for name, data in streamed["events"] if name == "delta"]
    assert len(deltas) >= 1
    meta = json_loads(streamed["events"][0][1])
    done = json_loads(streamed["events"][-1][1])
    assert meta["provider"] == "local", "无凭证时应回退 local"
    assert done["provider"] == "local"
    assert done["content"] == "".join(deltas)

    messages = e2e_harness.request("GET", f"/api/chat/sessions/{session_id}/messages", headers=headers)
    assert messages.status_code == 200
    roles = [m["role"] for m in messages.json()]
    assert "user" in roles and "assistant" in roles
    assert next(m for m in messages.json() if m["role"] == "assistant")["provider"] == "local"

    # 对抗：非法角色 / 未知会话
    bad_mode = e2e_harness.request("POST", "/api/chat/sessions", json={"mode": "wizard"}, headers=headers)
    assert bad_mode.status_code == 422 and bad_mode.json()["code"] == "INVALID_MODE"
    missing = e2e_harness.request(
        "POST", "/api/chat/stream", json={"session_id": "no-such", "content": "hi"}, headers=headers
    )
    assert missing.status_code == 404 and missing.json()["code"] == "SESSION_NOT_FOUND"


def test_missing_user_id_returns_400(e2e_harness: Harness) -> None:
    """缺 X-User-Id -> 400 MISSING_USER_ID（T1 共享行为）。"""
    assert e2e_harness.request("GET", "/api/review/items").status_code == 400
    assert e2e_harness.request("GET", "/api/review/items").json()["code"] == "MISSING_USER_ID"
    assert e2e_harness.request("GET", "/api/profile").status_code == 400
    assert e2e_harness.request("POST", "/api/chat/sessions", json={"mode": "teacher"}).status_code == 400


def json_loads(text: str) -> Any:
    """SSE data 帧 JSON 解析小工具。"""
    import json as _json

    return _json.loads(text)