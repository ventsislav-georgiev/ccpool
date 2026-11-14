#!/usr/bin/env python3
"""ccpool-proxy -- rotate accounts under a LIVE Claude Code session.

    ccpool proxy                       # listens on 127.0.0.1:8790
    ANTHROPIC_BASE_URL=http://127.0.0.1:8790 claude

    ccpool launch                      # both: starts the proxy if it is down

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
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ccpool as C  # noqa: E402
import ccpool_watch as W  # noqa: E402

UPSTREAM = os.environ.get("CCPOOL_UPSTREAM", "https://api.anthropic.com")
# Answered by the proxy itself, never relayed: it is how `ccpool launch` tells
# this proxy apart from anything else holding the port.
HEALTH = "/ccpool/health"
SERVICE = "ccpool-proxy"
PREFIX = "anthropic-ratelimit-unified-"
# ~/.ccpool/usage.jsonl gets one JSON line per relayed response: account, model,
# conversation, every claim's utilization and reset, and the response's usage.
# Enough to fit what each kind of token really costs against the limits.
# shortcut: grows ~10 MB/day and is never rotated; add rotation if that bites.
USAGE_LOCK = threading.Lock()
# Hop-by-hop headers, plus the ones we must own rather than forward.
STRIP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
         "te", "trailers", "transfer-encoding", "upgrade", "host",
         "authorization", "x-api-key", "content-length"}
LOCK = threading.Lock()

# Conversation -> (account, last request). Claude Code tags every request with
# its session id, and a subagent's with its agent id too. Each SESSION gets an
# account of its own, and its subagents follow it there: the prompt cache is
# per organization, so when an account runs out only the sessions on it move
# and cold-start, not every live one at once. That was ~8% of all usage when
# one account served everything (session-tokens, Nov 10-14). Requests without
# a session id (`ccpool run`, other clients) still follow C.pick().
PINS: dict[str, tuple[str, float]] = {}
PIN_IDLE = 3600     # the longest prompt-cache TTL: after that there is nothing to keep
BACKOFF = (1, 2, 4)  # seconds between same-account retries of a throttled request


def conversation(headers) -> str | None:
    sid = headers.get("x-claude-code-session-id")
    return f"{sid}/{headers.get('x-claude-code-agent-id', '')}" if sid else None


def load_pins() -> None:
    """Pins outlive a restart: without them every live session would be
    re-placed, mostly onto another account, and cold-start at once."""
    now = time.time()
    for key, acct in C.load(C.STATE).get("pins", {}).items():
        PINS[key] = (acct, now)


def save_pins() -> None:
    """Caller holds LOCK. The mapping only, on change -- not on every request."""
    st = C.load(C.STATE)
    st["pins"] = {k: acct for k, (acct, _) in PINS.items()}
    C.write_atomic(C.STATE, st)


def pin(key: str, name: str | None) -> None:
    """Caller holds LOCK. Point a conversation at an account (None unpins it)."""
    before = PINS.get(key, (None, 0))[0]
    if name:
        PINS[key] = (name, time.time())
    else:
        PINS.pop(key, None)
    if before != name:
        save_pins()


def choose(key: str | None, model: str | None, tried: set[str]) -> str | None:
    """Caller holds LOCK. The conversation's own account, else its session's,
    else a fresh placement -- each only while it can still serve the model."""
    now = time.time()
    stale = [k for k, (_, seen) in PINS.items() if now - seen > PIN_IDLE]
    for k in stale:
        del PINS[k]
    if stale:
        save_pins()
    if not key:
        name = C.pick(model, tried)
        if name:
            C.remember_last(name)
        return name

    def ok(n):
        return n in C.accounts() and n not in tried and C.eligible(n, model, now)

    sid = key.split("/", 1)[0] + "/"
    own = PINS.get(key, (None, 0))[0]
    # The session's main conversation first, then whichever sibling ran last:
    # a session that just moved pulls its subagents after it.
    siblings = sorted(((k == sid, seen, acct) for k, (acct, seen) in PINS.items()
                       if k.startswith(sid) and k != key), reverse=True)
    name = next((n for n in [own] + [a for _, _, a in siblings] if n and ok(n)), None)
    if name is None:
        name = place(model, tried)
        if name:
            C.remember_last(name)      # `current` is the latest placement
    if name:
        pin(key, name)
    return name


def place(model: str | None, tried: set[str]) -> str | None:
    """Caller holds LOCK. Where a new or moving session goes: the eligible
    account with the most five-hour headroom per live session on it --
    (1 - utilization) x capacity / (sessions + 1). Plans differ up to 12x in
    capacity, so a session on a small one would run it out and move (cold
    cache) far sooner. Live = pinned within the hour: a session idle on its
    user still holds a warm cache. Ties: the window that rolls over soonest."""
    now = time.time()
    load: dict[str, set] = {}
    for k, (acct, _) in PINS.items():
        load.setdefault(acct, set()).add(k.split("/", 1)[0])
    names = list(C.accounts())
    ready = [n for n in names if n not in tried and C.eligible(n, model, now)]
    return min(ready, key=lambda n: (-(1 - util_now(n, now)) * capacity(n)
                                     / (len(load.get(n, ())) + 1),
                                     C.reset_at(n, "five_hour"),
                                     names.index(n)), default=None)


# --- capacity: what one point of five-hour utilization buys, per account ------
# List price, $ per million tokens: input, output, cache read. Cache writes are
# 1.25x input (5 min) and 2x (1 hour). A proxy for what the limit charges, not
# the limit's own weights -- those are unpublished; this only has to rank plans.
PRICES = {"fable": (8, 40, 0.2), "mythos": (8, 40, 0.2), "opus": (4, 20, 0.2),
          "sonnet": (2, 10, 0.1), "haiku": (0.1, 0.5, 0.01)}
CAP: dict[str, list] = {}    # account -> [dollars, points] over observed steps
STEP: dict[str, list] = {}   # account -> [window reset, max utilization, dollars since it rose]
MIN_POINTS = 0.05            # below this an account's estimate is noise


def dollars(model: str | None, u: dict) -> float:
    fam = next((f for f in PRICES if model and f in model), "opus")
    inp, out, read = PRICES[fam]
    cc = u.get("cache_creation") or {}
    w5 = cc.get("ephemeral_5m_input_tokens", u.get("cache_creation_input_tokens", 0) or 0)
    w1 = cc.get("ephemeral_1h_input_tokens", 0)
    return ((u.get("input_tokens") or 0) * inp + (w5 or 0) * inp * 1.25
            + (w1 or 0) * inp * 2 + (u.get("cache_read_input_tokens") or 0) * read
            + (u.get("output_tokens") or 0) * out) / 1e6


def learn(acct: str, model: str | None, limits: dict, usage: dict | None) -> None:
    """Caller holds LOCK. Utilization comes in whole points (2 decimals), so
    the spend between two rises is what one rise cost; summed over many rises
    that is the account's capacity. Only rises of the window's running maximum
    count: the header is stamped when a request starts and the row when it
    ends, so concurrent requests make the series wobble (.34 .35 .34 .35), and
    counting every rise doubled the points on the busiest accounts."""
    try:
        fh = limits["five_hour"]
        util, reset = float(fh["utilization"]), float(fh["reset"])
    except (KeyError, TypeError, ValueError):
        return
    st = STEP.get(acct)
    if not st or st[0] != reset:
        STEP[acct] = [reset, util, 0.0]      # a new window starts over
        return
    st[2] += dollars(model, usage) if usage else 0.0
    if util > st[1]:
        c = CAP.setdefault(acct, [0.0, 0.0])
        c[0] += st[2]
        c[1] += util - st[1]
        st[1], st[2] = util, 0.0


def util_now(acct: str, now: float) -> float:
    st = STEP.get(acct)
    return st[1] if st and st[0] > now else 0.0   # a rolled-over window is empty


def capacity(acct: str) -> float:
    """$ per point; an account without enough data counts as the median."""
    known = sorted(d / p for d, p in CAP.values() if p >= MIN_POINTS)
    d, p = CAP.get(acct, (0.0, 0.0))
    if p >= MIN_POINTS:
        return d / p
    return known[len(known) // 2] if known else 1.0


def load_usage(days: float = 7) -> None:
    """Relearn capacity and current utilization from usage.jsonl at startup.
    shortcut: reads the whole file; bound it (seek to the tail) if it grows big."""
    since = time.time() - days * 86400
    try:
        with open(C.HOME / "usage.jsonl") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                    if row.get("t", 0) >= since:
                        learn(row["acct"], row.get("model"), row.get("limits") or {},
                              row.get("usage"))
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
    except OSError:
        pass

# Idle upstream connections, per account. Without this every request pays a
# fresh TLS handshake to api.anthropic.com -- ~100-300ms on a call whose whole
# round trip is under a second. Keyed by account rather than shared, so a
# rotation never hands one org's connection to another. Most traffic is on one
# account; the few moved conversations keep their own account's pool warm.
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


class UsageTap:
    """Reads `usage` off a response body as it is relayed. Strictly passive:
    it is handed the bytes after they are written, never alters them, and
    never raises -- anything it cannot read leaves the row without usage."""

    def __init__(self, headers):
        enc = (headers.get("content-encoding") or "identity").lower()
        self.inflate = (zlib.decompressobj(16 + zlib.MAX_WBITS) if enc == "gzip"
                        else zlib.decompressobj() if enc == "deflate" else None)
        self.dead = enc not in ("identity", "gzip", "deflate")
        self.sse = "event-stream" in (headers.get("content-type") or "")
        self.buf = b""
        self.usage: dict = {}

    def feed(self, chunk: bytes) -> None:
        if self.dead:
            return
        try:
            if self.inflate:
                chunk = self.inflate.decompress(chunk)
            self.buf += chunk
            if not self.sse:
                if len(self.buf) > 4 << 20:
                    self.dead, self.buf = True, b""
                return
            # message_start carries input and cache usage, message_delta the
            # final cumulative counts; later values win.
            *lines, self.buf = self.buf.split(b"\n")
            for line in lines:
                if line.startswith(b"data:") and b'"usage"' in line:
                    self.take(json.loads(line[5:]))
        except Exception:  # noqa: BLE001
            self.dead = True

    def take(self, event: dict) -> None:
        u = event.get("usage") or (event.get("message") or {}).get("usage")
        if isinstance(u, dict):
            self.usage.update({k: v for k, v in u.items() if v is not None})

    def result(self) -> dict | None:
        if not self.dead and not self.sse and self.buf:
            try:
                self.take(json.loads(self.buf))
            except Exception:  # noqa: BLE001
                pass
        return self.usage or None


def log_usage(row: dict) -> None:
    try:
        line = json.dumps(row, separators=(",", ":")) + "\n"
        with USAGE_LOCK, open(C.HOME / "usage.jsonl", "a") as fh:
            fh.write(line)
    except Exception:  # noqa: BLE001
        pass                     # a diagnostic must never fail a request


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
        if self.path == HEALTH:
            return self.health()
        self.relay()

    def health(self):
        payload = json.dumps({"service": SERVICE, "pid": os.getpid()}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def relay(self):
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        model = self.model_of(body)
        started = time.time()

        fwd = {k: v for k, v in self.headers.items() if k.lower() not in STRIP}
        tried: set[str] = set()
        key = conversation(self.headers)
        moved, back = False, None
        waits, throttles = list(BACKOFF), 0

        for _ in range(max(1, len(C.accounts())) + len(BACKOFF)):
            with LOCK:
                name = choose(key, model, tried)
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

            if status == 429:
                spent = C.exhausted_claims(limits)
                detail = (", ".join(f"{c}={r.get('status','?')}/"
                                    f"{r.get('utilization','?')}"
                                    for c, r in sorted(limits.items()))
                          or "NO unified rate-limit headers")
                retry = headers.get("retry-after")
                try:
                    wait = int(retry or 0)
                except ValueError:
                    wait = 0
                reason = "reported exhausted quota" if spent else "no exhausted quota"
                if not spent:
                    try:
                        snippet = resp.read(200).decode("utf-8", "replace")
                    except OSError:
                        snippet = "<unreadable>"
                    if wait > 3600:
                        # This authentic cooldown belongs to the requested
                        # model's weekly claim, not every model. Unknown model
                        # names keep the old conservative global fallback.
                        known_model = model and any(k in model for k in W.CLAIMS)
                        claim = W.claims_for(model)[-1] if known_model else "five_hour"
                        spent = {claim: int(time.time()) + wait}
                        reason = "long Retry-After for requested model"
                    else:
                        reason += f"; body={snippet!r}"
                chosen = ",".join(sorted(spent)) if spent else "none"
                self.note(f"{name}: 429 model={model!r}; quota={detail}; "
                          f"retry-after={retry or '?'}; cooldown={chosen}; "
                          f"reason={reason}")
                conn.close()
                if spent:
                    with LOCK:
                        for claim, reset in spent.items():
                            C.bench(name, claim, reset)
                    self.note(f"{name} out on {','.join(sorted(spent))} -- "
                              f"rotating to another account and retrying request")
                    continue
                # No spent claim: a throughput limit. Nothing is benched. First
                # retry right here with backoff -- the cache is here, and the
                # callers seen hitting this (one-shot sessions) never retried
                # themselves. Then move the conversation once to the least busy
                # account. Once only: a second account refusing too means the
                # limit is not this account's (one conversation was refused by
                # all 8 in 2s), and walking the pool would turn each client
                # retry into one upstream call per account.
                throttles += 1
                if waits and not moved:
                    step = waits.pop(0)
                    delay = wait if 0 < wait <= 10 else step
                    self.note(f"{name}: throttled -- retrying "
                              f"{(key or 'request')[:8]} here in {delay}s")
                    time.sleep(delay)
                    tried.discard(name)
                    continue
                if key and not moved:
                    with LOCK:
                        back = PINS.get(key, (None, 0))[0]
                        dest = place(model, tried)
                        if dest:
                            pin(key, dest)
                    if dest:
                        self.note(f"{name}: throttled -- moving conversation "
                                  f"{key[:8]} to {dest}")
                        moved = True
                        continue
                elif moved:
                    # Refused there too: the move bought nothing, so the
                    # conversation goes back to where its cache is.
                    with LOCK:
                        pin(key, back)
                return self.fail(429, "upstream rate limit (not account quota)")

            # Retire before streaming, not after: the body can take a minute to
            # drain, and a request arriving in that window would otherwise pick
            # an account already known to be empty.
            if throttles:
                self.note(f"{name}: served {(key or 'request')[:8]} after "
                          f"{throttles} throttled attempt(s)")
            self.retire_if_full(name, limits)
            self.stream_down(status, headers, resp, conn, name, {
                "acct": name, "model": model, "conv": key, "path": self.path,
                "status": status, "limits": limits, "t0": started})
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

    def stream_down(self, status, headers, resp, conn, name, row=None):
        """Past this point nothing may be retried -- bytes are on the wire."""
        done = False
        tap = UsageTap(headers)
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
                tap.feed(chunk)
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
            if row is not None:
                t0 = row.pop("t0")
                usage = tap.result()
                with LOCK:
                    learn(name, row.get("model"), row.get("limits") or {}, usage)
                log_usage({"t": round(time.time(), 3), **row,
                           "ms": round((time.time() - t0) * 1000),
                           "complete": done, "usage": usage})

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


def probe(port: int, host: str = "127.0.0.1", timeout: float = 5) -> dict | None:
    """The health answer of the ccpool proxy on this port: None when nothing
    listens, {} from a proxy too old to answer HEALTH itself.

    Every ccpool proxy names itself in the first Server header, the old ones
    too: they relay the path upstream, but send_response puts ours before
    Anthropic's. Raises OSError when anything else answers, socket.timeout when
    nothing answers in time. http.client rather than urllib, so an
    HTTP(S)_PROXY in the environment is never asked. The default timeout is
    long enough for an old proxy's round trip to Anthropic.
    """
    refused = OSError(f"port {port} is held by something that is not {SERVICE}")
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        try:
            conn.request("GET", HEALTH)
        except ConnectionRefusedError:
            return None
        resp = conn.getresponse()
        raw = resp.read()
        server = (resp.headers.get_all("server") or [""])[0]
        if server.split(" ")[0] != Handler.server_version:
            raise refused
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        return body if resp.status == 200 and isinstance(body, dict) else {}
    except http.client.HTTPException as e:
        raise refused from e
    finally:
        conn.close()


def serve(host="127.0.0.1", port=8790):
    if not C.accounts():
        sys.exit("ccpool: no accounts. `ccpool add <name>` first.")
    load_pins()
    load_usage()
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
