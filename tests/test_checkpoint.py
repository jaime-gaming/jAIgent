"""Tests for the checkpoint / undo store."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jaigent.checkpoint import (
    MAX_CHECKPOINTS,
    MAX_SNAPSHOT_BYTES,
    AmbiguousCheckpoint,
    Checkpoint,
    CheckpointStore,
    FileState,
    checkpoint_dir,
    paths_for_tool,
)


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("JAIGENT_HOME", str(tmp_path / "home"))
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture()
def store(workspace):
    return CheckpointStore(workspace)


def snap(store, name, *, tool="write_file"):
    """Capture ``name`` with the boilerplate filled in."""
    return store.capture([Path(name)], label=f"{tool} {name}", tool=tool)


# --------------------------------------------------------------------- paths


@pytest.mark.parametrize(
    ("tool", "args", "expected"),
    [
        ("write_file", {"path": "a.md"}, [Path("a.md")]),
        ("edit_file", {"file": "b.py"}, [Path("b.py")]),
        ("delete_file", {"filename": "c.txt"}, [Path("c.txt")]),
        ("write_file", {"target": "d.txt"}, [Path("d.txt")]),
        ("read_file", {}, []),
        ("write_file", {"path": "   "}, []),
    ],
)
def test_paths_for_tool(tool, args, expected):
    assert paths_for_tool(tool, args) == expected


def test_run_command_is_never_snapshotted():
    """A shell command could touch anything, so snapshotting one path would lie."""
    assert paths_for_tool("run_command", {"path": "anything"}) == []


def test_path_keys_are_tried_in_order():
    assert paths_for_tool("write_file", {"target": "b", "path": "a"}) == [Path("a")]


def test_checkpoint_dir_lives_under_the_project_home(workspace):
    assert checkpoint_dir(workspace).name == "checkpoints"


# ------------------------------------------------------------------- capture


def test_capture_records_an_existing_file(store, workspace):
    (workspace / "a.md").write_text("before")

    cp = snap(store, "a.md")

    assert cp is not None
    assert cp.tool == "write_file"
    assert cp.files[0].existed is True
    assert cp.files[0].path == "a.md"


def test_capture_records_a_missing_file_so_undo_can_delete_it(store):
    cp = snap(store, "new.md")

    assert cp is not None
    assert cp.files[0].existed is False


def test_capture_returns_none_without_paths(store):
    assert store.capture([], label="run_command", tool="run_command") is None


def test_capture_ignores_paths_outside_the_workspace(store, tmp_path):
    outside = tmp_path / "elsewhere.md"
    outside.write_text("not yours")

    assert store.capture([outside], label="write", tool="write_file") is None


def test_capture_marks_oversized_files_as_skipped(store, workspace):
    (workspace / "big.bin").write_bytes(b"x" * (MAX_SNAPSHOT_BYTES + 1))

    cp = snap(store, "big.bin")

    assert cp.files[0].skipped is True
    assert cp.restorable == []


def test_identical_content_is_stored_once(store, workspace):
    (workspace / "a.md").write_text("same bytes")
    (workspace / "b.md").write_text("same bytes")

    snap(store, "a.md")
    snap(store, "b.md")

    blobs = list((checkpoint_dir(store.workspace) / "objects").iterdir())
    assert len(blobs) == 1


# ------------------------------------------------------------------- restore


def test_restore_reverts_an_edit(store, workspace):
    target = workspace / "a.md"
    target.write_text("original")

    cp = snap(store, "a.md")
    target.write_text("clobbered")

    assert store.restore(cp) == ["a.md"]
    assert target.read_text() == "original"


def test_restore_deletes_a_file_that_did_not_exist(store, workspace):
    cp = snap(store, "new.md")
    created = workspace / "new.md"
    created.write_text("hello")

    store.restore(cp)

    assert not created.exists()


def test_restore_recreates_a_deleted_file(store, workspace):
    target = workspace / "gone.md"
    target.write_text("keep me")

    cp = snap(store, "gone.md", tool="delete_file")
    target.unlink()
    store.restore(cp)

    assert target.read_text() == "keep me"


def test_restore_reports_nothing_when_the_file_is_untouched(store, workspace):
    (workspace / "a.md").write_text("v1")

    assert store.restore(snap(store, "a.md")) == []


def test_restore_is_idempotent(store, workspace):
    target = workspace / "a.md"
    target.write_text("v1")
    cp = snap(store, "a.md")
    target.write_text("v2")

    store.restore(cp)
    store.restore(cp)

    assert target.read_text() == "v1"


def test_restore_preserves_binary_content(store, workspace):
    blob = bytes(range(256))
    target = workspace / "img.bin"
    target.write_bytes(blob)

    cp = snap(store, "img.bin")
    target.write_bytes(b"corrupt")
    store.restore(cp)

    assert target.read_bytes() == blob


def test_restore_skips_oversized_files(store, workspace):
    target = workspace / "big.bin"
    target.write_bytes(b"x" * (MAX_SNAPSHOT_BYTES + 1))
    cp = snap(store, "big.bin")
    target.write_bytes(b"small now")

    assert store.restore(cp) == []
    assert target.read_bytes() == b"small now"


def test_restore_recreates_missing_parent_directories(store, workspace):
    nested = workspace / "deep" / "a.md"
    nested.parent.mkdir()
    nested.write_text("v1")

    cp = store.capture([nested], label="write", tool="write_file")
    import shutil

    shutil.rmtree(workspace / "deep")
    store.restore(cp)

    assert nested.read_text() == "v1"


def test_restore_tolerates_a_pruned_object(store, workspace):
    target = workspace / "a.md"
    target.write_text("v1")
    cp = snap(store, "a.md")
    for blob in (checkpoint_dir(store.workspace) / "objects").iterdir():
        blob.unlink()
    target.write_text("v2")

    assert store.restore(cp) == []


# ------------------------------------------------------------------ ordering


def test_ids_are_unique_even_within_one_millisecond(store):
    """Regression: timestamp-derived ids collided, so undo rewound the wrong step."""
    ids = [snap(store, f"f{i}.md").id for i in range(25)]

    assert len(set(ids)) == 25


def test_list_is_newest_first(store):
    made = [snap(store, f"f{i}.md").id for i in range(5)]

    assert [cp.id for cp in store.history()] == list(reversed(made))


def test_capture_stamps_move_forward_when_the_clock_does_not(store, monkeypatch):
    """Windows can return the same time.time() for several captures."""
    monkeypatch.setattr("jaigent.checkpoint.time.time", lambda: 1_700_000_000.0)
    first = snap(store, "a.md")
    second = snap(store, "b.md")

    assert second.created > first.created
    assert store.latest().id == second.id


def test_list_is_newest_first_when_timestamps_collide(store):
    """Windows time.time() often returns the same value for rapid captures.

    history() used to sort only on created, so equal timestamps kept load
    order (oldest first) and undo walked the wrong way.
    """
    made = [snap(store, f"f{i}.md") for i in range(5)]
    stamp = made[0].created
    rewritten = store._load()
    for checkpoint in rewritten:
        checkpoint.created = stamp
    store._save(rewritten)

    assert [cp.id for cp in store.history()] == list(reversed([c.id for c in made]))


def test_list_honours_the_limit(store):
    for i in range(6):
        snap(store, f"f{i}.md")

    assert len(store.history(limit=2)) == 2


def test_latest_returns_the_newest(store):
    snap(store, "a.md")
    last = snap(store, "b.md")

    assert store.latest().id == last.id


def test_latest_is_none_when_empty(store):
    assert store.latest() is None


def test_get_matches_an_exact_id(store):
    cp = snap(store, "a.md")

    assert store.get(cp.id).id == cp.id


def test_get_matches_an_id_prefix(store):
    cp = snap(store, "a.md")

    assert store.get(cp.id[:4]).id == cp.id


def test_get_returns_none_for_an_unknown_id(store):
    snap(store, "a.md")

    assert store.get("zzzzzz") is None


# ----------------------------------------------------------- pruning / clear


def test_pruning_caps_the_history(store):
    for i in range(MAX_CHECKPOINTS + 12):
        snap(store, f"f{i}.md")

    assert len(store.history(limit=1000)) <= MAX_CHECKPOINTS


def test_pruning_keeps_the_newest_when_timestamps_collide(store):
    """A full store with one clock tick must drop the oldest inserts, not a random slice."""
    stamp = 1_700_000_000.0
    made = [
        Checkpoint(id=f"cp{i:03d}", label="l", created=stamp) for i in range(MAX_CHECKPOINTS + 5)
    ]

    kept = store._prune(made)

    assert [c.id for c in kept] == [c.id for c in made[-MAX_CHECKPOINTS:]]


def test_pruning_drops_unreferenced_objects(store, workspace):
    for i in range(MAX_CHECKPOINTS + 5):
        (workspace / f"f{i}.md").write_text(f"contents {i}")
        snap(store, f"f{i}.md")

    blobs = list((checkpoint_dir(store.workspace) / "objects").iterdir())
    assert len(blobs) <= MAX_CHECKPOINTS


def test_clear_removes_everything(store):
    snap(store, "a.md")

    assert store.clear() >= 1
    assert store.history() == []


def test_clear_on_an_empty_store_is_harmless(store):
    assert store.clear() == 0


def test_size_grows_after_a_capture(store, workspace):
    (workspace / "a.md").write_text("x" * 400)
    assert store.size() == 0

    snap(store, "a.md")

    assert store.size() >= 400


# ------------------------------------------------------------------ metadata


def test_diff_summary_reports_a_pending_delete(store, workspace):
    cp = snap(store, "new.md")
    (workspace / "new.md").write_text("created")

    assert store.diff_summary(cp) == [("new.md", "delete")]


def test_diff_summary_reports_a_pending_revert(store, workspace):
    (workspace / "a.md").write_text("v1")
    cp = snap(store, "a.md")
    (workspace / "a.md").write_text("v2")

    assert store.diff_summary(cp) == [("a.md", "revert")]


def test_diff_summary_reports_a_pending_recreate(store, workspace):
    (workspace / "a.md").write_text("v1")
    cp = snap(store, "a.md")
    (workspace / "a.md").unlink()

    assert store.diff_summary(cp) == [("a.md", "recreate")]


def test_diff_summary_marks_untouched_files_unchanged(store, workspace):
    (workspace / "a.md").write_text("same")

    assert store.diff_summary(snap(store, "a.md")) == [("a.md", "unchanged")]


def test_diff_summary_flags_skipped_files(store, workspace):
    (workspace / "big.bin").write_bytes(b"x" * (MAX_SNAPSHOT_BYTES + 1))

    assert store.diff_summary(snap(store, "big.bin")) == [("big.bin", "skipped (too large)")]


def test_summary_mentions_the_file(store, workspace):
    (workspace / "a.md").write_text("x")

    assert "a.md" in snap(store, "a.md").summary()


def test_summary_truncates_a_long_file_list():
    cp = Checkpoint(id="x", label="l", files=[FileState(path=f"f{i}.md") for i in range(5)])

    assert cp.summary().endswith("+2")


def test_summary_of_an_empty_checkpoint():
    assert Checkpoint(id="x", label="l").summary() == "(no files)"


@pytest.mark.parametrize(
    ("seconds_ago", "expected"),
    [(0, "just now"), (30, "just now"), (120, "2m ago"), (7200, "2h ago"), (172800, "2d ago")],
)
def test_age_is_human_readable(seconds_ago, expected):
    import time

    assert Checkpoint(id="x", label="l", created=time.time() - seconds_ago).age() == expected


# ---------------------------------------------------------------- durability


def test_history_survives_a_new_store_instance(store, workspace):
    target = workspace / "a.md"
    target.write_text("v1")
    cp = snap(store, "a.md")

    reopened = CheckpointStore(workspace)
    target.write_text("v2")
    reopened.restore(reopened.get(cp.id))

    assert target.read_text() == "v1"


def test_a_corrupt_index_does_not_crash(store, workspace):
    snap(store, "a.md")
    (checkpoint_dir(workspace) / "index.json").write_text("{ not json")

    assert CheckpointStore(workspace).history() == []


def test_a_malformed_entry_is_skipped_not_fatal(store, workspace):
    snap(store, "a.md")
    index = checkpoint_dir(workspace) / "index.json"
    payload = json.loads(index.read_text())
    payload["checkpoints"].append("this is not an object")
    index.write_text(json.dumps(payload))

    assert len(CheckpointStore(workspace).history()) >= 1


def test_index_records_a_version(store):
    snap(store, "a.md")

    payload = json.loads((checkpoint_dir(store.workspace) / "index.json").read_text())
    assert "version" in payload


def test_checkpoint_survives_a_roundtrip_through_json(store, workspace):
    (workspace / "a.md").write_text("x")
    cp = snap(store, "a.md")

    revived = Checkpoint.from_dict(json.loads(json.dumps(cp.to_dict())))

    assert revived.id == cp.id
    assert revived.files[0].digest == cp.files[0].digest


def test_file_state_roundtrip():
    state = FileState(path="a.md", digest="abc", size=3, skipped=True)

    assert FileState.from_dict(state.to_dict()) == state


class TestDiscard:
    """undo consumes a checkpoint, so undoing twice steps back twice."""

    def test_discard_removes_it_from_the_history(self, store, workspace):
        cp = snap(store, "a.md")

        assert store.discard(cp) is True
        assert store.history() == []

    def test_discard_returns_false_for_an_unknown_checkpoint(self, store):
        assert store.discard(Checkpoint(id="nope", label="l")) is False

    def test_latest_moves_back_after_a_discard(self, store):
        first = snap(store, "a.md")
        second = snap(store, "b.md")

        store.discard(second)

        assert store.latest().id == first.id

    def test_sequential_undo_walks_back_through_history(self, store, workspace):
        """Regression: undo always restored latest(), so it never advanced."""
        target = workspace / "a.md"
        target.write_text("v1")
        first = snap(store, "a.md")
        target.write_text("v2")
        second = snap(store, "a.md")
        target.write_text("v3")

        store.restore(second)
        store.discard(second)
        assert target.read_text() == "v2"

        store.restore(first)
        store.discard(first)
        assert target.read_text() == "v1"

        assert store.latest() is None

    def test_objects_still_referenced_are_kept(self, store, workspace):
        """Two checkpoints of identical content share one object."""
        (workspace / "a.md").write_text("shared")
        (workspace / "b.md").write_text("shared")
        first = snap(store, "a.md")
        second = snap(store, "b.md")

        store.discard(second)

        assert store.restore(first) == [] or (workspace / "a.md").read_text() == "shared"
        assert len(list((checkpoint_dir(store.workspace) / "objects").iterdir())) == 1

    def test_unreferenced_objects_are_cleaned_up(self, store, workspace):
        (workspace / "a.md").write_text("unique content")
        cp = snap(store, "a.md")

        store.discard(cp)

        assert list((checkpoint_dir(store.workspace) / "objects").iterdir()) == []


class TestAmbiguousPrefix:
    """Restoring is destructive, so an ambiguous id must not be guessed at."""

    def test_an_ambiguous_prefix_raises(self, store):
        for i in range(3):
            snap(store, f"f{i}.md")
        shared = store.history()[0].id[:2]

        with pytest.raises(AmbiguousCheckpoint) as excinfo:
            store.get(shared)

        assert "matches" in str(excinfo.value)

    def test_the_error_lists_the_candidates(self, store):
        for i in range(3):
            snap(store, f"f{i}.md")
        ids = {c.id for c in store.history()}

        with pytest.raises(AmbiguousCheckpoint) as excinfo:
            store.get(next(iter(ids))[:2])

        assert any(i in str(excinfo.value) for i in ids)

    def test_the_error_caps_how_many_it_lists(self, store):
        for i in range(9):
            snap(store, f"f{i}.md")

        with pytest.raises(AmbiguousCheckpoint, match="more"):
            store.get(store.history()[0].id[:2])

    def test_an_exact_id_wins_over_a_prefix_match(self, store, workspace):
        """An id that is also a prefix of others must still resolve exactly."""
        for i in range(3):
            snap(store, f"f{i}.md")
        exact = sorted(c.id for c in store.history())[0]

        assert store.get(exact).id == exact

    def test_a_unique_prefix_still_works(self, store):
        cp = snap(store, "a.md")

        assert store.get(cp.id).id == cp.id


class TestOnlyMutatingToolsSnapshot:
    """A read-only tool must not put anything in the undo history.

    `paths_for_tool` matched on the *argument* name, so `list_files(path=".")`
    and `read_file(path="x")` each produced a checkpoint. The timeline filled
    with entries that revert nothing, and `undo` had to be pressed once per
    read before it reached a real change.
    """

    @pytest.mark.parametrize("tool", ["list_files", "read_file", "search_files"])
    def test_read_only_tools_are_not_snapshotted(self, tool: str) -> None:
        assert paths_for_tool(tool, {"path": "notes.md"}) == []

    @pytest.mark.parametrize("tool", ["write_file", "edit_file", "delete_file"])
    def test_mutating_tools_still_are(self, tool: str) -> None:
        assert paths_for_tool(tool, {"path": "notes.md"}) == [Path("notes.md")]

    def test_an_unknown_tool_is_not_snapshotted(self) -> None:
        # Guessing from an argument name is what caused the bug.
        assert paths_for_tool("some_new_tool", {"path": "notes.md"}) == []

    def test_the_set_matches_the_approval_policy(self) -> None:
        # One source of truth: a tool that needs approval is one worth undoing.
        from jaigent.approval import MUTATING_TOOLS

        snapshotted = {tool for tool in MUTATING_TOOLS if paths_for_tool(tool, {"path": "x"})}
        assert snapshotted == MUTATING_TOOLS - {"run_command"}
