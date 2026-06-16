"""Lightweight tracing for runs, turns, tool calls, and handoffs.

The :class:`Tracer` Protocol is intentionally tiny: a single ``span`` context
manager that returns a :class:`Span` handle. Anything fancier (sampling,
span links, custom exporters) belongs in a real backend (OpenTelemetry,
Logfire, ...) — wire one of those up by writing your own ``Tracer`` adapter.

Out of the box you get three implementations:

* :class:`NoopTracer` — drops everything. Used when ``Agent.tracer`` is
  ``None`` so the runner never has to ``if tracer is not None``.
* :class:`ConsoleTracer` — prints an indented tree to a ``logging`` logger.
  Useful for quick demos and local debugging; **not** intended for
  production. It honours ``min_level`` so you can filter noisy spans.
* :class:`InMemoryTracer` — records spans in a list. Useful in tests.
"""

from __future__ import annotations

import contextvars
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import ContextManager, Iterator, Protocol

# Per-task indent depth, used by ConsoleTracer to render nesting. A contextvar
# is the right tool because runs may be interleaved in the same event loop.
_depth: contextvars.ContextVar[int] = contextvars.ContextVar(
    "lovia_trace_depth", default=0
)


class Span(Protocol):
    """A live span. Methods are best-effort — adapters may no-op some of them."""

    # TODO: 只有set_attribute太简单了
    # TODO: 是否需要预定义一些可以span的东西，好处：类型安全 & 通过此文件就知道哪些值得span & 易维护
    # TODO: 优先级较低，待主要功能完成
    def set_attribute(self, key: str, value: object) -> None: ...
    def record_exception(self, exc: BaseException) -> None: ...


class Tracer(Protocol):
    """Produces :class:`Span` instances inside a context manager."""

    def span(self, name: str, /, **attributes: object) -> "ContextManager[Span]":
        """Open a span named ``name``. Use as ``with tracer.span(...) as s:``."""
        ...


# ---------------------------------------------------------------------------
# Noop


class _NoopSpan:
    """A span that silently discards every attribute and exception."""

    def set_attribute(self, key: str, value: object) -> None:
        return None

    def record_exception(self, exc: BaseException) -> None:
        return None


class NoopTracer:
    """Drop-everything tracer. The runner's default."""

    _SPAN = _NoopSpan()

    @contextmanager
    def span(self, name: str, /, **attributes: object) -> Iterator[Span]:
        yield self._SPAN


# ---------------------------------------------------------------------------
# Console


@dataclass
class _ConsoleSpan:
    name: str
    attrs: dict[str, object]
    exception: BaseException | None = None

    def set_attribute(self, key: str, value: object) -> None:
        self.attrs[key] = value

    def record_exception(self, exc: BaseException) -> None:
        self.exception = exc


class ConsoleTracer:
    """Indented, human-readable span tree printed via the ``logging`` module.

    Configure verbosity with ``min_duration_ms`` (skip very short spans) or
    by adjusting the underlying logger (default ``lovia.trace``).
    """

    def __init__(
        self,
        *,
        logger: logging.Logger | None = None,
        min_duration_ms: float = 0.0,
    ) -> None:
        self.logger = logger or logging.getLogger("lovia.trace")
        self.min_duration_ms = min_duration_ms

    @contextmanager
    def span(self, name: str, /, **attributes: object) -> Iterator[Span]:
        span = _ConsoleSpan(name=name, attrs=dict(attributes))
        depth = _depth.get()
        token = _depth.set(depth + 1)
        start = time.perf_counter()
        try:
            yield span
        except BaseException as exc:
            span.record_exception(exc)
            raise
        finally:
            _depth.reset(token)
            elapsed_ms = (time.perf_counter() - start) * 1000
            if elapsed_ms < self.min_duration_ms and span.exception is None:
                pass
            else:
                prefix = "  " * depth
                attrs = " ".join(f"{k}={v!r}" for k, v in span.attrs.items())
                status = ""
                if span.exception is not None:
                    status = f" ! {type(span.exception).__name__}: {span.exception}"
                line = (
                    f"{prefix}{span.name} ({elapsed_ms:.1f}ms) {attrs}{status}".rstrip()
                )
                self.logger.info(line)


# ---------------------------------------------------------------------------
# In-memory (handy for tests)


@dataclass
class RecordedSpan:
    """A span captured by :class:`InMemoryTracer`."""

    name: str
    attrs: dict[str, object] = field(default_factory=dict)
    duration_ms: float = 0.0
    exception: BaseException | None = None

    def set_attribute(self, key: str, value: object) -> None:
        self.attrs[key] = value

    def record_exception(self, exc: BaseException) -> None:
        self.exception = exc


class InMemoryTracer:
    """Stores every span in :attr:`spans` for later assertions in tests."""

    def __init__(self) -> None:
        self.spans: list[RecordedSpan] = []

    @contextmanager
    def span(self, name: str, /, **attributes: object) -> Iterator[Span]:
        rec = RecordedSpan(name=name, attrs=dict(attributes))
        self.spans.append(rec)
        start = time.perf_counter()
        try:
            yield rec
        except BaseException as exc:
            rec.record_exception(exc)
            raise
        finally:
            rec.duration_ms = (time.perf_counter() - start) * 1000
