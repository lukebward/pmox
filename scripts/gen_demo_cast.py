"""Build docs/demo.cast (asciicast v2) from timed pmox capture files.

Usage: python scripts/gen_demo_cast.py <cap1.txt> <cap2.txt> <out.cast>

Each capture file holds real `pmox` output, one line per row, as
"<elapsed-seconds>\t<text>" (see the capture command in scripts/README
or the repo docs). The cast replays a prompt, types each command at
human speed, then emits the captured lines with long waits clamped so
the demo stays snappy. Render with:

    agg --theme dracula --font-size 16 --font-family "Cascadia Mono" \
        --renderer resvg docs/demo.cast docs/demo.gif

(resvg draws box-drawing table borders without gaps; the default
swash renderer leaves notches at the line-height seams.)

Capture command (PowerShell; creates a real VM, delete it after):

    $sw = [Diagnostics.Stopwatch]::StartNew()
    pmox --no-json --dangerous vm up web --image ubuntu-24.04 --node lukeserver --wait 2>&1 |
      ForEach-Object { "{0:F3}`t{1}" -f $sw.Elapsed.TotalSeconds, $_ } |
      Out-File -Encoding utf8 cap1.txt
"""
import json
import re
import sys

TYPING_S = 0.035       # per keystroke
MAX_GAP_S = 2.5        # clamp real waits to this
PROMPT = "\x1b[1;34m>\x1b[0m "
IP_RE = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}")

COMMANDS = [
    "pmox --dangerous vm up web --image ubuntu-24.04 --node lukeserver --wait",
    "pmox vm ip 113 --wait",
]


def read_capture(path):
    rows = []
    with open(path, encoding="utf-8-sig") as fh:
        for raw in fh:
            raw = raw.rstrip("\n")
            if not raw:
                continue
            t, _, text = raw.partition("\t")
            rows.append((float(t), text))
    return rows


def style(line, first):
    if first:
        line = "\x1b[1m" + IP_RE.sub("\x1b[36m\\g<0>\x1b[39m", line) + "\x1b[0m"
    return line


def main(cap_paths, out_path):
    events = []
    t = 0.5
    for cmd, cap_path in zip(COMMANDS, cap_paths):
        events.append([t, "o", PROMPT])
        t += 0.6
        for ch in cmd:
            events.append([t, "o", ch])
            t += TYPING_S
        t += 0.4
        events.append([t, "o", "\r\n"])
        rows = read_capture(cap_path)
        prev = 0.0
        for i, (rel, text) in enumerate(rows):
            t += min(rel - prev, MAX_GAP_S)
            prev = rel
            events.append([t, "o", style(text, first=i == 0) + "\r\n"])
        events.append([t + 0.8, "o", "\r\n"])
        t += 1.2
    events.append([t + 4.0, "o", ""])  # hold the final frame

    header = {
        "version": 2,
        "width": 104,
        "height": 14,
        "env": {"TERM": "xterm-256color", "SHELL": "powershell"},
        "title": "pmox - one-shot agent-backed VM",
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(header, ensure_ascii=False) + "\n")
        for ev in events:
            fh.write(json.dumps([round(ev[0], 3), ev[1], ev[2]], ensure_ascii=False) + "\n")
    print(f"wrote {out_path}: {len(events)} events, {events[-1][0]:.1f}s")


if __name__ == "__main__":
    main(sys.argv[1:3], sys.argv[3])
