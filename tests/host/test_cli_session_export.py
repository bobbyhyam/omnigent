"""Unit tests for ``omnigent session export``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from omnigent.cli import cli
from omnigent.entities.conversation import MessageData, NewConversationItem
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


def _add_items(store: SqlAlchemyConversationStore, conv_id: str) -> None:
    store.append(
        conv_id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_1",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "hello"}],
                ),
            ),
            NewConversationItem(
                type="message",
                response_id="resp_1",
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": "hi there"}],
                    agent="my-agent",
                ),
            ),
        ],
    )


def test_session_export_writes_jsonl(db_uri: str, tmp_path: Path) -> None:
    """Export writes one session_meta line then one item line per item."""
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(kind="default", title="test session")
    _add_items(store, conv.id)

    out_file = tmp_path / "out.jsonl"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["session", "export", "--id", conv.id, "--output", str(out_file), "--database-uri", db_uri],
    )

    assert result.exit_code == 0, result.output
    assert out_file.exists()

    lines = [json.loads(l) for l in out_file.read_text().splitlines() if l]
    assert len(lines) == 3  # 1 meta + 2 items

    meta = lines[0]
    assert meta["record_type"] == "session_meta"
    assert meta["id"] == conv.id
    assert meta["title"] == "test session"

    item_lines = lines[1:]
    assert all(r["record_type"] == "item" for r in item_lines)
    roles = [r.get("role") for r in item_lines]
    assert roles == ["user", "assistant"]
    assert item_lines[1]["content"] == [{"type": "output_text", "text": "hi there"}]


def test_session_export_default_filename(db_uri: str, tmp_path: Path) -> None:
    """Without --output, the file is named <session_id>.jsonl in cwd."""
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(kind="default")

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(
            cli,
            ["session", "export", "--id", conv.id, "--database-uri", db_uri],
        )
        assert result.exit_code == 0, result.output
        default_path = Path(f"{conv.id}.jsonl")
        assert default_path.exists()
        lines = [json.loads(l) for l in default_path.read_text().splitlines() if l]
    # session_meta line only (no items added)
    assert len(lines) == 1
    assert lines[0]["record_type"] == "session_meta"
    assert lines[0]["id"] == conv.id


def test_session_export_missing_session_errors(db_uri: str, tmp_path: Path) -> None:
    """Export of an unknown session id exits non-zero with a clear message."""
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "session",
            "export",
            "--id",
            "conv_doesnotexist",
            "--output",
            str(tmp_path / "out.jsonl"),
            "--database-uri",
            db_uri,
        ],
    )
    assert result.exit_code != 0
    assert "conv_doesnotexist" in result.output


def test_session_export_items_ordered_ascending(db_uri: str, tmp_path: Path) -> None:
    """Items in the JSONL appear in ascending position order."""
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(kind="default")
    _add_items(store, conv.id)

    out_file = tmp_path / "ordered.jsonl"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["session", "export", "--id", conv.id, "--output", str(out_file), "--database-uri", db_uri],
    )
    assert result.exit_code == 0, result.output

    records = [json.loads(l) for l in out_file.read_text().splitlines() if l]
    item_records = [r for r in records if r["record_type"] == "item"]
    ids = [r["id"] for r in item_records]
    # items should be distinct and match what the store holds
    assert len(ids) == 2
    assert ids[0] != ids[1]
    # first item is user, second is assistant
    assert item_records[0]["role"] == "user"
    assert item_records[1]["role"] == "assistant"
