---
name: rename-kicad-project
description: Rename a KiCad project end-to-end — files on disk (KiCad sources, manufacturing outputs, STEP, images, README) AND internal references inside the source files (project name, hierarchical sheet filenames, title block, free text). Use whenever the user wants to rename, retitle, rebrand, fork, or "change the name of" a KiCad project, board, or PCB design — even if "skill" isn't mentioned. Triggers include "rename my KiCad project", "change the name of this PCB", "board renamed from X to Y, sort the files", "rebrand the project", "fork this design as Y", "the project ID's the same but the descriptor changed". Works for any KiCad project; gives extra protection to those using the PTXXXY_namedescription_subname.ext convention (project-ID prefix that shouldn't change by accident). Preserves git history via git mv, leaves manufacturing outputs (Gerbers/drill/STEP/CSV/PDFs) byte-identical, and refuses to alter the PTXXXY project-ID unless the user opts in.
---

# Rename KiCad Project

A skill for renaming a KiCad project — the files on disk and the internal references inside `.kicad_pro`, `.kicad_sch`, `.kicad_pcb`, etc. — without breaking anything that downstream consumers (manufacturers, the customer, archived design packs) rely on.

## What this skill is for

The user has a working KiCad project and wants to rename it. The basename (`pt140a_vsmsc_8sim_4g_usb_dongle`) appears in dozens of places: every filename, the `.kicad_pro` `top_level_sheets` block, every `(project "...")` instance in every `.kicad_sch`, every `(sheetfile "...")` reference in `.kicad_pcb` and `.kicad_sch`, image links in the README, etc. Doing this by hand is tedious and error-prone.

There's a second, separate problem: a human-readable title (`8-SIM 4G USB Dongle`) usually appears in the schematic title block and in free-text annotations on the schematic/PCB. That's a different string from the filename basename and needs its own find/replace pair.

This skill handles both, while:
- Preserving git history by using `git mv` when the project is in a git repo.
- Leaving manufacturing outputs (`.gbr`, `.drl`, `.gbrjob`, `.zip`, `.step`, `.png`, `.pdf`, `.csv` position/BOM files) **byte-identical** — only their filenames change. Manufacturing files are a contractual deliverable; their contents must not be silently mutated.
- Protecting the PTXXXY project-ID prefix from being changed by accident. If a find/replace pair would alter a `PT\d{3}[A-Z]` token, the script refuses unless the user explicitly opts in with `--allow-project-id-change`.
- Skipping `.git/`, `.gitignore`, `.gitmodules`, KiCad lock files (`~*.lck`), and VS Code `.history/` backups entirely.

## File-class taxonomy (summary)

The helper script splits every file into one of three buckets. You don't need to memorise the full lists — they're in `references/file_classes.md` if you need to extend or debug them — but the categories matter for explaining the plan to the user:

- **Skip entirely**: `.git/`, lock files (`*.lck`, `~*.lck`), `.history/` backups. Never touched.
- **Rename only, contents byte-identical**: manufacturing outputs and exports — Gerbers (`.gbr`, `.gbrjob`), drill (`.drl`), mechanical (`.step`, `.stl`), images (`.png`, `.jpg`, `.svg`), PDFs, CSVs (treated as manufacturing artifacts by default), archive bundles (`.zip` etc.). These are contractual deliverables generated *from* the KiCad source — editing them silently would desync them from their source and break the audit trail.
- **Rename + content edit**: KiCad sources (`.kicad_pro`, `.kicad_sch`, `.kicad_pcb`, `.kicad_dru`, `.kicad_prl`, `.kicad_sym`, `.kicad_mod`, `.kicad_wks`), library tables (`fp-lib-table`, `sym-lib-table`), and documentation (`*.md`, `*.txt`).

If the project has unusual files (e.g., a custom build script that hardcodes the project basename), the user can pass `--extra-content-glob "scripts/*.py"` to promote them into the content-edit class. Conversely, `--exclude-glob` demotes anything to the skip class.

## Workflow

Don't try to do this by hand with `Edit` calls — the kicad_pcb file alone has hundreds of references and you'll miss some. Use the bundled script.

### Step 1 — discover the project

Locate the root `.kicad_pro` file. The basename of that file (without extension) is the **project basename** that needs to change. There should normally be exactly one `.kicad_pro` per project; if you find more, ask the user which one is in scope.

```bash
find <project-root> -name "*.kicad_pro" -not -path "*/.git/*"
```

Show the user the basename you found and confirm it's the one they want to rename.

### Step 2 — agree the find/replace pairs with the user

A KiCad rename usually needs **two pairs**, sometimes more:

1. **The filename pair** — snake_case basename, e.g. `usb_dongle` → `cellular_gateway`. This is the bit of the filename that changes. The PTXXXY prefix and the customer/business segment usually stay put.
2. **The human-readable pair** — Title Case, e.g. `USB Dongle` → `Cellular Gateway`. This appears in the schematic/PCB title block and in free-text annotations.

Sometimes a third pair is needed to fix awkward fragments left by the second pair. Example: in PT140A's README, the description read `"USB Dongle for 8off SIM Dongle"`. After applying `USB Dongle` → `Cellular Gateway`, this became `"USB Cellular Gateway for 8off SIM Dongle"` — technically a correct mechanical replace, but the leading `"USB "` is now a fossil. If the user cares about that, add a follow-up pair like `USB Cellular Gateway` → `Cellular Gateway`. Pairs are applied in order, so a fixup pair after the main pair will land on the just-rewritten text.

Ask the user the smallest set of questions that nails this down. A reasonable opener:

> "I'll rename `pt140a_vsmsc_8sim_4g_usb_dongle` to `pt140a_vsmsc_8sim_4g_cellular_gateway` for the filenames. For the human-readable title in the schematic title block — what's the new title? (e.g. `8-SIM 4G USB Dongle` → ?)"

Don't invent the new title yourself unless the user has clearly told you what it is.

### Step 3 — show the plan, get explicit confirmation

Run the helper in `--dry-run` mode and show the user:
- The list of files that will be renamed (grouped by file-class).
- A sample of content edits per editable file (first 3-5 hits is enough; the script reports the total count).
- Any warnings (e.g., a pair that would touch the PTXXXY prefix).

```bash
python <skill-dir>/scripts/rename_kicad.py \
  --base-dir <project-root> \
  --pair "usb_dongle=cellular_gateway" \
  --content-pair "USB Dongle=Cellular Gateway" \
  --dry-run
```

Replace `<skill-dir>` with this skill's installed path — Claude Code and Cowork both expose this in the available-skills metadata. (Quick way to find it from a shell: `find ~ /sessions -name SKILL.md -path "*rename-kicad-project*"` or whatever search makes sense for the platform.)

Wait for the user's "yes, go" before doing the live run. If they want to tweak a pair, re-run dry until they're happy.

### Step 4 — execute

Drop the `--dry-run` flag and run for real. The script:
- Renames files via `git mv` if the file is tracked, otherwise plain `mv`.
- Edits content files in-place using the configured pairs.
- Writes a summary to stdout (number of renames, number of edits per file).
- Refuses to overwrite an existing destination filename — bail out and ask the user how to resolve a collision.
- Verifies any `.kicad_pro` files still parse as JSON before exiting.

The changes land in the working tree but are **not committed**. Leaving the commit to the user is deliberate — they may want to inspect `git diff` before accepting hundreds of replacements, and they may want to split the result into "rename files" + "update internal references" commits (which is a common pattern, useful for reviewers).

### Step 5 — verify

The script's own JSON-validity check covers the most common breakage. One additional check is worth running:

```bash
grep -rn "<old_basename>" --include="*.kicad_*" --include="*.md" <project-root>
```

Expect zero hits. If something turns up, it's a string the user's pairs didn't reach — show it to them and ask whether to add another pair or leave it.

After that, tell the user to **open the project in KiCad and re-save it**. KiCad rewrites a few internal caches (timestamps, the `.kicad_prl` UI state) on next open/save; that re-save belongs in its own commit so the script's deterministic rename stays clean and auditable.

## Common variations the user might ask for

**"Just rename the files, don't touch the contents."**
Use `--filename-pair "OLD=NEW"` (instead of `--pair`). One flag, one pair, filenames-only — that's it. (`--no-content` exists too as a global override but it's belt-and-braces; you don't need both.) The KiCad project will break in this intermediate state — sheet references won't resolve — so this is unusual, but useful if the user wants to do the content edits manually in KiCad's UI.

**"I want to change the project ID too — PT140A → PT141B."**
Add `--allow-project-id-change` and include the PTXXXY tokens in your pairs:
```
--pair "pt140a=pt141b" --pair "PT140A=PT141B"
```
Note both cases — KiCad source uses lowercase, the README and title block use uppercase.

**"Don't touch the README."**
Add `--exclude-glob "README*" --exclude-glob "*.md"`.

**"There's a custom build script that hardcodes the basename."**
Add `--extra-content-glob "scripts/*.py"` (or whatever the path is).

**"This isn't in a git repo / I don't want git mv."**
Add `--no-git`. Plain `mv` is used; you lose the rename-detection in `git log --follow` but everything else still works.

## Edge cases worth flagging to the user

- **Library subdirectories** (`design/optimised_kicad-libraries/`, `design/FOOTPRINTS/`, `design/LIBRARIES/`) often contain `.kicad_sym` and `.kicad_mod` files whose names are deliberately not tied to the project basename. The script only renames a file if the find/replace pairs actually match its filename, so library files normally pass through untouched. Mention this if the user is worried.
- **Lock files** (`~*.lck`) sometimes survive a crashed KiCad session. The script skips them. If the user has KiCad open while running this, ask them to close it first.
- **Sub-sheet files** like `pt140a_vsmsc_8sim_4g_usb_dongle_latch.kicad_sch` — the trailing `_latch` is the *subname*. The skill renames these correctly because the pair matches the basename portion regardless of what suffix follows.
- **Outputs in the project root** (top-level `*_schematic.pdf`, `*_layout.pdf`) — these are exports that get renamed but not edited. Manufacturing keeps the PDF copies bundled with the design pack, so the rename matters even though the bytes stay the same.

## What "success" looks like

A successful run leaves you with:
- All filenames updated to use the new basename / new human title where the pairs match.
- Every reference inside KiCad source files (schematic instances, PCB sheetfile lines, project file's `top_level_sheets`, etc.) updated consistently.
- Manufacturing artifacts byte-identical, just renamed.
- `.git/`, lock files, and history backups untouched.
- A clean `git status` showing one big batch of rename + edit operations, ready to commit as one or two commits (e.g., `"rename files"` then `"update internal references"` if you want them separated, which is what Kevin actually did on PT140A).
- The KiCad project opens cleanly with no missing-sheet errors.

## Reference

The script lives at `scripts/rename_kicad.py`. Read its `--help` output if you need any flag details not covered above. The exhaustive file-classification rules are in `references/file_classes.md` if you need to extend them.