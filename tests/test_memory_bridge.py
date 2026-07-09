"""Tests for Hermes memory bridge."""
from __future__ import annotations


from memory_bridge import import_hermes, hermes_available, _parse_section_entries


class TestParseEntries:
    def test_basic_split(self):
        text = "First entry about Python setup\n§\nSecond entry about Docker config"
        entries = _parse_section_entries(text)
        assert len(entries) == 2
        assert "Python setup" in entries[0]
        assert "Docker config" in entries[1]

    def test_skips_short_entries(self):
        text = "Good entry about something useful\n§\nshort\n§\nAnother good entry here"
        entries = _parse_section_entries(text)
        assert len(entries) == 2

    def test_empty_text(self):
        assert _parse_section_entries("") == []

    def test_single_entry(self):
        entries = _parse_section_entries("Just one entry about the project setup and conventions")
        assert len(entries) == 1


class TestHermesImport:
    def test_import_memory_md(self, tmp_path):
        # Create fake hermes dir
        hermes_dir = tmp_path / "hermes"
        (hermes_dir / "memories").mkdir(parents=True)
        (hermes_dir / "memories" / "MEMORY.md").write_text(
            "User's project uses Rust with Axum framework\n§\n"
            "Machine runs Ubuntu 22.04 with Docker installed"
        )

        charon_dir = tmp_path / "charon_state"
        charon_dir.mkdir()

        stats = import_hermes(charon_dir, hermes_dir, import_messages=False)
        assert stats["memory_entries"] == 2
        assert stats["user_entries"] == 0

        # Verify memories are searchable
        from memory_engine import MemoryEngine
        engine = MemoryEngine(charon_dir)
        result = engine.recall("Rust framework")
        assert len(result.memories) > 0
        assert any("Rust" in m.memory.content for m in result.memories)
        engine.close()

    def test_import_user_md(self, tmp_path):
        hermes_dir = tmp_path / "hermes"
        (hermes_dir / "memories").mkdir(parents=True)
        (hermes_dir / "memories" / "USER.md").write_text(
            "Prefers concise responses over verbose explanations\n§\n"
            "Uses Vim keybindings in all editors"
        )

        charon_dir = tmp_path / "charon_state"
        charon_dir.mkdir()

        stats = import_hermes(charon_dir, hermes_dir, import_messages=False)
        assert stats["user_entries"] == 2

        # User entries should be static
        from memory_engine import MemoryEngine
        engine = MemoryEngine(charon_dir)
        result = engine.recall("editor preferences", include_profile=True)
        assert len(result.profile_static) > 0
        engine.close()

    def test_import_with_no_hermes(self, tmp_path):
        charon_dir = tmp_path / "charon_state"
        charon_dir.mkdir()
        nonexistent = tmp_path / "no_hermes_here"

        stats = import_hermes(charon_dir, nonexistent, import_messages=False)
        assert stats["memory_entries"] == 0
        assert stats["user_entries"] == 0

    def test_hermes_available(self, tmp_path):
        assert hermes_available(tmp_path) is False

        (tmp_path / "memories").mkdir()
        (tmp_path / "memories" / "MEMORY.md").write_text("test entry here for checking")
        assert hermes_available(tmp_path) is True
