"""C5-PROFILE@1 契约测试：VARK 测评、自适应策略、画像 PUT 语义与行为日志
（plan 2.2.5 / 3.3 T13）。

覆盖：
- ``evaluate_assessment`` 的 VARK 计分 / 并列优先序 / 全 0→mixed（AC-R5-2）；
- ``compute_strategy`` 的复习强度分档、会话时长、有效预算、恒含 feynman（AC-R5-3）；
- ``PUT /api/profile`` 的「省略=不变 / 显式 null=清空」语义；
- 问卷、策略、统计、行为日志端点与 MISSING_USER_ID/422 错误体。
"""

from __future__ import annotations

import sqlite3

from backend.app.models import QuizAttemptView, UserProfileView
from backend.app.services.learning_strategy import (
    compute_strategy,
    evaluate_assessment,
    recommended_modes_for_style,
)

CELL_KP = "cell-membrane"


# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------


def make_profile(**overrides) -> UserProfileView:
    """构造 ``UserProfileView``（供 compute_strategy 单测）。"""
    base = {
        "user_id": "u1",
        "nickname": "学习者",
        "daily_minutes": 60,
        "learning_style": "visual",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    }
    base.update(overrides)
    return UserProfileView(**base)


def make_attempt(is_correct: bool, index: int = 0) -> QuizAttemptView:
    """构造一条 ``QuizAttemptView``。"""
    return QuizAttemptView(
        id=index,
        kp_id=CELL_KP,
        qid=f"q{index}",
        question_type="single_choice",
        user_answer="A",
        score=100 if is_correct else 0,
        is_correct=is_correct,
        keyword_hits=[],
        graded_by="local",
        feedback="",
        created_at="2024-01-01T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# evaluate_assessment（AC-R5-2）
# ---------------------------------------------------------------------------


def test_evaluate_assessment_empty_answers_is_mixed() -> None:
    """全部为 0 / 无 style 作答 → mixed。"""
    result = evaluate_assessment({})
    assert result.learning_style == "mixed"
    assert "feynman" in result.recommended_modes


def test_evaluate_assessment_normalizes_scores() -> None:
    """归一化得分 = 维度原始分 / 该维度最大可得；本问卷 style 题每维度最大可得为 5。"""
    result = evaluate_assessment({"q4": "v"})
    assert result.learning_style == "visual"
    assert result.style_scores["visual"] == 0.2  # 1/5
    assert result.style_scores["auditory"] == 0.0


def test_evaluate_assessment_dominant_by_highest_score() -> None:
    """主导维度为归一化分最高者。"""
    result = evaluate_assessment({"q4": "a", "q5": "a"})
    assert result.learning_style == "auditory"
    assert result.style_scores["auditory"] == 0.4  # 2/5


def test_evaluate_assessment_tie_prefers_visual() -> None:
    """并列时优先序 visual > auditory > reading > kinesthetic。"""
    result = evaluate_assessment({"q4": "v", "q5": "a"})
    assert result.style_scores["visual"] == result.style_scores["auditory"] == 0.2
    assert result.learning_style == "visual"


def test_evaluate_assessment_ignores_non_style_questions() -> None:
    """非 style 题与本问卷无关的 qid 一律忽略。"""
    result = evaluate_assessment({"q1": "morning", "q2": 3, "unknown": "x"})
    assert result.learning_style == "mixed"


def test_all_styles_recommend_feynman() -> None:
    """所有风格推荐学习方式恒含 feynman（学习金字塔教授他人）。"""
    for style in ("visual", "auditory", "reading", "kinesthetic", "mixed"):
        modes = recommended_modes_for_style(style)
        assert "feynman" in modes, f"{style} 推荐缺 feynman"
        assert len(modes) == len(set(modes)), f"{style} 推荐存在重复"


# ---------------------------------------------------------------------------
# compute_strategy（AC-R5-3）
# ---------------------------------------------------------------------------


def test_compute_strategy_no_attempts_default_intensity() -> None:
    """无作答数据 → 复习强度默认 1.0，有效预算 = daily_minutes。"""
    strategy = compute_strategy(make_profile(daily_minutes=60), [], [], [])
    assert strategy.review_intensity_multiplier == 1.0
    assert strategy.daily_minutes_effective == 60
    assert strategy.session_length_min == 30  # min(30, max(10, 60//2))
    assert "feynman" in strategy.recommended_modes


def test_compute_strategy_intensity_high_when_low_accuracy() -> None:
    """正确率 <0.6 → 倍率 1.5，有效预算折算。"""
    attempts = [make_attempt(i < 1, i) for i in range(5)]  # 1/5=0.2
    strategy = compute_strategy(make_profile(daily_minutes=60), [], attempts, [])
    assert strategy.review_intensity_multiplier == 1.5
    assert strategy.daily_minutes_effective == round(60 / 1.5) == 40


def test_compute_strategy_intensity_medium_band() -> None:
    """0.6<=r<0.8 → 倍率 1.2。"""
    attempts = [make_attempt(i < 3, i) for i in range(5)]  # 3/5=0.6
    strategy = compute_strategy(make_profile(daily_minutes=50), [], attempts, [])
    assert strategy.review_intensity_multiplier == 1.2
    assert strategy.daily_minutes_effective == round(50 / 1.2) == 42


def test_compute_strategy_intensity_normal_when_high_accuracy() -> None:
    """r>=0.8 → 倍率 1.0。"""
    attempts = [make_attempt(i < 9, i) for i in range(10)]  # 0.9
    strategy = compute_strategy(make_profile(daily_minutes=45), [], attempts, [])
    assert strategy.review_intensity_multiplier == 1.0


def test_compute_strategy_kinesthetic_shortens_session() -> None:
    """动觉型会话时长再 -5（下限 10）。"""
    strategy = compute_strategy(
        make_profile(learning_style="kinesthetic", daily_minutes=60), [], [], []
    )
    assert strategy.session_length_min == 25  # 30 - 5


def test_compute_strategy_session_length_floor() -> None:
    """会话时长下限 10（daily_minutes 很小时）。"""
    strategy = compute_strategy(make_profile(daily_minutes=4), [], [], [])
    assert strategy.session_length_min == 10


def test_compute_strategy_rationale_has_key_reasons() -> None:
    """rationale 至少含主导学习风格与复习强度两条。"""
    strategy = compute_strategy(make_profile(), [], [make_attempt(True, 0)], [])
    joined = "\n".join(strategy.rationale)
    assert "学习风格" in joined
    assert "复习强度" in joined or "正确率" in joined


# ---------------------------------------------------------------------------
# API 测试
# ---------------------------------------------------------------------------


def test_get_profile_creates_default(client, auth_headers) -> None:
    """GET /api/profile 不存在则创建默认画像。"""
    response = client.get("/api/profile", headers=auth_headers)
    assert response.status_code == 200
    profile = response.json()
    assert profile["nickname"] == "学习者"
    assert profile["daily_minutes"] == 30
    assert profile["learning_style"] == "mixed"
    assert profile["reminder_enabled"] is True
    assert profile["reminder_time"] == "20:00"
    assert profile["questionnaire_completed"] is False
    assert profile["preferred_slots"] == []


def test_put_profile_omitted_keeps_value(client, auth_headers) -> None:
    """省略字段=不变。"""
    client.get("/api/profile", headers=auth_headers)
    updated = client.put(
        "/api/profile",
        json={"daily_minutes": 45},
        headers=auth_headers,
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["daily_minutes"] == 45
    # 未提供的 nickname 保持不变
    assert body["nickname"] == "学习者"


def test_put_profile_explicit_null_clears(client, auth_headers) -> None:
    """显式 null 表示清空/回默认。"""
    client.get("/api/profile", headers=auth_headers)
    # 先设置非默认值
    client.put(
        "/api/profile",
        json={"nickname": "张三", "daily_minutes": 45, "preferred_slots": ["19:00"]},
        headers=auth_headers,
    )
    # 再显式清空
    cleared = client.put(
        "/api/profile",
        json={"nickname": None, "daily_minutes": None, "preferred_slots": None},
        headers=auth_headers,
    ).json()
    assert cleared["nickname"] == "学习者"
    assert cleared["daily_minutes"] == 30
    assert cleared["preferred_slots"] == []


def test_put_profile_learning_style_null_is_ignored(client, auth_headers) -> None:
    """learning_style / style_scores 不接受 null（忽略该字段，保留原值）。"""
    client.post(
        "/api/profile/questionnaire",
        json={"answers": {"q4": "v", "q5": "v", "q6": "v", "q7": "v", "q8": ["v"]}},
        headers=auth_headers,
    )
    before = client.get("/api/profile", headers=auth_headers).json()
    assert before["learning_style"] == "visual"

    after = client.put(
        "/api/profile",
        json={"learning_style": None, "style_scores": None},
        headers=auth_headers,
    ).json()
    assert after["learning_style"] == "visual"
    assert after["style_scores"] == before["style_scores"]


def test_put_profile_reminder_disabled_keeps_time(client, auth_headers) -> None:
    """reminder_enabled=false 时保留 reminder_time。"""
    client.put(
        "/api/profile", json={"reminder_time": "07:30"}, headers=auth_headers
    )
    body = client.put(
        "/api/profile", json={"reminder_enabled": False}, headers=auth_headers
    ).json()
    assert body["reminder_enabled"] is False
    assert body["reminder_time"] == "07:30"

    # 显式 null 才回到默认
    reset = client.put(
        "/api/profile", json={"reminder_time": None}, headers=auth_headers
    ).json()
    assert reset["reminder_time"] == "20:00"


def test_get_questionnaire_document(client, auth_headers) -> None:
    """GET /questionnaire 返回版本化问卷，含 >=4 道 style 题且覆盖 v/a/r/k。"""
    response = client.get("/api/profile/questionnaire", headers=auth_headers)
    assert response.status_code == 200
    document = response.json()
    assert document["version"]
    style_questions = [
        q for q in document["questions"] if q["dimension"] == "style"
    ]
    assert len(style_questions) >= 4
    codes = set()
    for question in style_questions:
        for option in question["options"]:
            codes.add(option["value"])
    assert {"v", "a", "r", "k"} <= codes


def test_submit_questionnaire_persists_and_completes(client, auth_headers) -> None:
    """POST /questionnaire 返回 AssessmentResult 并置 questionnaire_completed=true。"""
    response = client.post(
        "/api/profile/questionnaire",
        json={"answers": {"q4": "v", "q5": "v", "q6": "v", "q7": "v", "q8": ["v"]}},
        headers=auth_headers,
    )
    assert response.status_code == 200
    result = response.json()
    assert result["learning_style"] == "visual"
    assert result["style_scores"]["visual"] == 1.0
    assert "feynman" in result["recommended_modes"]

    profile = client.get("/api/profile", headers=auth_headers).json()
    assert profile["questionnaire_completed"] is True
    assert profile["learning_style"] == "visual"


def test_get_strategy_records_snapshot(client, auth_headers, temp_db_path) -> None:
    """GET /strategy 返回 AdaptiveStrategy 且持久化 strategy_snapshot。"""
    response = client.get("/api/profile/strategy", headers=auth_headers)
    assert response.status_code == 200
    strategy = response.json()
    assert {"recommended_modes", "session_length_min", "review_intensity_multiplier", "rationale"} <= set(
        strategy.keys()
    )
    assert "feynman" in strategy["recommended_modes"]
    assert strategy["rationale"]

    conn = sqlite3.connect(str(temp_db_path))
    try:
        count = conn.execute("SELECT COUNT(*) FROM strategy_snapshot").fetchone()[0]
    finally:
        conn.close()
    assert count >= 1


def test_get_analytics_shape(client, auth_headers) -> None:
    """GET /analytics 返回统计视图字段。"""
    response = client.get("/api/profile/analytics", headers=auth_headers)
    assert response.status_code == 200
    analytics = response.json()
    for field in (
        "streak_days",
        "total_minutes_estimate",
        "attempts",
        "correct_rate",
        "kps_in_review",
        "review_due_today",
        "by_day",
        "mastery_by_chapter",
    ):
        assert field in analytics


def test_log_behavior_and_invalid_action(client, auth_headers) -> None:
    """POST /api/log/behavior 写入成功（201）；非法 action_type → 422。"""
    response = client.post(
        "/api/log/behavior",
        json={"action_type": "kp_view", "kp_id": CELL_KP, "payload": {"src": "knowledge"}},
        headers=auth_headers,
    )
    assert response.status_code == 201
    log = response.json()
    assert log["action_type"] == "kp_view"
    assert log["kp_id"] == CELL_KP

    invalid = client.post(
        "/api/log/behavior", json={"action_type": "__bad__"}, headers=auth_headers
    )
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "INVALID_ACTION_TYPE"


def test_missing_user_id_returns_400(client) -> None:
    """缺 X-User-Id → 400 MISSING_USER_ID。"""
    response = client.get("/api/profile")
    assert response.status_code == 400
    assert response.json()["code"] == "MISSING_USER_ID"