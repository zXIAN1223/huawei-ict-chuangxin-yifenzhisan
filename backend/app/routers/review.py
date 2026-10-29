"""复习调度与提醒 API（契约 C2-REVIEW@1 的 REST 部分，plan 2.2.2）。

统一前缀 ``/api/review``，共 10 个端点：

=========================================  ==========================================
方法 / 路径                                语义
=========================================  ==========================================
GET    /today                             今日待复习清单（预算裁剪，返回 DailyPlan）
GET    /items                             复习项列表（``status=active|removed|all``）
POST   /items                             幂等加入复习项（201；已存在则返回现有项）
DELETE /items/{kp_id}                     软删除（``status=removed``），保留历史
POST   /items/{kp_id}/restore             恢复为 active（保留 level/interval）
POST   /items/{kp_id}/complete            完成一次复习并推进记忆曲线
GET    /schedule                          未来 ``days`` 天的到期分布
PUT    /budget                            写 ``user_profile.daily_minutes``
GET    /reminder                          读取提醒设置
PUT    /reminder                          写提醒设置（``enabled=false`` 保留 time）
=========================================  ==========================================

错误语义：``404 KP_NOT_FOUND``（kp 不在内容库）、``422 INVALID_GRADE``（grade 不在 0..5）。
统一错误体 ``{"code","message","detail"?}``（由 ``main.py`` 的异常处理器渲染）。

依赖：``deps.get_user_id``（用户身份）、``deps.get_db``（每请求独立连接）、
``deps.get_content_repo``（已加载内容库）。``review_complete`` 行为日志经 T8 的
``learning_strategy.record_behavior`` 写入（模块未就绪时安全跳过，不阻塞主流程）。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from backend.app.data.content_loader import ContentRepository
from backend.app.db import (
    local_day_bounds,
    parse_iso,
    to_iso,
    transaction,
    utc_now,
)
from backend.app.deps import ApiError, get_content_repo, get_db, get_user_id
from backend.app.models import (
    KnowledgePointMeta,
    ReviewItemView,
    ReviewState,
    ReviewStateView,
)
from backend.app.services.spaced_repetition import (
    EASE_START,

    DailyPlan,
    apply_grade,
    build_daily_plan,
    compute_interval_days,
)

# T8 尚未交付时安全跳过行为日志（按契约签名调用）。
try:  # pragma: no cover - 依赖 T8 交付布局
    from backend.app.services.learning_strategy import (
        record_behavior as _record_behavior,
    )
except Exception:  # noqa: BLE001 - 任何导入错误都不应阻塞本模块加载
    _record_behavior = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/review", tags=["review"])

#: 提醒时间格式：``HH:MM``（00:00 ~ 23:59）。
TIME_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"

#: 每日预算缺省值（``budget_minutes`` 传 null 时回退），单位分钟。
DEFAULT_BUDGET_MINUTES = 30


# ---------------------------------------------------------------------------
# 请求 / 响应模型
# ---------------------------------------------------------------------------


class AddReviewItemRequest(BaseModel):
    """``POST /items`` 请求体。"""

    kp_id: str
    chapter_id: str | None = None


class CompleteRequest(BaseModel):
    """``POST /items/{kp_id}/complete`` 请求体（grade 范围在处理器内校验）。"""

    grade: int


class CompleteResponse(BaseModel):
    """``POST /items/{kp_id}/complete`` 响应体。"""

    state: ReviewStateView
    next_due_at: str


class ScheduleDay(BaseModel):
    """未来到期分布中的一天。"""

    date: str
    due_count: int
    total_minutes: int


class ScheduleResponse(BaseModel):
    """``GET /schedule`` 响应体。"""

    days: list[ScheduleDay] = Field(default_factory=list)


class BudgetRequest(BaseModel):
    """``PUT /budget`` 请求体；``null`` 表示回退默认 30。"""

    budget_minutes: int | None = Field(default=None, ge=0)


class BudgetResponse(BaseModel):
    """``PUT /budget`` 响应体。"""

    budget_minutes: int


class ReminderRequest(BaseModel):
    """``PUT /reminder`` 请求体。``time`` 为 null 时保留原值。"""

    enabled: bool
    time: str | None = Field(default=None, pattern=TIME_PATTERN)


class ReminderResponse(BaseModel):
    """``GET/PUT /reminder`` 响应体。"""

    enabled: bool
    time: str


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _row_to_state(row: sqlite3.Row) -> ReviewState:
    """将 ``review_state`` 行映射为 :class:`ReviewState`。"""
    return ReviewState(
        user_id=row["user_id"],
        kp_id=row["kp_id"],
        chapter_id=row["chapter_id"],
        status=row["status"],
        level=row["level"],
        interval_days=row["interval_days"],
        ease=row["ease"],
        due_at=row["due_at"],
        last_reviewed_at=row["last_reviewed_at"],
        review_count=row["review_count"],
        lapses=row["lapses"],
        mastery=row["mastery"],
        selected_at=row["selected_at"],
        updated_at=row["updated_at"],
    )


def _state_view(state: ReviewState) -> ReviewStateView:
    """由 :class:`ReviewState` 构造 :class:`ReviewStateView`。"""
    return ReviewStateView(
        kp_id=state.kp_id,
        chapter_id=state.chapter_id,
        status=state.status,
        level=int(state.level),
        interval_days=int(state.interval_days),
        due_at=state.due_at,
        last_reviewed_at=state.last_reviewed_at,
        review_count=int(state.review_count),
        lapses=int(state.lapses),
        mastery=float(state.mastery),
    )


def _row_to_item_view(
    row: sqlite3.Row, repo: ContentRepository
) -> ReviewItemView:
    """将 ``review_state`` 行 + 知识点元数据组装为 :class:`ReviewItemView`。"""
    state = _row_to_state(row)
    meta = repo.get_knowledge_point_meta(state.kp_id)
    return ReviewItemView(
        kp_id=state.kp_id,
        chapter_id=state.chapter_id,
        status=state.status,
        level=int(state.level),
        interval_days=int(state.interval_days),
        due_at=state.due_at,
        last_reviewed_at=state.last_reviewed_at,
        review_count=int(state.review_count),
        lapses=int(state.lapses),
        mastery=float(state.mastery),
        title=meta.title if meta is not None else state.kp_id,
        difficulty=meta.difficulty if meta is not None else 3,
        importance=meta.importance if meta is not None else 3,
        est_minutes=meta.est_minutes if meta is not None else 1,
    )


def _require_kp(repo: ContentRepository, kp_id: str) -> KnowledgePointMeta:
    """校验 kp 存在于内容库，否则抛 404 KP_NOT_FOUND。"""
    meta = repo.get_knowledge_point_meta(kp_id)
    if meta is None:
        raise ApiError(404, "KP_NOT_FOUND", f"知识点不存在：{kp_id}", {"kp_id": kp_id})
    return meta


def _fetch_state_row(
    conn: sqlite3.Connection, user_id: str, kp_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM review_state WHERE user_id = ? AND kp_id = ?",
        (user_id, kp_id),
    ).fetchone()


def _ensure_profile(conn: sqlite3.Connection, user_id: str) -> sqlite3.Row:
    """读取 ``user_profile`` 行；不存在则创建默认行（与 C5 默认值一致）。

    使用 ``INSERT OR IGNORE`` 以容忍与 T8 的并发创建。
    """
    row = conn.execute(
        "SELECT * FROM user_profile WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row is not None:
        return row

    now_iso = to_iso(utc_now())
    conn.execute(
        "INSERT OR IGNORE INTO user_profile (user_id, created_at, updated_at) "
        "VALUES (?, ?, ?)",
        (user_id, now_iso, now_iso),
    )
    return conn.execute(
        "SELECT * FROM user_profile WHERE user_id = ?", (user_id,)
    ).fetchone()


def _correct_ratio(conn: sqlite3.Connection, user_id: str, kp_id: str) -> float:
    """由 C3 作答历史计算该 kp 的正确率；无历史返回 0.0。"""
    rows = conn.execute(
        "SELECT is_correct FROM quiz_attempt WHERE user_id = ? AND kp_id = ?",
        (user_id, kp_id),
    ).fetchall()
    if not rows:
        return 0.0
    correct = sum(1 for row in rows if row["is_correct"])
    return correct / len(rows)


def _record_review_complete(
    conn: sqlite3.Connection,
    user_id: str,
    kp_id: str,
    grade: int,
    state: ReviewState,
) -> None:
    """经 T8 的 ``record_behavior`` 记录 ``review_complete`` 行为日志（失败不抛）。"""
    if _record_behavior is None:
        logger.debug("learning_strategy.record_behavior 未就绪，跳过行为日志")
        return
    try:
        _record_behavior(
            conn,
            user_id,
            "review_complete",
            kp_id,
            {
                "grade": int(grade),
                "level": int(state.level),
                "interval_days": int(state.interval_days),
                "ease": float(state.ease),
                "mastery": float(state.mastery),
                "due_at": state.due_at,
            },
        )
    except Exception:  # noqa: BLE001 - 行为日志失败不得影响主流程
        logger.exception("记录 review_complete 行为日志失败：user=%s kp=%s", user_id, kp_id)


# ---------------------------------------------------------------------------
# 端点 1：GET /today
# ---------------------------------------------------------------------------


@router.get(
    "/today",
    response_model=DailyPlan,
    summary="今日待复习清单",
    description=(
        "在用户时间预算内生成今日待复习清单。budget_minutes 缺省时读取 "
        "user_profile.daily_minutes（不存在则创建默认画像，默认 30）。"
    ),
)
def get_today(
    budget_minutes: int | None = Query(
        None, ge=0, description="当日时间预算（分钟）；缺省读取用户画像"
    ),
    user_id: str = Depends(get_user_id),
    conn: sqlite3.Connection = Depends(get_db),
    repo: ContentRepository = Depends(get_content_repo),
) -> DailyPlan:
    if budget_minutes is None:
        profile = _ensure_profile(conn, user_id)
        budget_minutes = int(profile["daily_minutes"])

    now = utc_now()
    rows = conn.execute(
        "SELECT * FROM review_state WHERE user_id = ? AND status = 'active'",
        (user_id,),
    ).fetchall()
    states = [_row_to_state(row) for row in rows]

    kp_index: dict[str, KnowledgePointMeta] = {}
    for state in states:
        meta = repo.get_knowledge_point_meta(state.kp_id)
        if meta is not None:
            kp_index[state.kp_id] = meta

    return build_daily_plan(states, now, budget_minutes, kp_index)


# ---------------------------------------------------------------------------
# 端点 2：GET /items
# ---------------------------------------------------------------------------


@router.get(
    "/items",
    response_model=list[ReviewItemView],
    summary="复习项列表",
    description="按状态过滤复习项：active（默认）、removed 或 all。",
)
def list_items(
    status: Literal["active", "removed", "all"] = Query(
        "active", description="状态过滤：active | removed | all"
    ),
    user_id: str = Depends(get_user_id),
    conn: sqlite3.Connection = Depends(get_db),
    repo: ContentRepository = Depends(get_content_repo),
) -> list[ReviewItemView]:
    if status == "all":
        rows = conn.execute(
            "SELECT * FROM review_state WHERE user_id = ? ORDER BY due_at ASC",
            (user_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM review_state WHERE user_id = ? AND status = ? "
            "ORDER BY due_at ASC",
            (user_id, status),
        ).fetchall()
    return [_row_to_item_view(row, repo) for row in rows]


# ---------------------------------------------------------------------------
# 端点 3：POST /items（幂等加入）
# ---------------------------------------------------------------------------


@router.post(
    "/items",
    response_model=ReviewItemView,
    status_code=201,
    summary="加入复习（幂等）",
    description="加入某知识点的复习；已存在（无论 active/removed）时返回现有项，不重复创建。",
    responses={404: {"description": "知识点不存在（KP_NOT_FOUND）"}},
)
def add_item(
    payload: AddReviewItemRequest,
    user_id: str = Depends(get_user_id),
    conn: sqlite3.Connection = Depends(get_db),
    repo: ContentRepository = Depends(get_content_repo),
) -> ReviewItemView:
    meta = _require_kp(repo, payload.kp_id)

    existing = _fetch_state_row(conn, user_id, payload.kp_id)
    if existing is not None:
        # 幂等：已存在则原样返回现有项
        return _row_to_item_view(existing, repo)

    now = utc_now()
    now_iso = to_iso(now)
    interval_days = compute_interval_days(0, meta.difficulty, meta.importance, EASE_START)
    due_at = to_iso(now + timedelta(days=interval_days))

    with transaction(conn):
        conn.execute(
            "INSERT INTO review_state ("
            "user_id, kp_id, chapter_id, status, level, interval_days, ease, "
            "due_at, last_reviewed_at, review_count, lapses, mastery, "
            "selected_at, updated_at"
            ") VALUES (?, ?, ?, 'active', 0, ?, ?, ?, NULL, 0, 0, 0.0, ?, ?)",
            (
                user_id,
                payload.kp_id,
                meta.chapter_id,
                interval_days,
                EASE_START,
                due_at,
                now_iso,
                now_iso,
            ),
        )

    row = _fetch_state_row(conn, user_id, payload.kp_id)
    return _row_to_item_view(row, repo)


# ---------------------------------------------------------------------------
# 端点 4：DELETE /items/{kp_id}（软删除）
# ---------------------------------------------------------------------------


@router.delete(
    "/items/{kp_id}",
    summary="删除复习项（软删除）",
    description="将复习项标记为 removed（停止提醒），历史作答与掌握度记录保留。",
)
def delete_item(
    kp_id: str,
    user_id: str = Depends(get_user_id),
    conn: sqlite3.Connection = Depends(get_db),
    repo: ContentRepository = Depends(get_content_repo),
) -> dict[str, str]:
    row = _fetch_state_row(conn, user_id, kp_id)
    if row is None:
        # 知识点不在内容库 → 404；否则幂等返回（无可删除项）
        _require_kp(repo, kp_id)
        return {"status": "removed"}

    with transaction(conn):
        conn.execute(
            "UPDATE review_state SET status = 'removed', updated_at = ? "
            "WHERE user_id = ? AND kp_id = ?",
            (to_iso(utc_now()), user_id, kp_id),
        )
    return {"status": "removed"}


# ---------------------------------------------------------------------------
# 端点 5：POST /items/{kp_id}/restore
# ---------------------------------------------------------------------------


@router.post(
    "/items/{kp_id}/restore",
    response_model=ReviewItemView,
    summary="恢复复习项",
    description="将复习项恢复为 active，保留原有 level / interval / 历史。",
    responses={404: {"description": "知识点或复习项不存在"}},
)
def restore_item(
    kp_id: str,
    user_id: str = Depends(get_user_id),
    conn: sqlite3.Connection = Depends(get_db),
    repo: ContentRepository = Depends(get_content_repo),
) -> ReviewItemView:
    meta = repo.get_knowledge_point_meta(kp_id)
    if meta is None:
        raise ApiError(404, "KP_NOT_FOUND", f"知识点不存在：{kp_id}", {"kp_id": kp_id})

    row = _fetch_state_row(conn, user_id, kp_id)
    if row is None:
        raise ApiError(
            404,
            "REVIEW_ITEM_NOT_FOUND",
            f"复习项不存在：{kp_id}",
            {"kp_id": kp_id},
        )

    with transaction(conn):
        conn.execute(
            "UPDATE review_state SET status = 'active', updated_at = ? "
            "WHERE user_id = ? AND kp_id = ?",
            (to_iso(utc_now()), user_id, kp_id),
        )

    updated = _fetch_state_row(conn, user_id, kp_id)
    return _row_to_item_view(updated, repo)


# ---------------------------------------------------------------------------
# 端点 6：POST /items/{kp_id}/complete
# ---------------------------------------------------------------------------


@router.post(
    "/items/{kp_id}/complete",
    response_model=CompleteResponse,
    summary="完成一次复习",
    description="按 grade（0..5）推进记忆曲线并更新状态；非法 grade 返回 422 INVALID_GRADE。",
    responses={
        404: {"description": "知识点或复习项不存在"},
        422: {"description": "grade 不在 0..5（INVALID_GRADE）"},
    },
)
def complete_review(
    kp_id: str,
    payload: CompleteRequest,
    user_id: str = Depends(get_user_id),
    conn: sqlite3.Connection = Depends(get_db),
    repo: ContentRepository = Depends(get_content_repo),
) -> CompleteResponse:
    if payload.grade < 0 or payload.grade > 5:
        raise ApiError(
            422,
            "INVALID_GRADE",
            f"grade 必须在 0..5 之间，实际为 {payload.grade}",
            {"grade": payload.grade},
        )

    meta = repo.get_knowledge_point_meta(kp_id)
    if meta is None:
        raise ApiError(404, "KP_NOT_FOUND", f"知识点不存在：{kp_id}", {"kp_id": kp_id})

    row = _fetch_state_row(conn, user_id, kp_id)
    if row is None:
        raise ApiError(
            404,
            "REVIEW_ITEM_NOT_FOUND",
            f"复习项不存在：{kp_id}",
            {"kp_id": kp_id},
        )

    state = _row_to_state(row)
    correct_ratio = _correct_ratio(conn, user_id, kp_id)
    now = utc_now()
    new_state = apply_grade(
        state,
        payload.grade,
        now,
        difficulty=meta.difficulty,
        importance=meta.importance,
        correct_ratio=correct_ratio,
    )

    with transaction(conn):
        conn.execute(
            "UPDATE review_state SET level = ?, interval_days = ?, ease = ?, "
            "due_at = ?, last_reviewed_at = ?, review_count = ?, lapses = ?, "
            "mastery = ?, updated_at = ? "
            "WHERE user_id = ? AND kp_id = ?",
            (
                int(new_state.level),
                int(new_state.interval_days),
                float(new_state.ease),
                new_state.due_at,
                new_state.last_reviewed_at,
                int(new_state.review_count),
                int(new_state.lapses),
                float(new_state.mastery),
                new_state.updated_at,
                user_id,
                kp_id,
            ),
        )

    _record_review_complete(conn, user_id, kp_id, payload.grade, new_state)

    return CompleteResponse(state=_state_view(new_state), next_due_at=new_state.due_at)


# ---------------------------------------------------------------------------
# 端点 7：GET /schedule
# ---------------------------------------------------------------------------


@router.get(
    "/schedule",
    response_model=ScheduleResponse,
    summary="未来到期分布",
    description="返回自今日起 days 天内每天的到期数量与预计总时长（仅统计 active）。",
)
def get_schedule(
    days: int = Query(14, ge=1, le=365, description="统计天数（含今日）"),
    user_id: str = Depends(get_user_id),
    conn: sqlite3.Connection = Depends(get_db),
    repo: ContentRepository = Depends(get_content_repo),
) -> ScheduleResponse:
    now = utc_now()
    rows = conn.execute(
        "SELECT * FROM review_state WHERE user_id = ? AND status = 'active'",
        (user_id,),
    ).fetchall()

    parsed: list[tuple[str, Any, int]] = []
    for row in rows:
        meta = repo.get_knowledge_point_meta(row["kp_id"])
        est = meta.est_minutes if meta is not None else 1
        try:
            due = parse_iso(row["due_at"])
        except (ValueError, TypeError):
            continue
        parsed.append((row["kp_id"], due, est))

    start, _ = local_day_bounds(now)
    result: list[ScheduleDay] = []
    for offset in range(days):
        day_start = start + timedelta(days=offset)
        day_end = day_start + timedelta(days=1)
        count = 0
        minutes = 0
        for _kp_id, due, est in parsed:
            if day_start <= due < day_end:
                count += 1
                minutes += est
        result.append(
            ScheduleDay(
                date=day_start.astimezone().date().isoformat(),
                due_count=count,
                total_minutes=minutes,
            )
        )

    return ScheduleResponse(days=result)



# ---------------------------------------------------------------------------
# 端点 8：PUT /budget
# ---------------------------------------------------------------------------


@router.put(
    "/budget",
    response_model=BudgetResponse,
    summary="设置每日时间预算",
    description="写 user_profile.daily_minutes；budget_minutes 传 null 时回退默认 30。",
)
def put_budget(
    payload: BudgetRequest,
    user_id: str = Depends(get_user_id),
    conn: sqlite3.Connection = Depends(get_db),
) -> BudgetResponse:
    value = DEFAULT_BUDGET_MINUTES if payload.budget_minutes is None else payload.budget_minutes
    _ensure_profile(conn, user_id)
    with transaction(conn):
        conn.execute(
            "UPDATE user_profile SET daily_minutes = ?, updated_at = ? WHERE user_id = ?",
            (value, to_iso(utc_now()), user_id),
        )
    return BudgetResponse(budget_minutes=value)


# ---------------------------------------------------------------------------
# 端点 9：GET /reminder
# ---------------------------------------------------------------------------


@router.get(
    "/reminder",
    response_model=ReminderResponse,
    summary="读取提醒设置",
    description="读取 user_profile 的提醒开关与时间。",
)
def get_reminder(
    user_id: str = Depends(get_user_id),
    conn: sqlite3.Connection = Depends(get_db),
) -> ReminderResponse:
    profile = _ensure_profile(conn, user_id)
    return ReminderResponse(
        enabled=bool(profile["reminder_enabled"]),
        time=str(profile["reminder_time"]),
    )


# ---------------------------------------------------------------------------
# 端点 10：PUT /reminder
# ---------------------------------------------------------------------------


@router.put(
    "/reminder",
    response_model=ReminderResponse,
    summary="设置提醒",
    description="写提醒开关与时间；time 必须匹配 HH:MM（非法 422），null 时保留原值。",
    responses={422: {"description": "time 格式非法"}},
)
def put_reminder(
    payload: ReminderRequest,
    user_id: str = Depends(get_user_id),
    conn: sqlite3.Connection = Depends(get_db),
) -> ReminderResponse:
    profile = _ensure_profile(conn, user_id)
    # enabled=false 时保留传入/原有 time，不丢失
    new_time = payload.time if payload.time is not None else str(profile["reminder_time"])
    with transaction(conn):
        conn.execute(
            "UPDATE user_profile SET reminder_enabled = ?, reminder_time = ?, "
            "updated_at = ? WHERE user_id = ?",
            (1 if payload.enabled else 0, new_time, to_iso(utc_now()), user_id),
        )
    return ReminderResponse(enabled=payload.enabled, time=new_time)


__all__ = ["router"]