#!/usr/bin/env python3
"""ccpool-watch -- Phase 1.

Reads Claude Code's `--output-format stream-json` on stdin, passes every line
through to stdout unchanged, and classifies why a run stopped.

    claude -p ... --output-format stream-json --verbose | ccpool-watch --model opus

Exit codes:
    0   ran to EOF, nothing wrong
    75  quota SUSPECTED -- a 429 naming a rate-limit claim. NOT proof. The
        caller must confirm against GET /api/oauth/usage before benching.
    76  schema drift: the event shape is outside the enums this was built against
    77  transport 429 -- a 429 carrying no claim. Never a quota problem; do not
        bench, do not rotate. Retry or fail over.

Why this program decides nothing
--------------------------------
It used to write cooldowns itself, on the theory that
`status=="rejected"` + `rateLimitType` + `resetsAt` meant the account was
exhausted. That is false, and constructing it takes one stub. Feed Claude Code
a 429 whose own headers say the account is FINE:

    anthropic-ratelimit-unified-status: allowed
    anthropic-ratelimit-unified-5h-utilization: 0.11
    anthropic-ratelimit-unified-representative-claim: five_hour

and it emits, verbatim:

    {"status":"rejected","resetsAt":1785685839,"rateLimitType":"five_hour",
     "isUsingOverage":false}

byte-identical in shape to a real exhaustion. In the 2.1.220 binary `fpo`
assigns `l.status="rejected"` unconditionally on the 429 path, discarding the
server's own verdict; `rateLimitType` and `resetsAt` are just the
`-representative-claim` and `-reset` headers copied across, neither of which is
rejection-specific -- a healthy 200 carries both. So the old predicate did not
detect exhaustion. It detected "this response carried the unified header
block", which any intermediary can produce. Benching on it walks a pool of
healthy accounts down to nothing, silently.

Anthropic evidently knows: the binary has a `tengu_quota_mismatch` telemetry
event keyed on exactly this disagreement.

There is no in-band fix, because the forged and genuine events are identical.
The only authority is out-of-band: `GET /api/oauth/usage` (the binary's `$ke`),
which returns real per-claim `percent` and `resets_at` straight from Anthropic
rather than from whatever returned the 429. Hence: this program signals, the
caller confirms.

The three traps that each produced a wrong draft, still one typo away:

  1. `overageStatus` is ALREADY the literal string "rejected" on a perfectly
     healthy account. Only `status` may be read.
  2. A 429 with no `rateLimitType` is the transport class -- exit 77, never a
     bench. Locally this is the *most common* 429 by a wide margin.
  3. Exit 75 is a suspicion, not a verdict. Anything that treats it as proof
     reintroduces the bug above.
"""
from __future__ import annotations

import argparse
import json
import sys

# From the Zod enum in the 2.1.220 binary. A value outside this set means the
# schema moved and we should fail loudly rather than guess.
RATE_LIMIT_TYPES = frozenset({
    "five_hour", "seven_day", "seven_day_opus",
    "seven_day_sonnet", "seven_day_overage_included", "overage",
})
STATUSES = frozenset({"allowed", "allowed_warning", "rejected"})

# Which claims a model draws on, for reporting only -- nothing branches on it.
# fable is seven_day_overage_included, not seven_day_opus: the binary's display
# map reads seven_day_overage_included:"Fable 5 limit".
CLAIMS = {
    "opus": ("five_hour", "seven_day", "seven_day_opus"),
    "fable": ("five_hour", "seven_day", "seven_day_overage_included"),
    "sonnet": ("five_hour", "seven_day", "seven_day_sonnet"),
    "haiku": ("five_hour", "seven_day"),
}
MOST_RESTRICTIVE = ("five_hour", "seven_day", "seven_day_opus",
                    "seven_day_sonnet", "seven_day_overage_included")

EXIT_OK, EXIT_QUOTA, EXIT_SCHEMA, EXIT_TRANSPORT = 0, 75, 76, 77

# Claude Code's own words for the two classes, used when no event is available.
# Measured: under ANTHROPIC_AUTH_TOKEN -- the variable rotation depends on --
# `rate_limit_event` is never published at all. The emitter is
# `if(fPp(...)) q.enqueue({type:"rate_limit_event",...})` and fPp yields nothing
# for non-subscription auth. So the event is a fast path, never the mechanism.
# These strings are, deliberately, only ever a suspicion: the usage probe
# decides, so a false positive costs one HTTP call and nothing else.
TRANSPORT_TEXT = ("not your usage limit",)
QUOTA_TEXT = ("(429)", "rate limit", "usage limit", "session limit",
              "weekly limit", "opus limit", "sonnet limit", "usage credit limit")


class SchemaDrift(Exception):
    """An event field is present but outside the enum we were built against."""


def claims_for(model: str | None) -> tuple[str, ...]:
    for key, claims in CLAIMS.items():
        if model and key in model:
            return claims
    return MOST_RESTRICTIVE


def classify(info: dict) -> tuple[str | None, str | None]:
    """Return (signal, claim). signal is None | "quota" | "transport".

    Raises SchemaDrift if a present field is outside its enum.
    """
    status = info.get("status")
    if status is not None and status not in STATUSES:
        raise SchemaDrift(f"unknown status {status!r}")

    claim = info.get("rateLimitType")
    if claim is not None and claim not in RATE_LIMIT_TYPES:
        raise SchemaDrift(f"unknown rateLimitType {claim!r}")

    if status != "rejected":
        return None, claim
    # Trap 2. No claim means no quota was named, so no quota was hit.
    return ("transport" if claim is None else "quota"), claim


def error_text(msg: dict) -> str:
    """Whatever user-facing error text a message carries, lowercased."""
    if msg.get("type") == "result" and msg.get("is_error"):
        return str(msg.get("result") or "").lower()
    if msg.get("type") == "assistant":
        content = msg.get("message", {}).get("content") or []
        if isinstance(content, list):
            return " ".join(c.get("text", "") for c in content
                            if isinstance(c, dict)).lower()
    return ""


def classify_text(text: str) -> str | None:
    if not text:
        return None
    if any(t in text for t in TRANSPORT_TEXT):
        return "transport"
    if any(t in text for t in QUOTA_TEXT):
        return "quota"
    return None


def run(stdin, stdout, model: str | None = None) -> int:
    exit_code = EXIT_OK
    for line in stdin:
        # Pass through first and always: this stream is the caller's output.
        stdout.write(line)
        stdout.flush()

        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        if msg.get("type") != "rate_limit_event":
            # The primary path. `rate_limit_event` is the optimization -- it is
            # absent whenever ANTHROPIC_AUTH_TOKEN is in use, which is always,
            # for a pooled run.
            signal = classify_text(error_text(msg))
            if signal == "transport" and exit_code == EXIT_OK:
                print("ccpool: CLI reports a transport limit, not quota.",
                      file=sys.stderr)
                exit_code = EXIT_TRANSPORT
            elif signal == "quota":
                print("ccpool: CLI reports a rate limit -- SUSPECTED quota. "
                      "Confirm via GET /api/oauth/usage before benching.",
                      file=sys.stderr)
                exit_code = EXIT_QUOTA
            continue

        info = msg.get("rate_limit_info") or {}
        try:
            signal, claim = classify(info)
        except SchemaDrift as e:
            print(f"ccpool: {e} -- rate_limit_event schema moved; refusing to "
                  f"guess. Check Claude Code's version.", file=sys.stderr)
            return EXIT_SCHEMA

        # Present only when Claude Code's own threshold table fires, and it is
        # the honest number when it is there. Reported, not acted on: a wrapper
        # may use it to rotate early, but this program does not decide.
        util = info.get("utilization")
        if util is not None:
            relevant = "" if claim in claims_for(model) else " (other model)"
            print(f"ccpool: {claim} at {util:.0%}{relevant}", file=sys.stderr)

        if signal == "transport":
            print("ccpool: 429 with no claim -- transport/proxy, not quota. "
                  "Not benching.", file=sys.stderr)
            # Do not let a real quota signal earlier in the stream be downgraded.
            if exit_code == EXIT_OK:
                exit_code = EXIT_TRANSPORT
        elif signal == "quota":
            print(f"ccpool: 429 naming {claim} -- SUSPECTED quota. Confirm via "
                  f"GET /api/oauth/usage before benching.", file=sys.stderr)
            # Keep draining: exiting here truncates the stream before `result`.
            exit_code = EXIT_QUOTA
    return exit_code


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Classify why a Claude Code run stopped. Decides nothing.")
    ap.add_argument("--model", default=None,
                    help="model passed to claude, for utilization reporting")
    args = ap.parse_args(argv)
    return run(sys.stdin, sys.stdout, args.model)


if __name__ == "__main__":
    sys.exit(main())
