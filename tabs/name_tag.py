"""Name Tag tab - the classic "HELLO my name is" badge."""

import logging
import os

import streamlit as st
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger("sticker_factory.tabs.name_tag")

FRAKTUR = "fonts/UnifrakturCook-Bold.ttf"

# Wording for each flavour. Edit here to change what the badge says - the
# greeting is the only word on the tag that isn't the person's name.
FLAVOURS = {
    "English": {
        "greeting": "HELLO",
        "subtitle": "my name is",
        # None means "whatever font the user picked".
        "font": None,
    },
    "Deutsch": {
        "greeting": "HEIL",
        "subtitle": "Mein Name ist",
        # Pinned: this flavour is always set in the blackletter face.
        "font": FRAKTUR,
    },
}

# Classic badge proportions, 3.5" x 2.25", so the tag looks right whatever
# the tape width is.
ASPECT = 2.25 / 3.5
BAND_FRACTION = 0.46      # black banner across the top
BORDER = 6
MARGIN = 8


def _font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except (OSError, TypeError):
        return ImageFont.load_default()


def _measure(draw, text, font):
    """(width, height, x_offset, y_offset) of text drawn at the origin."""
    x0, y0, x1, y1 = draw.textbbox((0, 0), text, font=font)
    return x1 - x0, y1 - y0, x0, y0


def _fit_font(draw, path, text, max_w, max_h, hard_max=320):
    """Largest size at which text fits the box.

    Binary search rather than stepping up one size at a time: the name field
    is most of the badge, so sizes run large and each probe rasterises
    metrics, which is not free on a Pi.
    """
    if not text:
        return _font(path, 12)
    low, high, best = 6, hard_max, 6
    while low <= high:
        mid = (low + high) // 2
        w, h, _, _ = _measure(draw, text, _font(path, mid))
        if w <= max_w and h <= max_h:
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    return _font(path, best)


def _draw_centered(draw, text, font, box, fill):
    """Centre text in (x0, y0, x1, y1), correcting for the glyph bearings."""
    x0, y0, x1, y1 = box
    w, h, ox, oy = _measure(draw, text, font)
    draw.text((x0 + (x1 - x0 - w) / 2 - ox, y0 + (y1 - y0 - h) / 2 - oy),
              text, font=font, fill=fill)


def missing_glyphs(path, text):
    """Characters in text that this font has no glyph for.

    Done by rendering: a character that comes out identical to a codepoint the
    font certainly lacks is being drawn as .notdef. Keeps the check free of
    any font-parsing dependency.
    """
    try:
        font = _font(path, 48)

        def bitmap(ch):
            img = Image.new("L", (120, 120), 255)
            ImageDraw.Draw(img).text((10, 10), ch, font=font, fill=0)
            return img.tobytes()

        notdef = bitmap("")  # private use area, never mapped
        return sorted({c for c in text
                       if not c.isspace() and bitmap(c) == notdef})
    except Exception as e:
        logger.debug(f"Could not check glyph coverage for {path}: {e}")
        return []


def build_name_tag(name, greeting, subtitle, heading_font, name_font, width):
    """Render the badge: black banner on top, big name underneath."""
    height = max(int(width * ASPECT), 120)
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)

    # Outer rule, then the banner flush inside it.
    draw.rectangle([MARGIN, MARGIN, width - 1 - MARGIN, height - 1 - MARGIN],
                   outline="black", width=BORDER)
    inner = (MARGIN + BORDER, MARGIN + BORDER,
             width - 1 - MARGIN - BORDER, height - 1 - MARGIN - BORDER)
    band_bottom = inner[1] + int((inner[3] - inner[1]) * BAND_FRACTION)
    draw.rectangle([inner[0], inner[1], inner[2], band_bottom], fill="black")

    pad_x, pad_y = 18, 8
    band_h = band_bottom - inner[1]

    # Greeting takes the upper ~62% of the banner, subtitle the rest.
    greet_box = (inner[0] + pad_x, inner[1] + pad_y,
                 inner[2] - pad_x, inner[1] + int(band_h * 0.62))
    sub_box = (inner[0] + pad_x, inner[1] + int(band_h * 0.62),
               inner[2] - pad_x, band_bottom - pad_y)

    greet_font = _fit_font(draw, heading_font, greeting,
                           greet_box[2] - greet_box[0], greet_box[3] - greet_box[1])
    _draw_centered(draw, greeting, greet_font, greet_box, "white")

    sub_font = _fit_font(draw, heading_font, subtitle,
                         sub_box[2] - sub_box[0], sub_box[3] - sub_box[1])
    _draw_centered(draw, subtitle, sub_font, sub_box, "white")

    # The name gets everything below the banner.
    name_box = (inner[0] + pad_x, band_bottom + pad_y,
                inner[2] - pad_x, inner[3] - pad_y)
    if name:
        nf = _fit_font(draw, name_font, name,
                       name_box[2] - name_box[0], name_box[3] - name_box[1])
        _draw_centered(draw, name, nf, name_box, "black")

    return img


def render(printer_info, print_image, get_fonts):
    """Render the Name Tag tab."""
    st.subheader(":printer: a name tag")

    label_width = printer_info["label_width"]

    flavour = st.radio("Flavour", list(FLAVOURS), horizontal=True, key="name_tag_flavour")
    spec = FLAVOURS[flavour]

    name = st.text_input("Name", key="name_tag_name", placeholder="e.g. Gandalf")

    heading_font = spec["font"]
    if heading_font is None:
        # English is not pinned to a face, so let the operator choose.
        fonts = get_fonts()
        default = st.session_state.get("selected_font", fonts[0])
        index = fonts.index(default) if default in fonts else 0
        heading_font = st.selectbox(
            "Font", fonts, index=index,
            format_func=lambda p: os.path.splitext(os.path.basename(p))[0],
            key="name_tag_font",
        )
    else:
        st.caption(f"{flavour} is always set in "
                   f"{os.path.splitext(os.path.basename(heading_font))[0]}.")

    name_font = heading_font

    # UnifrakturCook covers the umlauts and eszett a German name tag needs, but
    # the English flavour lets any font be picked and some of the bundled ones
    # (Germanica, Tami) have no accented glyphs at all. A badge that can't spell
    # someone's name is not much of a badge, so say so and offer a face that can.
    gaps = missing_glyphs(heading_font, name) if name else []
    if gaps:
        st.warning(
            f"{os.path.splitext(os.path.basename(heading_font))[0]} has no glyph for "
            f"{', '.join(repr(c) for c in gaps)} - these would print as empty boxes."
        )
        if st.checkbox("Set the name in a font that has them", value=True,
                       key="name_tag_fallback"):
            name_font = next(
                (f for f in get_fonts() if not missing_glyphs(f, name)),
                heading_font,
            )
            if name_font != heading_font:
                st.caption(f"Name set in "
                           f"{os.path.splitext(os.path.basename(name_font))[0]}; "
                           f"the heading stays in "
                           f"{os.path.splitext(os.path.basename(heading_font))[0]}.")
            else:
                st.error("No available font covers those characters.")

    img = build_name_tag(name, spec["greeting"], spec["subtitle"],
                         heading_font, name_font, label_width)
    st.image(img, width="stretch")

    if not name:
        st.info("Enter a name to print the tag.")
        return

    if st.button("Print name tag", key="print_name_tag"):
        print_image(img, printer_info=printer_info)
        st.success("name tag sent to printer")
