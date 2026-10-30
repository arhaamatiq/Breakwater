"""Record the README demo: run real commands, capture real output, write an
asciinema v2 cast (then render with `npx svg-term-cli --in demo.cast --out demo.svg`).

Nothing is faked: every line is produced by the command shown. Long silent
waits are compressed (capped at --max-gap seconds) so the recording stays
watchable; the cast header records the compression.

Usage: uv run python scripts/record_demo.py docs/demo.cast "cmd1" "# comment" "cmd2" ...
"""
import json
import os
import subprocess
import sys
import time

COLS, ROWS = 110, 32
MAX_GAP = float(os.environ.get("MAX_GAP", "1.2"))
TYPE_DELAY = float(os.environ.get("TYPE_DELAY", "0.035"))
HOLD = float(os.environ.get("HOLD", "1.2"))          # pause after each command's output
COMMENT_HOLD = float(os.environ.get("COMMENT_HOLD", "0.6"))


def main():
    out, cmds = sys.argv[1], sys.argv[2:]
    events, t = [], 0.6

    def emit(text, dt=0.0):
        nonlocal t
        t += dt
        events.append([round(t, 3), "o", text])

    for cmd in cmds:
        emit("\x1b[1;32m$\x1b[0m ")
        if cmd.startswith("#"):
            emit(f"\x1b[2m{cmd}\x1b[0m\r\n", 0.2)
            t += COMMENT_HOLD
            continue
        for ch in cmd:
            emit(ch, TYPE_DELAY)
        emit("\r\n", 0.25)
        p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             env={**os.environ, "PYTHONUNBUFFERED": "1", "COLUMNS": str(COLS)})
        last = time.monotonic()
        for line in iter(p.stdout.readline, b""):
            now = time.monotonic()
            gap = min(now - last, MAX_GAP)
            last = now
            emit(line.decode(errors="replace").rstrip("\n") + "\r\n", max(gap, 0.04))
        p.wait()
        t += HOLD
    header = {"version": 2, "width": COLS, "height": ROWS, "timestamp": int(time.time()),
              "title": "breakwater", "env": {"SHELL": "/bin/zsh", "TERM": "xterm-256color"},
              "x-note": f"real output; idle gaps capped at {MAX_GAP}s"}
    with open(out, "w") as f:
        f.write(json.dumps(header) + "\n")
        for e in events:
            f.write(json.dumps(e) + "\n")
    print(f"{out}: {len(events)} events, {t:.1f}s")


if __name__ == "__main__":
    main()
