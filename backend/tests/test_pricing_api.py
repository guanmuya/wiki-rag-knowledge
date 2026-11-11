"""参考单价接口：权限、404/422、覆盖价 upsert、回到内置价、审计留痕。

价格是「参考价」——这组用例同时守住三件事：非管理员看不到也改不了、
未知模型不能凭空建价、审计里查得到谁改的。
"""
from __future__ import annotations

import uuid

import pytest

from conftest import mark_user_ready

PASSWORD = "Price-Employee#2026"


@pytest.fixture(scope="module")
def pricing_model(app_client, admin_headers):
    """一个能命中内置参考价的模型配置（kz/deepseek-v4.1-flash → 0.022 / 0.294）。"""
    provider = app_client.post("/api/settings/providers", headers=admin_headers, json={
        "name": f"price-{uuid.uuid4().hex[:8]}",
        "base_url": "http://127.0.0.1:9/v1",
        "api_key": "sk-fake-key-for-pricing",
        "enabled": True,
        "protocol": "openai",
    })
    assert provider.status_code == 201, provider.text
    created = app_client.post("/api/settings/models", headers=admin_headers, json={
        "provider_id": provider.json()["id"], "purpose": "llm",
        "model": "kz/deepseek-v4.1-flash", "label": "参考价测试模型",
        "is_default": False, "enabled": True,
    })
    assert created.status_code == 201, created.text
    return created.json()


@pytest.fixture(scope="module")
def employee_headers(app_client, admin_headers):
    username = f"price_emp_{uuid.uuid4().hex[:6]}"
    created = app_client.post("/api/users", headers=admin_headers, json={
        "username": username, "display_name": "价格只读员工",
        "password": PASSWORD, "role": "employee", "grants": [],
    })
    assert created.status_code == 201, created.text
    mark_user_ready(username)
    login = app_client.post("/api/auth/login",
                            json={"username": username, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['token']}"}


def _item(client, headers, model_config_id: int) -> dict:
    data = client.get("/api/settings/pricing", headers=headers).json()
    return next(i for i in data["items"] if i["model_config_id"] == model_config_id)


def test_pricing_is_admin_only(app_client, employee_headers, pricing_model):
    assert app_client.get("/api/settings/pricing").status_code == 401
    assert app_client.get("/api/settings/pricing", headers=employee_headers).status_code == 403
    body = {"input_per_1m": 1.0, "output_per_1m": 2.0}
    assert app_client.put(f"/api/settings/pricing/{pricing_model['id']}",
                          headers=employee_headers, json=body).status_code == 403
    assert app_client.delete(f"/api/settings/pricing/{pricing_model['id']}",
                             headers=employee_headers).status_code == 403


def test_catalog_exposes_sources_and_builtin_price(app_client, admin_headers, pricing_model):
    data = app_client.get("/api/settings/pricing", headers=admin_headers).json()
    assert data["currency"] == "USD" and data["price_unit"] == "per_1m_tokens"
    # 参考价的标记与说明必须一起返回：界面要能原样告诉用户「这不是账单」。
    assert data["reference"] is True
    assert "参考价" in data["note"]
    assert data["sources"] and all(source["url"].startswith("https://")
                                  for source in data["sources"])

    item = next(i for i in data["items"] if i["model_config_id"] == pricing_model["id"])
    assert item["model"] == "kz/deepseek-v4.1-flash"
    assert item["match_key"] == "deepseek-v4.1-flash"
    assert item["builtin"]["input_per_1m"] == 0.022
    assert item["builtin"]["output_per_1m"] == 0.294
    assert item["builtin"]["verified_at"] == "2026-11-11"
    assert item["builtin"]["source_url"].startswith("https://")
    assert item["user"] is None
    assert item["effective"]["origin"] == "builtin"
    assert item["effective"]["input_per_1m"] == 0.022


def test_put_sets_user_price_and_delete_restores_builtin(app_client, admin_headers,
                                                        pricing_model):
    model_id = pricing_model["id"]
    saved = app_client.put(f"/api/settings/pricing/{model_id}", headers=admin_headers,
                           json={"input_per_1m": 0.5, "output_per_1m": 1.5})
    assert saved.status_code == 200, saved.text
    assert saved.json()["effective"]["origin"] == "user"
    assert saved.json()["effective"]["input_per_1m"] == 0.5
    assert saved.json()["user"]["input_per_1m"] == 0.5
    assert saved.json()["user"]["updated_at"]

    listed = _item(app_client, admin_headers, model_id)
    assert listed["effective"]["origin"] == "user"
    assert listed["effective"]["output_per_1m"] == 1.5
    assert listed["builtin"]["input_per_1m"] == 0.022      # 内置价仍然看得到

    cleared = app_client.delete(f"/api/settings/pricing/{model_id}", headers=admin_headers)
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["effective"]["origin"] == "builtin"
    assert cleared.json()["user"] is None

    # 幂等：没有覆盖价时再删一次也成功。
    again = app_client.delete(f"/api/settings/pricing/{model_id}", headers=admin_headers)
    assert again.status_code == 200
    assert again.json()["effective"]["origin"] == "builtin"


def test_zero_price_is_a_real_price(app_client, admin_headers, pricing_model):
    model_id = pricing_model["id"]
    saved = app_client.put(f"/api/settings/pricing/{model_id}", headers=admin_headers,
                           json={"input_per_1m": 0, "output_per_1m": 0})
    assert saved.status_code == 200, saved.text
    # 0 是「这个渠道不额外计费」，不能退回内置价、也不能算成未填。
    assert saved.json()["effective"]["origin"] == "user"
    assert saved.json()["effective"]["input_per_1m"] == 0.0
    app_client.delete(f"/api/settings/pricing/{model_id}", headers=admin_headers)


def test_unknown_model_is_404(app_client, admin_headers):
    body = {"input_per_1m": 1.0, "output_per_1m": 2.0}
    assert app_client.put("/api/settings/pricing/99999999",
                          headers=admin_headers, json=body).status_code == 404
    assert app_client.delete("/api/settings/pricing/99999999",
                             headers=admin_headers).status_code == 404


@pytest.mark.parametrize("body", [
    {"input_per_1m": -1, "output_per_1m": 1},
    {"input_per_1m": 1, "output_per_1m": -0.5},
    {"input_per_1m": 100001, "output_per_1m": 1},
    {"input_per_1m": 1},
])
def test_invalid_price_is_422(app_client, admin_headers, pricing_model, body):
    response = app_client.put(f"/api/settings/pricing/{pricing_model['id']}",
                              headers=admin_headers, json=body)
    assert response.status_code == 422, response.text


def test_price_changes_are_audited(app_client, admin_headers, pricing_model):
    model_id = pricing_model["id"]
    assert app_client.put(f"/api/settings/pricing/{model_id}", headers=admin_headers,
                          json={"input_per_1m": 2.0, "output_per_1m": 4.0}).status_code == 200
    assert app_client.delete(f"/api/settings/pricing/{model_id}",
                             headers=admin_headers).status_code == 200

    items = app_client.get("/api/settings/audit?page_size=100", headers=admin_headers).json()["items"]
    for action in ("set_model_price", "clear_model_price"):
        assert any(entry["action"] == action and entry["target"] == pricing_model["model"]
                   for entry in items), action
