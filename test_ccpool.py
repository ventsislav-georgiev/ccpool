#!/usr/bin/env python3
"""Self-check for ccpool rotation. `python3 test_ccpool.py` -- no framework.

Runs the real wrapper against a fake `claude` and a fake usage API, so the
rotation logic is exercised end to end without touching a real account.

The two cases that matter, and they pull in opposite directions:

  * exhausted account  -> MUST bench and rotate, or the pool is pointless
  * forged 429 (C1)    -> MUST NOT bench, or the pool eats itself

Anything that gets one right by ignoring the other is not a fix.
"""
import http.server
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
FAILURES = []
USAGE = {}          # token-substring -> percent for five_hour
HITS = {"usage": 0}


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}{'' if cond else ' -- ' + str(detail)}")
    if not cond:
        FAILURES.append(name)


class Handler(http.server.BaseHTTPRequestHandler):
    """Stands in for api.anthropic.com's /v1/messages, headers and all.

    Header names and values are copied from a real 200 captured against the
    live API, including `overage-status: rejected` on a perfectly healthy
    account -- the trap that produced two wrong drafts.
    """

    def do_POST(self):
        auth = self.headers.get("Authorization", "")
        util = 0.10
        for frag, p in USAGE.items():
            if frag in auth:
                util = p
        HITS["usage"] += 1
        reset = int(time.time()) + 3600
        body = b'{"type":"message","content":[]}'
        self.send_response(200)
        for k, v in {
            "5h-status": "rejected" if util >= 0.98 else "allowed",
            "5h-utilization": str(util),
            "5h-reset": str(reset),
            "7d-status": "allowed",
            "7d-utilization": "0.12",
            "7d-reset": str(reset + 86400),
            "overage-status": "rejected",
            "overage-disabled-reason": "org_level_disabled",
            "representative-claim": "five_hour",
            "status": "rejected" if util >= 0.98 else "allowed",
        }.items():
            self.send_header(f"anthropic-ratelimit-unified-{k}", v)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def ccpool(*args, home=None, api=None, stdin=None, claude=None):
    env = dict(os.environ)
    env["CCPOOL_HOME"] = str(home)
    env["CCPOOL_CLAUDE"] = claude or f"{sys.executable} {HERE / 'fake_claude.py'}"
    if api:
        env["CCPOOL_PROBE_BASE"] = api
    # fake_claude is a script, so CCPOOL_CLAUDE needs to be argv-splittable
    env["CCPOOL_CLAUDE"] = str(HERE / "fake_claude_sh")
    return subprocess.run([sys.executable, str(HERE / "ccpool.py"), *args],
                          env=env, input=stdin, capture_output=True, text=True)


def add(home, name, token, api):
    return ccpool("add", name, home=home, api=api, stdin=token)


def main():
    # A tiny exec shim so CCPOOL_CLAUDE can be a single argv[0].
    shim = HERE / "fake_claude_sh"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{HERE}/fake_claude.py" "$@"\n')
    shim.chmod(0o755)

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    api = f"http://127.0.0.1:{srv.server_port}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    d = Path(tempfile.mkdtemp())
    home = d / "home"
    try:
        print("\nSetup")
        add(home, "alpha", "tok-quota-alpha", api)
        add(home, "beta", "tok-ok-beta", api)
        r = ccpool("ls", home=home, api=api)
        check("ls shows both accounts", "alpha" in r.stdout and "beta" in r.stdout, r.stdout)
        check("ls never prints a token",
              "tok-" not in r.stdout, r.stdout)
        vault = home / "vault.json"
        check("vault is 0600", stat.S_IMODE(vault.stat().st_mode) == 0o600,
              oct(stat.S_IMODE(vault.stat().st_mode)))

        print("\nThe goal: exhausted account benches and rotates, unattended")
        USAGE.clear(); USAGE["tok-quota-alpha"] = 0.995     # alpha genuinely out
        r = ccpool("run", "--", "-p", "hi", home=home, api=api)
        check("run exits 0 -- work continued on another account", r.returncode == 0,
              f"rc={r.returncode} err={r.stderr[-300:]}")
        check("it actually switched credentials", "ran under tok-ok-beta" in r.stdout,
              r.stdout[-200:])
        check("alpha was benched", "alpha" in r.stderr and "benched" in r.stderr,
              r.stderr[-300:])
        r2 = ccpool("ls", home=home, api=api)
        check("bench is durable across invocations",
              "benched" in r2.stdout.split("beta")[0], r2.stdout)

        print("\nC1: a forged 429 from a healthy account must NOT bench it")
        home2 = d / "home2"
        add(home2, "solo", "tok-quota-solo", api)
        USAGE.clear(); USAGE["tok-quota-solo"] = 0.10       # healthy, per Anthropic
        before = HITS["usage"]
        r = ccpool("run", "--", "-p", "hi", home=home2, api=api)
        check("the probe was actually consulted", HITS["usage"] > before)
        check("healthy account is NOT benched", "NOT out" in r.stderr, r.stderr[-300:])
        check("and no cooldown was written",
              "benched" not in ccpool("ls", home=home2, api=api).stdout)
        check("failure surfaces rather than silently rotating", r.returncode != 0)

        print("\nTransport 429: never a bench, never a rotation")
        home3 = d / "home3"
        add(home3, "t1", "tok-transport-t1", api)
        add(home3, "t2", "tok-ok-t2", api)
        before = HITS["usage"]
        r = ccpool("run", "--", "-p", "hi", home=home3, api=api)
        check("not benched", "benched" not in ccpool("ls", home=home3, api=api).stdout)
        check("no probe -- nothing to confirm", HITS["usage"] == before)
        check("did not rotate to t2", "ran under tok-ok-t2" not in r.stdout)
        check("says it is not a usage limit", "not your usage limit" in r.stderr,
              r.stderr[-200:])

        print("\nStickiness (cache cost is real: every switch is a full cache write)")
        home4 = d / "home4"
        add(home4, "s1", "tok-ok-s1", api)
        add(home4, "s2", "tok-ok-s2", api)
        first = ccpool("run", "--", "-p", "hi", home=home4, api=api).stdout
        second = ccpool("run", "--", "-p", "hi", home=home4, api=api).stdout
        check("consecutive runs stay on one account",
              first.count("ran under") == 1 and first == second, (first, second))

        print("\nExhaustion of the whole pool is reported, not hidden")
        home5 = d / "home5"
        add(home5, "e1", "tok-quota-e1", api)
        USAGE.clear(); USAGE["tok-quota-e1"] = 0.999
        r = ccpool("run", "--", "-p", "hi", home=home5, api=api)
        check("exits 75 when nothing is usable", r.returncode == 75, r.returncode)
        check("names when it comes back", "reset" in r.stderr.lower(), r.stderr[-200:])

        print("\nOperator can undo a wrong bench")
        r = ccpool("clear", "e1", home=home5, api=api)
        check("clear reports success", r.returncode == 0)
        check("account is ready again",
              "ready" in ccpool("ls", home=home5, api=api).stdout)

        print("\nProbe failure is a guess, and says so")
        home6 = d / "home6"
        add(home6, "p1", "tok-quota-p1", "http://127.0.0.1:1")   # nothing listens
        add(home6, "p2", "tok-ok-p2", "http://127.0.0.1:1")
        r = ccpool("run", "--", "-p", "hi", home=home6, api="http://127.0.0.1:1")
        check("unreachable probe still lets work continue", r.returncode == 0,
              f"rc={r.returncode} {r.stderr[-200:]}")
        check("bench is short and labelled UNCONFIRMED", "UNCONFIRMED" in r.stderr,
              r.stderr[-300:])

        print("\nOperator can pull an account out of rotation by hand")
        home8 = d / "home8"
        add(home8, "h1", "tok-ok-h1", api)
        add(home8, "h2", "tok-ok-h2", api)
        check("holding a name that does not exist is an error, not a no-op",
              ccpool("hold", "nope", home=home8, api=api).returncode == 2)
        ccpool("hold", "h1", home=home8, api=api)
        r = ccpool("run", "--", "-p", "hi", home=home8, api=api)
        check("the held account is skipped", "ran under tok-ok-h2" in r.stdout,
              r.stdout[-200:])
        ccpool("clear", "h1", home=home8, api=api)
        cd = json.loads((home8 / "state.json").read_text())["accounts"]["h1"]
        check("clear releases it", not cd.get("cooldown"), cd)

        print("\n`status` records what it already paid to learn")
        home7 = d / "home7"
        add(home7, "s-full", "tok-quota-s-full", api)
        add(home7, "s-ok", "tok-ok-s-ok", api)
        USAGE.clear(); USAGE["tok-quota-s-full"] = 0.995
        r = ccpool("status", home=home7, api=api)
        check("status still reports both", "s-full" in r.stdout and "s-ok" in r.stdout,
              r.stdout)
        check("the full one is flagged as benched", "[benched: five_hour]" in r.stdout,
              r.stdout)
        st = json.loads((home7 / "state.json").read_text())
        cd = st.get("accounts", {})
        check("and the cooldown is on disk, so no request pays to rediscover it",
              "five_hour" in cd.get("s-full", {}).get("cooldown", {}), st)
        check("the healthy one is left alone", "s-ok" not in cd, st)
        check("the reset is Anthropic's, not a 15-minute guess",
              cd["s-full"]["cooldown"]["five_hour"] > time.time() + 1800, st)
    finally:
        srv.shutdown()
        shutil.rmtree(d, ignore_errors=True)
        shim.unlink(missing_ok=True)

    print(f"\n{'FAILED: ' + ', '.join(FAILURES) if FAILURES else 'all checks passed'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
