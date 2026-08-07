"""Sweep R/C compensation values and measure loop stability at each point.

Orchestrates four instrument drivers: control_compensation_probe.py sets
resistance/capacitance on the AD EVAL-LTPA-COMPRB compensation probe,
control_oscilloscope_rtb2004.py runs a Bode plot sweep on the R&S RTB2004 and
computes the stability margins, and control_psu_mx180tp.py (TTi MX180TP) and
control_dcload_plz405w.py (Kikusui PLZ405W) supply and load the circuit at the
operating point the sweep is measured at. Connection settings (the probe's COM
port, the other three instruments' LAN addresses) live in the drivers.

The whole R/C sweep is repeated once per entry in OPERATING_POINTS, so one run
covers what used to be one run per operating point. The PSU's voltage and
current limit, the load's current, both instruments' ranges and the load's mode
are set per operating point, from OPERATING_POINTS and the table beside it,
with the circuit powered down over the change and back up afterwards; the run
always ends powered down. Nothing about the operating point is left to the
bench.

Outputs, all in a run folder named <timestamp>_bode_sweep <TEST_DESCRIPTION_SHORT>
and prefixed with the run's start timestamp. Covering the whole run:
  - a summary CSV, one row per R/C point of every operating point, appended
    immediately after each point;
  - a log.txt of everything the script prints (stderr too, so tracebacks
    and warnings are kept), headed by TEST_DESCRIPTION (a manual note of
    the test circumstances, edited per run).
Then a sub-folder per operating point, named for it, holding what a run of one
operating point used to produce:
  - a raw Bode CSV per R/C point (frequency/gain/phase);
  - a scope screenshot PNG per R/C point, markers on the crossovers;
  - four matrix CSVs for graphing (rows = capacitance, columns = resistance):
    phase margin, gain margin, gain crossover frequency and phase crossover
    frequency, rewritten in full after each point.
An interrupted run therefore keeps every completed point on disk.

Scope signal configuration (channels, generator amplitude, sweep range,
points) is set up manually on the scope beforehand; the drivers never *RST.
Close LTPowerAnalyzer and any terminals before a run: the probe's COM port is
exclusive on Windows, the scope serves only one remote session at a time (a
second one hangs until it times out rather than failing cleanly), and the PSU
accepts one control socket. An instrument failure aborts the run (the drivers
exit on error), but every completed point remains on disk and every instrument
is released on the way out.

Requires: pyserial for the probe, RsInstrument for the scope, PyVISA and
pyvisa-py for the DC load; the PSU needs nothing beyond the standard library
(pip install pyserial RsInstrument pyvisa pyvisa-py). Python 3.13.

Copyright Optimised Product Design Ltd 2026. Available for public use
(copyright reserved) - see repository README; use at your own risk.
"""

import csv
import io
import os
import sys
import time

import control_compensation_probe as probe
import control_dcload_plz405w as dcload
import control_oscilloscope_rtb2004 as scope
import control_psu_mx180tp as psu

# --- Sweep configuration ------------------------------------------------------
# Manual note of the test circumstances, logged at the start of the run
TEST_DESCRIPTION = ("PT136F 1.0 1A Charger with LTC4020 (plus Cff 100pF, VIN_REG Disabled, ILIMIT override disabled, VFBMAX 11k 47k for 14.5V) - Varying ITH, fixed VC 150pF 120k.")

# Short version used to name the run's output folder (not the files in it);
# use only characters legal in a folder name (no \ / : * ? " < > |). The
# operating points are no longer part of it - a run covers several, and each
# gets its own sub-folder named after itself.
TEST_DESCRIPTION_SHORT = "ITH vary with VC 150pF 120k"

# Values to test, manually defined per run.
# Outer loop is capacitance, inner resistance, matching the matrix CSV layout.
SWEEP_RESISTANCE_OHM = [22000, 27000, 33000, 39000, 47000, 56000, 68000, 82000, 100000]
SWEEP_CAPACITANCE_PF = [2200, 2700, 3300, 3900, 4700, 5600, 6800, 8200, 10000]

# Operating Points to sweep at - Runs the sweep at each VIN, ILOAD pair such as
# 9.5 V in at 1.5 A out, with the instrument settings that point needs. Pairs
# rather than two swept lists, so the points can be scattered around the
# envelope rather than a full grid. Edited per run - comment out a line to skip
# that point. Ranges use the drivers' names for what each one allows.
#
#   (vin_v, iout_a): (psu_output_num, psu_range_num,     psu_current_limit_a, dcload_mode,    dcload_current_range, dcload_voltage_range)
OPERATING_POINTS = {
    (9.5, 1.5):      (1,              psu.RANGE_15V_10A, 4.0,                 dcload.MODE_CC, dcload.RANGE_8A,      dcload.RANGE_15V),
}
TIME_SETTLE_S = 2.0                                # after each power step

# --- Output configuration (run-level) -----------------------------------------
# The run folder, its log, and the summary CSV. The summary spans the whole
# run, so each row names its operating point.
RUN_TIMESTAMP    = time.strftime("%Y%m%d_%H%M%S")  # shared by all files of a run
RUN_DIR          = RUN_TIMESTAMP + "_bode_sweep " + TEST_DESCRIPTION_SHORT
RUN_PREFIX       = os.path.join(RUN_DIR, RUN_TIMESTAMP)  # folder + filename stem
CSV_SUMMARY_NAME = RUN_PREFIX + "_summary.csv"     # one row of margins per point
LOG_NAME         = RUN_PREFIX + "_log.txt"         # everything the script prints
SUMMARY_COLUMNS  = ("timestamp",
                    "target_vin_v", "target_iout_a",
                    "target_resistance_ohm", "actual_resistance_ohm",
                    "target_capacitance_pf", "actual_capacitance_pf",
                    "gain_crossover_hz", "phase_margin_deg",
                    "phase_crossover_hz", "gain_margin_db")

# --- Output configuration (per operating point) -------------------------------
# Each operating point gets a sub-folder of the run folder holding its Bode
# CSVs, screenshots and four matrix CSVs. {op_point_name} names both that
# sub-folder and a part of every filename in it, so a file still says where
# it came from once copied out; {c} and {r} are the R/C point - giving
# 9V5in_1A5out/20260807_170116_9V5in_1A5out_bode_c2200pf_r22000ohm.csv.
OPERATING_POINT_NAME   = "{vin}in_{iout}out"       # 9V5in_1A5out
OPERATING_POINT_PREFIX = os.path.join(RUN_DIR, "{op_point_name}",
                                      RUN_TIMESTAMP + "_{op_point_name}")
CSV_BODE_NAME          = OPERATING_POINT_PREFIX + "_bode_c{c}pf_r{r}ohm.csv"
PNG_BODE_NAME          = OPERATING_POINT_PREFIX + "_bode_c{c}pf_r{r}ohm.png"
CSV_MATRIX_NAMES       = {                         # margin key -> matrix file
    "phase_margin_deg":   OPERATING_POINT_PREFIX + "_phase_margin_deg.csv",
    "gain_margin_db":     OPERATING_POINT_PREFIX + "_gain_margin_db.csv",
    "gain_crossover_hz":  OPERATING_POINT_PREFIX + "_gain_crossover_hz.csv",
    "phase_crossover_hz": OPERATING_POINT_PREFIX + "_phase_crossover_hz.csv"}
MATRIX_CORNER          = "c_pf\\r_ohm"             # top-left axis-label cell

# Margins of every completed point, keyed (capacitance pF, resistance ohm);
# shared so the matrix CSVs can be rewritten in full after each point
results: dict[tuple[int, int], dict[str, float | None]] = {}

# The PSU output the circuit is powered from, so the run can take it down again
psu_output_num: int | None = None


class TeeStream(io.TextIOBase):
    """Stand-in for sys.stdout/stderr that copies everything into the run log.

    All output goes through these two streams - this script's and the
    drivers' print() calls on stdout; tracebacks and warnings on stderr -
    so replacing both captures the lot. io.TextIOBase supplies the rest of
    the file interface for any code that expects more than write/flush.
    """

    def __init__(self, stream, log_file) -> None:
        self.stream = stream
        self.log_file = log_file

    def write(self, text: str) -> int:
        self.stream.write(text)
        self.log_file.write(text)
        self.log_file.flush()     # keep the log complete if the run is interrupted
        return len(text)

    def flush(self) -> None:
        self.stream.flush()


def open_log() -> None:
    """Create the run's output folder, start the log and record the test
    circumstances."""
    try:
        os.makedirs(RUN_DIR, exist_ok=True)
        # UTF-8 explicitly: the locale default (cp1252) cannot encode the
        # U+FFFD characters that the drivers' errors="replace" decodes produce
        log_file = open(LOG_NAME, "a", encoding="utf-8")
    except OSError as exc:
        print(f"Could not open {LOG_NAME}: {exc}")
        sys.exit(1)
    sys.stdout = TeeStream(sys.stdout, log_file)
    sys.stderr = TeeStream(sys.stderr, log_file)
    print(f"Sweep run {RUN_TIMESTAMP}, logging to {LOG_NAME}")
    print(f"Test description: {TEST_DESCRIPTION}")
    print(f"Sweep resistances : {SWEEP_RESISTANCE_OHM} ohm")
    print(f"Sweep capacitances: {SWEEP_CAPACITANCE_PF} pF")
    print(f"Operating points  : "
          + "; ".join(format_op_point(operating_point)
                      for operating_point in OPERATING_POINTS))
    print(f"\n")


def format_r_notation(value: float, unit: str) -> str:
    """A value with its unit letter where the decimal point would be - 9V5.

    The bench's own convention (the earlier runs' folders were named "9V5 In
    7A Out"), and it keeps decimal points out of the names: 9.5 V is "9V5",
    30 V is "30V", 0.35 A is "0A35".
    """
    text = f"{value:g}"
    return text.replace(".", unit) if "." in text else text + unit


def format_op_point(operating_point: tuple[float, float]) -> str:
    """One operating point as text - "9V5in_1A5out".

    Names both its output sub-folder and the operating point part of every
    filename in it, so a file always matches the folder it belongs in. Also
    used in the log, so a folder is easy to match to the run it came from.
    """
    vin_v, iout_a = operating_point
    return OPERATING_POINT_NAME.format(vin=format_r_notation(vin_v, "V"),
                                       iout=format_r_notation(iout_a, "A"))


def open_operating_point_dir(op_point_name: str) -> None:
    """Create one operating point's output sub-folder."""
    path = os.path.join(RUN_DIR, op_point_name)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        print(f"Could not create {path}: {exc}")
        sys.exit(1)


def open_instruments() -> None:
    """Open every instrument connection and confirm each instrument is alive."""
    probe.open_port()
    probe.cmd_status_update()
    scope.open_connection()
    scope.cmd_identify()
    psu.open_connection()
    psu.cmd_identify()
    dcload.open_connection()
    dcload.cmd_identify()


def close_instruments() -> None:
    """Release every instrument connection.

    Each driver's close is a no-op on an instrument it never opened, so this
    can be called however far into the run things got - including from a
    driver's own sys.exit(1), which raises SystemExit and so still unwinds
    through main()'s finally.
    """
    probe.close_port()
    scope.close_connection()
    psu.close_connection()
    dcload.close_connection()


def append_summary_row(operating_point: tuple[float, float],
                       r_ohm: int, c_pf: int,
                       config: dict[str, float],
                       margins: dict[str, float | None]) -> None:
    """Append one point to the summary CSV (header first on a new file).

    Margins without a crossing are None and appear as empty cells - a
    legitimate result, not an error.
    """
    vin_v, iout_a = operating_point
    row = (time.strftime("%Y-%m-%d %H:%M:%S"),
           vin_v, iout_a,
           r_ohm, config["total_resistance_ohm"],
           c_pf, config["total_capacitance_pf"],
           margins["gain_crossover_hz"], margins["phase_margin_deg"],
           margins["phase_crossover_hz"], margins["gain_margin_db"])
    try:
        with open(CSV_SUMMARY_NAME, "a", newline="") as file:
            writer = csv.writer(file)
            if file.tell() == 0:
                writer.writerow(SUMMARY_COLUMNS)
            writer.writerow(row)
    except OSError as exc:
        print(f"Could not write {CSV_SUMMARY_NAME}: {exc}")
        sys.exit(1)
    print(f"Summary row appended to {CSV_SUMMARY_NAME}")


def write_matrix_csvs(op_point_name: str) -> None:
    """Rewrite the four margin matrix CSVs from all points measured so far.

    A matrix row is only complete once every resistance for that capacitance
    has been measured, so the files are rewritten in full after each point
    rather than appended; they always reflect every completed measurement.
    Axes are the target values; cells are empty when not yet measured or when
    the margin has no crossing. They hold one operating point's margins, and
    live in that operating point's sub-folder.
    """
    for key, name in CSV_MATRIX_NAMES.items():
        path = name.format(op_point_name=op_point_name)
        try:
            with open(path, "w", newline="") as file:
                writer = csv.writer(file)
                writer.writerow([MATRIX_CORNER, *SWEEP_RESISTANCE_OHM])
                for c_pf in SWEEP_CAPACITANCE_PF:
                    row: list[float | None] = [c_pf]
                    for r_ohm in SWEEP_RESISTANCE_OHM:
                        margins = results.get((c_pf, r_ohm))
                        row.append(margins[key] if margins is not None else None)
                    writer.writerow(row)
        except OSError as exc:
            print(f"Could not write {path}: {exc}")
            sys.exit(1)
    print("Matrix CSVs updated: "
          + ", ".join(name.format(op_point_name=op_point_name)
                      for name in CSV_MATRIX_NAMES.values()))


def measure_point(operating_point: tuple[float, float],
                  r_ohm: int, c_pf: int) -> None:
    """Measure one R/C point and record it in every output CSV."""
    op_point_name = format_op_point(operating_point)
    probe.cmd_set_resistance(r_ohm)
    probe.cmd_set_capacitance(c_pf)
    config = probe.cmd_get_configuration(False)
    print(f"Actual configuration: {config['total_resistance_ohm']:.1f} ohm, "
          f"{config['total_capacitance_pf']:.1f} pF")

    scope.cmd_bode_run()
    data = scope.cmd_bode_get_data()
    margins = scope.compute_bode_margins(data)
    scope.cmd_bode_set_markers(margins["gain_crossover_hz"],
                               margins["phase_crossover_hz"])
    scope.cmd_save_screenshot(
        PNG_BODE_NAME.format(op_point_name=op_point_name, r=r_ohm, c=c_pf))
    scope.save_bode_csv(
        data, CSV_BODE_NAME.format(op_point_name=op_point_name,
                                   r=r_ohm, c=c_pf))

    append_summary_row(operating_point, r_ohm, c_pf, config, margins)
    results[(c_pf, r_ohm)] = margins
    write_matrix_csvs(op_point_name)


def run_sweep(operating_point: tuple[float, float], number: int) -> None:
    """Measure every R/C combination, recording all outputs as each completes."""
    total = len(SWEEP_CAPACITANCE_PF) * len(SWEEP_RESISTANCE_OHM)
    for c_pf in SWEEP_CAPACITANCE_PF:
        for r_ohm in SWEEP_RESISTANCE_OHM:
            print(f"\nOperating point {number} of {len(OPERATING_POINTS)} | "
                  f"Point {len(results) + 1} of {total}: "
                  f"R={r_ohm} ohm, C={c_pf} pF")
            measure_point(operating_point, r_ohm, c_pf)
    print(f"\nSweep complete: {len(results)} of {total} point(s) measured")


def set_operating_point(operating_point: tuple[float, float]) -> None:
    """Put the circuit at one operating point, ready to be swept.

    Powers down before changing anything and back up afterwards, so the
    circuit never passes through a setpoint pair that is not an operating
    point - and because the ranges and the load's mode are only settable
    while both instruments are off.
    """
    global psu_output_num
    vin_v, iout_a = operating_point
    (psu_output_num, psu_range_num, psu_current_limit_a,
     dcload_mode, dcload_current_range,
     dcload_voltage_range) = OPERATING_POINTS[operating_point]

    # Power down, supply first, the load left on to drain the circuit
    psu.cmd_set_output_enable(psu_output_num, False)
    time.sleep(TIME_SETTLE_S)
    dcload.cmd_set_input_enable(False)

    # Set the ranges and the load mode, all of which are refused while live,
    # and the ranges before the setpoints, which a lower range would clamp
    psu.cmd_set_range(psu_output_num, psu_range_num)
    dcload.cmd_set_mode(dcload_mode)
    dcload.cmd_set_current_range(dcload_current_range)
    dcload.cmd_set_voltage_range(dcload_voltage_range)

    # Set the PSU current limit, the PSU voltage, then the load current
    psu.cmd_set_current(psu_output_num, psu_current_limit_a)
    psu.cmd_set_voltage(psu_output_num, vin_v)
    dcload.cmd_set_current(iout_a)

    # Power up, supply first, settling after each step
    psu.cmd_set_output_enable(psu_output_num, True)
    time.sleep(TIME_SETTLE_S)
    dcload.cmd_set_input_enable(True)
    time.sleep(TIME_SETTLE_S)


def main() -> None:
    # Start the run log and record the test circumstances
    open_log()

    try:
        # Connect to all four instruments and confirm each one is alive
        open_instruments()

        # Run the whole R/C sweep once at each operating point
        for number, operating_point in enumerate(OPERATING_POINTS, start=1):
            op_point_name = format_op_point(operating_point)
            print(f"\n=== Operating point {number} of "
                  f"{len(OPERATING_POINTS)}: {op_point_name} ===")

            set_operating_point(operating_point)

            # Its own sub-folder, and its own margins: the matrix CSVs and the
            # "point N of M" count cover one operating point, while the summary
            # CSV is appended to and keeps every point of the whole run
            open_operating_point_dir(op_point_name)
            results.clear()

            run_sweep(operating_point, number)

        print(f"\nRun complete: "
              f"{len(OPERATING_POINTS)} operating point(s) swept")
    finally:
        # Power down, supply first, the load left on to drain the circuit
        if psu_output_num is not None:
            psu.cmd_set_output_enable(psu_output_num, False)
            time.sleep(TIME_SETTLE_S)
            dcload.cmd_set_input_enable(False)

        # However the run ends - finished, Ctrl+C, or a driver's sys.exit(1)
        # on an instrument error - hand every instrument back
        close_instruments()


if __name__ == "__main__":
    main()
