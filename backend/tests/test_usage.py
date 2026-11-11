"""Integration tests for the read-only global usage dashboard."""
from __future__ import annotations

import datetime as dt
import json
import uuid
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.orm import sessionmaker

from app import models, security
from app.db import get_db
from app.main import app
from app.services import answering, cost, llm, taskflow, usage, worker
from app.services.agent import plugins as agent_plugins


TODAY = dt.date(2025, 3, 1)
FIELDS = (
    "calls", "live_calls", "offline_calls", "chat_calls", "embed_calls", "vision_calls",
    "prompt_tokens", "completion_tokens", "embed_tokens", "total_tokens", "latency_ms",
    "record_count", "unmetered_records",
)
# 一个汇总桶除计数外还带 cost；比较子集时要用这两组键分别取。
BUCKET_FIELDS = (*FIELDS, "cost")


def cost_bucket(amount=0.0, priced=0, unpriced=0, unpriced_tokens=0):
    """金额桶：priced/unpriced 是「有 token 且找到/没找到单价」的行数。"""
    return {"currency": "USD", "amount": amount, "priced_records": priced,
            "unpriced_records": unpriced, "unpriced_tokens": unpriced_tokens}


def totals(**values):
    bucket = {field: values.get(field, 0) for field in FIELDS}
    bucket["cost"] = values.get("cost") or cost_bucket()
    return bucket


def stamp(day, hour=0, minute=0, second=0, microsecond=0):
    return dt.datetime.combine(day, dt.time(hour, minute, second, microsecond))


def encoded(counters, **other):
    return json.dumps({"usage": counters, **other})


@pytest.fixture
def isolated_api(tmp_path, monkeypatch):
    # A separate database for each test: no deletion or assumptions about data
    # left by other tests, and actual auth still uses the app's dependencies.
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'usage.sqlite3').as_posix()}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    models.Base.metadata.create_all(engine)
    make_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = make_session()
    admin = models.User(username="usage-admin", password_hash="unused", role="admin",
                        status="active", must_change_password=False)
    employee = models.User(username="usage-employee", password_hash="unused", role="employee",
                           status="active", must_change_password=False)
    db.add_all([admin, employee])
    db.commit()
    headers = {
        "admin": {"Authorization": f"Bearer {security.issue_token(admin.id, admin.role)}"},
        "employee": {"Authorization": f"Bearer {security.issue_token(employee.id, employee.role)}"},
    }

    def get_test_db():
        with make_session() as request_db:
            yield request_db

    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = get_test_db
    monkeypatch.setattr(usage, "_today_utc", lambda: TODAY)
    client = TestClient(app)  # no application lifespan or background workers
    try:
        yield client, db, headers, admin
    finally:
        client.close()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        db.close()
        engine.dispose()


def test_empty_response_period_validation_and_auth(isolated_api):
    client, _, headers, _ = isolated_api
    assert client.get("/api/usage").status_code == 401
    assert client.get("/api/usage", headers={"Authorization": "Bearer invalid"}).status_code == 401
    assert client.get("/api/usage", headers=headers["employee"]).status_code == 403
    for invalid in ("1", "-1", "31", "invalid"):
        assert client.get(f"/api/usage?days={invalid}", headers=headers["admin"]).status_code == 422

    response = client.get("/api/usage", headers=headers["admin"])
    assert response.status_code == 200
    data = response.json()
    assert set(data) == {"period_days", "timezone", "currency", "price_unit",
                         "totals", "daily", "by_source", "by_core"}
    assert data["period_days"] == 30 and data["timezone"] == "UTC"
    assert data["currency"] == "USD" and data["price_unit"] == "per_1m_tokens"
    assert data["totals"] == totals()
    assert len(data["daily"]) == 30
    assert data["daily"][0] == {"date": "2025-01-31", **totals()}
    assert data["daily"][-1] == {"date": "2025-03-01", **totals()}
    assert {row["source"] for row in data["by_source"]} == {"processing", "chat", "agent"}
    assert data["by_core"] == []
    assert all(row["label"] and {k: row[k] for k in BUCKET_FIELDS} == totals()
               for row in data["by_source"])

    data = client.get("/api/usage?days=0", headers=headers["admin"]).json()
    assert data["daily"] == [] and data["totals"] == totals()


def test_real_sources_aggregate_without_duplicate_tasks_or_kbs(isolated_api):
    client, db, headers, admin = isolated_api
    kb_a = models.KnowledgeBase(name="first", mode="basic")
    kb_b = models.KnowledgeBase(name="second", mode="wiki")
    db.add_all([kb_a, kb_b])
    db.flush()
    current_task = models.Task(
        kind="ingest_document", kb_id=kb_a.id, status="success", created_at=stamp(TODAY),
        result=encoded({"calls": 900, "prompt_tokens": 900}),
    )
    covered_outside = models.Task(
        kind="ingest_document", kb_id=kb_a.id, status="success", created_at=stamp(TODAY),
        result=encoded({"calls": 800}),
    )
    legacy_finished = models.Task(
        kind="rebuild_wiki", kb_id=kb_b.id, status="success",
        created_at=stamp(dt.date(2025, 2, 1)), finished_at=stamp(dt.date(2025, 2, 23)),
        result=encoded({"calls": 5, "live_calls": 2, "offline_calls": 3, "chat_calls": 2,
                        "embed_calls": 2, "vision_calls": 1, "prompt_tokens": 40,
                        "completion_tokens": 5, "embed_tokens": 15, "latency_ms": 25}),
    )
    legacy_created = models.Task(
        kind="reindex_kb", kb_id=kb_b.id, status="failed", created_at=stamp(TODAY),
        result=encoded({"calls": 1, "offline_calls": 1, "embed_calls": 1,
                        "embed_tokens": 7, "latency_ms": 2}),
    )
    unrelated = models.Task(kind="reindex_kb", kb_id=None, status="success",
                            created_at=stamp(TODAY), result=encoded({"calls": 700}))
    db.add_all([current_task, covered_outside, legacy_finished, legacy_created, unrelated])
    db.flush()
    db.add_all([
        models.CostRecord(
            kb_id=kb_a.id, kind="ingest_document", source="task", source_id=current_task.id,
            created_at=stamp(TODAY), calls=4, live_calls=3, offline_calls=1,
            chat_calls=2, embed_calls=1, vision_calls=1, prompt_tokens=100,
            completion_tokens=20, embed_tokens=30, latency_ms=70,
        ),
        models.CostRecord(
            kb_id=kb_a.id, kind="failed_attempt", source="task", source_id=current_task.id,
            created_at=stamp(dt.date(2025, 2, 28), 23, 59), calls=2, live_calls=1,
            offline_calls=1, chat_calls=1, embed_calls=1, prompt_tokens=10,
            completion_tokens=3, embed_tokens=8, latency_ms=20,
        ),
        models.CostRecord(
            kb_id=kb_b.id, kind="wiki_edit", source="wiki_edit", source_id=legacy_finished.id,
            created_at=stamp(TODAY), calls=1, offline_calls=1, embed_calls=1,
            embed_tokens=11, latency_ms=4,
        ),
        # Its presence suppresses covered_outside even in the 7-day window.
        models.CostRecord(
            kb_id=kb_a.id, kind="failed_attempt", source="task", source_id=covered_outside.id,
            created_at=stamp(dt.date(2025, 2, 22)), calls=11, live_calls=11,
        ),
    ])
    conv = models.Conversation(user_id=admin.id, kb_ids=json.dumps([kb_a.id, kb_b.id]))
    db.add(conv)
    db.flush()
    chat_usage = {"calls": 3, "live_calls": 1, "offline_calls": 2, "chat_calls": 1,
                  "embed_calls": 1, "vision_calls": 1, "prompt_tokens": 50,
                  "completion_tokens": 6, "embed_tokens": 9, "latency_ms": 30}
    db.add_all([
        models.Message(conversation_id=conv.id, role="assistant", status="ok",
                       content="private answer", kb_ids=conv.kb_ids, created_at=stamp(TODAY),
                       route=encoded(chat_usage, provider="private provider", prompt="private prompt")),
        models.Message(conversation_id=conv.id, role="assistant", status="failed",
                       created_at=stamp(TODAY), route=encoded({"calls": 200})),
        models.Message(conversation_id=conv.id, role="assistant", status="pending",
                       created_at=stamp(TODAY), route=encoded({"calls": 300})),
        models.Message(conversation_id=conv.id, role="user", status="ok",
                       created_at=stamp(TODAY), route=encoded({"calls": 400})),
    ])
    db.commit()

    processing = totals(calls=13, live_calls=6, offline_calls=7, chat_calls=5,
                        embed_calls=6, vision_calls=2, prompt_tokens=150,
                        completion_tokens=28, embed_tokens=71, total_tokens=249,
                        latency_ms=121, record_count=5,
                        # 这些行都没有 model_config_id，按定义未计价。
                        cost=cost_bucket(unpriced=5, unpriced_tokens=249))
    chat = totals(calls=3, live_calls=1, offline_calls=2, chat_calls=1, embed_calls=1,
                  vision_calls=1, prompt_tokens=50, completion_tokens=6,
                  embed_tokens=9, total_tokens=65, latency_ms=30, record_count=1,
                  cost=cost_bucket(unpriced=1, unpriced_tokens=65))
    expected = totals(calls=16, live_calls=7, offline_calls=9, chat_calls=6,
                      embed_calls=7, vision_calls=3, prompt_tokens=200,
                      completion_tokens=34, embed_tokens=80, total_tokens=314,
                      latency_ms=151, record_count=6,
                      cost=cost_bucket(unpriced=6, unpriced_tokens=314))
    before = [db.scalar(select(func.count()).select_from(model))
              for model in (models.CostRecord, models.Task, models.Message)]
    statements = []
    engine = db.get_bind()

    def capture_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture_sql)
    try:
        response = client.get("/api/usage?days=7", headers=headers["admin"])
    finally:
        event.remove(engine, "before_cursor_execute", capture_sql)
    assert response.status_code == 200, response.text
    assert statements and all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    data = response.json()
    assert data["period_days"] == 7 and data["timezone"] == "UTC"
    assert data["totals"] == expected
    by_source = {row["source"]: row for row in data["by_source"]}
    assert {k: by_source["processing"][k] for k in BUCKET_FIELDS} == processing
    assert {k: by_source["chat"][k] for k in BUCKET_FIELDS} == chat
    assert [row["date"] for row in data["daily"]] == [
        (dt.date(2025, 2, 23) + dt.timedelta(days=i)).isoformat() for i in range(7)
    ]
    assert data["daily"][0]["calls"] == 5
    assert data["daily"][-2]["calls"] == 2
    assert data["daily"][-1]["calls"] == 9
    assert all(data["daily"][i]["record_count"] == 0 for i in range(1, 5))
    assert all(sum(row[field] for row in data["daily"]) == expected[field] for field in FIELDS)
    assert all(secret not in response.text for secret in ("private answer", "private provider", "private prompt"))
    assert client.get("/api/usage?days=7", headers=headers["admin"]).json() == data
    after = [db.scalar(select(func.count()).select_from(model))
             for model in (models.CostRecord, models.Task, models.Message)]
    assert before == after

    all_time = client.get("/api/usage?days=0", headers=headers["admin"]).json()
    assert all_time["totals"]["calls"] == 27
    assert all_time["totals"]["record_count"] == 7
    assert all_time["daily"][0]["date"] == "2025-02-22"


def test_existing_writers_are_reflected_in_the_endpoint(isolated_api, monkeypatch):
    client, db, headers, _ = isolated_api
    monkeypatch.setattr(usage, "_today_utc", lambda: models.utcnow().date())
    kb_a = models.KnowledgeBase(name="writer-a", mode="basic")
    kb_b = models.KnowledgeBase(name="writer-b", mode="wiki")
    db.add_all([kb_a, kb_b])
    db.flush()

    ingest = llm.CallStats()
    ingest.record_chat(prompt_tokens=7, completion_tokens=3, latency_ms=10)
    ingest.record_embed(texts=1, tokens=2, latency_ms=5, live=False)
    wiki = llm.CallStats()
    wiki.record_vision(prompt_tokens=11, completion_tokens=4, latency_ms=12)
    combined = llm.CallStats()
    combined.merge(ingest)
    combined.merge(wiki)
    task = models.Task(kind="ingest_document", kb_id=kb_a.id, status="success",
                       result=encoded(combined.as_dict()))
    db.add(task)
    db.flush()
    cost.record_phases(db, kb_id=kb_a.id, task_kind=task.kind, task_id=task.id,
                       doc_id=None, phases={"ingest_document": ingest.as_dict(),
                                            "rebuild_wiki": wiki.as_dict()}, total=combined)
    db.commit()

    chat_stats = llm.CallStats()
    chat_stats.record_embed(texts=1, tokens=13, latency_ms=6, live=True)
    chat_stats.record_chat(prompt_tokens=17, completion_tokens=5, latency_ms=9)

    def answer(_db, question, kbs, *, top_k):
        assert question == "test question" and {kb.id for kb in kbs} == {kb_a.id, kb_b.id}
        return {"content": "private content", "evidence": [], "route": {},
                "usage": chat_stats.as_dict(), "insufficient": False}

    monkeypatch.setattr(answering, "answer", answer)
    conversation = client.post("/api/conversations", headers=headers["admin"],
                               json={"kb_ids": [kb_a.id, kb_b.id]})
    assert conversation.status_code == 201, conversation.text
    response = client.post(f"/api/conversations/{conversation.json()['id']}/ask",
                           headers=headers["admin"], json={"question": "test question"})
    assert response.status_code == 200, response.text
    data = client.get("/api/usage?days=7", headers=headers["admin"]).json()
    assert data["totals"] == totals(
        calls=5, live_calls=4, offline_calls=1, chat_calls=2, embed_calls=2,
        vision_calls=1, prompt_tokens=35, completion_tokens=12, embed_tokens=15,
        total_tokens=62, latency_ms=42, record_count=3,
        cost=cost_bucket(unpriced=3, unpriced_tokens=62),
    )
    sources = {row["source"]: row for row in data["by_source"]}
    assert sources["processing"]["record_count"] == 2
    assert sources["chat"]["record_count"] == 1


def test_utc_day_boundaries_and_all_time_future(isolated_api):
    client, db, headers, admin = isolated_api
    conv = models.Conversation(user_id=admin.id)
    db.add(conv)
    db.flush()
    dates = [
        stamp(dt.date(2025, 2, 22), 23, 59, 59, 999999),
        stamp(dt.date(2025, 2, 23)),
        stamp(TODAY, 23, 59, 59, 999999),
        stamp(dt.date(2025, 3, 2)),
    ]
    for when in dates:
        db.add(models.Message(conversation_id=conv.id, role="assistant", status="ok",
                              created_at=when, route=encoded({"calls": 1})))
    db.commit()
    bounded = client.get("/api/usage?days=7", headers=headers["admin"]).json()
    assert bounded["totals"]["calls"] == 2
    assert bounded["daily"][0]["calls"] == 1
    assert bounded["daily"][-1]["calls"] == 1
    longer = client.get("/api/usage?days=30", headers=headers["admin"]).json()
    assert longer["totals"]["calls"] == 3
    all_time = client.get("/api/usage?days=0", headers=headers["admin"]).json()
    assert all_time["totals"]["calls"] == 4
    assert all_time["daily"][-1]["date"] == "2025-03-02"

    # Some database drivers return aware values; dates must be converted to UTC.
    plus_eight = dt.timezone(dt.timedelta(hours=8))
    minus_five = dt.timezone(dt.timedelta(hours=-5))
    assert usage._utc_date(dt.datetime(2025, 3, 1, 0, 30, tzinfo=plus_eight)) == dt.date(2025, 2, 28)
    assert usage._utc_date(dt.datetime(2025, 2, 28, 23, 30, tzinfo=minus_five)) == TODAY


def test_bounded_queries_handle_offsets_and_old_data(isolated_api, monkeypatch):
    client, db, headers, admin = isolated_api
    kb = models.KnowledgeBase(name="offset-test", mode="basic")
    conv = models.Conversation(user_id=admin.id)
    db.add_all([kb, conv])
    db.flush()
    # Two values belong to the UTC window even though their stored calendar
    # date is just outside it; the other two belong outside the window.
    timestamps = [
        "2025-02-22 19:00:00-05:00",  # Feb 23 00:00 UTC
        "2025-03-02 07:59:59+08:00",  # Mar 1 23:59 UTC
        "2025-02-23 00:01:00+08:00",  # Feb 22 UTC
        "2025-03-01 23:59:59-08:00",  # Mar 2 UTC
        "2020-01-01 00:00:00+00:00",  # far outside SQL bounds
    ]
    for when in timestamps:
        raw = encoded({"calls": 1}, marker=when)
        task = models.Task(kind="reindex_kb", kb_id=kb.id, created_at=stamp(TODAY), result=raw)
        record = models.CostRecord(kb_id=kb.id, kind="wiki_edit", source="wiki_edit", calls=1)
        message = models.Message(conversation_id=conv.id, role="assistant", status="ok", route=raw)
        db.add_all([task, record, message])
        db.flush()
        db.execute(text("UPDATE tasks SET finished_at = :when WHERE id = :id"),
                   {"when": when, "id": task.id})
        db.execute(text("UPDATE cost_records SET created_at = :when WHERE id = :id"),
                   {"when": when, "id": record.id})
        db.execute(text("UPDATE messages SET created_at = :when WHERE id = :id"),
                   {"when": when, "id": message.id})
    db.commit()

    observed = []
    original = usage._json_counters

    def spy(raw):
        observed.append(raw)
        return original(raw)

    monkeypatch.setattr(usage, "_json_counters", spy)
    statements = []
    engine = db.get_bind()

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        data = client.get("/api/usage?days=7", headers=headers["admin"]).json()
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert data["totals"] == totals(calls=6, record_count=6, unmetered_records=6)
    assert data["daily"][0]["calls"] == 3
    assert data["daily"][-1]["calls"] == 3
    assert len(observed) == 4  # outside-window JSON is never parsed
    assert any("messages.created_at >= " in sql and "messages.created_at < " in sql
               for sql in statements)
    assert any("cost_records.created_at >= " in sql and "cost_records.created_at < " in sql
               for sql in statements)
    assert any("coalesce(tasks.finished_at, tasks.created_at) >= " in sql for sql in statements)


def test_concurrent_worker_commit_uses_one_snapshot(isolated_api):
    client, db, headers, admin = isolated_api
    # Match the application's WAL mode so the writer can commit while the
    # dashboard's SELECT cursor is still open.
    assert db.execute(text("PRAGMA journal_mode=WAL")).scalar().lower() == "wal"
    db.commit()
    kb = models.KnowledgeBase(name="concurrent-test", mode="basic")
    conv = models.Conversation(user_id=admin.id)
    db.add_all([kb, conv])
    db.flush()
    task = models.Task(kind="ingest_document", kb_id=kb.id, created_at=stamp(TODAY),
                       result=encoded({"calls": 1, "prompt_tokens": 10}))
    db.add(task)
    db.commit()
    kb_id, task_id, conversation_id = kb.id, task.id, conv.id
    engine = db.get_bind()
    make_writer = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    committed = []
    statements = []

    def interleave(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)
        if committed or "FROM cost_records" not in statement:
            return
        # The first cost query has started, but the reader has not consumed it.
        # The old three-query implementation would subsequently suppress the
        # legacy task because of this record, after missing it in its first query.
        committed.append(True)
        with make_writer() as writer:
            current_task = writer.get(models.Task, task_id)
            current_task.result = encoded({"calls": 2, "prompt_tokens": 20})
            writer.add(models.CostRecord(
                kb_id=kb_id, kind="ingest_document", source="task", source_id=task_id,
                created_at=stamp(TODAY), calls=2, prompt_tokens=20,
            ))
            writer.add(models.Message(
                conversation_id=conversation_id, role="assistant", status="ok",
                created_at=stamp(TODAY), route=encoded({"calls": 4}),
            ))
            writer.commit()

    event.listen(engine, "after_cursor_execute", interleave)
    try:
        response = client.get("/api/usage?days=7", headers=headers["admin"])
    finally:
        event.remove(engine, "after_cursor_execute", interleave)
    assert committed == [True]
    assert response.status_code == 200, response.text
    # The in-flight read sees the complete old state, not an empty mixture.
    assert response.json()["totals"] == totals(
        calls=1, prompt_tokens=10, total_tokens=10, record_count=1,
        cost=cost_bucket(unpriced=1, unpriced_tokens=10))
    aggregation = [sql for sql in statements if "FROM cost_records" in sql]
    assert len(aggregation) == 1 and "UNION ALL" in aggregation[0]
    # A later read sees the complete committed state, still counted once.
    after = client.get("/api/usage?days=7", headers=headers["admin"]).json()
    assert after["totals"] == totals(
        calls=6, prompt_tokens=20, total_tokens=20, record_count=2,
        unmetered_records=1,
        cost=cost_bucket(unpriced=1, unpriced_tokens=20))
    sources = {row["source"]: row for row in after["by_source"]}
    assert sources["processing"]["calls"] == 2 and sources["chat"]["calls"] == 4


def test_summarize_preserves_caller_transaction_and_pending_writes(isolated_api):
    _, db, _, _ = isolated_api
    db.autoflush = True
    kb = models.KnowledgeBase(name="uncommitted-kb", mode="basic")
    db.add(kb)
    db.flush()
    db.add(models.Task(kind="ingest_document", kb_id=kb.id, created_at=stamp(TODAY),
                       result=encoded({"calls": 5})))
    db.flush()
    pending = models.KnowledgeBase(name="pending-kb", mode="basic")
    db.add(pending)

    result = usage.summarize(db, 7)
    assert result["totals"] == totals(calls=5, record_count=1, unmetered_records=1)
    assert pending in db.new and pending.id is None
    db.rollback()
    assert db.scalar(select(models.KnowledgeBase.id).where(
        models.KnowledgeBase.name.in_(["uncommitted-kb", "pending-kb"]))) is None


def test_legacy_usage_survives_retry_new_attempt_and_retry_again(isolated_api, monkeypatch):
    client, db, headers, _ = isolated_api
    kb = models.KnowledgeBase(name="legacy-retry", mode="basic")
    db.add(kb)
    db.flush()
    finished = stamp(dt.date(2025, 2, 26), 12)
    legacy = {"calls": 0, "prompt_tokens": 42, "completion_tokens": 7,
              "embed_tokens": 15, "embed_texts": 3, "mode": "offline", "embed_mode": "live"}
    task = models.Task(kind="ingest_document", status="failed", kb_id=kb.id,
                       created_at=stamp(dt.date(2025, 2, 1)), finished_at=finished,
                       result=encoded(legacy))
    db.add(task)
    db.commit()
    kb_id, task_id = kb.id, task.id

    def read_totals():
        dashboard = client.get("/api/usage?days=0", headers=headers["admin"])
        kb_cost = client.get(f"/api/kbs/{kb_id}/cost", headers=headers["admin"])
        assert dashboard.status_code == kb_cost.status_code == 200
        return dashboard.json(), kb_cost.json()

    def assert_counts(data, kb_cost, *, calls, prompt, completion, embed, texts, count):
        for bucket in (data["totals"], kb_cost["totals"]):
            assert bucket["calls"] == calls
            assert bucket["prompt_tokens"] == prompt
            assert bucket["completion_tokens"] == completion
            assert bucket["embed_tokens"] == embed
            assert bucket["total_tokens"] == prompt + completion + embed
            assert bucket["record_count"] == count
        assert kb_cost["totals"]["embed_texts"] == texts

    dashboard, summary = read_totals()
    assert_counts(dashboard, summary, calls=0, prompt=42, completion=7, embed=15, texts=3, count=1)
    assert summary["legacy_records"] == 1
    assert dashboard["daily"][0]["date"] == "2025-02-26"

    retry = client.post(f"/api/tasks/{task_id}/retry", headers=headers["admin"])
    assert retry.status_code == 200, retry.text
    db.expire_all()
    assert db.get(models.Task, task_id).result == "{}"
    rows = db.execute(select(models.CostRecord).where(
        models.CostRecord.source == "task", models.CostRecord.source_id == task_id)).scalars().all()
    assert len(rows) == 1 and rows[0].created_at == finished
    assert rows[0].mode == "offline" and rows[0].embed_mode == "live"
    dashboard, summary = read_totals()
    assert_counts(dashboard, summary, calls=0, prompt=42, completion=7, embed=15, texts=3, count=1)
    assert summary["legacy_records"] == 0
    assert dashboard["daily"][0]["date"] == "2025-02-26"

    # Exercise the actual worker's attempt and phase-recording transaction in
    # this isolated database, while keeping the model work deterministic.
    make_worker = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)

    @contextmanager
    def worker_session(*, role):
        with make_worker() as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    def ingest(_db, current_task, *, stats):
        assert current_task.id == task_id
        stats.record_chat(prompt_tokens=3, completion_tokens=5, latency_ms=20)
        stats.record_embed(texts=1, tokens=7, latency_ms=15, live=False)
        return {"usage_by_phase": {current_task.kind: stats.as_dict()}}

    monkeypatch.setattr(worker, "session_scope", worker_session)
    monkeypatch.setattr(worker.ingest, "ingest_version", ingest)
    assert worker.run_once("legacy-retry-test")
    dashboard, summary = read_totals()
    assert_counts(dashboard, summary, calls=2, prompt=45, completion=12, embed=22, texts=4, count=2)
    assert next(row for row in dashboard["daily"] if row["date"] == "2025-02-26")["prompt_tokens"] == 42

    retry_again = client.post(f"/api/tasks/{task_id}/retry", headers=headers["admin"])
    assert retry_again.status_code == 200, retry_again.text
    assert retry_again.json()["retry_count"] == 2
    dashboard, summary = read_totals()
    assert_counts(dashboard, summary, calls=2, prompt=45, completion=12, embed=22, texts=4, count=2)
    db.expire_all()
    assert len(db.execute(select(models.CostRecord).where(
        models.CostRecord.source == "task", models.CostRecord.source_id == task_id)).scalars().all()) == 2


def test_legacy_preservation_rolls_back_with_requeue(isolated_api):
    _, db, _, _ = isolated_api
    kb = models.KnowledgeBase(name="retry-rollback", mode="basic")
    db.add(kb)
    db.flush()
    original = encoded({"calls": 2, "prompt_tokens": 9})
    task = models.Task(kind="ingest_document", status="failed", kb_id=kb.id,
                       created_at=stamp(TODAY), result=original)
    db.add(task)
    db.commit()
    task_id = task.id
    taskflow.requeue(db, task)
    assert db.execute(select(models.CostRecord).where(
        models.CostRecord.source_id == task_id)).scalar_one().prompt_tokens == 9
    task.result = original
    cost.preserve_legacy_before_retry(db, task)
    assert db.scalar(select(func.count()).select_from(models.CostRecord)) == 1
    db.rollback()
    assert db.get(models.Task, task_id).status == "failed"
    assert db.get(models.Task, task_id).result == original
    assert db.execute(select(models.CostRecord).where(
        models.CostRecord.source_id == task_id)).first() is None


def test_pending_and_existing_cost_records_suppress_legacy_insert(isolated_api):
    _, db, _, _ = isolated_api
    kb = models.KnowledgeBase(name="retry-covered", mode="basic")
    db.add(kb)
    db.flush()
    task = models.Task(kind="ingest_document", status="failed", kb_id=kb.id,
                       created_at=stamp(TODAY), result=encoded({"calls": 50}))
    db.add(task)
    db.flush()
    # The default source becomes 'task' on flush, but is None on this pending
    # object. Repeated requeue calls must not duplicate it or flush the caller.
    pending = models.CostRecord(kb_id=kb.id, kind="failed_attempt", source_id=task.id,
                                calls=4, created_at=stamp(dt.date(2020, 1, 1)))
    db.add(pending)
    taskflow.requeue(db, task)
    task.result = encoded({"calls": 50})
    taskflow.requeue(db, task)
    assert pending in db.new
    assert db.execute(select(func.count()).select_from(models.CostRecord)).scalar() == 0
    db.commit()
    task.result = encoded({"calls": 50})
    taskflow.requeue(db, task)
    db.commit()
    rows = db.execute(select(models.CostRecord).where(
        models.CostRecord.source == "task", models.CostRecord.source_id == task.id)).scalars().all()
    assert len(rows) == 1 and rows[0].calls == 4


def test_retry_preserves_every_counter_and_created_timestamp(isolated_api):
    _, db, _, _ = isolated_api
    kb = models.KnowledgeBase(name="all-legacy-counters", mode="basic")
    db.add(kb)
    db.flush()
    created = stamp(dt.date(2025, 2, 20), 8)
    counters = {"calls": 6, "live_calls": 4, "offline_calls": 2, "chat_calls": 1,
                "embed_calls": 3, "vision_calls": 2, "prompt_tokens": 11,
                "completion_tokens": 12, "embed_tokens": 13, "embed_texts": 14,
                "latency_ms": 15}
    task = models.Task(kind="reindex_kb", status="failed", kb_id=kb.id,
                       created_at=created, result=encoded(counters))
    db.add(task)
    db.flush()
    # A non-task source with the same numeric ID is not coverage.
    db.add(models.CostRecord(kb_id=kb.id, kind="wiki_edit", source="wiki_edit",
                             source_id=task.id, calls=1, created_at=created))
    db.commit()
    taskflow.requeue(db, task)
    db.commit()
    row = db.execute(select(models.CostRecord).where(
        models.CostRecord.source == "task", models.CostRecord.source_id == task.id)).scalar_one()
    assert row.created_at == created
    assert {field: getattr(row, field) for field in counters} == counters
    assert row.mode == "" and row.embed_mode == ""  # absent modes are not inferred


def test_retry_normalizes_legacy_offset_timestamp_to_utc(isolated_api):
    client, db, headers, _ = isolated_api
    kb = models.KnowledgeBase(name="offset-retry", mode="basic")
    db.add(kb)
    db.flush()
    task = models.Task(kind="ingest_document", status="failed", kb_id=kb.id,
                       created_at=stamp(dt.date(2025, 1, 1)), result=encoded({"calls": 3}))
    db.add(task)
    db.flush()
    task_id = task.id
    # The local wall date is March 1, but its UTC date is February 28.
    db.execute(text("UPDATE tasks SET finished_at = :finished WHERE id = :id"),
               {"finished": "2025-03-01 01:00:00+08:00", "id": task_id})
    db.commit()
    before = client.get("/api/usage?days=7", headers=headers["admin"]).json()
    assert next(day for day in before["daily"] if day["calls"])["date"] == "2025-02-28"
    response = client.post(f"/api/tasks/{task_id}/retry", headers=headers["admin"])
    assert response.status_code == 200, response.text
    after = client.get("/api/usage?days=7", headers=headers["admin"]).json()
    assert after == before
    row = db.execute(select(models.CostRecord).where(
        models.CostRecord.source == "task", models.CostRecord.source_id == task_id)).scalar_one()
    assert row.created_at == dt.datetime(2025, 2, 28, 17)


def test_out_of_range_legacy_counter_does_not_prevent_retry(isolated_api):
    client, db, headers, _ = isolated_api
    kb = models.KnowledgeBase(name="large-invalid-retry", mode="basic")
    db.add(kb)
    db.flush()
    task = models.Task(kind="ingest_document", status="failed", kb_id=kb.id,
                       created_at=stamp(TODAY),
                       result=encoded({"calls": 10**100, "prompt_tokens": 9}))
    db.add(task)
    db.commit()
    response = client.post(f"/api/tasks/{task.id}/retry", headers=headers["admin"])
    assert response.status_code == 200, response.text
    row = db.execute(select(models.CostRecord).where(
        models.CostRecord.source == "task", models.CostRecord.source_id == task.id)).scalar_one()
    assert row.calls == 0 and row.prompt_tokens == 9


@pytest.mark.parametrize("raw", ["{", "null", "[]", '{"usage": {}}',
                                 '{"usage": {"calls": "bad"}}',
                                 '{"usage": {"calls": -1, "prompt_tokens": true}}'])
def test_invalid_legacy_usage_does_not_break_retry(isolated_api, raw):
    client, db, headers, _ = isolated_api
    kb = models.KnowledgeBase(name="invalid-retry", mode="basic")
    db.add(kb)
    db.flush()
    task = models.Task(kind="ingest_document", status="failed", kb_id=kb.id,
                       created_at=stamp(TODAY), result=raw)
    db.add(task)
    db.commit()
    response = client.post(f"/api/tasks/{task.id}/retry", headers=headers["admin"])
    assert response.status_code == 200, response.text
    assert db.scalar(select(func.count()).select_from(models.CostRecord)) == 0


def test_malformed_and_invalid_usage_is_ignored(isolated_api):
    client, db, headers, admin = isolated_api
    kb = models.KnowledgeBase(name="invalid-data", mode="basic")
    conv = models.Conversation(user_id=admin.id)
    db.add_all([kb, conv])
    db.flush()
    bad_json = ["{", "null", "[]", '"text"', '{"usage": []}', '{"usage": {}}',
                '{"usage": {"calls": -1, "prompt_tokens": true}}']
    for raw in bad_json:
        db.add(models.Task(kind="ingest_document", kb_id=kb.id, result=raw, created_at=stamp(TODAY)))
        db.add(models.Message(conversation_id=conv.id, role="assistant", status="ok",
                              route=raw, created_at=stamp(TODAY)))
    db.add(models.Message(
        conversation_id=conv.id, role="assistant", status="ok", created_at=stamp(TODAY),
        route=encoded({"calls": 3, "prompt_tokens": -4, "completion_tokens": "broken",
                       "embed_tokens": True, "latency_ms": 1.5, "total_tokens": 999}),
    ))
    # Zero is a valid stored counter, and every CostRecord counts even if its
    # integer column contains corrupt SQLite data.
    db.add(models.Task(kind="reindex_kb", kb_id=kb.id, created_at=stamp(TODAY),
                       result=encoded({"calls": 0})))
    db.add(models.CostRecord(kb_id=kb.id, kind="failed_attempt", source="task",
                             calls="corrupt", created_at=stamp(TODAY)))
    db.commit()

    data = client.get("/api/usage?days=7", headers=headers["admin"]).json()
    assert data["totals"] == totals(calls=3, record_count=3, unmetered_records=3)
    sources = {row["source"]: row for row in data["by_source"]}
    assert sources["processing"]["record_count"] == 2
    assert sources["chat"]["record_count"] == 1
    assert data["daily"][-1]["total_tokens"] == 0


# ------------------------------------------------------------------ 个人 Agent 与计价
def _model_config(db, model: str, *, purpose: str = "llm") -> models.ModelConfig:
    provider = models.Provider(
        name=f"usage-{uuid.uuid4().hex[:8]}", base_url="https://example.test/v1",
        api_key_enc="", protocol="openai")
    db.add(provider)
    db.flush()
    cfg = models.ModelConfig(provider_id=provider.id, purpose=purpose, model=model,
                             label="测试模型")
    db.add(cfg)
    db.commit()
    return cfg


def _agent_session(db, user_id: int, core_id: str,
                   model_config_id: int | None = None) -> models.AgentSession:
    session = models.AgentSession(user_id=user_id, core_id=core_id,
                                  model_config_id=model_config_id)
    db.add(session)
    db.flush()
    return session


def test_agent_usage_counts_once_and_splits_by_core(isolated_api):
    client, db, headers, admin = isolated_api
    # 用户模型标识带渠道前缀、目录里带日期后缀：归一化后应命中同一条参考价。
    cfg = _model_config(db, "kz/deepseek-v4.1-flash")
    kb_session = _agent_session(db, admin.id, "kb", cfg.id)
    cli_session = _agent_session(db, admin.id, "codex")
    conv = models.Conversation(user_id=admin.id)
    db.add(conv)
    db.flush()

    kb_usage = {"calls": 1, "chat_calls": 1, "live_calls": 1, "offline_calls": 0,
                "prompt_tokens": 1_000_000, "completion_tokens": 500_000,
                "embed_tokens": 2_000, "latency_ms": 1200}
    cli_usage = {"calls": 1, "chat_calls": 1, "live_calls": 1, "offline_calls": 0,
                 "prompt_tokens": 300_000, "completion_tokens": 100_000,
                 "embed_tokens": 0, "latency_ms": 800}
    chat_usage = {"calls": 1, "chat_calls": 1, "live_calls": 1,
                  "prompt_tokens": 1_000_000, "completion_tokens": 0}
    db.add_all([
        models.AgentMessage(session_id=kb_session.id, role="assistant", status="ok",
                            core_id="kb", meta=encoded(kb_usage, core="kb"),
                            created_at=stamp(TODAY)),
        models.AgentMessage(session_id=cli_session.id, role="assistant", status="ok",
                            core_id="codex", meta=encoded(cli_usage, core="codex"),
                            created_at=stamp(TODAY)),
        models.Message(conversation_id=conv.id, role="assistant", status="ok",
                       model_config_id=cfg.id, route=encoded(chat_usage),
                       created_at=stamp(TODAY)),
    ])
    db.commit()

    data = client.get("/api/usage?days=7", headers=headers["admin"]).json()
    assert data["currency"] == "USD" and data["price_unit"] == "per_1m_tokens"
    total = data["totals"]
    # 个人 Agent 的每次执行、每条成功问答各算一次：三条记录，互不吞并。
    assert total["calls"] == 3 and total["record_count"] == 3
    assert total["prompt_tokens"] == 2_300_000
    assert total["completion_tokens"] == 600_000
    assert total["unmetered_records"] == 0
    # 内置参考价 0.022 / 0.294 USD / 1M token：
    #   kb 核心 = 1.0 × 0.022 + 0.5 × 0.294 = 0.169
    #   问答    = 1.0 × 0.022 = 0.022
    #   codex 会话没有配置模型 → 整行未计价
    assert total["cost"]["amount"] == pytest.approx(0.191)
    assert total["cost"]["priced_records"] == 2
    assert total["cost"]["unpriced_records"] == 1
    # 未计价 token = codex 的 400000 + kb 记录的向量 token 2000（向量永不按生成价计）
    assert total["cost"]["unpriced_tokens"] == 402_000

    sources = {row["source"]: row for row in data["by_source"]}
    assert sources["agent"]["calls"] == 2
    assert sources["agent"]["record_count"] == 2
    assert sources["chat"]["calls"] == 1
    cores = {row["core_id"]: row for row in data["by_core"]}
    assert set(cores) == {"kb", "codex"}
    assert cores["kb"]["label"] == agent_plugins.BUILTIN_BY_ID["kb"].name
    assert cores["codex"]["label"] == agent_plugins.BUILTIN_BY_ID["codex"].name
    assert cores["kb"]["cost"]["amount"] == pytest.approx(0.169)
    assert cores["codex"]["cost"]["unpriced_records"] == 1
    # 每日金额之和必须等于总计，否则趋势图和汇总表会对不上。
    assert sum(day["cost"]["amount"] for day in data["daily"]) == pytest.approx(
        total["cost"]["amount"])


def test_agent_without_usage_is_unmetered_and_filters_apply(isolated_api):
    client, db, headers, admin = isolated_api
    session = _agent_session(db, admin.id, "kb")
    db.add_all([
        # 执行确实发生了，但没留下任何 token：计 1 次调用、标未计量。
        models.AgentMessage(session_id=session.id, role="assistant", status="ok",
                            core_id="kb", meta="{}", created_at=stamp(TODAY)),
        # 失败/停止的执行与问答同口径，不计入；用户消息也不是用量记录。
        models.AgentMessage(session_id=session.id, role="assistant", status="failed",
                            core_id="kb", meta=encoded({"calls": 1, "prompt_tokens": 10}),
                            created_at=stamp(TODAY)),
        models.AgentMessage(session_id=session.id, role="assistant", status="stopped",
                            core_id="kb", meta=encoded({"calls": 1, "prompt_tokens": 10}),
                            created_at=stamp(TODAY)),
        models.AgentMessage(session_id=session.id, role="user", status="ok",
                            core_id="kb", meta=encoded({"calls": 1, "prompt_tokens": 10}),
                            created_at=stamp(TODAY)),
    ])
    db.commit()

    data = client.get("/api/usage?days=7", headers=headers["admin"]).json()
    assert data["totals"] == totals(calls=1, chat_calls=1, live_calls=1,
                                    record_count=1, unmetered_records=1)
    assert data["by_core"] == [{
        "core_id": "kb", "label": agent_plugins.BUILTIN_BY_ID["kb"].name,
        **totals(calls=1, chat_calls=1, live_calls=1, record_count=1,
                 unmetered_records=1),
    }]


def test_user_price_overrides_builtin_price(isolated_api):
    client, db, headers, admin = isolated_api
    cfg = _model_config(db, "kz/deepseek-v4.1-flash")
    session = _agent_session(db, admin.id, "codex", cfg.id)
    db.add(models.AgentMessage(
        session_id=session.id, role="assistant", status="ok", core_id="codex",
        meta=encoded({"calls": 1, "chat_calls": 1, "live_calls": 1,
                      "prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}),
        created_at=stamp(TODAY)))
    db.commit()

    builtin = client.get("/api/usage?days=7", headers=headers["admin"]).json()
    assert builtin["totals"]["cost"]["amount"] == pytest.approx(0.022 + 0.294)

    # 用户单价优先于内置参考价；0 也是合法价格（行存在 = 填过）。
    price = models.ModelPrice(model_config_id=cfg.id, currency="USD",
                              input_per_1m=1.0, output_per_1m=2.0, updated_by=admin.id)
    db.add(price)
    db.commit()
    override = client.get("/api/usage?days=7", headers=headers["admin"]).json()
    assert override["totals"]["cost"]["amount"] == pytest.approx(3.0)
    assert override["totals"]["cost"]["priced_records"] == 1
    assert override["totals"]["cost"]["unpriced_tokens"] == 0

    price.input_per_1m = 0.0
    price.output_per_1m = 0.0
    db.commit()
    zero = client.get("/api/usage?days=7", headers=headers["admin"]).json()
    assert zero["totals"]["cost"]["amount"] == 0.0
    # 「填了 0」仍是已计价：不能退回内置价，也不能算成未计价。
    assert zero["totals"]["cost"]["priced_records"] == 1
    assert zero["totals"]["cost"]["unpriced_records"] == 0

    db.delete(price)
    db.commit()
    restored = client.get("/api/usage?days=7", headers=headers["admin"]).json()
    assert restored["totals"]["cost"]["amount"] == pytest.approx(0.022 + 0.294)
