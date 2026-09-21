"""LLM Provider 可换层（架构书宪.1 · 可换层清单第一项）。

宪法 C-3：「会变的做 Interface / Config」——核心代码出现 LLM 提供方的
**具体实现硬编码**即失败。因此这里只定义接口，任何真实提供方都从外部注入。

V1 内置两个实现：
- ``NullProvider``        —— 占位。未配置时 ``complete()`` 显式抛
  ``ProviderNotConfigured``，**绝不静默降级**（P4：失败显性化）。
- ``OpenAICompatibleProvider`` —— 面向 OpenAI 兼容端点（本机 OmniRoute 网关、
  各家中转站都是这个协议），配置走环境变量，key 由用户自己填，本模块不碰敏感文件。

代理说明：本机存在全局 HTTP 代理劫持回环的已知故障（见 readme「两个真实故障」），
因此这里默认**不读环境代理**（ProxyHandler({})）。真实外部 API 如需走代理，
由调用方在 Provider 之上再做一层包装，不在本模块里猜。
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Protocol, runtime_checkable

# 环境变量命名空间：TRACEABLE_LLM_*（不读用户全局变量，避免与别的工具打架）
ENV_BASE_URL = "TRACEABLE_LLM_BASE_URL"
ENV_API_KEY = "TRACEABLE_LLM_API_KEY"
ENV_MODEL = "TRACEABLE_LLM_MODEL"

#: 本机 OmniRoute 网关（用户已有部署，OpenAI 兼容格式）
DEFAULT_BASE_URL = "http://127.0.0.1:20128/v1"
DEFAULT_MODEL = "auto"


class ProviderNotConfigured(RuntimeError):
    """LLM Provider 未配置。显式失败，不是「算了不解释了」。"""


@runtime_checkable
class LLMProvider(Protocol):
    """一切 LLM 提供方必须实现的接口（宪.1 可换层的边界）。

    只做一件事：给一段 prompt，返回一段文本。**不负责数字正确性**——
    数字正确性由 claims_gate（D6）在另一层保证。
    """

    name: str

    def complete(self, prompt: str) -> str: ...


class NullProvider:
    """占位 Provider：未配置时明确报错，让上层知道「LLM 解释没开」。"""

    name = "null"

    def complete(self, prompt: str) -> str:
        raise ProviderNotConfigured(
            "LLM Provider 未配置。设置环境变量 "
            f"{ENV_BASE_URL} / {ENV_API_KEY} / {ENV_MODEL} 后重试，"
            "或显式接受「无 LLM 解释」的报告形态（这不等于静默降级）。"
        )


class OpenAICompatibleProvider:
    """OpenAI 兼容端点（chat/completions）。

    配置全部走环境变量（key 由用户手动填写，本模块不读取任何本地敏感文件）：
      TRACEABLE_LLM_BASE_URL  默认 http://127.0.0.1:20128/v1（本机 OmniRoute）
      TRACEABLE_LLM_API_KEY   可为空（本地网关常不需要）
      TRACEABLE_LLM_MODEL     默认 "auto"
    """

    name = "openai-compatible"

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: int = 90,
    ) -> None:
        self.base_url = (base_url or os.environ.get(ENV_BASE_URL) or DEFAULT_BASE_URL)
        self.api_key = api_key if api_key is not None else os.environ.get(ENV_API_KEY, "")
        self.model = model or os.environ.get(ENV_MODEL, DEFAULT_MODEL)
        self.timeout = timeout
        if not self.base_url:
            raise ProviderNotConfigured(f"{ENV_BASE_URL} 未设置，无法确定端点")

    def complete(self, prompt: str) -> str:
        # 不读环境代理（本机代理劫持回环是已知故障，见模块 docstring）
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        url = self.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,  # 解释是事实陈述，不是创作：确定性优先
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), headers=headers
        )
        with opener.open(req, timeout=self.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        try:
            return body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise ProviderNotConfigured(
                f"LLM 端点返回了意外结构（{type(e).__name__}）：{str(body)[:200]}"
            ) from e


def explain_status() -> dict:
    """查询当前 Provider 配置状态（供界面显示「LLM 解释开/关」）。"""
    base = os.environ.get(ENV_BASE_URL, DEFAULT_BASE_URL)
    key = os.environ.get(ENV_API_KEY, "")
    model = os.environ.get(ENV_MODEL, DEFAULT_MODEL)
    return {
        "configured": bool(base and (key or "127.0.0.1" in base)),
        "base_url": base,
        "model": model,
        "api_key_set": bool(key),
        "hint": "key 由用户自行填写环境变量，本模块不读取本地敏感文件",
    }


__all__ = [
    "LLMProvider", "NullProvider", "OpenAICompatibleProvider",
    "ProviderNotConfigured", "explain_status",
    "ENV_BASE_URL", "ENV_API_KEY", "ENV_MODEL",
]
