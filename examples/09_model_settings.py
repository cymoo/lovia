"""Model choice and sampling settings.

``model=`` accepts a ``"vendor:model"`` string, a constructed provider
instance, or a list of either for fallback (see 14_reliability.py).
:class:`ModelSettings` carries the portable sampling knobs; vendor-only
options ride in ``provider_options`` under the adapter's key, so a fallback
chain never leaks one vendor's knobs into another's payload.

Any OpenAI-compatible service (DeepSeek, Ollama, vLLM, an internal
gateway) works through ``OpenAIChatProvider(base_url=...)`` — or simply set
``OPENAI_BASE_URL`` in the environment and keep using ``"openai:<model>"``
strings.

Run::

    python examples/09_model_settings.py
"""

from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv

from lovia import Agent, ModelSettings, OpenAIChatProvider, Runner, model_from_env

load_dotenv()
MODEL = model_from_env()  # LOVIA_MODEL etc.; raises with a hint if unset

QUESTION = "Name a paint colour for a tiny reading nook."


async def main() -> None:
    # 1. Portable sampling knobs live on the agent.
    precise = Agent(
        name="Precise",
        instructions="Answer in one short sentence.",
        model=MODEL,
        settings=ModelSettings(temperature=0.1, max_tokens=200),
    )
    # clone() derives a variant without touching the original.
    creative = precise.clone(settings=ModelSettings(temperature=1.2, max_tokens=200))

    print("precise: ", (await Runner.run(precise, QUESTION)).output)
    print("creative:", (await Runner.run(creative, QUESTION)).output)

    # 2. Vendor-only knobs go under provider_options, keyed by adapter name
    #    ("openai", "anthropic", ...). Keys pass through to the wire payload;
    #    a None value strips an adapter default the endpoint rejects.
    tuned = precise.clone(
        settings=ModelSettings(
            temperature=0.1,
            provider_options={"openai": {"presence_penalty": 0.6}},
        )
    )
    print("tuned:   ", (await Runner.run(tuned, QUESTION)).output)

    # 3. A provider *instance* instead of a string — point it at any
    #    OpenAI-compatible endpoint (DeepSeek, Ollama, vLLM, ...).
    base_url = os.environ.get("OPENAI_BASE_URL")
    api_key = os.environ.get("OPENAI_API_KEY")
    if base_url and api_key:
        provider = OpenAIChatProvider(
            model=os.environ.get("OPENAI_DEFAULT_MODEL", "glm-5.2"),
            api_key=api_key,
            base_url=base_url,
        )
        compat = Agent(
            name="Compat", instructions="Answer in one sentence.", model=provider
        )
        print("compat:  ", (await Runner.run(compat, QUESTION)).output)
    else:
        print("compat:   (set OPENAI_BASE_URL + OPENAI_API_KEY to try section 3)")


if __name__ == "__main__":
    asyncio.run(main())
