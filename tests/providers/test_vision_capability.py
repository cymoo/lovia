"""supports_vision: host-aware default, explicit override, and the helper.

The default must be host-aware (not a hardcoded True) because ``ANTHROPIC_BASE_URL``
can point an ``AnthropicProvider`` at a non-Anthropic, possibly text-only gateway.
"""

from __future__ import annotations

from lovia.providers import provider_from_string, supports_vision
from lovia.providers.anthropic import AnthropicProvider
from lovia.providers.openai_chat import OpenAIChatProvider

OFFICIAL_OAI = "https://api.openai.com/v1"
OFFICIAL_ANT = "https://api.anthropic.com/v1"
DEEPSEEK = "https://api.deepseek.com"
DEEPSEEK_ANT = "https://api.deepseek.com/anthropic"


def test_openai_defaults_true_on_official_false_on_compat() -> None:
    assert OpenAIChatProvider("m", base_url=OFFICIAL_OAI).supports_vision is True
    assert OpenAIChatProvider("m", base_url=DEEPSEEK).supports_vision is False


def test_openai_explicit_override_wins_either_direction() -> None:
    off = OpenAIChatProvider("m", base_url=DEEPSEEK, supports_vision=True)
    on = OpenAIChatProvider("m", base_url=OFFICIAL_OAI, supports_vision=False)
    assert off.supports_vision is True
    assert on.supports_vision is False


def test_anthropic_host_aware_default_covers_compat_gateways() -> None:
    # Real Anthropic host is multimodal; a deepseek /anthropic gateway may not be.
    assert AnthropicProvider("m", base_url=OFFICIAL_ANT).supports_vision is True
    assert AnthropicProvider("m", base_url=DEEPSEEK_ANT).supports_vision is False
    forced = AnthropicProvider("m", base_url=DEEPSEEK_ANT, supports_vision=True)
    assert forced.supports_vision is True


def test_helper_reads_flag_and_defaults_false_for_unaware_objects() -> None:
    assert supports_vision(OpenAIChatProvider("m", base_url=OFFICIAL_OAI)) is True
    assert supports_vision(OpenAIChatProvider("m", base_url=DEEPSEEK)) is False
    assert supports_vision(object()) is False
    assert supports_vision(None) is False


def test_provider_from_string_threads_supports_vision() -> None:
    bare = provider_from_string("qwen-vl", base_url=DEEPSEEK, supports_vision=True)
    vendor = provider_from_string("openai:qwen-vl", base_url=DEEPSEEK, supports_vision=True)
    anth = provider_from_string("anthropic:claude", base_url=DEEPSEEK_ANT)
    assert bare.supports_vision is True
    assert vendor.supports_vision is True
    assert anth.supports_vision is False  # host-aware default, no override given
