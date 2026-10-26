"""用户模型与学习策略优化：监测使用情况，动态推荐学习策略"""
from . import storage, memory_engine, active_recall, theory
from .learning_test import VARK_DESCRIPTIONS


def get_user_dashboard():
    """汇总用户学习状态，供仪表盘展示"""
    profile = storage.get_profile()
    overview = memory_engine.get_mastery_overview()
    answers = storage.get_recall_answers(limit=20)
    perf = active_recall.get_recent_performance()
    style_code = profile.get("learning_style", "") if profile else ""
    style = VARK_DESCRIPTIONS.get(style_code) if style_code else None
    return {
        "profile": profile,
        "overview": overview,
        "performance": perf,
        "style": style,
        "total_learned": len(overview),
        "avg_mastery": round(sum(o["mastery"] for o in overview) / len(overview), 2) if overview else 0,
        "avg_retention": round(sum(o["retention"] for o in overview) / len(overview), 2) if overview else 0,
        "total_answers": len(answers),
    }


def get_strategy_recommendations():
    """根据用户表现与风格生成策略优化建议(元认知+持续监测)"""
    dashboard = get_user_dashboard()
    recs = []
    perf = dashboard["performance"]
    style = dashboard["style"]
    if perf["count"] == 0:
        recs.append("还没有答题记录，建议先到「主动回忆」练习几道题，我会据此分析你的学习情况。")
    else:
        if perf["avg_score"] < 0.5:
            recs.append("近期主动回忆得分偏低，建议增加复习频率，先巩固基础再挑战应用题。")
        if perf["trend"] == "下降":
            recs.append("答题表现呈下降趋势，可能是遗忘开始显现，建议优先复习掌握度低的知识点。")
        if perf["trend"] == "上升":
            recs.append("答题表现稳步上升，当前策略有效！可以尝试交错学习提升迁移能力。")
    weak = [o for o in dashboard["overview"] if o["mastery"] < 0.4]
    if weak:
        titles = "、".join(o["title"] for o in weak[:3])
        recs.append(f"以下知识点掌握度较低，建议优先复习：{titles}。")
    low_retention = [o for o in dashboard["overview"] if o["retention"] < 0.4]
    if low_retention:
        recs.append("部分知识点遗忘较多(保持率<40%)，建议尽快安排一次主动回忆测试。")
    if style:
        recs.append(f"你是{style['name']}学习者，推荐：{style['tips'][0]}。")
    if not recs:
        recs.append("学习状态良好，继续保持！可以尝试「教授AI」模式挑战自己。")
    return recs


def get_theory_overview():
    """返回所有学习理论，供展示"""
    return theory.LEARNING_THEORIES