"""Unit tests for the goose-native approval mirror's pane parser."""

from __future__ import annotations

import asyncio

import pytest

import omnigent.goose_native_permissions as gp
from omnigent.goose_native_permissions import (
    goose_permission_elicitation_id,
    parse_goose_approval_prompt,
)

# cliclack radio with "Always Allow" → Deny is the 3rd item (2 downs from Allow).
_THREE_ITEM = (
    "│ developer__shell\n"
    "│ command: rm -rf /tmp/x\n"
    "◆ Goose would like to call the above tool, do you allow?\n"
    "│ ● Allow          Allow the tool call once\n"
    "│ ○ Always Allow   Always allow the tool call\n"
    "│ ○ Deny           Deny the tool call\n"
    "│ ○ Cancel         Cancel the AI response and tool call\n"
)

# Security-prompt variant: no "Always Allow" → Deny is the 2nd item (1 down).
_TWO_ITEM = (
    "⚠ this command writes files\n"
    "◆ Do you allow this tool call?\n"
    "│ ● Allow   Allow the tool call once\n"
    "│ ○ Deny    Deny the tool call\n"
    "│ ○ Cancel  Cancel the AI response and tool call\n"
)


def test_parses_three_item_prompt_and_deny_index() -> None:
    prompt = parse_goose_approval_prompt(_THREE_ITEM)
    assert prompt is not None
    # Allow(0) → Always Allow(1) → Deny(2): two Down presses.
    assert prompt.deny_down_count == 2
    # Subject is scraped from the tool-request lines above the question.
    assert "developer__shell" in prompt.subject
    assert prompt.block_hash


def test_parses_two_item_prompt_and_deny_index() -> None:
    prompt = parse_goose_approval_prompt(_TWO_ITEM)
    assert prompt is not None
    # Allow(0) → Deny(1): one Down press.
    assert prompt.deny_down_count == 1


def test_requires_question_and_both_items() -> None:
    # Question but no Deny item → not a confirmation block.
    assert parse_goose_approval_prompt("◆ do you allow?\n│ ● Allow\n") is None
    # Items but no question → not live.
    assert parse_goose_approval_prompt("│ ● Allow\n│ ○ Deny\n") is None
    assert parse_goose_approval_prompt("") is None


def test_block_hash_differs_per_tool_and_id_is_deterministic() -> None:
    a = parse_goose_approval_prompt(_THREE_ITEM)
    other = _THREE_ITEM.replace("rm -rf /tmp/x", "cat /etc/passwd")
    b = parse_goose_approval_prompt(other)
    assert a is not None and b is not None
    assert a.block_hash != b.block_hash
    eid = goose_permission_elicitation_id("conv_9", a.block_hash)
    assert eid == goose_permission_elicitation_id("conv_9", a.block_hash)
    assert eid.startswith("elicit_goose_conv_9_")


# --- policy enforcement (store read → /policies/evaluate verdict → cliclack) ---


import re  # noqa: E402 — local to the plumbing tests below

from omnigent.goose_native_forwarder import PendingToolCall  # noqa: E402


class _Resp:
    def __init__(self, status: int = 200, content: bytes | None = None, payload=None):
        self.status_code = status
        self._payload = payload
        if content is not None:
            self.content = content
        elif payload is not None:
            import json as _json

            self.content = _json.dumps(payload).encode()
        else:
            self.content = b""
        self.text = self.content.decode() if isinstance(self.content, bytes) else str(self.content)

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _RoutingClient:
    """Fake httpx client that routes a POST to a response by URL substring."""

    def __init__(self, routes: dict[str, _Resp], *, raise_on: str | None = None) -> None:
        self._routes = routes
        self._raise_on = raise_on
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url, json=None, **_kwargs):
        self.posts.append((url, json or {}))
        if self._raise_on is not None and self._raise_on in url:
            import httpx

            raise httpx.ConnectError("boom")
        for sub, resp in self._routes.items():
            if sub in url:
                return resp
        raise AssertionError(f"no fake route for {url}")


def _gprompt(deny_downs: int = 2) -> gp.GooseApprovalPrompt:
    return gp.GooseApprovalPrompt(
        subject="shell rm -rf x",
        message="m",
        preview="shell",
        deny_down_count=deny_downs,
        block_hash="h",
    )


def _call(name: str = "shell", args: dict | None = None) -> PendingToolCall:
    return PendingToolCall(
        request_id="t1",
        name=name,
        arguments=args if args is not None else {"command": "ls"},
        extension="developer",
    )


async def _run(client, tmp_path, *, prompt=None, holder=None):
    """Invoke ``_run_one_approval`` with the structured-read path wired up."""
    await gp._run_one_approval(
        client,
        session_id="c",
        bridge_dir=tmp_path,
        prompt=prompt or _gprompt(),
        db_path=tmp_path,
        goose_session_id="gs1",
        episode=1,
        holder=holder if holder is not None else {},
    )


def test_evaluate_elicitation_id_matches_server_contract() -> None:
    eid = gp._evaluate_elicitation_id("conv_9", 3)
    assert eid == gp._evaluate_elicitation_id("conv_9", 3)  # deterministic
    assert re.fullmatch(r"elicit_evaluate_[0-9a-f]{32}", eid)


async def test_policy_allow_presses_enter_and_posts_tool_call(tmp_path, monkeypatch) -> None:
    sent: list[tuple] = []
    monkeypatch.setattr(gp, "send_goose_pane_keys", lambda _bd, *keys: sent.append(keys))
    monkeypatch.setattr(gp, "read_pending_tool_request", lambda *a, **k: _call())
    client = _RoutingClient(
        {"/policies/evaluate": _Resp(payload={"result": "POLICY_ACTION_ALLOW"})}
    )
    await _run(client, tmp_path)
    assert sent == [("Enter",)]  # Allow is the default-highlighted item
    url, body = client.posts[0]
    assert url.endswith("/policies/evaluate")
    assert body["event"]["type"] == "PHASE_TOOL_CALL"
    assert body["event"]["data"] == {"name": "shell", "arguments": {"command": "ls"}}
    assert body["_omnigent_elicitation_id"].startswith("elicit_evaluate_")


async def test_policy_unspecified_allows(tmp_path, monkeypatch) -> None:
    sent: list[tuple] = []
    monkeypatch.setattr(gp, "send_goose_pane_keys", lambda _bd, *keys: sent.append(keys))
    monkeypatch.setattr(gp, "read_pending_tool_request", lambda *a, **k: _call())
    client = _RoutingClient(
        {"/policies/evaluate": _Resp(payload={"result": "POLICY_ACTION_UNSPECIFIED"})}
    )
    await _run(client, tmp_path)
    assert sent == [("Enter",)]  # no agent / no policy matched → let it run


async def test_policy_deny_walks_to_deny(tmp_path, monkeypatch) -> None:
    sent: list[tuple] = []
    monkeypatch.setattr(gp, "send_goose_pane_keys", lambda _bd, *keys: sent.append(keys))
    monkeypatch.setattr(gp, "read_pending_tool_request", lambda *a, **k: _call())
    client = _RoutingClient(
        {"/policies/evaluate": _Resp(payload={"result": "POLICY_ACTION_DENY"})}
    )
    await _run(client, tmp_path, prompt=_gprompt(deny_downs=2))
    assert sent == [("Down", "Down", "Enter")]  # Allow → Always Allow → Deny


async def test_policy_http_error_fails_closed_to_deny(tmp_path, monkeypatch) -> None:
    sent: list[tuple] = []
    monkeypatch.setattr(gp, "send_goose_pane_keys", lambda _bd, *keys: sent.append(keys))
    monkeypatch.setattr(gp, "read_pending_tool_request", lambda *a, **k: _call())
    client = _RoutingClient({"/policies/evaluate": _Resp(status=503, content=b"down")})
    await _run(client, tmp_path, prompt=_gprompt(deny_downs=1))
    assert sent == [("Down", "Enter")]  # fail closed → Deny


async def test_policy_transport_error_fails_closed_to_deny(tmp_path, monkeypatch) -> None:
    sent: list[tuple] = []
    monkeypatch.setattr(gp, "send_goose_pane_keys", lambda _bd, *keys: sent.append(keys))
    monkeypatch.setattr(gp, "read_pending_tool_request", lambda *a, **k: _call())
    client = _RoutingClient({}, raise_on="/policies/evaluate")
    await _run(client, tmp_path, prompt=_gprompt(deny_downs=1))
    assert sent == [("Down", "Enter")]  # transport failure → fail closed → Deny


async def test_no_pending_tool_falls_back_to_blind_ask(tmp_path, monkeypatch) -> None:
    sent: list[tuple] = []
    monkeypatch.setattr(gp, "send_goose_pane_keys", lambda _bd, *keys: sent.append(keys))
    monkeypatch.setattr(gp, "read_pending_tool_request", lambda *a, **k: None)
    monkeypatch.setattr(gp, "_PENDING_READ_RETRIES", 1)
    monkeypatch.setattr(gp, "_PENDING_READ_RETRY_DELAY_S", 0.0)
    client = _RoutingClient(
        {"/hooks/native-permission-request": _Resp(payload={"action": "accept"})}
    )
    await _run(client, tmp_path)
    assert sent == [("Enter",)]
    assert client.posts[0][0].endswith("/hooks/native-permission-request")


async def test_blind_ask_external_resolution_drives_nothing(tmp_path, monkeypatch) -> None:
    sent: list[tuple] = []
    monkeypatch.setattr(gp, "send_goose_pane_keys", lambda _bd, *keys: sent.append(keys))
    monkeypatch.setattr(gp, "read_pending_tool_request", lambda *a, **k: None)
    monkeypatch.setattr(gp, "_PENDING_READ_RETRIES", 1)
    monkeypatch.setattr(gp, "_PENDING_READ_RETRY_DELAY_S", 0.0)
    # Empty body = resolved in the TUI / timed out → no drive.
    client = _RoutingClient({"/hooks/native-permission-request": _Resp(content=b"")})
    await _run(client, tmp_path)
    assert sent == []


async def test_post_external_elicitation_resolved_targets_events(tmp_path) -> None:
    client = _RoutingClient({"/events": _Resp(status=200, content=b"")})
    await gp._post_external_elicitation_resolved(client, "conv_g", "e3")
    url, body = client.posts[0]
    assert url == "/v1/sessions/conv_g/events"
    assert body["type"] == "external_elicitation_resolved"


async def test_supervise_one_episode_per_prompt(tmp_path, monkeypatch) -> None:
    panes = [_THREE_ITEM, _THREE_ITEM, None]
    seq = {"i": 0}

    def _cap(_bd):
        i = seq["i"]
        seq["i"] += 1
        return panes[i] if i < len(panes) else None

    monkeypatch.setattr(gp, "capture_goose_pane", _cap)
    monkeypatch.setattr(gp, "_resolve_goose_session_id", lambda _db, _name: "gs1")
    created: list[int] = []

    async def _fake_run_one(
        _client, *, session_id, bridge_dir, prompt, db_path, goose_session_id, episode, holder
    ):
        created.append(episode)

    monkeypatch.setattr(gp, "_run_one_approval", _fake_run_one)

    sleeps = {"n": 0}

    async def _sleep(_s):
        sleeps["n"] += 1
        if sleeps["n"] >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(gp.asyncio, "sleep", _sleep)

    with pytest.raises(asyncio.CancelledError):
        await gp.supervise_goose_approval_mirror(
            base_url="http://x",
            headers={},
            session_id="c",
            bridge_dir=tmp_path,
            goose_session_name="c-123",
        )
    assert len(created) == 1
