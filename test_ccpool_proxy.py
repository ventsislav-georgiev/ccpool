#!/usr/bin/env python3
"""Self-check for ccpool-proxy. `python3 test_ccpool_proxy.py` -- no framework.

The claim under test is the one the whole proxy exists for: a client that gets
rate limited **never finds out**. It sends one request, the proxy swaps accounts
underneath it, and a normal 200 comes back.

A fake upstream stands in for api.anthropic.com and decides per-token whether to
429, so a rotation is only possible if the credential really changed.
"""
import http.server
import json
import os
import shutil
import socket
import socketserver
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

FAILURES = []
SPENT = set()        # token substrings that are "out of quota"
OVERLOAD = set()     # token substrings that 429 WITHOUT a spent claim
FULL = set()         # token substrings that succeed but report 100% used
SEEN = []            # tokens the upstream actually received, in order
CONNS = []           # one entry per upstream TCP connection accepted
BIG = set()          # token substrings that get a large body back


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}{'' if cond else ' -- ' + str(detail)}")
    if not cond:
        FAILURES.append(name)


class Upstream(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def setup(self):
        # One handler instance per TCP connection, so this counts connections
        # rather than requests -- which is exactly what pooling changes.
        super().setup()
        CONNS.append(self.client_address)

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
        auth = self.headers.get("Authorization", "")
        SEEN.append(auth)
        reset = int(time.time()) + 3600

        spent = any(f in auth for f in SPENT)
        overload = any(f in auth for f in OVERLOAD)
        full = any(f in auth for f in FULL)
        hdrs = {
            "5h-status": "rejected" if spent else "allowed",
            "5h-utilization": "1.0" if (spent or full) else "0.42",
            "5h-reset": str(reset),
            "7d-status": "allowed", "7d-utilization": "0.10",
            "7d-reset": str(reset),
            "overage-status": "rejected",   # true on a healthy account
        }
        if overload:
            # Upstream congestion: a 429 with nothing actually exhausted.
            hdrs["5h-status"] = "allowed"
            hdrs["5h-utilization"] = "0.42"

        body = (b'{"type":"error","error":{"message":"limit"}}' if (spent or overload)
                else b'{"type":"message","content":[{"type":"text","text":"hi"}]}')
        if any(f in auth for f in BIG) and not (spent or overload):
            body = b'{"type":"message","pad":"' + b"x" * 400000 + b'"}'
        self.send_response(429 if (spent or overload) else 200)
        for k, v in hdrs.items():
            self.send_header(f"anthropic-ratelimit-unified-{k}", v)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, addr):
        # The proxy closes the upstream socket as soon as it decides to rotate,
        # which is correct and makes this fake upstream log a reset. Expected.
        pass


def post(port, payload=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/messages",
        data=json.dumps(payload or {"model": "claude-opus-5"}).encode(),
        headers={"content-type": "application/json",
                 "authorization": "Bearer client-token-should-be-replaced"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def main():
    up = TServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    home = Path(tempfile.mkdtemp())

    os.environ["CCPOOL_HOME"] = str(home)
    os.environ["CCPOOL_UPSTREAM"] = f"http://127.0.0.1:{up.server_port}"
    import ccpool as C
    import ccpool_proxy as P
    C.HOME, C.VAULT, C.STATE = home, home / "vault.json", home / "state.json"
    C.write_atomic(C.VAULT, {"accounts": {
        "one": {"token": "tok-one"}, "two": {"token": "tok-two"},
        "three": {"token": "tok-three"}}})

    px = TServer(("127.0.0.1", 0), P.Handler)
    threading.Thread(target=px.serve_forever, daemon=True).start()
    port = px.server_port

    try:
        print("\nThe point: a live session survives a limit hit")
        SPENT.clear(); SPENT.add("tok-one")
        SEEN.clear()
        C.write_atomic(C.STATE, {"current": "one"})    # the ACTIVE account is the spent one
        status, body = post(port)
        check("client sees a normal 200, not a rate-limit error", status == 200,
              f"{status} {body[:120]}")
        check("it never learns a rotation happened", "limit" not in body, body[:120])
        check("upstream saw the exhausted account, then a different one",
              len(SEEN) >= 2 and "tok-one" in SEEN[0] and "tok-one" not in SEEN[-1],
              SEEN)
        check("the exhausted account is benched",
              "five_hour" in C.cooldowns("one"), C.cooldowns("one"))
        check("healthy accounts are untouched",
              not C.cooldowns("two") and not C.cooldowns("three"))

        print("\nThe client's own credential is never forwarded")
        check("proxy replaced it", all("client-token" not in a for a in SEEN), SEEN)

        print("\nOne account at a time -- never two, never a gratuitous switch")
        SPENT.clear(); SEEN.clear()
        C.write_atomic(C.STATE, {"current": "two"})
        used = []
        for _ in range(5):
            post(port)
            used.append(SEEN[-1])
        check("every request goes to the same account while it is healthy",
              len(set(used)) == 1, used)
        check("and it is the one already in use, not a fresh pick",
              "tok-two" in used[0], used[0])

        print("\nZero waste: full accounts retire on success, never on rejection")
        SPENT.clear(); SEEN.clear()
        C.write_atomic(C.STATE, {"current": "one"})
        FULL.add("tok-one")          # 200, but the headers say nothing is left
        status, _ = post(port)
        check("the request itself still succeeds", status == 200, status)
        check("no rotation was needed for it", len(SEEN) == 1, SEEN)
        check("but the drained account is benched immediately",
              "five_hour" in C.cooldowns("one"), C.cooldowns("one"))
        SEEN.clear()
        post(port)
        check("so the next request never touches it",
              all("tok-one" not in a for a in SEEN), SEEN)
        FULL.clear()
        C.write_atomic(C.STATE, {"current": "three"})

        print("\nConnections are reused, so a call does not pay for a handshake")
        SPENT.clear(); SEEN.clear(); CONNS.clear()
        C.write_atomic(C.STATE, {"current": "three"})
        for _ in range(4):
            post(port)
        check("all four requests went upstream", len(SEEN) == 4, SEEN)
        check("but they did not open four connections",
              len(CONNS) < 4, f"{len(CONNS)} connections for 4 requests")
        print(f"       ({len(CONNS)} upstream connection(s) for 4 requests)")

        print("A pooled connection the far end already closed is not a failure")
        idle = P.POOL.get("three") or []
        check("there is something in the pool to go stale", idle, P.POOL)
        for conn in idle:
            conn.sock.close()          # server-side close, invisible until used
        SEEN.clear()
        status, body = post(port)
        check("the request still succeeds on a fresh connection", status == 200,
              f"{status} {body[:120]}")
        check("and it was not silently sent twice", len(SEEN) == 1, SEEN)

        print("A client that hangs up mid-stream cannot corrupt the next response")
        BIG.add("tok-three")
        raw = socket.create_connection(("127.0.0.1", port), timeout=10)
        payload = json.dumps({"model": "claude-opus-5"}).encode()
        raw.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: x\r\n"
                    b"content-type: application/json\r\n"
                    b"content-length: %d\r\n\r\n%s" % (len(payload), payload))
        raw.recv(64)                   # take a sip, then walk away
        raw.close()
        time.sleep(0.3)
        BIG.clear()
        status, body = post(port)
        check("the next response is complete and its own", status == 200, status)
        check("not spliced with the abandoned one",
              json.loads(body).get("content", [{}])[0].get("text") == "hi",
              body[:200])

        print("\nUpstream congestion is not an account problem")
        SPENT.clear(); OVERLOAD.clear()
        OVERLOAD.update({"tok-one", "tok-two", "tok-three"})
        SEEN.clear()
        before = {n: dict(C.cooldowns(n)) for n in C.accounts()}
        status, body = post(port)
        check("a 429 with nothing exhausted is passed through", status == 429, status)
        check("and says it is not your quota", "not account quota" in body, body[:160])
        check("nobody gets benched for it",
              all(C.cooldowns(n) == before[n] for n in C.accounts()),
              {n: C.cooldowns(n) for n in C.accounts()})
        OVERLOAD.clear()

        print("\nWhole pool exhausted is reported honestly")
        SPENT.update({"tok-one", "tok-two", "tok-three"})
        status, body = post(port)
        check("client gets a 429", status == 429, status)
        check("message names the pool, not a single account",
              "all pooled accounts" in body, body[:200])
        check("and says when it comes back", "reset" in body.lower(), body[:200])
    finally:
        px.shutdown(); up.shutdown()
        shutil.rmtree(home, ignore_errors=True)

    print(f"\n{'FAILED: ' + ', '.join(FAILURES) if FAILURES else 'all checks passed'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
