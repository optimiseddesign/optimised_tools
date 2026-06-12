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

import csv
import math
import sys
import time

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
CMD_CLEAR_STATUS   = "*CLS"           # clear status registers and error queue
CMD_GET_IDENTITY   = "*IDN?"          # manufacturer,model,serial,firmware
CMD_GET_OPTIONS    = "*OPT?"          # installed options, comma-separated
CMD_GET_ERROR      = "SYSTem:ERRor?"  # oldest error in the queue; "0,..." = none
CMD_FORMAT_ASCII   = "FORMat ASC"     # data queries reply in ASCII, comma-separated
CMD_BODE_ENABLE_ON = "BPLot:ENABle ON"  # opens the Bode plot application
CMD_BODE_RUN       = "BPLot:STATe RUN"  # starts a Bode sweep
CMD_BODE_GET_STATE = "BPLot:STATe?"   # sweep state: RUN or STOP
CMD_BODE_GET_FREQ  = "BPLot:FREQuency:DATA?"  # comma-separated frequencies, Hz
CMD_BODE_GET_GAIN  = "BPLot:GAIN:DATA?"       # comma-separated gain values, dB
CMD_BODE_GET_PHASE = "BPLot:PHASe:DATA?"      # comma-separated phase values, deg
CMD_BODE_MARKER1_FREQ    = "BPLot:MARKer1:FREQuency"   # set marker position, Hz
CMD_BODE_MARKER2_FREQ    = "BPLot:MARKer2:FREQuency"   # (snaps to nearest sample)
CMD_BODE_GET_MARK1_FREQ  = "BPLot:MARKer1:FREQuency?"  # actual (snapped) position
CMD_BODE_GET_MARK2_FREQ  = "BPLot:MARKer2:FREQuency?"
CMD_BODE_GET_MARK1_GAIN  = "BPLot:MARKer1:GAIN?"       # gain at marker, dB
CMD_BODE_GET_MARK2_GAIN  = "BPLot:MARKer2:GAIN?"
CMD_BODE_GET_MARK1_PHASE = "BPLot:MARKer1:PHASe?"      # phase at marker, deg
CMD_BODE_GET_MARK2_PHASE = "BPLot:MARKer2:PHASe?"
CMD_SCREENSHOT_FORMAT    = "HCOPy:LANGuage PNG"  # screenshot image format
CMD_GET_SCREENSHOT       = "HCOPy:DATA?"      # screenshot as 488.2 block data
REPLY_NO_ERROR     = "0,"             # SYSTem:ERRor? reply prefix when queue empty
REPLY_BODE_STOP    = "STOP"           # BPLot:STATe? reply once the sweep finished
OPTION_BODE        = "K36"            # Bode plot application option (RTB-K36)
TIMEOUT_BODE_S     = 90.0            # max sweep time (low start freqs are slow)
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

    print(f"Opening Oscilloscope {COM_PORT} at {BAUD_RATE} baud")

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
    # Other software may leave data format as binary (REAL,32); pin it to
    # ASCII so replies parse as text. Affects remote transfers only.
    scpi_set(CMD_FORMAT_ASCII)


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


def scpi_query_block(command: str, timeout_s: float = TIMEOUT_READ_S) -> bytes:
    """Send a query whose reply is an IEEE 488.2 definite-length binary block.

    Block format: '#', one digit giving the length of the byte count, the
    byte count, then the raw bytes, then LF. Needed because binary data
    (e.g. a PNG screenshot) contains stray LF bytes that break the
    line-based scpi_query(); print and exit on a malformed or short reply.
    """
    ser.timeout = timeout_s       # applies to each read below
    scpi_send(command)
    header = ser.read(2)          # b'#' then the digit count of the length
    if not (header.startswith(b"#") and header[1:].isdigit()):
        print(f"Scope did not return block data for {command}: got {header!r}")
        sys.exit(1)
    length = ser.read(int(header[1:]))
    if not length.isdigit():
        print(f"Bad block length for {command}: got {length!r}")
        sys.exit(1)
    payload = ser.read(int(length))
    ser.read_until(RX_EOL)        # consume the trailing terminator
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
    if error.startswith(REPLY_NO_ERROR):
        return                    # command accepted
    else:
        print(f"Command {command} failed: {error}")
        sys.exit(1)


def cmd_identify(print_results: bool = True) -> dict[str, str]:
    """Identify the scope (*IDN?) and check the Bode plot option is installed.

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

    Noise can make a trace cross more than once; every crossing is evaluated
    and the worst case (smallest margin) is returned, so false noise
    crossings can only under-report a margin, never over-report it.
    freq_min_hz/freq_max_hz bound the search to exclude the noisy sweep
    extremes. A margin is None when its crossing does not occur within
    bounds - a legitimate result, not an error.
    """
    # Restrict to the requested frequency window (data is in ascending order)
    in_window = [i for i, f in enumerate(data["frequency_hz"])
                 if freq_min_hz <= f <= freq_max_hz]
    window = slice(in_window[0], in_window[-1] + 1)
    frequency = data["frequency_hz"][window]
    gain = data["gain_db"][window]
    phase = data["phase_deg"][window]

    margins: dict[str, float | None] = {
        "gain_crossover_hz": None,
        "phase_margin_deg": None,
        "phase_crossover_hz": None,
        "gain_margin_db": None}

    gain_crossings = find_crossings(frequency, gain, GAIN_CROSSOVER_DB, phase)
    if gain_crossings:
        # Worst case = lowest phase at a crossover = lowest phase margin
        crossover_hz, phase_at_crossover = min(gain_crossings,
                                               key=lambda c: c[1])
        margins["gain_crossover_hz"] = crossover_hz
        margins["phase_margin_deg"] = phase_at_crossover

    phase_crossings = find_crossings(frequency, phase, PHASE_CROSSOVER_DEG, gain, PHASE_WRAP_STEP_DEG)
    if phase_crossings:
        # Worst case = highest gain at a crossover = lowest gain margin
        crossover_hz, gain_at_crossover = max(phase_crossings,
                                              key=lambda c: c[1])
        margins["phase_crossover_hz"] = crossover_hz
        margins["gain_margin_db"] = -gain_at_crossover

    if print_results:
        print(f"Bode margins ({len(gain_crossings)} gain and "
              f"{len(phase_crossings)} phase crossing(s) between "
              f"{frequency[0]:.6g} Hz and {frequency[-1]:.6g} Hz):")
        if margins["phase_margin_deg"] is not None:
            print(f"  gain crossover  : {margins['gain_crossover_hz']:.6g} Hz")
            print(f"  phase margin    : {margins['phase_margin_deg']:.1f} deg")
        else:
            print("  gain crossover  : none (gain does not cross 0 dB)")
        if margins["gain_margin_db"] is not None:
            print(f"  phase crossover : {margins['phase_crossover_hz']:.6g} Hz")
            print(f"  gain margin     : {margins['gain_margin_db']:.1f} dB")
        else:
            print("  phase crossover : none (phase does not cross 0 deg)")
        # Extra crossings are usually noise; list all so the choice is visible
        if len(gain_crossings) > 1:
            print("  all gain crossings : " + ", ".join(
                f"{f:.6g} Hz ({p:.1f} deg)" for f, p in gain_crossings))
        if len(phase_crossings) > 1:
            print("  all phase crossings: " + ", ".join(
                f"{f:.6g} Hz ({-g:.1f} dB)" for f, g in phase_crossings))
    return margins


def cmd_bode_set_markers(marker1_hz: float | None,
                         marker2_hz: float | None) -> None:
    """Place Bode markers 1 and 2, e.g. on the computed crossover frequencies.

    The scope snaps each marker to the nearest measured sample point; the
    snapped position and its gain/phase are read back and printed. Pass None
    to leave a marker untouched (e.g. when a crossover was not found).
    """
    if marker1_hz is not None:
        scpi_set(CMD_BODE_MARKER1_FREQ, str(marker1_hz))
        actual_hz = scpi_query(CMD_BODE_GET_MARK1_FREQ)
        gain = scpi_query(CMD_BODE_GET_MARK1_GAIN)
        phase = scpi_query(CMD_BODE_GET_MARK1_PHASE)
        print(f"Marker 1 at {actual_hz} Hz: gain {gain} dB, phase {phase} deg")
    if marker2_hz is not None:
        scpi_set(CMD_BODE_MARKER2_FREQ, str(marker2_hz))
        actual_hz = scpi_query(CMD_BODE_GET_MARK2_FREQ)
        gain = scpi_query(CMD_BODE_GET_MARK2_GAIN)
        phase = scpi_query(CMD_BODE_GET_MARK2_PHASE)
        print(f"Marker 2 at {actual_hz} Hz: gain {gain} dB, phase {phase} deg")


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
    if ser.is_open:
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
