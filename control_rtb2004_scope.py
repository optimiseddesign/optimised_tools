"""Control a Rohde & Schwarz RTB2004 oscilloscope over USB virtual COM port.

Drives the Bode plot application (option RTB-K36) remotely: start a sweep,
wait for completion, fetch the frequency/gain/phase data, and compute gain
and phase margins. Signal configuration (input/output channels, generator
amplitude, sweep range etc.) is assumed to be already set up on the scope.

Connection:
  - Scope set to USB VCP mode (Setup > Interface > USB > Parameter > USB VCP);
    it appears as a virtual COM port carrying plain ASCII SCPI text.
  - The same SCPI text works over LAN, so the transport can be swapped later
    without touching the command functions: all instrument I/O is confined to
    open_connection(), scpi_send(), scpi_query() and close_connection().
    Planned LAN transport is the RsInstrument package, whose API maps 1:1
    (write_str/query_str; resource "TCPIP::<ip>::5025::SOCKET" with
    SelectVisa='SocketIo' needs no VISA install).
  - Commands are ASCII lines terminated with LF; replies are LF-terminated.
  - SCPI commands from the R&S RTB2 user manual v14 (doc 1333.1611.02),
    Bode plot remote commands chapter 16.8.7.

Requires: pyserial (pip install pyserial). Python 3.13.

Copyright Optimised Product Design Ltd 2026. Available for public use
(copyright reserved) - see repository README; use at your own risk.
"""

import sys

import serial

# --- Connection configuration -----------------------------------------------
COM_PORT        = "COM23"
BAUD_RATE       = 115200          # VCP ignores UART settings, but set anyway
DATA_BITS       = serial.EIGHTBITS
PARITY          = serial.PARITY_NONE
STOP_BITS       = serial.STOPBITS_ONE
TIMEOUT_READ_S  = 2.0             # per-reply read timeout
TIMEOUT_WRITE_S = 1.0
TX_EOL          = b"\n"           # commands sent with bare LF
RX_EOL          = b"\n"           # reply lines terminated with LF

# --- Protocol constants -------------------------------------------------------
CMD_CLEAR_STATUS = "*CLS"         # clear status registers and error queue
CMD_IDENTIFY     = "*IDN?"        # identity: manufacturer,model,serial,firmware
CMD_GET_OPTIONS  = "*OPT?"        # installed options, comma-separated
OPTION_BODE      = "K36"          # Bode plot application option (RTB-K36)

# Single shared port object (used by all functions)
ser = serial.Serial()


def open_connection() -> None:
    """Configure and open the COM port; print and exit on failure."""
    ser.port = COM_PORT
    ser.baudrate = BAUD_RATE
    ser.bytesize = DATA_BITS
    ser.parity = PARITY
    ser.stopbits = STOP_BITS
    ser.timeout = TIMEOUT_READ_S
    ser.write_timeout = TIMEOUT_WRITE_S

    print(f"Opening {COM_PORT} at {BAUD_RATE} baud")

    try:
        ser.open()                # pyserial asserts DTR/RTS, like a terminal
    except serial.SerialException as exc:
        # Typical causes: scope not in USB VCP mode / wrong COM number, or the
        # port is held open by another program (exclusive on Windows).
        print(f"Could not open {COM_PORT}: {exc}")
        sys.exit(1)
    ser.reset_input_buffer()      # purge stale bytes from a previous session
    # Clear the scope's status/error queue too. Deliberately no *RST: the
    # signal configuration on the scope must be preserved.
    scpi_send(CMD_CLEAR_STATUS)


def scpi_send(command: str) -> None:
    """Send a SCPI command that produces no reply."""
    ser.write(command.encode("ascii") + TX_EOL)


def scpi_query(command: str, timeout_s: float = TIMEOUT_READ_S) -> str:
    """Send a SCPI query and return its one-line reply; print and exit on timeout.

    timeout_s allows slow queries (e.g. during a running Bode sweep) to wait
    longer.
    """
    ser.timeout = timeout_s       # applies per query; set on every call
    scpi_send(command)
    raw = ser.read_until(RX_EOL)  # returns whatever arrived on timeout
    if raw.endswith(RX_EOL):
        return raw.decode("ascii", errors="replace").strip()
    else:
        print(f"Scope did not reply to {command}: got {raw!r}")
        sys.exit(1)


def cmd_identify(print_results: bool = True) -> dict[str, str]:
    """Identify the scope (*IDN?) and check the Bode plot option is installed.

    Returns a dict of strings so other functions can use the results.
    """
    identity = scpi_query(CMD_IDENTIFY)
    fields = identity.split(",")
    if len(fields) == 4:
        keys = ("manufacturer", "model", "serial", "version_fw")
        info = dict(zip(keys, (field.strip() for field in fields)))
    else:
        print(f"Identify failed: unexpected reply {identity!r}")
        sys.exit(1)

    options = scpi_query(CMD_GET_OPTIONS)
    if OPTION_BODE in options:
        info["options"] = options
    else:
        print(f"Bode plot option {OPTION_BODE} not installed: options={options!r}")
        sys.exit(1)

    if print_results:
        print("Scope identification:")
        for key, value in info.items():
            print(f"  {key:<12}: {value}")
    return info


def close_connection() -> None:
    if ser.is_open:
        ser.close()
        print(f"{COM_PORT} closed")


def main() -> None:
    # Connect to the scope
    open_connection()

    # Confirm the scope is alive and the Bode plot option is present
    cmd_identify()

    # Done - release the port
    close_connection()


if __name__ == "__main__":
    main()
