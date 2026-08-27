#!/usr/bin/env python3
"""ccpool-proxy -- rotate accounts under a LIVE Claude Code session.

    ccpool proxy                       # listens on 127.0.0.1:8790
    ANTHROPIC_BASE_URL=http://127.0.0.1:8790 claude

A running `claude` process reads its *environment* once, at startup, so
ANTHROPIC_AUTH_TOKEN cannot rotate a live session. (It does re-read the keychain
per request -- logging into a new account in one window visibly switches every
other open window -- so a keychain swapper is a real alternative. It mutates
your actual login state, which this does not.)

On a rate-limit rejection the proxy benches that account, picks the next one,
and **replays the same request** upstream. The session sees one slightly slow
response and carries on. No restart, nobody at the keyboard.

Why a proxy is right here and was wrong before
----------------------------------------------
Earlier drafts used a proxy as an *observation point*, to read rate-limit
headers off the wire. Claude Code already puts that on stdout, so the proxy was
deleted -- correctly. This is an *interception point*, and stdout cannot be one.

It also happens to fix the problem that broke every stdout-based design. This
process holds the connection to api.anthropic.com itself, so the
`anthropic-ratelimit-unified-*` headers it reads are authentic. The C1
counterexample -- an intermediary synthesising a 429 that looks exactly like
real exhaustion -- cannot apply to the one hop that has no intermediary. No
confirmation probe is needed, and utilization arrives free on every response.

Retry is only safe before any body bytes have gone downstream. A rate-limit 429
arrives as a complete response before streaming starts, so that holds; a failure
mid-stream is not retried and is passed through.
"""
from __future__ import annotations

import http.client
import http.server
import json
import os
import socketserver
import sys
import threading
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ccpool as C  # noqa: E402

UPSTREAM = os.environ.get("CCPOOL_UPSTREAM", "https://api.anthropic.com")
PREFIX = "anthropic-ratelimit-unified-"
# Hop-by-hop headers, plus the ones we must own rather than forward.
STRIP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
         "te", "trailers", "transfer-encoding", "upgrade", "host",
         "authorization", "x-api-key", "content-length"}
LOCK = threading.Lock()

# Idle upstream connections, per account. Without this every request pays a
# fresh TLS handshake to api.anthropic.com -- ~100-300ms on a call whose whole
# round trip is under a second. Keyed by account rather than shared, so a
# rotation never hands one org's connection to another; there is one active
# account at a time anyway, so the pool stays warm regardless.
POOL: dict[str, list] = {}
POOL_LOCK = threading.Lock()
POOL_MAX = 8


def upstream_conn():
    u = urllib.parse.urlsplit(UPSTREAM)
    cls = (http.client.HTTPSConnection if u.scheme == "https"
           else http.client.HTTPConnection)
    return cls(u.hostname, u.port, timeout=900)


def checkout(name: str):
    """An idle connection for this account, or a new one."""
    with POOL_LOCK:
        idle = POOL.get(name) or []
        while idle:
            conn = idle.pop()
            if conn.sock is not None:
                return conn, True
            conn.close()
    return upstream_conn(), False


def checkin(name: str, conn) -> None:
    if conn.sock is None:
        conn.close()
        return
    with POOL_LOCK:
        idle = POOL.setdefault(name, [])
        if len(idle) >= POOL_MAX:
            conn.close()
            return
        idle.append(conn)


def record_utilization(name: str, headers) -> dict[str, dict]:
    """Every response carries the account's real quota. Free, and authentic:
    there is no intermediary between here and Anthropic to forge it."""
    hdrs = {k.lower(): v for k, v in headers.items()
            if k.lower().startswith(PREFIX)}
    limits: dict[str, dict] = {}
    for key, val in hdrs.items():
        rest = key[len(PREFIX):]
        for field in ("-status", "-utilization", "-reset"):
            if rest.endswith(field):
                abbrev = rest[: -len(field)]
                if not abbrev or abbrev in ("overage", "fallback"):
                    continue      # overage-status reads "rejected" when healthy
                claim = C.ABBREV.get(abbrev)
                if claim:
                    limits.setdefault(claim, {})[field[1:]] = val
    return limits


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ccpool"

    def log_message(self, fmt, *a):
        pass

    def note(self, msg):
        # Timestamped: without one, a log line cannot be correlated with the
        # slowdown a human noticed, which is exactly what it is read for.
        print(f"{time.strftime('%H:%M:%S')} ccpool-proxy: {msg}",
              file=sys.stderr, flush=True)

    def do_POST(self):
        self.relay()

    def do_GET(self):
        self.relay()

    def relay(self):
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        model = self.model_of(body)

        fwd = {k: v for k, v in self.headers.items() if k.lower() not in STRIP}
        tried: set[str] = set()

        for _ in range(max(1, len(C.accounts()))):
            with LOCK:
                name = C.pick(model, tried)
            if name is None:
                return self.no_accounts(model)
            tried.add(name)
            token = C.accounts()[name].get("token", "")

            try:
                status, headers, resp, conn = self.send_upstream(
                    fwd, body, token, name)
            except OSError as e:
                self.note(f"{name}: upstream unreachable ({e.__class__.__name__})")
                return self.fail(502, "upstream unreachable")

            limits = record_utilization(name, headers)
            with LOCK:
                C.remember_last(name)

            if status == 429:
                spent = C.exhausted_claims(limits)
                if not spent:
                    # "No exhausted claim" has two very different causes that
                    # used to look identical in the log: quota really is fine
                    # (upstream congestion), or no unified headers arrived at
                    # all and we simply cannot tell. Record which.
                    detail = (", ".join(f"{c}={r.get('status','?')}/"
                                        f"{r.get('utilization','?')}"
                                        for c, r in sorted(limits.items()))
                              or "NO unified rate-limit headers on this 429")
                    try:
                        snippet = resp.read(200).decode("utf-8", "replace")
                    except OSError:
                        snippet = "<unreadable>"
                    retry = headers.get("retry-after")
                    self.note(f"{name}: 429 not quota -- {detail}"
                              + (f"; retry-after={retry}" if retry else "")
                              + f"; body={snippet!r}")
                    # A rate-limit 429 telling us to come back in over an hour
                    # is exhaustion in everything but headers (seen live:
                    # seven_day at 0.97 "allowed_warning", retry-after of two
                    # days). Bench for exactly that long -- otherwise the
                    # client hammers a dead account until reset. five_hour,
                    # not a made-up claim: it is the one claim in every CLAIMS
                    # tuple, so the picker actually skips the account (same
                    # reasoning as `ccpool hold`).
                    try:
                        wait = int(retry or 0)
                    except ValueError:
                        wait = 0
                    if wait > 3600:
                        spent = {"five_hour": int(time.time()) + wait}
                conn.close()
                if spent:
                    with LOCK:
                        for claim, reset in spent.items():
                            C.bench(name, claim, reset)
                    self.note(f"{name} out on {','.join(sorted(spent))} -- "
                              f"rotating, session continues")
                    continue
                # A 429 carrying no spent claim is upstream load, not quota.
                # Rotating would not help and benching would be a lie.
                return self.fail(429, "upstream rate limit (not account quota)")

            # Retire before streaming, not after: the body can take a minute to
            # drain, and a request arriving in that window would otherwise pick
            # an account already known to be empty.
            self.retire_if_full(name, limits)
            self.stream_down(status, headers, resp, conn, name)
            return

        self.no_accounts(model)

    def send_upstream(self, fwd, body, token, name):
        headers = dict(fwd)
        headers["Authorization"] = f"Bearer {token}"
        if body:
            headers["Content-Length"] = str(len(body))
        # A pooled connection can have been closed by the far end since we last
        # used it, and there is no way to know until the write fails. So: try
        # it, and on failure fall back to a fresh one exactly once. Replaying is
        # safe for the same reason the rotation retry is -- nothing has gone
        # downstream yet, and a request that died on send was never processed.
        for reused in (True, False):
            conn, was_pooled = (checkout(name) if reused else (upstream_conn(), False))
            try:
                conn.request(self.command, self.path, body=body, headers=headers)
                resp = conn.getresponse()
                return resp.status, resp.headers, resp, conn
            except (http.client.HTTPException, OSError):
                conn.close()
                if not (reused and was_pooled):
                    raise    # a brand new connection failing is a real error

    def stream_down(self, status, headers, resp, conn, name):
        """Past this point nothing may be retried -- bytes are on the wire."""
        done = False
        try:
            self.send_response(status)
            for k, v in headers.items():
                if k.lower() in ("connection", "transfer-encoding",
                                 "content-length", "keep-alive"):
                    continue
                self.send_header(k, v)
            self.send_header("Connection", "close")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                self.wfile.write(b"%X\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            done = True
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            # Only a connection drained to the end of its body can be reused.
            # A client that hung up mid-stream leaves unread bytes on the
            # socket, and handing that to the next request would splice one
            # response into another.
            if done and resp.isclosed() and not resp.will_close:
                checkin(name, conn)
            else:
                conn.close()

    def retire_if_full(self, name, limits):
        """Bench an account the moment a SUCCESSFUL response says it is full.

        This is the whole answer to "rotate early and waste the tail, or rotate
        late and eat a failed request". Neither: the response that consumed the
        last of the quota also reports the quota is gone, so the next request
        picks a different account and no request is ever rejected. Zero forfeit,
        zero wasted round trip.
        """
        spent = C.exhausted_claims(limits)
        with LOCK:
            C.record_resets(name, limits)
            for claim, reset in spent.items():
                C.bench(name, claim, reset)
        if spent:
            self.note(f"{name} now full on {','.join(sorted(spent))} -- retired "
                      f"before it could reject anything")

    def model_of(self, body):
        try:
            return json.loads(body).get("model")
        except Exception:  # noqa: BLE001
            return None

    def no_accounts(self, model):
        with LOCK:
            when = C.next_reset(model)
        hint = (f" Earliest reset {time.strftime('%a %H:%M', time.localtime(when))}."
                if when else "")
        self.note(f"every account is benched.{hint}")
        self.fail(429, f"all pooled accounts are rate limited.{hint}")

    def fail(self, status, message):
        payload = json.dumps({"type": "error",
                              "error": {"type": "rate_limit_error" if status == 429
                                        else "api_error",
                                        "message": f"ccpool: {message}"}}).encode()
        try:
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(host="127.0.0.1", port=8790):
    if not C.accounts():
        sys.exit("ccpool: no accounts. `ccpool add <name>` first.")
    srv = Server((host, port), Handler)
    print(f"ccpool-proxy on http://{host}:{port} -> {UPSTREAM}\n"
          f"  {len(C.accounts())} accounts. Point Claude Code at it:\n"
          f"  export ANTHROPIC_BASE_URL=http://{host}:{port}\n"
          f"  ...or, behind headroom, ANTHROPIC_TARGET_API_URL (headroom owns\n"
          f"  ANTHROPIC_BASE_URL). ccpool must stay the last hop before\n"
          f"  Anthropic so its rate-limit headers have no intermediary.",
          file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    serve(port=int(sys.argv[1]) if len(sys.argv) > 1 else 8790)
