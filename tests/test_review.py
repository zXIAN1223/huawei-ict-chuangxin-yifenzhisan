"""C2-REVIEW@1 契约测试：记忆曲线纯函数与复习 API（plan 2.2.2 / 3.3 T13）。

覆盖：
- ``compute_interval_days`` 的 AC-R2-1（难度高者间隔不长于难度低者）；
- ``apply_grade`` 四档（>=4 / ==3 / <=2）的 level/ease/lapses/mastery；
- ``build_daily_plan`` 排序、预算裁剪、truncated/dropped_count、至少保留 1 项
  （AC-R2-2、AC-R2-5）；
- ``/api/review`` 端点的幂等加入、软删除/恢复、grade 校验、预算与提醒语义、
  404/422/MISSING_USER_ID 错误体。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from backend.app.db import to_iso, utc_now
from backend.app.models import KnowledgePointMeta, ReviewState
from backend.app.services.spaced_repetition import (
    BASE_INTERVALS_DAYS,
    EASE_MAX,
    EASE_MIN,
    MAX_LEVEL,
    apply_grade,
    build_daily_plan,
    compute_interval_days,
)

CELL_KP = "cell-membrane"


# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------


def make_state(
    kp_id: str,
    *,
    due_at: str | None = None,
    level: int = 0,
    status: str = "active",
    ease: float = 2.5,
    lapses: int = 0,
    review_count: int = 0,
    mastery: float = 0.0,
    chapter_id: str = "cells",
) -> ReviewState:
    """构造 ``ReviewState``（默认已到期）。"""
    if due_at is None:
        due_at = to_iso(datetime.now(timezone.utc) - timedelta(hours=1))
    return ReviewState(
        user_id="u1",
        kp_id=kp_id,
        chapter_id=chapter_id,
        status=status,
        level=level,
        interval_days=1,
        ease=ease,
        due_at=due_at,
        last_reviewed_at=None,
        review_count=review_count,
        lapses=lapses,
        mastery=mastery,
        selected_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )


def make_meta(
    kp_id: str,
    *,
    difficulty: int = 3,
    importance: int = 3,
    est_minutes: int = 10,
    chapter_id: str = "cells",
) -> KnowledgePointMeta:
    """构造 ``KnowledgePointMeta``。"""
    return KnowledgePointMeta(
        kp_id=kp_id,
        chapter_id=chapter_id,
        title=kp_id,
        summary="摘要",
        tags=[],
        difficulty=difficulty,
        importance=importance,
        est_minutes=est_minutes,
        has_images=True,
        quiz_count=2,
    )


# ---------------------------------------------------------------------------
# compute_interval_days（AC-R2-1）
# ---------------------------------------------------------------------------


def test_compute_interval_days_first_interval_example_a() -> None:
    """plan 1.4 示例 A：level 0 时难度 5=1 天、难度 3=1 天、难度 1=2 天。"""
    assert compute_interval_days(0, 5, 3, 2.5) == 1
    assert compute_interval_days(0, 3, 3, 2.5) == 1
    assert compute_interval_days(0, 1, 3, 2.5) == 2


def test_compute_interval_days_monotonic_by_difficulty() -> None:
    """AC-R2-1：任意 level 下难度越高者间隔不长于难度更低者。"""
    for level in range(len(BASE_INTERVALS_DAYS)):
        intervals = [compute_interval_days(level, difficulty, 3, 2.5) for difficulty in (5, 4, 3, 2, 1)]
        # 难度 5→1 间隔应单调不减
        assert intervals == sorted(intervals), f"level={level} 间隔非单调：{intervals}"


def test_compute_interval_days_monotonic_by_importance() -> None:
    """重要性越高（因子越小）间隔越短。"""
    for level in range(len(BASE_INTERVALS_DAYS)):
        intervals = [compute_interval_days(level, 3, importance, 2.5) for importance in (1, 2, 3, 4, 5)]
        assert intervals == sorted(intervals, reverse=True), f"level={level}: {intervals}"


def test_compute_interval_days_minimum_one() -> None:
    """间隔下限为 1 天。"""
    assert compute_interval_days(0, 5, 5, EASE_MIN) >= 1


# ---------------------------------------------------------------------------
# apply_grade（四档）
# ---------------------------------------------------------------------------


def test_apply_grade_high_grade_promotes_level_and_ease() -> None:
    """grade>=4：level+1、ease+0.1、lapses 不变、review_count+1、mastery 公式。"""
    now = datetime.now(timezone.utc)
    state = make_state("kp-a", level=2, ease=2.5, lapses=1, review_count=3)
    updated = apply_grade(state, 4, now, difficulty=3, importance=3, correct_ratio=0.5)

    assert updated.level == 3
    assert abs(updated.ease - 2.6) < 1e-9
    assert updated.lapses == 1
    assert updated.review_count == 4
    # mastery = round(clamp01(0.7*3/7 + 0.3*0.5), 3) = 0.45
    assert updated.mastery == 0.45
    # 输入未被修改
    assert state.level == 2
    # interval 与 due_at 一致推进
    assert updated.interval_days == compute_interval_days(3, 3, 3, 2.6)
    due = datetime.fromisoformat(updated.due_at.replace("Z", "+00:00"))
    assert (due - now).days in (updated.interval_days, updated.interval_days - 1)


def test_apply_grade_grade_three_keeps_level_and_ease() -> None:
    """grade==3：level/ease/lapses 均不变，仅 review_count 与时间推进。"""
    now = datetime.now(timezone.utc)
    state = make_state("kp-b", level=2, ease=2.5, lapses=1, review_count=3)
    updated = apply_grade(state, 3, now, difficulty=3, importance=3, correct_ratio=0.5)

    assert updated.level == 2
    assert abs(updated.ease - 2.5) < 1e-9
    assert updated.lapses == 1
    assert updated.review_count == 4
    # mastery = round(0.7*2/7 + 0.15) = 0.35
    assert updated.mastery == 0.35


def test_apply_grade_low_grade_demotes_level_and_counts_lapse() -> None:
    """grade<=2：level-1、ease-0.2、lapses+1。"""
    now = datetime.now(timezone.utc)
    state = make_state("kp-c", level=2, ease=2.5, lapses=1, review_count=3)
    updated = apply_grade(state, 1, now, difficulty=3, importance=3, correct_ratio=0.5)

    assert updated.level == 1
    assert abs(updated.ease - 2.3) < 1e-9
    assert updated.lapses == 2
    assert updated.review_count == 4
    # mastery = round(0.7*1/7 + 0.15) = 0.25
    assert updated.mastery == 0.25


def test_apply_grade_clamps_level_and_ease() -> None:
    """level 上限封顶 MAX_LEVEL，ease 夹在 [EASE_MIN, EASE_MAX]。"""
    now = datetime.now(timezone.utc)
    # 上限：level=MAX_LEVEL 且 grade=5 不再提升；ease 封顶 EASE_MAX
    state_high = make_state("kp-d", level=MAX_LEVEL, ease=2.95)
    updated_high = apply_grade(state_high, 5, now)
    assert updated_high.level == MAX_LEVEL
    assert abs(updated_high.ease - EASE_MAX) < 1e-9

    # 下限：level=0 且 grade=0 不再下降；ease 触底 EASE_MIN
    state_low = make_state("kp-e", level=0, ease=1.35)
    updated_low = apply_grade(state_low, 0, now)
    assert updated_low.level == 0
    assert abs(updated_low.ease - EASE_MIN) < 1e-9
    assert updated_low.lapses == 1


def test_apply_grade_rejects_invalid_grade() -> None:
    """grade 不在 0..5 时抛 ValueError。"""
    now = datetime.now(timezone.utc)
    state = make_state("kp-f")
    for invalid in (-1, 6, 99):
        try:
            apply_grade(state, invalid, now)
        except ValueError:
            continue
        raise AssertionError(f"grade={invalid} 应抛 ValueError")


# ---------------------------------------------------------------------------
# build_daily_plan
# ---------------------------------------------------------------------------


def test_build_daily_plan_orders_by_importance_when_due_equal() -> None:
    """到期相同则按重要性降序。"""
    now = utc_now()
    due = to_iso(now - timedelta(hours=1))
    states = [
        make_state("kp-imp1", due_at=due),
        make_state("kp-imp5", due_at=due),
        make_state("kp-imp3", due_at=due),
    ]
    kp_index = {
        "kp-imp1": make_meta("kp-imp1", importance=1),
        "kp-imp5": make_meta("kp-imp5", importance=5),
        "kp-imp3": make_meta("kp-imp3", importance=3),
    }

    plan = build_daily_plan(states, now, 100, kp_index)

    assert [item.kp_id for item in plan.items] == ["kp-imp5", "kp-imp3", "kp-imp1"]


def test_build_daily_plan_orders_by_due_at_ascending() -> None:
    """逾期越久越靠前（due_at 升序）。"""
    now = utc_now()
    earlier = to_iso(now - timedelta(days=2))
    later = to_iso(now - timedelta(hours=1))
    states = [make_state("kp-later", due_at=later), make_state("kp-earlier", due_at=earlier)]
    kp_index = {
        "kp-later": make_meta("kp-later"),
        "kp-earlier": make_meta("kp-earlier"),
    }

    plan = build_daily_plan(states, now, 100, kp_index)

    assert [item.kp_id for item in plan.items] == ["kp-earlier", "kp-later"]


def test_build_daily_plan_budget_truncation_example_b() -> None:
    """plan 1.4 示例 B：预算 20 分钟、15 项各 10 分钟 → 保留 2 项、裁剪 13 项。"""
    now = utc_now()
    due = to_iso(now - timedelta(hours=1))
    states = [make_state(f"kp-{i:02d}", due_at=due) for i in range(15)]
    kp_index = {f"kp-{i:02d}": make_meta(f"kp-{i:02d}", est_minutes=10) for i in range(15)}

    plan = build_daily_plan(states, now, 20, kp_index)

    assert len(plan.items) == 2
    assert plan.total_minutes == 20
    assert plan.budget_minutes == 20
    assert plan.truncated is True
    assert plan.dropped_count == 13
    assert "13" in plan.note


def test_build_daily_plan_keeps_at_least_one_item_over_budget() -> None:
    """AC-R2-5：单项已超预算时至少保留 1 项并给出预算不足提示。"""
    now = utc_now()
    due = to_iso(now - timedelta(hours=1))
    states = [make_state("kp-big", due_at=due)]
    kp_index = {"kp-big": make_meta("kp-big", est_minutes=10)}

    plan = build_daily_plan(states, now, 5, kp_index)

    assert len(plan.items) == 1
    assert plan.items[0].kp_id == "kp-big"
    assert plan.total_minutes == 10
    assert plan.budget_minutes == 5
    assert plan.note != ""
    assert "预算不足" in plan.note or "1 项" in plan.note


def test_build_daily_plan_excludes_removed_and_future() -> None:
    """仅纳入 active 且今日日界前到期的项。"""
    now = utc_now()
    due = to_iso(now - timedelta(hours=1))
    future = to_iso(now + timedelta(days=10))
    states = [
        make_state("kp-active", due_at=due),
        make_state("kp-removed", due_at=due, status="removed"),
        make_state("kp-future", due_at=future),
    ]
    kp_index = {sid: make_meta(sid) for sid in ("kp-active", "kp-removed", "kp-future")}

    plan = build_daily_plan(states, now, 100, kp_index)

    assert [item.kp_id for item in plan.items] == ["kp-active"]


def test_build_daily_plan_not_truncated_when_within_budget() -> None:
    """预算充足时 truncated=False、dropped_count=0。"""
    now = utc_now()
    due = to_iso(now - timedelta(hours=1))
    states = [make_state("kp-1", due_at=due)]
    kp_index = {"kp-1": make_meta("kp-1", est_minutes=5)}

    plan = build_daily_plan(states, now, 100, kp_index)

    assert plan.truncated is False
    assert plan.dropped_count == 0
    assert plan.total_minutes == 5


# ---------------------------------------------------------------------------
# API 测试
# ---------------------------------------------------------------------------


def test_missing_user_id_returns_400(client) -> None:
    """缺 X-User-Id → 400 MISSING_USER_ID。"""
    response = client.get("/api/review/items")
    assert response.status_code == 400
    assert response.json()["code"] == "MISSING_USER_ID"


def test_add_item_is_idempotent_and_validates_kp(client, auth_headers) -> None:
    """POST /items 幂等（201）；未知 kp → 404 KP_NOT_FOUND。"""
    first = client.post("/api/review/items", json={"kp_id": CELL_KP}, headers=auth_headers)
    assert first.status_code == 201
    body = first.json()
    assert body["kp_id"] == CELL_KP
    assert body["status"] == "active"
    assert "title" in body and "due_at" in body

    second = client.post("/api/review/items", json={"kp_id": CELL_KP}, headers=auth_headers)
    assert second.status_code == 201
    assert second.json()["due_at"] == body["due_at"]

    not_found = client.post(
        "/api/review/items", json={"kp_id": "no-such-kp"}, headers=auth_headers
    )
    assert not_found.status_code == 404
    assert not_found.json()["code"] == "KP_NOT_FOUND"


def test_delete_restore_preserve_history(client, auth_headers) -> None:
    """DELETE 为软删除（status=removed），restore 恢复为 active。"""
    client.post("/api/review/items", json={"kp_id": CELL_KP}, headers=auth_headers)

    deleted = client.delete(f"/api/review/items/{CELL_KP}", headers=auth_headers)
    assert deleted.status_code == 200
    assert deleted.json() == {"status": "removed"}

    active = client.get("/api/review/items", headers=auth_headers).json()
    assert CELL_KP not in [item["kp_id"] for item in active]

    all_items = client.get(
        "/api/review/items", params={"status": "all"}, headers=auth_headers
    ).json()
    assert CELL_KP in [item["kp_id"] for item in all_items]

    restored = client.post(f"/api/review/items/{CELL_KP}/restore", headers=auth_headers)
    assert restored.status_code == 200
    assert restored.json()["status"] == "active"


def test_complete_review_invalid_grade_returns_422(client, auth_headers) -> None:
    """非法 grade → 422 INVALID_GRADE。"""
    client.post("/api/review/items", json={"kp_id": CELL_KP}, headers=auth_headers)
    response = client.post(
        f"/api/review/items/{CELL_KP}/complete", json={"grade": 6}, headers=auth_headers
    )
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_GRADE"


def test_complete_review_advances_curve(client, auth_headers) -> None:
    """合法 grade 推进曲线并返回 state 与 next_due_at。"""
    client.post("/api/review/items", json={"kp_id": CELL_KP}, headers=auth_headers)
    response = client.post(
        f"/api/review/items/{CELL_KP}/complete", json={"grade": 5}, headers=auth_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["state"]["level"] == 1
    assert body["state"]["review_count"] == 1
    assert body["state"]["last_reviewed_at"] is not None
    assert body["next_due_at"] == body["state"]["due_at"]


def test_budget_put_null_uses_default(client, auth_headers) -> None:
    """PUT /budget 省略/传 null 回退默认 30；显式值生效。"""
    default = client.put("/api/review/budget", json={"budget_minutes": None}, headers=auth_headers)
    assert default.status_code == 200
    assert default.json()["budget_minutes"] == 30

    explicit = client.put("/api/review/budget", json={"budget_minutes": 45}, headers=auth_headers)
    assert explicit.status_code == 200
    assert explicit.json()["budget_minutes"] == 45


def test_reminder_get_default_and_update(client, auth_headers) -> None:
    """提醒默认 {enabled:true,time:'20:00'}；更新生效；非法时间 422；enabled=false 保留时间。"""
    default = client.get("/api/review/reminder", headers=auth_headers)
    assert default.status_code == 200
    assert default.json() == {"enabled": True, "time": "20:00"}

    updated = client.put(
        "/api/review/reminder", json={"enabled": False, "time": "07:30"}, headers=auth_headers
    )
    assert updated.json() == {"enabled": False, "time": "07:30"}

    # enabled=false 且未传 time → 保留原时间
    keep = client.put(
        "/api/review/reminder", json={"enabled": True}, headers=auth_headers
    )
    assert keep.json() == {"enabled": True, "time": "07:30"}

    invalid = client.put(
        "/api/review/reminder", json={"enabled": True, "time": "25:00"}, headers=auth_headers
    )
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "VALIDATION_ERROR"


def test_today_returns_daily_plan_and_includes_due_item(
    client, auth_headers, temp_db_path
) -> None:
    """GET /today 返回 DailyPlan 结构，且把已到期项纳入清单。"""
    client.put("/api/review/budget", json={"budget_minutes": 30}, headers=auth_headers)
    client.post("/api/review/items", json={"kp_id": CELL_KP}, headers=auth_headers)

    # 直接把该项置为已到期（模拟跨日）
    past_due = to_iso(utc_now() - timedelta(hours=1))
    conn = sqlite3.connect(str(temp_db_path))
    try:
        conn.execute(
            "UPDATE review_state SET due_at = ? WHERE kp_id = ?", (past_due, CELL_KP)
        )
        conn.commit()
    finally:
        conn.close()

    response = client.get("/api/review/today", headers=auth_headers)
    assert response.status_code == 200
    plan = response.json()
    assert set(plan.keys()) == {
        "items",
        "total_minutes",
        "budget_minutes",
        "truncated",
        "dropped_count",
        "note",
    }
    assert plan["budget_minutes"] == 30
    assert CELL_KP in [item["kp_id"] for item in plan["items"]]
    included = next(item for item in plan["items"] if item["kp_id"] == CELL_KP)
    for field in (
        "chapter_id",
        "title",
        "due_at",
        "overdue_days",
        "est_minutes",
        "level",
        "interval_days",
        "mastery",
        "priority_score",
    ):
        assert field in included


