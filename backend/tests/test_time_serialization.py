"""时间字段必须带时区偏移。

背景（真实缺陷）：SQLite 的 DATETIME 列不保存时区，`models.utcnow()` 写入的是 UTC，
读回来却是 naive datetime。序列化时直接 `.isoformat()`，浏览器按**本地时间**解析，
于是东八区用户看到的所有时间都差 8 小时：任务一创建就显示「已运行 8 小时」、
「最近登录」显示的是 8 小时前。这里锁住「输出必须带 UTC 偏移」这一契约。
"""
from __future__ import annotations

import datetime as dt

from app import models, serializers


def _assert_utc_offset(value: str, field: str) -> None:
    assert isinstance(value, str) and value, f"{field} 为空"
    assert value.endswith("+00:00"), f"{field} 缺少 UTC 偏移: {value!r}"
    parsed = dt.datetime.fromisoformat(value)
    assert parsed.utcoffset() == dt.timedelta(0)


def test_iso_marks_naive_datetime_as_utc():
    naive = dt.datetime(2026, 11, 6, 12, 45, 9, 883980)
    assert serializers.iso(naive) == "2026-11-06T12:45:09.883980+00:00"


def test_iso_keeps_existing_offset():
    aware = dt.datetime(2026, 11, 6, 12, 45, 9, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    assert serializers.iso(aware).endswith("+08:00")


def test_iso_none_stays_none():
    assert serializers.iso(None) is None


def test_kb_and_task_timestamps_are_utc(app_client, admin_headers, db_session):
    kb = models.KnowledgeBase(name="时间显示测试库")
    db_session.add(kb)
    db_session.flush()
    task = models.Task(kind=models.TaskKind.ingest_document.value, kb_id=kb.id)
    db_session.add(task)
    db_session.commit()

    kbs = app_client.get("/api/kbs", headers=admin_headers)
    assert kbs.status_code == 200, kbs.text
    item = next(k for k in kbs.json()["items"] if k["id"] == kb.id)
    _assert_utc_offset(item["created_at"], "kbs.created_at")

    tasks = app_client.get("/api/tasks", headers=admin_headers)
    assert tasks.status_code == 200, tasks.text
    row = next(t for t in tasks.json()["items"] if t["id"] == task.id)
    _assert_utc_offset(row["created_at"], "tasks.created_at")


def test_current_user_timestamps_are_utc(app_client, admin_headers):
    me = app_client.get("/api/auth/me", headers=admin_headers)
    assert me.status_code == 200, me.text
    body = me.json()
    user = body.get("user", body)
    _assert_utc_offset(user["created_at"], "auth.created_at")
