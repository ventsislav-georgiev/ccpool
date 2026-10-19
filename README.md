# ccpool

Run Claude Code across a pool of accounts, rotating automatically when one hits
its rate limit.

Based on the idea behind [torkay/ccpool](https://github.com/torkay/ccpool), but
a much simpler take: three Python files, standard library only, no daemon, no
config format, no database. A 0600 JSON vault and a small state file.

## Install

```sh
brew install ventsislav-georgiev/tap/ccpool
```

Or just symlink it — there is nothing to build:

```sh
git clone https://github.com/ventsislav-georgiev/ccpool
ln -s "$PWD/ccpool/ccpool.py" ~/.local/bin/ccpool
```

Requires Python 3 and the `claude` CLI on PATH.

## Use

Mint one token per account with `claude setup-token` while logged into that
account, then paste it on stdin (never on argv, never logged):

```sh
ccpool add work
ccpool add personal
ccpool ls
```

Two ways to run:

```sh
# one-shot: picks an eligible account, rotates on rate limits, retries
ccpool run -- -p "do the thing" --model opus

# live session: rotates underneath a running claude, no restart
ccpool proxy &
ANTHROPIC_BASE_URL=http://127.0.0.1:8790 claude
```

`ccpool launch` does the live-session setup in one step, the way
`ollama launch claude` does for Ollama. It starts the proxy if nothing answers on
the port, then runs `claude` with `ANTHROPIC_BASE_URL` pointed at it:

```sh
ccpool launch                          # claude
ccpool launch -- --model opus          # args after -- go to claude
ccpool launch cmux -- claude-teams     # any other tool
```

The proxy runs detached and keeps running after the session ends, so later
launches and other windows share it. Its output goes to `~/.ccpool/proxy.log`.
If the port is held by something that is not a ccpool proxy, `launch` refuses
rather than sending your session to it.

`ccpool statusline` tells you which sessions go through the pool. It prints
`ccpool <account>` while the session's `ANTHROPIC_BASE_URL` answers as a ccpool
proxy, `ccpool down` when nothing answers on that local port, and nothing when
the session goes anywhere else. Behind headroom it reads
`ANTHROPIC_TARGET_API_URL` instead, the hop that has to be ccpool. Use it as the
status line, or call it from your own status-line script:

```json
"statusLine": { "type": "command", "command": "ccpool statusline" }
```

It runs on every status refresh, so give it an interpreter that starts fast: a
pyenv shim adds about 0.3s, `/usr/bin/python3 ~/.local/bin/ccpool statusline`
does not.

Other commands: `status` (probe real quota, costs one call per account), `hold`
(pull an account out of rotation), `clear` (undo a bench), `rm`.

## How it decides an account is exhausted

Two paths, because they have different evidence available.

`run` watches Claude Code's event stream. A 429 from *anything* — a proxy, a
CDN, a local dev server — produces an event indistinguishable from real account
exhaustion, so benching on that signal alone silently walks a healthy pool down
to nothing. The stream therefore only raises a suspicion; `GET
/api/oauth/usage` against `api.anthropic.com` decides, deliberately bypassing
whatever returned the 429.

`proxy` needs no confirmation probe. It holds the upstream connection itself, so
the `anthropic-ratelimit-unified-*` headers it reads are authentic and
utilization arrives free on every response. On a rate-limit rejection it benches
the account, picks the next one, and replays the request — the session sees one
slightly slow response. Replay is only attempted before any body bytes have gone
downstream; a failure mid-stream is passed through untouched.

Rotation works by setting `ANTHROPIC_AUTH_TOKEN` per invocation. Measured
against Claude Code 2.1.220, that variable overrides an existing keychain login
and `CLAUDE_CODE_OAUTH_TOKEN` does not.

## Security

The vault is a 0600 file. That is real protection against other users on the
machine and no protection against anything running as you.

## Environment

| Variable | Default |
| --- | --- |
| `CCPOOL_HOME` | `~/.ccpool` |
| `CCPOOL_CLAUDE` | `claude` |
| `CCPOOL_PROBE_BASE` | `https://api.anthropic.com` |
| `CCPOOL_PROBE_MODEL` | `claude-haiku-4-5-20251001` |

## Tests

```sh
python3 -m pytest
./e2e_test.sh
```
