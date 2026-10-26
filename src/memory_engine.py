"""记忆调度引擎：基于艾宾浩斯曲线与SM-2算法安排复习，结合用户时间分配"""
from datetime import datetime, timedelta
from . import theory, storage, knowledge_base


def add_knowledge_point(kp_id):
    """将知识点加入复习计划（首次学习）"""
    kp = knowledge_base.get_knowledge_point(kp_id)
    if not kp:
        return
    existing = storage.get_review_record(kp_id)
    if existing:
        return
    now = datetime.now()
    storage.upsert_review_record(
        kp_id=kp_id,
        first_learned_at=now.isoformat(),
        last_reviewed_at=now.isoformat(),
        stage=0,
        ef=2.5,
        interval=0,
        repetitions=0,
        next_review_at=(now + timedelta(days=1)).isoformat(),
        mastery=0.1,
        review_count=1,
        content_type=kp["content_type"],
    )


def get_due_reviews(now=None):
    """获取当前已到期的复习知识点"""
    if now is None:
        now = datetime.now()
    records = storage.get_all_review_records()
    due = []
    for r in records:
        next_at = datetime.fromisoformat(r["next_review_at"])
        if next_at <= now:
            kp = knowledge_base.get_knowledge_point(r["kp_id"])
            if kp:
                due.append({"record": r, "kp": kp})
    due.sort(key=lambda x: x["record"]["mastery"])
    return due


def get_today_reviews(limit=None):
    """获取今日复习计划，结合用户每日上限"""
    due = get_due_reviews()
    if limit is None:
        profile = storage.get_profile()
        limit = profile["daily_review_limit"] if profile else 5
    return due[:limit]


def record_review(kp_id, quality):
    """记录一次复习结果，更新SM-2参数与下次复习时间"""
    record = storage.get_review_record(kp_id)
    if not record:
        add_knowledge_point(kp_id)
        record = storage.get_review_record(kp_id)
    ef, interval, reps = theory.sm2_update(
        record["ef"], record["interval"], record["repetitions"], quality)
    stage = min(record["stage"] + 1, len(theory.EBBINGHAUS_INTERVALS) - 1)
    if quality < 3:
        stage = max(0, stage - 1)
    next_interval = theory.next_review_interval(stage, record["content_type"], quality)
    now = datetime.now()
    if quality >= 3:
        mastery = min(1.0, record["mastery"] + 0.15)
    else:
        mastery = max(0.0, record["mastery"] - 0.1)
    storage.upsert_review_record(
        kp_id=kp_id,
        first_learned_at=record["first_learned_at"],
        last_reviewed_at=now.isoformat(),
        stage=stage,
        ef=ef,
        interval=interval,
        repetitions=reps,
        next_review_at=(now + timedelta(days=next_interval)).isoformat(),
        mastery=mastery,
        review_count=record["review_count"] + 1,
        content_type=record["content_type"],
    )
    return {"next_interval": next_interval, "mastery": mastery, "stage": stage}


def get_mastery_overview():
    """获取所有已学知识点的掌握度与当前保持率概览"""
    records = storage.get_all_review_records()
    overview = []
    for r in records:
        kp = knowledge_base.get_knowledge_point(r["kp_id"])
        if kp:
            days = (datetime.now() - datetime.fromisoformat(r["last_reviewed_at"])).days
            stability = theory.CONTENT_TYPES.get(r["content_type"], {}).get("stability", 1.0)
            retention = theory.retention_rate(days, stability)
            overview.append({
                "kp_id": r["kp_id"],
                "title": kp["title"],
                "chapter": kp["chapter"],
                "content_type": r["content_type"],
                "mastery": r["mastery"],
                "retention": retention,
                "next_review": r["next_review_at"],
                "stage": r["stage"],
                "review_count": r["review_count"],
            })
    return overview


def get_upcoming_reminders(days=7):
    """获取未来N天的复习提醒"""
    now = datetime.now()
    end = now + timedelta(days=days)
    records = storage.get_all_review_records()
    reminders = []
    for r in records:
        next_at = datetime.fromisoformat(r["next_review_at"])
        if now < next_at <= end:
            kp = knowledge_base.get_knowledge_point(r["kp_id"])
            if kp:
                reminders.append({
                    "kp_id": r["kp_id"],
                    "title": kp["title"],
                    "chapter": kp["chapter"],
                    "date": next_at,
                    "days_away": (next_at - now).days,
                    "stage": r["stage"],
                })
    reminders.sort(key=lambda x: x["date"])
    return reminders


def estimate_review_time(num_items):
    """估算复习所需时间（分钟），用于结合用户时间分配"""
    return num_items * 4


def get_study_recommendation():
    """根据用户可用时间与待复习数量给出今日学习建议"""
    due = get_due_reviews()
    profile = storage.get_profile()
    limit = profile["daily_review_limit"] if profile else 5
    minutes = profile["daily_minutes"] if profile else 30
    today = due[:limit]
    est_time = estimate_review_time(len(today))
    if est_time > minutes:
        adjusted = max(1, minutes // 4)
        return {
            "planned": today[:adjusted],
            "est_time": estimate_review_time(adjusted),
            "note": f"今日可用约{minutes}分钟，已为你精简到{adjusted}个最优先复习项。"
        }
    return {
        "planned": today,
        "est_time": est_time,
        "note": f"今日计划复习{len(today)}个知识点，预计{est_time}分钟，在可用时间内。"
    }