"""Native-TUI transport driver (phase-2, walking skeleton).

Drives a native-tui harness — a resident vendor CLI (``claude``, ``codex``,
``pi``, ``cursor-agent``, ...) running in a runner-owned tmux pane — through
the bench's :class:`~tests.harness_bench.transport.Driver` protocol.

The research finding this is built on: a native-tui turn rides the *same*
HTTP surface as the full server — ``POST /v1/sessions/{id}/events`` to send,
``GET /v1/sessions/{id}/stream`` for ``response.output_text.delta`` events,
``/v1/sessions/{id}/policies`` for a tool-call deny, and item polling for the
assistant reply. So ~90% of this driver is shared with
:class:`~tests.harness_bench.full_server_driver.FullServerDriver`. Three
things genuinely diverge, and they are the entire reason this is a separate
driver:

1. **Provisioning** — instead of registering an agent tarball, a native
   harness needs a **host daemon** (``omnigent host``) registered with the
   server, the auto-registered ``<harness>-native-ui`` agent, and a session
   created with ``{agent_id, host_id, workspace}``. The runner then launches
   the vendor CLI in tmux itself.
2. **Interrupt detection** — native turns do not persist an "interrupted"
   user-message marker; cancellation surfaces as a ``session.interrupted``
   event / status, so the interrupt probe keys off that.
3. **Auth + login** — the vendor CLI must be interactively logged in on the
   host. ``OMNIGENT_CREDENTIAL`` vendors (claude, codex) can take a minted
   bearer; ``OWN_AUTH`` vendors (cursor, kiro, ...) must be pre-logged-in.
   Either way the bench cannot provision a fresh login, so this transport is
   only exercisable on a host with the vendor CLI already authenticated.

Per-vendor differences beyond auth (tmux paste vs app-server RPC delivery,
readiness signal, tool-deny surface) are captured in :class:`NativeVendor`
records, so adding a harness is a config entry, not a new driver — until a
vendor diverges in kind (codex-native is RPC-delivered; opencode-native is
``native-server`` not ``native-tui``), which will want its own handling.

Scope: this skeleton wires **claude-native** and the shared turn/observe
path. It is structurally complete and offline-tested, but was NOT
live-verified in the authoring environment (no interactive vendor login
available); the gated live test runs it where a login exists.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests.e2e.helpers import lookup_databricks_host
from tests.harness_bench.driver import TurnResult
from tests.harness_bench.full_server_driver import (
    _find_free_port,
    _mint_bearer,
    spawn_omnigent_server,
)
from tests.harness_bench.profile import BenchProfile

_HEALTH_TIMEOUT_S = 90.0
_HOST_ONLINE_TIMEOUT_S = 45.0
_POLL_INTERVAL_S = 0.3
_TURN_TIMEOUT_S = 180.0

# Native turns take longer (terminal boot + a real interactive vendor turn),
# so the prompts stay short and the timeouts generous.
_STREAM_PROMPT = "Count from 1 to 20 in words, one per line."
_LONG_PROMPT = "Write a detailed 500-word essay about the history of computing."

# Session SSE event names (confirmed live against claude-native).
_DELTA_EVENT = "response.output_text.delta"
_COMPLETED_EVENT = "response.completed"
_FAILED_EVENT = "response.failed"
_INTERRUPTED_EVENT = "session.interrupted"
_TERMINAL_EVENTS = frozenset({_COMPLETED_EVENT, _FAILED_EVENT, _INTERRUPTED_EVENT})


@dataclass(frozen=True)
class NativeVendor:
    """Per-vendor facts a native-tui harness needs beyond the shared path.

    :param harness: The native harness id, e.g. ``"claude-native"``.
    :param agent_name: The server's auto-registered UI agent, e.g.
        ``"claude-native-ui"``.
    :param own_auth: ``True`` when the vendor uses its own login (cannot take
        a minted bearer); such a harness is only runnable pre-logged-in.
    """

    harness: str
    agent_name: str
    own_auth: bool = False


# Vendor registry. Both entries are OMNIGENT_CREDENTIAL vendors the bench can
# run (given the vendor CLI is logged in on the host). Delivery differs under
# the hood — claude-native is tmux-paste, codex-native is app-server RPC — but
# the bench only touches the shared session HTTP surface (POST events / SSE /
# item polling), which the runner drives identically for both; the RPC-vs-tmux
# split lives entirely runner-side. OWN_AUTH natives are intentionally absent
# (the bench cannot provision their login).
_VENDORS: dict[str, NativeVendor] = {
    "claude-native": NativeVendor("claude-native", "claude-native-ui", own_auth=False),
    "codex-native": NativeVendor("codex-native", "codex-native-ui", own_auth=False),
}


class NativeTuiDriver:
    """Drive a native-tui harness through a live server + host daemon.

    Async context manager: on enter it spawns a server, a host daemon (under
    the real ``$HOME`` so the vendor login is inherited), waits for the host
    to come online, and creates a native session bound to that host. The
    ``run_*`` methods drive turns over the same HTTP surface the full-server
    driver uses; the runner mirrors them into the tmux vendor TUI.
    """

    transport = "native-tui"

    def __init__(self, profile: BenchProfile, *, databricks_profile: str) -> None:
        self._profile = profile
        self._db_profile = databricks_profile
        self._vendor = _VENDORS.get(profile.harness)
        self._proc: subprocess.Popen[bytes] | None = None
        self._daemon: subprocess.Popen[bytes] | None = None
        self._client: httpx.Client | None = None
        self._session_id: str | None = None
        self._base_url = ""
        self._tmp = Path("/tmp") / f"omni-bench-nt-{uuid.uuid4().hex[:8]}"

    @staticmethod
    def unavailable(profile: BenchProfile, *, databricks_profile: str | None) -> str | None:
        """Return a skip reason if this driver cannot run *profile*, else None."""
        vendor = _VENDORS.get(profile.harness)
        if vendor is None:
            return f"no native-tui vendor entry for {profile.harness!r}"
        if not databricks_profile:
            return "no --profile / databricks profile provided; native-tui needs a gateway route"
        if lookup_databricks_host(databricks_profile) is None:
            return (
                f"databricks profile {databricks_profile!r} missing/hostless in ~/.databrickscfg"
            )
        # The vendor CLI must exist AND be interactively logged in on this
        # host; the bench cannot provision a login. Presence on PATH is the
        # cheapest precondition we can check — a missing login still fails the
        # live turn, reported as a capability-neutral skip by the probes.
        from tests.e2e._harness_probes import cli_unavailable_reason

        binary = profile.cli_binary
        if binary is not None:
            reason = cli_unavailable_reason(binary)
            if reason is not None:
                return reason
        return None

    # ── async driver protocol ────────────────────────────────

    async def __aenter__(self) -> NativeTuiDriver:
        await asyncio.to_thread(self._provision)
        return self

    async def __aexit__(self, *exc: object) -> None:
        await asyncio.to_thread(self._teardown)

    async def run_basic_turn(self, marker: str) -> TurnResult:
        prompt = f"Reply with exactly the literal string {marker} and nothing else."
        return await asyncio.to_thread(self._drive_turn, prompt)

    async def run_streaming_turn(self) -> TurnResult:
        return await asyncio.to_thread(self._drive_turn, _STREAM_PROMPT, count_deltas=True)

    async def run_tool_turn(self, *, deny: bool) -> TurnResult:
        # Native tool calls are the vendor's own tools (Bash/Read/...), not a
        # server-dispatched builtin the bench can force, and a native deny
        # surfaces as a vendor permission decision rather than a
        # function_call_output. Wiring that observation is the next
        # increment; today the skeleton reports it unmeasured so the probe
        # records a capability-neutral skip rather than a false verdict.
        result = TurnResult()
        result.error = "native tool/policy observation not yet wired (skeleton)"
        return result

    async def run_interrupt_turn(self) -> TurnResult:
        return await asyncio.to_thread(self._drive_interrupt_turn)

    # ── provisioning ─────────────────────────────────────────

    def _provision(self) -> None:
        self._tmp.mkdir(mode=0o700, parents=True, exist_ok=True)
        assert self._vendor is not None
        host = lookup_databricks_host(self._db_profile)
        assert host is not None
        port = _find_free_port()
        self._base_url = f"http://localhost:{port}"
        binding_token = uuid.uuid4().hex

        base_env = {
            **os.environ,
            "OPENAI_API_KEY": _mint_bearer(self._db_profile),
            "OPENAI_BASE_URL": f"{host}/serving-endpoints",
            "DATABRICKS_CONFIG_PROFILE": self._db_profile,
            "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token,
        }
        self._proc = spawn_omnigent_server(self._tmp, port, base_env, binding_token)
        self._wait_health()
        self._daemon = self._spawn_host_daemon(base_env)
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=300.0,
            headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        )
        host_id = self._wait_host_online()
        agent_id = self._agent_id(self._vendor.agent_name)
        workspace = self._tmp / "workspace"
        workspace.mkdir(exist_ok=True)
        created = self._client.post(
            "/v1/sessions",
            json={"agent_id": agent_id, "host_id": host_id, "workspace": str(workspace)},
            timeout=60.0,
        )
        created.raise_for_status()
        self._session_id = str(created.json()["id"])

    def _spawn_host_daemon(self, base_env: dict[str, str]) -> subprocess.Popen[bytes]:
        # Under the real $HOME so the vendor's interactive login is inherited
        # (auth cannot be relocated for native harnesses).
        log = (self._tmp / "host-daemon.log").open("wb")
        return subprocess.Popen(
            [runner_executable(), "-m", "omnigent.host._daemon_entry", "--server", self._base_url],
            env=apply_runner_env(base_env),
            cwd=compat_runner_cwd(),
            stdout=subprocess.DEVNULL,
            stderr=log,
        )

    def _wait_health(self) -> None:
        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{self._base_url}/health", timeout=2).status_code == 200:
                    return
            except httpx.HTTPError:
                # Connection refused while the server boots; keep polling.
                pass
            time.sleep(_POLL_INTERVAL_S)
        raise RuntimeError(f"server not healthy within {_HEALTH_TIMEOUT_S}s; logs in {self._tmp}")

    def _wait_host_online(self) -> str:
        assert self._client is not None
        deadline = time.monotonic() + _HOST_ONLINE_TIMEOUT_S
        while time.monotonic() < deadline:
            resp = self._client.get("/v1/hosts")
            if resp.status_code == 200:
                online = [h for h in resp.json().get("hosts", []) if h.get("status") == "online"]
                if online:
                    return str(online[0]["host_id"])
            time.sleep(_POLL_INTERVAL_S)
        raise RuntimeError(f"no host came online within {_HOST_ONLINE_TIMEOUT_S}s")

    def _agent_id(self, agent_name: str) -> str:
        assert self._client is not None
        resp = self._client.get("/v1/agents")
        resp.raise_for_status()
        for agent in resp.json()["data"]:
            if agent.get("name") == agent_name:
                return str(agent["id"])
        raise RuntimeError(f"{agent_name!r} not auto-registered on the server")

    def _teardown(self) -> None:
        if self._client is not None:
            self._client.close()
        for proc in (self._daemon, self._proc):
            if proc is not None and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    # ── turns ────────────────────────────────────────────────

    def _post_message(self, prompt: str) -> None:
        assert self._client is not None
        self._client.post(
            f"/v1/sessions/{self._session_id}/events",
            json={
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
            },
            timeout=30.0,
        ).raise_for_status()

    def _drive_turn(self, prompt: str, *, count_deltas: bool = False) -> TurnResult:
        """Send *prompt* and read this turn's outcome from the SSE stream.

        The turn is driven **entirely from the stream**, not from item
        polling. This is load-bearing: the driver reuses one session across
        probes, so polling for "an assistant item" returns a *prior* turn's
        stale reply and races the current turn (the bug two live smokes
        surfaced as a false streaming DRIFT — deltas arrive, but the stale
        item ended the read before they were counted). Subscribing to the
        stream first, then posting, then reading to ``response.completed``
        scopes every signal to *this* turn: delta count, delta text, and the
        terminal state.
        """
        assert self._client is not None
        result = TurnResult()
        events: list[str] = []
        ready = threading.Event()

        def _read() -> None:
            assert self._client is not None
            try:
                with self._client.stream(
                    "GET", f"/v1/sessions/{self._session_id}/stream", timeout=_TURN_TIMEOUT_S
                ) as resp:
                    ready.set()
                    for line in resp.iter_lines():
                        if line.startswith("event:"):
                            events.append(line[len("event:") :].strip())
                        elif line.startswith("data:") and events and events[-1] == _DELTA_EVENT:
                            # Accumulate the delta text so result.text reflects
                            # THIS turn (item polling would read a prior turn).
                            result.text += _delta_text(line)
                        if events and events[-1] in _TERMINAL_EVENTS:
                            return
            except httpx.HTTPError as exc:
                result.error = repr(exc)

        reader = threading.Thread(target=_read)
        reader.start()
        ready.wait(timeout=10.0)  # subscribe before posting so no delta is lost
        self._post_message(prompt)
        reader.join(timeout=_TURN_TIMEOUT_S)

        result.text_delta_count = sum(1 for e in events if e == _DELTA_EVENT)
        if _COMPLETED_EVENT in events:
            result.completed = True
        elif not events:
            result.timed_out = True
        else:
            # Stream ended on failed / no terminal within timeout.
            result.failed = _FAILED_EVENT in events
            result.timed_out = not result.failed
        return result

    def _drive_interrupt_turn(self) -> TurnResult:
        """Start a long turn, interrupt it once text streams, detect the cancel.

        Native cancellation surfaces as a ``session.interrupted`` SSE event
        (no persisted "interrupted" user message, unlike full-server). Like
        the other turns this subscribes to the stream **before** posting, so
        it sees the first delta (its cue to fire the interrupt) and the
        terminal ``session.interrupted``.
        """
        assert self._client is not None
        result = TurnResult()
        ready = threading.Event()

        def _read() -> None:
            assert self._client is not None
            interrupted = False
            try:
                with self._client.stream(
                    "GET", f"/v1/sessions/{self._session_id}/stream", timeout=_TURN_TIMEOUT_S
                ) as resp:
                    ready.set()
                    for line in resp.iter_lines():
                        if not line.startswith("event:"):
                            continue
                        etype = line[len("event:") :].strip()
                        if etype == _DELTA_EVENT:
                            result.text_delta_count += 1
                            if not interrupted:
                                interrupted = True
                                self._client.post(
                                    f"/v1/sessions/{self._session_id}/events",
                                    json={"type": "interrupt"},
                                    timeout=15.0,
                                )
                        elif etype == _INTERRUPTED_EVENT:
                            result.cancelled = True
                            return
                        elif etype in (_COMPLETED_EVENT, _FAILED_EVENT):
                            return
            except httpx.HTTPError as exc:
                result.error = repr(exc)

        reader = threading.Thread(target=_read)
        reader.start()
        ready.wait(timeout=10.0)
        self._post_message(_LONG_PROMPT)
        reader.join(timeout=_TURN_TIMEOUT_S)
        return result


def _delta_text(data_line: str) -> str:
    """Extract the delta text from a ``data:`` SSE line for an output_text.delta."""
    import json

    try:
        payload = json.loads(data_line[len("data:") :].strip())
    except json.JSONDecodeError:
        return ""
    if isinstance(payload, dict):
        return str(payload.get("delta", "") or "")
    return ""
