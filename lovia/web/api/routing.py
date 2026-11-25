"""The API's route class: the auth guard runs before the body is read."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

try:
    from fastapi import Request, Response
    from fastapi.routing import APIRoute
except ImportError as exc:  # pragma: no cover - depends on optional env
    from .._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)


class GuardedRoute(APIRoute):
    """A route whose ``dependencies=`` pass before its body is read.

    FastAPI reads and parses a declared body first, so a request the guard
    refuses would be buffered in full. Here a body-less route runs the
    dependencies, and its endpoint answers with the route's own handler; the
    route itself stays declared whole, for the OpenAPI schema. Every API
    router takes this as its ``route_class``.
    """

    def __init__(self, path: str, endpoint: Callable[..., Any], **kwargs: Any) -> None:
        self._declared = path, endpoint, kwargs
        super().__init__(path, endpoint, **kwargs)  # calls get_route_handler

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        if self.body_field is None or not self.dependencies:
            return super().get_route_handler()
        path, endpoint, kwargs = self._declared
        unguarded = APIRoute(path, endpoint, **{**kwargs, "dependencies": None})
        answer = unguarded.get_route_handler()

        async def guarded(request: Request, response: Response) -> Response:
            answered = await answer(request)
            # What a dependency set (a cookie, a status): FastAPI adds it to an
            # endpoint's value, never to a Response it returns.
            answered.headers.raw.extend(response.headers.raw)
            if response.status_code:
                answered.status_code = response.status_code
            return answered

        return APIRoute(
            path,
            guarded,
            dependencies=self.dependencies,
            dependency_overrides_provider=self.dependency_overrides_provider,
            response_model=None,
        ).get_route_handler()
