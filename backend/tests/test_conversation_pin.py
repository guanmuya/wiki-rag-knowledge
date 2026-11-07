"""会话置顶：`pinned` 要能单独改，列表按「置顶优先、最近更新优先」排序。"""
from __future__ import annotations


def _create(client, headers, title: str) -> dict:
    res = client.post("/api/conversations", headers=headers, json={"title": title, "kb_ids": []})
    assert res.status_code == 201, res.text
    return res.json()


def test_pin_sorts_first_and_keeps_other_fields(app_client, admin_headers):
    older = _create(app_client, admin_headers, "置顶测试-旧")
    newer = _create(app_client, admin_headers, "置顶测试-新")
    assert older["pinned"] is False and newer["pinned"] is False

    res = app_client.patch(f"/api/conversations/{older['id']}", headers=admin_headers,
                           json={"pinned": True})
    assert res.status_code == 200, res.text
    assert res.json()["pinned"] is True

    items = app_client.get("/api/conversations", headers=admin_headers).json()["items"]
    ids = [item["id"] for item in items]
    assert ids.index(older["id"]) < ids.index(newer["id"]), "置顶会话应排在普通会话之前"
    assert items[0]["pinned"] is True
    assert all(not item["pinned"] for item in items if item["id"] != older["id"])

    # 只改标题时不能把置顶状态冲掉
    res = app_client.patch(f"/api/conversations/{older['id']}", headers=admin_headers,
                           json={"title": "置顶测试-改名"})
    assert res.status_code == 200, res.text
    assert res.json()["title"] == "置顶测试-改名"
    assert res.json()["pinned"] is True

    res = app_client.patch(f"/api/conversations/{older['id']}", headers=admin_headers,
                           json={"pinned": False})
    assert res.status_code == 200, res.text
    assert res.json()["pinned"] is False
    items = app_client.get("/api/conversations", headers=admin_headers).json()["items"]
    assert all(not item["pinned"] for item in items)
