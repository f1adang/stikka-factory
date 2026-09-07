"""Printer handling and detection utilities for the Sticker Factory."""

import logging
import re
import subprocess
import tempfile
import threading
import time
import os
from pathlib import Path
from brother_ql.models import ModelsManager
from brother_ql.backends import backend_factory
from brother_ql import labels
from brother_ql.raster import BrotherQLRaster
from brother_ql.conversion import convert
from brother_ql.backends.helpers import get_printer
from brother_ql.reader import interpret_response
import usb.core
from dataclasses import dataclass

import streamlit as st
from job_queue import print_queue
from config_manager import PRIVACY_MODE, DEBUG_MODE, FALLBACK_LABEL_TYPE, FALLBACK_MODELS, MEDIA_TYPES

logger = logging.getLogger("sticker_factory.printer_utils")

# Pre-2012 QL models have no status back-channel: they never answer the
# "ESC i S" status request. Asking anyway makes brother_ql raise
# NameError('Insufficient amount of data received', '') and the libusb
# teardown that follows segfaults, leaving the device wedged for the next job.
# For these models we use the configured fallback label type instead of asking.
# (These are exactly the models brother_ql marks mode_setting=False.)
MODELS_WITHOUT_STATUS = frozenset({"QL-500", "QL-550", "QL-560", "QL-570", "QL-700"})

# Serialises every USB conversation with a printer. Printing happens on the
# job-queue worker thread while Streamlit reruns call find_and_parse_printer()
# on the main thread; without this both can hold a libusb handle on the same
# device at once, which is what wedges the printer. Reentrant so that
# find_and_parse_printer() can hold it across a loop whose body also takes it.
_usb_lock = threading.RLock()

# How long printer discovery waits for the bus before giving up. A print holds
# the lock for up to ~10s; rather than freeze the Streamlit thread that long we
# skip the refresh, and printit.py keeps showing the cached printer list.
DISCOVERY_LOCK_TIMEOUT = 2.0

# How long to let a status-less printer digest a job before we touch USB again.
# Those models can't tell us when they are done, so this stands in for the
# read-back we would otherwise wait on.
STATUS_LESS_SETTLE_SECONDS = 1.5


# Trailing " - <4 chars>" as appended by find_and_parse_printer(). Anchored and
# fixed-width so a name without one (the virtual printer) is left alone.
_SERIAL_SUFFIX = re.compile(r" - [A-Za-z0-9]{4}$")


def display_name(name):
    """Printer name as users should see it, without the serial suffix.

    The stored name keeps the suffix: it is the identity that [media] keys,
    stats records and the selection radio all match on, and two printers of
    the same model would otherwise collide. Only the presentation drops it.
    """
    return _SERIAL_SUFFIX.sub("", str(name))


def get_media_type(name, serial_number=""):
    """Configured paper for a printer, or "" if it isn't listed.

    A [media] key may be the displayed name ("QL-500 - 8169"), the full
    serial, or its last four characters, whichever the operator finds easier.
    Matching is exact against those three - note that a name key embeds the
    model, so it stops matching if model detection ever changes; key by serial
    to be immune to that.
    """
    for key in (name, serial_number, str(serial_number)[-4:]):
        if key and key in MEDIA_TYPES:
            return MEDIA_TYPES[key]
    return ""


def model_has_status_channel(model):
    """True if this model can answer a status request."""
    return str(model) not in MODELS_WITHOUT_STATUS and str(model) not in FALLBACK_MODELS

def safe_filename(text):
    epoch_time = int(time.time())
    return f"{epoch_time}_{text}.png"

@dataclass
class PrinterInfo:
    identifier: str
    backend: str
    protocol: str
    vendor_id: str
    product_id: str
    serial_number: str
    name: str = "Brother QL Printer"
    model: str = "QL-570"
    status: str = "unknown"
    label_type: str = "unknown"
    label_size : str = "unknown"
    label_width: int = 0
    label_height: int = 0
    # What paper is loaded. These models can't report it, so it comes from
    # config.toml's [media] section; empty means nobody has said.
    media: str = ""
    
    def __getitem__(self, item):
        return getattr(self, item)
    
    def __setitem__(self, key, value):
        setattr(self, key, value)


def create_virtual_printer():
    """Create a virtual printer for debug mode."""
    virtual_printer = PrinterInfo(
        identifier="virtual/debug/0000",
        backend="virtual",
        model="QL-570",
        protocol="virtual",
        vendor_id="0000",
        product_id="0000",
        serial_number="DEBUG-0000",
        name="Virtual Debug Printer",
        status="Waiting to receive",
        label_type=FALLBACK_LABEL_TYPE,
        label_size=f"{FALLBACK_LABEL_TYPE}mm",
        label_width=get_label_width(FALLBACK_LABEL_TYPE),
        label_height=None,
        media=get_media_type("Virtual Debug Printer", "DEBUG-0000") or "virtual",
    )
    logger.info("Created virtual debug printer")
    return virtual_printer


def find_and_parse_printer():
    logger.info("Searching for Brother QL printers...")
    model_manager = ModelsManager()
    
    found_printers = []
    
    # Add virtual printer if debug mode is enabled
    if DEBUG_MODE:
        virtual_printer = create_virtual_printer()
        found_printers.append(virtual_printer)
        logger.info("DEBUG MODE: Added virtual printer to available printers")

    if not _usb_lock.acquire(timeout=DISCOVERY_LOCK_TIMEOUT):
        logger.warning("Printer busy, skipping discovery this round")
        return found_printers

    try:
        for backend_name in ["pyusb", "linux_kernel"]:
            try:
                logger.debug(f"Trying backend: {backend_name}")
                backend = backend_factory(backend_name)
                available_devices = backend["list_available_devices"]()
                logger.debug(f"Found {len(available_devices)} devices with {backend_name} backend")
            
                for printer in available_devices:
                    logger.debug(f"Found device: {printer}")
                    identifier = printer["identifier"]
                    parts = identifier.split("/")

                    if len(parts) < 4:
                        logger.warning(f"Skipping device with invalid identifier format: {identifier}")
                        continue

                    protocol = parts[0]
                    device_info = parts[2]
                    serial_number = parts[3]
                
                    try:
                        vendor_id, product_id = device_info.split(":")
                    except ValueError:
                        logger.warning(f"Invalid device info format: {device_info}")
                        continue
                
                    try:
                        product_id_int = int(product_id, 16)
                    except ValueError:
                        logger.warning(f"Invalid product ID format: {product_id}")
                        continue

                    model = next(
                        (m.identifier for m in model_manager.iter_elements()
                         if m.product_id == product_id_int),
                        None,
                    )
                    if model is None:
                        # Without this the name from the previous loop iteration
                        # would leak in and mislabel the printer.
                        logger.warning(f"No known model for product ID {product_id}, skipping {identifier}")
                        continue
                    logger.debug(f"Matched printer model: {model}")

                    printer_info = PrinterInfo(
                        identifier=identifier,
                        backend=backend_name,
                        model=model,
                        protocol=protocol,
                        vendor_id=vendor_id,
                        product_id=product_id,
                        serial_number=serial_number,
                    )

                    found_printers.append(printer_info)   
                    printer_info['name'] = f"{printer_info['model']} - {printer_info['serial_number'][-4:]}"
                    printer_info['media'] = get_media_type(printer_info['name'], printer_info['serial_number'])
                    get_printer_status(printer_info)
                    logger.debug(f"Added printer: {printer_info}")

            except Exception as e:
                logger.error(f"Error with backend {backend_name}: {str(e)}")
                continue
    finally:
        _usb_lock.release()

    return found_printers


def get_printer_status(printer):
    printer['status'] = "unknown"
    printer['label_type'] = "unknown"
    printer['label_size'] = "unknown"
    printer['label_width'] = 0
    printer['label_height'] = 0
    logger.debug(
        f"Checking if '{printer['model']}' can report status "
        f"(FALLBACK_MODELS: {FALLBACK_MODELS}, no-status models: {sorted(MODELS_WITHOUT_STATUS)})"
    )
    if not model_has_status_channel(printer['model']):
        printer['label_type'] = FALLBACK_LABEL_TYPE
        printer['label_size'] = f"{FALLBACK_LABEL_TYPE}mm"
        printer['label_width'] = get_label_width(FALLBACK_LABEL_TYPE)
        printer['label_height'] = 0
        printer['status'] = "Waiting to receive"
        logger.debug(f"Using fallback label type {printer['label_type']} for model {printer['model']}")
    else:
        try:
            cmd = [
                "brother_ql", "-b", "pyusb",
                "--model", str(printer['model']),
                "-p", str(printer['identifier']),
                "status",
            ]
            logger.debug(f"Running status command: {' '.join(cmd)}")
            # Held across the subprocess so we never open a second libusb
            # handle on a device the print worker is currently using.
            with _usb_lock:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            
            # Log the raw output for debugging
            if result.stdout:
                logger.debug(f"Status command stdout:\n{result.stdout}")
            if result.stderr:
                logger.warning(f"Status command stderr:\n{result.stderr}")
            if result.returncode != 0:
                logger.warning(f"Status command returned non-zero exit code: {result.returncode}")
                
            for line in result.stdout.splitlines():
                if "Phase:" in line:
                    printer['status'] = line.split("Phase:")[1].strip()
                    logger.debug(f"Detected status: {printer['status']}")
                if "Media size:" in line:
                    printer['label_size'] = line.split("Media size:")[1].strip()
                    size_str = line.split("Media size:")[1].strip().split('x')[0].strip()
                    try:
                        media_width_mm = int(size_str)
                        label_sizes = {
                            12: "12", 29: "29", 38: "38", 50: "50", 54: "54",
                            62: "62", 102: "102", 103: "103", 104: "104"
                        }
                        if media_width_mm in label_sizes:
                            label_type = label_sizes[media_width_mm]
                            printer['label_type'] = label_type
                            printer['label_width'] = get_label_width(label_type)
                            printer['label_height'] = None
                            logger.debug(f"Detected label type: {label_type} from width: {media_width_mm}mm")
                    except Exception as e:
                        logger.warning(f"Exception parsing media width: {str(e)}")
            logger.info(f"Printer {printer['name']}: label type: {printer['label_type']}, status: {printer['status']}")

        except subprocess.TimeoutExpired:
            logger.error(f"Timeout getting status for printer {printer['name']} - USB might be busy")
            printer['status'] = "timeout"
        except Exception as e:
            logger.warning(f"Error getting status for printer {printer['name']}: {str(e)}")
            printer['status'] = str(e)

def get_label_width(label_type):
    """Get the pixel width of a label type."""
    label_definitions = labels.ALL_LABELS
    for label in label_definitions:
        if label.identifier == label_type:
            width = label.dots_printable[0]
            logger.debug(f"Label type {label_type} width: {width} dots")
            return width
    raise ValueError(f"Label type {label_type} not found in label definitions")

def _send_instructions(instructions, printer_info):
    """Send raster instructions to a printer and always release the USB handle.

    brother_ql's own send() never disposes the backend it opens - the handle
    only goes away whenever __del__ happens to run. In a long-lived Streamlit
    process that leaves the USB interface claimed after every print, so the
    next libusb_open on the device (ours, or the `brother_ql status`
    subprocess') fails or segfaults. Returns (success, message).
    """
    identifier = printer_info["identifier"]
    model = printer_info["model"]
    expects_status = model_has_status_channel(model)

    try:
        brother = get_printer(identifier, "pyusb")
    except SystemExit:
        # BrotherQLBackendPyUSB.__init__ calls sys.exit(1) when it can't claim
        # the device. SystemExit is a BaseException, so left alone it would
        # tear down the queue worker thread instead of failing this one job.
        raise RuntimeError(f"Could not open printer {identifier} (busy or permission denied)")

    try:
        logger.info(f"Sending {len(instructions)} bytes to {printer_info['name']}")
        brother.write(instructions)

        if not expects_status:
            # No back-channel to wait on: give the printer a moment to take the
            # job before anything else touches the bus.
            logger.debug(f"{model} cannot report status; skipping read-back")
            time.sleep(STATUS_LESS_SETTLE_SECONDS)
            return True, "Sent (printer does not report status)"

        # Wait for the printer to confirm it printed and is free again.
        did_print = ready = False
        start = time.time()
        while time.time() - start < 10:
            data = brother.read()
            if not data:
                time.sleep(0.005)
                continue
            try:
                result = interpret_response(data)
            except (ValueError, NameError) as e:
                logger.debug(f"Unparsable status response: {e}")
                continue
            if result["errors"]:
                return False, f"Printer reported errors: {result['errors']}"
            if result["status_type"] == "Printing completed":
                did_print = True
            if result["status_type"] == "Phase change" and result["phase_type"] == "Waiting to receive":
                ready = True
            if did_print and ready:
                return True, None

        logger.warning(f"No completion status from {printer_info['name']} within 10s")
        return True, "Sent, but the printer did not confirm completion"
    finally:
        # The whole point of this function: hand the interface back.
        brother.dispose()
        logger.debug(f"Released USB handle for {identifier}")


def print_image(image, printer_info, rotate=0, dither=False):
    """Queue a print job."""
    temp_dir = tempfile.gettempdir()
    os.makedirs(temp_dir, exist_ok=True)

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False, dir=temp_dir) as temp_file:
        temp_file_path = temp_file.name
        image.save(temp_file_path, "PNG")
        logger.info(f"{temp_file_path} added to print queue for printer {printer_info['name']}")

    logger.debug(f"Using label type: {printer_info['label_type']}")

    job_id = print_queue.add_job(
        image,
        rotate=rotate,
        dither=dither,
        printer_info=printer_info,
        temp_file_path=temp_file_path,
        label_type=printer_info["label_type"]
    )

    status = print_queue.get_job_status(job_id)
    status_container = st.empty()
    
    while status.status in ["pending", "processing"]:
        status_container.info(f"Print job status: {status.status}")
        time.sleep(0.5)
        status = print_queue.get_job_status(job_id)

    if status.status == "completed":
        status_container.success("Print job completed successfully!")
        if PRIVACY_MODE:
            status_container.info("Privacy mode is enabled; sticker not saved locally.")
        else:
            filename = safe_filename("Stikka-")
            file_path = os.path.join("labels", filename)
            image.save(file_path, "PNG")
            status_container.success(f"Sticker saved as {filename}")
        
        # Stats are pure stdlib and never touch pandas/pyarrow, so they can't
        # reintroduce the SIGILL that got this disabled. Still non-critical:
        # a stats failure must not turn a successful print into a failure.
        try:
            from stats_utils import record_print
            record_print(printer_info['name'], printer_info['model'])
        except Exception as e:
            logger.warning(f"Could not record print stats (non-critical): {e}")
        
        return True
    else:
        status_container.error(f"Print job failed: {status.error}")
        return False


def process_print_job(image, printer_info, temp_file_path, rotate=0, dither=False, label_type="102"):
    """
    Process a single print job.
    Returns (success, error_message)
    """

    try:
        # If debug mode is enabled, use virtual printer (save to debug directory)
        if DEBUG_MODE:
            debug_dir = Path("debug")
            debug_dir.mkdir(exist_ok=True)
            
            # Generate a filename with timestamp
            timestamp = int(time.time())
            filename = f"{timestamp}_debug_print_{printer_info['name'].replace(' ', '_')}.png"
            output_path = debug_dir / filename
            
            # Copy the image to debug directory
            image.save(output_path, "PNG")
            logger.info(f"DEBUG MODE: Virtual printer saved file to {output_path}")
            logger.debug(f"""
            Debug print parameters:
            - Label type: {label_type}
            - Rotate: {rotate}
            - Dither: {dither}
            - Model: {printer_info['model']}
            - Output: {output_path}
            """)
            return True, None
        
        # Prepare the image for printing
        qlr = BrotherQLRaster(printer_info["model"])
        
        logger.debug(f"Printing {temp_file_path} on label type {label_type} on printer {printer_info['name']}")
        
        instructions = convert(
            qlr=qlr,
            images=[temp_file_path],
            label=label_type,
            rotate=rotate,
            threshold=70,
            dither=dither,
            compress=True,
            red=False,
            dpi_600=False,
            hq=False,
            cut=True,
        )


        logger.debug(f"""
        Print parameters:
        - Label type: {label_type}
        - Rotate: {rotate}
        - Dither: {dither}
        - Model: {printer_info['model']}
        - Backend: {printer_info['backend']}
        - Identifier: {printer_info['identifier']}
        """)

        # Held for the whole conversation so printer discovery on the main
        # Streamlit thread can't open the same device mid-print.
        with _usb_lock:
            return _send_instructions(instructions, printer_info)

    except usb.core.USBError as e:
        # Treat timeout errors as successful since they often occur after print completion
        if e.errno == 110:  # Operation timed out
            logger.error("USB timeout occurred - this is normal and the print likely completed")
            return True, "Print completed (timeout is normal)"
        error_msg = f"USBError encountered: {e}"
        logger.error(error_msg)
        return False, error_msg

    except Exception as e:
        error_msg = f"Unexpected error during printing: {str(e)}"
        logger.error(error_msg)
        return False, error_msg

    finally:
        # Clean up temporary file
        try:
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)
                logger.debug(f"Temporary file {temp_file_path} deleted.")
        except Exception as e:
            logger.warning(f"Failed to delete temporary file {temp_file_path}: {str(e)}")