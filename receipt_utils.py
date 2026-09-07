"""Receipt printing on an ESC/POS thermal printer.

Written against an Epson TM-T20III (the label calls it M267D), 04b8:0e28,
but nothing here is model specific - any ESC/POS printer that exposes a USB
printer-class interface should work by changing the ids in config.toml.

Talks to the device directly over pyusb rather than pulling in python-escpos:
the command set we need is small, and doing it here means the USB handle is
disposed deterministically. brother_ql's send() leaking its handle is what
wedged the label printers once already, so this module always hands the
interface back in a finally.
"""

import logging
import threading
from datetime import datetime

import usb.core
import usb.util

from config_manager import RECEIPT_CONFIG

logger = logging.getLogger("sticker_factory.receipt_utils")

# --- ESC/POS ---------------------------------------------------------------
INIT = b"\x1b@"                 # ESC @   reset to power-on defaults
ALIGN_LEFT = b"\x1ba\x00"       # ESC a 0
ALIGN_CENTER = b"\x1ba\x01"     # ESC a 1
BOLD_ON = b"\x1bE\x01"          # ESC E 1
BOLD_OFF = b"\x1bE\x00"
SIZE_NORMAL = b"\x1d!\x00"      # GS ! 0   1x1
SIZE_DOUBLE = b"\x1d!\x11"      # GS ! 17  2x height and width
CUT_PARTIAL = b"\x1dV\x42\x00"  # GS V 66 0  feed to the blade, then partial cut

# GS v 0 raster images go out in horizontal bands. The whole image in one
# command can overrun the print buffer on a long sticker, and a stalled bulk
# write is far more annoying than a few extra command headers.
RASTER_BAND_ROWS = 128

# Two browser sessions can finish a print at the same moment; the receipt
# printer has one bulk endpoint and no interest in interleaved commands.
_receipt_lock = threading.Lock()


def _cfg(key, default):
    return RECEIPT_CONFIG.get(key, default)


def _usb_id(key, default):
    """Read a vendor/product id. TOML has no hex literals, so "0x04b8" is a
    string; base 0 also accepts a plain decimal if someone prefers that."""
    raw = _cfg(key, default)
    if isinstance(raw, int):
        return raw
    try:
        return int(str(raw), 0)
    except ValueError:
        logger.error(f"Bad receipt.{key} in config.toml: {raw!r}, using {default}")
        return int(default, 0)


def is_enabled():
    return bool(_cfg("enabled", False))


def find_receipt_printer():
    """The configured ESC/POS device, or None if it isn't plugged in."""
    return usb.core.find(
        idVendor=_usb_id("vendor_id", "0x04b8"),
        idProduct=_usb_id("product_id", "0x0e28"),
    )


def describe_printer():
    """(connected, label) for the UI."""
    if not is_enabled():
        return False, "disabled"
    try:
        dev = find_receipt_printer()
    except Exception as e:
        logger.warning(f"Could not look for the receipt printer: {e}")
        return False, "lookup failed"
    if dev is None:
        return False, "not connected"
    try:
        return True, usb.util.get_string(dev, dev.iProduct) or "connected"
    except Exception:
        return True, "connected"


def _raster(image, width_dots, max_height_dots):
    """Encode a PIL image as GS v 0 raster bands.

    Vertical labels can be a metre long; scaled to the receipt width that
    would eat an absurd amount of paper, so the image is fitted inside
    max_height_dots as well and shrunk to whichever limit binds first.
    """
    from PIL import Image

    img = image.convert("L")
    scale = min(width_dots / img.width, max_height_dots / img.height, 1.0)
    if scale < 1.0:
        img = img.resize((max(int(img.width * scale), 1),
                          max(int(img.height * scale), 1)), Image.LANCZOS)

    # convert("1") dithers, which suits photos and leaves line art alone.
    img = img.convert("1")
    width_bytes = (img.width + 7) // 8
    # Pad each row out to a byte boundary; ESC/POS wants whole bytes.
    padded = Image.new("1", (width_bytes * 8, img.height), 1)
    padded.paste(img, (0, 0))

    # In ESC/POS a set bit is black; in PIL mode "1" a set pixel is white.
    data = bytes(b ^ 0xFF for b in padded.tobytes())

    out = bytearray()
    for top in range(0, padded.height, RASTER_BAND_ROWS):
        rows = min(RASTER_BAND_ROWS, padded.height - top)
        out += b"\x1dv0\x00"
        out += bytes([width_bytes & 0xFF, width_bytes >> 8, rows & 0xFF, rows >> 8])
        out += data[top * width_bytes:(top + rows) * width_bytes]
    return bytes(out)


def _text(line):
    """Encode a line for the printer's default code page."""
    return line.encode("cp437", errors="replace") + b"\n"


def build_receipt(printer_name, media, sticker_number=None, image=None, when=None):
    """Assemble the ESC/POS byte stream for one sticker."""
    width = int(_cfg("width_dots", 576))
    when = when or datetime.now()

    out = bytearray(INIT)

    if image is not None and _cfg("show_image", True):
        try:
            out += ALIGN_CENTER + _raster(image, width, int(_cfg("max_image_dots", 480)))
            out += b"\n"
        except Exception as e:
            # A receipt without the picture still beats no receipt.
            logger.warning(f"Could not rasterise the sticker for the receipt: {e}")

    out += ALIGN_CENTER
    header = str(_cfg("header", "STIKKA CENTRAAL"))
    if header:
        out += BOLD_ON + SIZE_DOUBLE + _text(header) + SIZE_NORMAL + BOLD_OFF
    out += _text("-" * 32)

    if sticker_number is not None:
        out += BOLD_ON + _text(f"Sticker #{sticker_number}") + BOLD_OFF
    out += _text(when.strftime("%Y-%m-%d %H:%M:%S"))
    out += _text(printer_name)
    out += _text(f"{media} paper" if media else "paper not configured")
    out += _text("-" * 32)

    footer = str(_cfg("footer", "Kleben und kleben lassen"))
    if footer:
        out += _text(footer)

    out += b"\n" * int(_cfg("feed_lines", 2))
    if _cfg("cut", True):
        out += CUT_PARTIAL
    return bytes(out)


def send_raw(payload):
    """Write bytes to the receipt printer, always releasing the interface."""
    dev = find_receipt_printer()
    if dev is None:
        raise RuntimeError("receipt printer not found on USB")

    timeout = int(_cfg("timeout_ms", 5000))
    with _receipt_lock:
        try:
            try:
                if dev.is_kernel_driver_active(0):
                    dev.detach_kernel_driver(0)
            except (NotImplementedError, usb.core.USBError):
                # macOS has no kernel driver to detach; Linux may already be free.
                pass

            dev.set_configuration()
            intf = usb.util.find_descriptor(dev.get_active_configuration(),
                                            bInterfaceClass=7)
            if intf is None:
                raise RuntimeError("no USB printer-class interface on the receipt printer")
            ep_out = usb.util.find_descriptor(
                intf,
                custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress)
                == usb.util.ENDPOINT_OUT,
            )
            if ep_out is None:
                raise RuntimeError("no bulk OUT endpoint on the receipt printer")

            # Chunked so a big raster can't sit in one oversized bulk transfer.
            for i in range(0, len(payload), 4096):
                ep_out.write(payload[i:i + 4096], timeout)
        finally:
            # Same lesson as the label printers: hand the interface back, or
            # the next open on this device fails or crashes.
            usb.util.dispose_resources(dev)


def print_receipt(printer_name, media="", sticker_number=None, image=None):
    """Print one receipt. Returns True if it went out.

    Never raises: a receipt is a nicety and must not turn a successful
    sticker into a failed one.
    """
    if not is_enabled():
        logger.debug("Receipt printing is disabled")
        return False
    try:
        send_raw(build_receipt(printer_name, media, sticker_number, image))
        logger.info(f"Printed receipt for {printer_name}")
        return True
    except Exception as e:
        logger.warning(f"Could not print receipt (non-critical): {e}")
        return False
