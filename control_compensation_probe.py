"""Control the Analog Devices EVAL-LTPA-COMPRB compensation probe over USB serial.

The probe is normally driven by LTPowerAnalyzer but accepts plain ASCII commands
on its USB virtual COM port (protocol reverse-engineered from a port capture,
2026-06-10). It switches resistance/capacitance arrays based on the commands.

Protocol:
  - 115200 baud, 8 data bits, no parity, 1 stop bit, no flow control.
  - DTR must be asserted, RTS held off (matches LTPowerAnalyzer's port setup).
  - Commands are ASCII fields, each terminated with LF.
  - Reply lines are terminated with CR+LF; the final line is a status code,
    "0" meaning success.

Known commands (reply line count includes the final status line):
  code  args            reply
  1     -               "CompProbe", status            (status update)
  2     -               name, fw ver, hw ver, serial, value?, 2 dates, status
  3     -               <EOT>, status                  (blink LED on PCB)
  11    -               target R, cap R, array R, total R [ohm],
                        target C, total C [pF], status (get configuration)
  20    resistance ohm  <EOT>, status                  (set resistance)
  21    capacitance pF  <EOT>, status                  (set capacitance)

Requires: pyserial (pip install pyserial). Python 3.13.
"""

import sys

import serial

# --- Connection configuration -----------------------------------------------
COM_PORT        = "COM22"
BAUD_RATE       = 115200
DATA_BITS       = serial.EIGHTBITS
PARITY          = serial.PARITY_NONE
STOP_BITS       = serial.STOPBITS_ONE
FLOW_XONXOFF    = False           # no software flow control
FLOW_RTSCTS     = False           # no hardware flow control
FLOW_DSRDTR     = False
DTR_STATE       = True            # probe needs DTR asserted before it responds
RTS_STATE       = False           # LTPowerAnalyzer keeps RTS off
READ_TIMEOUT_S  = 1.0             # per-line read timeout (capture used 1000 ms)
WRITE_TIMEOUT_S = 1.0
TX_EOL          = b"\n"           # commands sent with bare LF
RX_EOL          = b"\r\n"         # reply lines terminated with CR+LF

# --- Protocol constants -------------------------------------------------------
CMD_STATUS     = "1"              # status update command code
CMD_GET_CONFIG = "11"             # get configuration command code
STATUS_OK      = "0"              # final reply line indicating success

# Single shared port object (used by all functions)
ser = serial.Serial()


def open_port() -> None:
    """Configure and open the COM port; print and exit on failure."""
    ser.port = COM_PORT
    ser.baudrate = BAUD_RATE
    ser.bytesize = DATA_BITS
    ser.parity = PARITY
    ser.stopbits = STOP_BITS
    ser.xonxoff = FLOW_XONXOFF
    ser.rtscts = FLOW_RTSCTS
    ser.dsrdtr = FLOW_DSRDTR
    ser.dtr = DTR_STATE           # set before open so lines are correct from
    ser.rts = RTS_STATE           # the first moment
    ser.timeout = READ_TIMEOUT_S
    ser.write_timeout = WRITE_TIMEOUT_S
    
    print(f"Opening {COM_PORT} at {BAUD_RATE} baud "
          f"({DATA_BITS}{PARITY}{STOP_BITS}, "
          f"DTR={'on' if DTR_STATE else 'off'}, "
          f"RTS={'on' if RTS_STATE else 'off'})")
    
    try:
        ser.open()
    except serial.SerialException as exc:
        # Typical causes: probe not plugged in / wrong COM number, or the port
        # is held open by LTPowerAnalyzer or a terminal (exclusive on Windows).
        print(f"Could not open {COM_PORT}: {exc}")
        sys.exit(1)
    ser.reset_input_buffer()      # purge stale bytes (capture did RXCLEAR)


def send_command(*fields: str) -> None:
    """Send a command: each field (code, then any arguments) followed by LF."""
    for field in fields:
        ser.write(field.encode("ascii") + TX_EOL)


def read_reply(n_lines: int) -> list[str]:
    """Read n_lines reply lines, returned stripped; print and exit on timeout."""
    lines = []
    for _ in range(n_lines):
        raw = ser.read_until(RX_EOL)  # returns whatever arrived on timeout
        if not raw.endswith(RX_EOL):
            print(f"Probe did not respond: got {len(lines)} of {n_lines} "
                  f"line(s) {lines}, then {raw!r}")
            sys.exit(1)
        lines.append(raw.removesuffix(RX_EOL).decode("ascii", errors="replace"))
    return lines


def cmd_status_update() -> None:
    """Status update (command 1): expect 'CompProbe' then status '0'."""
    send_command(CMD_STATUS)
    name, status = read_reply(2)
    ok = name == "CompProbe" and status == STATUS_OK
    print(f"Status update: name={name!r} status={status!r} -> "
          f"{'OK' if ok else 'UNEXPECTED'}")
    if not ok:
        sys.exit(1)


def cmd_get_configuration(print_results: bool = True) -> dict[str, float]:
    """Get configuration (command 11): return the probe's current R/C values.

    Returns a dict of floats so other functions can use the results, e.g.
    cmd_get_configuration(print_results=False)["total_resistance_ohm"].
    """
    send_command(CMD_GET_CONFIG)
    *values, status = read_reply(7)
    if status != STATUS_OK:
        print(f"Get configuration failed: status={status!r}")
        sys.exit(1)
    keys = ("target_resistance_ohm", "cap_resistance_ohm",
            "array_resistance_ohm", "total_resistance_ohm",
            "target_capacitance_pf", "total_capacitance_pf")
    try:
        config = dict(zip(keys, (float(v) for v in values)))
    except ValueError:
        print(f"Get configuration returned non-numeric value(s): {values}")
        sys.exit(1)
    if print_results:
        print("Configuration:")
        for key, value in config.items():
            print(f"  {key:<22}: {value:.3f}")
    return config


def close_port() -> None:
    if ser.is_open:
        ser.close()
        print(f"{COM_PORT} closed")


def main() -> None:
    # Connect to the probe
    open_port()
    # Confirm the probe is alive and talking
    cmd_status_update()
    # Read and show the current R/C configuration
    cmd_get_configuration()
    # Done - release the port
    close_port()


if __name__ == "__main__":
    main()
