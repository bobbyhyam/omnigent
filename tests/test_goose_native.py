"""Unit tests for the omni goose CLI-side helpers (no server needed)."""

from __future__ import annotations

import click
import pytest

from omnigent import goose_native as gn


def test_resolve_goose_executable_found() -> None:
    resolved = gn.resolve_goose_executable(
        env={}, which=lambda cmd: f"/usr/local/bin/{cmd}" if cmd == "goose" else None
    )
    assert resolved == "/usr/local/bin/goose"


def test_resolve_goose_executable_honors_path_override() -> None:
    resolved = gn.resolve_goose_executable(
        env={"OMNIGENT_GOOSE_PATH": "/opt/goose"},
        which=lambda cmd: cmd if cmd == "/opt/goose" else None,
    )
    assert resolved == "/opt/goose"


def test_resolve_goose_executable_missing_raises_with_hint() -> None:
    with pytest.raises(click.ClickException) as exc:
        gn.resolve_goose_executable(env={}, which=lambda _cmd: None)
    assert "block-goose-cli" in str(exc.value)


def test_build_goose_launch_argv() -> None:
    launch = gn.build_goose_launch(
        ["session", "--name", "x"],
        env={},
        which=lambda cmd: f"/bin/{cmd}",
    )
    assert launch.executable == "/bin/goose"
    assert launch.argv == ["/bin/goose", "session", "--name", "x"]


def test_terminal_resource_id_stable() -> None:
    assert gn.goose_terminal_resource_id() == gn.goose_terminal_resource_id()


# --- goose-native policy plugin (web approval elicitation) --------------------

_GOOSE_INFO_SAMPLE = (
    "\x1b[36mPaths:\x1b[0m\n"
    "Config dir:           /Users/d/Library/Application Support/goose        (exists)\n"
    "Sessions DB (sqlite): /Users/d/.local/share/goose/sessions/sessions.db    (exists)\n"
    "Logs dir:             /Users/d/.local/state/goose/logs                    (missing)\n"
)


def test_path_after_label_handles_ansi_and_spaces() -> None:
    from pathlib import Path

    from omnigent import goose_native_bridge as b

    # macOS paths contain single spaces ("Application Support"); the >=2-space gap
    # before the status glyph is the real separator.
    assert b._path_after_label(_GOOSE_INFO_SAMPLE, "Config dir:") == Path(
        "/Users/d/Library/Application Support/goose"
    )
    assert b._path_after_label(_GOOSE_INFO_SAMPLE, "Missing label:") is None


def _fake_goose(tmp_path, stdout: str) -> str:
    goose = tmp_path / "goose"
    goose.write_text("#!/bin/sh\ncat <<'EOF'\n" + stdout + "EOF\n")
    goose.chmod(0o755)
    return str(goose)


def test_real_goose_dirs_derives_data_and_state(tmp_path) -> None:
    from pathlib import Path

    from omnigent import goose_native_bridge as b

    dirs = b.real_goose_dirs(_fake_goose(tmp_path, _GOOSE_INFO_SAMPLE))
    assert dirs is not None
    assert dirs["config"] == Path("/Users/d/Library/Application Support/goose")
    # data is two parents up from the sessions.db path.
    assert dirs["data"] == Path("/Users/d/.local/share/goose")
    # state is the parent of the logs dir.
    assert dirs["state"] == Path("/Users/d/.local/state/goose")


def test_setup_goose_native_plugin_root_builds_plugin(tmp_path) -> None:
    import json

    from omnigent import goose_native_bridge as b

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    root = b.setup_goose_native_plugin_root(
        bridge_dir,
        goose_command=_fake_goose(tmp_path, _GOOSE_INFO_SAMPLE),
        hook_script_path="/h.py",
    )
    assert root == b.goose_native_path_root(bridge_dir)
    plugin = root / ".agents" / "plugins" / "omnigent-policy"
    assert json.loads((plugin / "plugin.json").read_text())["name"] == "omnigent-policy"
    hooks = json.loads((plugin / "hooks" / "hooks.json").read_text())
    rule = hooks["hooks"]["PreToolUse"][0]
    # No matcher -> fires for every tool; command points at the hook; long timeout.
    assert "matcher" not in rule
    assert "/h.py" in rule["hooks"][0]["command"]
    assert rule["hooks"][0]["timeout"] == 86400
    # Real config dir symlinked back in (so auth survives GOOSE_PATH_ROOT).
    assert (root / "config").is_symlink()


def test_setup_goose_native_plugin_root_skips_when_info_unparseable(tmp_path) -> None:
    """If goose info can't be parsed (no Config dir), return None — no gating, no broken auth."""
    from omnigent import goose_native_bridge as b

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    root = b.setup_goose_native_plugin_root(
        bridge_dir,
        goose_command=_fake_goose(tmp_path, "no useful output here\n"),
        hook_script_path="/h.py",
    )
    assert root is None
