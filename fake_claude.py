#!/usr/bin/env python3
"""Stand-in for the `claude` binary, so rotation can be tested without burning
real quota. Behaviour is keyed off ANTHROPIC_AUTH_TOKEN, which is exactly the
variable ccpool rotates -- so a test that sees different behaviour per account
has proven the credential actually switched.

    token containing "quota"     -> rate_limit_event naming a claim, exit 1
    token containing "transport" -> rate_limit_event with no claim,  exit 1
    token containing "ok"        -> a normal successful run,         exit 0
"""
import json
import os
import sys
import time

tok = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")


def emit(obj):
    print(json.dumps(obj), flush=True)


emit({"type": "system", "subtype": "init", "session_id": "fake"})

if "quota" in tok:
    emit({"type": "rate_limit_event",
          "rate_limit_info": {"status": "rejected", "rateLimitType": "five_hour",
                              "resetsAt": int(time.time()) + 3600,
                              "isUsingOverage": False},
          "uuid": "u", "session_id": "fake"})
    emit({"type": "result", "subtype": "error", "is_error": True})
    sys.exit(1)

if "transport" in tok:
    emit({"type": "rate_limit_event",
          "rate_limit_info": {"status": "rejected", "isUsingOverage": False},
          "uuid": "u", "session_id": "fake"})
    emit({"type": "result", "subtype": "error", "is_error": True})
    sys.exit(1)

emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}})
emit({"type": "result", "subtype": "success", "is_error": False,
      "result": f"ran under {tok[:12]}"})
sys.exit(0)
