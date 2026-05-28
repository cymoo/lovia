"""LocalSandboxProvider lifecycle: refcount, reuse, shutdown."""

from __future__ import annotations

from pathlib import Path

from lovia.sandbox import (
    LocalSandbox,
    LocalSandboxProvider,
    single_sandbox_provider,
)


async def test_provider_acquire_creates_keyed_sandbox(tmp_path: Path) -> None:
    p = LocalSandboxProvider(root_base=tmp_path)
    sb = await p.acquire("s1")
    assert sb is not None
    assert "s1" in sb.id
    await p.shutdown()


async def test_provider_acquire_reuse(tmp_path: Path) -> None:
    p = LocalSandboxProvider(root_base=tmp_path)
    a = await p.acquire("s1")
    b = await p.acquire("s1")
    assert a is b
    await p.shutdown()


async def test_provider_get(tmp_path: Path) -> None:
    p = LocalSandboxProvider(root_base=tmp_path)
    assert await p.get("missing") is None
    await p.acquire("s1")
    sb = await p.get("s1")
    assert sb is not None
    await p.shutdown()


async def test_provider_refcount_release(tmp_path: Path) -> None:
    p = LocalSandboxProvider(root_base=tmp_path)
    sb1 = await p.acquire("s1")
    sb2 = await p.acquire("s1")
    assert sb1 is sb2
    await p.release("s1")
    # still alive: one ref left
    assert await p.get("s1") is sb1
    await p.release("s1")
    # gone
    assert await p.get("s1") is None


async def test_provider_release_unknown_key(tmp_path: Path) -> None:
    p = LocalSandboxProvider(root_base=tmp_path)
    await p.release("never")  # no-op, no error


async def test_provider_shutdown_closes_all(tmp_path: Path) -> None:
    p = LocalSandboxProvider(root_base=tmp_path)
    s1 = await p.acquire("s1")
    s2 = await p.acquire("s2")
    await p.shutdown()
    assert s1._closed is True  # type: ignore[attr-defined]
    assert s2._closed is True  # type: ignore[attr-defined]
    assert await p.get("s1") is None


async def test_provider_session_context(tmp_path: Path) -> None:
    p = LocalSandboxProvider(root_base=tmp_path)
    async with p.session("s1") as sb:
        await sb.write("x", "1")
    # refcount dropped, sandbox released
    assert await p.get("s1") is None
    await p.shutdown()


async def test_provider_isolation_per_key(tmp_path: Path) -> None:
    p = LocalSandboxProvider(root_base=tmp_path)
    a = await p.acquire("s1")
    b = await p.acquire("s2")
    await a.write("file", "from-a")
    await b.write("file", "from-b")
    assert (await a.read("file")) == b"from-a"
    assert (await b.read("file")) == b"from-b"
    await p.shutdown()


async def test_provider_async_context(tmp_path: Path) -> None:
    async with LocalSandboxProvider(root_base=tmp_path) as p:
        await p.acquire("s1")
    # shutdown ran
    assert await p.get("s1") is None


async def test_single_sandbox_provider(tmp_path: Path) -> None:
    sb = LocalSandbox(root=tmp_path)
    p = single_sandbox_provider(sb)
    assert await p.acquire("anything") is sb
    assert await p.acquire("else") is sb
    await p.release("x")  # no-op
    assert await p.get("y") is sb
    await p.shutdown()
    assert sb._closed is True  # type: ignore[attr-defined]


async def test_provider_key_sanitization(tmp_path: Path) -> None:
    p = LocalSandboxProvider(root_base=tmp_path)
    sb = await p.acquire("../weird/key")
    # root must remain under tmp_path
    assert str(sb._root).startswith(str(tmp_path.resolve()))  # type: ignore[attr-defined]
    await p.shutdown()
