"""Notes captured from Discord: persisted, folded in once, then left alone."""

from __future__ import annotations

from pathlib import Path

import pytest

from fpl_buddy.notes import FileNoteStore, build_note_store


@pytest.fixture
def notes(tmp_path: Path) -> FileNoteStore:
    return FileNoteStore(tmp_path / "notes.json")


def test_a_fresh_store_has_nothing_pending(notes):
    assert notes.pending() == []


def test_added_notes_are_pending(notes):
    note = notes.add("jishnu", "bench Vasquez, he's got a knock")
    assert [n.id for n in notes.pending()] == [note.id]
    assert notes.pending()[0].text == "bench Vasquez, he's got a knock"
    assert notes.pending()[0].author == "jishnu"


def test_consumed_notes_drop_out_of_pending(notes):
    first = notes.add("jishnu", "roll the transfer")
    second = notes.add("jishnu", "captain Oakley")
    notes.mark_consumed([first.id])
    assert [n.id for n in notes.pending()] == [second.id]


def test_marking_an_empty_list_consumed_is_a_no_op(notes):
    notes.add("jishnu", "note")
    notes.mark_consumed([])
    assert len(notes.pending()) == 1


def test_notes_survive_a_reload_from_disk(tmp_path):
    path = tmp_path / "notes.json"
    FileNoteStore(path).add("jishnu", "one to remember")
    reloaded = FileNoteStore(path)
    assert reloaded.pending()[0].text == "one to remember"


def test_a_corrupt_notes_file_is_treated_as_empty_not_fatal(tmp_path):
    path = tmp_path / "notes.json"
    path.write_text("not json at all")
    assert FileNoteStore(path).pending() == []


# -------------------------------------------------------------------- selection


def test_build_note_store_defaults_to_file(settings):
    store = build_note_store(settings)
    assert isinstance(store, FileNoteStore)
    assert store.path == Path(settings.state_dir) / "notes.json"


def test_azure_table_backend_needs_a_connection_string(settings):
    settings.state_backend = "azure_table"
    with pytest.raises(RuntimeError, match="AZURE_TABLE_CONNECTION_STRING"):
        build_note_store(settings)
