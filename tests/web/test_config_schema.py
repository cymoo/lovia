"""Tests for the config schema and storage (``lovia.web.config``)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from pydantic import ValidationError  # noqa: E402

from lovia.exceptions import UserError  # noqa: E402
from lovia.web.config import (  # noqa: E402
    ANTHROPIC_FLAVOR,
    OPENAI_FLAVOR,
    Connection,
    ModelProfile,
    WebConfig,
    flavor_for_model,
    slugify,
    storage,
)

# ----------------------------------------------------------------- flavor -


@pytest.mark.parametrize(
    ("spec", "name"),
    [
        ("openai:gpt-5.5", "openai"),
        ("oai:x", "openai"),
        ("openai-chat:x", "openai"),
        ("anthropic:claude-sonnet-4-5", "anthropic"),
        ("claude:claude-sonnet-4-5", "anthropic"),
        ("deepseek-v4-pro", "openai"),
        ("somevendor:model", "openai"),
    ],
)
def test_flavor_for_model_mirrors_provider_routing(spec: str, name: str) -> None:
    assert flavor_for_model(spec).name == name


def test_flavors_reuse_provider_constants() -> None:
    assert OPENAI_FLAVOR.default_base_url == "https://api.openai.com/v1"
    assert ANTHROPIC_FLAVOR.default_base_url == "https://api.anthropic.com/v1"


def test_auth_headers_openai_and_anthropic() -> None:
    assert OPENAI_FLAVOR.auth_headers("sk-1") == {"Authorization": "Bearer sk-1"}
    assert OPENAI_FLAVOR.auth_headers(None) == {}
    anthropic = ANTHROPIC_FLAVOR.auth_headers("sk-2")
    assert anthropic["x-api-key"] == "sk-2"
    assert "anthropic-version" in anthropic
    assert "x-api-key" not in ANTHROPIC_FLAVOR.auth_headers(None)


# ---------------------------------------------------------------- profile -


def test_profile_flavor_follows_vendor_prefix() -> None:
    p = ModelProfile(id="a", model="anthropic:claude-x", flavor="openai")
    assert p.flavor == "anthropic"
    assert p.spec() == "anthropic:claude-x"


def test_profile_bare_name_uses_declared_flavor() -> None:
    p = ModelProfile(id="a", model="glm-4.6v", flavor="anthropic")
    assert p.spec() == "anthropic:glm-4.6v"
    assert ModelProfile(id="b", model="glm-4.6v").spec() == "glm-4.6v"


def test_profile_normalises_base_url() -> None:
    p = ModelProfile(id="a", model="m", base_url="https://gw.example/v1/")
    assert p.base_url == "https://gw.example/v1"
    assert ModelProfile(id="b", model="m", base_url="").base_url is None


def test_profile_rejects_bad_id_and_window() -> None:
    with pytest.raises(ValidationError):
        ModelProfile(id="Has Spaces", model="m")
    with pytest.raises(ValidationError):
        ModelProfile(id="a", model="m", context_window=0)


def test_profile_display_name_and_vision_override() -> None:
    p = ModelProfile(id="a", model="deepseek-v4-pro", name="DeepSeek")
    assert p.display_name == "DeepSeek"
    assert ModelProfile(id="b", model="m").display_name == "m"
    assert ModelProfile(id="c", model="m").vision_override() is None
    assert ModelProfile(id="d", model="m", vision="on").vision_override() is True
    assert ModelProfile(id="e", model="m", vision="off").vision_override() is False


def test_slugify() -> None:
    assert slugify("DeepSeek V4 Pro!") == "deepseek-v4-pro"
    assert slugify("anthropic:claude-x") == "anthropic-claude-x"
    assert slugify("x", {"x"}) == "x-2"
    assert slugify("x", {"x", "x-2"}) == "x-3"
    assert slugify("!!!") == "model"


# ----------------------------------------------------------------- config -


def _profile(pid: str, model: str = "m") -> dict:
    return {"id": pid, "model": model}


def test_config_rejects_duplicate_ids() -> None:
    with pytest.raises(ValidationError, match="duplicate model ids"):
        WebConfig.model_validate({"models": [_profile("a"), _profile("a")]})


def test_config_chat_role_heals_to_first_profile() -> None:
    cfg = WebConfig.model_validate({"models": [_profile("a"), _profile("b")]})
    assert cfg.roles.chat == "a"
    assert cfg.default_profile() is cfg.models[0]


def test_config_rejects_unknown_role_references() -> None:
    for role in ("chat", "vision", "aux"):
        with pytest.raises(ValidationError, match=f"roles.{role}"):
            WebConfig.model_validate(
                {"models": [_profile("a")], "roles": {role: "nope"}}
            )


def test_config_role_lookups() -> None:
    cfg = WebConfig.model_validate(
        {
            "models": [_profile("a"), _profile("b"), _profile("c")],
            "roles": {"chat": "b", "vision": "c", "aux": "a"},
        }
    )
    assert cfg.default_profile().id == "b"  # type: ignore[union-attr]
    assert cfg.vision_profile().id == "c"  # type: ignore[union-attr]
    assert cfg.aux_profile().id == "a"  # type: ignore[union-attr]
    assert cfg.profile("nope") is None
    assert WebConfig().default_profile() is None


def test_config_ignores_unknown_keys_for_forward_compat() -> None:
    cfg = WebConfig.model_validate(
        {"models": [dict(_profile("a"), future_field=1)], "future_section": {}}
    )
    assert cfg.models[0].id == "a"


# ------------------------------------------------------------- connection -


def test_connection_from_profile_fills_flavor_default_base_url() -> None:
    conn = Connection.from_profile(ModelProfile(id="a", model="openai:gpt-5.5"))
    assert conn.base_url == "https://api.openai.com/v1"
    anth = Connection.from_profile(
        ModelProfile(id="b", model="claude-x", flavor="anthropic")
    )
    assert anth.model == "anthropic:claude-x"
    assert anth.base_url == "https://api.anthropic.com/v1"


def test_connection_missing_model() -> None:
    assert Connection().missing() == ["model"]


def test_connection_missing_key_on_official_host() -> None:
    conn = Connection.from_profile(ModelProfile(id="a", model="openai:gpt-5.5"))
    assert conn.missing() == ["API key"]


def test_connection_keyless_gateway_is_complete() -> None:
    profile = ModelProfile(
        id="a", model="deepseek-v4-pro", base_url="http://localhost:11434/v1"
    )
    assert Connection.from_profile(profile).missing() == []


def test_connection_official_host_with_key_is_complete() -> None:
    profile = ModelProfile(id="a", model="openai:gpt-5.5", api_key="sk-x")
    assert Connection.from_profile(profile).missing() == []


# ---------------------------------------------------------------- storage -


def test_save_config_is_owner_only_and_gitignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = WebConfig(models=[ModelProfile(id="a", model="m", api_key="sk-secret")])
    path = storage.save_config(cfg, storage.project_config_path())
    assert path == storage.project_config_path()
    assert (path.parent / ".gitignore").read_text().strip().splitlines()[-1] == "*"
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600  # secret file: owner-only
        assert path.parent.stat().st_mode & 0o777 == 0o700  # dir: owner-only too
    assert not list(path.parent.glob("*.tmp"))  # the atomic temp file is gone


def test_save_config_omits_null_fields(tmp_path: Path) -> None:
    cfg = WebConfig(models=[ModelProfile(id="a", model="m")])
    path = storage.save_config(cfg, tmp_path / "config.json")
    payload = json.loads(path.read_text())
    assert "api_key" not in payload["models"][0]
    assert "base_url" not in payload["models"][0]
    assert payload["version"] == 1


def test_load_config_roundtrip(tmp_path: Path) -> None:
    cfg = WebConfig(
        models=[
            ModelProfile(
                id="a",
                model="deepseek-v4-pro",
                base_url="https://api.deepseek.com/",
                api_key="sk-x",
                context_window=128_000,
                vision="on",
            )
        ]
    )
    path = storage.save_config(cfg, tmp_path / "config.json")
    loaded = storage.load_config(path)
    assert loaded == cfg


def test_load_config_missing_is_none(tmp_path: Path) -> None:
    assert storage.load_config(tmp_path / "nope.json") is None


def test_load_config_broken_json_is_user_error(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{nope", encoding="utf-8")
    with pytest.raises(UserError, match="invalid JSON"):
        storage.load_config(path)


def test_load_config_invalid_schema_names_the_field(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"models": [{"id": "a", "model": "m", "context_window": 0}]}),
        encoding="utf-8",
    )
    with pytest.raises(UserError, match="context_window"):
        storage.load_config(path)


def test_load_active_project_beats_user(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_home: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    user = WebConfig(models=[ModelProfile(id="u", model="user-model")])
    storage.save_config(user, storage.user_config_path())
    project = WebConfig(models=[ModelProfile(id="p", model="project-model")])
    storage.save_config(project, storage.project_config_path())
    loaded = storage.load_active()
    assert loaded.exists
    assert loaded.label == storage.PROJECT_CONFIG_LABEL
    assert loaded.config.default_profile().model == "project-model"  # type: ignore[union-attr]


def test_load_active_falls_back_to_user_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_home: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    user = WebConfig(models=[ModelProfile(id="u", model="user-model")])
    storage.save_config(user, storage.user_config_path())
    loaded = storage.load_active()
    assert loaded.label == storage.USER_CONFIG_LABEL
    assert loaded.config.default_profile().model == "user-model"  # type: ignore[union-attr]


def test_load_active_empty_targets_user_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_home: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    loaded = storage.load_active()
    assert not loaded.exists
    assert loaded.config.models == []
    assert loaded.path == storage.user_config_path()
    assert loaded.label == storage.USER_CONFIG_LABEL


def test_load_active_warns_once_about_legacy_env_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_home: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.chdir(tmp_path)
    legacy = fake_home / ".lovia" / "config.env"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("LOVIA_MODEL=old\n", encoding="utf-8")
    with caplog.at_level("WARNING", logger="lovia.web.config"):
        storage.load_active()
    assert "no longer read" in caplog.text
    # Once config.json exists the nudge stops.
    storage.save_config(
        WebConfig(models=[ModelProfile(id="a", model="m")]), storage.user_config_path()
    )
    caplog.clear()
    with caplog.at_level("WARNING", logger="lovia.web.config"):
        storage.load_active()
    assert "no longer read" not in caplog.text
