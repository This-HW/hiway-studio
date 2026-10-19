"""Subtitle burn-in helpers (design §10.2, D-14, P0-15).

Burn-in happens only at render time, via ffmpeg's `subtitles` filter
(libass). This module converts cue rows + a style dict into an ASS file;
services/render.py feeds it to ffmpeg. It never imports services
(ARCHITECTURE §15).
"""
from __future__ import annotations

from pathlib import Path


def _ass_timestamp(seconds: float) -> str:
    total_cs = int(round(seconds * 100))
    h, rem = divmod(total_cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_color(hex_color: str, opacity: float = 0.0) -> str:
    """#RRGGBB (+alpha 0..1) -> ASS &HAABBGGRR."""
    h = (hex_color or "#FFFFFF").lstrip("#")
    if len(h) != 6:
        h = "FFFFFF"
    aa = int(round((1.0 - max(0.0, min(1.0, opacity))) * 255))
    return f"&H{aa:02X}{h[4:6]}{h[2:4]}{h[0:2]}"


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def _style_line(name: str, style: dict, height: int) -> str:
    font = style.get("font_family") or "Noto Sans KR"
    size = int(style.get("font_size_px") or 48)
    # Scale the configured size (authored for 1080p) to the render height.
    size = max(8, int(round(size * height / 1080)))
    color = _ass_color(str(style.get("color") or "#FFFFFF"))
    bg = style.get("background_color")
    back = _ass_color(str(bg), 1.0 - float(style.get("background_opacity", 0.6))) if bg else "&H00000000"
    position = style.get("position") or "bottom"
    alignment = {"top": 8, "middle": 5, "bottom": 2}.get(position, 2)
    margin_v = int(round(float(style.get("safe_area_margin_pct", 5.0)) / 100 * height))
    return (
        f"Style: {name},{font},{size},{color},&H000019FF,&H00000000,{back},"
        f"0,0,0,0,100,100,0,0,1,2,0,{alignment},40,40,{margin_v},1"
    )


def _opposite_position(position: str) -> str:
    return {"top": "bottom", "bottom": "top", "middle": "top"}.get(position, "top")


def write_ass(
    cues: list,
    style: dict,
    dst: str | Path,
    *,
    width: int,
    height: int,
) -> Path:
    """Write an ASS subtitle file for libass burn-in. Returns the path.

    `cues`: objects with start_sec/end_sec/text (style_overrides honoured
    for color only). `style`: the effective style dict from
    services/subtitles.get_style.
    """
    return write_ass_sections([(cues, style)], dst, width=width, height=height)


def write_ass_sections(
    sections: list[tuple[list, dict]],
    dst: str | Path,
    *,
    width: int,
    height: int,
) -> Path:
    """Multi-track ASS writer (dual subtitles). Each section is
    (cues, effective_style) and gets its own ASS style (S0, S1, ...).

    When two sections would render at the same vertical position, the later
    one is moved to the opposite position (top<->bottom, middle->top) so
    dual subtitles never print on top of each other.
    """
    dst = Path(dst)
    seen_positions: list[str] = []
    style_lines: list[str] = []
    dialogue: list[str] = []
    for i, (cues, style) in enumerate(sections):
        name = f"S{i}"
        position = style.get("position") or "bottom"
        if position in seen_positions:
            position = _opposite_position(position)
            style = {**style, "position": position}
        seen_positions.append(position)
        style_lines.append(_style_line(name, style, height))
        for cue in cues:
            overrides = getattr(cue, "style_overrides", None) or {}
            fg = _ass_color(str(overrides.get("color") or style.get("color") or "#FFFFFF"))
            text = _escape(str(getattr(cue, "text", ""))).replace("\n", "\\N")
            dialogue.append(
                f"Dialogue: 0,{_ass_timestamp(cue.start_sec)},{_ass_timestamp(cue.end_sec)},"
                f"{name},,0,0,0,,{{\\c{fg}}}{text}"
            )

    ass = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
""" + "\n".join(style_lines) + """

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""" + "\n".join(dialogue) + "\n"
    dst.write_text(ass, encoding="utf-8")
    return dst
