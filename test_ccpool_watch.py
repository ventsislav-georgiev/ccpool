#!/usr/bin/env python3
"""Self-check for ccpool-watch. `python3 test_ccpool_watch.py` -- no framework.

Every fixture below is a real capture from a constructed stub, not a shape
someone assumed. The load-bearing one is C1: it is the case that killed the
previous predicate, and the only reason it was found is that somebody built the
stub instead of reading the code path.
"""
import io
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ccpool_watch import (  # noqa: E402
    EXIT_OK, EXIT_QUOTA, EXIT_SCHEMA, EXIT_TRANSPORT,
    claims_for, classify, run,
)

FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}{'' if cond else ' ' + detail}")
    if not cond:
        FAILURES.append(name)


def event(**info):
    return json.dumps({"type": "rate_limit_event", "rate_limit_info": info}) + "\n"


# --- captures, all from stubs against the real 2.1.220 binary -----------------

# Healthy 200. Note overageStatus is "rejected" on a perfectly fine account.
HEALTHY = event(status="allowed", resetsAt=1785688200, rateLimitType="five_hour",
                overageStatus="rejected", overageDisabledReason="org_level_disabled",
                isUsingOverage=False)
# 429 with no anthropic-ratelimit-unified-* headers at all.
TRANSPORT = event(status="rejected", isUsingOverage=False)
# 429 with the full unified header set, unified-status: rejected.
QUOTA = event(status="rejected", rateLimitType="seven_day_opus",
              resetsAt=int(time.time()) + 3600, isUsingOverage=False)
# C1: 429 whose own headers said unified-status:allowed at 11% utilization.
# Byte-identical in shape to QUOTA. This is why the watcher cannot decide.
C1_FORGED = event(status="rejected", rateLimitType="five_hour",
                  resetsAt=1785685839, isUsingOverage=False)
# 200 whose 5h-utilization header was 0.95; the warning is client-derived.
WARNING = event(status="allowed_warning", resetsAt=1785682301,
                rateLimitType="five_hour", utilization=0.95, isUsingOverage=False)


def drive(lines, model=None):
    out = io.StringIO()
    code = run(io.StringIO("".join(lines)), out, model)
    return code, out.getvalue()


def test_signals():
    print("\nSignal classification")

    code, _ = drive([HEALTHY], "opus")
    check("healthy event is silent (overageStatus trap)", code == EXIT_OK, f"got {code}")

    code, _ = drive([WARNING], "opus")
    check("allowed_warning is not a failure", code == EXIT_OK, f"got {code}")

    code, _ = drive([TRANSPORT], "opus")
    check("claimless 429 is transport, not quota", code == EXIT_TRANSPORT, f"got {code}")

    code, _ = drive([QUOTA], "opus")
    check("429 naming a claim suspects quota", code == EXIT_QUOTA, f"got {code}")

    code, _ = drive([QUOTA], "haiku")
    check("suspicion does not depend on the model -- the run was blocked either way",
          code == EXIT_QUOTA, f"got {code}")


def test_c1():
    print("\nC1: the forged rejection that killed the previous design")
    c1, _ = drive([C1_FORGED], "opus")
    q, _ = drive([QUOTA], "opus")
    check("forged and genuine are indistinguishable in-band", c1 == q)
    check("so the watcher reports suspicion, never a verdict", c1 == EXIT_QUOTA, f"got {c1}")
    # The regression guard: nothing here may ever write a cooldown.
    import ccpool_watch
    writers = [n for n in dir(ccpool_watch)
               if n in ("record_cooldown", "write_atomic", "cool")]
    check("watcher has no cooldown-writing surface at all", not writers, str(writers))


def test_precedence():
    print("\nSignal precedence within one stream")
    code, _ = drive([TRANSPORT, QUOTA], "opus")
    check("quota after transport wins", code == EXIT_QUOTA, f"got {code}")
    code, _ = drive([QUOTA, TRANSPORT], "opus")
    check("transport after quota does not downgrade it", code == EXIT_QUOTA, f"got {code}")


def test_schema_drift():
    print("\nSchema drift fails loud, and distinctly")
    code, _ = drive([event(status="throttled")], "opus")
    check("unknown status exits 76", code == EXIT_SCHEMA, f"got {code}")
    code, _ = drive([event(status="rejected", rateLimitType="thirty_day", resetsAt=1)])
    check("unknown rateLimitType exits 76", code == EXIT_SCHEMA, f"got {code}")
    check("76 collides with nothing", len({EXIT_OK, EXIT_QUOTA, EXIT_SCHEMA, EXIT_TRANSPORT}) == 4)


def test_passthrough():
    print("\nStream integrity")
    lines = ['{"type":"system","subtype":"init"}\n', HEALTHY,
             '{"type":"assistant"}\n', QUOTA, '{"type":"result"}\n']
    code, out = drive(lines, "opus")
    check("every line passes through unchanged", out == "".join(lines))
    check("drains to EOF, so `result` is not truncated",
          out.endswith('{"type":"result"}\n'))
    check("signal survives the drain", code == EXIT_QUOTA, f"got {code}")
    code, out = drive(['not json\n', '\n', '{"type":"rate_limit_event"}\n'], "opus")
    check("junk and a bodyless event neither crash nor signal", code == EXIT_OK, f"got {code}")


def test_claims():
    print("\nClaim reporting")
    check("fable is seven_day_overage_included ('Fable 5 limit' in the binary)",
          "seven_day_overage_included" in claims_for("claude-fable-5"))
    check("fable is NOT seven_day_opus", "seven_day_opus" not in claims_for("claude-fable-5"))
    check("haiku claims no weekly model window",
          "seven_day_opus" not in claims_for("claude-haiku-4-5"))
    check("absent model does not crash and covers every family",
          set(claims_for(None)) >= {"seven_day_opus", "seven_day_sonnet",
                                    "seven_day_overage_included"})
    check("every claim any model reads is in the enum",
          all(c in __import__("ccpool_watch").RATE_LIMIT_TYPES
              for cs in list(__import__("ccpool_watch").CLAIMS.values()) + [claims_for(None)]
              for c in cs))


def test_text_path():
    print("\nText path -- the only one available under ANTHROPIC_AUTH_TOKEN")
    # Captured from a real 2.1.220 run against a 429 stub, with the rotation
    # credential set. No rate_limit_event was emitted at all.
    err = ('{"type":"result","subtype":"error_during_execution","is_error":true,'
           '"result":"API Error: Request rejected (429) \\u00b7 stub"}\n')
    code, _ = drive([err], "opus")
    check("a 429 with no event still signals quota", code == EXIT_QUOTA, f"got {code}")

    trans = ('{"type":"result","is_error":true,"result":"API Error: Server is '
             'temporarily limiting requests (not your usage limit) \\u00b7 exhausted"}\n')
    code, _ = drive([trans], "opus")
    check("the CLI's own transport wording is not a quota signal",
          code == EXIT_TRANSPORT, f"got {code}")

    ok = '{"type":"result","subtype":"success","is_error":false,"result":"done"}\n'
    code, _ = drive([ok], "opus")
    check("a successful result signals nothing", code == EXIT_OK, f"got {code}")

    benign = ('{"type":"result","is_error":true,'
              '"result":"Error: file not found"}\n')
    code, _ = drive([benign], "opus")
    check("an unrelated error is not a rate limit", code == EXIT_OK, f"got {code}")

    synth = ('{"type":"assistant","message":{"content":[{"type":"text",'
             '"text":"API Error: Request rejected (429)"}]}}\n')
    code, _ = drive([synth], "opus")
    check("the synthetic assistant turn counts too", code == EXIT_QUOTA, f"got {code}")


def test_classify_direct():
    print("\nclassify()")
    check("allowed returns no signal", classify({"status": "allowed"})[0] is None)
    check("rejected + claim -> quota",
          classify({"status": "rejected", "rateLimitType": "overage"})[0] == "quota")
    check("rejected alone -> transport", classify({"status": "rejected"})[0] == "transport")
    check("empty info is not a failure", classify({})[0] is None)


if __name__ == "__main__":
    test_signals()
    test_c1()
    test_precedence()
    test_schema_drift()
    test_passthrough()
    test_text_path()
    test_claims()
    test_classify_direct()
    print(f"\n{'FAILED: ' + ', '.join(FAILURES) if FAILURES else 'all checks passed'}")
    sys.exit(1 if FAILURES else 0)
