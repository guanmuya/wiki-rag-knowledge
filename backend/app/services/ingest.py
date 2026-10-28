"""文档入库：解析 → 分块 → 关键词/向量索引，并把状态与进度写回任务表。

幂等性：同一文档版本重复执行时，先清掉该版本的旧分块与索引，再重建；
Wiki 页面按 slug 复用，不会因重跑产生重复页面。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .. import config, models
from . import chunking, llm, parser, wiki

log = logging.getLogger("wrk.ingest")


def _set_progress(db: Session, task: models.Task, stage: str, progress: int) -> None:
    task.stage = stage
    task.progress = progress
    db.add(task)
    db.commit()


def ingest_version(db: Session, task: models.Task, *, stats: llm.CallStats) -> dict:
    """文档入库：解析 → 分块 → 向量索引（Wiki 库再加一段 Wiki 生成）。

    成本口径为什么拆成两个子统计：
    一次入库里「分块向量化」和「Wiki 生成」是两种完全不同的整理代价——
    前者随文档字数线性增长，后者随主题数增长。合并成一个 usage 之后，
    论文里没法回答「Wiki 到底额外花了多少」这个问题。所以：
    - chunk_stats：解析后的分块向量化（embedding）
    - wiki_stats：主题归纳 + 页面生成 + 章节向量化（chat + embedding）
    两者都 merge 进调用方传入的 stats（任务总量，不丢信息），
    同时各自序列化进 result["usage_by_phase"]，供成本流水按阶段入账。
    """
    version = db.get(models.DocumentVersion, task.doc_version_id)
    if not version:
        raise RuntimeError("文档版本不存在")
    doc = db.get(models.Document, version.doc_id)
    kb = db.get(models.KnowledgeBase, doc.kb_id)
    if not doc or not kb:
        raise RuntimeError("文档或知识库不存在")

    path = Path(version.storage_path)
    if not path.exists():
        raise RuntimeError(f"原始文件缺失：{path.name}")

    _set_progress(db, task, "解析文档", 10)
    parsed = parser.parse_document(path)

    # 图片暂不解析内容：视觉模型/OCR 属后续阶段。
    # 这里只把图片数量记入元信息，不占用上下文，也不假装理解了图片。
    if parsed.images:
        log.info("文档 %s 含 %d 张图片，当前阶段不做图片内容解析",
                 doc.id, len(parsed.images))

    _set_progress(db, task, "分块", 35)
    units = chunking.split_blocks(parsed.blocks)
    if not units:
        raise RuntimeError("解析后没有可索引的文本")

    _set_progress(db, task, "向量化", 55)
    # 先清旧数据（重跑幂等）
    db.execute(delete(models.Chunk).where(models.Chunk.doc_version_id == version.id))
    db.flush()

    # 分块向量化的成本单独记一份，便于与 Wiki 成本分列
    chunk_stats = llm.CallStats()
    cfg, provider, sig, dim = llm.active_embedding(db)
    if kb.embed_signature and kb.embed_signature != sig:
        # 不允许在未重建时混用新模型向量：本次仍按库内签名写入
        sig = kb.embed_signature
        dim = kb.embed_dim
    texts = [u.text for u in units]
    vectors: list[list[float] | None] = [None] * len(texts)
    batch = 16
    for i in range(0, len(texts), batch):
        chunk_batch = texts[i:i + batch]
        try:
            vecs = llm.embed(db, chunk_batch, stats=chunk_stats)
        except llm.ModelUnavailable as exc:
            chunk_stats.mode = "offline"
            chunk_stats.add_note(f"向量化失败，仅建关键词索引：{exc}")
            vecs = []
        for j, vec in enumerate(vecs):
            vectors[i + j] = vec

    # 立刻并入调用方总账，而不是等函数末尾再 merge。
    # 为什么：向量化之后的建索引/写库步骤仍可能抛异常（磁盘满、约束冲突…），
    # 若 merge 放在最后，这些已经花掉的调用会随异常一起丢失，
    # 失败重试的成本就永远统计不到。成本必须在「花掉的那一刻」记账。
    stats.merge(chunk_stats)

    _set_progress(db, task, "建立索引", 80)
    count = 0
    for unit, vec in zip(units, vectors):
        db.add(models.Chunk(
            kb_id=kb.id, doc_id=doc.id, doc_version_id=version.id, ordinal=unit.ordinal,
            text=unit.text, heading=unit.heading[:500], locator=unit.locator,
            keywords=chunking.keyword_field(unit.text),
            vector_blob=llm.pack_vector(vec) if vec else None,
            dim=len(vec) if vec else 0, embed_signature=sig if vec else "",
            chunk_hash=chunking.chunk_hash(unit.text, unit.heading),
            kind=unit.kind,
            meta=json.dumps(unit.meta, ensure_ascii=False) if unit.meta else "{}",
        ))
        count += 1
    db.flush()

    if not kb.embed_signature:
        kb.embed_signature = sig
        kb.embed_dim = dim or (len(next((v for v in vectors if v), [])) or 0)
        db.add(kb)

    version.chunk_count = count
    version.text_chars = sum(len(u.text) for u in units)
    version.status = models.TaskStatus.success.value
    version.status_detail = ""
    doc.status = models.KbStatus.ready.value
    doc.current_version_id = version.id
    db.add_all([version, doc])
    db.flush()

    # 新版本成功入库后才下线旧版本的分块：处理失败时旧索引仍然可用。
    stale = db.execute(
        delete(models.Chunk).where(
            models.Chunk.doc_id == doc.id,
            models.Chunk.doc_version_id != version.id,
        )
    )
    if stale.rowcount:
        log.info("文档 %s 下线旧版本分块 %s 个", doc.id, stale.rowcount)

    db.add(models.IndexVersion(
        kb_id=kb.id, kind="chunk", embed_signature=kb.embed_signature,
        status=models.KbStatus.ready.value, unit_count=count,
    ))
    _refresh_kb_status(db, kb)

    result = {
        "chunks": count,
        "chars": version.text_chars,
        "images": len(parsed.images),
        "embedding_mode": "live" if any(vectors) else "keyword-only",
        "embedding_signature": kb.embed_signature,
    }
    # 分块阶段成本已在向量化结束时并入 stats（见上），这里只留阶段明细。
    # 重复 merge 会把同一批调用算两遍，所以此处不能再 merge。
    phases = {"ingest_document": chunk_stats.as_dict()}

    # Wiki 增强库：文档入库后增量重建 Wiki
    if kb.mode == models.KbMode.wiki.value:
        _set_progress(db, task, "生成 Wiki 页面", 88)
        # Wiki 成本单独一份：这是「Wiki 增强」相对「基础分块」多出来的代价，
        # 也是论文里最想量化的那一列。它不进 chunk 阶段，必须能独立报出。
        wiki_stats = llm.CallStats()
        try:
            wres = wiki.build_wiki(db, kb, stats=wiki_stats)
            result["wiki"] = {
                "created": wres.pages_created, "updated": wres.pages_updated,
                "skipped": wres.pages_skipped, "sections": wres.sections,
                "evidence_links": wres.evidence_links, "notes": wres.notes or [],
            }
            phases["rebuild_wiki"] = wiki_stats.as_dict()
            stats.merge(wiki_stats)
        except Exception as exc:  # Wiki 失败不推翻分块索引
            db.rollback()
            result["wiki_error"] = f"{exc.__class__.__name__}: {exc}"
            # 失败前已经发生的调用仍是真实开销，不能因为异常就丢掉
            phases["rebuild_wiki"] = wiki_stats.as_dict()
            stats.merge(wiki_stats)
            kb.status = models.KbStatus.partial.value
            kb.status_detail = f"分块索引已就绪，Wiki 生成失败：{result['wiki_error']}"
            db.add(kb)
            db.commit()
    result["usage_by_phase"] = phases
    return result


def _refresh_kb_status(db: Session, kb: models.KnowledgeBase, *,
                       commit: bool = True) -> None:
    """重算知识库状态与说明。

    `commit=False` 给 Worker 用：任务成功时调用方还要写 task.result / 成本流水，
    这里提前 commit 会把未完成的事务切开，失败时留下「状态已就绪但任务失败」的假象。
    """
    total = db.execute(
        select(func.count(models.Document.id)).where(
            models.Document.kb_id == kb.id, models.Document.deleted.is_(False))
    ).scalar() or 0
    if total == 0:
        kb.status = models.KbStatus.empty.value
        kb.status_detail = "还没有文档"
    else:
        chunk_total = db.execute(
            select(func.count(models.Chunk.id)).where(models.Chunk.kb_id == kb.id)
        ).scalar() or 0
        kb.status = models.KbStatus.ready.value if chunk_total else models.KbStatus.partial.value
        kb.status_detail = f"{total} 个文档 / {chunk_total} 个索引单元"
    db.add(kb)
    if commit:
        db.commit()


def reindex_kb(db: Session, task: models.Task, *, stats: llm.CallStats) -> dict:
    """全量重建：按当前 Embedding 配置重算所有分块向量（换模型后使用）。

    成本口径与 ingest_version 一致：分块重建与 Wiki 重建各自独立统计，
    再 merge 进任务总量。换一次 embedding 模型的真实代价通常远高于日常入库，
    论文里要能单独报出这个数字，所以不能只留一个合并后的 calls。
    """
    kb = db.get(models.KnowledgeBase, task.kb_id)
    if not kb:
        raise RuntimeError("知识库不存在")
    chunk_stats = llm.CallStats()
    cfg, provider, sig, dim = llm.active_embedding(db)
    chunks = list(db.execute(
        select(models.Chunk).where(models.Chunk.kb_id == kb.id).order_by(models.Chunk.id)
    ).scalars())
    if not chunks:
        raise RuntimeError("知识库没有分块，无法重建")
    total = len(chunks)
    for i in range(0, total, 16):
        batch = chunks[i:i + 16]
        try:
            vecs = llm.embed(db, [c.text for c in batch], stats=chunk_stats)
        except llm.ModelUnavailable as exc:
            # 失败也要把已发生的调用记账：换了模型跑了一半炸掉，钱已经花了
            stats.merge(chunk_stats)
            raise RuntimeError(f"重建失败：{exc}") from exc
        for c, vec in zip(batch, vecs):
            c.vector_blob = llm.pack_vector(vec)
            c.dim = len(vec)
            c.embed_signature = sig
            db.add(c)
        db.commit()
        task.stage = f"重建向量 {min(i + 16, total)}/{total}"
        task.progress = int(10 + 70 * min(i + 16, total) / total)
        db.add(task)
        db.commit()

    kb.embed_signature = sig
    kb.embed_dim = dim or (len(vecs[0]) if chunks else 0)
    db.add(kb)
    db.add(models.IndexVersion(
        kb_id=kb.id, kind="chunk", embed_signature=sig,
        status=models.KbStatus.ready.value, unit_count=total,
    ))

    stats.merge(chunk_stats)
    phases = {models.TaskKind.reindex_kb.value: chunk_stats.as_dict()}
    result = {"chunks": total, "embedding_signature": sig}
    if kb.mode == models.KbMode.wiki.value:
        db.commit()
        wiki_stats = llm.CallStats()
        try:
            wres = wiki.build_wiki(db, kb, stats=wiki_stats)
            result["wiki"] = {"created": wres.pages_created, "updated": wres.pages_updated,
                              "sections": wres.sections}
        except Exception as exc:
            result["wiki_error"] = str(exc)
        # Wiki 阶段的调用无论成败都要入账
        phases[models.TaskKind.rebuild_wiki.value] = wiki_stats.as_dict()
        stats.merge(wiki_stats)
    result["usage_by_phase"] = phases
    return result
