"""Phase 2: persistent memory document tests."""
from __future__ import annotations

from pathlib import Path

from node.memory import MemoryDoc


def test_save_creates_file(tmp_path: Path):
    p = tmp_path / "data" / "saturn.md"
    mem = MemoryDoc("saturn", path=p)
    mem.save()
    assert p.exists()
    assert "# Node: saturn" in p.read_text()


def test_save_persists_table_section(tmp_path: Path):
    p = tmp_path / "saturn.md"
    mem = MemoryDoc("saturn", path=p)
    mem.update_table_section(
        "users",
        "## Table: users\n| id | name |\n|----|------|\n| 1  | Alice |",
    )
    mem.save()
    # Reload from file
    mem2 = MemoryDoc("saturn", path=p)
    assert "Alice" in mem2.get_text()


def test_save_is_atomic(tmp_path: Path):
    """No .tmp file left after save."""
    p = tmp_path / "saturn.md"
    mem = MemoryDoc("saturn", path=p)
    mem.save()
    assert not (tmp_path / "saturn.tmp").exists()


def test_no_path_save_is_noop():
    mem = MemoryDoc("saturn")
    mem.save()  # must not raise


def test_reload_preserves_schema(tmp_path: Path):
    p = tmp_path / "saturn.md"
    mem = MemoryDoc("saturn", path=p)
    mem.update_schema_section("## Schema\n- orders: id INTEGER, amount REAL")
    mem.save()
    mem2 = MemoryDoc("saturn", path=p)
    assert "orders: id INTEGER" in mem2.get_text()


def test_save_overwrites_on_update(tmp_path: Path):
    p = tmp_path / "saturn.md"
    mem = MemoryDoc("saturn", path=p)
    mem.update_table_section("users", "## Table: users\n| id |\n|----|\n| 1  |")
    mem.save()
    mem.update_table_section("users", "## Table: users\n| id |\n|----|\n| 1  |\n| 2  |")
    mem.save()
    content = p.read_text()
    assert "| 2  |" in content
    assert content.count("## Table: users") == 1
