"""supports_vision: models see unless told otherwise.

Neither the host nor the model id says which models take images, so nothing
is guessed: a provider sees unless it was built with ``supports_vision=False``.
"""

from __future__ import annotations

from lovia.providers import provider_from_string, supports_vision
from lovia.providers.anthropic import AnthropicProvider
from lovia.providers.openai_chat import OpenAIChatProvider

OFFICIAL_OAI = "https://api.openai.com/v1"
OFFICIAL_ANT = "https://api.anthropic.com/v1"
DEEPSEEK = "https://api.deepseek.com"
DEEPSEEK_ANT = "https://api.deepseek.com/anthropic"


def test_every_endpoint_sees_by_default() -> None:
    assert OpenAIChatProvider("m", base_url=OFFICIAL_OAI).supports_vision is True
    assert OpenAIChatProvider("m", base_url=DEEPSEEK).supports_vision is True
    assert AnthropicProvider("m", base_url=OFFICIAL_ANT).supports_vision is True
    assert AnthropicProvider("m", base_url=DEEPSEEK_ANT).supports_vision is True


def test_false_is_the_only_way_not_to() -> None:
    oai = OpenAIChatProvider("m", base_url=OFFICIAL_OAI, supports_vision=False)
    ant = AnthropicProvider("m", base_url=OFFICIAL_ANT, supports_vision=False)
    assert oai.supports_vision is False
    assert ant.supports_vision is False


def test_helper_reads_flag_and_defaults_true_for_unaware_objects() -> None:
    off = OpenAIChatProvider("m", base_url=DEEPSEEK, supports_vision=False)
    assert supports_vision(off) is False
    assert supports_vision(OpenAIChatProvider("m", base_url=DEEPSEEK)) is True
    # A third-party provider that says nothing is taken to see.
    assert supports_vision(object()) is True


def test_provider_from_string_threads_supports_vision() -> None:
    bare = provider_from_string("m", base_url=DEEPSEEK, supports_vision=False)
    vendor = provider_from_string("openai:m", base_url=DEEPSEEK, supports_vision=False)
    anth = provider_from_string("anthropic:claude", base_url=DEEPSEEK_ANT)
    assert bare.supports_vision is False
    assert vendor.supports_vision is False
    assert anth.supports_vision is True  # nothing said: it sees
