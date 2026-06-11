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
TX_EOL          = b"\n"           # commands sent with bare LF (replies use CR+LF)

# --- Protocol constants -------------------------------------------------------
CMD_STATUS = "1"                  # status update command code
STATUS_OK  = "0"                  # final reply line indicating success


def open_port() -> serial.Serial:
    """Open and configure the COM port; return the ready-to-use Serial object."""
    ser = serial.Serial()         # construct unopened so DTR/RTS apply at open
    ser.port = COM_PORT
    ser.baudrate = BAUD_RATE
    ser.bytesize = DATA_BITS
    ser.parity = PARITY
    ser.stopbits = STOP_BITS
    ser.xonxoff = FLOW_XONXOFF
    ser.rtscts = FLOW_RTSCTS
    ser.dsrdtr = FLOW_DSRDTR
    ser.dtr = DTR_STATE
    ser.rts = RTS_STATE
    ser.timeout = READ_TIMEOUT_S
    ser.write_timeout = WRITE_TIMEOUT_S
    ser.open()
    ser.reset_input_buffer()      # purge stale bytes (capture did RXCLEAR)
    return ser


def send_command(ser: serial.Serial, *fields: str) -> None:
    """Send a command: each field (code, then any arguments) followed by LF."""
    for field in fields:
        ser.write(field.encode("ascii") + TX_EOL)


def read_reply(ser: serial.Serial, n_lines: int) -> list[str]:
    """Read n_lines CR+LF-terminated reply lines, returned stripped.

    Raises TimeoutError if a line does not arrive within READ_TIMEOUT_S.
    """
    lines = []
    for _ in range(n_lines):
        raw = ser.readline()      # returns b"" on timeout
        if not raw:
            raise TimeoutError(
                f"no reply after {len(lines)} of {n_lines} line(s): {lines}"
            )
        lines.append(raw.decode("ascii", errors="replace").rstrip("\r\n"))
    return lines


def status_update(ser: serial.Serial) -> bool:
    """Send the status update command; expect 'CompProbe' then status '0'."""
    send_command(ser, CMD_STATUS)
    name, status = read_reply(ser, 2)
    ok = name == "CompProbe" and status == STATUS_OK
    print(f"Status update: name={name!r} status={status!r} -> "
          f"{'OK' if ok else 'UNEXPECTED'}")
    return ok


def close_port(ser: serial.Serial) -> None:
    if ser.is_open:
        ser.close()


def main() -> int:
    print(f"Opening {COM_PORT} at {BAUD_RATE} baud "
          f"(8N1, DTR={'on' if DTR_STATE else 'off'}, "
          f"RTS={'on' if RTS_STATE else 'off'})")
    try:
        ser = open_port()
    except serial.SerialException as exc:
        # Typical causes: probe not plugged in / wrong COM number, or the port
        # is held open by LTPowerAnalyzer or a terminal (exclusive on Windows).
        print(f"Could not open {COM_PORT}: {exc}")
        return 1

    try:
        ok = status_update(ser)
    except TimeoutError as exc:
        print(f"Probe did not respond: {exc}")
        return 1
    finally:
        close_port(ser)
        print(f"{COM_PORT} closed")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
