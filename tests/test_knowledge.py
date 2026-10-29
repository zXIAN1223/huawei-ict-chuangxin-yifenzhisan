"""C1-KNOWLEDGE@1 契约测试：内容加载器与知识点查询 API（plan 2.2.1 / 3.3 T13）。

覆盖：
- ``ContentRepository`` 缺文件容忍 / 坏 schema 抛 ``ContentSchemaError``；
- ``/api/knowledge`` 6 个端点的响应形状与过滤语义；
- 404 错误体（``CHAPTER_NOT_FOUND`` / ``KP_NOT_FOUND``）、422 校验错误体形状；
- 测试库与运行库隔离（AC-R8-3）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from backend.app.data.content_loader import (
    ContentRepository,
    ContentSchemaError,
)


# ---------------------------------------------------------------------------
# ContentRepository 单元测试
# ---------------------------------------------------------------------------


def test_load_all_tolerates_missing_chapter_files(tmp_path: Path) -> None:
    """目录中缺少章节文件时应记录 warning 并跳过，而不是抛错。"""
    empty_dir = tmp_path / "empty_data"
    empty_dir.mkdir()

    repository = ContentRepository(empty_dir)

    # 不抛异常
    repository.load_all()
    assert repository.loaded is True
    assert repository.list_chapters() == []


def test_load_all_raises_content_schema_error_on_bad_schema(tmp_path: Path) -> None:
    """文件存在但不符合 C1 schema（此处 knowledge_points 为空）应抛 ContentSchemaError。"""
    data_dir = tmp_path / "bad_data"
    data_dir.mkdir()
    bad_chapter = {
        "subject": "biology",
        "chapter_id": "bad",
        "chapter_title": "坏章节",
        "chapter_summary": "用于测试 schema 校验",
        "knowledge_points": [],
    }
    (data_dir / "biology_bad.json").write_text(
        json.dumps(bad_chapter, ensure_ascii=False), encoding="utf-8"
    )

    repository = ContentRepository(data_dir)

    with pytest.raises(ContentSchemaError) as exc_info:
        repository.load_all()
    assert exc_info.value.file is not None
    assert "knowledge_points" in exc_info.value.message


def test_real_repository_loads_two_chapters_with_expected_scale(repo) -> None:
    """真实内容库应加载两章且知识点总数 >= 16（R1.2）。"""
    chapters = repo.list_chapters()
    chapter_ids = {chapter.chapter_id for chapter in chapters}
    assert chapter_ids == {"cells", "genetics"}

    total_kp = sum(chapter.kp_count for chapter in chapters)
    assert total_kp >= 16

    # 元数据过滤：cells 章节全部知识点均属于 cells
    cells_kps = repo.list_knowledge_points(chapter_id="cells")
    assert len(cells_kps) == next(
        chapter.kp_count for chapter in chapters if chapter.chapter_id == "cells"
    )
    assert all(meta.chapter_id == "cells" for meta in cells_kps)


def test_list_knowledge_points_difficulty_range_filter(repo) -> None:
    """难度区间过滤为闭区间。"""
    all_kps = repo.list_knowledge_points()
    filtered = repo.list_knowledge_points(difficulty_min=3, difficulty_max=4)
    assert filtered, "应至少命中一个难度在 3..4 的知识点"
    assert all(3 <= meta.difficulty <= 4 for meta in filtered)
    assert len(filtered) <= len(all_kps)


# ---------------------------------------------------------------------------
# API 测试
# ---------------------------------------------------------------------------


def test_missing_user_id_returns_400(client) -> None:
    """缺少 X-User-Id 时统一返回 400 MISSING_USER_ID（2.3.2）。"""
    # profile 端点依赖 get_user_id
    response = client.get("/api/profile")
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "MISSING_USER_ID"
    assert "message" in body


def test_get_chapters_returns_metadata(client) -> None:
    """GET /api/knowledge/chapters 返回 ChapterMeta[]。"""
    response = client.get("/api/knowledge/chapters")
    assert response.status_code == 200
    chapters = response.json()
    assert isinstance(chapters, list) and len(chapters) == 2
    for chapter in chapters:
        assert set(chapter.keys()) == {
            "chapter_id",
            "chapter_title",
            "chapter_summary",
            "kp_count",
        }
        assert chapter["kp_count"] >= 8


def test_get_chapter_detail_metadata_and_full(client) -> None:
    """章节详情默认返回知识点元数据；full=true 返回完整知识点。"""
    meta_response = client.get("/api/knowledge/chapters/cells")
    assert meta_response.status_code == 200
    meta_body = meta_response.json()
    assert meta_body["chapter_id"] == "cells"
    first_kp = meta_body["knowledge_points"][0]
    # 元数据视图不含 content_blocks
    assert "content_blocks" not in first_kp
    assert "kp_id" in first_kp and "quiz_count" in first_kp

    full_response = client.get("/api/knowledge/chapters/cells", params={"full": "true"})
    assert full_response.status_code == 200
    full_body = full_response.json()
    full_kp = full_body["knowledge_points"][0]
    assert "content_blocks" in full_kp
    assert "mindmap" in full_kp and "quiz" in full_kp


def test_get_chapter_not_found_error_shape(client) -> None:
    """未知 chapter_id 返回 404 CHAPTER_NOT_FOUND 且错误体为 {code,message,detail?}。"""
    response = client.get("/api/knowledge/chapters/does-not-exist")
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "CHAPTER_NOT_FOUND"
    assert isinstance(body["message"], str) and body["message"]
    assert "detail" in body


def test_get_knowledge_point_full_shape(client) -> None:
    """GET /api/knowledge/kps/{kp_id} 返回完整知识点（含 mindmap/images/quiz）。"""
    response = client.get("/api/knowledge/kps/cell-membrane")
    assert response.status_code == 200
    kp = response.json()
    assert kp["kp_id"] == "cell-membrane"
    assert len(kp["content_blocks"]) >= 1
    assert len(kp["images"]) >= 1
    assert len(kp["quiz"]) >= 2
    # AC-R1-1：mindmap 至少根 + 1 子节点
    assert kp["mindmap"]["label"]
    assert len(kp["mindmap"]["children"]) >= 1
    for image in kp["images"]:
        assert image["src"].startswith("/static/img/biology/")
        assert image["caption"] is not None


def test_get_knowledge_point_not_found(client) -> None:
    """未知 kp_id 返回 404 KP_NOT_FOUND。"""
    response = client.get("/api/knowledge/kps/no-such-kp")
    assert response.status_code == 404
    assert response.json()["code"] == "KP_NOT_FOUND"


def test_get_mindmap_and_images_endpoints(client) -> None:
    """思维导图与配图端点返回契约形状。"""
    mindmap_response = client.get("/api/knowledge/kps/cell-membrane/mindmap")
    assert mindmap_response.status_code == 200
    mindmap = mindmap_response.json()
    assert set(mindmap.keys()) >= {"id", "label", "children"}

    images_response = client.get("/api/knowledge/kps/cell-membrane/images")
    assert images_response.status_code == 200
    images = images_response.json()
    assert isinstance(images, list) and len(images) >= 1
    assert set(images[0].keys()) == {"src", "caption", "alt"}


def test_get_mindmap_not_found(client) -> None:
    """思维导图端点对未知 kp_id 返回 404 KP_NOT_FOUND。"""
    response = client.get("/api/knowledge/kps/no-such-kp/mindmap")
    assert response.status_code == 404
    assert response.json()["code"] == "KP_NOT_FOUND"


def test_kps_filter_by_chapter_and_difficulty(client) -> None:
    """GET /api/knowledge/kps 支持 chapter_id 与难度区间组合过滤。"""
    response = client.get(
        "/api/knowledge/kps",
        params={"chapter_id": "genetics", "difficulty_min": 2, "difficulty_max": 5},
    )
    assert response.status_code == 200
    items = response.json()
    assert items
    for item in items:
        assert item["chapter_id"] == "genetics"
        assert 2 <= item["difficulty"] <= 5
        # 元数据不含正文
        assert "content_blocks" not in item


def test_kps_invalid_query_returns_422_validation_error(client) -> None:
    """非法查询参数（difficulty_min=0 违反 ge=1）返回 422 与统一校验错误体。"""
    response = client.get("/api/knowledge/kps", params={"difficulty_min": 0})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert isinstance(body["message"], str)
    assert "detail" in body


# ---------------------------------------------------------------------------
# 数据库隔离（AC-R8-3）
# ---------------------------------------------------------------------------


def test_test_db_is_isolated_from_runtime_db(temp_db_path, runtime_db_path) -> None:
    """测试库必须与运行库物理隔离，且 ALA_DB_PATH 指向测试库。"""
    assert temp_db_path.resolve() != runtime_db_path.resolve()
    assert Path(os.environ["ALA_DB_PATH"]).resolve() == temp_db_path.resolve()
    # 运行库不应是测试库
    assert not str(temp_db_path).endswith("app.db")