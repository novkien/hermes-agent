"""Config-driven reasoning_content preservation for custom-provider models."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import agent.agent_runtime_helpers as helpers
import hermes_cli.config as hermes_config


ROUTER_URL = "http://9router.local:8080/v1"
MODEL = "qwen-agent"


def _config(preserve=True):
    model_cfg = {}
    if preserve is not None:
        model_cfg["preserve_thinking"] = preserve
    return {
        "providers": {
            "9router": {
                "api": ROUTER_URL,
                "models": {
                    MODEL: model_cfg,
                    "strict-alias": {},
                },
            }
        }
    }


def _stub_config(monkeypatch, preserve=True):
    monkeypatch.setattr(
        hermes_config,
        "load_config_readonly",
        lambda: _config(preserve),
    )


def _agent(*, model=MODEL, require=False):
    return SimpleNamespace(
        provider="custom",
        requested_provider="custom:9router",
        model=model,
        base_url=ROUTER_URL,
        _needs_thinking_reasoning_pad=lambda: require,
    )


def test_preserve_true_keeps_explicit_reasoning_content_verbatim(monkeypatch):
    _stub_config(monkeypatch, True)
    source = {
        "role": "assistant",
        "content": "",
        "reasoning_content": "think bytes\n  stay exactly here",
        "reasoning": "trajectory copy",
    }
    api_msg = dict(source)

    helpers.copy_reasoning_content_for_api(_agent(), source, api_msg)

    assert api_msg["reasoning_content"] == "think bytes\n  stay exactly here"


def test_preserve_true_does_not_fabricate_or_promote_reasoning(monkeypatch):
    _stub_config(monkeypatch, True)
    source = {
        "role": "assistant",
        "content": "visible",
        "reasoning": "internal trajectory reasoning",
    }
    api_msg = dict(source)

    helpers.copy_reasoning_content_for_api(_agent(), source, api_msg)

    assert "reasoning_content" not in api_msg


@pytest.mark.parametrize("flag", [False, None])
def test_false_or_absent_preserve_flag_keeps_existing_strip_behavior(monkeypatch, flag):
    _stub_config(monkeypatch, flag)
    source = {
        "role": "assistant",
        "reasoning_content": "must not leak",
    }
    api_msg = dict(source)

    helpers.copy_reasoning_content_for_api(_agent(), source, api_msg)

    assert "reasoning_content" not in api_msg


def test_exact_model_alias_controls_preservation(monkeypatch):
    _stub_config(monkeypatch, True)
    source = {
        "role": "assistant",
        "reasoning_content": "only qwen-agent opted in",
    }
    api_msg = dict(source)

    helpers.copy_reasoning_content_for_api(
        _agent(model="strict-alias"), source, api_msg
    )

    assert "reasoning_content" not in api_msg


def test_require_side_takes_precedence_and_keeps_padding_semantics(monkeypatch):
    _stub_config(monkeypatch, True)
    source = {
        "role": "assistant",
        "reasoning": "private reasoning from another provider",
        "tool_calls": [{"id": "call_1", "function": {"name": "terminal"}}],
    }
    api_msg = dict(source)

    helpers.copy_reasoning_content_for_api(
        _agent(require=True), source, api_msg
    )

    assert api_msg["reasoning_content"] == " "


def test_fallback_to_non_preserve_model_strips_stale_reasoning(monkeypatch):
    _stub_config(monkeypatch, True)
    agent = _agent()
    source = {
        "role": "assistant",
        "reasoning_content": "qwen tool-loop thought",
    }
    api_msg = dict(source)
    helpers.copy_reasoning_content_for_api(agent, source, api_msg)
    assert api_msg["reasoning_content"] == "qwen tool-loop thought"

    agent.model = "strict-alias"
    changed = helpers.reapply_reasoning_echo_for_provider(agent, [api_msg])

    assert changed == 1
    assert "reasoning_content" not in api_msg


def test_reapply_on_preserve_destination_leaves_reasoning_untouched(monkeypatch):
    _stub_config(monkeypatch, True)
    api_msg = {
        "role": "assistant",
        "reasoning_content": "same bytes",
    }

    changed = helpers.reapply_reasoning_echo_for_provider(
        _agent(), [api_msg]
    )

    assert changed == 0
    assert api_msg["reasoning_content"] == "same bytes"
