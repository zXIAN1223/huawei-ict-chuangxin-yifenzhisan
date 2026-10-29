"""C4-CHAT@1 契约测试：会话管理、角色校验与 SSE 流式帧序列（plan 2.2.4 / 3.3 T13）。

覆盖：
- ``POST /sessions`` 创建、非法角色 422、未知 kp 404；
- 会话列表、消息读取、删除（204）与 404；
- ``POST /stream`` 的 SSE 帧序列 ``meta → delta* → done``、``Content-Type``、
  ``done`` 前落库（AC-R4-1）；
- AI 适配层 provider 判定（无凭证 → local，含密钥 → llm；值域见
  ``C-LLM-PROVIDER@1``，plan 2.2.3）。

说明：本模块的 autouse 夹具 ``_isolate_llm_environment`` 会清空 ``LLM_*`` /
``PANGU_*`` 环境变量并把 ``.env`` 指向不存在的文件，故聊天始终走本地模拟，
``provider`` 恒为 ``local``，符合 AC-R6-2；含密钥的 provider 判定由
``test_adapter_provider_llm_with_credentials`` 在进程内直接断言。
"""

from __future__ import annotations

import json

import pytest

from backend.app import config as config_module
from backend.app.services.ai_adapter import AIConfig, AIAdapter

CELL_KP = "cell-membrane"

#: 影响 AI 适配层 provider 判定的环境变量（见 ``config.get_settings``）。
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


@pytest.fixture(autouse=True)
def _isolate_llm_environment(monkeypatch, tmp_path):
    """隔离外部 LLM 配置，保证「无凭证 → local」断言不受运行机环境影响。"""
    monkeypatch.setattr(config_module, "ENV_FILE", tmp_path / "absent.env")
    for key in _LLM_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    # 兜底断言：隔离后确实没有有效凭证（若失败说明环境注入未被清空）。
    assert config_module.get_settings().llm_enabled is False


# ---------------------------------------------------------------------------
# SSE 解析辅助
# ---------------------------------------------------------------------------


def parse_sse_frames(text: str) -> list[tuple[str, object]]:
    """解析 SSE 文本为 ``[(event, data), ...]``（data 为已解析 JSON）。"""
    frames: list[tuple[str, object]] = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event: str | None = None
        data_lines: list[str] = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())
        if event is None:
            continue
        raw = "\n".join(data_lines)
        try:
            data: object = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            data = raw
        frames.append((event, data))
    return frames


# ---------------------------------------------------------------------------
# AI 适配层 provider 判定（AC-R6-1/2）
# ---------------------------------------------------------------------------


def test_adapter_provider_local_without_credentials() -> None:
    """无凭证 → provider == local。"""
    adapter = AIAdapter(AIConfig(api_url=None, api_key=None))
    assert adapter.provider == "local"


def test_adapter_provider_llm_with_credentials() -> None:
    """地址与密钥均非空 → provider == "llm"（C-LLM-PROVIDER@1 值域扩展）。"""
    adapter = AIAdapter(AIConfig(api_url="https://example.invalid/api", api_key="secret"))
    assert adapter.provider == "llm"
    # 远程提供方必须经 resolve_endpoint() 归一化（C-LLM-ADAPTER@1，plan 2.2.2）。
    assert adapter.config.resolve_endpoint() == (
        "https://example.invalid/api/chat/completions"
    )
    # 密钥不得出现在对象表示中（R5.4）。
    assert "secret" not in repr(adapter.config)


# ---------------------------------------------------------------------------
# API 测试
# ---------------------------------------------------------------------------


def test_create_session_and_invalid_mode(client, auth_headers) -> None:
    """创建会话返回 201 ChatSessionView；非法角色 → 422 INVALID_MODE。"""
    response = client.post(
        "/api/chat/sessions",
        json={"mode": "teacher", "kp_id": CELL_KP},
        headers=auth_headers,
    )
    assert response.status_code == 201
    session = response.json()
    assert session["mode"] == "teacher"
    assert session["kp_id"] == CELL_KP
    assert session["session_id"]
    assert session["title"]

    invalid = client.post(
        "/api/chat/sessions", json={"mode": "narrator"}, headers=auth_headers
    )
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "INVALID_MODE"


def test_create_session_unknown_kp_returns_404(client, auth_headers) -> None:
    """kp_id 不存在 → 404 KP_NOT_FOUND。"""
    response = client.post(
        "/api/chat/sessions", json={"mode": "student", "kp_id": "no-such-kp"}, headers=auth_headers
    )
    assert response.status_code == 404
    assert response.json()["code"] == "KP_NOT_FOUND"


def test_list_sessions_and_delete(client, auth_headers) -> None:
    """会话列表可见新建会话；删除后 204 且消息端点 404。"""
    created = client.post(
        "/api/chat/sessions", json={"mode": "teacher"}, headers=auth_headers
    ).json()
    session_id = created["session_id"]

    listed = client.get("/api/chat/sessions", headers=auth_headers)
    assert listed.status_code == 200
    assert session_id in [item["session_id"] for item in listed.json()]

    deleted = client.delete(f"/api/chat/sessions/{session_id}", headers=auth_headers)
    assert deleted.status_code == 204

    missing = client.get(f"/api/chat/sessions/{session_id}/messages", headers=auth_headers)
    assert missing.status_code == 404
    assert missing.json()["code"] == "SESSION_NOT_FOUND"


def test_messages_of_unknown_session_returns_404(client, auth_headers) -> None:
    """未知会话的消息端点 → 404 SESSION_NOT_FOUND。"""
    response = client.get(
        "/api/chat/sessions/does-not-exist/messages", headers=auth_headers
    )
    assert response.status_code == 404
    assert response.json()["code"] == "SESSION_NOT_FOUND"


def test_stream_frames_and_persistence(client, auth_headers) -> None:
    """SSE 帧序列 meta → delta* → done，且 done 前完成落库（AC-R4-1）。"""
    session = client.post(
        "/api/chat/sessions",
        json={"mode": "teacher", "kp_id": CELL_KP},
        headers=auth_headers,
    ).json()
    session_id = session["session_id"]

    response = client.post(
        "/api/chat/stream",
        json={"session_id": session_id, "content": "请讲解细胞膜"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    frames = parse_sse_frames(response.text)
    events = [event for event, _ in frames]
    # 首帧必为 meta，末帧必为 done
    assert events[0] == "meta"
    assert events[-1] == "done"
    # 中间帧只能是 delta（不得出现 error）
    assert "error" not in events
    assert all(event in ("meta", "delta", "done") for event in events)

    meta = frames[0][1]
    assert isinstance(meta, dict)
    assert meta["provider"] == "local"
    assert meta["mode"] == "teacher"
    assert meta["session_id"] == session_id
    message_id = meta["message_id"]

    done = frames[-1][1]
    assert isinstance(done, dict)
    assert done["message_id"] == message_id
    assert done["provider"] == "local"
    assert isinstance(done["content"], str) and done["content"]

    # delta 拼接应等于 done.content
    deltas = [data["text"] for event, data in frames if event == "delta"]
    assert deltas, "本地模拟应产生至少一个 delta 帧"
    assert "".join(deltas) == done["content"]

    # 落库：user + assistant 两条消息（assistant 含 provider）
    messages = client.get(
        f"/api/chat/sessions/{session_id}/messages", headers=auth_headers
    ).json()
    roles = [message["role"] for message in messages]
    assert "user" in roles and "assistant" in roles
    user_message = next(m for m in messages if m["role"] == "user")
    assistant_message = next(m for m in messages if m["role"] == "assistant")
    assert user_message["content"] == "请讲解细胞膜"
    assert assistant_message["content"] == done["content"]
    assert assistant_message["provider"] == "local"


def test_stream_unknown_session_returns_404(client, auth_headers) -> None:
    """未知会话流式请求 → 404 SESSION_NOT_FOUND。"""
    response = client.post(
        "/api/chat/stream",
        json={"session_id": "no-such-session", "content": "你好"},
        headers=auth_headers,
    )
    assert response.status_code == 404
    assert response.json()["code"] == "SESSION_NOT_FOUND"


def test_stream_invalid_mode_returns_422(client, auth_headers) -> None:
    """流式请求显式传入非法角色 → 422 INVALID_MODE。"""
    session = client.post(
        "/api/chat/sessions", json={"mode": "teacher"}, headers=auth_headers
    ).json()
    response = client.post(
        "/api/chat/stream",
        json={"session_id": session["session_id"], "content": "你好", "mode": "narrator"},
        headers=auth_headers,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_MODE"


def test_stream_feynman_role_behavior(client, auth_headers) -> None:
    """feynman 角色流式回复且成功落库（AC-R4-2）。"""
    session = client.post(
        "/api/chat/sessions",
        json={"mode": "feynman", "kp_id": CELL_KP},
        headers=auth_headers,
    ).json()

    response = client.post(
        "/api/chat/stream",
        json={"session_id": session["session_id"], "content": "我来讲细胞膜是由磷脂双分子层构成的"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    frames = parse_sse_frames(response.text)
    assert frames[0][0] == "meta"
    assert frames[-1][0] == "done"
    assert frames[0][1]["mode"] == "feynman"


def test_missing_user_id_returns_400(client) -> None:
    """缺 X-User-Id → 400 MISSING_USER_ID。"""
    response = client.get("/api/chat/sessions")
    assert response.status_code == 400
    assert response.json()["code"] == "MISSING_USER_ID"