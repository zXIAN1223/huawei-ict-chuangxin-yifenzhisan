"""问卷评分、VARK 测评、行为日志与自适应策略计算（契约 C5-PROFILE@1，plan 2.2.5）。

本模块为**纯业务逻辑 + 少量数据库写入**，不含 HTTP：

* :func:`evaluate_assessment` —— 依据问卷作答计算 VARK 学习风格与推荐学习方式；
* :func:`compute_strategy` —— 依据画像、最近作答与复习状态生成自适应策略；
* :func:`record_behavior` —— 向 ``behavior_log`` 写入一条行为日志并返回视图；
* :func:`profile_summary` —— 为 C4 聊天（T7）提供轻量画像摘要。

常量与规则严格对应 plan 2.2.5「测评与策略契约」的规则表。当数据缺失时一律使用
安全默认值（不抛错），以保证前端在问卷/测评完成前也能获得可用响应。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from backend.app.config import DATA_DIR
from backend.app.db import to_iso, transaction, utc_now
from backend.app.models import (
    BehaviorLogView,
    QuizAttemptView,
    ReviewStateView,
    UserProfileModel,
)

logger = logging.getLogger(__name__)

__all__ = [
    "LearningStyle",
    "AssessmentResult",
    "AdaptiveStrategy",
    "QUESTIONNAIRE_FILENAME",
    "STYLE_PRIORITY",
    "DIMENSION_BY_CODE",
    "CODE_BY_DIMENSION",
    "recommended_modes_for_style",
    "evaluate_assessment",
    "compute_strategy",
    "record_behavior",
    "profile_summary",
]

# ---------------------------------------------------------------------------
# 常量与类型（冻结契约，plan 2.2.5）
# ---------------------------------------------------------------------------

LearningStyle = Literal["visual", "auditory", "reading", "kinesthetic", "mixed"]

#: 问卷文件名（相对 ``config.DATA_DIR``）。
QUESTIONNAIRE_FILENAME = "questionnaire.json"

#: 并列时的维度优先序（plan 2.2.5：visual > auditory > reading > kinesthetic）。
STYLE_PRIORITY: tuple[str, ...] = ("visual", "auditory", "reading", "kinesthetic")

#: style 选项 value 到维度的映射。
DIMENSION_BY_CODE: dict[str, str] = {
    "v": "visual",
    "a": "auditory",
    "r": "reading",
    "k": "kinesthetic",
}

#: 维度到 style 选项 value 的反向映射。
CODE_BY_DIMENSION: dict[str, str] = {v: k for k, v in DIMENSION_BY_CODE.items()}

#: 各维度的中文标签（用于 rationale）。
STYLE_LABELS: dict[str, str] = {
    "visual": "视觉型",
    "auditory": "听觉型",
    "reading": "读写型",
    "kinesthetic": "动觉型",
    "mixed": "综合型",
}

#: 各风格的基础推荐学习方式（恒追加 ``feynman``，见 :func:`recommended_modes_for_style`）。
_BASE_MODES: dict[str, list[str]] = {
    "visual": ["flashcard_visual", "mindmap"],
    "auditory": ["chat_teacher", "listen_summary"],
    "reading": ["read_summary", "note_taking"],
    "kinesthetic": ["feynman", "recall_practice"],
    "mixed": ["mindmap", "recall_practice", "feynman"],
}

#: 复习强度分档阈值与倍率（plan 2.2.5 复习强度规则）。
_HIGH_MULTIPLIER = 1.5
_MEDIUM_MULTIPLIER = 1.2
_DEFAULT_MULTIPLIER = 1.0
_MULTIPLIER_MEDIUM_THRESHOLD = 0.6
_MULTIPLIER_HIGH_THRESHOLD = 0.8

#: 会话时长边界（分钟）。
_SESSION_MIN = 10
_SESSION_MAX = 30
_KINESTHETIC_PENALTY = 5

#: 复习强度评估所用的最近作答次数上限。
_RECENT_ATTEMPT_LIMIT = 20


# ---------------------------------------------------------------------------
# 视图模型
# ---------------------------------------------------------------------------


class AssessmentResult(BaseModel):
    """测评结果（``POST /api/profile/questionnaire`` 与 ``/assessment`` 响应）。"""

    learning_style: LearningStyle
    style_scores: dict[str, float] = Field(default_factory=dict)
    recommended_modes: list[str] = Field(default_factory=list)
    rationale: list[str] = Field(default_factory=list)


class AdaptiveStrategy(BaseModel):
    """自适应策略（``GET /api/profile/strategy`` 响应）。"""

    recommended_modes: list[str] = Field(default_factory=list)
    session_length_min: int
    review_intensity_multiplier: float
    daily_minutes_effective: int
    rationale: list[str] = Field(default_factory=list)
    generated_at: str


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _utc_iso(value: datetime | None = None) -> str:
    """返回 UTC ISO-8601 字符串。"""
    return to_iso(value or utc_now())


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """兼容 Pydantic 模型与 dict 的字段读取。"""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _load_questionnaire_raw() -> dict[str, Any] | None:
    """读取 ``data/questionnaire.json``；不可用时返回 ``None``（不抛错）。"""
    path = DATA_DIR / QUESTIONNAIRE_FILENAME
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:  # 文件缺失/非法均降级
        logger.warning("读取问卷文件失败（%s）：%s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def _style_questions() -> list[dict[str, Any]]:
    """返回问卷中 ``dimension=="style"`` 的题目列表。"""
    data = _load_questionnaire_raw()
    if not data:
        return []
    questions = data.get("questions")
    if not isinstance(questions, list):
        return []
    return [
        question
        for question in questions
        if isinstance(question, dict) and question.get("dimension") == "style"
    ]


def _as_answer_values(answer: Any) -> list[str]:
    """将单值/多值/数值作答统一为字符串列表。"""
    if answer is None:
        return []
    if isinstance(answer, (list, tuple, set)):
        return [str(item) for item in answer]
    return [str(answer)]


def recommended_modes_for_style(style: str) -> list[str]:
    """按主导风格返回推荐学习方式，恒追加 ``feynman`` 并去重（保持顺序）。

    规则（plan 2.2.5）：所有风格恒追加 ``"feynman"``（学习金字塔「教授他人」）。
    ``kinesthetic`` 与 ``mixed`` 的基础列表已含 ``feynman``，去重后不重复。
    """
    base = list(_BASE_MODES.get(style, _BASE_MODES["mixed"]))
    if "feynman" not in base:
        base.append("feynman")
    # 保序去重
    seen: set[str] = set()
    result: list[str] = []
    for mode in base:
        if mode not in seen:
            seen.add(mode)
            result.append(mode)
    return result


# ---------------------------------------------------------------------------
# VARK 计分
# ---------------------------------------------------------------------------


def _max_obtainable_by_dimension(questions: list[dict[str, Any]]) -> dict[str, int]:
    """计算每个维度在问卷中「最大可得」原始分。

    * ``single_choice``：该题含某维度的选项时，最多贡献 1 分（一次只能选一个）；
    * ``multi_choice``：最多贡献「该题中该维度选项的数量」（可全选）；
    * 其它题型（如 scale）不计入 VARK。
    """
    maxima: dict[str, int] = {dim: 0 for dim in STYLE_PRIORITY}
    for question in questions:
        options = question.get("options")
        if not isinstance(options, list):
            continue
        counts: dict[str, int] = {}
        for option in options:
            if not isinstance(option, dict):
                continue
            dimension = DIMENSION_BY_CODE.get(str(option.get("value")))
            if dimension is not None:
                counts[dimension] = counts.get(dimension, 0) + 1
        is_multi = question.get("type") == "multi_choice"
        for dimension, count in counts.items():
            maxima[dimension] += count if is_multi else 1
    return maxima


def _pick_dominant(normalized: dict[str, float]) -> str:
    """按归一化分挑选主导维度；并列时按 ``STYLE_PRIORITY`` 优先。全 0 返回 ``mixed``。"""
    if not normalized or max(normalized.values()) <= 0.0:
        return "mixed"
    for dimension in STYLE_PRIORITY:  # 优先序即遍历序
        if normalized.get(dimension, 0.0) == max(normalized.values()):
            return dimension
    return "mixed"


def evaluate_assessment(answers: dict[str, Any]) -> AssessmentResult:
    """依据问卷作答计算 VARK 学习风格与推荐学习方式。

    计分（plan 2.2.5）：

    * 每选中一个 ``style`` 选项，对应维度 +1；
    * ``style_scores`` 归一化为「维度得分 / 该维度最大可得」，∈ ``[0, 1]``；
    * 主导 = 归一化最高者；并列时优先序 ``visual > auditory > reading > kinesthetic``；
    * 全部为 0 或无 style 作答时 ``learning_style == "mixed"``。

    :param answers: ``{qid: value}``，其中 style 题的 value 为 option.value（``v/a/r/k``），
        多选题为 ``list[str]``，量表题为 ``int``。非 style 题与未知 qid 一律忽略。
    """
    answers = answers or {}
    style_questions = _style_questions()
    maxima = _max_obtainable_by_dimension(style_questions)

    raw_scores: dict[str, int] = {dim: 0 for dim in STYLE_PRIORITY}
    for question in style_questions:
        qid = str(question.get("qid"))
        if qid not in answers:
            continue
        for value in _as_answer_values(answers.get(qid)):
            dimension = DIMENSION_BY_CODE.get(value)
            if dimension is not None:
                raw_scores[dimension] += 1

    # 归一化：优先使用「该维度最大可得」；若问卷缺失致分母全 0，则退化为按最大原始分归一。
    fallback_denominator = max(raw_scores.values()) if any(raw_scores.values()) else 0
    normalized: dict[str, float] = {}
    for dimension in STYLE_PRIORITY:
        denominator = maxima.get(dimension, 0)
        if denominator <= 0:
            denominator = fallback_denominator
        normalized[dimension] = (
            round(raw_scores[dimension] / denominator, 3) if denominator > 0 else 0.0
        )

    learning_style = _pick_dominant(normalized)
    modes = recommended_modes_for_style(learning_style)

    rationale = [
        f"主导学习风格为「{STYLE_LABELS[learning_style]}」（{learning_style}）。",
        "各维度归一化得分：" + "、".join(
            f"{dimension} {normalized[dimension]:.2f}" for dimension in STYLE_PRIORITY
        )
        + "。",
        f"据此推荐学习方式：{'、'.join(modes)}（恒包含 feynman 教授他人环节）。",
    ]

    return AssessmentResult(
        learning_style=learning_style,  # type: ignore[arg-type]
        style_scores=normalized,
        recommended_modes=modes,
        rationale=rationale,
    )


# ---------------------------------------------------------------------------
# 自适应策略
# ---------------------------------------------------------------------------


def _review_intensity(attempts: list[Any]) -> tuple[float, str]:
    """依据最近 ≤20 次作答的正确率返回（倍率, 依据说明）。"""
    recent = list(attempts)[-_RECENT_ATTEMPT_LIMIT:] if attempts else []
    if not recent:
        return _DEFAULT_MULTIPLIER, "暂无作答记录，复习强度取默认倍率 1.0。"

    correct = sum(1 for attempt in recent if _field(attempt, "is_correct", False))
    ratio = correct / len(recent)
    if ratio < _MULTIPLIER_MEDIUM_THRESHOLD:
        multiplier = _HIGH_MULTIPLIER
    elif ratio < _MULTIPLIER_HIGH_THRESHOLD:
        multiplier = _MEDIUM_MULTIPLIER
    else:
        multiplier = _DEFAULT_MULTIPLIER

    reason = (
        f"最近 {len(recent)} 次作答正确率 {ratio:.0%}，"
        f"据此设置复习强度倍率 {multiplier}（越高复习越密集）。"
    )
    return multiplier, reason


def _session_length(learning_style: str, daily_minutes: int) -> int:
    """会话时长：``min(30, max(10, daily_minutes//2))``；动觉型再 -5（下限 10）。"""
    length = min(_SESSION_MAX, max(_SESSION_MIN, daily_minutes // 2))
    if learning_style == "kinesthetic":
        length = max(_SESSION_MIN, length - _KINESTHETIC_PENALTY)
    return length


def compute_strategy(
    profile: UserProfileModel,
    recent_logs: list[Any] | None = None,
    recent_attempts: list[QuizAttemptView] | None = None,
    states: list[ReviewStateView] | None = None,
) -> AdaptiveStrategy:
    """依据画像、最近行为/作答与复习状态生成自适应策略。

    规则（plan 2.2.5）：

    * 复习强度：最近 ≤20 次作答正确率 ``r``，``r<0.6→1.5``、``0.6≤r<0.8→1.2``、
      ``r≥0.8→1.0``，无数据 ``→1.0``；
    * 会话时长：``min(30, max(10, daily_minutes//2))``，``kinesthetic`` 再 ``-5``（下限 10）；
    * 有效预算：``round(daily_minutes / review_intensity_multiplier)``；
    * ``recommended_modes`` 依据主导学习风格（恒含 ``feynman``）。

    :param recent_logs: 预留的最近行为日志（当前不影响计算，供后续扩展）。
    :param states: 预留的复习状态列表（当前不影响计算，供后续扩展）。
    """
    learning_style = str(_field(profile, "learning_style", "mixed") or "mixed")
    try:
        daily_minutes = int(_field(profile, "daily_minutes", 30) or 0)
    except (TypeError, ValueError):
        daily_minutes = 30
    daily_minutes = max(0, daily_minutes)

    multiplier, intensity_reason = _review_intensity(list(recent_attempts or []))
    session_length_min = _session_length(learning_style, daily_minutes)
    daily_minutes_effective = int(round(daily_minutes / multiplier)) if multiplier else daily_minutes
    modes = recommended_modes_for_style(learning_style)

    rationale = [
        f"主导学习风格为「{STYLE_LABELS.get(learning_style, learning_style)}」"
        f"（{learning_style}），推荐学习方式：{'、'.join(modes)}。",
        intensity_reason,
        f"建议单次专注时长约 {session_length_min} 分钟。",
        f"综合复习强度后，每日有效学习预算约 {daily_minutes_effective} 分钟。",
    ]

    return AdaptiveStrategy(
        recommended_modes=modes,
        session_length_min=session_length_min,
        review_intensity_multiplier=multiplier,
        daily_minutes_effective=daily_minutes_effective,
        rationale=rationale,
        generated_at=_utc_iso(),
    )


# ---------------------------------------------------------------------------
# 行为日志
# ---------------------------------------------------------------------------


def record_behavior(
    conn: sqlite3.Connection,
    user_id: str,
    action_type: str,
    kp_id: str | None = None,
    payload: dict | None = None,
) -> BehaviorLogView:
    """向 ``behavior_log`` 写入一条行为日志并返回视图。

    供 T5/T6/T7 直接调用（plan 2.3.2）。写入使用独立事务；``payload`` 序列化为 JSON。
    为容忍上游误传，``action_type`` 不作白名单校验（由 REST 端点负责校验）。
    """
    body = payload if isinstance(payload, dict) else {}
    created_at = _utc_iso()
    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO behavior_log (user_id, action_type, kp_id, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, action_type, kp_id, json.dumps(body, ensure_ascii=False), created_at),
        )
        log_id = int(cursor.lastrowid or 0)

    return BehaviorLogView(
        id=log_id,
        user_id=user_id,
        action_type=action_type,
        kp_id=kp_id,
        payload=body,
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# 供 C4 聊天（T7）使用的画像摘要
# ---------------------------------------------------------------------------


def profile_summary(profile: UserProfileModel) -> dict[str, Any]:
    """构造轻量画像摘要（plan 2.2.4 ``build_system_prompt`` 的 ``profile_summary``）。

    含 ``learning_style`` 与 ``recommended_modes``；字段缺失时安全降级为 ``mixed``。
    """
    learning_style = str(_field(profile, "learning_style", "mixed") or "mixed")
    return {
        "learning_style": learning_style,
        "recommended_modes": recommended_modes_for_style(learning_style),
        "nickname": _field(profile, "nickname", "学习者"),
        "daily_minutes": _field(profile, "daily_minutes", 30),
    }