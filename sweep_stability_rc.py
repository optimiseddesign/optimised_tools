"""Sweep R/C compensation values and measure loop stability at each point.

Orchestrates the two instrument drivers: control_compensation_probe.py sets
resistance/capacitance on the AD EVAL-LTPA-COMPRB compensation probe, and
control_rtb2004_scope.py runs a Bode plot sweep on the R&S RTB2004 and
computes the stability margins. Connection settings (COM ports etc.) live in
the drivers.

Outputs, all prefixed with the run's start timestamp:
  - a raw Bode CSV per R/C point (frequency/gain/phase);
  - a scope screenshot PNG per R/C point, markers on the crossovers;
  - a summary CSV, one row per point, appended immediately after each point;
  - three matrix CSVs for graphing (rows = capacitance, columns = resistance):
    phase margin, gain margin and gain crossover frequency, rewritten in full
    after each point;
  - a log.txt of everything the script prints (stderr too, so tracebacks
    and warnings are kept), headed by TEST_DESCRIPTION (a manual note of
    the test circumstances, edited per run).
An interrupted run therefore keeps every completed point on disk.

Scope signal configuration (channels, generator amplitude, sweep range,
points) is set up manually on the scope beforehand; the drivers never *RST.
Both COM ports are exclusive on Windows - close LTPowerAnalyzer and any
terminals before a run. An instrument failure currently aborts the run
(the drivers exit on error); completed points remain on disk.

Requires: pyserial (pip install pyserial). Python 3.13.

Copyright Optimised Product Design Ltd 2026. Available for public use
(copyright reserved) - see repository README; use at your own risk.
"""

import csv
import io
import sys
import time

import control_compensation_probe as probe
import control_rtb2004_scope as scope

# --- Sweep configuration ------------------------------------------------------
# Manual note of the test circumstances, logged at the start of the run
TEST_DESCRIPTION = ("PT136F 1.0 1A Charger based on LTC4020 - Varying ITH, fixed VC 150pF 120k. At 15V In, 0.35A Out.")

# Values to test, manually defined per run.
# Outer loop is capacitance, inner resistance, matching the matrix CSV layout.
SWEEP_RESISTANCE_OHM = [22000, 33000, 47000, 68000, 100000]
SWEEP_CAPACITANCE_PF = [2200, 3300, 4700, 6800, 8200]

# --- Output configuration -----------------------------------------------------
RUN_TIMESTAMP    = time.strftime("%Y%m%d_%H%M%S")  # shared by all files of a run
CSV_BODE_NAME    = RUN_TIMESTAMP + "_bode_r{r}ohm_c{c}pf.csv"  # per-point raw data
PNG_BODE_NAME    = RUN_TIMESTAMP + "_bode_r{r}ohm_c{c}pf.png"  # per-point screenshot
CSV_SUMMARY_NAME = RUN_TIMESTAMP + "_summary.csv"
LOG_NAME         = RUN_TIMESTAMP + "_log.txt"      # everything the script prints
CSV_MATRIX_NAMES = {                               # margin key -> matrix file
    "phase_margin_deg":  RUN_TIMESTAMP + "_phase_margin_deg.csv",
    "gain_margin_db":    RUN_TIMESTAMP + "_gain_margin_db.csv",
    "gain_crossover_hz": RUN_TIMESTAMP + "_gain_crossover_hz.csv"}
SUMMARY_COLUMNS  = ("timestamp",
                    "target_resistance_ohm", "actual_resistance_ohm",
                    "target_capacitance_pf", "actual_capacitance_pf",
                    "gain_crossover_hz", "phase_margin_deg",
                    "phase_crossover_hz", "gain_margin_db")
MATRIX_CORNER    = "c_pf\\r_ohm"                   # top-left axis-label cell

# Margins of every completed point, keyed (capacitance pF, resistance ohm);
# shared so the matrix CSVs can be rewritten in full after each point
results: dict[tuple[int, int], dict[str, float | None]] = {}


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
    """Start the run log and record the test circumstances."""
    try:
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
    print(f"\n")


def open_instruments() -> None:
    """Open both COM ports and confirm both instruments are alive."""
    probe.open_port()
    probe.cmd_status_update()
    scope.open_connection()
    scope.cmd_identify()


def close_instruments() -> None:
    probe.close_port()
    scope.close_connection()


def append_summary_row(r_ohm: int, c_pf: int,
                       config: dict[str, float],
                       margins: dict[str, float | None]) -> None:
    """Append one point to the summary CSV (header first on a new file).

    Margins without a crossing are None and appear as empty cells - a
    legitimate result, not an error.
    """
    row = (time.strftime("%Y-%m-%d %H:%M:%S"),
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


def write_matrix_csvs() -> None:
    """Rewrite the three margin matrix CSVs from all points measured so far.

    A matrix row is only complete once every resistance for that capacitance
    has been measured, so the files are rewritten in full after each point
    rather than appended; they always reflect every completed measurement.
    Axes are the target values; cells are empty when not yet measured or when
    the margin has no crossing.
    """
    for key, path in CSV_MATRIX_NAMES.items():
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
    print("Matrix CSVs updated: " + ", ".join(CSV_MATRIX_NAMES.values()))


def measure_point(r_ohm: int, c_pf: int) -> None:
    """Measure one R/C point and record it in every output CSV."""
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
    scope.cmd_save_screenshot(PNG_BODE_NAME.format(r=r_ohm, c=c_pf))
    scope.save_bode_csv(data, CSV_BODE_NAME.format(r=r_ohm, c=c_pf))

    append_summary_row(r_ohm, c_pf, config, margins)
    results[(c_pf, r_ohm)] = margins
    write_matrix_csvs()


def run_sweep() -> None:
    """Measure every R/C combination, recording all outputs as each completes."""
    total = len(SWEEP_CAPACITANCE_PF) * len(SWEEP_RESISTANCE_OHM)
    for c_pf in SWEEP_CAPACITANCE_PF:
        for r_ohm in SWEEP_RESISTANCE_OHM:
            print(f"\nPoint {len(results) + 1} of {total}: "
                  f"R={r_ohm} ohm, C={c_pf} pF")
            measure_point(r_ohm, c_pf)
    print(f"\nSweep complete: {len(results)} of {total} point(s) measured")


def main() -> None:
    # Start the run log and record the test circumstances
    open_log()

    # Connect to the probe and the scope, and confirm both are alive
    open_instruments()

    # Measure every R/C combination, recording results as each completes
    run_sweep()

    # Done - release both ports
    close_instruments()


if __name__ == "__main__":
    main()
