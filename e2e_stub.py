#!/usr/bin/env python3
"""429 stub with switchable rate-limit headers, for end-to-end watcher tests.

    e2e_stub.py <port> bare    -- 429, no anthropic-ratelimit-unified-* headers
                                  (the false class: proxy/transport 429s)
    e2e_stub.py <port> quota   -- 429 with the full unified header set
                                  (what a genuine account exhaustion should look like)
    e2e_stub.py <port> allowed -- 429 whose own unified-status header says "allowed"
                                  at 11% utilization (round-5 C1 counterexample)

The point is to observe what Claude Code actually puts on stdout for each, rather
than to assert what it ought to. Every wrong fact in this project came from the
latter.
"""
import json
import os
import socket
import sys
import threading
import time

PORT, MODE = int(sys.argv[1]), sys.argv[2]
BODY = b'{"type":"error","error":{"type":"rate_limit_error","message":"stub"}}'

reset = int(time.time()) + 7200

# C1: a 429 whose own unified-status says the account is fine. If Claude Code
# still emits status="rejected" here, then "rejected + claim + reset" means
# "this response carried the header block", not "this account is exhausted".
allowed_headers = (
    f"anthropic-ratelimit-unified-status: allowed\r\n"
    f"anthropic-ratelimit-unified-reset: {reset}\r\n"
    f"anthropic-ratelimit-unified-representative-claim: five_hour\r\n"
    f"anthropic-ratelimit-unified-5h-status: allowed\r\n"
    f"anthropic-ratelimit-unified-5h-reset: {reset}\r\n"
    f"anthropic-ratelimit-unified-5h-utilization: 0.11\r\n"
).encode()

quota_headers = (
    f"anthropic-ratelimit-unified-status: rejected\r\n"
    f"anthropic-ratelimit-unified-reset: {reset}\r\n"
    f"anthropic-ratelimit-unified-representative-claim: seven_day_opus\r\n"
    f"anthropic-ratelimit-unified-5h-status: allowed\r\n"
    f"anthropic-ratelimit-unified-5h-reset: {reset}\r\n"
    f"anthropic-ratelimit-unified-5h-utilization: 0.31\r\n"
    f"anthropic-ratelimit-unified-7d-status: rejected\r\n"
    f"anthropic-ratelimit-unified-7d-reset: {reset}\r\n"
    f"anthropic-ratelimit-unified-7d-utilization: 1.0\r\n"
).encode()

HEAD = (
    b"HTTP/1.1 429 Too Many Requests\r\n"
    b"content-type: application/json\r\n"
    + {"quota": quota_headers, "allowed": allowed_headers}.get(MODE, b"")
    + b"content-length: %d\r\n\r\n" % len(BODY)
)


# `probe` mode: a healthy-looking 200 carrying real-shaped rate-limit headers,
# standing in for api.anthropic.com. STUB_USAGE_PCT decides whether the account
# is actually out. This runs on a DIFFERENT port from the 429 stub on purpose --
# the confirmation probe must not travel through the thing it is adjudicating.
USAGE_PCT = float(os.environ.get("STUB_USAGE_PCT", "0.99"))
_spent = "rejected" if USAGE_PCT >= 0.98 else "allowed"
PROBE_BODY = b'{"type":"message","content":[]}'
PROBE_HEAD = (
    "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n"
    f"anthropic-ratelimit-unified-status: {_spent}\r\n"
    f"anthropic-ratelimit-unified-representative-claim: five_hour\r\n"
    f"anthropic-ratelimit-unified-5h-status: {_spent}\r\n"
    f"anthropic-ratelimit-unified-5h-utilization: {USAGE_PCT}\r\n"
    f"anthropic-ratelimit-unified-5h-reset: {reset}\r\n"
    f"anthropic-ratelimit-unified-7d-status: allowed\r\n"
    f"anthropic-ratelimit-unified-7d-utilization: 0.12\r\n"
    f"anthropic-ratelimit-unified-7d-reset: {reset}\r\n"
    f"anthropic-ratelimit-unified-overage-status: rejected\r\n"
    f"content-length: {len(PROBE_BODY)}\r\n\r\n"
).encode()


def handle(c):
    try:
        c.recv(1 << 20)
        c.sendall(PROBE_HEAD + PROBE_BODY if MODE == "probe" else HEAD + BODY)
    except OSError:
        pass
    finally:
        c.close()


s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", PORT))
s.listen(16)
print(f"stub {MODE} on {PORT} (reset={reset})", flush=True)
while True:
    conn, _ = s.accept()
    threading.Thread(target=handle, args=(conn,), daemon=True).start()
