# chrome-downloader-client (English)

**Let Chrome do the downloading; let your program be the dispatcher.**
The program only says "fetch this / how far along / stop". Not a single byte of file
content passes through it — which is exactly why your browser-side accelerator still
sees the traffic.

## What is guaranteed (and is now tested)

- **Byte-path invariant (C1)**: 100% of file bytes are fetched by the browser via
  `chrome.downloads`. Bridge / Client / CLI / MCP / Daemon are command channels only.
  Control frames are capped at **8192 bytes**; a bigger frame closes the link (1009).
- **Non-loopback egress fuse (C2)**: the Bridge installs an in-process guard at start-up.
  Any socket/DNS/subprocess path aiming outside `127.0.0.1` raises `EgressBlocked` and is
  written to an audit log. Spawning code therefore lives **only** in processes that do not
  install the guard (CLI / SDK / MCP).
- **Zero required third-party dependencies (C3)**: `dependencies = []`. `aiohttp` is an
  optional extra; without it the Bridge falls back to a stdlib RFC6455 implementation.
  Build tools (`build`, `twine`) are installed only inside a throw-away venv.

## Install

```bash
python -m pip install --no-build-isolation .
```

(Not marked runnable: this mutates your environment.)

## Three steps

```bash
# runnable
# exit: 0
chrome-dl env --json
```

```bash
# runnable
# exit: 0
chrome-dl extension-path
```

Load that folder in Chrome: `chrome://extensions` → Developer mode →
"Load unpacked". That is the only manual step. No extension ID to copy, no registry keys.

Then:

```bash
# runnable
# exit: 0
chrome-dl daemon start --quiet --state-dir <STATE_DIR> --port <PORT>
```

```bash
# runnable
# exit: 0
chrome-dl download "<URL>" --filename readme-en-demo.bin --sha256 <KNOWN_HEX> --wait --timeout 180 --state-dir <STATE_DIR> --port <PORT>
```

```bash
# runnable
# exit: 0
chrome-dl show <jobId> --state-dir <STATE_DIR> --port <PORT>
```

`show` must print `"sha256Actual": "OK"`. Pass `--sha256` in the first place, or the
Bridge's verify step returns `"SKIP"` and integrity is simply not checked.

## Cold start honesty

If the extension's service worker has been asleep, expect **about 30 seconds at least**
before the first progress tick. That is Chrome's own minimum alarm interval, not a bug.
Clicking the extension icon speeds it up. The same sentence is asserted to appear in
all eight MCP tool descriptions (criterion M13).

## MCP server

`chrome-dl-mcp` is a zero-dependency stdio MCP server speaking two protocol eras
(2026-07-28 and 2025-11-25). Client configuration: `docs/mcp-setup.md`.
Spec verbatim quotes and file hashes: `docs/mcp-spec-archive.md`.

## Turn auto-spawn off

```bash
# runnable
# exit: 0
chrome-dl --no-auto-spawn env --state-dir <STATE_DIR>
```

or `set CHROME_DL_NO_AUTOSPAWN=1`. `chrome-dl daemon stop` only terminates a Bridge it
can attribute to its own state-dir; anything else is left alone and reported.
