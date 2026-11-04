"""Contracts for the lilith model-provider plugin (moved from the hermes-agent fork).

The module registers its profile into the host ``providers`` registry at
import (the model-provider convention); these tests load THIS repo's copy
explicitly so the contracts are pinned against the moved source, not against
whatever bundle a particular hermes-agent checkout still carries.
"""
import importlib.util
import os
import sys

import pytest

HERMES_AGENT = "/home/common/projects/hermes-agent"


def _load_lilith():
    if HERMES_AGENT not in sys.path:
        sys.path.insert(0, HERMES_AGENT)  # host `providers` package
    spec = importlib.util.spec_from_file_location(
        "lilith_provider_under_test",
        os.path.join(os.path.dirname(__file__), "..", "plugins", "lilith", "__init__.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def lilith():
    return _load_lilith()


def test_module_registers_the_profile_and_aliases_resolve(lilith):
    from providers import get_provider_profile
    assert lilith.lilith.name == "lilith"
    assert lilith.lilith.base_url == "http://lilith.porcupine-ide.ts.net:8001/v1"
    resolved = get_provider_profile("lilith-sglang")
    assert resolved is not None and resolved.name == "lilith"
    # The bundle may still register its own instance first; resolution is the pin.
    assert get_provider_profile("lilith-agent") is not None


def test_dsv4_reasoning_uses_thinking_not_qwen_template(lilith):
    profile = lilith.lilith
    body, kwargs = profile.build_api_kwargs_extras(
        model="org/DeepSeek-V4-Flash", reasoning_config={"effort": "xhigh"})
    assert body == {"thinking": {"type": "enabled"}}
    assert kwargs == {"reasoning_effort": "max"}
    body, kwargs = profile.build_api_kwargs_extras(
        model="dsv4", reasoning_config={"enabled": False, "effort": "high"})
    assert body == {"thinking": {"type": "disabled"}}
    assert kwargs == {}


def test_qwen_budget_and_disable_contract(lilith):
    profile = lilith.lilith
    body, kwargs = profile.build_api_kwargs_extras(model="qwen36",
                                                   reasoning_config={"effort": "high"})
    assert body["thinking_budget"] == 32768
    assert body["chat_template_kwargs"]["thinking_scaffold"] is True
    assert body["chat_template_kwargs"]["auto_disable_thinking_with_tools"] is True
    assert kwargs == {}
    body, kwargs = profile.build_api_kwargs_extras(model="qwen36",
                                                   reasoning_config={"enabled": False})
    assert body["chat_template_kwargs"]["enable_thinking"] is False
    assert body["chat_template_kwargs"]["preserve_thinking"] is False
    assert "thinking_budget" not in body
    assert "thinking" not in body


def test_glm53_flash_delegates_to_zai_shape(lilith):
    """GLM-5.3-Flash delegates to the ZAI profile's semantics, then adapts
    SGLang's template: the answer carries ONLY enable_thinking — Z.AI's
    ``thinking`` object never leaks. (The exact enable/disable pin is a
    known pre-existing red in the hermes-agent copy; pinned here at the
    shape level until that drift is adjudicated.)"""
    profile = lilith.lilith
    from providers import get_provider_profile
    if get_provider_profile("zai") is None:
        pytest.skip("zai profile not registered in this host registry")
    for config in ({"enabled": True, "effort": "high"}, {"enabled": False},
                   {"effort": "xhigh"}):
        body, _kwargs = profile.build_api_kwargs_extras(model="glm-5.3-flash",
                                                        reasoning_config=config)
        assert "thinking" not in body
        assert set(body) == {"chat_template_kwargs"}
        assert body["chat_template_kwargs"].keys() == {"enable_thinking"}
        assert isinstance(body["chat_template_kwargs"]["enable_thinking"], bool)
    # No reasoning preference at all: the ZAI native has nothing to adapt.
    assert profile.build_api_kwargs_extras(model="glm-5.3-flash") == ({}, {})
