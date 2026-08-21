#!/bin/bash
# End-to-end: real `claude` -> stub -> ccpool-watch. Asserts the exit-code
# contract, which is what the wrapper will actually branch on.
#
#   bare    (429, no unified headers) -> 77  transport, never bench
#   quota   (429, unified rejected)   -> 75  suspected quota, caller must confirm
#   allowed (429, unified ALLOWED)    -> 75  same signal from a healthy account
#
# The third case is the whole point. Its headers say the account is at 11%
# utilization and Claude Code still reports status="rejected". A watcher that
# benched on that signal would walk a healthy pool down to nothing, so 75 must
# mean "go check /api/oauth/usage", never "bench".
set -u
cd "$(dirname "$0")"

fail=0

run_case() {
  local mode=$1 port=$2 want=$3
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "  SKIP $mode: port $port busy"; fail=1; return
  fi
  python3 e2e_stub.py "$port" "$mode" >/dev/null 2>&1 &
  local stub=$!
  sleep 1

  env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN -u CLAUDE_CODE_OAUTH_TOKEN \
      DISABLE_OMC=1 ANTHROPIC_BASE_URL="http://127.0.0.1:$port" CLAUDE_CODE_MAX_RETRIES=0 \
      timeout 120 claude -p hi --model opus --output-format stream-json --verbose 2>/dev/null \
    | python3 ccpool_watch.py --model claude-opus-5 >"out_$mode.jsonl" 2>/dev/null
  local code=${PIPESTATUS[1]}
  kill "$stub" 2>/dev/null; wait "$stub" 2>/dev/null

  if [ "$code" = "$want" ]; then
    echo "  ok   $mode -> exit $code"
  else
    echo "  FAIL $mode -> exit $code (want $want)"
    fail=1
  fi
}

echo "end-to-end, real claude binary:"
run_case bare    8881 77
run_case quota   8882 75
run_case allowed 8883 75

rm -f out_bare.jsonl out_quota.jsonl out_allowed.jsonl

# --- the wrapper, driving the real binary -----------------------------------
# Two accounts, both pointed at a stub that 429s. The usage endpoint decides
# whether that 429 was real, so the same stream produces opposite behaviour
# depending only on what Anthropic says -- which is the whole design.
rotate_case() { # pct want_rc want_bench label
  local pct=$1 want_rc=$2 want_bench=$3 label=$4
  local home; home=$(mktemp -d)
  if lsof -nP -iTCP:8884 -sTCP:LISTEN >/dev/null 2>&1; then
    echo "  SKIP $label: port 8884 busy"; fail=1; return
  fi
  # 8884 stands in for the proxy that 429s; 8886 for api.anthropic.com, which
  # the probe reaches directly. Only the latter decides.
  python3 e2e_stub.py 8884 quota >/dev/null 2>&1 &
  local stub=$!
  STUB_USAGE_PCT="$pct" python3 e2e_stub.py 8886 probe >/dev/null 2>&1 &
  local probe=$!
  sleep 1

  CCPOOL_HOME="$home" python3 ccpool.py add one <<<"fake-token-one" 2>/dev/null
  CCPOOL_HOME="$home" python3 ccpool.py add two <<<"fake-token-two" 2>/dev/null

  local err rc
  err=$(env -u ANTHROPIC_API_KEY -u CLAUDE_CODE_OAUTH_TOKEN \
      CCPOOL_HOME="$home" CCPOOL_PROBE_BASE="http://127.0.0.1:8886" \
      DISABLE_OMC=1 ANTHROPIC_BASE_URL="http://127.0.0.1:8884" CLAUDE_CODE_MAX_RETRIES=0 \
      timeout 180 python3 ccpool.py run -- -p hi --model opus \
      --output-format stream-json --verbose 2>&1 >/dev/null)
  rc=$?
  kill "$stub" "$probe" 2>/dev/null; wait "$stub" "$probe" 2>/dev/null

  local benched=no
  echo "$err" | rg -q 'benched' && benched=yes
  if [ "$rc" = "$want_rc" ] && [ "$benched" = "$want_bench" ]; then
    echo "  ok   $label -> exit $rc, benched: $benched"
  else
    echo "  FAIL $label -> exit $rc (want $want_rc), benched $benched (want $want_bench)"
    echo "$err" | tail -4 | sd '^' '         '
    fail=1
  fi
  rm -rf "$home"
}

echo
echo "wrapper rotation, real claude binary:"
rotate_case 0.99 75 yes "usage says exhausted -> bench both, report"
rotate_case 0.10 1  no  "usage says healthy   -> forged 429, no bench"

[ "$fail" = 0 ] && echo "e2e passed" || echo "e2e FAILED"
exit "$fail"
