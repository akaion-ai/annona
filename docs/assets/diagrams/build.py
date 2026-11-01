#!/usr/bin/env python3
"""Draw the README diagrams as SVG, one light and one dark file each.

    python docs/assets/diagrams/build.py

GitHub renders Mermaid in grey on dark, with no say over type or colour; these
are drawn by hand so they read the same on both themes and on the docs site.
The README picks the file with <picture> and prefers-color-scheme. Edit the
text here, not the SVG: the SVG is output.
"""

from __future__ import annotations

from html import escape
from pathlib import Path

OUT = Path(__file__).parent
SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, monospace"

THEMES = {
    "light": {
        "text": "#1f2328",
        "muted": "#59636e",
        "line": "#d1d9e0",
        "card": "#f6f8fa",
        "edge": "#c8d1da",
        "teal": "#1f6f68",
        "teal_fill": "#1f6f68",
        "teal_soft": "#e3f1ef",
        "on_teal": "#ffffff",
        "gold": "#8a6a12",
        "gold_soft": "#f8f0d8",
        "red": "#b42318",
        "red_soft": "#fdecea",
        "violet": "#6639ba",
        "violet_soft": "#f1ebfb",
        "arrow": "#59636e",
        "perimeter": "#1f6f68",
    },
    "dark": {
        "text": "#e6edf3",
        "muted": "#9198a1",
        "line": "#3d444d",
        "card": "#151b23",
        "edge": "#3d444d",
        "teal": "#4cc2b4",
        "teal_fill": "#1a5a54",
        "teal_soft": "#0f2e2b",
        "on_teal": "#ffffff",
        "gold": "#e3b341",
        "gold_soft": "#2b230f",
        "red": "#f47067",
        "red_soft": "#3a1b1b",
        "violet": "#b392f0",
        "violet_soft": "#251b3b",
        "arrow": "#9198a1",
        "perimeter": "#4cc2b4",
    },
}


class Svg:
    def __init__(self, w: int, h: int, t: dict[str, str]):
        self.w, self.h, self.t, self.parts = w, h, t, []

    def add(self, s: str) -> None:
        self.parts.append(s)

    def text(
        self, x, y, s, *, size=13, weight=400, fill="text", anchor="start", mono=False, spacing=0.0
    ):
        self.add(
            f'<text x="{x}" y="{y}" font-family="{MONO if mono else SANS}" font-size="{size}" '
            f'font-weight="{weight}" fill="{self.t.get(fill, fill)}" text-anchor="{anchor}"'
            f'{f" letter-spacing={chr(34)}{spacing}{chr(34)}" if spacing else ""}>{escape(s)}</text>'
        )

    def box(self, x, y, w, h, *, fill="card", stroke="edge", r=12, dash="", sw=1.2):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.add(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{self.t.get(fill, fill)}" '
            f'stroke="{self.t.get(stroke, stroke)}" stroke-width="{sw}"{d}/>'
        )

    def pill(self, cx, cy, s, *, fg="muted", bg="card", stroke=None, size=11.5, weight=600):
        w = len(s) * size * 0.58 + 22
        self.box(cx - w / 2, cy - 12, w, 24, fill=bg, stroke=stroke or bg, r=12, sw=1)
        self.text(cx, cy + 4, s, size=size, weight=weight, fill=fg, anchor="middle")

    def line(self, pts, *, color="arrow", dash="", start=False, end=True, sw=1.6):
        d = "M" + " L".join(f"{x},{y}" for x, y in pts)
        c = self.t.get(color, color)
        mid = f"a-{color}"
        self.add(
            f'<path d="{d}" fill="none" stroke="{c}" stroke-width="{sw}" stroke-linejoin="round"'
            f'{f" stroke-dasharray={chr(34)}{dash}{chr(34)}" if dash else ""}'
            f'{f" marker-end={chr(34)}url(#{mid}){chr(34)}" if end else ""}'
            f'{f" marker-start={chr(34)}url(#{mid}-s){chr(34)}" if start else ""}/>'
        )

    def render(self, title: str) -> str:
        markers = "".join(
            f'<marker id="a-{k}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
            f'markerHeight="7" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="{v}"/></marker>'
            f'<marker id="a-{k}-s" viewBox="0 0 10 10" refX="1" refY="5" markerWidth="7" '
            f'markerHeight="7" orient="auto"><path d="M10,0 L0,5 L10,10 z" fill="{v}"/></marker>'
            for k, v in self.t.items()
        )
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
            f'viewBox="0 0 {self.w} {self.h}" role="img" aria-label="{escape(title)}">'
            f"<title>{escape(title)}</title><defs>{markers}</defs>"
            + "".join(self.parts)
            + "</svg>\n"
        )


def lock(s: Svg, x, y, color) -> None:
    c = s.t[color]
    s.add(
        f'<g transform="translate({x},{y})" fill="none" stroke="{c}" stroke-width="1.6">'
        f'<rect x="0" y="6" width="12" height="9" rx="2" fill="{c}"/>'
        f'<path d="M2.5,6 V4 a3.5,3.5 0 0 1 7,0 V6"/></g>'
    )


def architecture(t) -> str:
    s = Svg(960, 600, t)
    # control plane
    s.box(250, 20, 420, 92)
    s.text(
        460, 46, "CONTROL PLANE", size=11, weight=700, fill="muted", anchor="middle", spacing=1.4
    )
    s.text(460, 72, "Agents Studio · Kai · or your own", size=16, weight=650, anchor="middle")
    s.text(460, 94, "plans · memory · orchestration", size=12.5, fill="muted", anchor="middle")
    # perimeter
    s.box(24, 176, 620, 400, fill="teal_soft", stroke="perimeter", r=18, dash="7 6", sw=1.6)
    lock(s, 46, 190, "perimeter")
    s.text(66, 204, "YOUR PERIMETER", size=11, weight=700, fill="perimeter", spacing=1.4)
    s.text(620, 204, "outbound 443 only · no listening port", size=11.5, fill="muted", anchor="end")
    # kernel
    s.box(164, 230, 340, 108, fill="teal_fill", stroke="teal_fill", r=14)
    s.text(334, 266, "ANNONA", size=19, weight=800, fill="on_teal", anchor="middle", spacing=2)
    s.text(
        334,
        292,
        "classify → place → execute → record",
        size=13.5,
        weight=500,
        fill="on_teal",
        anchor="middle",
    )
    s.text(
        334, 316, "every inference · every tool call", size=11.5, fill="#cfe9e5", anchor="middle"
    )
    # inside
    s.box(56, 448, 260, 96)
    s.text(186, 486, "your data", size=14.5, weight=650, anchor="middle")
    s.text(186, 510, "files · databases · internal apps", size=12, fill="muted", anchor="middle")
    s.box(352, 448, 260, 96)
    s.text(482, 486, "your GPU", size=14.5, weight=650, anchor="middle")
    s.text(482, 510, "Ollama · vLLM · open weights", size=12, fill="muted", anchor="middle")
    s.line([(250, 338), (250, 446)], start=True)
    s.line([(440, 338), (440, 446)], start=True)
    s.pill(250, 392, "read, by policy", size=10.5, weight=500)
    s.pill(
        440, 392, "restricted runs here", fg="teal", bg="teal_soft", stroke="perimeter", size=10.5
    )
    # control plane <-> kernel
    s.line([(300, 112), (300, 228)])
    s.line([(400, 228), (400, 112)], dash="5 4")
    s.text(290, 150, "signed plan", size=12, weight=600, anchor="end")
    s.text(410, 144, "cleared results · ledger digests", size=12, weight=600)
    s.text(410, 162, "never raw material", size=12, fill="muted")
    # outside
    s.box(696, 236, 240, 92)
    s.text(816, 272, "private cluster", size=14.5, weight=650, anchor="middle")
    s.text(816, 296, "your EU tenant", size=12, fill="muted", anchor="middle")
    s.box(696, 404, 240, 92)
    s.text(816, 440, "frontier APIs", size=14.5, weight=650, anchor="middle")
    s.text(816, 464, "the best models, abroad", size=12, fill="muted", anchor="middle")
    s.line([(504, 282), (694, 282)], start=True)
    s.pill(600, 264, "class ≤ internal", fg="gold", bg="gold_soft", stroke="gold")
    s.line([(504, 318), (666, 318), (666, 450), (694, 450)], start=True)
    s.pill(666, 384, "public, or a brief", fg="gold", bg="gold_soft", stroke="gold")
    s.box(696, 520, 240, 56, fill="red_soft", stroke="red", r=12)
    s.text(816, 543, "restricted never crosses", size=13, weight=700, fill="red", anchor="middle")
    s.text(816, 562, "nothing permitted → held", size=11.5, fill="red", anchor="middle")
    return s.render("How Annona sits between the control plane, your data and every model")


def verdicts(t) -> str:
    s = Svg(960, 400, t)
    cols = [
        (
            24,
            "A client's file",
            "class restricted · touched /mnt/pratiche",
            "red",
            [
                ("summarise locally → a 380-token brief", "local-gpu", "teal"),
                ("reclassify the brief", "still internal", "muted"),
                ("reason over brief + local index", "local-gpu", "teal"),
                ("call the frontier model", "HELD · never called", "red"),
            ],
            "3 steps · 3 local · 0 crossings",
        ),
        (
            492,
            "A public tender",
            "class public",
            "gold",
            [
                ("summarise the tender", "frontier", "gold"),
                ("reclassify the summary", "public", "muted"),
                ("reason over summary + index", "frontier", "gold"),
                ("call the frontier model", "allowed", "gold"),
            ],
            "same plan · 2 steps abroad · a better answer",
        ),
    ]
    for x, title, sub, tone, steps, foot in cols:
        s.box(x, 20, 444, 360, r=16)
        s.text(x + 24, 54, title, size=17, weight=700)
        s.pill(
            x + 420 - (len(sub) * 11 * 0.58 + 22) / 2,
            49,
            sub,
            fg=tone,
            bg=f"{tone}_soft",
            stroke=tone,
            size=11,
        )
        for i, (what, where, color) in enumerate(steps):
            y = 92 + i * 62
            s.box(x + 20, y, 404, 48, fill="card", stroke="line", r=10)
            s.text(x + 40, y + 29, f"{i + 1}" if i < 3 else "·", size=13, weight=800, fill="muted")
            s.text(x + 62, y + 29, what, size=13.5)
            bg = {"teal": "teal_soft", "gold": "gold_soft", "red": "red_soft"}.get(color, "card")
            pw = len(where) * 11 * 0.58 + 22
            s.pill(x + 412 - pw / 2, y + 24, where, fg=color, bg=bg, stroke=color, size=11)
        s.text(x + 24, 358, foot, size=13.5, weight=700, fill=tone if tone == "gold" else "teal")
    return s.render(
        "The same plan, two verdicts: a client's file stays local, a public tender goes out"
    )


def link(t) -> str:
    s = Svg(960, 420, t)
    # studio
    s.box(24, 40, 300, 176)
    s.text(48, 72, "AGENTS STUDIO", size=11, weight=700, fill="muted", spacing=1.4)
    s.text(48, 100, "someone in your org", size=16, weight=650)
    s.text(48, 124, "picks a machine, writes the task,", size=12.5, fill="muted")
    s.text(48, 143, "closes the browser", size=12.5, fill="muted")
    s.pill(120, 184, "Run on machine", fg="violet", bg="violet_soft", stroke="violet")
    # machine
    s.box(560, 24, 376, 210, fill="teal_soft", stroke="perimeter", r=18, dash="7 6", sw=1.6)
    lock(s, 582, 40, "perimeter")
    s.text(
        602,
        54,
        "YOUR MACHINE · annona link serve",
        size=11,
        weight=700,
        fill="perimeter",
        spacing=1.2,
    )
    s.box(584, 72, 328, 62, fill="teal_fill", stroke="teal_fill", r=12)
    s.text(
        748,
        99,
        "its policy · its models · its ledger",
        size=13.5,
        weight=650,
        fill="on_teal",
        anchor="middle",
    )
    s.text(
        748,
        120,
        "DGX Spark · Ryzen AI Max · a Mac · a rack",
        size=11.5,
        fill="#cfe9e5",
        anchor="middle",
    )
    s.box(584, 150, 156, 62)
    s.text(662, 177, "local model", size=13, weight=650, anchor="middle")
    s.text(662, 197, "on its GPU", size=11.5, fill="muted", anchor="middle")
    s.box(756, 150, 156, 62)
    s.text(834, 177, "inbox 0600", size=13, weight=650, anchor="middle")
    s.text(834, 197, "what stays here", size=11.5, fill="muted", anchor="middle")
    # wire
    s.line([(558, 92), (326, 92)])
    s.text(442, 80, "claims work · every 3 s", size=12, weight=600, anchor="middle")
    s.line([(558, 168), (326, 168)])
    s.text(442, 156, "sends back what policy allows", size=12, weight=600, anchor="middle")
    s.pill(
        442, 124, "outbound HTTPS only", fg="teal", bg="teal_soft", stroke="perimeter", size=10.5
    )
    # outcomes
    s.text(24, 276, "WHAT STUDIO SHOWS", size=11, weight=700, fill="muted", spacing=1.4)
    outcomes = [
        ("Completed", "the answer goes back", "teal"),
        ("Kept on machine", "answer only in the inbox", "violet"),
        ("Failed", "policy held it · the rule, no data", "red"),
        ("Cancelled", "stopped from Studio", "muted"),
    ]
    for i, (name, what, tone) in enumerate(outcomes):
        x = 24 + i * 233
        s.box(x, 292, 219, 84, r=12)
        bg = {"teal": "teal_soft", "violet": "violet_soft", "red": "red_soft"}.get(tone, "card")
        pw = len(name) * 11.5 * 0.58 + 22
        s.pill(
            x + 16 + pw / 2, 318, name, fg=tone, bg=bg, stroke=tone if tone != "muted" else "line"
        )
        s.text(x + 16, 356, what, size=12, fill="muted")
    s.text(
        24,
        404,
        "Optional. Nothing talks to Studio until you enroll; revoke from either side at any time.",
        size=12,
        fill="muted",
    )
    return s.render(
        "The optional link: Agents Studio sends work, the machine runs it and decides what returns"
    )


TERMINAL = [
    ("$ ", "annona why step_7f3a"),
    ("step_7f3a  inference  ", "HELD"),
    ("  class        ", "restricted  (working set touched /mnt/pratiche/2026/BG-114.pdf)"),
    ("  rule         ", "rules[0]  restricted → [local-gpu], on_unavailable: hold"),
    ("  candidates   ", "local-gpu (unhealthy: connection refused since 14:02:11)"),
    ("  not chosen   ", "frontier — max_class public < restricted"),
    ("               ", "eu-cluster — max_class internal < restricted"),
    ("  outcome      ", "held at 14:03:07, queued for operator review"),
    ("  ledger       ", "#418  sha256:9c1f…a7  (chain verified)"),
]


def terminal() -> str:
    """The README's opening refusal, typed out. One file: a terminal is dark on both themes."""
    w, lh, top = 860, 24, 64
    h = top + lh * len(TERMINAL) + 28
    bg, fg, dim = "#0d1117", "#e6edf3", "#7d8590"
    colors = {"HELD": "#f47067", "restricted": "#f0b72f"}
    cmd = TERMINAL[0][1]
    type_s = 0.06 * len(cmd)
    rows = []
    for i, (label, value) in enumerate(TERMINAL):
        y = top + i * lh
        if i == 0:
            rows.append(
                f'<text x="24" y="{y}" fill="#4cc2b4">$</text>'
                f'<text x="42" y="{y}" fill="{fg}">{escape(cmd)}</text>'
                # a cover the colour of the terminal slides off the command, one character a step
                f'<rect class="type" x="40" y="{y - 17}" width="{len(cmd) * 8 + 12}" height="22" '
                f'fill="{bg}"/>'
                f'<rect class="caret" x="42" y="{y - 15}" width="8" height="18" fill="#4cc2b4"/>'
            )
            continue
        first = value.split(" ", 1)[0]
        color = colors.get(first, fg)
        rest = value[len(first) :]
        delay = type_s + 0.5 + (i - 1) * 0.22
        rows.append(
            f'<g class="row" style="animation-delay:{delay:.2f}s">'
            f'<text x="24" y="{y}" fill="{dim}" xml:space="preserve">{escape(label)}'
            f'<tspan fill="{color}" font-weight="{700 if color != fg else 400}">{escape(first)}</tspan>'
            f'<tspan fill="{fg}">{escape(rest)}</tspan></text></g>'
        )
    steps = len(cmd)
    style = (
        f".row{{opacity:0;animation:show .25s ease-out forwards}}"
        f"@keyframes show{{from{{opacity:0;transform:translateY(4px)}}to{{opacity:1;transform:none}}}}"
        f".type{{animation:type {type_s:.2f}s steps({steps}) .4s forwards}}"
        f"@keyframes type{{to{{transform:translateX({steps * 8 + 2}px)}}}}"
        f".caret{{animation:caret {type_s:.2f}s steps({steps}) .4s forwards, blink 1s step-end infinite}}"
        f"@keyframes caret{{to{{transform:translateX({steps * 8 + 2}px)}}}}"
        f"@keyframes blink{{50%{{opacity:0}}}}"
        f"@media (prefers-reduced-motion:reduce){{.row,.type,.caret{{animation:none;opacity:1}}.type,.caret{{display:none}}}}"
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
        f'role="img" aria-label="annona why: a step held, with the rule, the candidates and the ledger entry">'
        f"<title>annona why step_7f3a</title><style>{style}</style>"
        f'<rect x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" rx="12" fill="{bg}" stroke="#30363d"/>'
        f'<circle cx="22" cy="20" r="6" fill="#ff5f57"/><circle cx="42" cy="20" r="6" fill="#febc2e"/>'
        f'<circle cx="62" cy="20" r="6" fill="#28c840"/>'
        f'<text x="{w / 2}" y="25" fill="{dim}" font-family="{SANS}" font-size="12.5" '
        f'text-anchor="middle">annona — ~/pratiche</text>'
        f'<line x1="0" y1="38" x2="{w}" y2="38" stroke="#30363d"/>'
        f'<g font-family="{MONO}" font-size="13.4">' + "".join(rows) + "</g></svg>\n"
    )


def main() -> None:
    for name, draw in (("architecture", architecture), ("verdicts", verdicts), ("link", link)):
        for theme, tokens in THEMES.items():
            (OUT / f"{name}-{theme}.svg").write_text(draw(tokens), encoding="utf-8")
            print(f"wrote {name}-{theme}.svg")
    (OUT / "why-held.svg").write_text(terminal(), encoding="utf-8")
    print("wrote why-held.svg")


if __name__ == "__main__":
    main()
