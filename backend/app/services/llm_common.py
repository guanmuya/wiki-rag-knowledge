"""模型调用层的共享基础：异常、错误文案与 URL 拼接。

单独成模块是为了让 `llm_protocols.py`（协议适配）能引用 `ModelUnavailable`
而不必反向依赖门面 `llm.py`——这是「无循环依赖」的落点。本模块只依赖
标准库与 httpx，不导入 `llm.py`，也不导入任何业务服务。
"""
from __future__ import annotations

import json
import re

import httpx

DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)


class ModelUnavailable(RuntimeError):
    """没有可用模型配置，或供应商调用失败。"""


class ModelNotConfigured(ModelUnavailable):
    """一条可用的模型配置都没有。

    单独成类是为了把「没配模型」和「配了但调用失败」分开：前者回答里的
    离线提示不应该写成「生成模型调用失败」，否则用户会以为自己配错了。
    """


# ------------------------------------------------------------------ 错误文案
_ERROR_BODY_RE = re.compile(r"^供应商返回 (\d{3}):\s*(\{.*\})$", re.S)


def public_error(exc: ModelUnavailable) -> str:
    """向普通用户返回错误类别，避免暴露供应商地址、凭据或响应正文。"""
    if isinstance(exc, ModelNotConfigured):
        return "未配置可用模型"
    text = str(exc)
    status = re.match(r"供应商返回 (\d{3})", text)
    if status:
        return f"供应商返回 {status.group(1)}"
    if text.startswith("供应商接口地址"):
        return "供应商接口地址不合法"
    if text.startswith("网络请求失败"):
        return "无法连接供应商"
    return "模型调用失败"


def error_summary(exc: BaseException | str, limit: int = 200) -> str:
    """把供应商异常压成一句可展示的原因：只取错误体里的 message 并截断，
    完整原文仍保留在 `CallStats.notes`，排障以 notes 为准。"""
    text = re.sub(r"\s+", " ", str(exc or "")).strip()
    match = _ERROR_BODY_RE.match(text)
    if match:
        try:
            data = json.loads(match.group(2))
        except (TypeError, ValueError):
            data = None
        message = ""
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or error.get("msg") or "")
            elif isinstance(error, str):
                message = error
            if not message:
                message = str(data.get("message") or data.get("detail") or "")
        if message.strip():
            text = f"供应商返回 {match.group(1)}：{message.strip()}"
    if not text or len(text) <= limit:
        return text
    # 优先在句末/分号处截断：供应商常在错误信息后面附带升级链接，
    # 把 URL 切一半比少说一句更难读。完整原文仍在 notes 里。
    window = text[:limit]
    for sep in ("。", ". ", "；", "; "):
        pos = window.rfind(sep)
        if pos >= max(20, limit // 3):
            return window[: pos + len(sep)].rstrip()
    return window[: limit - 1] + "…"


# ------------------------------------------------------------------ URL 拼接
def _join(base: str, path: str) -> str:
    """拼接 OpenAI 兼容端点。兼容 base_url 带/不带 /v1，以及误带端口方括号的写法。"""
    base = (base or "").strip().rstrip("/")
    if not base:
        raise ModelUnavailable("供应商接口地址为空")
    if not re.match(r"^https?://", base, re.I):
        raise ModelUnavailable(f"供应商接口地址需以 http:// 或 https:// 开头：{base}")
    # Gemini 的 OpenAI 兼容地址以 /v1beta/openai 结尾，已经包含 API 根路径。
    # 其他无版本号的旧地址仍沿用补 /v1 的规则，避免改变已有供应商配置。
    if re.search(r"/(?:v\d+|v\d+beta/openai)$", base, re.I):
        return f"{base}/{path.lstrip('/')}"
    return f"{base}/v1/{path.lstrip('/')}"
