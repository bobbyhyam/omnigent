#!/usr/bin/env bash
# Turn the agent's result into the right artifact and notify the culprit PR.
#
#   staged test edits + verify green + not flagged regression -> READY fix PR
#   staged test edits but verify still red / agent unsure      -> DRAFT fix PR
#   no test edits (suspected regression)                       -> ISSUE
# Always comments the outcome on the culprit PR (if known).
#
# Env in: REPO, PR_NUMBER, AUTHOR, HEAD_SHA, AGENT_REPORT, VERIFY_RESULT
#         (green|fail), PUSH_TOKEN, FAILED_RUN_URL
# Uses:   .github/scripts/e2e-ui-agent/open-pr.sh
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out="${GITHUB_OUTPUT:-/dev/null}"
export GH_TOKEN="$PUSH_TOKEN"

report="$(cat "$AGENT_REPORT" 2>/dev/null || echo '(agent produced no report)')"
overall="$(grep -m1 '^OVERALL:' "$AGENT_REPORT" 2>/dev/null | sed 's/^OVERALL:[[:space:]]*//' || true)"
slug="${PR_NUMBER:-$HEAD_SHA}"
ref_line="Culprit: ${PR_NUMBER:+#$PR_NUMBER }(failed run: ${FAILED_RUN_URL:-n/a})"

has_changes=false
git diff --cached --quiet || has_changes=true

body_file="$(mktemp)"

if [[ "$has_changes" == "true" ]]; then
  draft=true; labels=""
  if [[ "$VERIFY_RESULT" == "green" && "$overall" != "SUSPECTED_REGRESSION" ]]; then
    draft=false
  else
    labels="suspected-e2e-ui-regression"
  fi
  {
    echo "## Auto-fix for post-merge e2e_ui failure"
    echo
    echo "$ref_line"
    echo
    if [[ "$draft" == "false" ]]; then
      echo "An agent updated the e2e_ui test(s) to match the merged UI change and the"
      echo "test(s) pass here. Please review that the new assertions reflect *intended*"
      echo "behavior (not just a green checkmark)."
    else
      echo "⚠️ Draft: the agent edited the test(s) but they did **not** verify green here,"
      echo "or it suspects a real regression. Treat this as a starting point, not a fix."
    fi
    echo
    echo "<details><summary>Agent diagnosis</summary>"
    echo; echo '```'; echo "$report"; echo '```'; echo "</details>"
    echo
    echo "_Auto-generated; review required. \`Maintainer Approval\` still gates merge._"
  } > "$body_file"

  BRANCH="e2e-ui-autofix/pr-${slug}" \
  BASE="main" \
  COMMIT_MSG="test(e2e_ui): auto-fix stale UI test after #${PR_NUMBER:-merge}" \
  PR_TITLE="test(e2e_ui): fix post-merge UI failure${PR_NUMBER:+ from #$PR_NUMBER}" \
  PR_BODY_FILE="$body_file" \
  REVIEWER="${AUTHOR:-}" \
  DRAFT="$draft" \
  LABELS="$labels" \
  bash "$here/../e2e-ui-agent/open-pr.sh"
  pr_url="$(grep -m1 '^pr_url=' "$out" | cut -d= -f2- || true)"
  outcome="opened ${draft:+draft }fix PR: ${pr_url}"
else
  # No test edits -> suspected regression. Open an issue, don't mutate the test.
  title="Suspected e2e_ui regression${PR_NUMBER:+ from #$PR_NUMBER}"
  {
    echo "A post-merge e2e_ui failure on main looks like a **real regression**, not a"
    echo "stale test, so no test was changed (changing it would mask the bug)."
    echo
    echo "$ref_line"
    [[ -n "${AUTHOR:-}" ]] && echo "cc @$AUTHOR"
    echo
    echo "<details><summary>Agent diagnosis</summary>"
    echo; echo '```'; echo "$report"; echo '```'; echo "</details>"
  } > "$body_file"
  issue_url="$(gh issue create --repo "$REPO" --title "$title" --body-file "$body_file" \
    --label "suspected-e2e-ui-regression" 2>/dev/null || echo "")"
  outcome="opened regression issue: ${issue_url:-<issue creation failed>}"
fi

echo "outcome=$outcome" >> "$out"
echo "$outcome"

# Notify the culprit PR thread.
if [[ -n "${PR_NUMBER:-}" ]]; then
  gh pr comment "$PR_NUMBER" --repo "$REPO" \
    --body "🤖 Post-merge e2e_ui auto-fix: $outcome" \
    || echo "::warning::could not comment on PR #$PR_NUMBER"
fi
