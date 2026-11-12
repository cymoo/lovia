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
    AgentConfig,
    Connection,
    ModelProfile,
    SkillsConfig,
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


@pytest.mark.parametrize("name", ["_Review", ".hidden", "-x-", "a" * 65, "é" * 80])
def test_slugify_makes_a_valid_id_from_any_name(name: str) -> None:
    taken: set[str] = set()
    for _ in range(3):  # the unique suffix fits too
        slug = slugify(name, taken)
        ModelProfile(id=slug, model="m")  # the stricter of the two id rules
        AgentConfig(id=slug.replace(".", "-"))
        assert len(slug) <= 64
        taken.add(slug)


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


# ----------------------------------------------------------------- skills -


def test_skills_defaults_to_the_global_convention() -> None:
    assert WebConfig().skills.dirs == ["~/.agents/skills"]
    # An older document without the key parses to the same default.
    old = WebConfig.model_validate({"version": 1, "models": []})
    assert old.skills.dirs == ["~/.agents/skills"]


def test_skills_saved_old_default_still_loads(tmp_path: Path) -> None:
    # The pre-0.9.179 default, as every saved file carries it: the project
    # entry is the convention now, dropped rather than refused.
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"skills": {"dirs": [".agents/skills", "~/.agents/skills"]}}),
        encoding="utf-8",
    )
    loaded = storage.load_config(path)
    assert loaded is not None and loaded.skills.dirs == ["~/.agents/skills"]


def test_skills_relative_entry_in_a_file_says_why(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"skills": {"dirs": ["skills"]}}), encoding="utf-8")
    with pytest.raises(UserError, match="loads by itself"):
        storage.load_config(path)


def test_web_runs_default_to_a_hundred_turns() -> None:
    # The web's own default; core's Runner keeps its library one.
    assert WebConfig().default_agent.max_turns == 100


def test_skills_explicit_empty_list_stays_empty() -> None:
    cfg = WebConfig.model_validate({"skills": {"dirs": []}})
    assert cfg.skills.dirs == []


def test_skills_dirs_stripped_and_deduped_in_order() -> None:
    assert SkillsConfig(dirs=[" ~/a ", "~/a", "~/b"]).dirs == ["~/a", "~/b"]


def test_skills_blank_entry_rejected() -> None:
    with pytest.raises(ValidationError, match="non-empty"):
        SkillsConfig(dirs=["  "])


def test_skills_roundtrips_through_storage(tmp_path: Path) -> None:
    cfg = WebConfig(skills=SkillsConfig(dirs=["~/team-skills"]))
    path = storage.save_config(cfg, tmp_path / "config.json")
    assert json.loads(path.read_text())["skills"] == {"dirs": ["~/team-skills"]}
    assert storage.load_config(path) == cfg


# ---------------------------------------------------------------- storage -


def test_save_config_is_owner_only_and_gitignored(tmp_path: Path) -> None:
    cfg = WebConfig(models=[ModelProfile(id="a", model="m", api_key="sk-secret")])
    # Any home, whatever it is called.
    target = storage.config_path(tmp_path / "my-home")
    path = storage.save_config(cfg, target)
    assert path == target
    assert (path.parent / ".gitignore").read_text().strip().splitlines()[-1] == "*"
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600  # secret file: owner-only
        assert path.parent.stat().st_mode & 0o777 == 0o700  # dir: owner-only too
    assert not list(path.parent.glob("*.tmp"))  # the atomic temp file is gone


@pytest.mark.parametrize(
    ("setup", "match"),
    [
        # A project's own .gitignore would leave the key committable.
        (lambda d: (d / ".gitignore").write_text(".venv/\n"), "ignore everything"),
        # A repository's root is a project, not lovia's own directory.
        (lambda d: (d / ".git").mkdir(), "repository's root"),
    ],
)
def test_save_config_refuses_a_home_that_is_not_lovias_own(
    tmp_path: Path, setup: object, match: str
) -> None:
    home = tmp_path / "project"
    home.mkdir()
    setup(home)  # type: ignore[operator]
    before = home.stat().st_mode
    cfg = WebConfig(models=[ModelProfile(id="a", model="m", api_key="sk-secret")])
    with pytest.raises(UserError, match=match) as info:
        storage.save_config(cfg, storage.config_path(home))
    assert "--home ./.lovia" in str(info.value)
    # Refused before anything changed.
    assert not (home / "config.json").exists()
    assert home.stat().st_mode == before


def test_save_config_accepts_a_home_whose_gitignore_ignores_everything(
    tmp_path: Path,
) -> None:
    home = tmp_path / "h"
    home.mkdir()
    (home / ".gitignore").write_text("# mine\n *\n", encoding="utf-8")
    storage.save_config(WebConfig(), storage.config_path(home))
    assert (home / ".gitignore").read_text(encoding="utf-8") == "# mine\n *\n"


def test_save_config_omits_null_fields(tmp_path: Path) -> None:
    cfg = WebConfig(models=[ModelProfile(id="a", model="m")])
    path = storage.save_config(cfg, tmp_path / "config.json")
    payload = json.loads(path.read_text())
    assert "api_key" not in payload["models"][0]
    assert "base_url" not in payload["models"][0]
    assert payload["version"] == 2


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


def test_load_active_reads_the_home_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_home: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    user = WebConfig(models=[ModelProfile(id="u", model="user-model")])
    storage.save_config(user, storage.config_path())
    # A launch directory's ./.lovia is just a directory now.
    here = WebConfig(models=[ModelProfile(id="p", model="here-model")])
    storage.save_config(here, tmp_path / ".lovia" / "config.json")
    loaded = storage.load_active()
    assert loaded.exists
    assert loaded.label == "~/.lovia/config.json"
    assert loaded.config.default_profile().model == "user-model"  # type: ignore[union-attr]
    # Another home is read whole, and labelled by its path.
    other = storage.load_active(tmp_path / ".lovia")
    assert other.config.default_profile().model == "here-model"  # type: ignore[union-attr]
    assert other.label == str((tmp_path / ".lovia" / "config.json").absolute())


def test_load_active_empty_targets_the_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_home: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    loaded = storage.load_active(tmp_path / "h")
    assert not loaded.exists
    assert loaded.config.models == []
    assert loaded.path == tmp_path / "h" / "config.json"


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
        WebConfig(models=[ModelProfile(id="a", model="m")]), storage.config_path()
    )
    caplog.clear()
    with caplog.at_level("WARNING", logger="lovia.web.config"):
        storage.load_active()
    assert "no longer read" not in caplog.text


# ----------------------------------------------------------------- agents -


def test_a_config_without_agents_gets_the_default_one() -> None:
    for doc in ({}, {"agents": []}):
        (agent,) = WebConfig.model_validate(doc).agents
        assert agent.id == "lovia" and agent.display_name == "lovia"
        assert (agent.mode, agent.max_turns, agent.model, agent.skills) == (
            "coding",
            100,
            None,
            None,
        )


def test_version_1_sections_are_ignored_not_converted() -> None:
    cfg = WebConfig.model_validate(
        {
            "version": 1,
            "models": [{"id": "m", "model": "x"}],
            "agent": {"instructions": "old prompt"},
            "run": {"max_turns": 7},
            "workspace": {"default_mode": "trusted"},
        }
    )
    assert cfg.version == 2
    assert cfg.roles.chat == "m"  # the unchanged sections still read
    agent = cfg.default_agent
    assert (agent.instructions, agent.max_turns, agent.mode) == ("", 100, "coding")


def test_agents_refer_to_configured_models_by_id() -> None:
    models = [{"id": "a", "model": "x"}, {"id": "b", "model": "y"}]
    cfg = WebConfig.model_validate(
        {"models": models, "agents": [{"id": "one"}, {"id": "two", "model": "b"}]}
    )
    assert [cfg.agent_profile(a).id for a in cfg.agents] == ["a", "b"]
    with pytest.raises(ValidationError, match="unknown model id 'c'"):
        WebConfig.model_validate(
            {"models": models, "agents": [{"id": "x", "model": "c"}]}
        )
    with pytest.raises(ValidationError, match="duplicate agent ids"):
        WebConfig.model_validate({"agents": [{"id": "x"}, {"id": "x"}]})


@pytest.mark.parametrize("bad", ["Upper", "has.dot", "-lead", ""])
def test_agent_ids_are_slugs_without_dots(bad: str) -> None:
    with pytest.raises(ValidationError):
        WebConfig.model_validate({"agents": [{"id": bad}]})


def test_an_agents_skills_are_global_paths_or_inherited() -> None:
    cfg = WebConfig.model_validate(
        {"skills": {"dirs": ["/g"]}, "agents": [{"id": "a"}, {"id": "b", "skills": []}]}
    )
    assert cfg.agent_skills(cfg.agents[0]).dirs == ["/g"]
    assert cfg.agent_skills(cfg.agents[1]).dirs == []
    with pytest.raises(ValidationError, match="global path"):
        WebConfig.model_validate({"agents": [{"id": "a", "skills": ["rel/dir"]}]})
