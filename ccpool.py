#!/usr/bin/env python3
"""ccpool -- run Claude Code across a pool of accounts, rotating on rate limits.

    ccpool add work            # paste a token on stdin; never on argv
    ccpool ls
    ccpool clear work          # undo a bench
    ccpool run -- -p "do the thing" --model opus

`run` picks an eligible account, runs Claude Code under it, and on a rate-limit
rejection confirms with Anthropic before benching that account and retrying on
the next one.

Why the confirmation step exists
--------------------------------
A 429 from *anything* -- a proxy, a CDN, a local dev server -- makes Claude Code
emit an event indistinguishable from real account exhaustion (see
ccpool_watch.py). Benching on that signal alone walks a healthy pool down to
nothing, silently. So the stream only ever raises a suspicion; `GET
/api/oauth/usage` decides, because it answers from Anthropic rather than from
whatever returned the 429.

Credentials
-----------
Rotation works by setting ANTHROPIC_AUTH_TOKEN per invocation. Measured against
2.1.220: that variable overrides an existing keychain login, and
CLAUDE_CODE_OAUTH_TOKEN does NOT -- planting it left the keychain credential on
the wire. Mint per-account tokens with `claude setup-token` while logged into
each account.

The vault is a 0600 file. That is real protection against other users and no
protection against anything running as you. Tokens are never printed, never
passed on argv, and never logged.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ccpool_watch as W  # noqa: E402

HOME = Path(os.environ.get("CCPOOL_HOME", Path.home() / ".ccpool"))
VAULT, STATE = HOME / "vault.json", HOME / "state.json"
CLAUDE = os.environ.get("CCPOOL_CLAUDE", "claude")
# Deliberately NOT ANTHROPIC_BASE_URL: the probe must not travel through
# whatever proxy produced the 429 it is being asked to adjudicate.
PROBE_BASE = os.environ.get("CCPOOL_PROBE_BASE", "https://api.anthropic.com")
PROBE_MODEL = os.environ.get("CCPOOL_PROBE_MODEL", "claude-haiku-4-5-20251001")
MARKER = "You are Claude Code, Anthropic's official CLI for Claude."

# Bench when the authoritative number says the claim is this consumed.
#
# 1.0, not 0.98, and deliberately: a threshold below 1.0 throws away the
# remainder of every account. Under the proxy a rejection is retried on the next
# account transparently, so the only cost of running an account to the very end
# is one wasted round trip -- and the proxy avoids even that by benching an
# account the moment a *successful* response reports it full.
EXHAUSTED = float(os.environ.get("CCPOOL_EXHAUSTED", "1.0"))
# Used only when the probe cannot answer. Short on purpose: long enough to break
# a retry spin, short enough that a wrongly-benched account comes back fast.
UNCONFIRMED_BENCH = 900
MAX_BENCH = 8 * 86400


# --- storage -----------------------------------------------------------------

def write_atomic(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    replaced = False
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(obj, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        replaced = True
    except BaseException:
        # Only unlink while tmp still exists. After a successful os.replace it
        # does not, and unlinking would mask the real exception with
        # FileNotFoundError -- on a write that actually landed.
        if not replaced:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
        raise


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def accounts() -> dict:
    return load(VAULT).get("accounts", {})


def cooldowns(name: str) -> dict:
    return load(STATE).get("accounts", {}).get(name, {}).get("cooldown", {})


def bench(name: str, claim: str, until: int) -> None:
    st = load(STATE)
    cd = (st.setdefault("accounts", {}).setdefault(name, {})
            .setdefault("cooldown", {}))
    cd[claim] = max(cd.get(claim, 0), int(min(until, time.time() + MAX_BENCH)))
    write_atomic(STATE, st)


def remember_last(name: str) -> None:
    """Persist the active account, so a restart resumes on the same one instead
    of moving to another organization and cold-starting its cache."""
    st = load(STATE)
    if st.get("current") == name:
        return                       # no state write on the common path
    st["current"] = name
    write_atomic(STATE, st)


# --- selection ---------------------------------------------------------------

def eligible(name: str, model: str | None, now: float) -> bool:
    cd = cooldowns(name)
    return all(cd.get(c, 0) <= now for c in W.claims_for(model))


def unreachable_keys() -> set[str]:
    """Cooldown keys no CLAIMS entry reads -- an account benched on one of these
    would never actually be skipped. Membership in the enum is not enough; that
    was the bug that let a fable mismapping through unnoticed."""
    readable = {c for cs in W.CLAIMS.values() for c in cs} | set(W.MOST_RESTRICTIVE)
    return {k for n in accounts() for k in cooldowns(n)} - readable


def pick(model: str | None, exclude: set[str]) -> str | None:
    """One account serves everything until it is exhausted. Then the next one.

    Never two at once, and never a switch for any reason other than exhaustion.
    Besides being what was asked for, it is the cheap option: the prompt cache
    is per-organization, so every switch re-writes the whole conversation prefix
    to cache instead of reading it -- roughly 12.5x the input cost of that turn,
    charged against the very quota being conserved. Spreading requests across
    accounts would burn the pool faster than draining one at a time.
    """
    now = time.time()
    names = list(accounts())
    if not names:
        return None

    current = load(STATE).get("current")
    if current in names and current not in exclude and eligible(current, model, now):
        return current

    # Current one is spent (or excluded after a rejection). Take the next in
    # order and stay there.
    start = names.index(current) + 1 if current in names else 0
    for i in range(len(names)):
        n = names[(start + i) % len(names)]
        if n not in exclude and eligible(n, model, now):
            return n
    return None


def next_reset(model: str | None) -> int | None:
    now = time.time()
    times = [t for n in accounts() for c, t in cooldowns(n).items()
             if c in W.claims_for(model) and t > now]
    return int(min(times)) if times else None


# --- confirmation ------------------------------------------------------------

# `anthropic-ratelimit-unified-<abbrev>-*` -> the claim names cooldowns use.
ABBREV = {"5h": "five_hour", "7d": "seven_day",
          "7d-opus": "seven_day_opus", "7d-sonnet": "seven_day_sonnet",
          "7d-overage-included": "seven_day_overage_included"}
PREFIX = "anthropic-ratelimit-unified-"

# Display names: the binary reads seven_day_overage_included as "Fable 5 limit".
LABEL = {"five_hour": "5h", "seven_day": "week", "seven_day_opus": "opus",
         "seven_day_sonnet": "sonnet", "seven_day_overage_included": "fable"}
RESET_SHOWN = ("five_hour", "seven_day")
ORDER = {c: i for i, c in enumerate(
    ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet",
     "seven_day_overage_included"))}


def fmt_reset(val) -> str:
    """"(4h4m)" / "(1d18h)" -- time left, same shape the HUD prints."""
    try:
        left = int(float(val) - time.time())
    except (TypeError, ValueError, OverflowError):
        return ""
    if left <= 0:
        return "(now)"
    d, rem = divmod(left, 86400)
    h, m = divmod(rem // 60, 60)
    if d:
        return f"({d}d{h}h)"
    return f"({h}h{m}m)" if h else f"({m}m)"


def probe_limits(token: str) -> dict[str, dict] | None:
    """Ask Anthropic directly what this account's quota actually is.

    Not /api/oauth/usage: that requires `user:profile` scope and a setup-token
    only carries `user:inference`, so it answers 403. Measured, not assumed.

    Instead: one minimal completion straight to api.anthropic.com. The
    rate-limit headers ride on the response, and because *we* make this call it
    bypasses ANTHROPIC_BASE_URL -- no proxy sits in the path to invent a 429 or
    forge a header. That directness is the entire point; it is the property the
    C1 counterexample showed the in-band signal lacks.

    Costs one haiku call of a few tokens, and only on suspicion.

    Returns {claim: {"status", "utilization", "reset"}}, or None for "unknown"
    -- which never means "healthy".
    """
    payload = json.dumps({
        "model": PROBE_MODEL,
        "max_tokens": 1,
        "system": [{"type": "text", "text": MARKER,
                    "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": "hi"}],
    }).encode()
    req = urllib.request.Request(
        f"{PROBE_BASE}/v1/messages", data=payload, method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "content-type": "application/json",
                 "anthropic-version": "2023-06-01",
                 "anthropic-beta": "oauth-2025-04-20"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            hdrs = dict(r.headers)
            r.read()
    except urllib.error.HTTPError as e:
        hdrs = dict(e.headers)          # a 429 here is the authority speaking
        with contextlib.suppress(Exception):
            e.read()
    except (urllib.error.URLError, OSError) as e:
        print(f"ccpool: probe failed ({e.__class__.__name__}); cannot confirm",
              file=sys.stderr)
        return None

    hdrs = {k.lower(): v for k, v in hdrs.items() if k.lower().startswith(PREFIX)}
    if not hdrs:
        print("ccpool: probe returned no rate-limit headers; cannot confirm",
              file=sys.stderr)
        return None

    out: dict[str, dict] = {}
    for key, val in hdrs.items():
        rest = key[len(PREFIX):]
        for field in ("-status", "-utilization", "-reset"):
            if rest.endswith(field):
                abbrev = rest[: -len(field)]
                if not abbrev or abbrev in ("overage", "fallback"):
                    continue        # overage-status is "rejected" when healthy
                claim = ABBREV.get(abbrev)
                if claim is None:
                    print(f"ccpool: unknown claim window {abbrev!r} -- not "
                          f"benching on it", file=sys.stderr)
                    continue
                out.setdefault(claim, {})[field[1:]] = val
    return out


def exhausted_claims(limits: dict[str, dict]) -> dict[str, int]:
    """{claim: reset_epoch} for claims Anthropic itself says are spent."""
    out = {}
    now = time.time()
    for claim, row in limits.items():
        util = row.get("utilization")
        with contextlib.suppress(TypeError, ValueError):
            util = float(util) if util is not None else None
        spent = row.get("status") == "rejected" or (
            util is not None and util >= EXHAUSTED)
        if not spent:
            continue
        try:
            reset = int(float(row.get("reset")))
        except (TypeError, ValueError):
            reset = int(now + UNCONFIRMED_BENCH)
        out[claim] = reset
    return out


def confirm_and_bench(name: str, token: str, claim: str | None) -> bool:
    """Return True if the account was genuinely benched."""
    limits = probe_limits(token)
    if limits is None:
        until = int(time.time()) + UNCONFIRMED_BENCH
        bench(name, claim or "five_hour", until)
        print(f"ccpool: {name} benched {UNCONFIRMED_BENCH // 60}m UNCONFIRMED "
              f"-- probe could not answer, so this is a guess, not a verdict",
              file=sys.stderr)
        return True
    hit = exhausted_claims(limits)
    if not hit:
        print(f"ccpool: {name} is NOT out (nothing at {EXHAUSTED:.0%}); the 429 "
              f"came from something other than your quota. Not benching.",
              file=sys.stderr)
        return False
    for c, reset in hit.items():
        bench(name, c, reset)
        print(f"ccpool: {name} benched on {c} until "
              f"{time.strftime('%H:%M', time.localtime(reset))}", file=sys.stderr)
    return True


# --- run ---------------------------------------------------------------------

def run_once(name: str, token: str, argv: list[str], model: str | None) -> tuple[int, int]:
    """Run Claude Code under one account. Returns (watch_signal, claude_rc).

    The watcher runs in-process over the child's stdout rather than as a shell
    pipeline, so both statuses survive -- a `claude | ccpool-watch` pipeline
    would hide claude's own exit code behind the watcher's.
    """
    env = dict(os.environ)
    env["ANTHROPIC_AUTH_TOKEN"] = token
    for stale in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        env.pop(stale, None)

    proc = subprocess.Popen([CLAUDE, *argv], stdout=subprocess.PIPE,
                            env=env, text=True, bufsize=1)
    try:
        signal = W.run(proc.stdout, sys.stdout, model)
    finally:
        proc.stdout.close()
        rc = proc.wait()
    return signal, rc


def cmd_run(args) -> int:
    if not accounts():
        print("ccpool: no accounts. `ccpool add <name>` first.", file=sys.stderr)
        return 2
    if bad := unreachable_keys():
        print(f"ccpool: WARNING cooldown keys nothing reads: {sorted(bad)} -- "
              f"accounts benched on these will never be skipped", file=sys.stderr)

    model = args.model or model_from(args.argv)
    tried: set[str] = set()
    for _ in range(len(accounts())):
        name = pick(model, tried)
        if name is None:
            break
        tried.add(name)
        token = accounts()[name].get("token", "")
        print(f"ccpool: using {name}", file=sys.stderr)
        remember_last(name)

        signal, rc = run_once(name, token, args.argv, model)

        if signal == W.EXIT_SCHEMA:
            print("ccpool: refusing to guess at a changed event schema. "
                  "Check Claude Code's version.", file=sys.stderr)
            return W.EXIT_SCHEMA
        if signal == W.EXIT_TRANSPORT:
            print("ccpool: 429 with no quota claim -- not your usage limit. "
                  "Not benching, not rotating.", file=sys.stderr)
            return rc or 1
        if signal == W.EXIT_QUOTA:
            if confirm_and_bench(name, token, args.claim_hint):
                continue                      # genuinely out -- rotate
            return rc or 1                    # forged 429 -- rotating won't help
        return rc                             # includes success

    when = next_reset(model)
    hint = (f" Earliest reset {time.strftime('%a %H:%M', time.localtime(when))}."
            if when else "")
    print(f"ccpool: every account is benched for this model.{hint}",
          file=sys.stderr)
    return 75


def model_from(argv: list[str]) -> str | None:
    for i, a in enumerate(argv):
        if a == "--model" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--model="):
            return a.split("=", 1)[1]
    return None


# --- account management ------------------------------------------------------

def cmd_add(args) -> int:
    print(f"Paste the token for '{args.name}' (from `claude setup-token` while "
          f"logged into that account), then Ctrl-D:", file=sys.stderr)
    token = sys.stdin.read().strip()
    if not token:
        print("ccpool: empty token, nothing stored.", file=sys.stderr)
        return 2
    v = load(VAULT)
    v.setdefault("accounts", {})[args.name] = {"token": token}
    write_atomic(VAULT, v)
    print(f"ccpool: stored '{args.name}' ({len(token)} chars) in {VAULT}",
          file=sys.stderr)
    return 0


def cmd_ls(_args) -> int:
    accts = accounts()
    if not accts:
        print("no accounts")
        return 0
    now = time.time()
    for name in accts:
        cd = {c: t for c, t in cooldowns(name).items() if t > now}
        if cd:
            soonest = min(cd.values())
            status = (f"benched on {','.join(sorted(cd))} until "
                      f"{time.strftime('%a %H:%M', time.localtime(soonest))}")
        else:
            status = "ready"
        print(f"{'*' if load(STATE).get('last') == name else ' '} {name:<16} {status}")
    return 0


def cmd_status(_args) -> int:
    """Probe every account. Costs one tiny completion per account, against that
    account's own quota -- so this is a command you run, never a poll."""
    accts = accounts()
    if not accts:
        print("no accounts")
        return 0
    bad = 0
    rows = []                    # (name, [cell per claim], tail)
    for name, rec in accts.items():
        limits = probe_limits(rec.get("token", ""))
        if limits is None:
            rows.append((name, [], "UNREACHABLE -- token may be dead"))
            bad += 1
            continue
        parts = []
        for claim in sorted(limits, key=lambda c: ORDER.get(c, 99)):
            row = limits[claim]
            label = LABEL.get(claim, claim)
            reset = fmt_reset(row.get("reset")) if claim in RESET_SHOWN else ""
            util = row.get("utilization")
            try:
                parts.append(f"{label}:{float(util):.0%}"
                             + ("!" if row.get("status") == "rejected" else "")
                             + reset)
            except (TypeError, ValueError):
                parts.append(f"{label}:{row.get('status', '?')}{reset}")
        # The probe already paid for this answer -- record it, so an account
        # that is known to be full never costs a rejected request to discover.
        spent = exhausted_claims(limits)
        for claim, reset in spent.items():
            bench(name, claim, reset)
        tail = (f"[benched: {','.join(LABEL.get(c, c) for c in sorted(spent))}]"
                if spent else "")
        rows.append((name, parts, tail))

    # Column widths from the widest cell, so the ` | ` separators line up.
    ncol = max((len(p) for _, p, _ in rows), default=0)
    wide = [max((len(p[i]) for _, p, _ in rows if len(p) > i), default=0)
            for i in range(ncol)]
    namew = max(len(n) for n, _, _ in rows)
    for name, parts, tail in rows:
        cells = " | ".join(p.ljust(wide[i]) for i, p in enumerate(parts))
        print(f"  {name:<{namew}}  {cells}{'  ' + tail if tail else ''}"
              .rstrip())
    return 1 if bad else 0


def cmd_clear(args) -> int:
    st = load(STATE)
    acct = st.get("accounts", {}).get(args.name)
    if not acct:
        print(f"ccpool: no cooldowns for '{args.name}'", file=sys.stderr)
        return 0
    if args.claim:
        acct.get("cooldown", {}).pop(args.claim, None)
    else:
        acct["cooldown"] = {}
    write_atomic(STATE, st)
    print(f"ccpool: cleared {args.claim or 'all cooldowns'} for '{args.name}'",
          file=sys.stderr)
    return 0


def cmd_hold(args) -> int:
    """Take an account out of rotation by hand, e.g. to save it for something.

    A bench is a bench: this is the same mechanism a real exhaustion uses, so
    `ccpool clear <name>` undoes it. five_hour because it is the one claim in
    every CLAIMS tuple -- benching it skips the account for any model.
    """
    if args.name not in accounts():
        print(f"ccpool: no such account '{args.name}'", file=sys.stderr)
        return 2
    until = int(time.time() + args.hours * 3600)
    bench(args.name, "five_hour", until)
    print(f"ccpool: '{args.name}' held out of rotation until "
          f"{time.strftime('%a %H:%M', time.localtime(until))} "
          f"-- `ccpool clear {args.name}` to release",
          file=sys.stderr)
    return 0


def cmd_rm(args) -> int:
    v = load(VAULT)
    if v.get("accounts", {}).pop(args.name, None) is None:
        print(f"ccpool: no such account '{args.name}'", file=sys.stderr)
        return 2
    write_atomic(VAULT, v)
    print(f"ccpool: removed '{args.name}'", file=sys.stderr)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ccpool", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help="store a token (read from stdin)")
    p.add_argument("name"); p.set_defaults(fn=cmd_add)

    p = sub.add_parser("ls", help="list accounts and cooldowns")
    p.set_defaults(fn=cmd_ls)

    p = sub.add_parser("proxy", help="rotate accounts under a live session")
    p.add_argument("--port", type=int, default=8790)
    p.set_defaults(fn=lambda a: __import__("ccpool_proxy").serve(port=a.port))

    p = sub.add_parser("status", help="probe every account's real quota (costs a call each)")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("rm", help="forget an account")
    p.add_argument("name"); p.set_defaults(fn=cmd_rm)

    p = sub.add_parser("hold", help="take an account out of rotation by hand")
    p.add_argument("name")
    p.add_argument("--hours", type=float, default=24,
                   help="how long to hold it out (default 24)")
    p.set_defaults(fn=cmd_hold)

    p = sub.add_parser("clear", help="undo a bench")
    p.add_argument("name"); p.add_argument("claim", nargs="?")
    p.set_defaults(fn=cmd_clear)

    p = sub.add_parser("run", help="run claude, rotating on rate limits")
    p.add_argument("--model", default=None)
    p.add_argument("--claim-hint", default=None, help=argparse.SUPPRESS)
    p.add_argument("argv", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_run)

    args = ap.parse_args(argv)
    if getattr(args, "argv", None) and args.argv and args.argv[0] == "--":
        args.argv = args.argv[1:]
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
