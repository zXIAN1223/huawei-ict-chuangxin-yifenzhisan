"""用户画像 / 问卷 / VARK 测评 / 自适应策略 / 行为日志 / 进度统计 API。

契约 C5-PROFILE@1（plan 2.2.5）。本模块对外暴露 8 个端点：

============================================  ================================================
方法 / 路径                                     语义
============================================  ================================================
GET    /api/profile                           读取画像（不存在则创建默认）
PUT    /api/profile                           部分更新画像（省略=不变；显式 null=清空/回默认）
GET    /api/profile/questionnaire             读取问卷文档（QuestionnaireDocument）
POST   /api/profile/questionnaire             提交问卷答案（存库 + questionnaire_completed=true）
POST   /api/profile/assessment                单次测评（不落 questionnaire_completed）
GET    /api/profile/strategy                  计算自适应策略并落 strategy_snapshot
GET    /api/profile/analytics?days=7          学习进度统计（AnalyticsView）
POST   /api/log/behavior                      写入行为日志（201；前端事件）
============================================  ================================================

.. note::
    ``POST /api/log/behavior`` 不在 ``/api/profile`` 前缀之下。为同时满足两组前缀，
    ``main.py`` 只注册模块级 ``router``，故此处以**无前缀的伞状 router** 组合两个
    子路由：``profile_router``（``/api/profile``）与 ``log_router``（``/api/log``）。

PUT 语义（plan 2.2.5）：省略字段=不变；显式传 ``null`` 表示清空/回默认——
``daily_minutes→30``、``reminder_time→"20:00"``、``preferred_slots→[]``、
``nickname→"学习者"``、``questionnaire_answers→{}``；``learning_style`` 与
``style_scores`` 不接受 ``null``（忽略该字段、保留原值）；
``reminder_enabled=false`` 时保留 ``reminder_time``。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from backend.app.config import DATA_DIR
from backend.app.db import (
    is_due_by_end_of_local_day,
    parse_iso,
    to_iso,
    transaction,
    utc_now,
)
from backend.app.deps import ApiError, get_db, get_user_id
from backend.app.models import (
    BehaviorLogView,
    QuizAttemptView,
    ReviewStateView,
    UserProfileView,
)
from backend.app.services.learning_strategy import (
    QUESTIONNAIRE_FILENAME,
    AssessmentResult,
    AdaptiveStrategy,
    compute_strategy,
    evaluate_assessment,
    record_behavior,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 路由（伞状 router + 两个子路由）
# ---------------------------------------------------------------------------

profile_router = APIRouter(prefix="/api/profile", tags=["profile"])
log_router = APIRouter(prefix="/api/log", tags=["log"])

#: ``main.py`` 注册的模块级 router（无前缀，内部包含两个子路由）。
router = APIRouter()
router.include_router(profile_router)
router.include_router(log_router)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 提醒时间格式 ``HH:MM``（00:00 ~ 23:59）。
TIME_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"

DEFAULT_NICKNAME = "学习者"
DEFAULT_DAILY_MINUTES = 30
DEFAULT_REMINDER_TIME = "20:00"

#: ``behavior_log.action_type`` 白名单（plan 2.2.5）。
ALLOWED_ACTION_TYPES: frozenset[str] = frozenset(
    {
        "kp_view",
        "quiz_attempt",
        "review_complete",
        "chat_message",
        "feynman_talk",
        "assessment_done",
        "session_start",
        "session_end",
    }
)

#: 复习强度评估所用的最近作答次数上限（与 learning_strategy 保持一致）。
_RECENT_LIMIT = 20


# ---------------------------------------------------------------------------
# 请求 / 响应模型
# ---------------------------------------------------------------------------


class ProfileUpdateRequest(BaseModel):
    """``PUT /api/profile`` 请求体（全部可选；区分「省略」与「显式 null」）。"""

    model_config = ConfigDict(extra="ignore")

    nickname: str | None = None
    daily_minutes: int | None = Field(default=None, ge=0)
    preferred_slots: list[str] | None = None
    learning_style: Literal["visual", "auditory", "reading", "kinesthetic", "mixed"] | None = (
        None
    )
    style_scores: dict[str, int] | None = None
    questionnaire_answers: dict[str, Any] | None = None
    questionnaire_completed: bool | None = None
    assessment_completed: bool | None = None
    reminder_enabled: bool | None = None
    reminder_time: str | None = Field(default=None, pattern=TIME_PATTERN)


class AnswersRequest(BaseModel):
    """``POST /questionnaire`` 与 ``POST /assessment`` 请求体。"""

    model_config = ConfigDict(extra="ignore")

    answers: dict[str, Any] = Field(default_factory=dict)


class BehaviorRequest(BaseModel):
    """``POST /api/log/behavior`` 请求体。"""

    model_config = ConfigDict(extra="ignore")

    action_type: str
    kp_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class QuestionnaireOption(BaseModel):
    """问卷选项。"""

    model_config = ConfigDict(extra="ignore")

    value: str
    label: str


class QuestionnaireQuestion(BaseModel):
    """问卷题目（``min``/``max`` 仅 scale 题使用）。"""

    model_config = ConfigDict(extra="ignore")

    qid: str
    text: str
    type: str
    dimension: str
    options: list[QuestionnaireOption] = Field(default_factory=list)
    min: int | None = None
    max: int | None = None


class QuestionnaireDocument(BaseModel):
    """问卷文档（``GET /api/profile/questionnaire`` 响应）。"""

    version: str
    title: str | None = None
    description: str | None = None
    questions: list[QuestionnaireQuestion] = Field(default_factory=list)


class ByDayEntry(BaseModel):
    """按日统计条目。"""

    date: str
    attempts: int
    minutes: int


class MasteryByChapter(BaseModel):
    """按章节掌握度条目。"""

    chapter_id: str
    avg_mastery: float


class AnalyticsView(BaseModel):
    """``GET /api/profile/analytics`` 响应。"""

    streak_days: int
    total_minutes_estimate: int
    attempts: int
    correct_rate: float
    kps_in_review: int
    review_due_today: int
    by_day: list[ByDayEntry] = Field(default_factory=list)
    mastery_by_chapter: list[MasteryByChapter] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _safe_json_loads(value: Any, default: Any) -> Any:
    """尽力解析 JSON 文本列；失败时返回默认值。"""
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


def _row_to_profile_view(row: sqlite3.Row) -> UserProfileView:
    """将 ``user_profile`` 行映射为 :class:`UserProfileView`（JSON 列已解析）。"""
    return UserProfileView(
        user_id=row["user_id"],
        nickname=row["nickname"],
        daily_minutes=int(row["daily_minutes"]),
        preferred_slots=_safe_json_loads(row["preferred_slots"], []),
        learning_style=row["learning_style"],
        style_scores=_safe_json_loads(row["style_scores"], {}),
        questionnaire_answers=_safe_json_loads(row["questionnaire_answers"], {}),
        questionnaire_completed=bool(row["questionnaire_completed"]),
        assessment_completed=bool(row["assessment_completed"]),
        reminder_enabled=bool(row["reminder_enabled"]),
        reminder_time=row["reminder_time"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _ensure_profile(conn: sqlite3.Connection, user_id: str) -> sqlite3.Row:
    """读取画像行；不存在则创建默认行（与 review.py 一致的 ``INSERT OR IGNORE``）。"""
    row = conn.execute(
        "SELECT * FROM user_profile WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row is not None:
        return row

    now_iso = to_iso(utc_now())
    with transaction(conn):
        conn.execute(
            "INSERT OR IGNORE INTO user_profile (user_id, created_at, updated_at) "
            "VALUES (?, ?, ?)",
            (user_id, now_iso, now_iso),
        )
    return conn.execute(
        "SELECT * FROM user_profile WHERE user_id = ?", (user_id,)
    ).fetchone()


def _load_questionnaire_document() -> QuestionnaireDocument:
    """读取并解析 ``data/questionnaire.json``；文件缺失/非法时抛 500。"""
    path = DATA_DIR / QUESTIONNAIRE_FILENAME
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        logger.exception("读取问卷文件失败：%s", path)
        raise ApiError(
            500,
            "QUESTIONNAIRE_UNAVAILABLE",
            "问卷数据不可用",
            {"path": str(path), "reason": str(exc)},
        ) from exc
    if not isinstance(data, dict):
        raise ApiError(500, "QUESTIONNAIRE_UNAVAILABLE", "问卷数据格式非法")
    return QuestionnaireDocument.model_validate(data)


def _persist_assessment(
    conn: sqlite3.Connection,
    user_id: str,
    answers: dict[str, Any],
    result: AssessmentResult,
    *,
    mark_questionnaire_completed: bool,
) -> None:
    """将测评结果写回画像（原始分存 ``style_scores``，风格存 ``learning_style``）。"""
    raw_scores = {
        "visual": 0,
        "auditory": 0,
        "reading": 0,
        "kinesthetic": 0,
    }
    # 由归一化分反推不作为存储来源；直接按原始作答重算原始分更稳妥。
    from backend.app.services.learning_strategy import DIMENSION_BY_CODE, _style_questions

    for question in _style_questions():
        qid = str(question.get("qid"))
        if qid not in answers:
            continue
        answer = answers.get(qid)
        values = answer if isinstance(answer, (list, tuple, set)) else [answer]
        for value in values:
            dimension = DIMENSION_BY_CODE.get(str(value))
            if dimension is not None:
                raw_scores[dimension] += 1

    now_iso = to_iso(utc_now())
    sets = [
        "learning_style = ?",
        "style_scores = ?",
        "questionnaire_answers = ?",
        "updated_at = ?",
    ]
    params: list[Any] = [
        result.learning_style,
        json.dumps(raw_scores, ensure_ascii=False),
        json.dumps(answers, ensure_ascii=False),
        now_iso,
    ]
    if mark_questionnaire_completed:
        sets.insert(0, "questionnaire_completed = 1")
    else:
        sets.insert(0, "assessment_completed = 1")

    with transaction(conn):
        conn.execute(
            f"UPDATE user_profile SET {', '.join(sets)} WHERE user_id = ?",
            (*params, user_id),
        )


def _local_date(iso_value: str):
    """将 UTC ISO 时间戳转换为服务器本地日期对象；非法值返回 ``None``。"""
    try:
        return parse_iso(iso_value).astimezone().date()
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# 端点 1：GET /api/profile
# ---------------------------------------------------------------------------


@profile_router.get(
    "",
    response_model=UserProfileView,
    summary="读取用户画像",
    description="读取画像；不存在则以默认值（nickname=学习者、daily_minutes=30 等）创建。",
)
def get_profile(
    user_id: str = Depends(get_user_id),
    conn: sqlite3.Connection = Depends(get_db),
) -> UserProfileView:
    row = _ensure_profile(conn, user_id)
    return _row_to_profile_view(row)


# ---------------------------------------------------------------------------
# 端点 2：PUT /api/profile（部分更新 + null 清除语义）
# ---------------------------------------------------------------------------


@profile_router.put(
    "",
    response_model=UserProfileView,
    summary="更新用户画像（部分更新）",
    description=(
        "省略字段=不变；显式传 null 表示清空/回默认。learning_style 与 style_scores "
        "不接受 null（忽略该字段）；reminder_enabled=false 时保留 reminder_time。"
    ),
)
def update_profile(
    payload: ProfileUpdateRequest,
    user_id: str = Depends(get_user_id),
    conn: sqlite3.Connection = Depends(get_db),
) -> UserProfileView:
    _ensure_profile(conn, user_id)
    provided = payload.model_fields_set

    sets: list[str] = []
    params: list[Any] = []

    def _add(column: str, value: Any) -> None:
        sets.append(f"{column} = ?")
        params.append(value)

    # 省略=不变：仅处理显式提供的字段（model_fields_set 区分「省略」与「显式 null」）。
    if "nickname" in provided:
        _add("nickname", payload.nickname if payload.nickname is not None else DEFAULT_NICKNAME)
    if "daily_minutes" in provided:
        _add(
            "daily_minutes",
            payload.daily_minutes if payload.daily_minutes is not None else DEFAULT_DAILY_MINUTES,
        )
    if "preferred_slots" in provided:
        slots = payload.preferred_slots if payload.preferred_slots is not None else []
        _add("preferred_slots", json.dumps(slots, ensure_ascii=False))
    if "questionnaire_answers" in provided:
        answers = payload.questionnaire_answers if payload.questionnaire_answers is not None else {}
        _add("questionnaire_answers", json.dumps(answers, ensure_ascii=False))
    if "reminder_time" in provided:
        _add(
            "reminder_time",
            payload.reminder_time if payload.reminder_time is not None else DEFAULT_REMINDER_TIME,
        )
    # learning_style / style_scores：显式 null 时忽略（保留原值）。
    if "learning_style" in provided and payload.learning_style is not None:
        _add("learning_style", payload.learning_style)
    if "style_scores" in provided and payload.style_scores is not None:
        _add("style_scores", json.dumps(payload.style_scores, ensure_ascii=False))
    if "questionnaire_completed" in provided and payload.questionnaire_completed is not None:
        _add("questionnaire_completed", 1 if payload.questionnaire_completed else 0)
    if "assessment_completed" in provided and payload.assessment_completed is not None:
        _add("assessment_completed", 1 if payload.assessment_completed else 0)
    # reminder_enabled=false 时保留 reminder_time（仅在显式 null 时重置，见上）。
    if "reminder_enabled" in provided and payload.reminder_enabled is not None:
        _add("reminder_enabled", 1 if payload.reminder_enabled else 0)

    _add("updated_at", to_iso(utc_now()))

    with transaction(conn):
        conn.execute(
            f"UPDATE user_profile SET {', '.join(sets)} WHERE user_id = ?",
            (*params, user_id),
        )

    row = conn.execute(
        "SELECT * FROM user_profile WHERE user_id = ?", (user_id,)
    ).fetchone()
    return _row_to_profile_view(row)


# ---------------------------------------------------------------------------
# 端点 3：GET /api/profile/questionnaire
# ---------------------------------------------------------------------------


@profile_router.get(
    "/questionnaire",
    response_model=QuestionnaireDocument,
    summary="读取问卷文档",
    description="返回版本化问卷（含题目、选项与维度）。",
)
def get_questionnaire() -> QuestionnaireDocument:
    return _load_questionnaire_document()


# ---------------------------------------------------------------------------
# 端点 4：POST /api/profile/questionnaire
# ---------------------------------------------------------------------------


@profile_router.post(
    "/questionnaire",
    response_model=AssessmentResult,
    summary="提交问卷并测评",
    description="存问卷答案并置 questionnaire_completed=true，返回 AssessmentResult。",
)
def submit_questionnaire(
    payload: AnswersRequest,
    user_id: str = Depends(get_user_id),
    conn: sqlite3.Connection = Depends(get_db),
) -> AssessmentResult:
    _ensure_profile(conn, user_id)
    result = evaluate_assessment(payload.answers)
    _persist_assessment(
        conn,
        user_id,
        payload.answers,
        result,
        mark_questionnaire_completed=True,
    )
    return result


# ---------------------------------------------------------------------------
# 端点 5：POST /api/profile/assessment
# ---------------------------------------------------------------------------


@profile_router.post(
    "/assessment",
    response_model=AssessmentResult,
    summary="单次测评",
    description="依据作答计算 VARK 测评结果并更新画像；置 assessment_completed=true。",
)
def run_assessment(
    payload: AnswersRequest,
    user_id: str = Depends(get_user_id),
    conn: sqlite3.Connection = Depends(get_db),
) -> AssessmentResult:
    _ensure_profile(conn, user_id)
    result = evaluate_assessment(payload.answers)
    _persist_assessment(
        conn,
        user_id,
        payload.answers,
        result,
        mark_questionnaire_completed=False,
    )
    return result


# ---------------------------------------------------------------------------
# 端点 6：GET /api/profile/strategy
# ---------------------------------------------------------------------------


@profile_router.get(
    "/strategy",
    response_model=AdaptiveStrategy,
    summary="自适应学习策略",
    description="计算策略并写入 strategy_snapshot，返回 AdaptiveStrategy。",
)
def get_strategy(
    user_id: str = Depends(get_user_id),
    conn: sqlite3.Connection = Depends(get_db),
) -> AdaptiveStrategy:
    profile_row = _ensure_profile(conn, user_id)
    profile = _row_to_profile_view(profile_row)

    attempt_rows = conn.execute(
        "SELECT * FROM quiz_attempt WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, _RECENT_LIMIT),
    ).fetchall()
    # compute_strategy 取列表末端为「最近」，故此处按时间升序回填。
    attempt_rows = list(reversed(attempt_rows))

    log_rows = conn.execute(
        "SELECT * FROM behavior_log WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, _RECENT_LIMIT),
    ).fetchall()

    state_rows = conn.execute(
        "SELECT * FROM review_state WHERE user_id = ? AND status = 'active'",
        (user_id,),
    ).fetchall()

    recent_attempts = [_row_to_attempt_view(row) for row in attempt_rows]
    recent_logs = [_row_to_behavior_view(row) for row in log_rows]
    states = [_row_to_state_view(row) for row in state_rows]

    strategy = compute_strategy(profile, recent_logs, recent_attempts, states)

    now_iso = to_iso(utc_now())
    with transaction(conn):
        conn.execute(
            "INSERT INTO strategy_snapshot (user_id, payload, rationale, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                user_id,
                json.dumps(strategy.model_dump(mode="json"), ensure_ascii=False),
                "\n".join(strategy.rationale),
                now_iso,
            ),
        )
    return strategy


# ---------------------------------------------------------------------------
# 端点 7：GET /api/profile/analytics
# ---------------------------------------------------------------------------


@profile_router.get(
    "/analytics",
    response_model=AnalyticsView,
    summary="学习进度统计",
    description="返回最近 days 天的连续打卡、作答正确率、待复习数与按日/按章节统计。",
)
def get_analytics(
    days: int = Query(7, ge=1, le=365, description="统计窗口（天，含今日）"),
    user_id: str = Depends(get_user_id),
    conn: sqlite3.Connection = Depends(get_db),
) -> AnalyticsView:
    _ensure_profile(conn, user_id)
    now = utc_now()
    today_local = now.astimezone().date()
    window_dates = [today_local - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
    window_set = set(window_dates)

    attempt_rows = conn.execute(
        "SELECT * FROM quiz_attempt WHERE user_id = ?", (user_id,)
    ).fetchall()
    log_rows = conn.execute(
        "SELECT * FROM behavior_log WHERE user_id = ?", (user_id,)
    ).fetchall()
    state_rows = conn.execute(
        "SELECT * FROM review_state WHERE user_id = ? AND status = 'active'",
        (user_id,),
    ).fetchall()

    day_attempts: dict[Any, int] = {day: 0 for day in window_dates}
    day_correct: dict[Any, int] = {day: 0 for day in window_dates}
    day_kps: dict[Any, set[str]] = {day: set() for day in window_dates}
    active_dates: set[Any] = set()

    window_attempts = 0
    window_correct = 0
    for row in attempt_rows:
        day = _local_date(row["created_at"])
        if day is None:
            continue
        active_dates.add(day)
        if day in window_set:
            window_attempts += 1
            day_attempts[day] += 1
            if row["is_correct"]:
                window_correct += 1
                day_correct[day] += 1
            day_kps[day].add(row["kp_id"])

    for row in log_rows:
        day = _local_date(row["created_at"])
        if day is None:
            continue
        active_dates.add(day)
        kp_id = row["kp_id"]
        if day in window_set and kp_id:
            day_kps[day].add(kp_id)

    # 按日统计（以次数 * 1 分钟兜底估计：无知识点元数据时按 1 分钟/知识点计）。
    from backend.app.data.content_loader import get_repository

    repo = get_repository()
    by_day: list[ByDayEntry] = []
    total_minutes = 0
    for day in window_dates:
        minutes = 0
        for kp_id in day_kps[day]:
            meta = repo.get_knowledge_point_meta(kp_id) if repo is not None else None
            minutes += meta.est_minutes if meta is not None else 1
        total_minutes += minutes
        by_day.append(
            ByDayEntry(date=day.isoformat(), attempts=day_attempts[day], minutes=minutes)
        )

    # 连续打卡：以「今日或昨日起往前」的连续活跃日计。
    streak = 0
    cursor = today_local
    if cursor not in active_dates:
        cursor = today_local - timedelta(days=1)
    while cursor in active_dates:
        streak += 1
        cursor -= timedelta(days=1)

    kps_in_review = len(state_rows)
    review_due_today = sum(
        1 for row in state_rows if is_due_by_end_of_local_day(str(row["due_at"]), now)
    )

    chapter_accum: dict[str, list[float]] = {}
    for row in state_rows:
        chapter_accum.setdefault(str(row["chapter_id"]), []).append(float(row["mastery"]))
    mastery_by_chapter = [
        MasteryByChapter(
            chapter_id=chapter_id,
            avg_mastery=round(sum(values) / len(values), 3),
        )
        for chapter_id, values in sorted(chapter_accum.items())
        if values
    ]

    correct_rate = round(window_correct / window_attempts, 3) if window_attempts else 0.0

    return AnalyticsView(
        streak_days=streak,
        total_minutes_estimate=total_minutes,
        attempts=window_attempts,
        correct_rate=correct_rate,
        kps_in_review=kps_in_review,
        review_due_today=review_due_today,
        by_day=by_day,
        mastery_by_chapter=mastery_by_chapter,
    )


# ---------------------------------------------------------------------------
# 端点 8：POST /api/log/behavior
# ---------------------------------------------------------------------------


@log_router.post(
    "/behavior",
    response_model=BehaviorLogView,
    status_code=201,
    summary="写入行为日志",
    description="记录前端行为事件（kp_view/session_start 等）；非法 action_type 返回 422。",
    responses={422: {"description": "action_type 非法（INVALID_ACTION_TYPE）"}},
)
def log_behavior(
    payload: BehaviorRequest,
    user_id: str = Depends(get_user_id),
    conn: sqlite3.Connection = Depends(get_db),
) -> BehaviorLogView:
    if payload.action_type not in ALLOWED_ACTION_TYPES:
        raise ApiError(
            422,
            "INVALID_ACTION_TYPE",
            f"action_type 非法：{payload.action_type}",
            {"allowed": sorted(ALLOWED_ACTION_TYPES)},
        )
    return record_behavior(
        conn,
        user_id,
        payload.action_type,
        payload.kp_id,
        payload.payload,
    )


# ---------------------------------------------------------------------------
# 行 -> 视图 映射（供 strategy 端点组装 compute_strategy 入参）
# ---------------------------------------------------------------------------


def _parse_user_answer(raw: Any) -> str | list[str]:
    """解析 ``quiz_attempt.user_answer``：数组形式为多选答案，其余按原文字符串处理。"""
    if isinstance(raw, str) and raw.lstrip().startswith("["):
        parsed = _safe_json_loads(raw, raw)
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    if isinstance(raw, (list, tuple)):
        return [str(item) for item in raw]
    return raw if isinstance(raw, str) else str(raw)


def _row_to_attempt_view(row: sqlite3.Row) -> QuizAttemptView:
    """将 ``quiz_attempt`` 行映射为 :class:`QuizAttemptView`。"""
    return QuizAttemptView(
        id=int(row["id"]),
        kp_id=row["kp_id"],
        qid=row["qid"],
        question_type=row["question_type"],
        user_answer=_parse_user_answer(row["user_answer"]),
        score=int(row["score"]),
        is_correct=bool(row["is_correct"]),
        keyword_hits=_safe_json_loads(row["keyword_hits"], []) or [],
        graded_by=row["graded_by"],
        feedback=row["feedback"] or "",
        created_at=row["created_at"],
    )


def _row_to_behavior_view(row: sqlite3.Row) -> BehaviorLogView:
    """将 ``behavior_log`` 行映射为 :class:`BehaviorLogView`。"""
    return BehaviorLogView(
        id=int(row["id"]),
        user_id=row["user_id"],
        action_type=row["action_type"],
        kp_id=row["kp_id"],
        payload=_safe_json_loads(row["payload"], {}),
        created_at=row["created_at"],
    )


def _row_to_state_view(row: sqlite3.Row) -> ReviewStateView:
    """将 ``review_state`` 行映射为 :class:`ReviewStateView`。"""
    return ReviewStateView(
        kp_id=row["kp_id"],
        chapter_id=row["chapter_id"],
        status=row["status"],
        level=int(row["level"]),
        interval_days=int(row["interval_days"]),
        due_at=row["due_at"],
        last_reviewed_at=row["last_reviewed_at"],
        review_count=int(row["review_count"]),
        lapses=int(row["lapses"]),
        mastery=float(row["mastery"]),
    )


__all__ = ["router", "profile_router", "log_router"]