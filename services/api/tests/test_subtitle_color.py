"""ASS colour/alpha semantics for burn-in subtitles.

Regression: the primary text colour used to be emitted with alpha FF (fully
transparent), so burned-in subtitles rendered as an outline only, and the
background opacity was inverted.
"""

from app.media.subtitle_render import _ass_color, _ass_inline_color, _style_line


def test_text_colour_is_opaque_by_default():
    assert _ass_color("#FFFFFF") == "&H00FFFFFF"


def test_opacity_maps_to_inverted_ass_alpha():
    assert _ass_color("#000000", 0.0) == "&HFF000000"  # invisible
    assert _ass_color("#000000", 0.6) == "&H66000000"  # 60% opaque -> alpha 0x66
    assert _ass_color("#112233", 1.0) == "&H00332211"  # BGR order


def test_inline_override_has_no_alpha():
    assert _ass_inline_color("#FF8800") == "&H0088FF&"


def test_style_line_primary_opaque_and_background_opacity_respected():
    line = _style_line(
        "Default",
        {"color": "#FFFFFF", "background_color": "#000000", "background_opacity": 0.6},
        1080,
    )
    fields = line.split(":", 1)[1].split(",")
    assert fields[3] == "&H00FFFFFF"  # PrimaryColour opaque white
    assert "&H66000000" in fields  # BackColour 60% opaque black
