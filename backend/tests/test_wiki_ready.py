"""`wiki_ready` 口径统一回归测试（k121）。

背景：同一个知识库在三个接口上会给出相反结论，因为判定各写了一套：
- `serializers.kb_out()`：已发布且 `current_version_id` 非空（= plan_route 口径）
- `routers/kbs.py:_index_status()`：`WikiSection` 计数 > 0（草稿页、空页都算）
- `routers/wiki.py:wiki_tree()`：任一页面 `status != draft`（不看正文版本）

现在三处共用 `serializers.wiki_ready()` / `wiki_published_count()`。
本文件把四条状态锁死：草稿页、发布但无正文版本、只有章节没有合格页面，
以及真实的「已发布且有版本」——前三条旧口径都会误报，最后一条不能误报成 False。
"""
from __future__ import annotations

import uuid

from sqlalchemy import func, select

from app import models, serializers
from app.services import retrieval, worker

DOC_MD = """# 差旅报销办法

## 住宿标准

一线城市住宿上限 600 元每晚，其他城市 400 元每晚。

## 交通标准

高铁二等座为标准配置，市内交通实报实销。

## 审批流程

提交人提交单据后，依次经过直属主管、部门负责人、财务复核，最后出纳付款。
"""


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _new_kb(app_client, admin_headers, mode: str = "wiki") -> int:
    kb = app_client.post("/api/kbs", headers=admin_headers,
                         json={"name": _unique("口径"), "description": "", "mode": mode}).json()
    return kb["id"]


def _three(app_client, admin_headers, kb_id: int) -> tuple[bool, bool, bool]:
    """三个出口各自的 wiki_ready：(详情 kb_out, 详情 index, Wiki 树)。"""
    detail = app_client.get(f"/api/kbs/{kb_id}", headers=admin_headers).json()
    tree = app_client.get(f"/api/kbs/{kb_id}/wiki/tree", headers=admin_headers).json()
    return detail["wiki_ready"], detail["index"]["wiki_ready"], tree["wiki_ready"]


def _assert_all_agree(db, app_client, admin_headers, kb_id: int, expected: bool) -> None:
    """三处展示口径一致，且与检索侧 `plan_route()` 的判定一致。

    最后一个条件才是真正的护栏：展示说「已就绪」而检索其实回退到分块
    （或反之），就是这次要修的 bug 的本质。`plan_route()` 是检索的实际口径，
    它的判定不能被展示层带偏。
    """
    assert _three(app_client, admin_headers, kb_id) == (expected, expected, expected)
    db.expire_all()
    kb = db.get(models.KnowledgeBase, kb_id)
    assert kb is not None
    used, _, _ = retrieval.plan_route(db, kb)
    assert (used == "wiki") is expected, (
        f"展示口径={expected} 与检索路由 used={used} 不一致，口径又漂了")


def _page(db, kb_id: int, slug: str, *, status: str, with_version: bool,
          sections: int = 0) -> models.WikiPage:
    """直接落库造页面，精确控制「状态 / 是否有正文版本 / 章节数」三个变量。"""
    page = models.WikiPage(kb_id=kb_id, slug=slug, title=f"页面 {slug}", summary="",
                           status=status, order_index=0, parent_slug="")
    db.add(page)
    db.flush()
    version = None
    if with_version:
        version = models.WikiPageVersion(
            page_id=page.id, version_no=1,
            content_md="## 小节\n\n正文内容足够长，可以切出检索单元。",
            summary="", origin="generated", note="自动生成",
        )
        db.add(version)
        db.flush()
        page.current_version_id = version.id
        db.add(page)
    # 章节必须挂在正文版本上（FK + 语义），没有版本就没有可切分的正文
    for i in range(sections if version else 0):
        db.add(models.WikiSection(
            kb_id=kb_id, page_id=page.id, page_version_id=version.id, ordinal=i,
            heading_path=f"小节 {i}", text="章节正文，用于模拟已有检索单元。" * 3,
        ))
    db.commit()
    return page


def test_draft_page_is_not_ready(app_client, admin_headers, db_session):
    """只有草稿页：三处都必须是 False（草稿页用户看不到，不能算就绪）。"""
    kb_id = _new_kb(app_client, admin_headers)
    _page(db_session, kb_id, "draft-only",
          status=models.WikiPageStatus.draft.value, with_version=True, sections=3)

    _assert_all_agree(db_session, app_client, admin_headers, kb_id, False)


def test_published_without_version_is_not_ready(app_client, admin_headers, db_session):
    """已发布但 `current_version_id` 为空：没有正文，检索侧用不到，不算就绪。

    旧 `wiki_tree()` 只看 `status != draft`，这里会误报 True。
    """
    kb_id = _new_kb(app_client, admin_headers)
    _page(db_session, kb_id, "published-no-version",
          status=models.WikiPageStatus.published.value, with_version=False)

    _assert_all_agree(db_session, app_client, admin_headers, kb_id, False)


def test_published_with_version_is_ready(app_client, admin_headers, db_session):
    """已发布且有正文版本：三处都必须是 True，且与共用判定函数一致。"""
    kb_id = _new_kb(app_client, admin_headers)
    _page(db_session, kb_id, "published-ok",
          status=models.WikiPageStatus.published.value, with_version=True, sections=2)

    _assert_all_agree(db_session, app_client, admin_headers, kb_id, True)
    assert serializers.wiki_published_count(db_session, kb_id) == 1
    assert serializers.wiki_ready(db_session, kb_id) is True


def test_sections_without_qualifying_page_is_not_ready(app_client, admin_headers, db_session):
    """有 WikiSection 但没有合格页面：旧 `_index_status()` 会误报 True。

    这是口径分歧里最要命的一种：页面还是草稿、或发布后丢了正文版本，
    章节残留下来，详情页于是显示「Wiki 已就绪」，而检索侧根本没有可用页面。
    """
    kb_id = _new_kb(app_client, admin_headers)
    _page(db_session, kb_id, "draft-with-sections",
          status=models.WikiPageStatus.draft.value, with_version=True, sections=4)
    _page(db_session, kb_id, "published-no-version",
          status=models.WikiPageStatus.published.value, with_version=False)

    # 前提校验：章节确实存在，否则这条用例就测不到旧口径的误判
    sections = db_session.execute(
        select(func.count(models.WikiSection.id)).where(models.WikiSection.kb_id == kb_id)
    ).scalar()
    assert sections == 4, "章节没造出来，用例失去意义"

    _assert_all_agree(db_session, app_client, admin_headers, kb_id, False)


def test_mixed_pages_ready_when_any_page_qualifies(app_client, admin_headers, db_session):
    """一库多页：只要有一个合格页面就算就绪，草稿页不参与判定也不拖累结果。"""
    kb_id = _new_kb(app_client, admin_headers)
    _page(db_session, kb_id, "draft-a", status=models.WikiPageStatus.draft.value,
          with_version=True, sections=1)
    _page(db_session, kb_id, "needs-review-b",
          status=models.WikiPageStatus.needs_review.value, with_version=True, sections=1)

    # needs_review 也算「已发布」——它带着正文版本，检索侧是能用的
    _assert_all_agree(db_session, app_client, admin_headers, kb_id, True)
    assert serializers.wiki_published_count(db_session, kb_id) == 1


def test_real_wiki_build_reports_ready_everywhere(app_client, admin_headers, db_session):
    """真实生成一次 Wiki：三处口径必须同时为 True（避免只对造出来的数据成立）。"""
    kb = app_client.post("/api/kbs", headers=admin_headers,
                         json={"name": _unique("真实Wiki"), "description": "",
                               "mode": "wiki"}).json()
    up = app_client.post(f"/api/kbs/{kb['id']}/documents", headers=admin_headers,
                         files=[("files", ("wiki-ready.md", DOC_MD.encode(), "text/markdown"))])
    assert up.status_code == 200, up.text
    while worker.run_once("test"):
        pass

    tree = app_client.get(f"/api/kbs/{kb['id']}/wiki/tree", headers=admin_headers).json()
    assert tree["total"] > 0, tree
    _assert_all_agree(db_session, app_client, admin_headers, kb["id"], True)
