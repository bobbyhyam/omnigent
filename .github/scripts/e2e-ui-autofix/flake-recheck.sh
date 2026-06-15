#!/usr/bin/env bash
# Mandatory flake gate: re-run the failing e2e_ui tests up to RECHECK_ATTEMPTS
# times. A test that passes on any re-run is treated as a flake and dropped; only
# tests that fail every attempt are "persistent" and worth an agent fix. The
# repo's flake-stress.yml exists precisely because these tests flake, so this
# guard avoids opening churn PRs for transient failures.
#
# Requires the SPA already built (caller does it once; we pass --ui-skip-build)
# and OPENAI_API_KEY/OPENAI_BASE_URL set for the spawned server.
#
# Env in:  RECHECK_ATTEMPTS (default 2)
# In file: artifacts/failing-tests.txt (node IDs, one per line)
# Out:     artifacts/persistent-tests.txt (still-failing after all attempts)
#          persistent_count=<n> on $GITHUB_OUTPUT
set -euo pipefail

out="${GITHUB_OUTPUT:-/dev/null}"
ATTEMPTS="${RECHECK_ATTEMPTS:-2}"
mkdir -p artifacts

mapfile -t remaining < <(grep . artifacts/failing-tests.txt || true)
if [[ ${#remaining[@]} -eq 0 ]]; then
  : > artifacts/persistent-tests.txt
  echo "persistent_count=0" >> "$out"
  echo "No failing tests to recheck."; exit 0
fi

for attempt in $(seq 1 "$ATTEMPTS"); do
  echo "=== flake recheck attempt $attempt/$ATTEMPTS on ${#remaining[@]} test(s) ==="
  rm -f artifacts/recheck.xml
  set +e
  uv run pytest "${remaining[@]}" \
    -v --tb=short -r a \
    --ui-skip-build \
    --junitxml=artifacts/recheck.xml
  set -e

  mapfile -t remaining < <(python3 - <<'PY'
import xml.etree.ElementTree as ET
try:
    root = ET.parse("artifacts/recheck.xml").getroot()
except Exception:
    raise SystemExit(0)
for tc in root.iter("testcase"):
    if tc.find("failure") is not None or tc.find("error") is not None:
        f, name = tc.get("file"), tc.get("name")
        if f and name:
            print(f"{f}::{name}")
PY
)
  echo "still failing after attempt $attempt: ${#remaining[@]}"
  [[ ${#remaining[@]} -eq 0 ]] && break
done

printf '%s\n' "${remaining[@]}" | sed '/^$/d' > artifacts/persistent-tests.txt
n=$(grep -c . artifacts/persistent-tests.txt || true)
echo "persistent_count=$n" >> "$out"
echo "Persistent (non-flaky) failures: $n"; cat artifacts/persistent-tests.txt