"""Render an asciinema v2 cast as a self-contained animated SVG (no dependencies).

Minimal terminal model: printable text, \\r, \\n, and SGR colour codes for the
handful of styles the recorder emits; the screen scrolls to keep the last
ROWS lines. One SVG frame per distinct screen state (sampled at --fps), each
shown for its time slice by a CSS keyframe animation; GitHub renders it in a
README via <img>.

Usage: uv run python scripts/cast_to_svg.py docs/demo.cast docs/demo.svg
"""
import html
import json
import re
import sys

FPS = 4
CW, LH, PAD = 7.8, 17, 14          # char width, line height, padding (px)
COLORS = {"32": "#4ec9b0", "2": "#7d8590", "1": None, "31": "#f47067", "33": "#e3b341"}
BG, FG = "#0d1117", "#c9d1d9"
SGR = re.compile(r"\x1b\[([0-9;]*)m")
OTHER_ESC = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def screens(events, rows, cols):
    lines, cur, style = [[]], [], {}
    out, last_t = [], None

    def snapshot():
        return [l[:] for l in lines[-rows:]]

    for t, kind, data in events:
        if kind != "o":
            continue
        pos = 0
        for m in SGR.finditer(data):
            chunk = OTHER_ESC.sub("", data[pos:m.start()])
            for ch in chunk:
                if ch == "\n":
                    lines.append([])
                elif ch == "\r":
                    continue
                else:
                    if len(lines[-1]) >= cols:      # soft wrap
                        lines.append([])
                    lines[-1].append((ch, dict(style)))
            codes = [c for c in m.group(1).split(";") if c] or ["0"]
            for c in codes:
                if c == "0":
                    style = {}
                elif c in COLORS:
                    style = {"fill": COLORS[c], "bold": c == "1" or style.get("bold")}
            pos = m.end()
        for ch in OTHER_ESC.sub("", data[pos:]):
            if ch == "\n":
                lines.append([])
            elif ch != "\r":
                if len(lines[-1]) >= cols:          # soft wrap
                    lines.append([])
                lines[-1].append((ch, dict(style)))
        if last_t is None or t - last_t >= 1 / FPS:
            out.append((t, snapshot()))
            last_t = t
        else:
            out[-1] = (out[-1][0], snapshot())
    return out


def render_line(cells, y):
    parts, run, run_style = [], "", None
    for ch, st in cells + [("", None)]:
        key = (st or {}).get("fill"), (st or {}).get("bold")
        if st is None or key != run_style:
            if run:
                fill, bold = run_style
                attrs = (f' fill="{fill}"' if fill else "") + (' font-weight="bold"' if bold else "")
                parts.append(f"<tspan{attrs}>{html.escape(run)}</tspan>")
            run, run_style = ch, key
        else:
            run += ch
    return f'<text x="{PAD}" y="{y}" xml:space="preserve">{"".join(parts)}</text>'


def main(src, dst):
    with open(src) as f:
        header = json.loads(f.readline())
        events = [json.loads(l) for l in f if l.strip()]
    cols, rows = header["width"], header["height"]
    frames = screens(events, rows, cols)
    total = events[-1][0] + 3.0
    w, h = int(cols * CW + 2 * PAD), int(rows * LH + 2 * PAD + 22)
    css, groups = [], []
    for i, (t, scr) in enumerate(frames):
        t_end = frames[i + 1][0] if i + 1 < len(frames) else total
        a, b = 100 * t / total, 100 * t_end / total
        css.append(f"@keyframes f{i}{{0%,{a:.3f}%{{opacity:0}}{a:.3f}%,{b:.3f}%{{opacity:1}}"
                   f"{min(b + 0.001, 100):.3f}%,100%{{opacity:0}}}}.f{i}{{animation:f{i} {total:.1f}s steps(1) infinite}}")
        body = "".join(render_line(line, PAD + 22 + (r + 1) * LH - 4) for r, line in enumerate(scr) if line)
        groups.append(f'<g class="f{i}" opacity="0">{body}</g>')
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
           f'font-family="ui-monospace,SFMono-Regular,Menlo,Consolas,monospace" font-size="13">'
           f'<style>{"".join(css)}</style>'
           f'<rect width="100%" height="100%" rx="8" fill="{BG}"/>'
           f'<circle cx="18" cy="15" r="5" fill="#f47067"/><circle cx="34" cy="15" r="5" fill="#e3b341"/>'
           f'<circle cx="50" cy="15" r="5" fill="#4ec9b0"/>'
           f'<g fill="{FG}">{"".join(groups)}</g></svg>')
    with open(dst, "w") as f:
        f.write(svg)
    print(f"{dst}: {len(frames)} frames, {total:.0f}s, {len(svg) // 1024} KiB")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
