"""Control a Rohde & Schwarz RTB2004 oscilloscope over LAN or USB virtual COM port.

Drives the Bode plot application (option RTB-K36) remotely: start a sweep,
wait for completion, fetch the frequency/gain/phase data, and compute gain
and phase margins. Signal configuration (input/output channels, generator
amplitude, sweep range etc.) is assumed to be already set up on the scope.

Connection:
  - The same SCPI text works over either transport, so all instrument I/O is
    confined to open_connection(), close_connection(), scpi_send(),
    scpi_query() and scpi_query_block(); the command functions and the margin
    maths never see the transport. Set CONNECTION to choose - nothing else
    changes between LAN and USB.
  - LAN (default): the RsInstrument package talking raw socket to port 5025.
    Its SelectVisa='socket' mode is a plain TCP implementation, so no VISA
    installation is needed; with VISA installed, setting LAN_RESOURCE to
    "TCPIP::<ip>::INSTR" and LAN_OPTIONS to "" switches to VXI-11 instead.
    Set LAN_ADDRESS to the scope's IP address, shown (and configured, DHCP
    or static) under Setup > Interface > Ethernet on the scope.
  - USB VCP: scope set to USB VCP mode (Setup > Interface > USB > Parameter >
    USB VCP); it appears as a virtual COM port carrying plain ASCII SCPI
    text.
  - Either transport serves one remote client at a time, so close other scope
    software before a run (the scope's own front panel stays usable). The two
    fail differently: a COM port is exclusive on Windows, so a second program
    holding it fails the open outright, whereas the SCPI socket accepts the
    second TCP connection and then never answers it - which surfaces as a
    query timeout, not a connection error (verified on FW 02.400).
  - Commands are ASCII lines terminated with LF; replies are LF-terminated.
  - SCPI commands from the R&S RTB2 user manual v14 (doc 1333.1611.02),
    Bode plot remote commands chapter 16.8.7. LAN usage follows the official
    R&S RsInstrument example for the RTB2000 (github.com/Rohde-Schwarz/
    Examples, Oscilloscopes/Python/RsInstrument).

Requires: RsInstrument for LAN, pyserial for USB VCP - only the selected
transport's package is imported (pip install RsInstrument pyserial).
Python 3.13.

Copyright Optimised Product Design Ltd 2026. Available for public use
(copyright reserved) - see repository README; use at your own risk.
"""

import csv
import math
import sys
import time

# --- Connection configuration -------------------------------------------------
CONNECTION_LAN  = "LAN"           # RsInstrument over Ethernet (see lan_open)
CONNECTION_USB  = "USB"           # pyserial over the USB VCP (see vcp_open)
CONNECTION      = CONNECTION_LAN  # transport this run uses - the only switch
TIMEOUT_READ_S  = 2.0             # per-reply read timeout, either transport

# LAN transport (RsInstrument)
LAN_ADDRESS     = "192.168.10.99"  # scope IP: Setup > Interface > Ethernet
LAN_RESOURCE    = f"TCPIP::{LAN_ADDRESS}::5025::SOCKET"  # raw socket port 5025
LAN_OPTIONS     = "SelectVisa='socket'"  # RsInstrument's own TCP, no VISA needed
LAN_MIN_VERSION = "1.53.0"        # RsInstrument release the calls below expect
MS_PER_S        = 1000            # RsInstrument timeouts are in milliseconds

# USB VCP transport (pyserial)
COM_PORT        = "COM23"
BAUD_RATE       = 115200          # VCP ignores UART settings, but set anyway
DATA_BITS       = 8               # serial.EIGHTBITS
PARITY          = "N"             # serial.PARITY_NONE
STOP_BITS       = 1               # serial.STOPBITS_ONE
TIMEOUT_WRITE_S = 1.0
TX_EOL          = b"\n"           # commands sent with bare LF
RX_EOL          = b"\n"           # reply lines terminated with LF

# Import only the selected transport's package, so a LAN-only machine needs no
# pyserial and a USB-only machine needs no RsInstrument
if CONNECTION == CONNECTION_LAN:
    from RsInstrument import RsInstrument, RsInstrException
else:
    import serial

# --- Protocol constants -------------------------------------------------------
CMD_CLEAR_STATUS   = "*CLS"           # clear status registers and error queue
CMD_GET_IDENTITY   = "*IDN?"          # manufacturer,model,serial,firmware
CMD_GET_OPTIONS    = "*OPT?"          # installed options, comma-separated
CMD_GET_ERROR      = "SYSTem:ERRor?"  # oldest error in the queue, code first
CMD_FORMAT_ASCII   = "FORMat ASC"     # data queries reply in ASCII, comma-separated
CMD_BODE_ENABLE_ON = "BPLot:ENABle ON"  # opens the Bode plot application
CMD_BODE_RUN       = "BPLot:STATe RUN"  # starts a Bode sweep
CMD_BODE_GET_STATE = "BPLot:STATe?"   # sweep state: RUN or STOP
CMD_BODE_GET_FREQ  = "BPLot:FREQuency:DATA?"  # comma-separated frequencies, Hz
CMD_BODE_GET_GAIN  = "BPLot:GAIN:DATA?"       # comma-separated gain values, dB
CMD_BODE_GET_PHASE = "BPLot:PHASe:DATA?"      # comma-separated phase values, deg
# Marker commands; {n} is the marker number, 1 or 2
CMD_BODE_MARKER_FREQ     = "BPLot:MARKer{n}:FREQuency"   # set position, Hz
                                                         # (snaps to a sample)
CMD_BODE_GET_MARK_FREQ   = "BPLot:MARKer{n}:FREQuency?"  # actual (snapped) one
CMD_BODE_GET_MARK_GAIN   = "BPLot:MARKer{n}:GAIN?"       # gain at marker, dB
CMD_BODE_GET_MARK_PHASE  = "BPLot:MARKer{n}:PHASe?"      # phase at marker, deg
CMD_SCREENSHOT_FORMAT    = "HCOPy:LANGuage PNG"  # screenshot image format
CMD_GET_SCREENSHOT       = "HCOPy:DATA?"      # screenshot as 488.2 block data
REPLY_NO_ERROR     = 0                # error code when the queue is empty
REPLY_BODE_STOP    = "STOP"           # BPLot:STATe? reply once the sweep finished
OPTION_BODE        = "K36"            # Bode plot application option (RTB-K36)
IDENTITY_FIELDS    = 4                # *IDN? reply has four comma-separated fields
MODEL_EXPECTED     = "RTB2"           # family prefix - the whole RTB2000 series
                                      # shares the Bode plot command set
TIMEOUT_BODE_S     = 120              # max sweep time (low start freqs are slow).
                                      # 200 Hz start, 50 points/decade = 185
                                      # points measured approx 80s, keep
                                      # plenty of headroom, as a trip here
                                      # aborts a whole orchestrator run.
TIMEOUT_DATA_S     = 10.0             # data arrays can be tens of kB of ASCII
TIMEOUT_SCREEN_S   = 15.0             # screenshot PNG is tens of kB of binary
POLL_BODE_S        = 1.0              # interval between sweep-state polls

# --- Output configuration ------------------------------------------------------
CSV_PATH        = "bode_result.csv"   # standalone-test outputs; the sweep
SCREENSHOT_PATH = "bode_result.png"   # orchestrator will own file naming later

# --- Margin computation configuration -------------------------------------------
MARGIN_FREQ_MIN_HZ  = 500.0           # bounds on the crossover search; tighten
MARGIN_FREQ_MAX_HZ  = 200e3           # to exclude the noisy sweep extremes
GAIN_CROSSOVER_DB   = 0.0             # gain crossover threshold
PHASE_CROSSOVER_DEG = 0.0             # instability point: positive reinforcement
PHASE_WRAP_STEP_DEG = 180.0           # larger steps between adjacent points are
                                      # the display wrapping, not crossings

# Single shared session object for the transport in use (used by all
# functions); created by open_connection(), the other stays None
instrument = None                 # RsInstrument session, LAN
ser = None                        # serial.Serial port, USB VCP


def open_connection() -> None:
    """Open the configured connection and put the scope in a known state."""
    if CONNECTION == CONNECTION_LAN:
        lan_open()
    else:
        vcp_open()
    # Clear the scope's status/error queue. Deliberately no *RST: the
    # signal configuration on the scope must be preserved.
    scpi_send(CMD_CLEAR_STATUS)
    # Other software may leave data format as binary (REAL,32); pin it to
    # ASCII so replies parse as text. Affects remote transfers only.
    scpi_set(CMD_FORMAT_ASCII)


def lan_open() -> None:
    """Open the RsInstrument LAN session; print and exit on failure."""
    global instrument
    print(f"Opening Oscilloscope at {LAN_RESOURCE}")

    try:
        RsInstrument.assert_minimum_version(LAN_MIN_VERSION)
        # id_query reads *IDN? to confirm the scope answers; reset stays off,
        # as *RST would discard the signal configuration set up on the scope
        instrument = RsInstrument(LAN_RESOURCE, id_query=True, reset=False,
                                  options=LAN_OPTIONS)
    except Exception as exc:
        # Deliberately broad: opening a session raises three unrelated
        # families - RsInstrException for a malformed resource string, a bare
        # OSError (ConnectionRefusedError, TimeoutError) straight from the
        # socket when the scope does not answer, and a plain Exception from
        # assert_minimum_version. Typical causes: scope powered off or on
        # another subnet, wrong LAN_ADDRESS, its LAN interface not yet up
        # after a cold start, or RsInstrument too old.
        print(f"Could not open {LAN_RESOURCE}: {exc}")
        sys.exit(1)
    # RsInstrument queries SYSTem:ERRor? after every write by default and
    # raises on an error; this file checks the queue itself in scpi_set() and
    # prints and exits instead, so switch the duplicate check off.
    instrument.instrument_status_checking = False


def vcp_open() -> None:
    """Configure and open the USB virtual COM port; print and exit on failure."""
    global ser
    ser = serial.Serial()
    ser.port = COM_PORT
    ser.baudrate = BAUD_RATE
    ser.bytesize = DATA_BITS
    ser.parity = PARITY
    ser.stopbits = STOP_BITS
    ser.timeout = TIMEOUT_READ_S
    ser.write_timeout = TIMEOUT_WRITE_S

    print(f"Opening Oscilloscope {COM_PORT} at {BAUD_RATE} baud")

    try:
        ser.open()                # pyserial asserts DTR/RTS, like a terminal
    except serial.SerialException as exc:
        # Typical causes: scope not in USB VCP mode / wrong COM number, or the
        # port is held open by another program (exclusive on Windows).
        print(f"Could not open {COM_PORT}: {exc}")
        sys.exit(1)
    ser.reset_input_buffer()      # purge stale bytes from a previous session


def scpi_send(command: str) -> None:
    """Send a SCPI command that produces no reply; print and exit on failure."""
    try:
        if CONNECTION == CONNECTION_LAN:
            instrument.write_str(command)
        else:
            ser.write(command.encode("ascii") + TX_EOL)
    except Exception as exc:
        # Broad, as in lan_open(): the transports raise unrelated families
        # (RsInstrException or OSError on LAN, serial.SerialException on USB)
        # and only the selected transport's package is imported
        print(f"Could not send {command}: {exc}")
        sys.exit(1)


def scpi_query(command: str, timeout_s: float = TIMEOUT_READ_S) -> str:
    """Send a SCPI query and return its one-line reply; print and exit on timeout.

    timeout_s allows slow queries (e.g. during a running Bode sweep) to wait
    longer. It applies per query and is set on every call.
    """
    if CONNECTION == CONNECTION_LAN:
        instrument.visa_timeout = int(timeout_s * MS_PER_S)
        try:
            return instrument.query_str(command)  # reply trimmed of its LF
        except (RsInstrException, OSError) as exc:
            # OSError as well: a dropped link surfaces straight from the socket
            print(f"Scope did not reply to {command}: {exc}")
            sys.exit(1)
    else:
        ser.timeout = timeout_s
        scpi_send(command)
        raw = ser.read_until(RX_EOL)  # returns whatever arrived on timeout
        if raw.endswith(RX_EOL):
            return raw.decode("ascii", errors="replace").strip()
        else:
            print(f"Scope did not reply to {command}: got {raw!r}")
            sys.exit(1)


def scpi_query_block(command: str, timeout_s: float = TIMEOUT_READ_S) -> bytes:
    """Send a query whose reply is an IEEE 488.2 definite-length binary block.

    Block format: '#', one digit giving the length of the byte count, the
    byte count, then the raw bytes, then LF. Needed because binary data
    (e.g. a PNG screenshot) contains stray LF bytes that break the
    line-based scpi_query(); print and exit on a malformed or short reply.
    RsInstrument parses the block itself, so the LAN branch is a single call.
    """
    if CONNECTION == CONNECTION_LAN:
        instrument.visa_timeout = int(timeout_s * MS_PER_S)
        try:
            return instrument.query_bin_block(command)
        except (RsInstrException, OSError) as exc:
            print(f"Scope did not return block data for {command}: {exc}")
            sys.exit(1)
    else:
        ser.timeout = timeout_s   # applies to each read below
        scpi_send(command)
        header = ser.read(2)      # b'#' then the digit count of the length
        if not (header.startswith(b"#") and header[1:].isdigit()):
            print(f"Scope did not return block data for {command}: got {header!r}")
            sys.exit(1)
        length = ser.read(int(header[1:]))
        if not length.isdigit():
            print(f"Bad block length for {command}: got {length!r}")
            sys.exit(1)
        payload = ser.read(int(length))
        ser.read_until(RX_EOL)    # consume the trailing terminator
        if len(payload) == int(length):
            return payload
        else:
            print(f"Block data for {command} incomplete: got {len(payload)} of "
                  f"{length.decode()} bytes")
            sys.exit(1)


def scpi_set(command: str, argument: str = "") -> None:
    """Send a set command, then check the scope's error queue.

    Set commands produce no reply, so errors (e.g. invalid parameter) would
    otherwise go unnoticed; print and exit on error. argument, if given, is
    appended after a space (e.g. a marker frequency).
    """
    if argument:
        command = command + " " + argument
    scpi_send(command)
    error = scpi_query(CMD_GET_ERROR)
    # Reply is code,"description" - compare the code as an int so a stray "0"
    # in the description cannot match
    if int(error.split(",")[0]) == REPLY_NO_ERROR:
        return                    # command accepted
    else:
        print(f"Command {command} failed: {error}")
        sys.exit(1)


def scpi_query_value(command: str) -> float:
    """Send a query with a numeric reply and return it as a float.

    Replies are plain numbers, e.g. "1.2345E+03"; print and exit on anything
    else.
    """
    reply = scpi_query(command)
    try:
        return float(reply)
    except ValueError:
        print(f"Reply to {command} was not numeric: {reply!r}")
        sys.exit(1)


def cmd_identify(print_results: bool = True) -> dict[str, str]:
    """Identify the scope (*IDN?), check the model and the Bode plot option.

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

    if not info["model"].startswith(MODEL_EXPECTED):
        print(f"Expected an RTB2000 series model but found {info['model']!r}: "
              f"its remote command set may differ")
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


def cmd_bode_run(timeout_s: float = TIMEOUT_BODE_S) -> None:
    """Run a Bode plot sweep and wait until it completes.

    Sweep duration depends on the frequency range, points per decade and
    measurement delay configured on the scope, so the default timeout is
    generous; print and exit if the sweep has not finished in time.
    """
    scpi_set(CMD_BODE_ENABLE_ON)  # no-op if the Bode app is already open
    scpi_set(CMD_BODE_RUN)
    print(f"Bode sweep running (timeout {timeout_s:.0f} s)")

    start = time.monotonic()
    while time.monotonic() - start < timeout_s:
        time.sleep(POLL_BODE_S)       # sleep first so the sweep has begun
        state = scpi_query(CMD_BODE_GET_STATE)
        if state == REPLY_BODE_STOP:
            print(f"Bode sweep complete after {time.monotonic() - start:.1f} s")
            return
    print(f"Bode sweep did not complete within {timeout_s:.0f} s")
    sys.exit(1)


def cmd_bode_get_data(print_results: bool = True) -> dict[str, list[float]]:
    """Fetch the completed Bode plot: frequency, gain and phase arrays.

    Returns a dict of equal-length lists so other functions can use the
    results, e.g. cmd_bode_get_data(print_results=False)["gain_db"].
    """
    frequency = scpi_query(CMD_BODE_GET_FREQ, TIMEOUT_DATA_S)
    gain = scpi_query(CMD_BODE_GET_GAIN, TIMEOUT_DATA_S)
    phase = scpi_query(CMD_BODE_GET_PHASE, TIMEOUT_DATA_S)
    try:
        data = {"frequency_hz": [float(value) for value in frequency.split(",")],
                "gain_db":      [float(value) for value in gain.split(",")],
                "phase_deg":    [float(value) for value in phase.split(",")]}
    except ValueError:
        print("Bode data contained non-numeric value(s) - has a sweep completed?")
        sys.exit(1)

    n_points = len(data["frequency_hz"])
    if len(data["gain_db"]) == n_points and len(data["phase_deg"]) == n_points:
        if print_results:
            print(f"Bode data: {n_points} points, "
                  f"{data['frequency_hz'][0]:.6g} Hz to "
                  f"{data['frequency_hz'][-1]:.6g} Hz")
    else:
        print(f"Bode data length mismatch: {n_points} frequency, "
              f"{len(data['gain_db'])} gain, {len(data['phase_deg'])} phase")
        sys.exit(1)
    return data


def find_crossings(frequency_hz: list[float],
                   values: list[float],
                   threshold: float,
                   other: list[float],
                   max_step: float = math.inf) -> list[tuple[float, float]]:
    """Find every frequency at which values crosses the threshold.

    Samples never land exactly on the zero threshold, so each crossing is a pair
    of samples straddling it, located by linear interpolation in
    log10(freq). Returns one pair of values per crossing (frequency, value)
    - e.g. the phase at a gain crossover.

    Sample pairs stepping by more than max_step are skipped: a wrapped phase
    display jumps e.g. -179 to +179 deg, which passes the threshold
    numerically but is not a real crossing.
    """
    crossings = []
    for i in range(len(values) - 1):
        start = values[i] - threshold
        end = values[i + 1] - threshold
        if abs(end - start) > max_step:
            continue                          # display wrap, not a crossing
        if start * end < 0.0 or (start == 0.0 and end != 0.0):
            t = start / (start - end)         # 0..1 position between samples
            log_f = (math.log10(frequency_hz[i]) * (1.0 - t)
                     + math.log10(frequency_hz[i + 1]) * t)
            crossings.append((10.0 ** log_f,
                              other[i] + t * (other[i + 1] - other[i])))
    return crossings


def compute_bode_margins(data: dict[str, list[float]],
                         freq_min_hz: float = MARGIN_FREQ_MIN_HZ,
                         freq_max_hz: float = MARGIN_FREQ_MAX_HZ,
                         print_results: bool = True) -> dict[str, float | None]:
    """Compute crossover frequencies and stability margins from Bode data.

    The Bode setup measures loop phase relative to the positive-reinforcement
    point, so instability is at 0 deg: phase margin = the phase reading at a
    gain 0 dB crossover, and gain margin = -gain where the phase crosses
    0 deg. A wave shifted by a whole number of turns reinforces identically,
    so the criterion applies to the wrapped phase exactly as the scope
    reports it, and every wrap's 0 deg crossing is found without unwrapping.
    (The display wraps themselves are skipped via PHASE_WRAP_STEP_DEG; the
    scope's default -180..+180 deg window keeps the wrap jumps far from
    0 deg.)

    Only phase crossings above the gain crossover frequency count for the
    gain margin: the loop is not made unstable by the phase passing through
    0 deg before the gain has fallen to 0 dB. With no gain crossover found,
    no gain margin is reported either.

    Noise can make a trace cross more than once; every counted crossing is
    evaluated and the worst case (smallest margin) is returned, so false
    noise crossings can only under-report a margin, never over-report it.
    Crossings are searched over the whole sweep, but only those between
    freq_min_hz and freq_max_hz count towards the margins, excluding the
    noisy sweep extremes; the printout lists every crossing found, marking
    the ones that did not count. A margin is None when no crossing counts
    - a legitimate result, not an error.
    """
    frequency = data["frequency_hz"]
    gain = data["gain_db"]
    phase = data["phase_deg"]

    margins: dict[str, float | None] = {
        "gain_crossover_hz": None,
        "phase_margin_deg": None,
        "phase_crossover_hz": None,
        "gain_margin_db": None}

    gain_crossings_all = find_crossings(frequency, gain, GAIN_CROSSOVER_DB, phase)
    gain_crossings = [crossing for crossing in gain_crossings_all
                      if freq_min_hz <= crossing[0] <= freq_max_hz]
    if gain_crossings:
        # Worst case = lowest phase at a crossover = lowest phase margin
        crossover_hz, phase_at_crossover = min(gain_crossings,
                                               key=lambda c: c[1])
        margins["gain_crossover_hz"] = crossover_hz
        margins["phase_margin_deg"] = phase_at_crossover

    phase_crossings_all = find_crossings(frequency, phase, PHASE_CROSSOVER_DEG, gain, PHASE_WRAP_STEP_DEG)
    # Phase passing 0 deg before gain reaches 0 dB is not an instability, so
    # only crossings above the gain crossover qualify for the gain margin
    if margins["gain_crossover_hz"] is not None:
        phase_crossings = [crossing for crossing in phase_crossings_all
                           if freq_min_hz <= crossing[0] <= freq_max_hz
                           and crossing[0] > margins["gain_crossover_hz"]]
    else:
        phase_crossings = []
    if phase_crossings:
        # Worst case = highest gain at a crossover = lowest gain margin
        crossover_hz, gain_at_crossover = max(phase_crossings,
                                              key=lambda c: c[1])
        margins["phase_crossover_hz"] = crossover_hz
        margins["gain_margin_db"] = -gain_at_crossover

    if print_results:
        print(f"Bode margins ({len(gain_crossings)} gain and "
              f"{len(phase_crossings)} phase crossing(s) counted between "
              f"{freq_min_hz:.6g} Hz and {freq_max_hz:.6g} Hz):")
        if margins["phase_margin_deg"] is not None:
            print(f"  gain crossover  : {margins['gain_crossover_hz']:.6g} Hz")
            print(f"  phase margin    : {margins['phase_margin_deg']:.1f} deg")
        else:
            print("  gain crossover  : none (gain does not cross 0 dB)")
        if margins["gain_margin_db"] is not None:
            print(f"  phase crossover : {margins['phase_crossover_hz']:.6g} Hz")
            print(f"  gain margin     : {margins['gain_margin_db']:.1f} dB")
        else:
            print("  phase crossover : none (no phase 0 deg crossing above "
                  "the gain crossover)")
        # Extra crossings are usually noise; list every crossing in the whole
        # sweep so the choice is visible, marking those that did not count
        # (outside freq bounds, or phase crossing below the gain crossover)
        if len(gain_crossings_all) > 1:
            print("  all gain crossings : " + ", ".join(
                f"{f:.6g} Hz ({p:.1f} deg"
                f"{'' if (f, p) in gain_crossings else ', not counted'})"
                for f, p in gain_crossings_all))
        if len(phase_crossings_all) > 1:
            print("  all phase crossings: " + ", ".join(
                f"{f:.6g} Hz ({-g:.1f} dB"
                f"{'' if (f, g) in phase_crossings else ', not counted'})"
                for f, g in phase_crossings_all))
    return margins


def cmd_bode_set_markers(marker1_hz: float | None,
                         marker2_hz: float | None) -> None:
    """Place Bode markers 1 and 2, e.g. on the computed crossover frequencies.

    The scope snaps each marker to the nearest measured sample point; the
    snapped position and its gain/phase are read back and printed. Pass None
    to leave a marker untouched (e.g. when a crossover was not found).
    """
    for number, marker_hz in enumerate((marker1_hz, marker2_hz), start=1):
        if marker_hz is None:
            continue
        scpi_set(CMD_BODE_MARKER_FREQ.format(n=number), str(marker_hz))
        actual_hz = scpi_query_value(CMD_BODE_GET_MARK_FREQ.format(n=number))
        gain = scpi_query_value(CMD_BODE_GET_MARK_GAIN.format(n=number))
        phase = scpi_query_value(CMD_BODE_GET_MARK_PHASE.format(n=number))
        print(f"Marker {number} at {actual_hz:.6g} Hz: "
              f"gain {gain:.1f} dB, phase {phase:.1f} deg")


def cmd_save_screenshot(path: str = SCREENSHOT_PATH) -> None:
    """Save a screenshot of the scope display to a PNG file."""
    scpi_set(CMD_SCREENSHOT_FORMAT)   # format persists on the scope; pin it
    image = scpi_query_block(CMD_GET_SCREENSHOT, TIMEOUT_SCREEN_S)
    try:
        with open(path, "wb") as file:
            file.write(image)
    except OSError as exc:
        print(f"Could not write {path}: {exc}")
        sys.exit(1)
    print(f"Screenshot saved to {path} ({len(image)} bytes)")


def save_bode_csv(data: dict[str, list[float]], path: str = CSV_PATH) -> None:
    """Save Bode data as CSV: header row from the dict keys, one row per point."""
    try:
        with open(path, "w", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(data.keys())
            writer.writerows(zip(*data.values()))
    except OSError as exc:
        print(f"Could not write {path}: {exc}")
        sys.exit(1)
    print(f"Bode data saved to {path}")


def close_connection() -> None:
    """Close the connection in use; a no-op if it was never opened."""
    global instrument
    if CONNECTION == CONNECTION_LAN:
        if instrument is not None:
            instrument.close()
            instrument = None     # so a second call is a no-op, as for ser
            print(f"{LAN_RESOURCE} closed")
    else:
        if ser is not None and ser.is_open:
            ser.close()
            print(f"{COM_PORT} closed")


def main() -> None:
    # Connect to the scope
    open_connection()

    # Confirm the scope is alive and the Bode plot option is present
    cmd_identify()

    # Run a Bode sweep using the signal configuration already on the scope
    cmd_bode_run()

    # Fetch the resulting frequency/gain/phase arrays
    data = cmd_bode_get_data()

    # Compute the crossover frequencies and stability margins
    margins = compute_bode_margins(data)

    # Put the scope's markers on the crossovers and save a screenshot
    cmd_bode_set_markers(margins["gain_crossover_hz"],
                         margins["phase_crossover_hz"])
    cmd_save_screenshot(SCREENSHOT_PATH)

    # Save the data to a CSV file (simple fixed name for the standalone test)
    save_bode_csv(data, CSV_PATH)

    # Done - release the port
    close_connection()


if __name__ == "__main__":
    main()
