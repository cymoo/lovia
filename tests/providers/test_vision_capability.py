"""supports_vision: every model is taken to see images unless declared not to.

A model that turns out to refuse images costs one refused call per chat; the
run loop then withholds them (tests/runtime/test_image_recovery.py).
"""

from __future__ import annotations

from lovia.providers import provider_from_string, supports_vision
from lovia.providers.anthropic import AnthropicProvider
from lovia.providers.openai_chat import OpenAIChatProvider

OFFICIAL_OAI = "https://api.openai.com/v1"
DEEPSEEK = "https://api.deepseek.com"
DEEPSEEK_ANT = "https://api.deepseek.com/anthropic"


def test_every_host_sees_unless_declared_not_to() -> None:
    assert OpenAIChatProvider("m", base_url=OFFICIAL_OAI).supports_vision is True
    assert OpenAIChatProvider("m", base_url=DEEPSEEK).supports_vision is True
    assert AnthropicProvider("m", base_url=DEEPSEEK_ANT).supports_vision is True
    off = OpenAIChatProvider("m", base_url=OFFICIAL_OAI, supports_vision=False)
    assert off.supports_vision is False
    assert AnthropicProvider("m", supports_vision=False).supports_vision is False


def test_helper_reads_flag_and_defaults_true_for_unaware_objects() -> None:
    assert supports_vision(OpenAIChatProvider("m", base_url=DEEPSEEK)) is True
    assert supports_vision(OpenAIChatProvider("m", supports_vision=False)) is False
    assert supports_vision(object()) is True


def test_provider_from_string_threads_supports_vision() -> None:
    off = provider_from_string("qwen", base_url=DEEPSEEK, supports_vision=False)
    vendor = provider_from_string("openai:qwen-vl", base_url=DEEPSEEK)
    anth = provider_from_string("anthropic:claude", base_url=DEEPSEEK_ANT)
    assert off.supports_vision is False
    assert vendor.supports_vision is True
    assert anth.supports_vision is True
