"""Control a TTi MX180TP triple-output DC power supply over LAN.

Sets output voltages, current limits and output on/off states remotely. This
first version covers the connection and instrument identification only; the
output commands follow.

The three outputs are numbered 1 to 3: outputs 1 and 2 are 30V/6A, 15V/10A or
60V/3A (output 1 has higher combined ranges available with output 2 disabled),
output 3 is 5.5V/3A or 12V/1.5A. The MX180T has no remote interfaces at all -
only the MX180TP can be controlled.

Connection:
  - LAN, raw TCP sockets on port 9221 - the only remote control path the LAN
    interface offers. The instrument is LXI Core 2011 compliant but implements
    just enough VXI-11 for discovery, so a VISA "TCPIP::<ip>::INSTR" resource
    name does not work; the manual specifies "TCPIP0::<ip>::9221::SOCKET"
    instead. That is a plain ASCII line protocol, so the standard library
    socket module drives it directly - no VISA, pyvisa or vendor package
    needed, unlike control_oscilloscope_rtb2004.py whose scope offers a full
    VISA stack and is driven through RsInstrument.
  - Set LAN_ADDRESS to the PSU's IP address, shown (and configured, DHCP or
    static) under Menu > Remote Control Interfaces on the instrument.
  - Commands are ASCII, case insensitive, terminated with LF; replies are
    terminated with CR+LF. The instrument accepts two simultaneous control
    sockets, so a second program does not block a run.
  - The instrument has no SCPI error queue (no SYSTem:ERRor?): a rejected
    command is reported in the Standard Event Status Register (*ESR?), with
    the reason in the Execution Error Register (EER?). Both are cleared when
    read, so each check starts from a clean register.
  - Any command puts the instrument into the remote state, which locks its
    keyboard, so close_connection() sends LOCAL to hand the front panel back.
  - Commands from the TTi MX180T & MX180TP instruction manual issue 5,
    Remote Commands chapter 13; LAN interface details from chapter 12.2.4.

Requires: no external packages. Python 3.13.

Copyright Optimised Product Design Ltd 2026. Available for public use
(copyright reserved) - see repository README; use at your own risk.
"""

import socket
import sys

# --- Connection configuration -------------------------------------------------
LAN_ADDRESS       = "192.168.10.25"  # PSU IP: Menu > Remote Control Interfaces
LAN_PORT          = 9221             # TTi instrument control/monitoring socket
TIMEOUT_CONNECT_S = 5.0              # TCP connection attempt
TIMEOUT_READ_S    = 2.0              # per-reply read timeout
RX_CHUNK_BYTES    = 256              # replies are short single lines
TX_EOL            = b"\n"            # commands terminated with LF
RX_EOL            = b"\r\n"          # reply lines terminated with CR+LF

# --- Protocol constants -------------------------------------------------------
CMD_CLEAR_STATUS   = "*CLS"      # clear every status register
CMD_GET_IDENTITY   = "*IDN?"     # manufacturer,model,serial,firmware
CMD_GET_EVENT_STAT = "*ESR?"     # standard event status register, as an integer
CMD_GET_EXEC_ERROR = "EER?"      # execution error register, as an integer
CMD_LOCK_ON        = "IFLOCK 1"  # take exclusive control of the instrument
CMD_LOCK_OFF       = "IFLOCK 0"  # release it
CMD_GO_LOCAL       = "LOCAL"     # unlock the front panel keyboard
MODEL_EXPECTED     = "MX180TP"   # model field of the *IDN? reply
ESR_ERROR_BITS     = {           # *ESR? bits reporting a rejected command
    0x20: "command error (syntax)",
    0x10: "execution error",
    0x08: "verify timeout",
    0x04: "query error"}
ESR_ERROR_MASK     = 0x3C        # the bits above; the rest are power-on and *OPC
ESR_BIT_EXEC_ERROR = 0x10        # the one bit whose detail EER? explains
ERROR_EXEC_TEXT    = {           # EER? codes, as returned, and their meaning
    "100": "numeric: a parameter was outside the permitted range",
    "102": "recall: the specified store holds no data",
    "103": "command invalid in the present circumstances",
    "104": "range change failed: >0.5 V still present on output 1 or 2",
    "200": "access denied: another interface holds the lock"}

# Single shared socket (used by all functions); created by open_connection()
sock = None


def open_connection() -> None:
    """Open the LAN socket and put the PSU in a known state."""
    lan_open()
    # Clear the status registers, including the power-on bit that would
    # otherwise be read as an error by the first scpi_set(). Deliberately no
    # *RST: the output settings on the PSU must be preserved.
    scpi_send(CMD_CLEAR_STATUS)
    # Take exclusive control, as the manual recommends, so that another
    # interface (the PSU's web page, USB, RS232 or GPIB) cannot change
    # settings mid-run. Released by close_connection(), by the front panel
    # LOCAL key, or when the PSU sees the socket close.
    scpi_set(CMD_LOCK_ON)


def lan_open() -> None:
    """Open the TCP socket to the PSU; print and exit on failure."""
    global sock
    print(f"Opening Power Supply at {LAN_ADDRESS}:{LAN_PORT}")

    try:
        sock = socket.create_connection((LAN_ADDRESS, LAN_PORT),
                                        TIMEOUT_CONNECT_S)
    except OSError as exc:
        # Typical causes: PSU powered off or on another subnet, wrong
        # LAN_ADDRESS, or both of its control sockets already in use.
        print(f"Could not connect to {LAN_ADDRESS}:{LAN_PORT}: {exc}")
        sys.exit(1)


def scpi_send(command: str) -> None:
    """Send a SCPI command that produces no reply; print and exit on failure."""
    try:
        sock.sendall(command.encode("ascii") + TX_EOL)
    except OSError as exc:
        print(f"Could not send {command} to {LAN_ADDRESS}: {exc}")
        sys.exit(1)


def scpi_query(command: str, timeout_s: float = TIMEOUT_READ_S) -> str:
    """Send a SCPI query and return its one-line reply; print and exit on timeout.

    timeout_s allows slow queries to wait longer. It applies per query and is
    set on every call. TCP delivers a byte stream rather than messages, so the
    reply is read until its CR+LF terminator arrives.
    """
    scpi_send(command)
    sock.settimeout(timeout_s)
    reply = bytearray()
    while not reply.endswith(RX_EOL):
        try:
            chunk = sock.recv(RX_CHUNK_BYTES)
        except TimeoutError:
            print(f"PSU did not reply to {command} within {timeout_s:.1f} s: "
                  f"got {bytes(reply)!r}")
            sys.exit(1)
        except OSError as exc:
            print(f"Lost the connection to {LAN_ADDRESS} reading {command}: {exc}")
            sys.exit(1)
        if not chunk:             # an empty read means the PSU closed the socket
            print(f"PSU closed the connection reading {command}: "
                  f"got {bytes(reply)!r}")
            sys.exit(1)
        reply += chunk
    return reply.decode("ascii", errors="replace").strip()


def scpi_set(command: str, argument: str = "") -> None:
    """Send a set command, then check the PSU accepted it.

    Set commands produce no reply, so errors (e.g. an out-of-range voltage)
    would otherwise go unnoticed; print and exit on error. argument, if given,
    is appended after a space (e.g. an output voltage).
    """
    if argument:
        command = command + " " + argument
    scpi_send(command)

    status = scpi_query(CMD_GET_EVENT_STAT)
    try:
        bits = int(status)
    except ValueError:
        print(f"Status check after {command} returned non-numeric {status!r}")
        sys.exit(1)

    if not bits & ESR_ERROR_MASK:
        return                    # command accepted
    else:
        faults = [text for bit, text in ESR_ERROR_BITS.items() if bits & bit]
        # The execution error bit says only that something failed; the reason
        # is a code held in the separate execution error register
        if bits & ESR_BIT_EXEC_ERROR:
            code = scpi_query(CMD_GET_EXEC_ERROR)
            faults.append(f"EER {code} - "
                          f"{ERROR_EXEC_TEXT.get(code, 'unrecognised code')}")
        print(f"Command {command} failed: {', '.join(faults)}")
        sys.exit(1)


def cmd_identify(print_results: bool = True) -> dict[str, str]:
    """Identify the PSU (*IDN?) and check it is the expected model.

    Returns a dict of strings so other functions can use the results.
    """
    identity = scpi_query(CMD_GET_IDENTITY)
    fields = identity.split(",")
    if len(fields) == 4:
        keys = ("manufacturer", "model", "serial", "version_fw")
        info = dict(zip(keys, (field.strip() for field in fields)))
    else:
        print(f"Identify failed: unexpected reply {identity!r}")
        sys.exit(1)

    if info["model"] == MODEL_EXPECTED:
        if print_results:
            print("Power supply identification:")
            for key, value in info.items():
                print(f"  {key:<12}: {value}")
    else:
        print(f"Expected model {MODEL_EXPECTED} but found {info['model']!r}: "
              f"its remote command set may differ")
        sys.exit(1)
    return info


def close_connection() -> None:
    if sock is not None:
        # Release the lock and hand the keyboard back: any command puts the
        # instrument into the remote state, which locks its front panel until
        # LOCAL is sent or its LOCAL key is pressed. Neither command is
        # error-checked, as the check is itself a command and would put the
        # instrument straight back into the remote state.
        scpi_send(CMD_LOCK_OFF)
        scpi_send(CMD_GO_LOCAL)
        sock.close()
        print(f"{LAN_ADDRESS}:{LAN_PORT} closed")


def main() -> None:
    # Connect to the power supply and take exclusive control of it
    open_connection()

    # Confirm the power supply is alive and is the expected model
    cmd_identify()

    # Done - release the interface lock and the socket
    close_connection()


if __name__ == "__main__":
    main()
