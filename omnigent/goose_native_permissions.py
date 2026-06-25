"""Goose-native Omnigent-policy enforcement point (TUI gate → Omnigent policy).

Goose has no tool-hook system, so Omnigent cannot register a pre-tool callback
the way Claude-/Hermes-native do. Instead this turns goose's own in-terminal
approval gate into an Omnigent policy checkpoint. The runner launches goose with
``GOOSE_MODE=approve`` (NOT ``smart_approve``), so goose's ``cliclack`` selector
(``prompt_tool_confirmation``) fires before **every** tool — nothing is
auto-allowed inside goose, so nothing bypasses this gate. The mirror then:

1. polls ``capture-pane`` and detects the confirmation block — goose renders the
   question ``…do you allow?`` followed by a cliclack radio list ``Allow`` /
   ``Always Allow`` / ``Deny`` / ``Cancel`` (verified against goose-cli
   ``session/mod.rs::prompt_tool_confirmation``). This is the liveness signal and
   the actuator only; the decision input comes from the store, not the screen,
2. reads the pending ``toolRequest`` (structured ``{name, arguments}``) from
   goose's ``sessions.db`` — goose persists the assistant message before it
   blocks on cliclack, so the call is readable at prompt time,
3. evaluates it against Omnigent policy via ``POST /policies/evaluate``
   (``PHASE_TOOL_CALL``). The engine returns a hard verdict: on ASK it holds the
   gate server-side, publishes the web approval card, waits for the human, and
   collapses to ALLOW/DENY — so the human is involved only when a policy says so,
4. DRIVES the cliclack selector from the verdict: ``Enter`` selects the
   default-highlighted ``Allow``; to deny, ``Down`` to the ``Deny`` row then
   ``Enter`` (``Deny`` index is 2 when ``Always Allow`` is offered, else 1).
   ALLOW / UNSPECIFIED → Allow; DENY → Deny; any transport/parse failure fails
   closed to Deny,
5. if the prompt disappears on its own (answered in the embedded terminal) while
   an ASK is parked, POSTs ``external_elicitation_resolved`` so the web card
   clears.

With ``approve`` mode, goose physically blocks at the prompt until the selector
is answered, so a DENY verdict truly blocks execution (real enforcement, not
advisory). When the store row isn't readable yet (a brief WAL-commit window) the
mirror falls back to the legacy blind ask (the generic ``native-permission-request``
hook) so a transient read miss degrades to "ask the human", never a silent
mis-handle. The embedded terminal remains a manual override channel (the user can
arrow + Enter), same as the other native harnesses.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from omnigent.goose_native_bridge import capture_goose_pane, send_goose_pane_keys
from omnigent.goose_native_forwarder import (
    PendingToolCall,
    _resolve_goose_session_id,
    default_sessions_db,
    read_pending_tool_request,
)
from omnigent.native_policy_hook import hook_payload_to_evaluation_request

_logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = 0.3
_POST_TIMEOUT_S = 86400.0

# Proto verdict strings returned by ``POST /policies/evaluate``. ASK is resolved
# server-side for the tool-call phase (the endpoint holds the gate, publishes the
# web approval card, and collapses to a hard ALLOW/DENY), so the mirror only ever
# sees these. ALLOW and UNSPECIFIED (no agent / no policy matched) both mean
# "let it run"; anything else (incl. a transport/parse failure) fails closed.
_ACTION_ALLOW = "POLICY_ACTION_ALLOW"
_ACTION_UNSPECIFIED = "POLICY_ACTION_UNSPECIFIED"

# How many quick re-reads of the store before falling back to a blind ask when a
# prompt is visible but its ``toolRequest`` row hasn't been flushed yet (a brief
# WAL-commit window between goose persisting the message and rendering cliclack).
_PENDING_READ_RETRIES = 3
_PENDING_READ_RETRY_DELAY_S = 0.15

# The confirmation question — both phrasings contain "do you allow"; it is only
# present while cliclack is awaiting a choice, so it's the liveness signal.
_PROMPT_RE = re.compile(r"do you allow", re.IGNORECASE)
# cliclack box-drawing / radio prefixes to strip when reading the subject lines.
_CLICLACK_PREFIX_RE = re.compile(r"^[\s│◆◇◊○●▲▶>|*-]+")
_ITEM_LABELS = ("Always Allow", "Allow", "Deny", "Cancel")
_SUBJECT_SCAN_LINES = 8


@dataclass(frozen=True)
class GooseApprovalPrompt:
    """A parsed Goose cliclack tool-confirmation prompt.

    :param subject: The tool/command context shown above the question (best
        effort, for the card preview + dedupe).
    :param message: Human-readable card message.
    :param preview: Compact preview for the card.
    :param deny_down_count: Number of ``Down`` presses from the default-
        highlighted ``Allow`` to reach ``Deny`` (2 with "Always Allow", else 1).
    :param block_hash: Stable hash of the subject used to dedupe across polls and
        to mint a stable elicitation id.
    """

    subject: str
    message: str
    preview: str
    deny_down_count: int
    block_hash: str


def goose_permission_elicitation_id(session_id: str, token: str) -> str:
    """Return the deterministic Omnigent elicitation id for a Goose prompt.

    *token* identifies one approval episode (a per-session counter), not the
    scraped content — the rendered tool context above the cliclack widget jitters
    across polls, so hashing it spawned a duplicate card every poll.
    """
    return f"elicit_goose_{session_id}_{token}"


def _looks_like_item(line: str) -> bool:
    """Whether *line* is one of the cliclack radio item rows."""
    stripped = _CLICLACK_PREFIX_RE.sub("", line).strip()
    return any(stripped.startswith(label) for label in _ITEM_LABELS)


def parse_goose_approval_prompt(pane: str) -> GooseApprovalPrompt | None:
    """Parse a Goose cliclack tool-confirmation block from rendered pane text.

    Requires the ``do you allow`` question AND both an ``Allow`` and a ``Deny``
    radio item, so unrelated text never trips it.

    :param pane: Visible pane text from ``capture-pane -p``.
    :returns: The parsed prompt, or ``None`` when no live prompt is visible.
    """
    if not pane:
        return None
    match = _PROMPT_RE.search(pane)
    if match is None:
        return None
    lines = pane.splitlines()
    question_idx = next((i for i, ln in enumerate(lines) if _PROMPT_RE.search(ln)), None)
    if question_idx is None:
        return None

    # The radio items render after the question.
    tail = "\n".join(lines[question_idx:])
    has_allow = re.search(r"\bAllow\b", tail) is not None
    has_deny = re.search(r"\bDeny\b", tail) is not None
    if not (has_allow and has_deny):
        return None
    # "Always Allow" present → Deny is the 3rd item (2 downs); else 2nd (1 down).
    deny_down_count = 2 if re.search(r"Always Allow", tail) else 1

    # Subject = the meaningful (non-item, non-box) lines just above the question,
    # i.e. the tool-request context Goose rendered. Best effort; used for the card
    # preview and to dedupe distinct tool calls (the question text is generic).
    subject_lines: list[str] = []
    start = max(0, question_idx - _SUBJECT_SCAN_LINES)
    for ln in lines[start:question_idx]:
        if _looks_like_item(ln):
            continue
        cleaned = _CLICLACK_PREFIX_RE.sub("", ln).strip()
        if cleaned:
            subject_lines.append(cleaned)
    subject = " | ".join(subject_lines[-3:])[:1024]

    digest_src = subject or tail
    block_hash = hashlib.sha256(digest_src.encode("utf-8")).hexdigest()[:16]
    return GooseApprovalPrompt(
        subject=subject,
        message="Goose wants to call a tool. Allow?",
        preview=subject or "Goose tool call",
        deny_down_count=deny_down_count,
        block_hash=block_hash,
    )


def _evaluate_elicitation_id(session_id: str, episode: int) -> str:
    """Deterministic ``_omnigent_elicitation_id`` for the policy-evaluate gate.

    Must match the server's ``elicit_evaluate_`` + 32-hex contract. Stable per
    ``(session, episode)`` so a mid-ASK supervisor restart re-attaches to the
    same parked gate rather than spawning a duplicate card.
    """
    digest = hashlib.sha256(f"{session_id}:{episode}".encode()).hexdigest()[:32]
    return f"elicit_evaluate_{digest}"


async def supervise_goose_approval_mirror(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    goose_session_name: str,
    auth: httpx.Auth | None = None,
    db_path: Path | None = None,
    poll_interval_s: float = _POLL_INTERVAL_S,
) -> None:
    """Poll the Goose pane and enforce Omnigent policy on each tool prompt.

    :param base_url: Server base URL.
    :param headers: Auth/routing headers for the runner's requests.
    :param session_id: Omnigent conversation id.
    :param bridge_dir: The goose-native bridge dir holding ``tmux.json``.
    :param goose_session_name: The ``--name`` the runner launched goose with,
        used to resolve this launch's ``sessions.id`` for the store read.
    :param auth: Optional httpx auth for the runner's requests.
    :param db_path: Goose sessions DB; defaults to :func:`default_sessions_db`.
    :param poll_interval_s: Pane poll cadence in seconds.
    """
    db = db_path or default_sessions_db()
    goose_session_id: str | None = None
    active_task: asyncio.Task[None] | None = None
    active_holder: dict[str, object] | None = None
    episode = 0
    timeout = httpx.Timeout(_POST_TIMEOUT_S, connect=10.0)
    async with httpx.AsyncClient(
        base_url=base_url, headers=headers, auth=auth, timeout=timeout
    ) as client:
        while True:
            try:
                pane = await asyncio.to_thread(capture_goose_pane, bridge_dir)
                prompt = parse_goose_approval_prompt(pane) if pane else None
                if prompt is not None:
                    # Rising edge only: ONE episode per visible-prompt. We do NOT
                    # re-mint while the prompt stays up.
                    if active_task is None:
                        if goose_session_id is None:
                            goose_session_id = await asyncio.to_thread(
                                _resolve_goose_session_id, db, goose_session_name
                            )
                        episode += 1
                        # Shared holder: the task records the elicitation id it
                        # actually parks (evaluate-gate vs blind-ask use different
                        # id schemes), so a falling edge can release the right one.
                        active_holder = {"elicitation_id": None}
                        active_task = asyncio.create_task(
                            _run_one_approval(
                                client,
                                session_id=session_id,
                                bridge_dir=bridge_dir,
                                prompt=prompt,
                                db_path=db,
                                goose_session_id=goose_session_id,
                                episode=episode,
                                holder=active_holder,
                            ),
                            name=f"goose-approval-{episode}",
                        )
                elif active_task is not None:
                    # Falling edge: the prompt vanished (answered in the TUI, or we
                    # drove it). If the task is still in flight (an ASK parked
                    # server-side), CANCEL it so a late verdict can't drive
                    # keystrokes into a gone prompt, and release any parked card.
                    if not active_task.done():
                        active_task.cancel()
                        eid = active_holder.get("elicitation_id") if active_holder else None
                        if isinstance(eid, str):
                            await _post_external_elicitation_resolved(client, session_id, eid)
                    active_task = None
                    active_holder = None
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception(
                    "goose approval mirror poll failed; session=%s bridge_dir=%s",
                    session_id,
                    bridge_dir,
                )
            await asyncio.sleep(poll_interval_s)


async def _run_one_approval(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    bridge_dir: Path,
    prompt: GooseApprovalPrompt,
    db_path: Path,
    goose_session_id: str | None,
    episode: int,
    holder: dict[str, object],
) -> None:
    """Decide one Goose tool prompt via Omnigent policy, then drive cliclack.

    Reads the pending ``toolRequest`` from the store (with a few quick retries for
    the WAL-commit window) and evaluates it against Omnigent policy. If the store
    row isn't readable, degrades to the legacy blind human ask so a transient miss
    never silently mis-handles. ``allow`` of ``None`` means "do not drive" (the
    prompt was resolved externally, e.g. answered in the TUI).
    """
    call: PendingToolCall | None = None
    if goose_session_id is not None:
        for _ in range(_PENDING_READ_RETRIES):
            call = await asyncio.to_thread(read_pending_tool_request, db_path, goose_session_id)
            if call is not None:
                break
            await asyncio.sleep(_PENDING_READ_RETRY_DELAY_S)
    if call is not None:
        allow = await _evaluate_tool_policy(
            client, session_id=session_id, call=call, episode=episode, holder=holder
        )
    else:
        allow = await _blind_ask(
            client, session_id=session_id, prompt=prompt, episode=episode, holder=holder
        )
    if allow is None:
        return
    await _drive_verdict(bridge_dir, session_id=session_id, prompt=prompt, allow=allow)


async def _evaluate_tool_policy(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    call: PendingToolCall,
    episode: int,
    holder: dict[str, object],
) -> bool | None:
    """Evaluate one tool call against Omnigent policy; return allow/deny.

    POSTs ``PHASE_TOOL_CALL`` to ``/policies/evaluate``; the engine resolves ASK
    server-side (publishes the web card, waits, collapses to ALLOW/DENY). Returns
    ``True`` for ALLOW/UNSPECIFIED, ``False`` for DENY (and any stray ASK), and
    ``False`` fail-closed on transport/parse failure.
    """
    body = hook_payload_to_evaluation_request(
        "PreToolUse", {"tool_name": call.name, "tool_input": call.arguments}
    )
    if body is None:
        # Not policy-relevant here (e.g. an Omnigent-MCP tool already gated on the
        # relay path) — let goose proceed; the relay owns its enforcement.
        return True
    elicitation_id = _evaluate_elicitation_id(session_id, episode)
    body["_omnigent_elicitation_id"] = elicitation_id
    # Record BEFORE the (potentially long, ASK-blocked) POST so a falling edge can
    # release the parked gate.
    holder["elicitation_id"] = elicitation_id
    try:
        response = await client.post(f"/v1/sessions/{session_id}/policies/evaluate", json=body)
    except httpx.HTTPError:
        _logger.exception(
            "goose policy evaluate POST failed; session=%s tool=%s — failing closed",
            session_id,
            call.name,
        )
        return False
    if response.status_code >= 400:
        _logger.warning(
            "goose policy evaluate rejected: status=%s body=%s — failing closed",
            response.status_code,
            response.text[:512],
        )
        return False
    try:
        result = response.json()
    except ValueError:
        _logger.warning(
            "goose policy evaluate returned non-JSON: %s — failing closed",
            response.text[:512],
        )
        return False
    action = result.get("result") if isinstance(result, dict) else None
    return action in (_ACTION_ALLOW, _ACTION_UNSPECIFIED)


async def _blind_ask(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    prompt: GooseApprovalPrompt,
    episode: int,
    holder: dict[str, object],
) -> bool | None:
    """Fallback: park a generic human approval when the tool can't be identified.

    Used only when the store row is momentarily unreadable. Returns ``None`` (no
    drive) on transport error / external resolution, matching the prior behavior.
    """
    elicitation_id = goose_permission_elicitation_id(session_id, str(episode))
    holder["elicitation_id"] = elicitation_id
    payload = {
        "elicitation_id": elicitation_id,
        "agent": "Goose",
        "policy_name": "goose_native_permission",
        "operation_type": "tool",
        "message": prompt.message,
        "content_preview": prompt.preview,
    }
    try:
        response = await client.post(
            f"/v1/sessions/{session_id}/hooks/native-permission-request",
            json=payload,
        )
    except httpx.HTTPError:
        _logger.exception("goose permission hook POST failed; session=%s", session_id)
        return None
    if response.status_code >= 400:
        _logger.warning(
            "goose permission hook rejected: status=%s body=%s",
            response.status_code,
            response.text[:512],
        )
        return None
    if not response.content:
        return None
    try:
        result = response.json()
    except ValueError:
        _logger.warning("goose permission hook returned non-JSON: %s", response.text[:512])
        return None
    action = result.get("action") if isinstance(result, dict) else None
    if action == "accept":
        return True
    if action in {"decline", "cancel"}:
        return False
    return None


async def _drive_verdict(
    bridge_dir: Path,
    *,
    session_id: str,
    prompt: GooseApprovalPrompt,
    allow: bool,
) -> None:
    """Drive the cliclack radio: Enter selects ``Allow``; Down×N + Enter ``Deny``."""
    keys: tuple[str, ...] = (
        ("Enter",) if allow else (*(["Down"] * prompt.deny_down_count), "Enter")
    )
    try:
        await asyncio.to_thread(send_goose_pane_keys, bridge_dir, *keys)
    except RuntimeError:
        _logger.exception(
            "failed to send goose approval keystrokes %r; session=%s", keys, session_id
        )


async def _post_external_elicitation_resolved(
    client: httpx.AsyncClient, session_id: str, elicitation_id: str
) -> None:
    """Tell the server the native TUI answered a pending Goose prompt."""
    try:
        response = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "external_elicitation_resolved",
                "data": {"elicitation_id": elicitation_id},
            },
            timeout=10.0,
        )
        if response.status_code >= 400:
            _logger.warning(
                "goose external_elicitation_resolved rejected: status=%s body=%s",
                response.status_code,
                response.text[:512],
            )
    except httpx.HTTPError:
        _logger.exception("goose external_elicitation_resolved POST failed")
