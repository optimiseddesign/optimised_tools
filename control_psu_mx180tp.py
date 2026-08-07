"""Control a TTi MX180TP triple-output DC power supply over LAN.

Sets output voltages, current limits, voltage/current ranges and output enable
states remotely, and reads back both the setpoints and what each output is
actually delivering.

The three outputs are numbered 1 to 3: outputs 1 and 2 are 30V/6A, 15V/10A or
60V/3A (output 1 has higher combined ranges available with output 2 disabled),
output 3 is 5.5V/3A or 12V/1.5A. The MX180T has no remote interfaces at all -
only the MX180TP can be controlled.

Connection:
  - LAN, a raw TCP socket on port 9221 - the only remote control path the LAN
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
    terminated with CR+LF.
  - Only one control socket is accepted at a time, despite the manual's claim
    of two, so a second program holding the port blocks a run - as the
    compensation probe's COM port does. The socket is released immediately
    on closing, an abrupt close included, so a repeat run is never locked out.
  - The instrument has no SCPI error queue (no SYSTem:ERRor?): a rejected
    command is reported in the Standard Event Status Register (*ESR?), with
    the reason in the Execution Error Register (EER?). Both are cleared when
    read, so each check starts from a clean register.
  - A query the instrument will not answer - one it does not recognise, e.g.
    "V9?", or one it refuses, e.g. any output 2 query while output 1 holds a
    high range - is met with silence rather than an error, so it shows up here
    as a read timeout. The reason is left in the status registers, which
    scpi_query() neither reads nor clears; the next scpi_set() would therefore
    report that stale error as its own. Harmless as the code stands, because a
    query timeout exits the run, but it matters if error handling is ever
    changed to continue past one.
  - The interface lock is taken with IFLOCK, released with IFUNLOCK and read
    with IFLOCK?, each replying with the resulting state: "1" if this
    interface instance owns the lock, "0" if no interface does, "-1" if
    another one does. They are therefore queries, not set commands. The
    "IFLOCK <NRF>" form in the MX180T manual (issue 5, section 13.2.4) is not
    what the firmware implements and is rejected as a syntax error; the older
    TTi wording, as in the CPX400DP manual, matches the instrument.
  - A session dropped without unlocking does not strand the lock: the next
    connection on that socket inherits it, and the front panel LOCAL key
    clears it. Taking the lock again is harmless, as is releasing it twice.
  - Any command puts the instrument into the remote state, which locks its
    keyboard, so close_connection() sends LOCAL to hand the front panel back.
    LOCAL takes effect at once, without needing the socket closed. The error
    paths below exit rather than return, so open_connection() registers
    close_connection() with atexit: the lock is released and the panel handed
    back however a run ends, an unhandled exception included.
  - Commands from the TTi MX180T & MX180TP instruction manual issue 5,
    Remote Commands chapter 13; LAN interface details from chapter 12.2.4.

Requires: no external packages. Python 3.13.
Tested against MX180TP firmware 1.09-1.00-1.09.

Copyright Optimised Product Design Ltd 2026. Available for public use
(copyright reserved) - see repository README; use at your own risk.
"""

import atexit
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
CMD_LOCK_ON        = "IFLOCK"    # take exclusive control; replies with the state
CMD_LOCK_OFF       = "IFUNLOCK"  # release it; replies with the state
CMD_GO_LOCAL       = "LOCAL"     # unlock the front panel keyboard
IDENTITY_FIELDS    = 4           # *IDN? reply has four comma-separated fields
MODEL_EXPECTED     = "MX180TP"   # model field of the *IDN? reply
REPLY_LOCK_OWNED   = "1"         # lock state: held by this interface instance
                                 # ("0" = held by none, "-1" = by another)
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

# --- Per-output commands ------------------------------------------------------
# {output} is the output number. The PSU answers a number it does not have with
# silence rather than an error, which would surface as a read timeout, so
# check_output() rejects one before it is ever sent.
OUTPUTS               = (1, 2, 3)
CMD_SET_VOLTAGE       = "V{output}"        # argument in volts
CMD_SET_CURRENT       = "I{output}"        # argument in amps
CMD_SET_RANGE         = "VRANGE{output}"   # argument is a RANGE_LIMITS index
CMD_SET_OUTPUT_ENABLE = "OP{output}"       # enable/disable the output
ARG_OUTPUT_ENABLE     = "1"                # OP<N> argument to enable
ARG_OUTPUT_DISABLE    = "0"                # and to disable
CMD_GET_VOLTAGE_SET   = "V{output}?"       # reply "V<N> <volts>", the setpoint
CMD_GET_CURRENT_SET   = "I{output}?"       # reply "I<N> <amps>", the limit
CMD_GET_RANGE         = "VRANGE{output}?"  # reply a RANGE_LIMITS index
CMD_GET_OUTPUT_ENABLE = "OP{output}?"      # reply "1" enabled, "0" disabled
CMD_MEASURE_VOLTAGE   = "V{output}O?"      # reply "<volts>V", measured
CMD_MEASURE_CURRENT   = "I{output}O?"      # reply "<amps>A", measured

# Range index -> (maximum volts, maximum amps), per output. Output 1's ranges 4
# to 7 reach 20 A or 120 V by borrowing output 2's hardware, so they are only
# available with output 2 disabled.
RANGE_LIMITS = {
    1: {1: (30.0, 6.0), 2: (15.0, 10.0), 3: (60.0, 3.0),
        4: (30.0, 12.0), 5: (15.0, 20.0), 6: (60.0, 6.0), 7: (120.0, 3.0)},
    2: {1: (30.0, 6.0), 2: (15.0, 10.0), 3: (60.0, 3.0)},
    3: {1: (5.5, 3.0), 2: (12.0, 1.5)}}
RANGES_USING_OUTPUT2 = (4, 5, 6, 7)  # output 1 ranges that consume output 2

# The same indices named by what they allow, for callers that would rather say
# the rating than the index. An index means different things on different
# outputs, so each name says which output it is for; cmd_set_range() checks
# the index exists on the output, but cannot tell that 1 was meant as output
# 3's 5.5 V range rather than output 1's 30 V one.
RANGE_30V_6A  = 1   # output 1 or 2
RANGE_15V_10A = 2   # output 1 or 2
RANGE_60V_3A  = 3   # output 1 or 2
RANGE_30V_12A = 4   # output 1 only, borrows output 2
RANGE_15V_20A = 5   # output 1 only, borrows output 2
RANGE_60V_6A  = 6   # output 1 only, borrows output 2
RANGE_120V_3A = 7   # output 1 only, borrows output 2
RANGE_5V5_3A  = 1   # output 3 only
RANGE_12V_1A5 = 2   # output 3 only

# Every setpoint is read back to confirm it landed, so the comparison has to
# allow for the PSU rounding to its own resolution: 1 mV / 1 mA on outputs 1
# and 2, but 10 mV / 10 mA on output 3. Rounding to nearest can only move a
# value by half a step, but the tolerance is a whole one: at exactly half a
# step the comparison is a floating point knife edge - 0.110 - 0.105 comes out
# as 0.0050000000000000044 - and one step is still far tighter than any
# setpoint that failed to take effect at all.
TOLERANCE_VOLTAGE_V = 0.01
TOLERANCE_CURRENT_A = 0.01

# Single shared socket (used by all functions); created by open_connection()
sock = None


def open_connection() -> None:
    """Open the LAN socket and take exclusive control of the PSU."""
    lan_open()
    # Unlock and hand the panel back however the run ends, error paths and
    # unhandled exceptions included; close_connection() is idempotent, so the
    # explicit call at the end of main() still works.
    atexit.register(close_connection)
    # Clear the status registers, including the power-on bit that would
    # otherwise be read as an error by the first scpi_set(). Deliberately no
    # *RST: the output settings on the PSU must be preserved.
    scpi_send(CMD_CLEAR_STATUS)
    # Lock the PSU's other interfaces (its web page, USB, RS232 and GPIB) out
    # of changing settings mid-run; the LAN socket is exclusive by itself, they
    # are not. IFLOCK replies with the resulting lock state instead of being a
    # silent set command, so it is checked against that reply, not scpi_set().
    state = scpi_query(CMD_LOCK_ON)
    if state == REPLY_LOCK_OWNED:
        return                    # lock taken, or already held by this socket
    else:
        # Either another interface holds it, or this one has been denied
        # control from the PSU's web page (Configure > interface privileges)
        print(f"Could not lock the PSU for exclusive control: "
              f"{CMD_LOCK_ON} returned {state!r}")
        sys.exit(1)


def lan_open() -> None:
    """Open the TCP socket to the PSU; print and exit on failure."""
    global sock
    print(f"Opening Power Supply at {LAN_ADDRESS}:{LAN_PORT}")

    try:
        sock = socket.create_connection((LAN_ADDRESS, LAN_PORT),
                                        TIMEOUT_CONNECT_S)
    except OSError as exc:
        # Typical causes: PSU powered off or on another subnet, wrong
        # LAN_ADDRESS, or its one control socket already held by another
        # program (the PSU refuses the connection outright).
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

    status = int(scpi_query(CMD_GET_EVENT_STAT))
    if not status & ESR_ERROR_MASK:
        return                    # command accepted
    else:
        faults = [text for bit, text in ESR_ERROR_BITS.items() if status & bit]
        # The execution error bit says only that something failed; the reason
        # is a code held in the separate execution error register
        if status & ESR_BIT_EXEC_ERROR:
            code = scpi_query(CMD_GET_EXEC_ERROR)
            faults.append(f"EER {code} - "
                          f"{ERROR_EXEC_TEXT.get(code, 'unrecognised code')}")
        print(f"Command {command} failed: {', '.join(faults)}")
        sys.exit(1)


def scpi_query_value(command: str) -> float:
    """Send a query with a numeric reply and return it as a float.

    Numeric replies come in three shapes: "V1 13.500" (keyword then value),
    "-0.008V" (value then unit) and "1" (value alone), so the last whitespace
    field is taken and any unit letter dropped; print and exit on anything
    that is not a number.
    """
    reply = scpi_query(command)
    try:
        return float(reply.split()[-1].rstrip("VA"))
    except (ValueError, IndexError):
        print(f"Reply to {command} was not numeric: {reply!r}")
        sys.exit(1)


def check_output(output: int) -> None:
    """Reject an output the PSU does not have; print and exit if invalid."""
    if output in OUTPUTS:
        return
    else:
        print(f"Output {output} does not exist: this PSU has outputs "
              f"{', '.join(str(number) for number in OUTPUTS)}")
        sys.exit(1)


def cmd_identify(print_results: bool = True) -> dict[str, str]:
    """Identify the PSU (*IDN?) and check it is the expected model.

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


def cmd_set_voltage(output: int, volts: float) -> float:
    """Set output <output>'s voltage setpoint, then read it back to confirm.

    Takes effect whether the output is on or off. The PSU rounds to its own
    resolution (1 mV on outputs 1 and 2, 10 mV on output 3) and rejects a
    value above the output's present range with execution error 100, so raise
    the range first where needed - see cmd_set_range(). The manual's "with
    verify" form (V<N>V) is deliberately not used: it waits for the output to
    reach the value, which never happens while the output is off.

    Returns the confirmed setpoint as a float so other functions can use it.
    """
    check_output(output)
    scpi_set(CMD_SET_VOLTAGE.format(output=output), f"{volts:.3f}")

    volts_get = cmd_get_voltage(output, False)
    if abs(volts_get - volts) <= TOLERANCE_VOLTAGE_V:
        print(f"Output {output} voltage set to {volts:.3f} V (confirmed)")
    else:
        print(f"Output {output} voltage set to {volts:.3f} V but reads back "
              f"{volts_get:.3f} V")
        sys.exit(1)
    return volts_get


def cmd_get_voltage(output: int, print_results: bool = True) -> float:
    """Read output <output>'s voltage setpoint, in volts.

    This is what the output is set to, not what it is delivering - see
    cmd_measure_voltage() for that. print_results is False when a caller is
    gathering several values and will print them on one line itself.
    """
    check_output(output)
    volts = scpi_query_value(CMD_GET_VOLTAGE_SET.format(output=output))
    if print_results:
        print(f"Output {output} voltage setpoint: {volts:.3f} V")
    return volts


def cmd_set_current(output: int, amps: float) -> float:
    """Set output <output>'s current limit, then read it back to confirm.

    Rounded and range-checked by the PSU as cmd_set_voltage() describes; the
    resolution is 1 mA on outputs 1 and 2, 10 mA on output 3.

    Returns the confirmed limit as a float so other functions can use it.
    """
    check_output(output)
    scpi_set(CMD_SET_CURRENT.format(output=output), f"{amps:.3f}")

    amps_get = cmd_get_current(output, False)
    if abs(amps_get - amps) <= TOLERANCE_CURRENT_A:
        print(f"Output {output} current limit set to {amps:.3f} A (confirmed)")
    else:
        print(f"Output {output} current limit set to {amps:.3f} A but reads "
              f"back {amps_get:.3f} A")
        sys.exit(1)
    return amps_get


def cmd_get_current(output: int, print_results: bool = True) -> float:
    """Read output <output>'s current limit, in amps.

    The limit the output will hold at, not the current it is delivering - see
    cmd_measure_current() for that.
    """
    check_output(output)
    amps = scpi_query_value(CMD_GET_CURRENT_SET.format(output=output))
    if print_results:
        print(f"Output {output} current limit: {amps:.3f} A")
    return amps


def format_range(output: int, range_index: int) -> str:
    """Render a range index with its limits, e.g. "1 (30 V / 6 A max)".

    Shared by everything that prints a range, so the wording stays the same
    whether it came from cmd_set_range(), cmd_get_range() or the combined
    cmd_get_settings() line.
    """
    limits = RANGE_LIMITS[output].get(range_index)
    if limits is None:
        return str(range_index)   # a range this file does not have listed
    else:
        return f"{range_index} ({limits[0]:g} V / {limits[1]:g} A max)"


def cmd_set_range(output: int, range_index: int) -> int:
    """Set output <output>'s voltage/current range, as listed in RANGE_LIMITS.

    A range change is refused with execution error 104 while more than 0.5 V
    is still present on output 1 or 2, so switch an output off and let it
    discharge before changing its range. Dropping to a lower range silently
    clamps the setpoints to the new maximum rather than refusing - 10 V on
    output 3 becomes 5.5 V - so set the range before the setpoints.

    Output 1's high ranges (4 to 7, up to 20 A or 120 V) use output 2's
    hardware and need output 2 off. While one of them is selected, output 2
    stops responding: its commands are refused with execution error 103 and
    its queries are answered with silence, so a query surfaces here as a read
    timeout rather than as the underlying error.

    Returns the confirmed range index as an int so other functions can use it.
    """
    check_output(output)
    if range_index not in RANGE_LIMITS[output]:
        print(f"Output {output} has no range {range_index}: its ranges are "
              f"{', '.join(str(index) for index in RANGE_LIMITS[output])}")
        sys.exit(1)

    scpi_set(CMD_SET_RANGE.format(output=output), str(range_index))
    index_get = cmd_get_range(output, False)
    if index_get == range_index:
        print(f"Output {output} range set to "
              f"{format_range(output, range_index)} (confirmed)")
    else:
        print(f"Output {output} range set to {range_index} but reads back "
              f"{index_get}")
        sys.exit(1)
    if output == 1 and range_index in RANGES_USING_OUTPUT2:
        print("  note: output 2 is unavailable until output 1 leaves this range")
    return index_get


def cmd_get_range(output: int, print_results: bool = True) -> int:
    """Read output <output>'s range index; RANGE_LIMITS says what it allows."""
    check_output(output)
    range_index = int(scpi_query_value(CMD_GET_RANGE.format(output=output)))
    if print_results:
        print(f"Output {output} range: {format_range(output, range_index)}")
    return range_index


def cmd_measure_voltage(output: int, print_results: bool = True) -> float:
    """Measure the voltage output <output> is delivering, as its meter shows.

    Reads near zero with the output off, rather than the setpoint.
    """
    check_output(output)
    volts = scpi_query_value(CMD_MEASURE_VOLTAGE.format(output=output))
    if print_results:
        print(f"Output {output} measured voltage: {volts:.3f} V")
    return volts


def cmd_measure_current(output: int, print_results: bool = True) -> float:
    """Measure the current output <output> is delivering, as its meter shows."""
    check_output(output)
    amps = scpi_query_value(CMD_MEASURE_CURRENT.format(output=output))
    if print_results:
        print(f"Output {output} measured current: {amps:.3f} A")
    return amps


def cmd_set_output_enable(output: int, on: bool) -> bool:
    """Enable or disable output <output>, then read it back to confirm.

    Enabling energises the terminals at once, with no ramp, at whatever
    setpoints and range are already in force - so set those first. Disabling
    leaves them untouched, ready to be enabled again. The manual's OPALL,
    which switches all three outputs on a configurable delay sequence, is
    deliberately not used: each output is switched explicitly here.

    Returns the confirmed output state as a bool so other functions can use it.
    """
    check_output(output)
    scpi_set(CMD_SET_OUTPUT_ENABLE.format(output=output),
             ARG_OUTPUT_ENABLE if on else ARG_OUTPUT_DISABLE)

    on_get = cmd_get_output_enable(output, False)
    if on_get == on:
        print(f"Output {output} switched {'ON' if on else 'OFF'} (confirmed)")
    else:
        print(f"Output {output} switched {'ON' if on else 'OFF'} but reads "
              f"back {'ON' if on_get else 'OFF'}")
        sys.exit(1)
    return on_get


def cmd_get_output_enable(output: int, print_results: bool = True) -> bool:
    """Query whether output <output> is enabled (on)."""
    check_output(output)
    on = bool(scpi_query_value(CMD_GET_OUTPUT_ENABLE.format(output=output)))
    if print_results:
        print(f"Output {output} state: {'ON' if on else 'OFF'}")
    return on


def cmd_get_settings(output: int,
                     print_results: bool = True) -> dict[str, float | int | bool]:
    """Read everything output <output> is set to, in one call.

    Gathers what cmd_get_voltage(), cmd_get_current(), cmd_get_range() and
    cmd_get_output_enable() each return, silencing their individual prints so
    the lot appears as a single line. Returns a dict so other functions can use
    the results, e.g. cmd_get_settings(1, False)["voltage_set_v"]; the values
    keep the types those functions return.
    """
    settings = {
        "voltage_set_v": cmd_get_voltage(output, False),
        "current_set_a": cmd_get_current(output, False),
        "range":         cmd_get_range(output, False),
        "output_enable": cmd_get_output_enable(output, False)}

    if print_results:
        print(f"Output {output} set to {settings['voltage_set_v']:.3f} V, "
              f"{settings['current_set_a']:.3f} A limit, range "
              f"{format_range(output, settings['range'])}, "
              f"{'ON' if settings['output_enable'] else 'OFF'}")
    return settings


def available_outputs() -> tuple[int, ...]:
    """The outputs that will answer, in order.

    Output 2 stops responding while output 1 holds one of the high ranges that
    borrow its hardware, and querying it would stall until the read timeout
    and then abort, so it is left out while that is the case.
    """
    if cmd_get_range(1, False) in RANGES_USING_OUTPUT2:
        return tuple(output for output in OUTPUTS if output != 2)
    else:
        return OUTPUTS


def close_connection() -> None:
    """Unlock the PSU, hand its front panel back and close the socket."""
    global sock
    if sock is not None:
        scpi_query(CMD_LOCK_OFF)  # reply is the resulting state, of no use here
        # Hand the keyboard back: any command puts the instrument into the
        # remote state, which locks its front panel until LOCAL is sent or its
        # LOCAL key is pressed. Not error-checked, as the check is itself a
        # command and would put the instrument straight back into remote.
        scpi_send(CMD_GO_LOCAL)
        sock.close()
        sock = None               # so a second close is a no-op, not an error
        print(f"{LAN_ADDRESS}:{LAN_PORT} closed")


def main() -> None:
    # Connect to the power supply and take exclusive control of it
    open_connection()

    # Confirm the power supply is alive and is the expected model
    cmd_identify()

    # Show what every available output is set to and what it is delivering
    for output in available_outputs():
        cmd_get_settings(output)
        cmd_measure_voltage(output)
        cmd_measure_current(output)

    # Exercise the set path without disturbing a supply that is in use: write
    # output 1's own setpoints straight back, so the commands and their status
    # checks are proven end to end while the values stay as they were.
    # Deliberately no range change (it would fail anyway with voltage still on
    # the terminals) and no cmd_set_output_enable(), which would switch a live
    # output off; call those directly when that is what you want.
    settings = cmd_get_settings(1, False)
    cmd_set_voltage(1, settings["voltage_set_v"])
    cmd_set_current(1, settings["current_set_a"])

    # The same values are readable one at a time, each printing its own line
    cmd_get_voltage(1)
    cmd_get_current(1)
    cmd_get_range(1)
    cmd_get_output_enable(1)

    # Done - unlock, return the front panel to the user, release the socket
    close_connection()


if __name__ == "__main__":
    main()
