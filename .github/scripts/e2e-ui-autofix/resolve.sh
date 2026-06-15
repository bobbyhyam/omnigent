#!/usr/bin/env bash
# Resolve what the auto-fix run should act on: the culprit PR + author (from the
# failed run's head SHA) and the list of failing e2e_ui test node IDs (from the
# failed run's junit artifacts).
#
# Env in:  REPO, GH_TOKEN, HEAD_SHA, FAILED_RUN_ID
#          OVERRIDE_PR, OVERRIDE_TESTS (optional; dry-run via workflow_dispatch --
#          skip SHA/junit resolution and use these instead)
# Out (on $GITHUB_OUTPUT): proceed=true|false, reason, pr_number, author
#          Writes failing node IDs (one per line) to artifacts/failing-tests.txt
set -euo pipefail

out="${GITHUB_OUTPUT:-/dev/null}"
mkdir -p artifacts
: > artifacts/failing-tests.txt

emit() { echo "$1=$2" >> "$out"; }

# --- Dry-run override path -------------------------------------------------
if [[ -n "${OVERRIDE_TESTS:-}" ]]; then
  printf '%s\n' "$OVERRIDE_TESTS" | tr ',' '\n' | sed '/^$/d' > artifacts/failing-tests.txt
  emit proceed true
  emit reason "override (dry-run)"
  emit pr_number "${OVERRIDE_PR:-}"
  emit author ""
  if [[ -n "${OVERRIDE_PR:-}" ]]; then
    author=$(gh api "repos/$REPO/pulls/$OVERRIDE_PR" --jq '.user.login' 2>/dev/null || echo "")
    emit author "$author"
  fi
  echo "Dry-run: failing tests ="; cat artifacts/failing-tests.txt
  exit 0
fi

# --- Culprit PR + author from the failed run's head SHA --------------------
pr_json=$(gh api "repos/$REPO/commits/$HEAD_SHA/pulls" --jq '.[0] // {}' 2>/dev/null || echo '{}')
pr_number=$(echo "$pr_json" | jq -r '.number // empty')
author=$(echo "$pr_json" | jq -r '.user.login // empty')
emit pr_number "$pr_number"
emit author "$author"

# --- Failing test node IDs from the failed run's junit artifacts -----------
rm -rf junit && mkdir -p junit
if ! gh run download "$FAILED_RUN_ID" --repo "$REPO" --dir junit --pattern 'e2e-ui-junit-*' 2>/dev/null; then
  echo "::warning::no junit artifacts on run $FAILED_RUN_ID"
fi

python3 - <<'PY' > artifacts/failing-tests.txt || true
import glob, xml.etree.ElementTree as ET
seen = set()
for path in glob.glob("junit/**/*.xml", recursive=True):
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        continue
    for tc in root.iter("testcase"):
        if tc.find("failure") is None and tc.find("error") is None:
            continue
        f = tc.get("file"); name = tc.get("name")
        if f and name:
            nid = f"{f}::{name}"
            if nid not in seen:
                seen.add(nid); print(nid)
PY

count=$(grep -c . artifacts/failing-tests.txt || true)
echo "Found $count failing e2e_ui test(s):"; cat artifacts/failing-tests.txt
if [[ "$count" -eq 0 ]]; then
  emit proceed false
  emit reason "no failing e2e_ui tests parsed from junit (nothing to fix)"
else
  emit proceed true
  emit reason "$count failing test(s); culprit PR #${pr_number:-unknown} by @${author:-unknown}"
fi
