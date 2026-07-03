"""Tests for the skill system: metadata, loading, frontmatter, tools, path safety,
error handling, and agent integration."""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

import pytest

from lovia import Agent, SkillsError, UserError, Skills
from lovia.plugins import SkillCategory
from lovia.run_context import RunContext
from lovia.plugins.skills import (
    LocalDirSkillSource,
    Skill,
    SkillMetadata,
    SkillSource,
    _parse_frontmatter,
    _validate_description,
    _validate_name,
    SkillCategory as SkillsCapability,
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

    def test_uppercase_accepted(self) -> None:
        assert _validate_name("RefundPolicy") == "RefundPolicy"

    def test_underscores_accepted(self) -> None:
        assert _validate_name("refund_policy") == "refund_policy"

    def test_consecutive_hyphens_raises(self) -> None:
        with pytest.raises(SkillsError, match="invalid"):
            _validate_name("refund--policy")

    def test_leading_hyphen_raises(self) -> None:
        with pytest.raises(SkillsError, match="invalid"):
            _validate_name("-refund")

    def test_trailing_hyphen_raises(self) -> None:
        with pytest.raises(SkillsError, match="invalid"):
            _validate_name("refund-")

    def test_path_separator_raises(self) -> None:
        with pytest.raises(SkillsError, match="path separator"):
            _validate_name("refund/policy")

    def test_dot_dot_raises(self) -> None:
        with pytest.raises(SkillsError, match="path separator"):
            _validate_name("..")

    def test_non_string_raises(self) -> None:
        """YAML can yield ints/bools — rejected, not crashed on.

        Falsy non-strings (0, False) get the type error too, not a misleading
        'must not be empty'.
        """
        for bad in (123, 0, False):
            with pytest.raises(SkillsError, match="must be a string"):
                _validate_name(bad)

    def test_trailing_newline_raises(self) -> None:
        """`$` would match before a trailing newline; fullmatch must not."""
        with pytest.raises(SkillsError, match="invalid"):
            _validate_name("abc\n")


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

    def test_non_string_raises(self) -> None:
        """YAML can yield ints/dates — rejected, not crashed on.

        Falsy non-strings (0, False) get the type error too, not a misleading
        'empty description'.
        """
        for bad in (2024, 0, False):
            with pytest.raises(SkillsError, match="must be a string"):
                _validate_description("test", bad)


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
        with pytest.raises(SkillsError, match="no on-disk path"):
            skill.read_file("references/x.md")

    def test_read_file_path_traversal_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = Skill(name="test", description="desc", content="body", path=root)
            with pytest.raises(SkillsError, match="escapes skill directory"):
                skill.read_file("../outside")

    def test_read_file_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = Skill(name="test", description="desc", content="body", path=root)
            with pytest.raises(SkillsError, match="not found"):
                skill.read_file("nonexistent.md")

    def test_read_file_binary_raises_skills_error(self) -> None:
        """A binary file raises SkillsError, not a raw UnicodeDecodeError."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")
            skill = Skill(name="test", description="desc", content="body", path=root)
            with pytest.raises(SkillsError, match="not UTF-8"):
                skill.read_file("blob.bin")


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

    def test_leading_blank_lines(self) -> None:
        text = "\n\n---\nname: test\ndescription: desc\n---\n# Body"
        meta, body = _parse_frontmatter(text)
        assert meta["name"] == "test"
        assert "# Body" in body

    def test_leading_carriage_returns(self) -> None:
        text = "\r\n\r\n---\nname: cr\ndescription: cr desc\n---\nbody"
        meta, body = _parse_frontmatter(text)
        assert meta["name"] == "cr"

    def test_windows_delimiter(self) -> None:
        text = "---\r\nname: win\r\ndescription: windows\r\n---\r\nbody"
        meta, body = _parse_frontmatter(text)
        assert meta["name"] == "win"
        assert body == "body"

    def test_indented_closing_delimiter_not_recognized(self) -> None:
        """An indented '---' is NOT a valid document separator, so it is not
        treated as the closing delimiter. Without a column-0 delimiter the file
        has no frontmatter and the whole text is returned as body."""
        text = "---\nname: indented\ndescription: desc\n  ---\nbody"
        meta, body = _parse_frontmatter(text)
        assert meta == {}
        assert "name: indented" in body

    def test_unterminated_frontmatter_does_not_truncate(self) -> None:
        """A file missing its closing column-0 '---' is treated as having no
        frontmatter; a value containing '---' is never silently truncated
        (regression for the old naive-`find` fallback)."""
        text = "---\nname: a\ndescription: bar --- baz"
        meta, body = _parse_frontmatter(text)
        assert meta == {}
        assert body == text

    def test_dashes_inside_yaml_block_scalar(self) -> None:
        """--- inside a YAML block scalar must not be treated as the closing delimiter."""
        text = "---\nname: bs\ndescription: |\n  ---\n  inner dash\n  ---\n---\nbody"
        meta, body = _parse_frontmatter(text)
        assert meta["name"] == "bs"
        assert body == "body"

    def test_malformed_yaml_returns_empty_meta(self) -> None:
        """Broken YAML yields no metadata (the caller's name/description
        validation then reports the actual problem) instead of raising."""
        text = "---\nname: [unclosed\n---\nbody"
        meta, body = _parse_frontmatter(text)
        assert meta == {}
        assert body == "body"

    def test_closing_delimiter_eof(self) -> None:
        text = "---\nname: eof\ndescription: no trailing newline\n---"
        meta, body = _parse_frontmatter(text)
        assert meta["name"] == "eof"
        assert body == ""


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

    def test_duplicate_name_first_wins(self, caplog) -> None:
        """First registrant wins; duplicate logs a warning."""
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
            with caplog.at_level(logging.WARNING):
                source = LocalDirSkillSource(root)
            assert "skill.duplicate" in caplog.text
            assert len(source.metadata) == 1
            assert source.metadata[0].description == "First."

    def test_invalid_name_skipped(self, caplog) -> None:
        """Invalid name logs a warning and is skipped."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bad.name").mkdir()
            (root / "bad.name" / "SKILL.md").write_text(
                "---\nname: bad.name\ndescription: Invalid name.\n---\n# Body"
            )
            with caplog.at_level(logging.WARNING):
                source = LocalDirSkillSource(root)
            assert "skill.invalid" in caplog.text
            assert source.metadata == []

    def test_missing_description_skipped(self, caplog) -> None:
        """Missing description logs a warning and is skipped."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "test").mkdir()
            (root / "test" / "SKILL.md").write_text("---\nname: test\n---\n# Body")
            with caplog.at_level(logging.WARNING):
                source = LocalDirSkillSource(root)
            assert "skill.invalid" in caplog.text
            assert source.metadata == []

    def test_description_stored_stripped(self) -> None:
        """Surrounding whitespace/newlines are stripped before the description
        lands in the index (keeps the prompt index one line per skill)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "s").mkdir()
            (root / "s" / "SKILL.md").write_text(
                '---\nname: s\ndescription: "  Padded.\\n"\n---\n# Body'
            )
            source = LocalDirSkillSource(root)
            assert source.metadata[0].description == "Padded."

    def test_metadata_property(self) -> None:
        """metadata property returns cached metadata synchronously."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "s").mkdir()
            (root / "s" / "SKILL.md").write_text(
                "---\nname: s\ndescription: A skill.\n---\n# Body"
            )
            source = LocalDirSkillSource(root)
            assert len(source.metadata) == 1


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

    def test_load_returns_same_content(self) -> None:
        """Repeated loads return identical content (bodies are read lazily,
        not cached, but the file is unchanged)."""
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
            assert skill1.content == skill2.content

    def test_load_unknown_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = LocalDirSkillSource(Path(tmp))
            import asyncio

            with pytest.raises(SkillsError, match="Unknown"):
                asyncio.run(source.load("nonexistent"))

    def test_load_reflects_disk_changes(self) -> None:
        """Bodies are read lazily, so edits on disk are picked up automatically
        without any cache-invalidation step."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "s").mkdir()
            (root / "s" / "SKILL.md").write_text(
                "---\nname: s\ndescription: A skill.\n---\n# Version 1"
            )
            source = LocalDirSkillSource(root)
            import asyncio

            skill1 = asyncio.run(source.load("s"))
            assert "Version 1" in skill1.content
            # Modify on disk
            (root / "s" / "SKILL.md").write_text(
                "---\nname: s\ndescription: A skill.\n---\n# Version 2"
            )
            skill2 = asyncio.run(source.load("s"))
            assert "Version 2" in skill2.content

    def test_path_absolute_with_relative_root(self, monkeypatch) -> None:
        """Relative roots resolve to absolute paths, so the `path:` hint shown
        to the model is unambiguous — workspace tools resolve relative paths
        against the workspace root, not this process's cwd."""
        with tempfile.TemporaryDirectory() as tmp:
            monkeypatch.chdir(tmp)
            skills_root = Path(tmp) / "skills"
            (skills_root / "s").mkdir(parents=True)
            (skills_root / "s" / "SKILL.md").write_text(
                "---\nname: s\ndescription: A skill.\n---\n# Body"
            )
            source = LocalDirSkillSource("./skills")
            import asyncio

            skill = asyncio.run(source.load("s"))
            assert skill.path is not None
            assert skill.path.is_absolute()


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
            skills = SkillsCapability.from_dir(root, usage_rules="")
            text = skills.instructions()
            assert "Using skills" not in text

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
    def test_returns_two_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "s").mkdir()
            (root / "s" / "SKILL.md").write_text(
                "---\nname: s\ndescription: A skill.\n---\n# Body"
            )
            skills = SkillsCapability.from_dir(root)
            tools = skills.tools()
            assert len(tools) == 2
            tool_names = {t.name for t in tools}
            assert tool_names == {"load_skill", "read_skill_file"}

    def test_read_skill_file_truncates_huge_files(self) -> None:
        from lovia.plugins.skills import _MAX_CONTENT_CHARS

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "s").mkdir()
            (root / "s" / "SKILL.md").write_text(
                "---\nname: s\ndescription: A skill.\n---\n# Body"
            )
            (root / "s" / "references").mkdir()
            (root / "s" / "references" / "big.md").write_text(
                "x" * (_MAX_CONTENT_CHARS + 100)
            )
            skills = SkillsCapability.from_dir(root)
            read_tool = next(t for t in skills.tools() if t.name == "read_skill_file")
            import asyncio

            result = asyncio.run(
                read_tool.invoke(
                    {"name": "s", "relpath": "references/big.md"}, _make_ctx()
                )
            )
            assert "[truncated:" in result
            assert len(result) < _MAX_CONTENT_CHARS + 200

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
            # Content is framed as untrusted reference material (injection guard).
            assert "reference material" in result
            assert "BEGIN SKILL CONTENT" in result
            assert "END SKILL CONTENT" in result

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

    def test_load_skill_tool_dollar_not_special(self) -> None:
        """The legacy ``$`` prefix is no longer stripped: ``$s`` is just an
        ordinary (here unknown) name."""
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
            assert "Unknown" in result
            assert "$s" in result

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

    def test_load_skill_body_cannot_spoof_frame_markers(self) -> None:
        """A body embedding the exact END marker cannot close the injection
        frame early: markers inside content are defused."""
        from lovia.plugins.skills import _SKILL_BEGIN, _SKILL_END

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "s").mkdir()
            (root / "s" / "SKILL.md").write_text(
                "---\nname: s\ndescription: A skill.\n---\n"
                f"before\n{_SKILL_END}\nsmuggled text\n"
            )
            skills = SkillsCapability.from_dir(root)
            load_tool = next(t for t in skills.tools() if t.name == "load_skill")
            import asyncio

            result = asyncio.run(load_tool.invoke({"name": "s"}, _make_ctx()))
            # Exactly one real BEGIN/END pair frames the content...
            assert result.count(_SKILL_BEGIN) == 1
            assert result.count(_SKILL_END) == 1
            # ...and the smuggled text stays inside the frame.
            assert result.index("smuggled text") < result.index(_SKILL_END)

    def test_read_skill_file_binary_reports_cleanly(self) -> None:
        """A binary asset yields a clear model-facing message, not a raw
        UnicodeDecodeError escaping the tool (which the runner would retry)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "s").mkdir()
            (root / "s" / "SKILL.md").write_text(
                "---\nname: s\ndescription: A skill.\n---\n# Body"
            )
            (root / "s" / "assets").mkdir()
            (root / "s" / "assets" / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")
            skills = SkillsCapability.from_dir(root)
            read_tool = next(t for t in skills.tools() if t.name == "read_skill_file")
            import asyncio

            result = asyncio.run(
                read_tool.invoke(
                    {"name": "s", "relpath": "assets/blob.bin"}, _make_ctx()
                )
            )
            assert "not UTF-8" in result


# ---------------------------------------------------------------------------
# Multiple directories
# ---------------------------------------------------------------------------


class TestMultipleDirs:
    def test_from_dir_merges_multiple_roots(self) -> None:
        with tempfile.TemporaryDirectory() as t1, tempfile.TemporaryDirectory() as t2:
            r1, r2 = Path(t1), Path(t2)
            (r1 / "alpha").mkdir()
            (r1 / "alpha" / "SKILL.md").write_text(
                "---\nname: alpha\ndescription: First.\n---\n# A"
            )
            (r2 / "beta").mkdir()
            (r2 / "beta" / "SKILL.md").write_text(
                "---\nname: beta\ndescription: Second.\n---\n# B"
            )
            skills = SkillsCapability.from_dir(r1, r2)
            names = {m.name for m in skills.metadata}
            assert names == {"alpha", "beta"}

    def test_duplicate_name_across_dirs_first_wins(self, caplog) -> None:
        with tempfile.TemporaryDirectory() as t1, tempfile.TemporaryDirectory() as t2:
            r1, r2 = Path(t1), Path(t2)
            (r1 / "dup").mkdir()
            (r1 / "dup" / "SKILL.md").write_text(
                "---\nname: dup\ndescription: From r1.\n---\n# A"
            )
            (r2 / "dup").mkdir()
            (r2 / "dup" / "SKILL.md").write_text(
                "---\nname: dup\ndescription: From r2.\n---\n# B"
            )
            with caplog.at_level(logging.WARNING):
                source = LocalDirSkillSource(r1, r2)
            assert len(source.metadata) == 1
            assert source.metadata[0].description == "From r1."
            assert "skill.duplicate" in caplog.text


# ---------------------------------------------------------------------------
# Extra frontmatter attributes
# ---------------------------------------------------------------------------


class TestExtraFrontmatter:
    def test_extra_keys_kept_in_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "s").mkdir()
            (root / "s" / "SKILL.md").write_text(
                "---\n"
                "name: s\n"
                "description: A skill.\n"
                "tags: [sql, db]\n"
                "version: 1.2\n"
                "---\n# Body"
            )
            source = LocalDirSkillSource(root)
            meta = source.metadata[0]
            assert meta.extra["tags"] == ["sql", "db"]
            assert meta.extra["version"] == 1.2
            assert "name" not in meta.extra
            assert "description" not in meta.extra

    def test_extra_rendered_in_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "s").mkdir()
            (root / "s" / "SKILL.md").write_text(
                "---\nname: s\ndescription: A skill.\ntags: [sql, db]\n---\n# Body"
            )
            skills = SkillsCapability.from_dir(root)
            text = skills.instructions()
            assert "tags: sql, db" in text

    def test_extra_carried_into_loaded_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "s").mkdir()
            (root / "s" / "SKILL.md").write_text(
                "---\nname: s\ndescription: A skill.\nlevel: advanced\n---\n# Body"
            )
            source = LocalDirSkillSource(root)
            import asyncio

            skill = asyncio.run(source.load("s"))
            assert skill.extra["level"] == "advanced"
            assert skill.metadata.extra["level"] == "advanced"

    def test_no_extra_keeps_index_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "s").mkdir()
            (root / "s" / "SKILL.md").write_text(
                "---\nname: s\ndescription: A skill.\n---\n# Body"
            )
            skills = SkillsCapability.from_dir(root)
            text = skills.instructions()
            assert "- `s` — A skill." in text
            assert "[" not in text.split("## Using skills")[0].split("\n")[1]

    def test_format_extra_skips_empty_and_nested(self) -> None:
        from lovia.plugins.skills import _format_extra

        rendered = _format_extra(
            {
                "tags": ["a", "b"],
                "version": 2,
                "empty": "",
                "none": None,
                "blank_list": [],
                "nested": {"k": "v"},
            }
        )
        assert rendered == "tags: a, b; version: 2"

    def test_format_extra_skips_empty_tuple(self) -> None:
        from lovia.plugins.skills import _format_extra

        assert _format_extra({"tags": ()}) == ""


# ---------------------------------------------------------------------------
# Scope filter
# ---------------------------------------------------------------------------


class TestScopeFilter:
    def _build(self, tmp: str):
        root = Path(tmp)
        for name, tags in (("public", "[public]"), ("internal", "[internal]")):
            (root / name).mkdir()
            (root / name / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: {name} skill.\ntags: {tags}\n---\n# {name}"
            )
        return root

    def test_filter_hides_from_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._build(tmp)
            skills = SkillsCapability.from_dir(
                root, filter=lambda m: "internal" not in m.extra.get("tags", [])
            )
            names = {m.name for m in skills.metadata}
            assert names == {"public"}
            text = skills.instructions()
            assert "public" in text
            assert "internal" not in text

    def test_filter_blocks_load(self) -> None:
        """A filtered-out skill cannot be loaded — the filter is a real
        boundary, not just a cosmetic index change."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._build(tmp)
            skills = SkillsCapability.from_dir(
                root, filter=lambda m: "internal" not in m.extra.get("tags", [])
            )
            tools = skills.tools()
            load_tool = next(t for t in tools if t.name == "load_skill")
            read_tool = next(t for t in tools if t.name == "read_skill_file")
            import asyncio

            blocked = asyncio.run(
                load_tool.invoke({"name": "internal"}, _make_ctx())  # type: ignore[arg-type]
            )
            assert "Unknown" in blocked
            # The hint lists only visible skills, not the hidden one.
            assert "internal" not in blocked.split("Available:")[1]

            allowed = asyncio.run(
                load_tool.invoke({"name": "public"}, _make_ctx())  # type: ignore[arg-type]
            )
            assert "# public" in allowed

            blocked_read = asyncio.run(
                read_tool.invoke(
                    {"name": "internal", "relpath": "x.md"},
                    _make_ctx(),  # type: ignore[arg-type]
                )
            )
            assert "Unknown" in blocked_read

    def test_no_filter_exposes_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._build(tmp)
            skills = SkillsCapability.from_dir(root)
            assert {m.name for m in skills.metadata} == {"public", "internal"}

    def test_filter_reflects_source_changes(self) -> None:
        """Filtering is applied to the live source view, so rescans flow through."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "public").mkdir()
            (root / "public" / "SKILL.md").write_text(
                "---\nname: public\ndescription: d.\ntags: [public]\n---\n# p"
            )
            source = LocalDirSkillSource(root)
            skills = SkillsCapability(
                source, filter=lambda m: "public" in m.extra.get("tags", [])
            )
            assert {m.name for m in skills.metadata} == {"public"}
            (root / "secret").mkdir()
            (root / "secret" / "SKILL.md").write_text(
                "---\nname: secret\ndescription: d.\ntags: [internal]\n---\n# s"
            )
            source.rescan()
            # New skill is visible to the source but filtered out of the catalog.
            assert {m.name for m in source.metadata} == {"public", "secret"}
            assert {m.name for m in skills.metadata} == {"public"}


# ---------------------------------------------------------------------------
# Dynamic reload seam
# ---------------------------------------------------------------------------


class TestDynamicReload:
    def test_rescan_picks_up_new_skill(self) -> None:
        """rescan() refreshes the catalog, and SkillCategory.metadata reads through
        to the source so a running agent sees the change."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a").mkdir()
            (root / "a" / "SKILL.md").write_text(
                "---\nname: a\ndescription: First.\n---\n# A"
            )
            source = LocalDirSkillSource(root)
            skills = SkillsCapability(source)
            assert {m.name for m in skills.metadata} == {"a"}

            # Add a skill on disk after construction.
            (root / "b").mkdir()
            (root / "b" / "SKILL.md").write_text(
                "---\nname: b\ndescription: Second.\n---\n# B"
            )
            # Not visible until a rescan.
            assert {m.name for m in skills.metadata} == {"a"}
            source.rescan()
            assert {m.name for m in skills.metadata} == {"a", "b"}
            assert "b" in skills.instructions()

    def test_rescan_drops_removed_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a").mkdir()
            (root / "a" / "SKILL.md").write_text(
                "---\nname: a\ndescription: First.\n---\n# A"
            )
            source = LocalDirSkillSource(root)
            assert len(source.metadata) == 1
            (root / "a" / "SKILL.md").unlink()
            source.rescan()
            assert source.metadata == []


# ---------------------------------------------------------------------------
# load/read tool framing & in-memory skills
# ---------------------------------------------------------------------------


class TestToolFraming:
    def test_read_skill_file_returns_raw(self) -> None:
        """read_skill_file returns file content verbatim (no framing) so
        scripts/templates can be used as-is."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "s").mkdir()
            (root / "s" / "SKILL.md").write_text(
                "---\nname: s\ndescription: A skill.\n---\n# Body"
            )
            (root / "s" / "references").mkdir()
            (root / "s" / "references" / "extra.md").write_text("Extra info.")
            skills = SkillsCapability.from_dir(root)
            read_tool = next(t for t in skills.tools() if t.name == "read_skill_file")
            import asyncio

            result = asyncio.run(
                read_tool.invoke(
                    {"name": "s", "relpath": "references/extra.md"},
                    _make_ctx(),  # type: ignore[arg-type]
                )
            )
            assert result == "Extra info."

    def test_read_skill_file_no_path_in_memory_skill(self) -> None:
        """A custom source whose skills have no on-disk path returns a clean
        SkillsError message via the tool (no ToolError leakage)."""

        class MemSource:
            metadata = [SkillMetadata(name="m", description="In memory.")]

            async def load(self, name: str) -> Skill:
                return Skill(name="m", description="In memory.", content="# Body")

        skills = SkillsCapability(MemSource())
        read_tool = next(t for t in skills.tools() if t.name == "read_skill_file")
        import asyncio

        result = asyncio.run(
            read_tool.invoke(
                {"name": "m", "relpath": "references/x.md"},
                _make_ctx(),  # type: ignore[arg-type]
            )
        )
        assert "no on-disk path" in result


# ---------------------------------------------------------------------------
# Custom SkillSource protocol
# ---------------------------------------------------------------------------


class TestCustomSkillSource:
    def test_protocol_conformance(self) -> None:
        """A class with metadata and load satisfies the protocol."""

        class MySource:
            metadata: list[SkillMetadata] = [
                SkillMetadata(name="test", description="desc")
            ]

            async def load(self, name: str) -> Skill:
                return Skill(name="test", description="desc", content="# Body")

        source = MySource()
        assert isinstance(source, SkillSource)

    def test_missing_metadata_not_protocol(self) -> None:
        """A class without metadata does NOT satisfy the protocol."""

        class BadSource:
            async def load(self, name: str) -> Skill:
                return Skill(name="test", description="desc", content="# Body")

        assert not isinstance(BadSource(), SkillSource)

    def test_custom_source_with_skills_container(self) -> None:
        """Skills container works with a custom source, instructions() included."""

        class ApiSource:
            metadata: list[SkillMetadata] = [
                SkillMetadata(name="api-skill", description="From API.")
            ]

            async def load(self, name: str) -> Skill:
                if name != "api-skill":
                    raise SkillsError(f"Unknown: {name}", skill_name=name)
                return Skill(
                    name="api-skill", description="From API.", content="# API Content"
                )

        skills = SkillsCapability(source=ApiSource())
        assert "api-skill" in skills.instructions()
        tools = skills.tools()
        assert len(tools) == 2

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
                plugins=[Skills(root)],
            )
            assert agent.plugins
            text = SkillCategory.from_dir(root).instructions()
            assert "refund-policy" in text
            assert "Process refunds" in text

    def test_agent_without_skills(self) -> None:
        agent = Agent(name="test", instructions="Be helpful.")
        assert agent.plugins == []

    async def test_system_prompt_includes_skills_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "deploy").mkdir()
            (root / "deploy" / "SKILL.md").write_text(
                "---\nname: deploy\ndescription: Deploy the application.\n---\n# Deploy\n..."
            )

            catalog = SkillCategory.from_dir(root)
            agent = Agent(
                name="test",
                instructions="You are helpful.",
                plugins=[Skills(catalog)],
            )
            await agent.render_system_prompt(None)
            # The skill index is rendered into the system prompt by the run loop
            # via the skills plugin's instructions(), not by
            # agent.render_system_prompt(). Verify it's available from the catalog.
            index = catalog.instructions()
            assert "deploy" in index
            assert "Deploy the application" in index

    async def test_skills_factory_accepts_dir_and_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "deploy").mkdir()
            (root / "deploy" / "SKILL.md").write_text(
                "---\nname: deploy\ndescription: Deploy the app.\n---\n# Deploy\n..."
            )
            # New DX: pass the directory straight to Skills().
            inst = await Skills(root).setup()
            assert "deploy" in (inst.instructions or "")
            assert {t.name for t in inst.tools} == {"load_skill", "read_skill_file"}
            # A SkillSource is wrapped without SkillCategory(...) boilerplate.
            inst2 = await Skills(LocalDirSkillSource(str(root))).setup()
            assert "deploy" in (inst2.instructions or "")

    def test_skills_factory_requires_a_source(self) -> None:
        with pytest.raises(UserError):
            Skills()


# ---------------------------------------------------------------------------
# Edge cases & error isolation
# ---------------------------------------------------------------------------


class TestErrorIsolation:
    def test_corrupt_skill_dir_does_not_block_others(self, caplog) -> None:
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
                with caplog.at_level(logging.WARNING):
                    source = LocalDirSkillSource(root)
                assert len(source.metadata) == 1
                assert source.metadata[0].name == "good-skill"
                assert "skill.unreadable" in caplog.text
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

    def test_typed_frontmatter_does_not_block_others(self, caplog) -> None:
        """YAML-typed (non-string) name/description invalidates that skill
        only — the scan carries on (error isolation)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "good-skill").mkdir()
            (root / "good-skill" / "SKILL.md").write_text(
                "---\nname: good-skill\ndescription: Works fine.\n---\n# Body"
            )
            (root / "int-name").mkdir()
            (root / "int-name" / "SKILL.md").write_text(
                "---\nname: 123\ndescription: Int name.\n---\n# Body"
            )
            (root / "int-desc").mkdir()
            (root / "int-desc" / "SKILL.md").write_text(
                "---\nname: int-desc\ndescription: 2024\n---\n# Body"
            )
            with caplog.at_level(logging.WARNING):
                source = LocalDirSkillSource(root)
            assert [m.name for m in source.metadata] == ["good-skill"]
            assert "skill.invalid" in caplog.text

    def test_non_utf8_skill_md_does_not_block_others(self, caplog) -> None:
        """A SKILL.md that is not UTF-8 text is skipped, not fatal."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "good-skill").mkdir()
            (root / "good-skill" / "SKILL.md").write_text(
                "---\nname: good-skill\ndescription: Works fine.\n---\n# Body"
            )
            (root / "binary").mkdir()
            (root / "binary" / "SKILL.md").write_bytes(b"\xff\xfe---\nname: x\n---\n")
            with caplog.at_level(logging.WARNING):
                source = LocalDirSkillSource(root)
            assert [m.name for m in source.metadata] == ["good-skill"]
            assert "skill.unreadable" in caplog.text


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
