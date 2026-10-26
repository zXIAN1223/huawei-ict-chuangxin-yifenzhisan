"""学习理论核心模块：艾宾浩斯曲线、学习金字塔、SM-2间隔重复等"""
from datetime import datetime, timedelta
import math

EBBINGHAUS_INTERVALS = [1, 2, 4, 7, 15, 30]

LEARNING_PYRAMID = [
    {"level": "讲授(Lecture)", "retention": 5, "type": "被动"},
    {"level": "阅读(Reading)", "retention": 10, "type": "被动"},
    {"level": "视听(Audio-Visual)", "retention": 20, "type": "被动"},
    {"level": "演示(Demonstration)", "retention": 30, "type": "被动"},
    {"level": "讨论(Discussion)", "retention": 50, "type": "主动"},
    {"level": "实践(Practice)", "retention": 75, "type": "主动"},
    {"level": "教授他人(Teach Others)", "retention": 90, "type": "主动"},
]

CONTENT_TYPES = {
    "factual": {
        "name": "事实性知识",
        "desc": "名称、数据、事实等，遗忘最快，需更频繁复习",
        "stability": 0.8,
        "interval_factor": 0.7,
    },
    "conceptual": {
        "name": "概念性知识",
        "desc": "原理、机制、概念，遗忘速度中等",
        "stability": 1.0,
        "interval_factor": 1.0,
    },
    "procedural": {
        "name": "程序性知识",
        "desc": "实验步骤、操作流程，需实践巩固，遗忘较慢",
        "stability": 1.3,
        "interval_factor": 1.2,
    },
}

LEARNING_THEORIES = {
    "ebbinghaus": {
        "name": "艾宾浩斯遗忘曲线",
        "category": "记忆",
        "desc": "遗忘是不均衡的，先快后慢。在特定时间间隔复习可大幅提升长期记忆保持率。",
        "application": "根据知识点类型生成个性化复习时间表，主动提醒复习。",
    },
    "active_recall": {
        "name": "主动回忆学习法",
        "category": "记忆",
        "desc": "主动从记忆中提取信息比反复阅读更有效，检索练习能强化记忆痕迹。",
        "application": "AI主动提问，引导用户回忆而非被动浏览。",
    },
    "learning_pyramid": {
        "name": "学习金字塔",
        "category": "学习方法",
        "desc": "主动学习(讨论50%/实践75%/教授他人90%)远比被动学习(讲授5%/阅读10%)高效。",
        "application": "提供讨论场景与'教授AI'模式，让用户以教促学。",
    },
    "spaced_repetition": {
        "name": "间隔重复(Spaced Repetition)",
        "category": "记忆",
        "desc": "将复习分散到逐渐拉长的时间间隔中，比集中重复更有效(SM-2算法)。",
        "application": "基于SM-2算法动态调整每个知识点的复习间隔。",
    },
    "interleaving": {
        "name": "交错学习(Interleaving)",
        "category": "学习方法",
        "desc": "混合学习不同类型内容，比逐块学习更能促进迁移与区分能力。",
        "application": "复习与提问时混合不同章节与难度，避免单一刷题。",
    },
    "feynman": {
        "name": "费曼学习法(Feynman Technique)",
        "category": "学习方法",
        "desc": "用最简单的语言把概念讲给外行听，讲不清说明没真懂，再回头补漏。",
        "application": "'教授AI'模式中AI扮演初学者提问，逼用户厘清表述。",
    },
    "metacognition": {
        "name": "元认知(Metacognition)",
        "category": "认知",
        "desc": "对自身学习过程的认知与监控：知道自己会什么、不会什么、该怎么学。",
        "application": "学习方式测试+掌握度仪表盘，让用户看见自己的薄弱点。",
    },
    "elaboration": {
        "name": "精细加工(Elaboration)",
        "category": "认知",
        "desc": "将新信息与已有知识建立深层联系(举例、类比、追问为什么)，加深理解。",
        "application": "讨论中AI追问'为什么''举个例子'，引导精细加工。",
    },
    "dual_coding": {
        "name": "双重编码理论(Dual Coding)",
        "category": "认知",
        "desc": "同时用语言和视觉表征信息，两条通道编码提升记忆与理解。",
        "application": "知识点配结构化图示描述与文字双通道呈现。",
    },
}


def retention_rate(days_since_learn, stability=1.0):
    """艾宾浩斯遗忘曲线 R = e^(-t/S)，返回保持率(0-1)"""
    return math.exp(-days_since_learn / (stability * 2.0))


def next_review_interval(stage, content_type="conceptual", quality=3):
    """根据复习阶段、内容类型、回答质量计算下次复习间隔(天)"""
    base = EBBINGHAUS_INTERVALS[min(stage, len(EBBINGHAUS_INTERVALS) - 1)]
    factor = CONTENT_TYPES.get(content_type, CONTENT_TYPES["conceptual"])["interval_factor"]
    quality_adjust = 1.0
    if quality < 3:
        quality_adjust = 0.5
    elif quality >= 4:
        quality_adjust = 1.3
    return max(1, round(base * factor * quality_adjust))


def sm2_update(ef, interval, repetitions, quality):
    """SM-2算法核心更新，返回(新EF, 新interval, 新repetitions)"""
    if quality < 3:
        return ef, 1, 0
    if repetitions == 0:
        new_interval = 1
    elif repetitions == 1:
        new_interval = 6
    else:
        new_interval = round(interval * ef)
    new_ef = ef + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    new_ef = max(1.3, new_ef)
    return new_ef, new_interval, repetitions + 1


def quality_from_score(score):
    """将0-1的得分映射为SM-2的0-5质量等级"""
    if score >= 0.9:
        return 5
    elif score >= 0.75:
        return 4
    elif score >= 0.5:
        return 3
    elif score >= 0.3:
        return 2
    else:
        return 1