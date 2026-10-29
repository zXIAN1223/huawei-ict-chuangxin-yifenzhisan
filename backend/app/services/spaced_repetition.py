"""记忆曲线算法与每日复习计划（契约 C2-REVIEW@1，plan 2.2.2）。

本模块为**纯函数**集合，不依赖数据库与 HTTP，便于单元测试：

* :func:`compute_interval_days` —— 依据当前层级 / 难度 / 重要性 / 难度因子计算间隔天数；
* :func:`apply_grade` —— 依据评分（0..5）推进记忆曲线状态机，返回更新后的状态副本；
* :func:`build_daily_plan` —— 在用户时间预算内生成当日复习清单（含裁剪说明）。

常量与规则严格对应 plan 2.2.2「记忆曲线算法契约」与「每日计划契约」。

.. note::
   契约中的 ``IMPORTANCE_FACTOR`` 语义（plan 2.2.2 第 368 行注释）为
   「越重要因子越小 → 间隔越短（复习更频繁）」，故取 ``{1:1.2, 2:1.1, 3:1.0, 4:0.9, 5:0.8}``。
   该方向与 ``DIFFICULTY_FACTOR``（难度越高间隔越短）一致，均体现「更需掌握者复习更频繁」。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field

from backend.app.models import KnowledgePointMeta, ReviewState

__all__ = [
    "BASE_INTERVALS_DAYS",
    "DIFFICULTY_FACTOR",
    "IMPORTANCE_FACTOR",
    "EASE_MIN",
    "EASE_MAX",
    "EASE_START",
    "MAX_LEVEL",
    "DailyPlanItem",
    "DailyPlan",
    "clamp01",
    "compute_interval_days",
    "apply_grade",
    "build_daily_plan",
]

# ---------------------------------------------------------------------------
# 常量（冻结契约，plan 2.2.2）
# ---------------------------------------------------------------------------

#: 基础间隔序列，按记忆曲线层级（level）索引。
BASE_INTERVALS_DAYS: list[int] = [1, 2, 4, 7, 15, 30, 60, 120]

#: 难度因子：难度越高 → 因子越小 → 间隔越短。
DIFFICULTY_FACTOR: dict[int, float] = {1: 1.5, 2: 1.3, 3: 1.0, 4: 0.8, 5: 0.6}

#: 重要性因子：越重要 → 因子越小 → 间隔越短（复习更频繁）。
IMPORTANCE_FACTOR: dict[int, float] = {1: 1.2, 2: 1.1, 3: 1.0, 4: 0.9, 5: 0.8}

#: 难度因子（ease）取值边界与初始值。
EASE_MIN: float = 1.3
EASE_MAX: float = 3.0
EASE_START: float = 2.5

#: 允许的最大层级（基础间隔序列的最后一个索引）。
MAX_LEVEL: int = len(BASE_INTERVALS_DAYS) - 1

#: 元数据缺失时的中性缺省值（仅在内容库未提供该 kp 时兜底，正常流程不触发）。
_DEFAULT_DIFFICULTY: int = 3
_DEFAULT_IMPORTANCE: int = 3
_DEFAULT_EST_MINUTES: int = 1


# ---------------------------------------------------------------------------
# 每日计划视图模型
# ---------------------------------------------------------------------------


class DailyPlanItem(BaseModel):
    """当日复习清单中的单项。"""

    kp_id: str
    chapter_id: str
    title: str
    due_at: str
    overdue_days: int
    est_minutes: int
    level: int
    interval_days: int
    mastery: float
    #: 展示用综合优先级分（启发式，越大越优先）；排序仍以契约排序键为准。
    priority_score: float


class DailyPlan(BaseModel):
    """当日复习计划（对应 ``GET /api/review/today`` 的响应体）。"""

    items: list[DailyPlanItem] = Field(default_factory=list)
    total_minutes: int = 0
    budget_minutes: int = 0
    truncated: bool = False
    dropped_count: int = 0
    note: str = ""


# ---------------------------------------------------------------------------
# 内部时间工具（保持模块纯函数性，不依赖 db 模块）
# ---------------------------------------------------------------------------


def _to_iso(dt: datetime) -> str:
    """将 datetime 规范化为 UTC ISO-8601 字符串 ``YYYY-MM-DDTHH:MM:SSZ``。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str) -> datetime:
    """解析 UTC ISO-8601 字符串为带 UTC 时区的 datetime。"""
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _as_utc(now: datetime) -> datetime:
    """将传入的 ``now`` 归一为带 UTC 时区的 datetime（naive 视为 UTC）。"""
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _local_bounds(now: datetime) -> tuple[datetime, datetime]:
    """返回服务器本地日界的半开区间 ``[start, end)``（均为 UTC）。

    与 ``backend.app.db.local_day_bounds`` 语义一致：以本地时区当日 00:00 起，
    至次日 00:00 止。
    """
    local_start = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return local_start.astimezone(timezone.utc), (local_start + timedelta(days=1)).astimezone(
        timezone.utc
    )


# ---------------------------------------------------------------------------
# 数值工具
# ---------------------------------------------------------------------------


def clamp01(value: float) -> float:
    """将数值限制到闭区间 ``[0.0, 1.0]``。"""
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def _factor(mapping: dict[int, float], value: int, default_key: int) -> float:
    """按 1..5 的取值从因子表中取值，越界时夹取到边界（健壮性兜底）。"""
    try:
        key = int(value)
    except (TypeError, ValueError):
        key = default_key
    key = min(5, max(1, key))
    return mapping[key]


# ---------------------------------------------------------------------------
# 核心算法
# ---------------------------------------------------------------------------


def compute_interval_days(level: int, difficulty: int, importance: int, ease: float) -> int:
    """计算复习间隔天数。

    公式（plan 2.2.2）::

        base  = BASE_INTERVALS_DAYS[min(level, len-1)]
        raw   = base * DIFFICULTY_FACTOR[difficulty] * IMPORTANCE_FACTOR[importance] * ease / EASE_START
        interval = max(1, round(raw))

    难度更高（``DIFFICULTY_FACTOR`` 更小）或重要性更高（``IMPORTANCE_FACTOR`` 更小）时，
    间隔更短，满足 AC-R2-1（难度高者首个间隔不长于难度低者）。
    """
    index = min(max(int(level), 0), MAX_LEVEL)
    base = BASE_INTERVALS_DAYS[index]
    raw = (
        base
        * _factor(DIFFICULTY_FACTOR, difficulty, _DEFAULT_DIFFICULTY)
        * _factor(IMPORTANCE_FACTOR, importance, _DEFAULT_IMPORTANCE)
        * float(ease)
        / EASE_START
    )
    return max(1, int(round(raw)))


def apply_grade(
    state: ReviewState,
    grade: int,
    now: datetime,
    difficulty: int = _DEFAULT_DIFFICULTY,
    importance: int = _DEFAULT_IMPORTANCE,
    correct_ratio: float = 0.0,
) -> ReviewState:
    """依据评分推进记忆曲线状态机，返回**更新后的状态副本**（不修改入参）。

    评分语义（契约 2.2.2）::

        grade >= 4 : level + 1（封顶 MAX_LEVEL）、ease + 0.1（封顶 EASE_MAX）、lapses 不变
        grade == 3 : level / ease / lapses 均不变
        grade <= 2 : level - 1（下限 0）、ease - 0.2（下限 EASE_MIN）、lapses + 1

    随后统一更新：

    * ``review_count += 1``；``last_reviewed_at = now``（UTC）；
    * ``interval_days = compute_interval_days(new_level, difficulty, importance, new_ease)``；
    * ``due_at = now + interval_days 天``；
    * ``mastery = round(clamp01(0.7 * new_level / (len(BASE)-1) + 0.3 * correct_ratio), 3)``。

    参数 ``difficulty`` / ``importance`` 来自知识点的静态属性（``ReviewState`` 本身不含），
    默认中性值 3；``correct_ratio`` 由作答历史计算（无历史时为 0）。

    :raises ValueError: 当 ``grade`` 不在 0..5 范围内。
    """
    grade = int(grade)
    if grade < 0 or grade > 5:
        raise ValueError(f"grade 必须在 0..5 之间，实际为 {grade}")

    now_utc = _as_utc(now)

    old_level = int(state.level)
    old_ease = min(EASE_MAX, max(EASE_MIN, float(state.ease)))
    old_lapses = int(state.lapses)

    if grade >= 4:
        new_level = min(MAX_LEVEL, old_level + 1)
        new_ease = min(EASE_MAX, old_ease + 0.1)
        new_lapses = old_lapses
    elif grade == 3:
        new_level = old_level
        new_ease = old_ease
        new_lapses = old_lapses
    else:  # grade <= 2
        new_level = max(0, old_level - 1)
        new_ease = max(EASE_MIN, old_ease - 0.2)
        new_lapses = old_lapses + 1

    review_count = int(state.review_count) + 1
    interval_days = compute_interval_days(new_level, difficulty, importance, new_ease)
    due_at = now_utc + timedelta(days=interval_days)

    mastery = round(
        clamp01(0.7 * (new_level / MAX_LEVEL) + 0.3 * clamp01(float(correct_ratio))),
        3,
    )

    return state.copy(
        level=new_level,
        ease=round(new_ease, 10),
        lapses=new_lapses,
        review_count=review_count,
        last_reviewed_at=_to_iso(now_utc),
        interval_days=interval_days,
        due_at=_to_iso(due_at),
        mastery=mastery,
        updated_at=_to_iso(now_utc),
    )


def build_daily_plan(
    states: list[ReviewState],
    now: datetime,
    budget_minutes: int,
    kp_index: dict[str, KnowledgePointMeta],
) -> DailyPlan:
    """在时间预算内生成当日复习清单。

    规则（契约 2.2.2）：

    * 仅纳入 ``status == 'active'`` 且 ``due_at <= 当日本地日界`` 的项；
    * 排序（优先级由高到低）：① ``due_at`` 升序 ② ``importance`` 降序
      ③ ``difficulty`` 降序 ④ ``est_minutes`` 升序；
    * 依序累加 ``est_minutes``，加入后超过预算则停止；**至少保留 1 项**
      （即使单项已超预算，对应 AC-R2-5）；
    * ``truncated`` / ``dropped_count`` / ``note`` 说明裁剪情况。
    """
    now_utc = _as_utc(now)
    _, end_of_day = _local_bounds(now_utc)
    budget = max(0, int(budget_minutes))

    # 1) 过滤：active 且在今日日界前到期
    candidates: list[tuple[ReviewState, datetime]] = []
    for state in states:
        if getattr(state, "status", "active") != "active":
            continue
        try:
            due = _parse_iso(state.due_at)
        except (ValueError, TypeError):
            continue
        if due <= end_of_day:
            candidates.append((state, due))

    def _meta_of(kp_id: str) -> KnowledgePointMeta | None:
        return kp_index.get(kp_id)

    def _sort_key(pair: tuple[ReviewState, datetime]) -> tuple:
        state, due = pair
        meta = _meta_of(state.kp_id)
        importance = meta.importance if meta is not None else _DEFAULT_IMPORTANCE
        difficulty = meta.difficulty if meta is not None else _DEFAULT_DIFFICULTY
        est = meta.est_minutes if meta is not None else _DEFAULT_EST_MINUTES
        # ① due_at 升序 ② importance 降序 ③ difficulty 降序 ④ est_minutes 升序
        return (due, -importance, -difficulty, est)

    candidates.sort(key=_sort_key)

    # 2) 预算裁剪（至少保留 1 项）
    items: list[DailyPlanItem] = []
    total_minutes = 0
    truncated = False
    for state, due in candidates:
        meta = _meta_of(state.kp_id)
        est = meta.est_minutes if meta is not None else _DEFAULT_EST_MINUTES
        # 首项无条件保留；后续项若加入即超预算则停止
        if items and total_minutes + est > budget:
            truncated = True
            break
        items.append(_build_item(state, due, meta, now_utc))
        total_minutes += est

    dropped_count = len(candidates) - len(items)

    # 3) 裁剪说明
    if truncated:
        note = (
            f"已裁剪 {dropped_count} 项，因超出预算"
            f"（预算 {budget} 分钟，本计划 {total_minutes} 分钟）"
        )
    elif items and total_minutes > budget:
        note = (
            f"预算不足：单项预计耗时已超过每日预算（{budget} 分钟），仍保留 1 项"
        )
    else:
        note = ""

    return DailyPlan(
        items=items,
        total_minutes=total_minutes,
        budget_minutes=budget,
        truncated=truncated,
        dropped_count=dropped_count,
        note=note,
    )


def _build_item(
    state: ReviewState,
    due: datetime,
    meta: KnowledgePointMeta | None,
    now_utc: datetime,
) -> DailyPlanItem:
    """将状态与元数据组装为清单项。"""
    importance = meta.importance if meta is not None else _DEFAULT_IMPORTANCE
    difficulty = meta.difficulty if meta is not None else _DEFAULT_DIFFICULTY
    est_minutes = meta.est_minutes if meta is not None else _DEFAULT_EST_MINUTES
    title = meta.title if meta is not None else state.kp_id

    overdue_days = max(0, (now_utc.astimezone().date() - due.astimezone().date()).days)

    # 展示用综合分：逾期越久、越重要、越难、越快完成者分越高（仅用于前端排序展示）。
    priority_score = round(
        3.0 * overdue_days + 1.0 * importance + 0.5 * difficulty - 0.1 * est_minutes,
        3,
    )

    return DailyPlanItem(
        kp_id=state.kp_id,
        chapter_id=state.chapter_id,
        title=title,
        due_at=state.due_at,
        overdue_days=overdue_days,
        est_minutes=est_minutes,
        level=int(state.level),
        interval_days=int(state.interval_days),
        mastery=float(state.mastery),
        priority_score=priority_score,
    )