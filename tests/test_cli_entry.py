"""Tests for the top-level ``lovia`` console entry point."""

from __future__ import annotations

import pytest

from lovia import __version__, cli


def test_no_args_prints_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert "usage: lovia" in out
    assert "web" in out


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_flag(flag: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([flag]) == 0
    assert "usage: lovia" in capsys.readouterr().out


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"lovia {__version__}"


def test_unknown_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["serve"]) == 2
    err = capsys.readouterr().err
    assert "unknown command 'serve'" in err
    assert "usage: lovia" in err


def test_web_forwards_args_and_prog(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("fastapi")
    import lovia.web.__main__ as web_main

    captured: dict[str, object] = {}

    def fake_main(argv: list[str], *, prog: str | None = None) -> int:
        captured["argv"] = argv
        captured["prog"] = prog
        return 0

    monkeypatch.setattr(web_main, "main", fake_main)
    assert cli.main(["web", "--port", "9000"]) == 0
    assert captured == {"argv": ["--port", "9000"], "prog": "lovia web"}


def test_web_version_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    pytest.importorskip("fastapi")
    # argparse's --version action raises SystemExit(0) from within web main.
    with pytest.raises(SystemExit) as exc:
        cli.main(["web", "--version"])
    assert exc.value.code == 0
    assert "lovia" in capsys.readouterr().out
