"""学习方式测试：基于VARK模型(视觉/听觉/读写/动觉)，推荐最适合的学习方式"""
import json
from . import storage


VARK_DESCRIPTIONS = {
    "V": {"name": "视觉型(Visual)", "code": "V",
          "desc": "偏好图表、结构图、思维导图等视觉呈现",
          "tips": ["多用思维导图梳理知识结构", "画流程图理解动态过程", "用不同颜色标注重点"]},
    "A": {"name": "听觉型(Aural)", "code": "A",
          "desc": "偏好听讲、讨论、口述，通过声音和交流学习",
          "tips": ["把知识点讲给别人听", "录音回放复习", "参与讨论和辩论"]},
    "R": {"name": "读写型(Read/Write)", "code": "R",
          "desc": "偏好阅读文字和做笔记，通过文字输入输出",
          "tips": ["整理详细文字笔记", "用关键词卡片复习", "主动写出回忆内容"]},
    "K": {"name": "动觉型(Kinesthetic)", "code": "K",
          "desc": "偏好动手实践、模拟、角色扮演",
          "tips": ["联系实验操作理解知识", "用实物模型辅助", "情境化学习与角色扮演"]},
}

TEST_QUESTIONS = [
    {"q": "学习一个新的生物概念时，你最容易理解的方式是？",
     "options": {"V": "看细胞结构图或流程图", "A": "听老师讲解或和同学讨论",
                 "R": "阅读课本上的文字描述", "K": "动手做实验或模拟操作"}},
    {"q": "复习时你最倾向哪种方式？",
     "options": {"V": "画思维导图把知识串起来", "A": "把知识讲给自己或别人听",
                 "R": "整理笔记反复阅读", "K": "通过做题和实验来巩固"}},
    {"q": "遇到不懂的问题，你首先会？",
     "options": {"V": "找图示或视频帮助理解", "A": "找人讨论或问老师",
                 "R": "查阅文字资料", "K": "动手尝试或做实验验证"}},
    {"q": "你觉得哪种考试题型最拿手？",
     "options": {"V": "识图作图题", "A": "口试或面试型问答",
                 "R": "文字简答题", "K": "实验设计与操作题"}},
    {"q": "记忆一组知识点时，你倾向？",
     "options": {"V": "用表格或图示分类记忆", "A": "编口诀或朗读记忆",
                 "R": "反复抄写和默写", "K": "联想实际情境记忆"}},
    {"q": "你觉得最有效的课堂是？",
     "options": {"V": "PPT图文丰富的课", "A": "老师讲述生动的课",
                 "R": "有详细讲义的课", "K": "有实验活动的课"}},
]


def get_test():
    return TEST_QUESTIONS


def submit_test(answers):
    """answers: list of 'V'/'A'/'R'/'K'，返回结果分析并保存"""
    scores = {"V": 0, "A": 0, "R": 0, "K": 0}
    for a in answers:
        if a in scores:
            scores[a] += 1
    primary = max(scores, key=scores.get)
    sorted_styles = sorted(scores.items(), key=lambda x: -x[1])
    result = {
        "scores": scores,
        "primary": primary,
        "ranking": sorted_styles,
        "profile": VARK_DESCRIPTIONS[primary],
    }
    storage.save_style_test(json.dumps(result, ensure_ascii=False))
    storage.update_profile(learning_style=primary)
    return result


def get_current_style():
    test = storage.get_latest_style_test()
    if test:
        return json.loads(test["result_json"])
    return None