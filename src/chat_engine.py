"""模拟AI对话引擎：知识问答、深入讨论(精细加工)、费曼教授模式(学习金字塔顶端)"""
import random
import re
from . import knowledge_base, storage


MODES = {
    "qa": {"name": "知识问答", "desc": "向AI提问，获取知识点讲解"},
    "discuss": {"name": "深入讨论", "desc": "与AI讨论，AI会追问引导你深度思考(精细加工)"},
    "teach": {"name": "教授AI(费曼法)", "desc": "你来讲解，AI扮演初学者提问，以教促学"},
}

ELABORATION_PROMPTS = [
    "追问一下：你能说说**为什么**会这样吗？背后的机制是什么？",
    "能举一个具体的**例子**来说明吗？",
    "这和之前学过的哪个知识点有联系？试着关联一下。",
    "如果条件改变了，结果会怎样变化？",
    "用自己的话再总结一句，检验是否真正理解。",
]


def _find_relevant_kps(user_input, kp_id=None):
    if kp_id:
        kp = knowledge_base.get_knowledge_point(kp_id)
        return [kp] if kp else []
    words = re.findall(r"[\u4e00-\u9fa5]+|[a-zA-Z0-9]+", user_input)
    scored = []
    for kp in knowledge_base.get_all_knowledge_points():
        text = kp["title"] + " " + kp["summary"] + " " + " ".join(kp["key_points"])
        score = 0
        for w in words:
            if len(w) >= 2 and w in text:
                score += 1
        if score > 0:
            scored.append((score, kp))
    scored.sort(key=lambda x: -x[0])
    return [kp for _, kp in scored[:3]]


def _format_kp_answer(kp):
    lines = [f"**{kp['title']}**", "", kp["details"], "", "核心要点："]
    for i, pt in enumerate(kp["key_points"], 1):
        lines.append(f"  {i}. {pt}")
    return "\n".join(lines)


def respond(user_input, mode="discuss", kp_id=None):
    """生成AI回复并持久化聊天记录"""
    user_input = user_input.strip()
    storage.add_chat_message("user", user_input, mode, kp_id)
    if mode == "teach":
        reply = _teach_response(user_input, kp_id)
    elif mode == "qa":
        reply = _qa_response(user_input, kp_id)
    else:
        reply = _discuss_response(user_input, kp_id)
    storage.add_chat_message("assistant", reply, mode, kp_id)
    return reply


def _qa_response(user_input, kp_id=None):
    kps = _find_relevant_kps(user_input, kp_id)
    if kps:
        parts = [_format_kp_answer(kps[0])]
        if len(kps) > 1:
            parts.append("")
            parts.append(f"相关知识点：{'、'.join(kp['title'] for kp in kps[1:])}")
        return "\n".join(parts)
    return "这个问题我在当前生物知识库中没有找到直接匹配。你可以换个关键词，或在'深入讨论'模式里我们一起探讨。"


def _discuss_response(user_input, kp_id=None):
    kps = _find_relevant_kps(user_input, kp_id)
    parts = []
    if kps:
        parts.append(_format_kp_answer(kps[0]))
    else:
        parts.append("这是一个值得思考的问题。你可以告诉我具体涉及哪个知识点吗？这样我能更准确地和你讨论。")
    parts.append("")
    parts.append(random.choice(ELABORATION_PROMPTS))
    return "\n".join(parts)


def _teach_response(user_input, kp_id=None):
    """费曼教授模式：AI扮演初学者，检验用户讲解并针对薄弱点提问"""
    kp = knowledge_base.get_knowledge_point(kp_id) if kp_id else None
    if not kp:
        kps = _find_relevant_kps(user_input)
        kp = kps[0] if kps else None
    if not kp:
        return "好的，你想教我哪个知识点呢？可以先选择一个知识点，然后用最简单的话讲给我听。"
    key_points = kp["key_points"]
    covered = []
    for pt in key_points:
        tokens = [w for w in re.split(r"[（）()\s、，,:：]", pt) if len(w) >= 2]
        if any(t in user_input for t in tokens):
            covered.append(pt)
    uncovered = [pt for pt in key_points if pt not in covered]
    if not user_input or len(user_input) < 15:
        return (f"我是生物初学者，想学**{kp['title']}**。"
                f"请你用最简单、通俗的话讲给我听，就像讲给完全不懂的人一样。")
    if uncovered:
        pt = uncovered[0]
        student_qs = [
            f"嗯，你讲得有点快。关于「{pt}」，我还没听明白，能再详细说说吗？",
            f"等一下，你好像没提到「{pt}」？这个部分很重要吧，具体是什么意思？",
            f"我有点糊涂了，「{pt}」能不能举个例子帮我理解？",
        ]
        return random.choice(student_qs)
    if len(user_input) < 50:
        token = random.choice(covered)
        return f"你讲到了要点，但有点简略。能把「{token}」展开讲讲吗？我想听得更明白。"
    praise = random.choice([
        f"讲得真清楚！我现在理解「{kp['title']}」了。你把核心要点都讲到了，而且通俗易懂。",
        f"太棒了，我学会了！你用简单的语言把「{kp['title']}」讲明白了，说明你真的掌握了。",
    ])
    followup = ""
    if kp.get("questions"):
        followup = "\n\n既然讲得这么好，我考考你：" + kp["questions"][0]["q"]
    return praise + followup


def get_history(mode=None, kp_id=None):
    return storage.get_chat_history(mode, kp_id)


def clear_history(mode=None):
    storage.clear_chat_history(mode)