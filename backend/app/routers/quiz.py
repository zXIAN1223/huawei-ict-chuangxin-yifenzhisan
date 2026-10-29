"""主动回忆问答与判分 API（契约 C3-QUIZ@1）。

本模块提供 4 个端点（统一前缀 ``/api/quiz``）：

===========================  =====================================================
方法 / 路径                    语义
===========================  =====================================================
GET  /{kp_id}                 取该知识点的题目（可按难度/重要性排序、可限量）
POST /{kp_id}/attempt         提交作答：判分 -> 落库 -> 联动复习状态
GET  /{kp_id}/attempts        该知识点的作答历史（近 N 条）
GET  /history                 当前用户的全部作答历史（近 N 条）
===========================  =====================================================

判分逻辑采用纯函数 :func:`grade_answer`（四题型规则严格遵循 plan 2.2.3），
不依赖 DB / 网络，便于单元测试与复用。

跨契约联动：

* **落库**：每次作答写入 ``quiz_attempt``（表结构由 T1 的 ``models`` 定义，本模块
  只做增删改查，不自建表）。
* **C2 联动**：若该 kp 已存在于 ``review_state``（任一 status），按
  ``score -> grade`` 映射调用 ``spaced_repetition.apply_grade`` 更新并回写；
  若 kp 不在复习库中，则**不创建** ``review_state``（仅记录作答）。
* **C5 行为日志**：作答记为 ``quiz_attempt`` 行为（``learning_strategy.record_behavior``）。
* **C4 AI 语义分**：简答题可选调用 ``AIAdapter.grade_short_answer`` 取语义分，
  以 0.4 权重叠加到关键词分之上。

容错与渐进式开发：上游 T5/T7/T8 模块尚未就绪时，本模块通过 ``try/except`` 降级
（AI 分为 ``None`` 即仅关键词判分；复习状态联动跳过；行为日志跳过），
保证端点本身始终可用，待上游就绪后自动生效。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from backend.app.config import get_settings
from backend.app.data.content_loader import ContentRepository
from backend.app.db import to_iso, transaction, utc_now
from backend.app.deps import ApiError, get_content_repo, get_db, get_user_id
from backend.app.models import (
    GradedBy,
    QuizAttemptView,
    QuizItem,
    QuizType,
    ReviewState,
    ReviewStateView,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 上游契约的可选依赖（未就绪时降级，不阻塞本模块加载）
# ---------------------------------------------------------------------------
try:  # pragma: no cover - 依赖 T5 交付
    from backend.app.services.spaced_repetition import apply_grade as _apply_grade
except Exception:  # noqa: BLE001 - 任何导入错误都降级
    _apply_grade = None

try:  # pragma: no cover - 依赖 T8 交付
    from backend.app.services.learning_strategy import record_behavior as _record_behavior
except Exception:  # noqa: BLE001
    _record_behavior = None

try:  # pragma: no cover - 依赖 T7 交付
    from backend.app.services.ai_adapter import AIAdapter as _AIAdapter
    from backend.app.services.ai_adapter import AIConfig as _AIConfig
except Exception:  # noqa: BLE001
    _AIAdapter = None
    _AIConfig = None


# ---------------------------------------------------------------------------
# 常量与判分纯函数（C3 判分契约）
# ---------------------------------------------------------------------------

#: 及格阈值：``is_correct = score >= PASS_THRESHOLD``
PASS_THRESHOLD = 60

#: T7 在判分文本中标注的 AI 部分分（0..1），形如「AI评分：0.85」（见 ai_adapter.grade 注释）
_AI_SCORE_RE = re.compile(r"AI\s*评分\s*[:：]\s*(\d+(?:\.\d+)?)")
#: 兜底：结构化评分字段 ``{"score": <number>}``（0..1 或 0..100）
_JSON_SCORE_RE = re.compile(r'"?score"?\s*[:：]\s*(\d+(?:\.\d+)?)')


def normalize(text: str) -> str:
    """归一化文本：``casefold`` + 去除所有空白字符。

    用于选择/判断/简答关键词的字面匹配，消除大小写与空格差异。
    """
    if text is None:
        return ""
    return "".join(str(text).casefold().split())


def _as_text(value: Any) -> str:
    """将用户答案收敛为单个文本（用于 single_choice / true_false / short_answer）。

    数组取其首个元素；``None`` 视为空串。
    """
    if isinstance(value, list):
        return str(value[0]) if value else ""
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _as_list(value: Any) -> list[str]:
    """将答案收敛为字符串列表（用于 multi_choice）。

    字符串且非空白 -> 单元素列表；数组 -> 逐项转字符串；``None``/空 -> 空列表。
    """
    if isinstance(value, list):
        return [str(item) for item in value]
    if value is None:
        return []
    text = str(value)
    return [text] if text.strip() else []


class GradeResult(BaseModel):
    """单题判分结果（C3-QUIZ@1）。"""

    qid: str
    question_type: QuizType
    score: int
    is_correct: bool
    keyword_hits: list[str] = []
    expected_answer: str | list[str]
    explanation: str
    graded_by: GradedBy
    feedback: str = ""


def _build_feedback(item: QuizItem, hits: list[str]) -> str:
    """由解析与原题关键词拼装反馈文本。

    简答题在解析之后追加「未命中关键词」提示；其它题型直接返回解析。
    """
    explanation = item.explanation or ""
    if item.type == "short_answer":
        keywords = list(item.keywords or [])
        missed = [kw for kw in keywords if kw not in hits]
        if missed:
            return f"{explanation}（未命中关键词：{'、'.join(missed)}）"
    return explanation


def grade_answer(
    item: QuizItem,
    user_answer: str | list[str],
    ai_score: int | None = None,
) -> GradeResult:
    """对单个题目判分（纯函数，严格遵循 plan 2.2.3 规则表）。

    规则：

    * ``single_choice`` / ``true_false``：归一化后相等 -> 100，否则 0。
    * ``multi_choice``：集合全等 -> 100；否则
      ``recall = |∩| / |expected|``、``precision = |∩| / |got|``（``got`` 为空则为 0），
      ``score = round(100 * (recall + precision) / 2)``。
    * ``short_answer``：命中关键词数占比 ``kw_score``；若提供 ``ai_score``，
      则 ``score = round(0.6 * kw_score + 0.4 * ai_score)``（``graded_by="local+ai"``），
      否则 ``score = kw_score``（``graded_by="local"``）。

    ``is_correct = score >= PASS_THRESHOLD``。
    """
    qtype = item.type
    hits: list[str] = []
    graded_by: GradedBy = "local"

    if qtype in ("single_choice", "true_false"):
        expected_text = _as_text(item.answer)
        got_text = _as_text(user_answer)
        score = 100 if normalize(got_text) == normalize(expected_text) else 0

    elif qtype == "multi_choice":
        # 契约字面：expected = set(answer)、got = set(user_answer)。
        expected = set(_as_list(item.answer))
        got = set(_as_list(user_answer))
        if expected == got:
            score = 100
        else:
            intersection = expected & got
            recall = len(intersection) / len(expected) if expected else 0.0
            precision = len(intersection) / len(got) if got else 0.0
            score = round(100 * (recall + precision) / 2)

    else:  # short_answer
        keywords = list(item.keywords or [])
        user_text = normalize(_as_text(user_answer))
        hits = [kw for kw in keywords if normalize(kw) in user_text]
        kw_score = round(100 * len(hits) / len(keywords)) if keywords else 0
        if ai_score is not None:
            ai = max(0, min(100, int(ai_score)))
            score = round(0.6 * kw_score + 0.4 * ai)
            graded_by = "local+ai"
        else:
            score = kw_score
            graded_by = "local"

    score = max(0, min(100, int(score)))
    return GradeResult(
        qid=item.qid,
        question_type=qtype,
        score=score,
        is_correct=score >= PASS_THRESHOLD,
        keyword_hits=hits,
        expected_answer=item.answer,
        explanation=item.explanation or "",
        graded_by=graded_by,
        feedback=_build_feedback(item, hits),
    )


def _score_to_grade(score: int) -> int:
    """将 0..100 的得分映射为 C2 的 0..5 复习等级（plan 2.2.3）。"""
    if score >= 90:
        return 5
    if score >= 80:
        return 4
    if score >= 60:
        return 3
    if score >= 40:
        return 2
    if score >= 20:
        return 1
    return 0


# ---------------------------------------------------------------------------
# 请求 / 响应模型
# ---------------------------------------------------------------------------


class AttemptRequest(BaseModel):
    """``POST /{kp_id}/attempt`` 请求体。"""

    qid: str
    user_answer: str | list[str]


class AttemptResponse(BaseModel):
    """``POST /{kp_id}/attempt`` 响应体。"""

    grade: GradeResult
    review_state: ReviewStateView | None = None


# ---------------------------------------------------------------------------
# DB 辅助
# ---------------------------------------------------------------------------


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """兼容 Pydantic 模型与 dict 的字段读取。"""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _row_to_attempt_view(row: sqlite3.Row) -> QuizAttemptView:
    """将 ``quiz_attempt`` 行转换为视图模型。"""
    user_answer = json.loads(row["user_answer"]) if row["user_answer"] else ""
    keyword_hits = json.loads(row["keyword_hits"]) if row["keyword_hits"] else []
    return QuizAttemptView(
        id=row["id"],
        kp_id=row["kp_id"],
        qid=row["qid"],
        question_type=row["question_type"],
        user_answer=user_answer,
        score=row["score"],
        is_correct=bool(row["is_correct"]),
        keyword_hits=keyword_hits,
        graded_by=row["graded_by"],
        feedback=row["feedback"] or "",
        created_at=row["created_at"],
    )


def _row_to_review_state(row: sqlite3.Row) -> ReviewState:
    """将 ``review_state`` 行转换为 C2 的 ``ReviewState`` 模型。"""
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


def _state_view(state: Any) -> ReviewStateView:
    """从 ``ReviewState``（或其等价 dict）构造 ``ReviewStateView``。"""
    return ReviewStateView(
        kp_id=_get(state, "kp_id"),
        chapter_id=_get(state, "chapter_id"),
        status=_get(state, "status"),
        level=_get(state, "level"),
        interval_days=_get(state, "interval_days"),
        due_at=_get(state, "due_at"),
        last_reviewed_at=_get(state, "last_reviewed_at"),
        review_count=_get(state, "review_count"),
        lapses=_get(state, "lapses"),
        mastery=_get(state, "mastery"),
    )


def _insert_attempt(
    conn: sqlite3.Connection,
    user_id: str,
    kp_id: str,
    item: QuizItem,
    user_answer: str | list[str],
    result: GradeResult,
    now_iso: str,
) -> None:
    """写入一条 ``quiz_attempt``（调用方需处于事务中）。"""
    conn.execute(
        "INSERT INTO quiz_attempt "
        "(user_id, kp_id, qid, question_type, user_answer, score, is_correct, "
        " keyword_hits, graded_by, feedback, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            user_id,
            kp_id,
            item.qid,
            item.type,
            json.dumps(user_answer, ensure_ascii=False),
            result.score,
            1 if result.is_correct else 0,
            json.dumps(result.keyword_hits, ensure_ascii=False),
            result.graded_by,
            result.feedback,
            now_iso,
        ),
    )


def _update_review_state(conn: sqlite3.Connection, state: Any) -> None:
    """将更新后的 ``ReviewState`` 回写 ``review_state`` 表（调用方需处于事务中）。"""
    conn.execute(
        "UPDATE review_state SET "
        "chapter_id=?, status=?, level=?, interval_days=?, ease=?, due_at=?, "
        "last_reviewed_at=?, review_count=?, lapses=?, mastery=?, updated_at=? "
        "WHERE user_id=? AND kp_id=?",
        (
            _get(state, "chapter_id"),
            _get(state, "status"),
            _get(state, "level"),
            _get(state, "interval_days"),
            _get(state, "ease"),
            _get(state, "due_at"),
            _get(state, "last_reviewed_at"),
            _get(state, "review_count"),
            _get(state, "lapses"),
            _get(state, "mastery"),
            _get(state, "updated_at"),
            _get(state, "user_id"),
            _get(state, "kp_id"),
        ),
    )


def _correct_ratio(conn: sqlite3.Connection, user_id: str, kp_id: str) -> float:
    """计算该用户在该知识点上的作答正确率（含本次），无历史时为 0.0。

    用途：供 C2 ``apply_grade`` 计算 ``mastery``（契约 2.2.2）。
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(is_correct), 0) AS c "
        "FROM quiz_attempt WHERE user_id = ? AND kp_id = ?",
        (user_id, kp_id),
    ).fetchone()
    total = int(row["n"] or 0)
    if total <= 0:
        return 0.0
    return int(row["c"] or 0) / total


def _sync_review_state(
    conn: sqlite3.Connection,
    user_id: str,
    kp_id: str,
    score: int,
    now: Any,
    difficulty: int,
    importance: int,
) -> ReviewStateView | None:
    """按 ``score -> grade`` 联动更新 C2 复习状态。

    * kp 不在 ``review_state`` 中 -> 返回 ``None``（**不创建**）。
    * kp 已存在 -> 调用 ``spaced_repetition.apply_grade`` 更新并回写，且以知识点真实
      ``difficulty``/``importance`` 与作答历史 ``correct_ratio`` 参与间隔/mastery 计算；
      T5 尚未就绪或调用失败时，返回当前状态视图（不推进曲线）并记录 warning。
    """
    row = conn.execute(
        "SELECT * FROM review_state WHERE user_id = ? AND kp_id = ?",
        (user_id, kp_id),
    ).fetchone()
    if row is None:
        return None

    state = _row_to_review_state(row)
    if _apply_grade is None:
        logger.warning("C2 spaced_repetition.apply_grade 尚未就绪，跳过复习状态推进：%s", kp_id)
        return _state_view(state)

    grade = _score_to_grade(score)
    correct_ratio = _correct_ratio(conn, user_id, kp_id)
    try:
        try:
            updated = _apply_grade(
                state,
                grade,
                now,
                difficulty=difficulty,
                importance=importance,
                correct_ratio=correct_ratio,
            )
        except TypeError:
            # 兼容仅支持 (state, grade, now) 的旧签名
            updated = _apply_grade(state, grade, now)
    except Exception:  # noqa: BLE001 - 联动失败不应使作答失败
        logger.exception("调用 C2 apply_grade 失败，保持原复习状态：%s", kp_id)
        return _state_view(state)

    if updated is None:
        return _state_view(state)

    _update_review_state(conn, updated)
    return _state_view(updated)


def _log_behavior(
    conn: sqlite3.Connection,
    user_id: str,
    kp_id: str,
    item: QuizItem,
    result: GradeResult,
) -> None:
    """写入 C5 行为日志 ``quiz_attempt``（T8 未就绪或失败时静默跳过）。"""
    if _record_behavior is None:
        return
    try:
        _record_behavior(
            conn,
            user_id,
            "quiz_attempt",
            kp_id,
            {
                "qid": item.qid,
                "question_type": item.type,
                "score": result.score,
                "is_correct": result.is_correct,
                "graded_by": result.graded_by,
            },
        )
    except Exception:  # noqa: BLE001 - 行为日志失败不影响作答
        logger.warning("记录 quiz_attempt 行为日志失败：%s", kp_id, exc_info=True)


def _to_score_100(value: float) -> int | None:
    """将 AI 原始分归一化到 0..100。

    T7 的判分文本使用 0..1 的「AI 部分分」，故 ``<= 1`` 视为比例分并乘 100；
    ``(1, 100]`` 视为已是百分制。越界返回 ``None``。
    """
    if 0.0 <= value <= 1.0:
        return round(value * 100)
    if 0.0 <= value <= 100.0:
        return round(value)
    return None


def _extract_ai_score(text: str | None) -> int | None:
    """从 T7 判分文本中提取 AI 语义分并归一化为 0..100；无法解析返回 ``None``。

    优先解析 ``AI评分：0.xx`` 标注，其次兜底解析结构化 ``score`` 字段。
    显式定界可避免把反馈文本中的普通数字（如「命中 6/6」的 6）误当作分数。
    """
    if not text:
        return None
    raw = str(text)
    match = _AI_SCORE_RE.search(raw)
    if match:
        return _to_score_100(float(match.group(1)))
    match = _JSON_SCORE_RE.search(raw)
    if match:
        return _to_score_100(float(match.group(1)))
    return None


async def _try_ai_score(item: QuizItem, user_answer: str | list[str]) -> int | None:
    """简答题调用 C4 ``AIAdapter.grade_short_answer`` 取语义分。

    T7 未就绪、调用异常或无法解析分数时返回 ``None``（回退仅关键词判分）。
    """
    if _AIAdapter is None or _AIConfig is None:
        return None
    try:
        settings = get_settings()
        config = _AIConfig(
            api_url=settings.pangu_api_url,
            api_key=settings.pangu_api_key,
            model=settings.pangu_model,
            timeout=settings.pangu_timeout,
        )
        adapter = _AIAdapter(config)
        reference = item.answer if isinstance(item.answer, str) else "；".join(item.answer)
        result = await adapter.grade_short_answer(
            item.question,
            reference,
            list(item.keywords or []),
            _as_text(user_answer),
        )
        return _extract_ai_score(getattr(result, "text", None))
    except Exception:  # noqa: BLE001 - AI 不可用则回退本地判分
        logger.warning("AI 简答评分不可用，回退本地关键词判分：%s", item.qid, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/quiz", tags=["quiz"])


def _kp_not_found(kp_id: str) -> ApiError:
    return ApiError(404, "KP_NOT_FOUND", f"知识点不存在：{kp_id}", {"kp_id": kp_id})


def _quiz_item_not_found(kp_id: str, qid: str) -> ApiError:
    return ApiError(
        404,
        "QUIZ_ITEM_NOT_FOUND",
        f"题目不存在：{qid}",
        {"kp_id": kp_id, "qid": qid},
    )


# 注意：``/history`` 必须注册在 ``/{kp_id}`` 之前，否则会被当作 kp_id 捕获。
@router.get(
    "/history",
    response_model=list[QuizAttemptView],
    summary="作答历史（当前用户）",
    description="返回当前用户最近的作答记录，按时间倒序，默认 50 条。",
)
def get_history(
    limit: int = Query(50, ge=1, le=500, description="返回条数上限"),
    user_id: str = Depends(get_user_id),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[QuizAttemptView]:
    rows = conn.execute(
        "SELECT * FROM quiz_attempt WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    return [_row_to_attempt_view(row) for row in rows]


@router.get(
    "/{kp_id}/attempts",
    response_model=list[QuizAttemptView],
    summary="作答历史（指定知识点）",
    description="返回当前用户在指定知识点的作答记录，按时间倒序，默认 20 条。",
)
def list_attempts(
    kp_id: str,
    limit: int = Query(20, ge=1, le=500, description="返回条数上限"),
    user_id: str = Depends(get_user_id),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[QuizAttemptView]:
    rows = conn.execute(
        "SELECT * FROM quiz_attempt WHERE user_id = ? AND kp_id = ? "
        "ORDER BY id DESC LIMIT ?",
        (user_id, kp_id, limit),
    ).fetchall()
    return [_row_to_attempt_view(row) for row in rows]


@router.post(
    "/{kp_id}/attempt",
    response_model=AttemptResponse,
    summary="提交作答并判分",
    description=(
        "对指定知识点的题目判分、落库，并在该 kp 已加入复习时按分数映射更新复习状态。"
    ),
    responses={
        404: {"description": "知识点不存在（KP_NOT_FOUND）或题目不存在（QUIZ_ITEM_NOT_FOUND）"}
    },
)
async def submit_attempt(
    kp_id: str,
    payload: AttemptRequest,
    user_id: str = Depends(get_user_id),
    conn: sqlite3.Connection = Depends(get_db),
    repo: ContentRepository = Depends(get_content_repo),
) -> AttemptResponse:
    kp = repo.get_knowledge_point(kp_id)
    if kp is None:
        raise _kp_not_found(kp_id)

    item = next((q for q in kp.quiz if q.qid == payload.qid), None)
    if item is None:
        raise _quiz_item_not_found(kp_id, payload.qid)

    # 简答题可选叠加 AI 语义分；其它题型忽略 ai_score。
    ai_score = None
    if item.type == "short_answer":
        ai_score = await _try_ai_score(item, payload.user_answer)

    result = grade_answer(item, payload.user_answer, ai_score)
    now = utc_now()
    now_iso = to_iso(now)

    with transaction(conn):
        _insert_attempt(conn, user_id, kp_id, item, payload.user_answer, result, now_iso)
        review_view = _sync_review_state(
            conn, user_id, kp_id, result.score, now, kp.difficulty, kp.importance
        )

    # 行为日志在业务事务之外写入，避免与其内部事务冲突。
    _log_behavior(conn, user_id, kp_id, item, result)

    return AttemptResponse(grade=result, review_state=review_view)


@router.get(
    "/{kp_id}",
    response_model=list[QuizItem],
    summary="获取知识点题目",
    description=(
        "返回指定知识点的题组；``order`` 可指定排序字段（``difficulty``/``importance``，"
        "降序优先），``limit`` 限定返回数量（缺省返回全部）。"
    ),
    responses={404: {"description": "知识点不存在（KP_NOT_FOUND）"}},
)
def get_quiz(
    kp_id: str,
    limit: int | None = Query(None, ge=1, description="返回题目数量上限，缺省为全部"),
    order: Literal["difficulty", "importance"] | None = Query(
        None, description="排序字段：difficulty 或 importance（降序）"
    ),
    repo: ContentRepository = Depends(get_content_repo),
) -> list[QuizItem]:
    kp = repo.get_knowledge_point(kp_id)
    if kp is None:
        raise _kp_not_found(kp_id)

    items = list(kp.quiz)
    if order is not None:
        # 稳定排序：按目标字段降序，保持同值题的原始顺序。
        items.sort(key=lambda q: getattr(q, order), reverse=True)
    if limit is not None:
        items = items[:limit]
    return items


__all__ = [
    "router",
    "PASS_THRESHOLD",
    "normalize",
    "grade_answer",
    "GradeResult",
    "AttemptRequest",
    "AttemptResponse",
]