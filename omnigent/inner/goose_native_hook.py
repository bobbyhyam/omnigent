"""Goose ``PreToolUse`` hook for Omnigent policy enforcement (web approval cards).

Registered by an Open-Plugins ``hooks/hooks.json`` in the per-session Goose plugin
root that :func:`omnigent.goose_native_bridge.setup_goose_native_plugin_root`
writes. Goose runs the hook command before every tool call and pipes a JSON
payload to stdin::

    {"hook_event_name": "PreToolUse", "tool_name": "...", "tool_input": {...},
     "session_id": "...", "cwd": "..."}

To block, the hook prints ``{"decision": "block", "reason": "..."}`` to stdout
(Claude-Code-style — Goose's ``emit_blocking`` parses exactly this). Empty JSON or
``{}`` means allow.

Unlike the headless ``hermes`` hook, the per-session values are read from the
environment Goose inherits (Goose's ``hook_command`` does not clear env), so the
runner sets them on the Goose terminal rather than baking them into a wrapper:

    _OMNIGENT_SERVER_URL  : Base URL of the Omnigent server.
    _OMNIGENT_SESSION_ID  : Session / conversation ID for policy evaluation.

When either is absent (e.g. a standalone ``goose`` run that happens to discover
the plugin) the hook fails OPEN — it allows the tool — so it never breaks Goose
used outside Omnigent.
"""

from __future__ import annotations

import json
import os
import sys


def main() -> None:
    server_url = os.environ.get("_OMNIGENT_SERVER_URL", "")
    session_id = os.environ.get("_OMNIGENT_SESSION_ID", "")

    # Not driven by Omnigent (standalone goose) -> fail open (allow).
    if not server_url or not session_id:
        json.dump({}, sys.stdout)
        return

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, ValueError):
        json.dump({}, sys.stdout)
        return

    tool_name = payload.get("tool_name") or "unknown"
    tool_input = payload.get("tool_input") or {}

    eval_body: dict[str, object] = {
        "event": {
            "type": "PHASE_TOOL_CALL",
            "target": "",
            "data": {
                "name": tool_name,
                "arguments": tool_input if isinstance(tool_input, dict) else {},
            },
            "context": {},
        },
    }

    url = f"{server_url.rstrip('/')}/v1/sessions/{session_id}/policies/evaluate"

    try:
        from omnigent.native_policy_hook import post_evaluate_with_retry

        resp = post_evaluate_with_retry(
            url=url,
            headers={"Content-Type": "application/json"},
            eval_request=eval_body,
            # Must outlast the server's ``ask_timeout`` so the hook stays alive
            # while the human responds to the web-UI approval card (ASK policy).
            read_timeout=86400.0,
            hook_label="goose PreToolUse",
        )
    except Exception:  # noqa: BLE001 -- fail open on import / unexpected error
        json.dump({}, sys.stdout)
        return

    if resp is None:
        # Network error / retry budget exhausted -- fail closed so a transient
        # server outage doesn't let unreviewed tools through.
        json.dump(
            {"decision": "block", "reason": "Policy evaluation unavailable"},
            sys.stdout,
        )
        return

    try:
        result = resp.json()
    except Exception:  # noqa: BLE001
        json.dump(
            {"decision": "block", "reason": "Malformed policy response"},
            sys.stdout,
        )
        return

    action = result.get("result", "POLICY_ACTION_ALLOW")
    reason = result.get("reason", "")

    if action == "POLICY_ACTION_DENY":
        out = {"decision": "block", "reason": f"Tool '{tool_name}' denied by Omnigent policy"}
        if reason:
            out["reason"] = f"Tool '{tool_name}' denied by Omnigent policy: {reason}"
        json.dump(out, sys.stdout)
    elif action == "POLICY_ACTION_ASK":
        # The server resolves ASK by parking the request until the human decides
        # via the web-UI approval card and returning a hard ALLOW/DENY. Receiving
        # ASK here means the gate was not held -- fail closed.
        out = {"decision": "block", "reason": f"Tool '{tool_name}' requires approval"}
        if reason:
            out["reason"] = f"Tool '{tool_name}' requires approval: {reason}"
        json.dump(out, sys.stdout)
    else:
        # ALLOW or UNSPECIFIED -- empty JSON means no objection.
        json.dump({}, sys.stdout)


if __name__ == "__main__":
    main()
