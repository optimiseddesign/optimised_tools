"""Control a Kikusui PLZ405W electronic DC load over LAN.

Drives the load input remotely - operation mode, current/voltage ranges and
setpoints, and input enable - so a supply or converter under test can be
loaded without touching the front panel. main()'s hardware test leaves the
input-enable calls commented out, so running this file never draws current on
its own; call cmd_set_input_enable(True) from another script to do that.

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
    Numeric replies carry an explicit sign, e.g. the empty error queue reads
    back as '+0,"No error"'.
  - Unlike a COM port, the load's LAN interface is not exclusive: it accepts
    a second SCPI-RAW session alongside this one, so a run can be disturbed
    by another program talking to the load at the same time.
  - The error paths below exit rather than return, so open_connection()
    registers close_connection() with atexit: the session is released however
    a run ends, an unhandled exception included.
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

import atexit
import sys

import pyvisa

# --- Connection configuration -------------------------------------------------
LAN_ADDRESS     = "192.168.10.34"  # load IP: its LAN settings menu / web page
LAN_RESOURCE    = f"TCPIP::{LAN_ADDRESS}::5025::SOCKET"  # SCPI-RAW, port 5025
VISA_BACKEND    = "@py"           # pyvisa-py, pure Python; "" uses vendor VISA
TIMEOUT_OPEN_S  = 5.0             # TCP connect wait (pyvisa-py defaults to 10 s)
TIMEOUT_READ_S  = 2.0             # per-reply read timeout
TX_EOL          = "\n"            # commands sent with bare LF
RX_EOL          = "\n"            # reply lines terminated with LF
MS_PER_S        = 1000            # PyVISA timeouts are in milliseconds

# --- Protocol constants -------------------------------------------------------
CMD_CLEAR_STATUS = "*CLS"             # clear status registers and error queue
CMD_GET_IDENTITY = "*IDN?"            # manufacturer,model,serial,firmware
CMD_GET_ERROR    = "SYSTem:ERRor?"    # oldest error in the queue, code first
CMD_SET_LOCAL    = "SYSTem:COMMunicate:RLSTate LOCal"  # release the front panel
CMD_SET_CURRENT  = "SOURce:CURRent"   # set the CC mode current level, A
CMD_GET_CURRENT  = "SOURce:CURRent?"  # query the current setpoint, A
CMD_SET_VOLTAGE  = "SOURce:VOLTage"   # set the CV mode voltage level, V
CMD_GET_VOLTAGE  = "SOURce:VOLTage?"  # query the voltage setpoint, V
CMD_SET_MODE     = "SOURce:FUNCtion"  # set the operation mode
CMD_GET_MODE     = "SOURce:FUNCtion?" # query the operation mode
CMD_SET_CURRENT_RANGE = "SOURce:CURRent:RANGe"   # set the CC/CR/CP range
CMD_GET_CURRENT_RANGE = "SOURce:CURRent:RANGe?"  # query the CC/CR/CP range
CMD_SET_VOLTAGE_RANGE = "SOURce:VOLTage:RANGe"   # set the CV range
CMD_GET_VOLTAGE_RANGE = "SOURce:VOLTage:RANGe?"  # query the CV range
CMD_MEASURE_VOLTAGE = "MEASure:VOLTage?"  # actual load terminal voltage, V
CMD_MEASURE_CURRENT = "MEASure:CURRent?"  # actual load terminal current, A
CMD_SET_INPUT_ENABLE = "INPut"        # enable/disable the load input
CMD_GET_INPUT_ENABLE = "INPut?"       # query whether the load input is enabled
REPLY_NO_ERROR   = 0                  # error code when the queue is empty
CURRENT_TOLERANCE_A = 0.002           # one whole step of the coarsest range -
                                      # 2 mA (H), 0.2 mA (M), 0.02 mA (L); half
                                      # a step lands on a float knife edge
VOLTAGE_TOLERANCE_V = 0.005           # one whole step: 5 mV (H), 0.5 mV (L)
IDENTITY_FIELDS  = 4                  # *IDN? reply has four comma-separated fields
MODEL_EXPECTED   = "PLZ405W"          # the PLZ-5W series shares a command set but
                                      # not its ratings - refuse the wrong model

# --- Operation mode -------------------------------------------------------
MODE_CC = "CC"                        # constant current
MODE_CR = "CR"                        # constant resistance
MODE_CV = "CV"                        # constant voltage
MODE_CP = "CP"                        # constant power
                                      # ARB (I-V characteristics map) exists
                                      # on the load but is out of scope here

# --- Current/voltage ranges -------------------------------------------------
# Range selects a maximum level, not an exact value - lower ranges trade
# headroom for resolution. Rated maximum for each:
#   current (CC/CR/CP): LOW 0.8 A, MED 8 A, HIGH 80 A
#   voltage (CV):        LOW 15 V,           HIGH 150 V (no MED)
RANGE_LOW  = "LOW"                    # current 0.8 A; voltage 15 V
RANGE_MED  = "MED"                    # current 8 A (current axis only)
RANGE_HIGH = "HIGH"                   # current 80 A; voltage 150 V

# The same three named by what they allow, for callers that would rather say
# the rating than the size. Worth having because LOW means 0.8 A to
# cmd_set_current_range() but 15 V to cmd_set_voltage_range().
RANGE_0A8  = RANGE_LOW
RANGE_8A   = RANGE_MED
RANGE_80A  = RANGE_HIGH
RANGE_15V  = RANGE_LOW
RANGE_150V = RANGE_HIGH

# Single shared session objects (used by all functions); created by
# open_connection(), both None until then
resource_manager = None           # PyVISA resource manager, owns the backend
dcload = None                     # PyVISA session to the load


def open_connection() -> None:
    """Open the LAN connection and put the load in a known state."""
    lan_open()
    # Release the session however the run ends, error paths and unhandled
    # exceptions included; close_connection() is idempotent, so the explicit
    # call at the end of main() still works.
    atexit.register(close_connection)
    # Clear the status/error queue - no *RST, since that would reset the
    # protection settings (OCP/OPP/UVP) and panel setup. Using scpi_set()
    # here also confirms the link works both ways before anything else runs.
    scpi_set(CMD_CLEAR_STATUS)


def lan_open() -> None:
    """Open the PyVISA LAN session; print and exit on failure."""
    global resource_manager, dcload
    print(f"Opening DC load at {LAN_RESOURCE}")

    try:
        resource_manager = pyvisa.ResourceManager(VISA_BACKEND)
        dcload = resource_manager.open_resource(
            LAN_RESOURCE, open_timeout=int(TIMEOUT_OPEN_S * MS_PER_S))
    except Exception as exc:
        # Typical causes: load off, LAN unplugged, or wrong LAN_ADDRESS.
        # Caught broadly because pyvisa-py raises a bare Exception here, not
        # a pyvisa.Error.
        print(f"Could not open {LAN_RESOURCE}: {exc}")
        sys.exit(1)
    # A raw socket carries no message boundaries, so PyVISA must be told the
    # terminators; VXI-11 supplies its own but accepts these unchanged
    dcload.write_termination = TX_EOL
    dcload.read_termination = RX_EOL
    dcload.timeout = int(TIMEOUT_READ_S * MS_PER_S)


def scpi_send(command: str) -> None:
    """Send a SCPI command that produces no reply; print and exit on failure."""
    try:
        dcload.write(command)
    except (pyvisa.Error, OSError) as exc:
        # A dropped link (load off/unplugged) raises OSError, not
        # pyvisa.Error - both are caught
        print(f"Could not send {command}: {exc}")
        sys.exit(1)


def scpi_query(command: str, timeout_s: float = TIMEOUT_READ_S) -> str:
    """Send a SCPI query and return its one-line reply; print and exit on timeout.

    timeout_s allows slow queries to wait longer. It applies per query and is
    set on every call.
    """
    dcload.timeout = int(timeout_s * MS_PER_S)
    try:
        return dcload.query(command).strip()
    except (pyvisa.Error, OSError) as exc:
        # pyvisa.Error covers the timeout, OSError a dropped link (see
        # scpi_send) - a query can hit either since it writes then reads
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
    # Reply is code,"description", code signed (e.g. '+0,"No error"') -
    # compare as int so "+0" matches and a stray "0" elsewhere doesn't.
    if int(error.split(",")[0]) == REPLY_NO_ERROR:
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

    if info["model"] == MODEL_EXPECTED:
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


def cmd_set_current(amps: float) -> float:
    """Set the CC mode current level, then read back the setpoint to confirm.

    Only sets the level - mode and input enable are untouched.

    Returns the confirmed setpoint as a float so other functions can use it.
    """
    scpi_set(CMD_SET_CURRENT, f"{amps:.3f}")

    amps_get = cmd_get_current(print_results=False)

    if abs(amps_get - amps) > CURRENT_TOLERANCE_A:
        # Readback should match to the 3 d.p. sent; a real mismatch means the
        # load silently clamped or ignored the value
        print(f"Set current {amps:.3f} A but readback is {amps_get:.3f} A")
        sys.exit(1)
    else:
        print(f"Set current: {amps:.3f} A ({amps_get:.3f} A confirmed)")
        return amps_get


def cmd_get_current(print_results: bool = True) -> float:
    """Query the CC mode current setpoint - the commanded level, not the
    measured load current (that is cmd_measure_current()).

    Returns the setpoint as a float so other functions can use it.
    """
    current_get = scpi_query(CMD_GET_CURRENT)
    try:
        current_get_amps = float(current_get)
    except ValueError:
        print(f"Get current returned non-numeric readback: {current_get!r}")
        sys.exit(1)
    if print_results:
        print(f"Get current: {current_get_amps:.3f} A")
    return current_get_amps


def cmd_set_voltage(volts: float) -> float:
    """Set the CV mode voltage level, then read back the setpoint to confirm.

    Only sets the level - mode and input enable are untouched.

    Returns the confirmed setpoint as a float so other functions can use it.
    """
    scpi_set(CMD_SET_VOLTAGE, f"{volts:.3f}")

    volts_get = cmd_get_voltage(print_results=False)

    if abs(volts_get - volts) > VOLTAGE_TOLERANCE_V:
        # Readback should match to the 3 d.p. sent; a real mismatch means the
        # load silently clamped or ignored the value
        print(f"Set voltage {volts:.3f} V but readback is {volts_get:.3f} V")
        sys.exit(1)
    else:
        print(f"Set voltage: {volts:.3f} V ({volts_get:.3f} V confirmed)")
        return volts_get


def cmd_get_voltage(print_results: bool = True) -> float:
    """Query the CV mode voltage setpoint - the commanded level, not the
    measured load voltage (that is cmd_measure_voltage()).

    Returns the setpoint as a float so other functions can use it.
    """
    voltage_get = scpi_query(CMD_GET_VOLTAGE)
    try:
        voltage_get_v = float(voltage_get)
    except ValueError:
        print(f"Get voltage returned non-numeric readback: {voltage_get!r}")
        sys.exit(1)
    if print_results:
        print(f"Get voltage: {voltage_get_v:.3f} V")
    return voltage_get_v


def require_input_disabled(action: str) -> None:
    """Exit if the load input is enabled; call before changing mode/range.

    The load itself refuses a mode/range change while the input is on (SCPI
    error +102, "Setting conflicts") - this fails the same way, just earlier
    and with a clearer message.
    """
    if cmd_get_input_enable(print_results=False):
        print(f"Cannot change {action} while the load input is enabled - "
              f"call cmd_set_input_enable(False) first")
        sys.exit(1)


def cmd_set_mode(mode: str) -> str:
    """Set the operation mode, then read it back to confirm.

    mode is one of MODE_CC/CR/CV/CP (their values, e.g. "CC" - not the
    names). Requires the load input to be disabled first.

    Returns the confirmed mode as a string so other functions can use it.
    """
    require_input_disabled("mode")
    scpi_set(CMD_SET_MODE, mode)

    mode_get = cmd_get_mode(print_results=False)

    if mode_get != mode:
        print(f"Set mode {mode} but readback is {mode_get}")
        sys.exit(1)
    else:
        print(f"Set mode: {mode} (confirmed)")
        return mode_get


def cmd_get_mode(print_results: bool = True) -> str:
    """Query the operation mode.

    Returns the load's code as a string, e.g. "CC" (see MODE_CC/CR/CV/CP).
    """
    mode_get = scpi_query(CMD_GET_MODE)
    if print_results:
        print(f"Get mode: {mode_get}")
    return mode_get


def cmd_set_current_range(range_: str) -> str:
    """Set the CC/CR/CP mode current range, then read it back to confirm.

    range_ is RANGE_LOW/MED/HIGH (see their ratings above). Requires the load
    input to be disabled first.

    Returns the confirmed range as a string so other functions can use it.
    """
    require_input_disabled("current range")
    scpi_set(CMD_SET_CURRENT_RANGE, range_)

    range_get = cmd_get_current_range(print_results=False)

    if range_get != range_:
        print(f"Set current range {range_} but readback is {range_get}")
        sys.exit(1)
    else:
        print(f"Set current range: {range_} (confirmed)")
        return range_get


def cmd_get_current_range(print_results: bool = True) -> str:
    """Query the CC/CR/CP mode current range.

    Returns the load's string, e.g. "LOW" (see RANGE_LOW/MED/HIGH above).
    """
    range_get = scpi_query(CMD_GET_CURRENT_RANGE)
    if print_results:
        print(f"Get current range: {range_get}")
    return range_get


def cmd_set_voltage_range(range_: str) -> str:
    """Set the CV mode voltage range, then read it back to confirm.

    range_ is RANGE_LOW or RANGE_HIGH (no RANGE_MED for voltage). Requires
    the load input to be disabled first.

    Returns the confirmed range as a string so other functions can use it.
    """
    require_input_disabled("voltage range")
    scpi_set(CMD_SET_VOLTAGE_RANGE, range_)

    range_get = cmd_get_voltage_range(print_results=False)

    if range_get != range_:
        print(f"Set voltage range {range_} but readback is {range_get}")
        sys.exit(1)
    else:
        print(f"Set voltage range: {range_} (confirmed)")
        return range_get


def cmd_get_voltage_range(print_results: bool = True) -> str:
    """Query the CV mode voltage range.

    Returns the load's string, e.g. "LOW" (see RANGE_LOW/RANGE_HIGH above).
    """
    range_get = scpi_query(CMD_GET_VOLTAGE_RANGE)
    if print_results:
        print(f"Get voltage range: {range_get}")
    return range_get


def cmd_measure_voltage() -> float:
    """Measure the actual voltage at the load's input terminals.

    A live measurement, not a setpoint readback - valid whether or not the
    input is enabled.

    Returns the measured voltage as a float so other functions can use it.
    """
    voltage = scpi_query(CMD_MEASURE_VOLTAGE)
    try:
        voltage_v = float(voltage)
    except ValueError:
        print(f"Measure voltage returned non-numeric value: {voltage!r}")
        sys.exit(1)
    print(f"Measured voltage: {voltage_v:.3f} V")
    return voltage_v


def cmd_measure_current() -> float:
    """Measure the actual current through the load's input terminals.

    A live measurement, not a setpoint readback - valid whether or not the
    input is enabled.

    Returns the measured current as a float so other functions can use it.
    """
    current = scpi_query(CMD_MEASURE_CURRENT)
    try:
        current_a = float(current)
    except ValueError:
        print(f"Measure current returned non-numeric value: {current!r}")
        sys.exit(1)
    print(f"Measured current: {current_a:.3f} A")
    return current_a


def cmd_set_input_enable(on: bool) -> bool:
    """Enable or disable the load input, then read it back to confirm.

    Applies the current mode/range/level to whatever is on the terminals -
    call this last, once those are set as intended.

    Returns the confirmed input state as a bool so other functions can use it.
    """
    scpi_set(CMD_SET_INPUT_ENABLE, "1" if on else "0")

    on_get = cmd_get_input_enable(print_results=False)

    if on_get != on:
        print(f"Set input enable {'ON' if on else 'OFF'} but readback is "
              f"{'ON' if on_get else 'OFF'}")
        sys.exit(1)
    else:
        print(f"Set input enable: {'ON' if on else 'OFF'} (confirmed)")
        return on_get


def cmd_get_input_enable(print_results: bool = True) -> bool:
    """Query whether the load input is enabled (on).

    Returns the input state as a bool so other functions can use it.
    """
    input_get = scpi_query(CMD_GET_INPUT_ENABLE)
    if input_get not in ("0", "1"):
        # Not treated as off: require_input_disabled() would then allow a
        # mode/range change while the input is actually on
        print(f"Get input enable returned unexpected reply: {input_get!r}")
        sys.exit(1)
    on_get = input_get == "1"
    if print_results:
        print(f"Get input enable: {'ON' if on_get else 'OFF'}")
    return on_get


def close_connection() -> None:
    global dcload
    # LOCal is a no-op over this raw-socket transport (no REN line to
    # assert REMote in the first place) but is kept for when LAN_RESOURCE
    # switches to VXI-11/HiSLIP, which do assert it.
    # resource_manager is a shared singleton per backend - closing it would
    # break any other PyVISA session in the process, so only the resource
    # itself is closed here.
    if dcload is not None:
        scpi_send(CMD_SET_LOCAL)
        dcload.close()
        dcload = None             # so a second close is a no-op, not an error
        print(f"{LAN_RESOURCE} closed")


def main() -> None:
    # Connect to the load
    open_connection()

    # Confirm the load is alive and is the expected model
    cmd_identify()

    # Query the load without setting anything
    cmd_get_mode()
    cmd_get_current_range()

    # Get current and voltage setpoints
    cmd_get_current(True)
    cmd_get_voltage(True)

    # Measure the terminal voltage/current with the input still disabled
    cmd_measure_voltage()
    cmd_measure_current()

    # Enabling the input draws a real load from whatever is on the
    # terminals - left commented out so running this file never does that
    # unintentionally. Uncomment to exercise the enable/disable path.
    
    #cmd_set_input_enable(True)
    #cmd_measure_voltage()
    #cmd_measure_current()
    #cmd_set_input_enable(False)

    # Done - release the connection
    close_connection()


if __name__ == "__main__":
    main()
