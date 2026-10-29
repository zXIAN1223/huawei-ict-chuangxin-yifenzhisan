"""C3-QUIZ@1 契约测试：判分纯函数与问答 API（plan 2.2.3 / 3.3 T13）。

覆盖：
- ``grade_answer`` 四题型规则（含 multi 的 recall/precision、简答关键词与 AI 加成）
  （AC-R3-2、AC-R3-3）与 ``PASS_THRESHOLD``；
- ``/api/quiz`` 取题、作答落库、与 C2 复习状态联动、404（KP/QUIZ_ITEM）与
  MISSING_USER_ID 错误体。
"""

from __future__ import annotations

from backend.app.models import QuizItem
from backend.app.routers.quiz import PASS_THRESHOLD, grade_answer

CELL_KP = "cell-membrane"


# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------


def make_item(**overrides) -> QuizItem:
    """构造一个 ``QuizItem``，未指定字段使用中性默认值。"""
    base = {
        "qid": "q1",
        "type": "single_choice",
        "difficulty": 3,
        "importance": 3,
        "question": "示例题",
        "options": ["A", "B"],
        "answer": "A",
        "keywords": None,
        "explanation": "示例解析",
        "points": 1,
    }
    base.update(overrides)
    return QuizItem(**base)


# ---------------------------------------------------------------------------
# grade_answer 纯函数
# ---------------------------------------------------------------------------


def test_single_choice_exact_match() -> None:
    """选择/判断精确匹配（归一化大小写与空白）得 100，否则 0。"""
    item = make_item(type="single_choice", options=["ATP", "DNA"], answer="ATP")

    correct = grade_answer(item, " atp ")
    assert correct.score == 100
    assert correct.is_correct is True
    assert correct.expected_answer == "ATP"
    assert correct.graded_by == "local"

    wrong = grade_answer(item, "DNA")
    assert wrong.score == 0
    assert wrong.is_correct is False


def test_true_false_grading() -> None:
    """判断题固定选项 ["正确","错误"]。"""
    item = make_item(
        type="true_false", options=["正确", "错误"], answer="正确"
    )
    assert grade_answer(item, "正确").score == 100
    assert grade_answer(item, "错误").score == 0


def test_multi_choice_full_match_and_partial_recall_precision() -> None:
    """多选：全等 100；否则 0.5*(recall+precision)。"""
    item = make_item(
        type="multi_choice",
        options=["A", "B", "C"],
        answer=["A", "B"],
    )

    full = grade_answer(item, ["A", "B"])
    assert full.score == 100

    # expected={A,B}, got={A} → recall=0.5, precision=1.0 → 75
    partial = grade_answer(item, ["A"])
    assert partial.score == 75

    # got={A,C} → recall=0.5, precision=0.5 → 50
    half = grade_answer(item, ["A", "C"])
    assert half.score == 50

    # got 为空 → precision=0, recall=0 → 0
    empty = grade_answer(item, [])
    assert empty.score == 0
    assert empty.is_correct is False


def test_short_answer_keyword_scoring() -> None:
    """简答：关键词命中比例 = 分；无 AI 时为 100% 本地判分。"""
    item = make_item(
        type="short_answer",
        options=None,
        answer="细胞膜由磷脂双分子层构成，控制物质进出",
        keywords=["细胞膜", "磷脂双分子层"],
        explanation="标准解析",
    )

    both = grade_answer(item, "细胞膜由磷脂双分子层构成")
    assert both.score == 100
    assert set(both.keyword_hits) == {"细胞膜", "磷脂双分子层"}
    assert both.graded_by == "local"

    one = grade_answer(item, "细胞膜很重要")
    assert one.score == 50
    assert one.keyword_hits == ["细胞膜"]

    none = grade_answer(item, "不知道")
    assert none.score == 0


def test_short_answer_ai_score_overlay() -> None:
    """简答叠加 AI 分：score = round(0.6*kw + 0.4*ai)，graded_by=local+ai。"""
    item = make_item(
        type="short_answer",
        options=None,
        answer="参考",
        keywords=["细胞膜", "磷脂双分子层"],
        explanation="解析",
    )

    result = grade_answer(item, "细胞膜由磷脂双分子层构成", ai_score=80)
    # 0.6*100 + 0.4*80 = 60 + 32 = 92
    assert result.score == 92
    assert result.graded_by == "local+ai"


def test_short_answer_without_keywords_scores_zero() -> None:
    """keywords 为空时 kw_score=0。"""
    item = make_item(
        type="short_answer", options=None, answer="参考", keywords=[], explanation="解析"
    )
    assert grade_answer(item, "任意作答").score == 0


def test_pass_threshold_boundary() -> None:
    """is_correct 阈值 PASS_THRESHOLD=60。"""
    item = make_item(
        type="short_answer",
        options=None,
        answer="参考",
        keywords=["a", "b", "c"],
        explanation="解析",
    )
    # 命中 2/3 → 67 分（>=60 正确）
    assert grade_answer(item, "a b").score == 67
    assert grade_answer(item, "a b").is_correct is True
    # 命中 1/3 → 33 分（<60 错误）
    assert grade_answer(item, "a").score == 33
    assert grade_answer(item, "a").is_correct is False
    assert PASS_THRESHOLD == 60


# ---------------------------------------------------------------------------
# API 测试
# ---------------------------------------------------------------------------


def _pick_gradable_item(client, auth_headers, kp_id: str) -> dict:
    """从真实题库中取一道可精确判分的题（single_choice / true_false）。"""
    response = client.get(f"/api/quiz/{kp_id}", headers=auth_headers)
    assert response.status_code == 200
    items = response.json()
    assert items
    for item in items:
        if item["type"] in ("single_choice", "true_false"):
            return item
    raise AssertionError(f"{kp_id} 未找到可精确判分的题目")


def test_get_quiz_returns_items_and_404(client, auth_headers) -> None:
    """GET /api/quiz/{kp_id} 返回题组；未知 kp → 404 KP_NOT_FOUND。"""
    response = client.get(f"/api/quiz/{CELL_KP}", headers=auth_headers)
    assert response.status_code == 200
    items = response.json()
    assert len(items) >= 2
    for item in items:
        assert {"qid", "type", "difficulty", "importance", "question", "explanation"} <= set(item)

    missing = client.get("/api/quiz/no-such-kp", headers=auth_headers)
    assert missing.status_code == 404
    assert missing.json()["code"] == "KP_NOT_FOUND"


def test_get_quiz_limit_and_order(client, auth_headers) -> None:
    """limit 限定条数；order=importance 时按该字段降序。"""
    limited = client.get(
        f"/api/quiz/{CELL_KP}", params={"limit": 1}, headers=auth_headers
    ).json()
    assert len(limited) == 1

    ordered = client.get(
        f"/api/quiz/{CELL_KP}", params={"order": "importance"}, headers=auth_headers
    ).json()
    importances = [item["importance"] for item in ordered]
    assert importances == sorted(importances, reverse=True)


def test_submit_attempt_correct_and_persist(client, auth_headers) -> None:
    """正确作答得 100 分并落库，可通过 attempts/history 查询。"""
    item = _pick_gradable_item(client, auth_headers, CELL_KP)
    response = client.post(
        f"/api/quiz/{CELL_KP}/attempt",
        json={"qid": item["qid"], "user_answer": item["answer"]},
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["grade"]["score"] == 100
    assert body["grade"]["is_correct"] is True
    # kp 未加入复习库 → review_state 为 null（不创建）
    assert body["review_state"] is None

    attempts = client.get(f"/api/quiz/{CELL_KP}/attempts", headers=auth_headers).json()
    assert len(attempts) >= 1
    assert attempts[0]["qid"] == item["qid"]
    assert attempts[0]["score"] == 100

    history = client.get("/api/quiz/history", headers=auth_headers).json()
    assert any(record["qid"] == item["qid"] for record in history)


def test_submit_attempt_wrong_answer(client, auth_headers) -> None:
    """错误作答得 0 分。"""
    item = _pick_gradable_item(client, auth_headers, CELL_KP)
    response = client.post(
        f"/api/quiz/{CELL_KP}/attempt",
        json={"qid": item["qid"], "user_answer": "__一定错误的答案__"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert response.json()["grade"]["score"] == 0
    assert response.json()["grade"]["is_correct"] is False


def test_submit_attempt_updates_review_state_when_enrolled(client, auth_headers) -> None:
    """kp 已加入复习时，正确作答按映射推进 review_state（AC-R3-4）。"""
    client.post("/api/review/items", json={"kp_id": CELL_KP}, headers=auth_headers)
    item = _pick_gradable_item(client, auth_headers, CELL_KP)

    response = client.post(
        f"/api/quiz/{CELL_KP}/attempt",
        json={"qid": item["qid"], "user_answer": item["answer"]},
        headers=auth_headers,
    )
    assert response.status_code == 200
    review_state = response.json()["review_state"]
    assert review_state is not None
    # score=100 → grade 5 → level 由 0 提升至 1
    assert review_state["level"] == 1
    assert review_state["review_count"] == 1


def test_submit_attempt_unknown_qid_returns_404(client, auth_headers) -> None:
    """未知 qid → 404 QUIZ_ITEM_NOT_FOUND。"""
    response = client.post(
        f"/api/quiz/{CELL_KP}/attempt",
        json={"qid": "__no_such_qid__", "user_answer": "A"},
        headers=auth_headers,
    )
    assert response.status_code == 404
    assert response.json()["code"] == "QUIZ_ITEM_NOT_FOUND"


def test_submit_attempt_unknown_kp_returns_404(client, auth_headers) -> None:
    """未知 kp → 404 KP_NOT_FOUND。"""
    response = client.post(
        "/api/quiz/no-such-kp/attempt",
        json={"qid": "q1", "user_answer": "A"},
        headers=auth_headers,
    )
    assert response.status_code == 404
    assert response.json()["code"] == "KP_NOT_FOUND"


def test_missing_user_id_returns_400(client) -> None:
    """缺 X-User-Id → 400 MISSING_USER_ID。"""
    response = client.post(
        f"/api/quiz/{CELL_KP}/attempt",
        json={"qid": "q1", "user_answer": "A"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "MISSING_USER_ID"