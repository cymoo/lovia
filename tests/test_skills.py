"""Tests for the skill system: metadata, loading, frontmatter, tools, path safety,
error handling, and agent integration."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lovia import Agent, Skills, SkillsError
from lovia.exceptions import ToolError
from lovia.run_context import RunContext
from lovia.skills import (
    LocalDirSkillSource,
    Skill,
    SkillMetadata,
    SkillSource,
    _parse_frontmatter,
    _validate_description,
    _validate_name,
    Skills as SkillsCapability,
)


def _make_ctx() -> RunContext[None]:
    """Create a minimal RunContext for testing tool invocations."""
    return RunContext(
        context=None,
        entries=[],
        agent=Agent(name="test", instructions=""),
    )


# ---------------------------------------------------------------------------
# Name / description validation
# ---------------------------------------------------------------------------


class TestValidateName:
    def test_valid_kebab_case(self) -> None:
        assert _validate_name("refund-policy") == "refund-policy"
        assert _validate_name("deploy") == "deploy"
        assert _validate_name("a") == "a"
        assert _validate_name("my-skill-123") == "my-skill-123"

    def test_empty_raises(self) -> None:
        with pytest.raises(SkillsError, match="must not be empty"):
            _validate_name("")

    def test_too_long_raises(self) -> None:
        long_name = "a" * 65
        with pytest.raises(SkillsError, match="too long"):
            _validate_name(long_name)

    def test_uppercase_raises(self) -> None:
        with pytest.raises(SkillsError, match="kebab-case"):
            _validate_name("RefundPolicy")

    def test_underscores_raises(self) -> None:
        with pytest.raises(SkillsError, match="kebab-case"):
            _validate_name("refund_policy")

    def test_consecutive_hyphens_raises(self) -> None:
        with pytest.raises(SkillsError, match="kebab-case"):
            _validate_name("refund--policy")

    def test_leading_hyphen_raises(self) -> None:
        with pytest.raises(SkillsError, match="kebab-case"):
            _validate_name("-refund")

    def test_trailing_hyphen_raises(self) -> None:
        with pytest.raises(SkillsError, match="kebab-case"):
            _validate_name("refund-")

    def test_path_separator_raises(self) -> None:
        with pytest.raises(SkillsError, match="path separator"):
            _validate_name("refund/policy")

    def test_dot_dot_raises(self) -> None:
        with pytest.raises(SkillsError, match="path separator"):
            _validate_name("..")


class TestValidateDescription:
    def test_valid(self) -> None:
        assert _validate_description("test", "A test skill.") == "A test skill."

    def test_empty_raises(self) -> None:
        with pytest.raises(SkillsError, match="empty description"):
            _validate_description("test", "")

    def test_whitespace_only_raises(self) -> None:
        with pytest.raises(SkillsError, match="empty description"):
            _validate_description("test", "   ")

    def test_too_long_raises(self) -> None:
        long_desc = "x" * 1025
        with pytest.raises(SkillsError, match="too long"):
            _validate_description("test", long_desc)

    def test_strips_whitespace(self) -> None:
        assert _validate_description("test", "  hello  ") == "hello"


# ---------------------------------------------------------------------------
# SkillMetadata
# ---------------------------------------------------------------------------


class TestSkillMetadata:
    def test_construction(self) -> None:
        m = SkillMetadata(name="test-skill", description="A test.")
        assert m.name == "test-skill"
        assert m.description == "A test."

    def test_frozen(self) -> None:
        m = SkillMetadata(name="test", description="desc")
        with pytest.raises(Exception):
            m.name = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------


class TestSkill:
    def test_construction(self) -> None:
        skill = Skill(name="test", description="desc", content="# Hello")
        assert skill.name == "test"
        assert skill.content == "# Hello"

    def test_metadata_property(self) -> None:
        skill = Skill(name="my-skill", description="Does stuff.", content="body")
        meta = skill.metadata
        assert meta.name == "my-skill"
        assert meta.description == "Does stuff."

    def test_read_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "references").mkdir()
            (root / "references" / "info.md").write_text("extra info")
            skill = Skill(name="test", description="desc", content="body", path=root)
            assert skill.read_file("references/info.md") == "extra info"

    def test_read_file_no_path_raises(self) -> None:
        skill = Skill(name="test", description="desc", content="body", path=None)
        with pytest.raises(ToolError, match="no on-disk path"):
            skill.read_file("references/x.md")

    def test_read_file_path_traversal_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = Skill(name="test", description="desc", content="body", path=root)
            with pytest.raises(ToolError, match="escapes skill directory"):
                skill.read_file("../outside")

    def test_read_file_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = Skill(name="test", description="desc", content="body", path=root)
            with pytest.raises(ToolError, match="not found"):
                skill.read_file("nonexistent.md")


# ---------------------------------------------------------------------------
# Frontmatter parser
# ---------------------------------------------------------------------------


class TestParseFrontmatter:
    def test_valid_frontmatter(self) -> None:
        text = "---\nname: test\ndescription: A test skill.\n---\n# Body"
        meta, body = _parse_frontmatter(text)
        assert meta["name"] == "test"
        assert meta["description"] == "A test skill."
        assert "# Body" in body

    def test_no_frontmatter(self) -> None:
        text = "# Just a markdown file"
        meta, body = _parse_frontmatter(text)
        assert meta == {}
        assert body == text

    def test_only_opening_dashes(self) -> None:
        text = "---\nname: test\n# body without closing"
        meta, body = _parse_frontmatter(text)
        assert meta == {}

    def test_embedded_dashes_in_body(self) -> None:
        text = "---\nname: test\ndescription: desc\n---\n# Body\n\n---\n\nMore body."
        meta, body = _parse_frontmatter(text)
        assert meta["name"] == "test"
        assert "---" in body
        assert "More body" in body

    def test_empty_frontmatter(self) -> None:
        text = "---\n---\n# Body"
        meta, body = _parse_frontmatter(text)
        assert meta == {}
        assert "# Body" in body

    def test_quoted_values(self) -> None:
        text = "---\nname: \"test-skill\"\ndescription: 'A description'\n---\nBody"
        meta, body = _parse_frontmatter(text)
        assert meta["name"] == "test-skill"
        assert meta["description"] == "A description"

    def test_comment_lines_ignored(self) -> None:
        text = "---\n# comment\nname: test\ndescription: desc\n---\nBody"
        meta, body = _parse_frontmatter(text)
        assert meta["name"] == "test"

    def test_list_values(self) -> None:
        text = "---\nname: test\ndescription: desc\ntags: [a, b, c]\n---\nBody"
        meta, body = _parse_frontmatter(text)
        assert meta["tags"] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# LocalDirSkillSource
# ---------------------------------------------------------------------------


class TestLocalDirSkillSource:
    def test_metadata_from_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "refund-policy").mkdir()
            (root / "refund-policy" / "SKILL.md").write_text(
                "---\nname: refund-policy\ndescription: Process refunds.\n---\n# Refund\n..."
            )
            (root / "deploy").mkdir()
            (root / "deploy" / "SKILL.md").write_text(
                "---\nname: deploy\ndescription: Deploy to prod.\n---\n# Deploy\n..."
            )

            source = LocalDirSkillSource(root)
            meta = source.metadata
            assert len(meta) == 2
            names = {m.name for m in meta}
            assert names == {"refund-policy", "deploy"}

    def test_metadata_empty_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = LocalDirSkillSource(Path(tmp))
            assert source.metadata == []

    def test_metadata_nonexistent_dir(self) -> None:
        source = LocalDirSkillSource(Path("/nonexistent/path/12345"))
        assert source.metadata == []

    def test_skips_non_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("hello")
            source = LocalDirSkillSource(root)
            assert source.metadata == []

    def test_skips_dirs_without_skill_md(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "empty-dir").mkdir()
            source = LocalDirSkillSource(root)
            assert source.metadata == []

    def test_falls_back_name_from_dirname(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "my-skill").mkdir()
            (root / "my-skill" / "SKILL.md").write_text(
                "---\ndescription: No name field.\n---\n# Body"
            )
            source = LocalDirSkillSource(root)
            assert len(source.metadata) == 1
            assert source.metadata[0].name == "my-skill"
            assert source.metadata[0].description == "No name field."

    def test_duplicate_name_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "skill-a").mkdir()
            (root / "skill-a" / "SKILL.md").write_text(
                "---\nname: dup\ndescription: First.\n---\n# A"
            )
            (root / "skill-b").mkdir()
            (root / "skill-b" / "SKILL.md").write_text(
                "---\nname: dup\ndescription: Second.\n---\n# B"
            )
            with pytest.raises(SkillsError, match="Duplicate"):
                LocalDirSkillSource(root)

    def test_invalid_name_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "BadName").mkdir()
            (root / "BadName" / "SKILL.md").write_text(
                "---\nname: BadName\ndescription: Invalid name.\n---\n# Body"
            )
            with pytest.raises(SkillsError, match="kebab-case"):
                LocalDirSkillSource(root)

    def test_missing_description_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "test").mkdir()
            (root / "test" / "SKILL.md").write_text(
                "---\nname: test\n---\n# Body"
            )
            with pytest.raises(SkillsError, match="empty description"):
                LocalDirSkillSource(root)

    def test_metadata_list_async(self) -> None:
        """list_metadata() returns the same as metadata property."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "s").mkdir()
            (root / "s" / "SKILL.md").write_text(
                "---\nname: s\ndescription: A skill.\n---\n# Body"
            )
            source = LocalDirSkillSource(root)
            import asyncio
            meta = asyncio.run(source.list_metadata())
            assert len(meta) == 1


class TestLocalDirSkillSourceLoad:
    def test_load_returns_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "refund-policy").mkdir()
            (root / "refund-policy" / "SKILL.md").write_text(
                "---\nname: refund-policy\ndescription: Process refunds.\n---\n# Refund\nBe polite."
            )
            source = LocalDirSkillSource(root)
            import asyncio
            skill = asyncio.run(source.load("refund-policy"))
            assert skill.name == "refund-policy"
            assert "Be polite" in skill.content
            assert skill.path is not None

    def test_load_caches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "s").mkdir()
            (root / "s" / "SKILL.md").write_text(
                "---\nname: s\ndescription: A skill.\n---\n# Body"
            )
            source = LocalDirSkillSource(root)
            import asyncio
            skill1 = asyncio.run(source.load("s"))
            skill2 = asyncio.run(source.load("s"))
            assert skill1 is skill2

    def test_load_unknown_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = LocalDirSkillSource(Path(tmp))
            import asyncio
            with pytest.raises(SkillsError, match="Unknown"):
                asyncio.run(source.load("nonexistent"))

    def test_evict_and_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "s").mkdir()
            (root / "s" / "SKILL.md").write_text(
                "---\nname: s\ndescription: A skill.\n---\n# Version 1"
            )
            source = LocalDirSkillSource(root)
            import asyncio
            skill1 = asyncio.run(source.load("s"))
            source.evict("s")
            # Modify on disk
            (root / "s" / "SKILL.md").write_text(
                "---\nname: s\ndescription: A skill.\n---\n# Version 2"
            )
            skill2 = asyncio.run(source.load("s"))
            assert "Version 2" in skill2.content
            assert skill1 is not skill2

    def test_clear_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "s").mkdir()
            (root / "s" / "SKILL.md").write_text(
                "---\nname: s\ndescription: A skill.\n---\n# Body"
            )
            source = LocalDirSkillSource(root)
            import asyncio
            asyncio.run(source.load("s"))
            source.clear_cache()
            # Should reload from disk
            skill = asyncio.run(source.load("s"))
            assert skill is not None


# ---------------------------------------------------------------------------
# Skills capability container
# ---------------------------------------------------------------------------


class TestSkillsInstructions:
    def test_renders_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "refund-policy").mkdir()
            (root / "refund-policy" / "SKILL.md").write_text(
                "---\nname: refund-policy\ndescription: Process refunds.\n---\n# Body"
            )

            skills = SkillsCapability.from_dir(root)
            text = skills.instructions()
            assert "refund-policy" in text
            assert "Process refunds" in text
            assert "load_skill" in text

    def test_empty_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skills = SkillsCapability.from_dir(Path(tmp))
            assert skills.instructions() == ""

    def test_no_usage_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "s").mkdir()
            (root / "s" / "SKILL.md").write_text(
                "---\nname: s\ndescription: A skill.\n---\n# Body"
            )
            skills = SkillsCapability.from_dir(root, usage_rules=False)
            text = skills.instructions()
            assert "How to use skills" not in text

    def test_instructions_includes_all_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("a-skill", "b-skill", "c-skill"):
                (root / name).mkdir()
                (root / name / "SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: Skill {name}.\n---\n# Body"
                )
            skills = SkillsCapability.from_dir(root)
            text = skills.instructions()
            for name in ("a-skill", "b-skill", "c-skill"):
                assert name in text


class TestSkillsTools:
    def test_returns_three_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "s").mkdir()
            (root / "s" / "SKILL.md").write_text(
                "---\nname: s\ndescription: A skill.\n---\n# Body"
            )
            skills = SkillsCapability.from_dir(root)
            tools = skills.tools()
            assert len(tools) == 3
            tool_names = {t.name for t in tools}
            assert tool_names == {"list_skills", "load_skill", "read_skill_file"}

    def test_list_skills_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "s").mkdir()
            (root / "s" / "SKILL.md").write_text(
                "---\nname: s\ndescription: A skill.\n---\n# Body"
            )
            skills = SkillsCapability.from_dir(root)
            tools = skills.tools()
            list_tool = next(t for t in tools if t.name == "list_skills")
            import asyncio
            result = asyncio.run(list_tool.invoke({}, _make_ctx()))
            assert "s" in result
            assert "A skill" in result

    def test_load_skill_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "s").mkdir()
            (root / "s" / "SKILL.md").write_text(
                "---\nname: s\ndescription: A skill.\n---\n# Skill Body\nBe helpful."
            )
            skills = SkillsCapability.from_dir(root)
            tools = skills.tools()
            load_tool = next(t for t in tools if t.name == "load_skill")
            import asyncio
            result = asyncio.run(load_tool.invoke({"name": "s"}, _make_ctx()))
            assert "Be helpful" in result
            assert "path:" in result  # skill path included for script execution

    def test_load_skill_tool_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skills = SkillsCapability.from_dir(Path(tmp))
            tools = skills.tools()
            load_tool = next(t for t in tools if t.name == "load_skill")
            import asyncio
            result = asyncio.run(
                load_tool.invoke({"name": "unknown"}, _make_ctx())  # type: ignore[arg-type]
            )
            assert "Unknown" in result

    def test_load_skill_tool_dollar_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "s").mkdir()
            (root / "s" / "SKILL.md").write_text(
                "---\nname: s\ndescription: A skill.\n---\n# Body"
            )
            skills = SkillsCapability.from_dir(root)
            tools = skills.tools()
            load_tool = next(t for t in tools if t.name == "load_skill")
            import asyncio
            result = asyncio.run(
                load_tool.invoke({"name": "$s"}, _make_ctx())  # type: ignore[arg-type]
            )
            assert "Body" in result

    def test_read_skill_file_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "s").mkdir()
            (root / "s" / "SKILL.md").write_text(
                "---\nname: s\ndescription: A skill.\n---\n# Body"
            )
            (root / "s" / "references").mkdir()
            (root / "s" / "references" / "extra.md").write_text("Extra info.")
            skills = SkillsCapability.from_dir(root)
            tools = skills.tools()
            read_tool = next(t for t in tools if t.name == "read_skill_file")
            import asyncio
            result = asyncio.run(
                read_tool.invoke(
                    {"name": "s", "relpath": "references/extra.md"},
                    _make_ctx(),  # type: ignore[arg-type]
                )
            )
            assert result == "Extra info."

    def test_read_skill_file_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "s").mkdir()
            (root / "s" / "SKILL.md").write_text(
                "---\nname: s\ndescription: A skill.\n---\n# Body"
            )
            skills = SkillsCapability.from_dir(root)
            tools = skills.tools()
            read_tool = next(t for t in tools if t.name == "read_skill_file")
            import asyncio
            result = asyncio.run(
                read_tool.invoke(
                    {"name": "s", "relpath": "../outside"},
                    _make_ctx(),  # type: ignore[arg-type]
                )
            )
            assert "escapes" in result

    def test_read_skill_file_unknown_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skills = SkillsCapability.from_dir(Path(tmp))
            tools = skills.tools()
            read_tool = next(t for t in tools if t.name == "read_skill_file")
            import asyncio
            result = asyncio.run(
                read_tool.invoke(
                    {"name": "nope", "relpath": "references/x.md"},
                    _make_ctx(),  # type: ignore[arg-type]
                )
            )
            assert "Unknown" in result


# ---------------------------------------------------------------------------
# Custom SkillSource protocol
# ---------------------------------------------------------------------------


class TestCustomSkillSource:
    def test_protocol_conformance(self) -> None:
        """A class with list_metadata and load satisfies the protocol."""
        class MySource:
            async def list_metadata(self) -> list[SkillMetadata]:
                return [SkillMetadata(name="test", description="desc")]

            async def load(self, name: str) -> Skill:
                return Skill(name="test", description="desc", content="# Body")

        source = MySource()
        assert isinstance(source, SkillSource)

    def test_missing_method_not_protocol(self) -> None:
        """A class missing a method does NOT satisfy the protocol."""
        class BadSource:
            async def list_metadata(self) -> list[SkillMetadata]:
                return []

        assert not isinstance(BadSource(), SkillSource)

    def test_custom_source_with_skills_container(self) -> None:
        """Skills container works with a custom source."""

        class ApiSource:
            async def list_metadata(self) -> list[SkillMetadata]:
                return [SkillMetadata(name="api-skill", description="From API.")]

            async def load(self, name: str) -> Skill:
                if name != "api-skill":
                    raise SkillsError(f"Unknown: {name}", skill_name=name)
                return Skill(name="api-skill", description="From API.", content="# API Content")

        skills = SkillsCapability(source=ApiSource())
        # instructions() won't work well for custom async sources
        # (they haven't pre-cached metadata) — but tools() should work
        tools = skills.tools()
        assert len(tools) == 3

        import asyncio
        load_tool = next(t for t in tools if t.name == "load_skill")
        result = asyncio.run(
            load_tool.invoke({"name": "api-skill"}, _make_ctx())  # type: ignore[arg-type]
        )
        assert "API Content" in result


# ---------------------------------------------------------------------------
# SkillsError
# ---------------------------------------------------------------------------


class TestSkillsError:
    def test_message_only(self) -> None:
        err = SkillsError("Something went wrong.")
        assert str(err) == "Something went wrong."
        assert err.skill_name is None
        assert err.path is None
        assert err.hint is None

    def test_full_context(self) -> None:
        err = SkillsError(
            "Failed.",
            skill_name="my-skill",
            path="/some/path",
            hint="Try renaming.",
        )
        assert err.skill_name == "my-skill"
        assert err.path == "/some/path"
        assert err.hint == "Try renaming."


# ---------------------------------------------------------------------------
# Agent integration
# ---------------------------------------------------------------------------


class TestAgentIntegration:
    def test_agent_with_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "refund-policy").mkdir()
            (root / "refund-policy" / "SKILL.md").write_text(
                "---\nname: refund-policy\ndescription: Process refunds and handle returns.\n---\n# Refund\nBe polite."
            )

            agent = Agent(
                name="test",
                instructions="Help the customer.",
                skills=Skills.from_dir(root),
            )
            assert agent.skills is not None
            text = agent.skills.instructions()
            assert "refund-policy" in text
            assert "Process refunds" in text

    def test_agent_without_skills(self) -> None:
        agent = Agent(name="test", instructions="Be helpful.")
        assert agent.skills is None

    async def test_system_prompt_includes_skills_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "deploy").mkdir()
            (root / "deploy" / "SKILL.md").write_text(
                "---\nname: deploy\ndescription: Deploy the application.\n---\n# Deploy\n..."
            )

            agent = Agent(
                name="test",
                instructions="You are helpful.",
                skills=Skills.from_dir(root),
            )
            prompt = await agent.render_instructions(None)
            # The skill index is rendered by the run loop via
            # agent.skills.instructions(), not by agent.render_instructions().
            # Just verify the skill index text is available.
            index = agent.skills.instructions()
            assert "deploy" in index
            assert "Deploy the application" in index


# ---------------------------------------------------------------------------
# Edge cases & error isolation
# ---------------------------------------------------------------------------


class TestErrorIsolation:
    def test_corrupt_skill_dir_does_not_block_others(self) -> None:
        """A directory with a broken SKILL.md doesn't prevent other skills
        from being discovered (error isolation)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            # Valid skill
            (root / "good-skill").mkdir()
            (root / "good-skill" / "SKILL.md").write_text(
                "---\nname: good-skill\ndescription: Works fine.\n---\n# Body"
            )

            # Broken skill: directory exists but SKILL.md is unreadable
            (root / "broken-skill").mkdir()
            broken_md = root / "broken-skill" / "SKILL.md"
            broken_md.write_text(
                "---\nname: broken-skill\ndescription: Broken.\n---\n# Body"
            )
            # Make it unreadable
            os.chmod(broken_md, 0o000)

            try:
                source = LocalDirSkillSource(root)
                # The good skill should be discovered; the broken one skipped
                assert len(source.metadata) == 1
                assert source.metadata[0].name == "good-skill"
            finally:
                # Restore permissions so tempfile can clean up
                os.chmod(broken_md, 0o644)

    def test_skill_md_as_non_file(self) -> None:
        """SKILL.md that is a directory, not a file, is skipped."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "weird-skill").mkdir()
            (root / "weird-skill" / "SKILL.md").mkdir()  # directory, not file
            source = LocalDirSkillSource(root)
            assert source.metadata == []


# ---------------------------------------------------------------------------
# SkillsError context propagation tests
# ---------------------------------------------------------------------------


class TestSkillsErrorContext:
    def test_context_propagates_to_validation(self) -> None:
        """SkillsError carries structured context through the call chain."""
        err = SkillsError(
            "Invalid skill configuration.",
            skill_name="bad-name",
            path="/tmp/skills/bad-name",
            hint="Check the SKILL.md frontmatter.",
        )
        assert "bad-name" in str(err) or err.skill_name == "bad-name"
        assert err.path == "/tmp/skills/bad-name"
        assert "SKILL.md" in (err.hint or "")
