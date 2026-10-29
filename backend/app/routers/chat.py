"""聊天 API（契约 C4-CHAT@1，plan 2.2.4；增量契约 C-CHAT-PARAMS@1，plan 2.2.6）。

统一前缀 ``/api/chat``，提供会话管理、消息读取与 **SSE 流式对话**：

==============================  ============================================
方法 / 路径                     语义
==============================  ============================================
POST /sessions                  创建会话（201，ChatSessionView）
GET  /sessions                  会话列表（?limit=30）
GET  /sessions/{id}/messages    消息列表（?limit=100）
DELETE /sessions/{id}           删除会话及其消息（204）
POST /stream                    SSE 流式对话（meta/delta/done/error）
==============================  ============================================

SSE 帧格式严格遵循 ``event: <name>\\ndata: <json>\\n\\n``，``Content-Type`` 为
``text/event-stream; charset=utf-8``。服务端在发送 ``done`` 前，将用户消息与助手
完整消息（含 ``provider``）落库。

错误体统一为 ``{"code","message","detail"?}``：404 ``SESSION_NOT_FOUND``、
404 ``KP_NOT_FOUND``、422 ``INVALID_MODE``。

流式参数（契约 C-CHAT-PARAMS@1，plan 2.2.6 / 2.3.2）：``_sse_generator`` 每次请求
读取一次 :func:`build_ai_config`，把 ``temperature`` / ``max_tokens`` **显式**传入
``AIAdapter.stream``，使 ``LLM_TEMPERATURE`` / ``LLM_MAX_TOKENS`` 生效；``timeout``
由 ``AIConfig`` 携带。``meta.provider`` 表示「本次尝试的提供方」，``done.provider``
表示「本次实际提供方」（值域 ``llm``/``pangu``/``local``，见 C-LLM-PROVIDER@1）。

生成异常时发出 ``error`` 帧：``code`` 恒为 ``STREAM_FAILED``（向后兼容），``message``
为面向用户的中文文案，``detail.reason`` 可选地标注原因类别
（``timeout``/``network``/``remote``/``generation``）；**不新增**前端未处理的必需 code。

角色编排：``teacher`` 讲解 / ``student`` 引导提问 / ``feynman`` 费曼点评；
``feynman`` 模式在成功回复后触发 ``feynman_talk`` 行为日志（经 T8
``learning_strategy.record_behavior``，未就绪时静默跳过）。

安全：AI 密钥仅在 :class:`~backend.app.services.ai_adapter.AIConfig` 内流转，
本模块不读取、不打印、不回显。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.app import db as db_module
from backend.app.data.content_loader import ContentRepository
from backend.app.deps import ApiError, get_content_repo, get_db, get_user_id
from backend.app.models import (
    ChatMessageView,
    ChatRole,
    ChatSessionView,
    KnowledgePoint,
)
from backend.app.services.ai_adapter import AIConfig, AIAdapter, ChatTurn, build_ai_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])

#: 合法角色集合（与 plan 2.2.4 一致）。
CHAT_ROLES: tuple[str, ...] = ("teacher", "student", "feynman")

#: 会话/消息列表默认与上限（契约默认 limit=30 / 100）。
DEFAULT_SESSION_LIMIT = 30
MAX_SESSION_LIMIT = 200
DEFAULT_MESSAGE_LIMIT = 100
MAX_MESSAGE_LIMIT = 500

#: 送入 AI 的历史消息条数上限（控制上下文长度）。
HISTORY_LIMIT = 20

#: 流式失败时 ``error`` 帧的 ``detail.reason`` 值域（可选字段，非必需 code）。
STREAM_ERROR_TIMEOUT = "timeout"
STREAM_ERROR_NETWORK = "network"
STREAM_ERROR_REMOTE = "remote"
STREAM_ERROR_GENERATION = "generation"

#: ``reason`` → ``error`` 帧的面向用户文案（保持 ``code="STREAM_FAILED"`` 不变）。
_STREAM_ERROR_MESSAGE: dict[str, str] = {
    STREAM_ERROR_TIMEOUT: "生成回复超时，请稍后重试。",
    STREAM_ERROR_NETWORK: "无法连接模型服务，请检查网络或接口配置后重试。",
    STREAM_ERROR_REMOTE: "模型服务返回异常，请稍后重试。",
    STREAM_ERROR_GENERATION: "生成回复时发生错误，请重试。",
}

#: 文案兜底（``reason`` 未覆盖时使用，保证 ``message`` 始终非空且可读）。
_STREAM_ERROR_FALLBACK_MESSAGE = "生成回复时发生错误，请重试。"

#: 角色 → 会话默认标题后缀。
_ROLE_TITLE = {
    "teacher": "老师讲解",
    "student": "学生提问",
    "feynman": "费曼讲解",
}

#: 学习风格 → 推荐学习方式（plan 2.2.5 规则表；所有风格恒追加 feynman）。
_STYLE_MODES: dict[str, list[str]] = {
    "visual": ["flashcard_visual", "mindmap"],
    "auditory": ["chat_teacher", "listen_summary"],
    "reading": ["read_summary", "note_taking"],
    "kinesthetic": ["feynman", "recall_practice"],
    "mixed": ["mindmap", "recall_practice", "feynman"],
}

_STYLE_LABEL = {
    "visual": "视觉型",
    "auditory": "听觉型",
    "reading": "读写型",
    "kinesthetic": "动觉型",
    "mixed": "混合型",
}

_STYLE_TIP = {
    "visual": "多给出结构化的、图示化的描述与类比。",
    "auditory": "语气口语化，像面对面讲解一样娓娓道来。",
    "reading": "条理清晰、分点罗列，便于阅读与记录。",
    "kinesthetic": "多用动手操作与生活实例来解释。",
    "mixed": "兼顾讲解、举例与提问，保持节奏多样。",
}


# --------------------------------------------------------------------------- #
# 请求模型
# --------------------------------------------------------------------------- #
class SessionCreateRequest(BaseModel):
    """``POST /api/chat/sessions`` 请求体。"""

    kp_id: Optional[str] = None
    mode: str = Field(description="teacher | student | feynman")
    title: Optional[str] = None


class StreamRequest(BaseModel):
    """``POST /api/chat/stream`` 请求体。"""

    session_id: str
    content: str
    mode: Optional[str] = None
    kp_id: Optional[str] = None


# --------------------------------------------------------------------------- #
# 错误与校验辅助
# --------------------------------------------------------------------------- #
def _session_not_found(session_id: str) -> ApiError:
    return ApiError(404, "SESSION_NOT_FOUND", f"会话不存在：{session_id}", {"session_id": session_id})


def _kp_not_found(kp_id: str) -> ApiError:
    return ApiError(404, "KP_NOT_FOUND", f"知识点不存在：{kp_id}", {"kp_id": kp_id})


def _validate_mode(mode: str) -> ChatRole:
    """校验角色取值；非法时抛 422 INVALID_MODE。"""
    value = (mode or "").strip()
    if value not in CHAT_ROLES:
        raise ApiError(
            422,
            "INVALID_MODE",
            f"非法的对话角色：{mode!r}（应为 teacher/student/feynman 之一）",
            {"allowed": list(CHAT_ROLES)},
        )
    return value  # type: ignore[return-value]


def _default_title(mode: ChatRole, kp: Optional[KnowledgePoint]) -> str:
    suffix = _ROLE_TITLE.get(mode, "对话")
    title = getattr(kp, "title", None) if kp is not None else None
    return f"{title} · {suffix}" if title else f"新对话 · {suffix}"


# --------------------------------------------------------------------------- #
# 行 → 视图
# --------------------------------------------------------------------------- #
def _row_to_session_view(row: sqlite3.Row) -> ChatSessionView:
    return ChatSessionView(
        session_id=row["session_id"],
        user_id=row["user_id"],
        kp_id=row["kp_id"],
        mode=row["mode"],
        title=row["title"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_message_view(row: sqlite3.Row) -> ChatMessageView:
    return ChatMessageView(
        id=row["id"],
        session_id=row["session_id"],
        role=row["role"],
        content=row["content"],
        provider=row["provider"],
        created_at=row["created_at"],
    )


# --------------------------------------------------------------------------- #
# 数据库辅助
# --------------------------------------------------------------------------- #
def _fetch_session(
    conn: sqlite3.Connection, session_id: str, user_id: str
) -> Optional[sqlite3.Row]:
    """按 `session_id` + `user_id` 读取会话（保证用户隔离）。"""
    return conn.execute(
        "SELECT session_id, user_id, kp_id, mode, title, created_at, updated_at "
        "FROM chat_session WHERE session_id = ? AND user_id = ?",
        (session_id, user_id),
    ).fetchone()


def _load_history(conn: sqlite3.Connection, session_id: str, limit: int = HISTORY_LIMIT) -> list[ChatTurn]:
    """读取会话最近的历史对话（user/assistant），按时间升序返回。"""
    rows = conn.execute(
        "SELECT role, content FROM chat_message "
        "WHERE session_id = ? AND role IN ('user', 'assistant') AND content != '' "
        "ORDER BY id DESC LIMIT ?",
        (session_id, int(limit)),
    ).fetchall()
    return [ChatTurn(role=row["role"], content=row["content"]) for row in reversed(rows)]


def _build_turns(history: list[ChatTurn], content: str) -> list[ChatTurn]:
    """构造本次请求的 ``turns``：历史 + 本轮用户消息，按时间升序（R2.1）。

    :func:`_load_history` 已按主键升序（即时间升序）返回最近 ``HISTORY_LIMIT`` 条
    非空 ``user``/``assistant`` 消息，因此此处只需追加本轮用户消息，模型看到的
    对话顺序即与真实发生顺序一致（「历史 → 本轮提问」）。

    :param history: 历史对话（可为空列表）。
    :param content: 本轮用户消息内容。
    """
    turns = list(history or [])
    turns.append(ChatTurn(role="user", content=content))
    return turns


def _insert_message(
    conn: sqlite3.Connection,
    session_id: str,
    user_id: str,
    role: str,
    content: str,
    provider: Optional[str],
) -> int:
    """插入一条消息，返回自增主键 id。"""
    now = db_module.to_iso(db_module.utc_now())
    with db_module.transaction(conn):
        cursor = conn.execute(
            "INSERT INTO chat_message (session_id, user_id, role, content, provider, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, user_id, role, content, provider, now),
        )
        return int(cursor.lastrowid)


def _update_message(
    conn: sqlite3.Connection, message_id: int, content: str, provider: str
) -> None:
    """以完整内容与最终提供方回填助手消息。"""
    with db_module.transaction(conn):
        conn.execute(
            "UPDATE chat_message SET content = ?, provider = ? WHERE id = ?",
            (content, provider, int(message_id)),
        )


def _delete_message(conn: sqlite3.Connection, message_id: int) -> None:
    """删除消息（用于流异常时清理助手占位行）。"""
    with db_module.transaction(conn):
        conn.execute("DELETE FROM chat_message WHERE id = ?", (int(message_id),))


def _touch_session(conn: sqlite3.Connection, session_id: str) -> None:
    """更新会话的 updated_at。"""
    with db_module.transaction(conn):
        conn.execute(
            "UPDATE chat_session SET updated_at = ? WHERE session_id = ?",
            (db_module.to_iso(db_module.utc_now()), session_id),
        )


# --------------------------------------------------------------------------- #
# 画像摘要与系统提示
# --------------------------------------------------------------------------- #
def _load_profile_summary(conn: sqlite3.Connection, user_id: str) -> Optional[dict]:
    """读取用户画像摘要（`learning_style` / `recommended_modes`）。

    直接读取 `user_profile` 表（T1 建表），因此不依赖 T8 是否已交付；无画像时返回 None。
    """
    try:
        row = conn.execute(
            "SELECT learning_style FROM user_profile WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    except sqlite3.Error:  # 表缺失等异常不应阻断聊天
        logger.warning("读取用户画像失败，忽略 profile_summary", exc_info=True)
        return None
    if row is None:
        return None

    style = row["learning_style"] or "mixed"
    modes = list(_STYLE_MODES.get(style, _STYLE_MODES["mixed"]))
    if "feynman" not in modes:  # 规则：所有风格恒追加 feynman
        modes.append("feynman")
    return {"learning_style": style, "recommended_modes": modes}


def _kp_context(kp: Optional[KnowledgePoint]) -> str:
    """将知识点收敛为系统提示中的上下文片段（只取已有内容）。"""
    if kp is None:
        return "（本次对话未关联具体知识点，请以通用对话方式协助学习者。）"
    lines = [
        "【当前知识点上下文】",
        f"标题：{getattr(kp, 'title', '')}",
        f"概述：{getattr(kp, 'summary', '')}",
    ]
    points: list[str] = []
    for block in getattr(kp, "content_blocks", None) or []:
        btype = getattr(block, "type", None)
        if btype == "paragraph":
            points.append(str(getattr(block, "text", "")).strip())
        elif btype == "list":
            points.extend(str(item).strip() for item in (getattr(block, "items", None) or []))
        elif btype == "callout":
            points.append(str(getattr(block, "text", "")).strip())
    points = [p for p in points if p][:6]
    if points:
        lines.append("要点：")
        lines.extend(f"- {p}" for p in points)
    return "\n".join(lines)


def _profile_hint(profile_summary: Optional[dict]) -> str:
    """将画像摘要转换为对 AI 的语气/风格提示。"""
    if not profile_summary:
        return ""
    style = str(profile_summary.get("learning_style") or "mixed")
    label = _STYLE_LABEL.get(style, "混合型")
    tip = _STYLE_TIP.get(style, "")
    modes = profile_summary.get("recommended_modes") or []
    text = f"【学习者画像】主导学习风格：{label}。"
    if tip:
        text += f"请{tip}"
    if modes:
        text += f"推荐学习方式：{'、'.join(str(m) for m in modes)}。"
    return text


def build_system_prompt(
    mode: ChatRole,
    kp: Optional[KnowledgePoint],
    profile_summary: Optional[dict],
) -> str:
    """构建系统提示（plan 2.2.4）。

    :param mode: 对话角色 teacher / student / feynman。
    :param kp: 关联知识点（可为 None）。
    :param profile_summary: C5 提供的画像摘要（可为 None）。
    """
    parts: list[str] = []

    if mode == "teacher":
        parts.append(
            "你是一位耐心、善于打比方的高中生物老师。请把知识讲清楚、讲透，"
            "按要点分层次展开，必要时用生活类比帮助学生理解，语气亲切。"
        )
    elif mode == "student":
        parts.append(
            "你现在扮演一名好学、爱刨根问底的高中学生。你要通过一步步提问，"
            "引导对方把知识讲清楚；问题要具体、循序渐进，可适当追问「为什么」「能举个例子吗」。"
        )
    elif mode == "feynman":
        parts.append(
            "你扮演费曼学习法的陪练。学习者会用自己的话讲解知识点，"
            "你要给出「先肯定、再指出遗漏或偏差、最后追问」的点评，帮助其查漏补缺。"
        )
    else:  # 理论上不可达（mode 已校验）
        parts.append("你是一位乐于助人的学习助手。")

    parts.append(_kp_context(kp))

    hint = _profile_hint(profile_summary)
    if hint:
        parts.append(hint)

    parts.append(
        "通用要求：使用简体中文；只依据给定的知识点内容作答，"
        "不得编造知识点之外的具体事实；若信息不足，请如实说明并引导学习者复习相关内容。"
    )
    return "\n\n".join(part for part in parts if part)


# --------------------------------------------------------------------------- #
# 行为日志（经 T8；未就绪时静默跳过）
# --------------------------------------------------------------------------- #
def _record_behavior(
    conn: sqlite3.Connection,
    user_id: str,
    action_type: str,
    kp_id: Optional[str] = None,
    payload: Optional[dict] = None,
) -> None:
    """调用 T8 的 `learning_strategy.record_behavior` 写行为日志。

    该函数由 T8 提供；若模块尚未交付或调用失败，仅记录 warning，不影响聊天主流程。
    """
    try:
        from backend.app.services.learning_strategy import record_behavior
    except Exception:  # noqa: BLE001 - T8 未就绪
        logger.debug("learning_strategy 未就绪，跳过行为日志：%s", action_type)
        return
    try:
        record_behavior(conn, user_id, action_type, kp_id, payload or {})
    except Exception:  # noqa: BLE001 - 行为日志失败不应影响主流程
        logger.warning("写入行为日志失败：%s", action_type, exc_info=True)


# --------------------------------------------------------------------------- #
# SSE 帧与生成器
# --------------------------------------------------------------------------- #
def _sse_frame(event: str, data: dict) -> str:
    """构造 SSE 帧：``event: <name>\\ndata: <json>\\n\\n``。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _build_ai_config_safe() -> AIConfig:
    """读取运行时 AI 配置；读取失败时退化为「未配置」（走本地模拟）。

    契约 C-CHAT-PARAMS@1 要求 ``_sse_generator`` 内取 :func:`build_ai_config`。
    配置读取本身（环境变量/``.env``）理论上不应失败，但一旦失败，整条 SSE 流会
    在首帧前中断；此处兜底为无凭证的 :class:`AIConfig`（``provider == "local"``），
    保证「对话始终可用」（R1.3）。
    """
    try:
        return build_ai_config()
    except Exception:  # noqa: BLE001 - 配置读取失败不应阻断对话
        logger.warning("读取 AI 配置失败，本次请求回退本地模拟", exc_info=True)
        return AIConfig()


def _classify_stream_error(exc: BaseException) -> str:
    """把生成异常归类为 ``error`` 帧的 ``detail.reason``（不输出异常原文）。

    仅依据异常**类型**判定，不携带 ``str(exc)``，避免任何敏感信息经由 SSE 回显。
    """
    name = type(exc).__name__.lower()
    # 顺序敏感：TimeoutError 在 3.10+ 也是 OSError 子类，必须先判超时。
    if isinstance(exc, TimeoutError) or "timeout" in name:
        return STREAM_ERROR_TIMEOUT
    if isinstance(exc, OSError) or "connect" in name or "network" in name:  # 含 ConnectionError
        return STREAM_ERROR_NETWORK
    if "transport" in name or "http" in name or "protocol" in name:
        return STREAM_ERROR_REMOTE
    return STREAM_ERROR_GENERATION


def _stream_error_frame(exc: BaseException) -> str:
    """构造 ``error`` 帧：``code`` 恒为 ``STREAM_FAILED``，``detail.reason`` 可选。

    帧形状 ``{"code","message","detail"?}`` 与既有统一错误体保持一致；
    ``detail`` 为**可选**附加字段，前端未消费时行为不变（R2.5）。
    """
    reason = _classify_stream_error(exc)
    return _sse_frame(
        "error",
        {
            "code": "STREAM_FAILED",
            "message": _STREAM_ERROR_MESSAGE.get(reason, _STREAM_ERROR_FALLBACK_MESSAGE),
            "detail": {"reason": reason},
        },
    )


async def _sse_generator(
    session_id: str,
    user_id: str,
    mode: ChatRole,
    kp: Optional[KnowledgePoint],
    content: str,
    system_prompt: str,
    history: list[ChatTurn],
) -> AsyncIterator[str]:
    """SSE 事件生成器：落库 → meta → delta* → （落库）done；异常发 error。

    ``temperature`` / ``max_tokens`` 取自本次请求的 :class:`AIConfig` 并显式传给
    :meth:`AIAdapter.stream`（C-CHAT-PARAMS@1 第 1 条 / plan 2.3.2 参数生效链）。
    """
    conn = db_module.connect()
    assistant_id: Optional[int] = None
    # 0) 读取一次 AI 配置：temperature/max_tokens 显式注入；读取失败退化为本地模拟
    ai_config = _build_ai_config_safe()
    try:
        # 1) 落库用户消息
        _insert_message(conn, session_id, user_id, "user", content, None)

        # 2) 预分配助手消息占位行（元信息帧需要其 id）
        assistant_id = _insert_message(conn, session_id, user_id, "assistant", "", None)

        # 3) 构建适配器（每次请求独立实例，避免流式状态串扰）
        adapter = AIAdapter(ai_config, mode=mode, kp=kp)

        # 4) meta 首帧（必发）：provider 为本次**尝试**的提供方
        yield _sse_frame(
            "meta",
            {
                "provider": adapter.provider,
                "session_id": session_id,
                "message_id": assistant_id,
                "mode": mode,
            },
        )

        # 5) 流式生成（多轮上下文：历史 + 本轮用户消息，按时间升序）
        parts: list[str] = []
        try:
            turns = _build_turns(history, content)
            async for piece in adapter.stream(
                system_prompt,
                turns,
                temperature=ai_config.temperature,
                max_tokens=ai_config.max_tokens,
            ):
                parts.append(piece)
                yield _sse_frame("delta", {"text": piece})
        except Exception as exc:  # noqa: BLE001 - 生成异常：清理占位并发 error
            logger.exception("聊天流式生成失败：session=%s", session_id)
            if assistant_id is not None:
                try:
                    _delete_message(conn, assistant_id)
                except Exception:  # noqa: BLE001 - 清理失败不应吞掉 error 帧
                    logger.warning("清理助手占位消息失败：id=%s", assistant_id, exc_info=True)
            # code 保持 STREAM_FAILED（向后兼容），message 可读，detail.reason 可选
            yield _stream_error_frame(exc)
            return

        full_text = "".join(parts)
        # done.provider / 落库 provider 为本次**实际**提供方（回退后为 local）
        final_provider = adapter.last_provider

        # 6) 发送 done 前完成落库（助手完整消息含 provider + 会话时间戳）
        _update_message(conn, assistant_id, full_text, final_provider)
        _touch_session(conn, session_id)

        # 7) 费曼模式触发行为日志
        if mode == "feynman":
            _record_behavior(
                conn,
                user_id,
                "feynman_talk",
                getattr(kp, "kp_id", None),
                {"session_id": session_id, "message_id": assistant_id},
            )

        # 8) done 结束帧（必发）
        yield _sse_frame(
            "done",
            {"message_id": assistant_id, "content": full_text, "provider": final_provider},
        )
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 端点
# --------------------------------------------------------------------------- #
@router.post(
    "/sessions",
    response_model=ChatSessionView,
    status_code=201,
    summary="创建聊天会话",
    responses={
        404: {"description": "知识点不存在（KP_NOT_FOUND）"},
        422: {"description": "非法角色（INVALID_MODE）"},
    },
)
def create_session(
    payload: SessionCreateRequest,
    user_id: str = Depends(get_user_id),
    conn: sqlite3.Connection = Depends(get_db),
    repo: ContentRepository = Depends(get_content_repo),
) -> ChatSessionView:
    """创建会话；`kp_id` 存在时校验其有效性，`mode` 非 teacher/student/feynman 时返回 422。"""
    mode = _validate_mode(payload.mode)

    kp: Optional[KnowledgePoint] = None
    if payload.kp_id:
        kp = repo.get_knowledge_point(payload.kp_id)
        if kp is None:
            raise _kp_not_found(payload.kp_id)

    title = (payload.title or "").strip() or _default_title(mode, kp)
    now = db_module.to_iso(db_module.utc_now())
    session_id = str(uuid.uuid4())

    with db_module.transaction(conn):
        conn.execute(
            "INSERT INTO chat_session "
            "(session_id, user_id, kp_id, mode, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, user_id, payload.kp_id, mode, title, now, now),
        )

    row = _fetch_session(conn, session_id, user_id)
    assert row is not None  # 刚插入，必存在
    return _row_to_session_view(row)


@router.get(
    "/sessions",
    response_model=list[ChatSessionView],
    summary="会话列表",
)
def list_sessions(
    limit: int = Query(DEFAULT_SESSION_LIMIT, ge=1, le=MAX_SESSION_LIMIT),
    user_id: str = Depends(get_user_id),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[ChatSessionView]:
    """返回当前用户的会话，按 `updated_at` 倒序。"""
    rows = conn.execute(
        "SELECT session_id, user_id, kp_id, mode, title, created_at, updated_at "
        "FROM chat_session WHERE user_id = ? ORDER BY updated_at DESC, created_at DESC LIMIT ?",
        (user_id, int(limit)),
    ).fetchall()
    return [_row_to_session_view(row) for row in rows]


@router.get(
    "/sessions/{session_id}/messages",
    response_model=list[ChatMessageView],
    summary="会话消息列表",
    responses={404: {"description": "会话不存在（SESSION_NOT_FOUND）"}},
)
def list_messages(
    session_id: str,
    limit: int = Query(DEFAULT_MESSAGE_LIMIT, ge=1, le=MAX_MESSAGE_LIMIT),
    user_id: str = Depends(get_user_id),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[ChatMessageView]:
    """返回指定会话的消息，按时间升序（取最近 `limit` 条）。"""
    if _fetch_session(conn, session_id, user_id) is None:
        raise _session_not_found(session_id)

    rows = conn.execute(
        "SELECT id, session_id, role, content, provider, created_at FROM ("
        "  SELECT id, session_id, role, content, provider, created_at "
        "  FROM chat_message WHERE session_id = ? ORDER BY id DESC LIMIT ?"
        ") ORDER BY id ASC",
        (session_id, int(limit)),
    ).fetchall()
    return [_row_to_message_view(row) for row in rows]


@router.delete(
    "/sessions/{session_id}",
    status_code=204,
    summary="删除会话",
    responses={404: {"description": "会话不存在（SESSION_NOT_FOUND）"}},
)
def delete_session(
    session_id: str,
    user_id: str = Depends(get_user_id),
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    """删除会话及其全部消息；不存在时返回 404，成功返回 204。"""
    if _fetch_session(conn, session_id, user_id) is None:
        raise _session_not_found(session_id)

    with db_module.transaction(conn):
        conn.execute("DELETE FROM chat_message WHERE session_id = ?", (session_id,))
        conn.execute(
            "DELETE FROM chat_session WHERE session_id = ? AND user_id = ?",
            (session_id, user_id),
        )
    return Response(status_code=204)


@router.post(
    "/stream",
    summary="SSE 流式对话",
    responses={
        200: {"description": "text/event-stream：meta / delta / done / error 帧"},
        404: {"description": "会话或知识点不存在（SESSION_NOT_FOUND / KP_NOT_FOUND）"},
        422: {"description": "非法角色（INVALID_MODE）"},
    },
)
def stream_chat(
    payload: StreamRequest,
    user_id: str = Depends(get_user_id),
    conn: sqlite3.Connection = Depends(get_db),
    repo: ContentRepository = Depends(get_content_repo),
) -> StreamingResponse:
    """流式对话端点。

    校验（返回 SSE 流之前完成，异常以统一错误体返回）：会话存在且归属当前用户
    （否则 404 SESSION_NOT_FOUND）、`mode` 合法（否则 422 INVALID_MODE）、
    `kp_id` 存在（否则 404 KP_NOT_FOUND）。
    """
    session = _fetch_session(conn, payload.session_id, user_id)
    if session is None:
        raise _session_not_found(payload.session_id)

    mode = _validate_mode(payload.mode) if payload.mode is not None else session["mode"]

    kp_id = payload.kp_id if payload.kp_id is not None else session["kp_id"]
    kp: Optional[KnowledgePoint] = None
    if kp_id:
        kp = repo.get_knowledge_point(kp_id)
        if kp is None:
            raise _kp_not_found(kp_id)

    profile_summary = _load_profile_summary(conn, user_id)
    system_prompt = build_system_prompt(mode, kp, profile_summary)
    history = _load_history(conn, payload.session_id)

    generator = _sse_generator(
        session_id=payload.session_id,
        user_id=user_id,
        mode=mode,  # type: ignore[arg-type]
        kp=kp,
        content=payload.content,
        system_prompt=system_prompt,
        history=history,
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )