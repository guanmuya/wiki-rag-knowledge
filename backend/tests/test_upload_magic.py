"""上传内容与扩展名一致性校验（P1-8）回归用例。

清单口径：上传只校验扩展名、没有魔数检测——把任意二进制改名成 `.pdf` 就能进库。
这里先单测判定函数，再用真实接口验证两条上传路径（批量上传 / 上传新版本）都会
拒绝伪装文件，同时不误伤合法内容（文本、真 PDF 头、OOXML 的 ZIP 容器）。
"""
from __future__ import annotations

import io
import uuid
import zipfile

import pytest

from app.services import parser

MD = "# 差旅报销\n\n一线城市住宿上限 600 元每晚，其他城市 400 元每晚。\n"


def _ooxml_bytes() -> bytes:
    """docx/xlsx/pptx 的最小共同点：一个 ZIP 容器（PK\\x03\\x04 开头）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", "<xml/>")
    return buf.getvalue()


@pytest.fixture(scope="module")
def kb(app_client, admin_headers):
    r = app_client.post("/api/kbs", headers=admin_headers, json={
        "name": f"魔数校验库-{uuid.uuid4().hex[:8]}", "description": "", "mode": "basic"})
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------- 判定函数
def test_magic_rules_per_suffix():
    assert parser.magic_error(b"%PDF-1.7\n...", ".pdf") is None, "真 PDF 头必须放行"
    assert parser.magic_error(b"MZ\x90\x00\x03", ".pdf") is not None, "改名成 pdf 的 exe 必须拒绝"

    ooxml = _ooxml_bytes()
    for suffix in (".docx", ".xlsx", ".pptx"):
        assert parser.magic_error(ooxml, suffix) is None
        assert parser.magic_error(b"plain text pretending to be office", suffix) is not None

    assert parser.magic_error(MD.encode(), ".md") is None
    assert parser.magic_error(MD.encode(), ".txt") is None
    assert parser.magic_error(b"\x00\x01\x02binary", ".txt") is not None, "二进制不应冒充文本"
    assert parser.magic_error(b"\x00binary", ".markdown") is not None


def test_magic_rules_do_not_touch_unknown_suffixes():
    """未纳入规则的扩展名不额外收紧：判定只对「已知容器格式」负责。"""
    assert parser.magic_error(b"\x00\x01", ".bin") is None
    assert parser.magic_error(b"", ".pdf") is not None, "空内容不可能符合 PDF 头"


# ---------------------------------------------------------------- 批量上传
def test_upload_rejects_disguised_binary(app_client, admin_headers, kb):
    r = app_client.post(f"/api/kbs/{kb['id']}/documents", headers=admin_headers,
                        files=[("files", ("伪装成PDF.pdf", b"MZ\x90\x00\x03", "application/pdf"))])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["items"] == [], "内容不符的文件不该入库"
    assert len(body["errors"]) == 1
    assert "扩展名不符" in body["errors"][0]["error"]
    assert body["errors"][0]["filename"] == "伪装成PDF.pdf"


def test_upload_accepts_matching_content(app_client, admin_headers, kb):
    r = app_client.post(f"/api/kbs/{kb['id']}/documents", headers=admin_headers, files=[
        ("files", ("正常说明.md", MD.encode(), "text/markdown")),
        ("files", ("正常文档.pdf", b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n", "application/pdf")),
        ("files", ("正常文档.docx", _ooxml_bytes(),
                   "application/vnd.openxmlformats-officedocument.wordprocessingml.document")),
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["errors"] == [], f"合法内容被误伤：{body['errors']}"
    assert {i["filename"] for i in body["items"]} == {"正常说明.md", "正常文档.pdf", "正常文档.docx"}


# ---------------------------------------------------------------- 上传新版本
def test_new_version_rejects_disguised_binary(app_client, admin_headers, kb):
    up = app_client.post(f"/api/kbs/{kb['id']}/documents", headers=admin_headers,
                         files=[("files", ("版本替换.md", MD.encode(), "text/markdown"))])
    assert up.status_code == 200, up.text
    doc_id = up.json()["items"][0]["doc_id"]

    bad = app_client.post(f"/api/documents/{doc_id}/versions", headers=admin_headers,
                          files={"file": ("v2.pdf", b"MZ\x90\x00\x03", "application/pdf")})
    assert bad.status_code == 400, bad.text
    assert "扩展名不符" in bad.json()["detail"]

    # 版本号不应因为被拒绝而推进：仍停在 v1
    detail = app_client.get(f"/api/documents/{doc_id}", headers=admin_headers).json()
    assert [v["version_no"] for v in detail["versions"]] == [1]
