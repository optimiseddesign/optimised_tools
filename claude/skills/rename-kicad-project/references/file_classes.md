# File-class reference

The full taxonomy used by `scripts/rename_kicad.py` to decide what to do with each file in the project. Read this if you need to extend the classification (e.g. adding support for a new export format) or debug why a particular file was or wasn't renamed/edited.

## Class A — Skip entirely

Never renamed, never edited. These are either VCS metadata, KiCad's own runtime artifacts, or local backups. Touching them either breaks tooling or pollutes the rename diff with noise.

| Pattern | Why we skip |
|---------|-------------|
| `.git/*`, `*/.git/*` | Git's internal storage. `git mv` updates these for us; touching them directly corrupts the repo. |
| `.gitignore`, `.gitmodules`, `.gitattributes` | Repo-level config. Almost never references the project basename. |
| `*.lck`, `~*.lck` | KiCad lock files. Created when a `.kicad_pro` / `.kicad_sch` is open. Renaming them is meaningless — KiCad rewrites them on next open — and editing them risks colliding with a live editor. |
| `.history/*`, `*/.history/*` | VS Code Local History extension. Backup snapshots that KiCad doesn't know about. Bulk-renaming them inflates the diff and serves no purpose. |
| `*-backups/*`, `*.bak` | KiCad's own auto-backup folders, plus generic `.bak` files. Same logic as `.history`. |

## Class B — Rename only, contents preserved byte-for-byte

These are downstream artifacts. They were generated *from* the KiCad source by a plot/export step. Their bytes must not be silently mutated, because:
- Manufacturers compare hashes against what was sent in a previous order.
- The customer / archived design pack treats these as immutable snapshots.
- Editing them in place desyncs them from the source they were generated from.

| Extension | Typical content |
|-----------|-----------------|
| `.gbr`, `.gbrjob` | Gerber layers and the job description that ties them together. |
| `.drl` | Excellon drill files (PTH + NPTH). |
| `.step`, `.stp`, `.stl`, `.wrl` | 3D mechanical models (board + components, for enclosure design). |
| `.png`, `.jpg`, `.jpeg`, `.svg`, `.dxf` | Raster/vector exports — silkscreen previews, marketing renders, panel layouts. |
| `.pdf` | Schematic prints, layout prints, design-review packs. |
| `.csv` | Position files (`-top-pos.csv`, `-bottom-pos.csv`), BOM exports, pick-and-place data. |
| `.zip`, `.tar`, `.tgz`, `.7z` | ODB++ bundles, Gerber zips for upload, archived design packs. |
| `.xlsx`, `.xls` | Impedance-control spreadsheets, simulation input data, vendor-supplied datasheets bundled with the project. |

If a project has a content-bearing file that ends in one of these extensions but *should* be edited (rare), pass `--extra-content-glob "<path>"` to override. Conversely, if a project has a `.csv` that genuinely is a source file (e.g. a hand-maintained variant matrix), be aware that this script treats it as binary-rename-only by default — the safer assumption.

## Class C — Rename + content edit

Both the filename and any matching strings inside the file are updated.

| Extension / name | What's inside |
|------------------|---------------|
| `.kicad_pro` | Project file (JSON). Contains `meta.filename`, `last_paths.netlist`, `last_paths.step`, `top_level_sheets[*].filename`, `top_level_sheets[*].name`, `sheets[*][1]` (sheet name) — all reference the basename. |
| `.kicad_prl` | Per-user layout state (JSON). Less critical but tidier to keep consistent. |
| `.kicad_sch` | Schematic (s-expression). Contains `(title "...")` in the title block, free-text annotations via `(text "...")`, and `(instances (project "<basename>" ...))` blocks for every component. Also `(property "Sheetfile" "<basename>_<sub>.kicad_sch")` for sub-sheet links. |
| `.kicad_pcb` | Layout (s-expression). Title block + free text strings; per-footprint `(sheetfile "<basename>_<sub>.kicad_sch")` references. |
| `.kicad_dru` | Custom design rules. Project name doesn't usually appear, but if it does it's worth keeping in sync. |
| `.kicad_sym`, `.kicad_mod`, `.kicad_wks` | Library/worksheet files. Project basename rarely appears, but the safe default is to scan and edit if it does. |
| `fp-lib-table`, `sym-lib-table` | KiCad library-table files (s-expression, no extension). Project-local variants reference paths under `${KIPRJMOD}/...` — usually stable, but edited if the basename appears. |
| `*.md`, `*.txt` | Documentation. README typically embeds image links like `images/<basename>_top.png` and human-readable references to the board name. |

## How a single pair is applied

For each find/replace pair on the command line:

1. **To filenames** (if `apply_to_filenames`): the script takes `path.name` (no directory part), runs `str.replace(old, new)`, and that becomes the destination filename. Directory names are not affected — projects are renamed by their basename, not by moving the whole tree.
2. **To file contents** (if `apply_to_contents` and the file is in Class C): the script reads the file as UTF-8, applies `str.replace(old, new)` on the whole text, and writes it back.

Pairs are applied **in command-line order**. Order matters when:
- A later pair fixes up a fragment left by an earlier pair (e.g. `"USB Dongle"="Cellular Gateway"` then `"USB Cellular Gateway"="Cellular Gateway"`).
- Two pairs overlap. The later pair sees the *post-first-pair* text, not the original.

## Class D — Detected at runtime, not by extension

These files don't have a fixed extension but the script picks them up:

- **`fp-lib-table`, `sym-lib-table`** — bare-name match. Always Class C.
- **Anything matched by `--extra-content-glob`** — promoted to Class C even if its extension would otherwise put it in B.
- **Anything matched by `--exclude-glob`** — demoted to Class A regardless of extension.

## Things not in this taxonomy (intentional)

- **Gerber `.txt` drill files**: KiCad emits drill data as `.drl`, not `.txt`. If your toolchain produces `.txt` drill output and bundles it with your manufacturing pack, add `--extra-content-glob "*-drill.txt"` to *include* it (no — wait, you want it byte-stable; pass `--exclude-glob "*-drill.txt"` to keep its filename out of the rename, or just leave it alone if the basename-substitution wouldn't match its name anyway).
- **3D model libraries (`.STEP` files inside `design/FOOTPRINTS/`)**: these are *footprint* models, not the project's own STEP export. Their filenames usually don't contain the project basename, so the find/replace pairs won't match them and they pass through untouched. If you've stuck a customer-named STEP into a footprint folder, the script will rename it; that's almost certainly not what you want, so pass `--exclude-glob "design/FOOTPRINTS/*"`.
- **Simulation files (`.asc`, `.plt`, `sim_*.txt`)**: these typically don't carry the project basename in their filenames (Kevin's convention uses descriptors like `sim_latchlogic_*` instead). They'll only be renamed if a pair happens to match. Their contents *aren't* edited by default (no entry in `TEXT_EDIT_EXTS`). If a simulation file does need its contents updated, pass `--extra-content-glob "simulation/*.asc"`.