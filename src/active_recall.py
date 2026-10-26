"""主动回忆学习法：提问生成、回答评估、交错学习"""
import random
from . import knowledge_base, storage, theory


def generate_question(kp_id, style=None, used_questions=None):
    """根据知识点生成主动回忆问题，结合学习风格调整提问方式"""
    kp = knowledge_base.get_knowledge_point(kp_id)
    if not kp:
        return None
    questions = kp.get("questions", [])
    if not questions:
        return None
    used = used_questions or []
    available = [q for q in questions if q["q"] not in used]
    if not available:
        available = questions
    question = random.choice(available)
    style_prefix = ""
    if style:
        prefixes = {
            "V": "[视觉化思考] 试着在脑中画出结构图再回答：",
            "A": "[口述想象] 假设你要讲给同学听：",
            "R": "[精细回忆] 请尽量完整、准确地写出：",
            "K": "[情境联想] 联系实验或生活场景回答：",
        }
        style_prefix = prefixes.get(style, "")
    return {
        "kp_id": kp_id,
        "title": kp["title"],
        "type": question["type"],
        "question": style_prefix + question["q"],
        "keywords": question["keywords"],
        "hint": kp["summary"],
    }


def evaluate_answer(question_info, answer):
    """评估用户回答：基于关键词匹配计算得分(0-1)与SM-2质量等级"""
    if not answer.strip():
        return {"score": 0.0, "feedback": "还没有作答哦，试着回忆一下再写下来。", "quality": 1}
    answer_lower = answer.lower()
    keywords = question_info["keywords"]
    hit = sum(1 for kw in keywords if kw.lower() in answer_lower)
    score = hit / len(keywords) if keywords else 0.5
    length_bonus = min(0.15, len(answer) / 300)
    score = min(1.0, score + length_bonus)
    quality = theory.quality_from_score(score)
    if score >= 0.8:
        feedback = "回答得很棒！关键点都覆盖到了，记忆很扎实。"
    elif score >= 0.5:
        missed = [kw for kw in keywords if kw.lower() not in answer_lower]
        feedback = f"不错，但还可以更完整。提示：可以补充这些要点——{'、'.join(missed)}。"
    else:
        feedback = f"似乎有些遗忘了。参考要点：{'、'.join(keywords)}。建议重新学习后再回忆。"
    return {"score": round(score, 2), "feedback": feedback, "quality": quality}


def get_interleaved_questions(count=5, style=None):
    """交错学习：从不同章节混合生成问题，避免单一刷题"""
    all_kps = knowledge_base.get_all_knowledge_points()
    learned_ids = {r["kp_id"] for r in storage.get_all_review_records()}
    candidates = [kp for kp in all_kps if kp["id"] in learned_ids] or all_kps
    by_chapter = {}
    for kp in candidates:
        by_chapter.setdefault(kp["chapter"], []).append(kp)
    questions = []
    chapters = list(by_chapter.keys())
    random.shuffle(chapters)
    idx = 0
    while len(questions) < count and by_chapter:
        ch = chapters[idx % len(chapters)]
        if by_chapter[ch]:
            kp = by_chapter[ch].pop(random.randrange(len(by_chapter[ch])))
            q = generate_question(kp["id"], style)
            if q:
                questions.append(q)
        else:
            by_chapter.pop(ch, None)
            chapters = list(by_chapter.keys())
            if not chapters:
                break
            continue
        idx += 1
    return questions


def save_answer(kp_id, question, answer, score, quality):
    storage.add_recall_answer(kp_id, question, answer, score, quality)


def get_recent_performance(kp_id=None, limit=20):
    """获取近期答题表现，用于策略优化"""
    answers = storage.get_recall_answers(kp_id, limit)
    if not answers:
        return {"avg_score": 0, "count": 0, "trend": "无数据"}
    avg = sum(a["score"] for a in answers) / len(answers)
    recent_half = answers[:max(1, len(answers) // 2)]
    older_half = answers[max(1, len(answers) // 2):]
    recent_avg = sum(a["score"] for a in recent_half) / len(recent_half) if recent_half else 0
    older_avg = sum(a["score"] for a in older_half) / len(older_half) if older_half else 0
    if recent_avg > older_avg + 0.1:
        trend = "上升"
    elif recent_avg < older_avg - 0.1:
        trend = "下降"
    else:
        trend = "平稳"
    return {"avg_score": round(avg, 2), "count": len(answers), "trend": trend}