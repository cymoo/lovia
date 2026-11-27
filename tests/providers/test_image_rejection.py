"""Reading a provider's refusal of the images in its request
(lovia/providers/_images.py)."""

from __future__ import annotations

import httpx
import pytest

from lovia.exceptions import ContextOverflowError, ProviderError
from lovia.providers._http import raise_for_provider_status
from lovia.providers._images import image_rejection
from lovia.providers.openai_chat import _is_context_overflow


def err(body: str, status: int = 400) -> ProviderError:
    return ProviderError(f"HTTP {status}: {body}", status_code=status, body=body)


@pytest.mark.parametrize(
    "body",
    [
        # DeepSeek: the user content's image part is not a variant it knows.
        "Failed to deserialize the JSON body into the target type: messages[1]: "
        "unknown variant `image_url`, expected `text` at line 1 column 300",
        # OpenAI, a model without vision.
        "Invalid content type. image_url is only supported by certain models.",
        # vLLM serving a text model.
        "Qwen2.5-7B-Instruct is not a multimodal model",
        "This model does not support image input.",
        "Image input is not supported for this model",
    ],
)
def test_a_model_without_images_says_so(body: str) -> None:
    assert image_rejection(err(body)) == "unsupported"


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ("messages.0.content.1.image.source.base64: image exceeds 5 MB maximum", 400),
        ("image dimensions exceed max allowed size: 8000 pixels", 400),
        ("Too many images in request. Max is 20.", 400),
        ("Could not process image", 400),
        ("Invalid image data", 422),
        ("Request too large: image payload", 413),
        # A format is about this image, not the model.
        ("Image format is not supported", 400),
        ("This model does not support image/gif", 400),
    ],
)
def test_a_problem_with_these_images_is_not_a_fact_about_the_model(
    body: str, status: int
) -> None:
    assert image_rejection(err(body, status)) == "image"


@pytest.mark.parametrize(
    "exc",
    [
        err("prompt is too long"),  # no image in it
        err("rate limited on image requests", 429),
        err("image service unavailable", 503),
        ProviderError("image exceeds 5 MB"),  # no status: not an HTTP refusal
        ValueError("image"),
    ],
)
def test_other_errors_are_not_image_refusals(exc: BaseException) -> None:
    assert image_rejection(exc) is None


async def test_an_image_too_large_is_not_taken_for_a_context_overflow() -> None:
    request = httpx.Request("POST", "https://api.example/v1/chat/completions")

    async def status(code: int, text: str) -> BaseException:
        response = httpx.Response(code, text=text, request=request)
        with pytest.raises((ProviderError, ContextOverflowError)) as caught:
            await raise_for_provider_status(
                response,
                vendor="openai",
                model="m",
                label="OpenAI",
                is_context_overflow=_is_context_overflow,
            )
        return caught.value

    image = await status(413, "Request too large: image exceeds the maximum size")
    assert type(image) is ProviderError and image_rejection(image) == "image"
    # A plain oversized prompt still takes the overflow path, and so does one
    # that names the context window, images or not.
    plain = await status(413, "Request too large for model")
    assert isinstance(plain, ContextOverflowError)
    both = await status(
        400, "The combined text and image tokens exceed the model's context window"
    )
    assert isinstance(both, ContextOverflowError)
