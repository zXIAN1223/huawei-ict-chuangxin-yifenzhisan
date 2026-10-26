"""高中生物知识库加载与检索"""
import json
import os

_DATA_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "biology.json")
_cache = None


def load_knowledge_base():
    global _cache
    if _cache is None:
        with open(_DATA_PATH, "r", encoding="utf-8") as f:
            _cache = json.load(f)
    return _cache


def get_subject():
    return load_knowledge_base()["subject"]


def get_chapters():
    return load_knowledge_base()["chapters"]


def get_all_knowledge_points():
    return load_knowledge_base()["knowledge_points"]


def get_knowledge_point(kp_id):
    for kp in get_all_knowledge_points():
        if kp["id"] == kp_id:
            return kp
    return None


def get_points_by_chapter(chapter_id):
    return [kp for kp in get_all_knowledge_points() if kp["chapter"] == chapter_id]


def get_chapter_name(chapter_id):
    for ch in get_chapters():
        if ch["id"] == chapter_id:
            return ch["name"]
    return chapter_id


def search_points(keyword):
    kw = keyword.strip().lower()
    if not kw:
        return get_all_knowledge_points()
    results = []
    for kp in get_all_knowledge_points():
        text = (kp["title"] + " " + kp["summary"] + " " + " ".join(kp["key_points"])).lower()
        if kw in text:
            results.append(kp)
    return results