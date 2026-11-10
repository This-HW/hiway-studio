"""Subtitle burn-in helpers (design §10.2, D-14, P0-15).

Burn-in happens only at render time, via ffmpeg's `subtitles` filter
(libass). This module converts cue rows + a style dict into an ASS file;
services/render.py feeds it to ffmpeg. `render_glyph_mask` draws one
track's cue the same way onto a blank canvas (opaque white) so the render
postflight knows which pixels that track must change. It never imports
services (ARCHITECTURE §15).
"""
from __future__ import annotations

from pathlib import Path


def _ass_timestamp(seconds: float) -> str:
    total_cs = int(round(seconds * 100))
    h, rem = divmod(total_cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _bgr(hex_color: str) -> str:
    h = (hex_color or "#FFFFFF").lstrip("#")
    if len(h) != 6:
        h = "FFFFFF"
    return f"{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def _ass_color(hex_color: str, opacity: float = 1.0) -> str:
    """#RRGGBB + opacity (1.0 = fully opaque, 0.0 = invisible) -> ASS &HAABBGGRR.

    ASS alpha is inverted (00 = opaque, FF = transparent), so alpha = 1 - opacity.
    """
    aa = int(round((1.0 - max(0.0, min(1.0, opacity))) * 255))
    return f"&H{aa:02X}{_bgr(hex_color)}"


def _ass_inline_color(hex_color: str) -> str:
    """Inline override tag value for \\c: &HBBGGRR& (no alpha; alpha is \\1a)."""
    return f"&H{_bgr(hex_color)}&"


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


_OUTLINE_BLACK = "&H00000000"
_MASK_WHITE = "&H00FFFFFF"


def _style_line(
    name: str, style: dict, height: int, *, outline: str = _OUTLINE_BLACK
) -> str:
    font = style.get("font_family") or "Noto Sans KR"
    size = int(style.get("font_size_px") or 48)
    # Scale the configured size (authored for 1080p) to the render height.
    size = max(8, int(round(size * height / 1080)))
    color = _ass_color(str(style.get("color") or "#FFFFFF"))
    bg = style.get("background_color")
    back = _ass_color(str(bg), float(style.get("background_opacity", 0.6))) if bg else "&H00000000"
    position = style.get("position") or "bottom"
    alignment = {"top": 8, "middle": 5, "bottom": 2}.get(position, 2)
    margin_v = int(round(float(style.get("safe_area_margin_pct", 5.0)) / 100 * height))
    return (
        f"Style: {name},{font},{size},{color},&H000019FF,{outline},{back},"
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
            fg = _ass_inline_color(str(overrides.get("color") or style.get("color") or "#FFFFFF"))
            text = _escape(str(getattr(cue, "text", ""))).replace("\n", "\\N")
            dialogue.append(
                f"Dialogue: 0,{_ass_timestamp(cue.start_sec)},{_ass_timestamp(cue.end_sec)},"
                f"{name},,0,0,0,,{{\\c{fg}}}{text}"
            )

    dst.write_text(_ass_document(style_lines, dialogue, width, height), encoding="utf-8")
    return dst


def _ass_document(
    style_lines: list[str], dialogue: list[str], width: int, height: int
) -> str:
    return f"""[Script Info]
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


def write_ass_mask(
    texts: list[str],
    style: dict,
    dst: str | Path,
    *,
    width: int,
    height: int,
) -> Path:
    """Glyph-mask ASS for the render postflight (B4b-fix, ADR-0013).

    The given cue texts of ONE track, laid out exactly like the burn-in
    (`_style_line`: font, size, position, margins, outline width) but drawn
    opaque white — fill AND outline, ignoring per-cue colours — from t=0.
    `style` must already carry the track's resolved position (dual mode
    moves a same-position track; services pass the resolved style)."""
    dst = Path(dst)
    style_lines = [_style_line("M", {**style, "color": "#FFFFFF"}, height,
                               outline=_MASK_WHITE)]
    dialogue = [
        f"Dialogue: 0,{_ass_timestamp(0.0)},{_ass_timestamp(3600.0)},M,,0,0,0,,"
        + _escape(str(text)).replace("\n", "\\N")
        for text in texts
    ]
    dst.write_text(_ass_document(style_lines, dialogue, width, height), encoding="utf-8")
    return dst


def _filter_path(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def render_glyph_mask(
    texts: list[str],
    style: dict,
    dst: str | Path,
    *,
    width: int,
    height: int,
) -> Path:
    """Render `write_ass_mask` onto a black width x height canvas and save
    the single frame (PNG) at `dst`: white = where these cues put glyph or
    outline pixels. Raises FFmpegError when ffmpeg fails."""
    from app.media import ffmpeg_ops

    dst = Path(dst)
    ass = write_ass_mask(texts, style, dst.with_suffix(".ass"), width=width, height=height)
    ffmpeg_ops._run(
        ["-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r=1:d=1",
         "-vf", f"subtitles='{_filter_path(ass)}'",
         "-frames:v", "1", str(dst)],
        what=f"render subtitle glyph mask {dst.name}",
    )
    return dst
