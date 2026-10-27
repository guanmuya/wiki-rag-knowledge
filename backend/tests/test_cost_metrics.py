"""模型调用成本统计的回归测试。

这组用例锁的是一个**真实缺陷**：`CallStats` 存在，但三处 `llm.embed()` 没传 stats，
于是查询向量化、Wiki 章节向量化、人工校对后的章节重建全部不计入成本。
论文要报「整理代价」，这三个漏点会让成本被系统性低估，尤其是 Wiki 库——
它的整理成本主要就压在章节向量化上。

所以这里的断言不是「跑通就行」，而是**逐条验证每一次调用都被记账**：
- 断言 stats 对象是被真正传进去的那个（不是新建的替身）；
- 断言 embed_calls / embed_texts 有值，而不是只看 calls；
- 断言离线模式仍然统计（否则本地演示跑出来的成本全是 0，实验没法做）。
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from app import models
from app.services import answering, llm, retrieval, wiki, worker


@pytest.fixture()
def scratch_dir():
    """本文件用的临时目录（不覆盖 pytest 内置的 tmp_path_factory）。"""
    import shutil
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="wrk_cost_"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- 夹具
@pytest.fixture(autouse=True)
def _force_offline_embedding(db_session):
    """清掉模型配置，强制走确定性哈希向量。

    两个原因：
    1. 测试不能发网络请求（其它用例会留下指向 127.0.0.1:9 的假供应商）；
    2. 离线模式才是成本统计最容易出错的分支——它不发 HTTP、没有 usage 字段，
       如果代码只在 live 分支记数，离线就会全 0。
    """
    db_session.execute(delete(models.ModelConfig))
    db_session.commit()
    yield


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _kb_with_chunks(db, mode: str, count: int = 6, headings: int = 4) -> models.KnowledgeBase:
    """造一个带分块的库。分块内容带标题，保证 Wiki 主题归纳能分出多页。"""
    tag = uuid.uuid4().hex[:8]
    kb = models.KnowledgeBase(name=_unique("成本测试库"), mode=mode, status="ready")
    db.add(kb)
    db.commit()
    doc = models.Document(kb_id=kb.id, filename="cost.md", suffix=".md", status="ready")
    db.add(doc)
    db.commit()
    ver = models.DocumentVersion(doc_id=doc.id, version_no=1, storage_path="/tmp/cost",
                                 status="success")
    db.add(ver)
    db.commit()
    for i in range(count):
        h = f"第{i % headings + 1}节 主题"
        db.add(models.Chunk(
            kb_id=kb.id, doc_id=doc.id, doc_version_id=ver.id, ordinal=i,
            text=f"{h}。一线城市住宿上限 600 元每晚，第 {i} 条说明。",
            heading=h, locator=f"第 {i} 段",
            keywords="住宿 上限 一线城市 报销", chunk_hash=f"{tag}-{i}",
        ))
    db.commit()
    return kb


def _spy_embed(monkeypatch) -> list[tuple[list[str], llm.CallStats | None]]:
    """记录每次 embed 调用的入参，并透传给真实实现。"""
    calls: list[tuple[list[str], llm.CallStats | None]] = []
    original = llm.embed

    def spy(db, texts, *, stats=None):
        calls.append((list(texts), stats))
        return original(db, texts, stats=stats)

    monkeypatch.setattr(llm, "embed", spy)
    return calls


# ================================================================ 1. 查询向量化
def test_query_embedding_is_counted(db_session, monkeypatch):
    """retrieval.retrieve 的查询向量化必须计入 stats。

    修复前：llm.embed(db, [query]) 没传 stats → embed_calls 恒为 0，
    每次问答的成本只剩生成那一次调用，问答成本被系统性低估。
    """
    kb = _kb_with_chunks(db_session, "basic", count=5, headings=3)
    calls = _spy_embed(monkeypatch)

    stats = llm.CallStats()
    evidence, routes, _ = retrieval.retrieve(db_session, [kb], "一线城市住宿上限", stats=stats)

    assert evidence, "检索应命中分块"
    assert calls, "检索必须调用 embedding 做查询向量化"
    assert any(s is stats for _, s in calls), (
        "查询向量化的 stats 必须是被传入的那个对象（修复前这里是 None）")
    assert stats.embed_calls >= 1, f"查询向量化未计入 embed_calls：{stats.as_dict()}"
    assert stats.embed_texts >= 1, "被向量化的文本条数也要记，论文要报这个"
    # 离线模式下不算 live 调用：本地哈希不产生费用
    assert stats.offline_calls >= 1
    assert stats.live_calls == 0


def test_retrieve_without_stats_does_not_crash(db_session):
    """不传 stats 仍可用（/search/preview 之外的老调用点），只是不记账。"""
    kb = _kb_with_chunks(db_session, "basic", count=3, headings=2)
    evidence, routes, _ = retrieval.retrieve(db_session, [kb], "住宿上限")
    assert routes[0].used == "chunk"


def test_answer_usage_includes_query_embedding(app_client, admin_headers):
    """retrieve 的 stats 必须一路传到 answering.answer() 的返回里。

    用真实 HTTP 链路验证，而不是直接调函数——因为「传参」这件事
    在路由层还有一次转手（chat.ask → answering.answer → retrieval.retrieve），
    任何一层漏了，用户拿到的 usage 就是错的。
    """
    kb = app_client.post("/api/kbs", headers=admin_headers,
                         json={"name": _unique("问答成本库"), "description": "",
                               "mode": "basic"}).json()
    app_client.post(f"/api/kbs/{kb['id']}/documents", headers=admin_headers,
                    files=[("files", ("q.md",
                                      "# 报销\n\n## 住宿\n\n一线城市住宿上限 600 元每晚。".encode(),
                                      "text/markdown"))])
    while worker.run_once("test"):
        pass

    conv = app_client.post("/api/conversations", headers=admin_headers,
                           json={"title": "成本会话", "kb_ids": [kb["id"]]}).json()
    r = app_client.post(f"/api/conversations/{conv['id']}/ask", headers=admin_headers,
                        json={"question": "一线城市住宿上限是多少"})
    assert r.status_code == 200, r.text
    usage = r.json()["usage"]

    assert usage["embed_calls"] >= 1, (
        f"问答的 usage 必须包含查询向量化，实际 {usage}")
    assert usage["embed_texts"] >= 1
    assert usage["total_tokens"] == (usage["prompt_tokens"] + usage["completion_tokens"]
                                     + usage["embed_tokens"]), "total_tokens 口径必须自洽"
    assert usage["mode"] == "offline", "离线环境生成通道应标记 offline"
    assert usage["embed_mode"] == "offline", "离线环境向量通道应标记 offline"

    # 刷新后仍能还原（usage 存进 message.route）
    detail = app_client.get(f"/api/conversations/{conv['id']}", headers=admin_headers).json()
    stored = detail["messages"][1]["usage"]
    assert stored["embed_calls"] >= 1, "历史消息必须保留向量调用数"


# ================================================================ 2. Wiki 章节向量化
def test_wiki_section_embedding_is_counted(db_session):
    """build_wiki 的章节向量化必须计入 stats.embed_calls。

    这是 Wiki 成本的大头：每个页面 3~8 个章节，一页一次 embed 调用。
    修复前这里没传 stats，Wiki 的整理成本只剩主题归纳 + 页面生成那几次 chat。
    """
    kb = _kb_with_chunks(db_session, "wiki", count=24, headings=6)

    stats = llm.CallStats()
    result = wiki.build_wiki(db_session, kb, stats=stats)
    db_session.commit()

    assert result.sections > 0, "应生成 Wiki 章节"
    assert stats.embed_calls > 0, (
        f"Wiki 章节向量化未计入 embed_calls，实际 {stats.as_dict()}")
    # 每次 embed 至少向量化 1 条文本，章节数应能对上量级
    assert stats.embed_texts >= result.sections, (
        f"向量化文本数 {stats.embed_texts} 少于章节数 {result.sections}，说明有章节没被向量化")
    assert stats.calls == stats.chat_calls + stats.embed_calls + stats.vision_calls, (
        "总调用数必须等于各通道之和")


def test_wiki_sections_actually_have_vectors(db_session):
    """统计不能只在数字上对：章节必须真的写进了向量，否则是「记了账没干活」。"""
    kb = _kb_with_chunks(db_session, "wiki", count=12, headings=4)
    wiki.build_wiki(db_session, kb)
    db_session.commit()

    sections = db_session.execute(
        select(models.WikiSection).where(models.WikiSection.kb_id == kb.id)
    ).scalars().all()
    assert sections, "应生成 WikiSection"
    assert any(s.vector_blob for s in sections), "章节应带向量（离线哈希也要有）"


def test_wiki_offline_stats_still_count(db_session):
    """离线模式下 stats 仍能正确统计：mode=offline，但调用与文本条数照记。

    为什么这条必须单独测：离线分支不返回 usage 字段，如果代码把「记数」
    写在解析 usage 之后，离线就会全部为 0，本地演示的实验数据直接作废。
    """
    kb = _kb_with_chunks(db_session, "wiki", count=18, headings=5)
    stats = llm.CallStats()

    wiki.build_wiki(db_session, kb, stats=stats)
    db_session.commit()

    assert stats.mode == "offline", f"无模型时应降级，实际 {stats.mode}"
    assert stats.embed_mode == "offline"
    assert stats.embed_calls > 0, "离线向量化也要计数（工作量是真实的）"
    assert stats.embed_texts > 0
    assert stats.live_calls == 0, "离线调用不得计入 live_calls（那是费用口径）"
    assert stats.offline_calls == stats.calls, "所有调用都是离线，二者应相等"
    assert stats.notes, "降级必须留下可读说明"


# ================================================================ 3. 人工校对重建索引
def test_human_edit_rebuild_counts_embedding(app_client, admin_headers, db_session):
    """人工校对后的章节重建必须计入成本，并且落成 cost_records 流水。

    修复前：routers/wiki.py 的 llm.embed 没传 stats，人工校对触发的向量化
    完全没有记录。论文里「人工校对后机器成本是否下降」这个对比就失去依据。
    """
    kb = app_client.post("/api/kbs", headers=admin_headers,
                         json={"name": _unique("人工校对成本库"), "description": "",
                               "mode": "wiki"}).json()
    app_client.post(f"/api/kbs/{kb['id']}/documents", headers=admin_headers,
                    files=[("files", ("w.md", (
                        "# 差旅办法\n\n## 住宿标准\n\n一线城市住宿上限 600 元每晚。\n\n"
                        "## 交通标准\n\n高铁二等座为标准配置。\n\n"
                        "## 审批流程\n\n提交后经主管、财务复核。\n"
                    ).encode(), "text/markdown"))])
    while worker.run_once("test"):
        pass

    tree = app_client.get(f"/api/kbs/{kb['id']}/wiki/tree", headers=admin_headers).json()
    assert tree["items"], "Wiki 页面应已生成"
    slug = tree["items"][0]["slug"]
    page = app_client.get(f"/api/kbs/{kb['id']}/wiki/pages/{slug}",
                          headers=admin_headers).json()

    edited = (page["version"]["content_md"]
              + "\n\n## 人工补充\n\n本条为人工校对内容，重建索引时必须计入成本。\n")
    r = app_client.put(f"/api/kbs/{kb['id']}/wiki/pages/{slug}", headers=admin_headers,
                       json={"content_md": edited, "note": "人工校对"})
    assert r.status_code == 200, r.text
    usage = r.json()["usage"]
    assert usage["embed_calls"] >= 1, f"人工校对重建索引未计入成本：{usage}"
    assert usage["embed_texts"] >= 1
    assert usage["embed_mode"] == "offline"

    # 流水表里必须有 wiki_edit 这一笔，否则 /cost 查历史看不到
    rows = db_session.execute(
        select(models.CostRecord).where(
            models.CostRecord.kb_id == kb["id"],
            models.CostRecord.kind == "wiki_edit")
    ).scalars().all()
    assert rows, "人工校对重建索引必须写成本流水"
    assert rows[0].embed_calls >= 1
    assert rows[0].source == "wiki_edit"


# ================================================================ 4. /cost 接口
def _cost_kb(app_client, admin_headers) -> dict:
    """建一个跑完入库（含 Wiki）的库，保证有成本数据。"""
    kb = app_client.post("/api/kbs", headers=admin_headers,
                         json={"name": _unique("成本接口库"), "description": "",
                               "mode": "wiki"}).json()
    app_client.post(f"/api/kbs/{kb['id']}/documents", headers=admin_headers,
                    files=[("files", ("c.md", (
                        "# 制度\n\n## 住宿\n\n一线城市住宿上限 600 元每晚。\n\n"
                        "## 交通\n\n高铁二等座为标准配置，市内交通实报实销。\n\n"
                        "## 审批\n\n提交后经直属主管、部门负责人、财务复核。\n"
                    ).encode(), "text/markdown"))])
    while worker.run_once("test"):
        pass
    return kb


def test_cost_endpoint_returns_full_structure(app_client, admin_headers, db_session):
    kb = _cost_kb(app_client, admin_headers)

    r = app_client.get(f"/api/kbs/{kb['id']}/cost", headers=admin_headers)
    assert r.status_code == 200, r.text
    data = r.json()

    # ---- 结构契约
    assert data["kb_id"] == kb["id"]
    assert data["mode"] == "wiki"
    for key in ("totals", "by_group", "by_stage", "by_mode", "records"):
        assert key in data, f"缺少字段 {key}"

    totals = data["totals"]
    for key in ("calls", "chat_calls", "embed_calls", "prompt_tokens",
                "completion_tokens", "embed_tokens", "total_tokens",
                "latency_ms", "record_count"):
        assert key in totals, f"totals 缺少 {key}"
        assert isinstance(totals[key], int)

    # ---- 数值自洽
    assert totals["calls"] == totals["chat_calls"] + totals["embed_calls"] + \
        totals.get("vision_calls", 0)
    assert totals["total_tokens"] == (totals["prompt_tokens"]
                                      + totals["completion_tokens"]
                                      + totals["embed_tokens"])
    assert totals["record_count"] > 0, "跑完入库后应有成本记录"

    # ---- 分组：文档入库与 Wiki 生成必须分列，且都非零
    groups = data["by_group"]
    assert "ingest" in groups and "wiki" in groups, f"缺少分组：{list(groups)}"
    assert groups["ingest"]["calls"] > 0, "文档入库阶段应有调用"
    assert groups["wiki"]["calls"] > 0, "Wiki 阶段应有调用（章节向量化也算）"
    assert groups["wiki"]["embed_calls"] > 0, (
        "Wiki 成本必须包含章节向量化，否则就是原来那个缺陷")

    # ---- 按阶段分组带中文标签
    stages = {s["stage"]: s for s in data["by_stage"]}
    assert "ingest_document" in stages
    assert "rebuild_wiki" in stages
    assert stages["rebuild_wiki"]["label"] == "重建 Wiki"

    # ---- 离线模式可识别
    assert data["by_mode"]["offline"]["calls"] > 0
    assert data["by_mode"]["live"]["calls"] == 0, "测试环境无模型，不应有 live 调用"


def test_cost_endpoint_requires_kb_access(app_client, admin_headers, db_session):
    """无权限用户必须 403，有 read 权限则 200（不能过度收紧）。"""
    kb = _cost_kb(app_client, admin_headers)

    pwd = "CostTest#Passw0rd1"
    created = app_client.post("/api/users", headers=admin_headers, json={
        "username": "emp_cost", "display_name": "成本测试员工",
        "password": pwd, "role": "employee", "grants": [],
    })
    if created.status_code == 409:
        users = app_client.get("/api/users", headers=admin_headers).json()["items"]
        uid = next(u["id"] for u in users if u["username"] == "emp_cost")
        app_client.post(f"/api/users/{uid}/reset-password", headers=admin_headers,
                        json={"new_password": pwd})
    login = app_client.post("/api/auth/login", json={"username": "emp_cost", "password": pwd})
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['token']}"}

    denied = app_client.get(f"/api/kbs/{kb['id']}/cost", headers=headers)
    assert denied.status_code == 403, (
        f"无授权用户读成本必须 403，实际 {denied.status_code}")

    # 未登录也必须 401
    assert app_client.get(f"/api/kbs/{kb['id']}/cost").status_code == 401

    # 不存在的库 → 404（而不是泄露「存在但无权」）
    assert app_client.get("/api/kbs/999999/cost", headers=admin_headers).status_code == 404

    # 授予 read 后应可查看
    emp = app_client.get("/api/users", headers=admin_headers).json()["items"]
    uid = next(u["id"] for u in emp if u["username"] == "emp_cost")
    app_client.put(f"/api/users/{uid}/grants", headers=admin_headers,
                   json={"grants": [{"kb_id": kb["id"], "level": "read"}]})
    allowed = app_client.get(f"/api/kbs/{kb['id']}/cost", headers=headers)
    assert allowed.status_code == 200, "read 权限即可查看成本，不应要求 manage"
    assert allowed.json()["my_level"] == "read"


def test_task_result_keeps_usage_and_phases(app_client, admin_headers):
    """Task.result 必须保留 usage（旧字段不动）并新增 usage_by_phase。"""
    kb = _cost_kb(app_client, admin_headers)
    tasks = app_client.get(f"/api/tasks?kb_id={kb['id']}&status=success",
                           headers=admin_headers).json()
    assert tasks["total"] >= 1
    ingest_task = next(t for t in tasks["items"]
                       if t["kind"] == "ingest_document")
    usage = ingest_task["result"]["usage"]
    assert usage["calls"] > 0, "任务 result 必须带 usage"
    assert "embed_calls" in usage, "usage 必须区分向量调用"
    phases = ingest_task["result"]["usage_by_phase"]
    assert "ingest_document" in phases, "必须记录分块阶段成本"
    assert "rebuild_wiki" in phases, "Wiki 库必须记录 Wiki 阶段成本"
    # 顶层也带一份，前端不必解包 result
    assert ingest_task["usage"]["calls"] == usage["calls"]
    assert ingest_task["usage_by_phase"].keys() == phases.keys()


def test_cost_endpoint_tolerates_empty_kb(app_client, admin_headers):
    """空库不报错，返回全零而不是 500。"""
    kb = app_client.post("/api/kbs", headers=admin_headers,
                         json={"name": _unique("空成本库"), "description": "",
                               "mode": "basic"}).json()
    r = app_client.get(f"/api/kbs/{kb['id']}/cost", headers=admin_headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["totals"]["calls"] == 0
    assert data["totals"]["total_tokens"] == 0
    assert data["records"] == []


# ================================================================ 5. CallStats 单元行为
def test_callstats_separates_chat_and_embed():
    """生成与向量必须分列，且 total_tokens 自洽。"""
    s = llm.CallStats()
    s.record_chat(prompt_tokens=100, completion_tokens=20, latency_ms=300)
    s.record_embed(texts=8, tokens=64, latency_ms=40, live=True)
    s.record_embed(texts=3, live=False)

    assert s.calls == 3
    assert s.chat_calls == 1 and s.embed_calls == 2
    assert s.prompt_tokens == 100 and s.completion_tokens == 20
    assert s.embed_tokens == 64, "embed token 不得混进 prompt_tokens"
    assert s.embed_texts == 11
    assert s.total_tokens == 184
    assert s.live_calls == 2 and s.offline_calls == 1
    assert s.latency_ms == 340
    assert s.latency_chat_ms == 300 and s.latency_embed_ms == 40
    assert s.embed_mode == "offline"
    assert s.mode == "live", "向量降级不应污染生成通道的 mode"
    assert s.degraded is True

    d = s.as_dict()
    for key in ("calls", "chat_calls", "embed_calls", "embed_tokens", "embed_texts",
                "live_calls", "offline_calls", "total_tokens", "embed_mode", "degraded"):
        assert key in d, f"as_dict 缺少 {key}"


def test_callstats_merge_keeps_mode_sticky():
    """合并时降级状态要「粘」：任一子统计离线，整体标记降级。"""
    a = llm.CallStats()
    a.record_chat(prompt_tokens=10, completion_tokens=5, latency_ms=100)
    b = llm.CallStats()
    b.record_embed(texts=4, live=False)

    a.merge(b)
    assert a.calls == 2
    assert a.chat_calls == 1 and a.embed_calls == 1
    assert a.prompt_tokens == 10
    assert a.embed_mode == "offline"
    assert a.degraded is True
    assert a.total_tokens == 15


def test_callstats_notes_are_deduplicated():
    """批量索引时同一条降级提示会重复上百次，必须去重。"""
    s = llm.CallStats()
    for _ in range(5):
        s.add_note("未配置 Embedding")
    assert s.notes == ["未配置 Embedding"]


# ================================================================ 6. 老库升级
def test_cost_table_is_created_on_old_database(tmp_path):
    """升级前的库没有 cost_records 表，启动后必须自动建出来。

    桌面版用户的 AppData 里是旧 schema。`create_all` 会建缺失的**表**
    （这点和加列不同），但这里仍要验证一遍——因为如果 cost_records 忘了
    写进 metadata（例如误放到别处的 Base），线上就是启动即 500。
    """
    from sqlalchemy import create_engine, inspect, text
    from app.db import ensure_sqlite_columns

    db_path = tmp_path / "old.sqlite3"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    models.Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE cost_records"))
    assert "cost_records" not in inspect(engine).get_table_names()
    engine.dispose()

    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    models.Base.metadata.create_all(bind=engine)
    ensure_sqlite_columns(engine, models.Base.metadata)

    insp = inspect(engine)
    assert "cost_records" in insp.get_table_names(), "老库升级后必须自动建出 cost_records"
    cols = {c["name"] for c in insp.get_columns("cost_records")}
    for need in ("kb_id", "kind", "calls", "chat_calls", "embed_calls",
                 "embed_tokens", "embed_texts", "latency_ms", "source", "source_id"):
        assert need in cols, f"cost_records 缺少列 {need}"
    engine.dispose()


def test_cost_summary_falls_back_to_task_result(db_session):
    """没有成本流水的历史任务，必须从 Task.result 回读，不能显示为 0。

    否则用户升级后打开「成本」页会看到自己之前的所有整理都是免费的。
    """
    from app.services import cost

    kb = models.KnowledgeBase(name=_unique("历史成本库"), mode="basic", status="ready")
    db_session.add(kb)
    db_session.commit()

    # 老任务：只有 result["usage"]，没有 CostRecord
    legacy_usage = {
        "calls": 7, "prompt_tokens": 900, "completion_tokens": 120,
        "latency_ms": 3000, "mode": "live", "notes": [],
    }
    db_session.add(models.Task(
        kind=models.TaskKind.ingest_document.value, status="success", kb_id=kb.id,
        dedupe_key=_unique("legacy"), result=json.dumps({"chunks": 3, "usage": legacy_usage}),
    ))
    db_session.commit()

    summary = cost.kb_summary(db_session, kb)
    assert summary["totals"]["calls"] == 7, "历史任务成本必须被回读"
    assert summary["totals"]["prompt_tokens"] == 900
    assert summary["legacy_records"] == 1
    assert summary["by_stage"][0]["stage"] == "ingest_document"


def test_cost_summary_does_not_double_count(db_session):
    """已有流水的任务不得再从 result 回读一次，否则数字翻倍。"""
    from app.services import cost

    kb = models.KnowledgeBase(name=_unique("去重成本库"), mode="basic", status="ready")
    db_session.add(kb)
    db_session.commit()

    task = models.Task(
        kind=models.TaskKind.ingest_document.value, status="success", kb_id=kb.id,
        dedupe_key=_unique("dup"),
        result=json.dumps({"usage": {"calls": 5, "prompt_tokens": 100}}),
    )
    db_session.add(task)
    db_session.flush()

    s = llm.CallStats()
    s.record_chat(prompt_tokens=100, completion_tokens=10, latency_ms=50)
    cost.record(db_session, kb_id=kb.id, kind="ingest_document", stats=s,
                source="task", source_id=task.id)
    db_session.commit()

    summary = cost.kb_summary(db_session, kb)
    assert summary["totals"]["calls"] == 1, (
        f"同一任务被算了两次：{summary['totals']}")
    assert summary["legacy_records"] == 0, "已有流水的任务不应被当作历史数据回读"


def test_failed_task_keeps_consumed_cost(app_client, admin_headers, db_session,
                                        scratch_dir, monkeypatch):
    """失败任务已发生的调用必须入账（「失败重试」的成本不能永远是 0）。

    造一个「先花钱、后失败」的任务：分块向量化跑完后抛异常。
    修复前的行为：except 分支只写 task.error，stats 直接丢弃，
    于是这次入库在成本表里完全不存在——重试三次的成本等于 0。
    """
    from app.services import cost, ingest, worker

    kb = app_client.post("/api/kbs", headers=admin_headers,
                         json={"name": _unique("失败成本库"), "description": "",
                               "mode": "basic"}).json()
    # 真实文件，保证能过解析和分块，走到向量化之后才失败
    doc = models.Document(kb_id=kb["id"], filename="ok.md", suffix=".md", status="indexing")
    db_session.add(doc)
    db_session.commit()
    store = scratch_dir / "ok.md"
    store.write_text("# 制度\n\n一线城市住宿上限 600 元每晚。\n\n## 交通\n\n高铁二等座。\n",
                     encoding="utf-8")
    ver = models.DocumentVersion(doc_id=doc.id, version_no=1, storage_path=str(store),
                                 status="queued")
    db_session.add(ver)
    db_session.commit()
    db_session.add(models.Task(
        kind=models.TaskKind.ingest_document.value, status="queued", kb_id=kb["id"],
        doc_id=doc.id, doc_version_id=ver.id, dedupe_key=_unique("failcost")))
    db_session.commit()

    # 向量化成功之后炸掉：确保失败时 stats 已经有值
    original = ingest._refresh_kb_status

    def boom(*a, **k):
        raise RuntimeError("模拟入库后半段失败")

    monkeypatch.setattr(ingest, "_refresh_kb_status", boom)
    try:
        while worker.run_once("test"):
            pass
    finally:
        monkeypatch.setattr(ingest, "_refresh_kb_status", original)

    tasks = db_session.execute(
        select(models.Task).where(models.Task.kb_id == kb["id"])
    ).scalars().all()
    failed = [t for t in tasks if t.status == "failed"]
    assert failed, "任务应失败"
    usage = json.loads(failed[0].result or "{}").get("usage") or {}
    assert usage.get("embed_calls", 0) > 0, (
        f"失败任务的 result 必须保留已消耗的调用：{failed[0].result}")

    r = app_client.get(f"/api/kbs/{kb['id']}/cost", headers=admin_headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["totals"]["calls"] > 0, "失败任务消耗的调用必须出现在成本统计里"
    assert any(s["stage"] == "failed_attempt" for s in data["by_stage"]), (
        f"失败尝试应单独成阶段：{[s['stage'] for s in data['by_stage']]}")


def test_task_failing_before_any_call_creates_no_record(app_client, admin_headers, db_session):
    """解析前就失败（文件缺失）的任务不该凭空造成本记录。"""
    from app.services import worker

    kb = app_client.post("/api/kbs", headers=admin_headers,
                         json={"name": _unique("空失败库"), "description": "",
                               "mode": "basic"}).json()
    doc = models.Document(kb_id=kb["id"], filename="ghost.md", suffix=".md", status="indexing")
    db_session.add(doc)
    db_session.commit()
    ver = models.DocumentVersion(doc_id=doc.id, version_no=1,
                                 storage_path="/nonexistent/ghost.md", status="queued")
    db_session.add(ver)
    db_session.commit()
    db_session.add(models.Task(
        kind=models.TaskKind.ingest_document.value, status="queued", kb_id=kb["id"],
        doc_id=doc.id, doc_version_id=ver.id, dedupe_key=_unique("failcost0")))
    db_session.commit()

    while worker.run_once("test"):
        pass

    r = app_client.get(f"/api/kbs/{kb['id']}/cost", headers=admin_headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["totals"]["record_count"] == 0, (
        f"零调用的失败任务不应产生成本记录：{data['records']}")

