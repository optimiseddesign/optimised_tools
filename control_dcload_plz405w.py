"""Control a Kikusui PLZ405W electronic DC load over LAN.

Drives the load input remotely - operation mode, current and voltage ranges,
load level and input on/off - so a supply or converter under test can be
loaded without touching the front panel.

This version covers the transport, connection and identification only; the
mode, range, level and on/off commands follow once the connection is proven
on the hardware.

Connection:
  - LAN (LXI): the PLZ-5W series is LXI compliant and accepts VXI-11, HiSLIP
    or SCPI-RAW on its LAN interface, all on port 5025. This file uses
    SCPI-RAW - a plain TCP stream of ASCII SCPI text - through PyVISA with
    the pure-Python pyvisa-py backend, so no VISA installation is needed.
    Kikusui publish no Python library of their own (unlike the R&S
    RsInstrument used by control_oscilloscope_rtb2004.py), and PyVISA is the
    standard vendor-neutral alternative. With NI-VISA (or another vendor
    VISA) installed, setting VISA_BACKEND to "" and LAN_RESOURCE to
    "TCPIP::<ip>::INSTR" switches to VXI-11 instead; nothing else changes.
  - Set LAN_ADDRESS to the load's IP address, shown (and configured, DHCP or
    static) in its LAN settings menu; the same settings are also reachable
    from the load's built-in web page.
  - Commands are ASCII lines terminated with LF; replies are LF-terminated.
  - All instrument I/O is confined to open_connection(), close_connection(),
    scpi_send(), scpi_query() and scpi_set(); the command functions never see
    the transport, so the USB or RS232C ports (both fitted as standard) can
    be added later without touching them.
  - SCPI commands from the Kikusui PLZ-5W communication interface manual,
    "List of Messages"; written here in their long form.

Requires: PyVISA and its pure-Python backend (pip install pyvisa pyvisa-py).
Python 3.13.

Copyright Optimised Product Design Ltd 2026. Available for public use
(copyright reserved) - see repository README; use at your own risk.
"""

import sys

import pyvisa

# --- Connection configuration -------------------------------------------------
LAN_ADDRESS     = "192.168.1.101"  # load IP: its LAN settings menu / web page
LAN_RESOURCE    = f"TCPIP::{LAN_ADDRESS}::5025::SOCKET"  # SCPI-RAW, port 5025
VISA_BACKEND    = "@py"           # pyvisa-py, pure Python; "" uses vendor VISA
TIMEOUT_READ_S  = 2.0             # per-reply read timeout
TX_EOL          = "\n"            # commands sent with bare LF
RX_EOL          = "\n"            # reply lines terminated with LF
MS_PER_S        = 1000            # PyVISA timeouts are in milliseconds

# --- Protocol constants -------------------------------------------------------
CMD_CLEAR_STATUS = "*CLS"             # clear status registers and error queue
CMD_GET_IDENTITY = "*IDN?"            # manufacturer,model,serial,firmware
CMD_GET_ERROR    = "SYSTem:ERRor?"    # oldest error in the queue; "0,..." = none
CMD_SET_LOCAL    = "SYSTem:COMMunicate:RLSTate LOCal"  # release the front panel
REPLY_NO_ERROR   = "0,"               # SYSTem:ERRor? prefix when queue is empty
IDENTITY_FIELDS  = 4                  # *IDN? replies with four comma-separated fields
MODEL_EXPECTED   = "PLZ405W"          # the PLZ-5W series shares a command set but
                                      # not its ratings - refuse the wrong model

# Single shared session objects (used by all functions); created by
# open_connection(), both None until then
resource_manager = None           # PyVISA resource manager, owns the backend
dcload = None                     # PyVISA session to the load


def open_connection() -> None:
    """Open the LAN connection and put the load in a known state."""
    lan_open()
    # Clear the load's status registers and error queue. Deliberately no
    # *RST: the protection settings (OCP, OPP, UVP) and any panel setup must
    # be preserved. Sent via scpi_set() so the error queue is read straight
    # back, confirming the SCPI link works in both directions before anything
    # is driven.
    scpi_set(CMD_CLEAR_STATUS)


def lan_open() -> None:
    """Open the PyVISA LAN session; print and exit on failure."""
    global resource_manager, dcload
    print(f"Opening DC load at {LAN_RESOURCE}")

    try:
        resource_manager = pyvisa.ResourceManager(VISA_BACKEND)
        dcload = resource_manager.open_resource(LAN_RESOURCE)
    except pyvisa.Error as exc:
        # Typical causes: load powered off or on another subnet, wrong
        # LAN_ADDRESS, or another program already holding the SCPI-RAW port.
        print(f"Could not open {LAN_RESOURCE}: {exc}")
        sys.exit(1)
    # A raw socket carries no message boundaries, so PyVISA must be told the
    # terminators; VXI-11 supplies its own but accepts these unchanged
    dcload.write_termination = TX_EOL
    dcload.read_termination = RX_EOL
    dcload.timeout = int(TIMEOUT_READ_S * MS_PER_S)


def scpi_send(command: str) -> None:
    """Send a SCPI command that produces no reply."""
    dcload.write(command)


def scpi_query(command: str, timeout_s: float = TIMEOUT_READ_S) -> str:
    """Send a SCPI query and return its one-line reply; print and exit on timeout.

    timeout_s allows slow queries to wait longer. It applies per query and is
    set on every call.
    """
    dcload.timeout = int(timeout_s * MS_PER_S)
    try:
        return dcload.query(command).strip()
    except pyvisa.Error as exc:
        print(f"Load did not reply to {command}: {exc}")
        sys.exit(1)


def scpi_set(command: str, argument: str = "") -> None:
    """Send a set command, then check the load's error queue.

    Set commands produce no reply, so errors (e.g. a value outside the
    selected range) would otherwise go unnoticed; print and exit on error.
    argument, if given, is appended after a space (e.g. a current level).
    """
    if argument:
        command = command + " " + argument
    scpi_send(command)
    error = scpi_query(CMD_GET_ERROR)
    if error.startswith(REPLY_NO_ERROR):
        return                    # command accepted
    else:
        print(f"Command {command} failed: {error}")
        sys.exit(1)


def cmd_identify(print_results: bool = True) -> dict[str, str]:
    """Identify the load (*IDN?) and check it is the expected model.

    Returns a dict of strings so other functions can use the results.
    """
    identity = scpi_query(CMD_GET_IDENTITY)
    fields = identity.split(",")
    if len(fields) == IDENTITY_FIELDS:
        keys = ("manufacturer", "model", "serial", "version_fw")
        info = dict(zip(keys, (field.strip() for field in fields)))
    else:
        print(f"Identify failed: unexpected reply {identity!r}")
        sys.exit(1)

    if MODEL_EXPECTED in info["model"]:
        if print_results:
            print("DC load identification:")
            for key, value in info.items():
                print(f"  {key:<12}: {value}")
    else:
        # Current and voltage ratings differ across the series, so driving a
        # sibling model with this file's levels could overload the load
        print(f"Expected a {MODEL_EXPECTED}, but found a {info['model']!r}")
        sys.exit(1)
    return info


def close_connection() -> None:
    # Hand the front panel back: the load latches into remote on the first
    # command received and would otherwise stay locked out after the run
    if dcload is not None:
        scpi_send(CMD_SET_LOCAL)
        dcload.close()
        resource_manager.close()
        print(f"{LAN_RESOURCE} closed")


def main() -> None:
    # Connect to the load
    open_connection()

    # Confirm the load is alive and is the expected model
    cmd_identify()

    # Done - release the connection
    close_connection()


if __name__ == "__main__":
    main()
