"""Lilith local SGLang provider profile.

Lilith can serve Qwen 3.x, DeepSeek V4-derived models, and GLM-5.3-Flash
through an OpenAI-compatible API. The request contract is selected from the
active model instead of applying Qwen chat-template fields to every model.

For Qwen, SGLang does not reliably infer thinking behavior from OpenAI's
top-level reasoning_effort, so this profile translates Hermes' reasoning
config into the Qwen/SGLang request fields that actually matter:

- extra_body.chat_template_kwargs.enable_thinking
- extra_body.chat_template_kwargs.preserve_thinking
- extra_body.chat_template_kwargs.thinking_scaffold for high/xhigh
- extra_body.thinking_budget
- extra_body.separate_reasoning
"""

from __future__ import annotations

from typing import Any

from providers import get_provider_profile, register_provider
from providers.base import ProviderProfile


_THINKING_BUDGET_BY_EFFORT = {
    "minimal": 2048,
    "low": 4096,
    "medium": 8192,
    "high": 32768,
    "xhigh": 65536,
}


def _is_deepseek_v4_model(model: str | None) -> bool:
    normalized = str(model or "").strip().lower().rsplit("/", 1)[-1]
    return (
        normalized == "dsv4"
        or normalized.startswith("dsv4-")
        or normalized.startswith("deepseek-v4")
    )


def _deepseek_v4_reasoning_kwargs(
    reasoning_config: dict | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    enabled = not (
        isinstance(reasoning_config, dict)
        and reasoning_config.get("enabled") is False
    )
    extra_body = {
        "thinking": {"type": "enabled" if enabled else "disabled"}
    }
    if not enabled or not isinstance(reasoning_config, dict):
        return extra_body, {}

    effort = str(reasoning_config.get("effort") or "").strip().lower()
    if effort in {"xhigh", "max", "ultra"}:
        return extra_body, {"reasoning_effort": "max"}
    if effort in {"low", "medium", "high"}:
        return extra_body, {"reasoning_effort": effort}
    return extra_body, {}


class LilithProviderProfile(ProviderProfile):
    """Provider profile for the local Lilith SGLang endpoint."""

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        model: str | None = None,
        **context: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        normalized = str(model or "").strip().lower().rsplit("/", 1)[-1]
        if normalized == "glm-5.3-flash":
            native = get_provider_profile("zai")
            if native is None:
                raise RuntimeError("Lilith GLM-5.3-Flash requires the ZAI provider profile")
            extra_body, top_level = native.build_api_kwargs_extras(
                reasoning_config=reasoning_config, model=normalized, **context,
            )
            # SGLang's GLM template accepts enable_thinking, not Z.AI's thinking object.
            thinking = extra_body.pop("thinking", None)
            if thinking is not None:
                extra_body["chat_template_kwargs"] = {
                    "enable_thinking": thinking["type"] == "enabled",
                }
            return extra_body, top_level

        if _is_deepseek_v4_model(model):
            return _deepseek_v4_reasoning_kwargs(reasoning_config)

        enabled = True
        effort = "medium"
        if isinstance(reasoning_config, dict):
            if reasoning_config.get("enabled") is False:
                enabled = False
            raw_effort = str(reasoning_config.get("effort") or "").strip().lower()
            if raw_effort in _THINKING_BUDGET_BY_EFFORT:
                effort = raw_effort

        if not enabled:
            return {
                "chat_template_kwargs": {
                    "auto_disable_thinking_with_tools": True,
                    "enable_thinking": False,
                    "preserve_thinking": False,
                    "thinking_scaffold": False,
                    "reasoning_effort": "none",
                },
                "separate_reasoning": True,
            }, {}

        budget = _THINKING_BUDGET_BY_EFFORT.get(effort, _THINKING_BUDGET_BY_EFFORT["medium"])
        scaffold = effort in {"high", "xhigh"}
        return {
            "chat_template_kwargs": {
                # The current Qwen3.6-derived SGLang route can return raw
                # <tool_call> XML when thinking and tools are both active.
                # Let the server disable scratchpad generation on tool turns
                # so Hermes receives structured OpenAI tool_calls instead.
                "auto_disable_thinking_with_tools": True,
                "enable_thinking": True,
                "preserve_thinking": True,
                "thinking_scaffold": scaffold,
                "reasoning_effort": effort,
            },
            "thinking_budget": budget,
            "separate_reasoning": True,
        }, {}


lilith = LilithProviderProfile(
    name="lilith",
    aliases=(
        "lilith-agent",
        "lilith-sglang",
        "qwen36-lilith",
    ),
    display_name="Lilith SGLang",
    description="Lilith local SGLang endpoint with model-aware reasoning controls",
    env_vars=(
        "LILITH_API_KEY",
        "LILITH_BASE_URL",
    ),
    base_url="http://lilith.porcupine-ide.ts.net:8001/v1",
    auth_type="api_key",
    default_max_tokens=81920,
    fallback_models=(
        "dsv4",
        "qwen36",
        "qwen-aeon-fp8-mtp",
        "qwen-3.6",
        "qwen3.6",
    ),
)

register_provider(lilith)
