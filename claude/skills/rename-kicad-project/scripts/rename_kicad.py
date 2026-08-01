#!/usr/bin/env python3
"""
rename_kicad.py — Rename a KiCad project end-to-end.

Renames files on disk and updates internal references inside KiCad source files
(.kicad_pro, .kicad_sch, .kicad_pcb, etc.) and human-readable documentation
(README.md and similar). Manufacturing artifacts (Gerbers, drill, STEP, images,
PDFs, CSVs, archives) are renamed but their contents are left byte-identical,
because they're contractual deliverables generated from the source.

Usage:
    python rename_kicad.py --base-dir <project-root> \\
        --pair "<old>=<new>" [--pair "<old>=<new>"]... \\
        [--filename-pair "<old>=<new>"] \\
        [--content-pair "<old>=<new>"] \\
        [--dry-run] [--no-content] [--no-git] \\
        [--allow-project-id-change] \\
        [--exclude-glob PATTERN] [--extra-content-glob PATTERN]

Pairs are applied in the order given on the command line. This matters when a
later pair fixes up an awkward fragment left by an earlier pair (e.g. doing
"USB Dongle"="Cellular Gateway" first then "USB Cellular Gateway"="Cellular
Gateway" to fix the leading "USB " left over).

Exit codes: 0 success, 1 user error (bad args, collision, parse failure),
2 unexpected internal error.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


# ---------------------------------------------------------------------------
# File classification
# ---------------------------------------------------------------------------

# Files matching any of these glob patterns are skipped entirely — never
# renamed, never edited. These are either VCS metadata, KiCad's own runtime
# artifacts, or local-history backups that aren't part of the design.
SKIP_PATTERNS: tuple[str, ...] = (
    ".git/*",
    "*/.git/*",
    ".gitignore",
    ".gitmodules",
    ".gitattributes",
    "*.lck",
    "~*.lck",
    ".history/*",
    "*/.history/*",
    "*-backups/*",
    "*.bak",
)

# Files with these extensions are renamed but their contents are left
# byte-identical. They're downstream artifacts whose bytes must match what
# was generated from the KiCad source.
BINARY_RENAME_ONLY_EXTS: frozenset[str] = frozenset({
    ".gbr", ".gbrjob", ".drl",
    ".step", ".stp", ".stl", ".wrl",
    ".png", ".jpg", ".jpeg", ".svg", ".pdf", ".dxf",
    ".csv",
    ".zip", ".tar", ".tgz", ".7z",
    ".xlsx", ".xls",
})

# Files with these extensions get both filename rename AND content edits.
TEXT_EDIT_EXTS: frozenset[str] = frozenset({
    ".kicad_pro", ".kicad_prl",
    ".kicad_sch", ".kicad_pcb",
    ".kicad_dru", ".kicad_sym", ".kicad_mod", ".kicad_wks",
    ".md", ".txt",
})

# Filenames (no extension) that should be treated as text-edit even though
# they have no extension.
TEXT_EDIT_BARE_NAMES: frozenset[str] = frozenset({
    "fp-lib-table",
    "sym-lib-table",
})

# Regex to detect Kevin's PTXXXY project-ID prefix (e.g. PT140A, PT321B).
# Case-insensitive so we catch both "PT140A" in README and "pt140a" in
# filenames.
PROJECT_ID_RE = re.compile(r"\bPT\d{3}[A-Z]\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Pair:
    """A find/replace pair, with scope flags."""
    old: str
    new: str
    apply_to_filenames: bool
    apply_to_contents: bool

    def __post_init__(self) -> None:
        if not self.old:
            raise ValueError("Empty 'old' in find/replace pair")
        if self.old == self.new:
            raise ValueError(
                f"Pair {self.old!r}={self.new!r} is a no-op (old == new)"
            )


@dataclass
class FileAction:
    """What we plan to do with a single file."""
    src: Path
    dst: Path  # equals src if no rename
    rename: bool
    content_edits: int = 0  # number of replacements that would land
    content_preview: list[tuple[str, str]] = field(default_factory=list)
    skip_reason: str | None = None


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_pair(spec: str, *, filename: bool, contents: bool) -> Pair:
    """Parse a 'old=new' string, allowing '=' inside the new portion."""
    if "=" not in spec:
        raise argparse.ArgumentTypeError(
            f"Pair {spec!r} must be of the form OLD=NEW"
        )
    old, _, new = spec.partition("=")
    return Pair(old=old, new=new,
                apply_to_filenames=filename, apply_to_contents=contents)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rename_kicad",
        description=__doc__.split("\n\n", 1)[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("\n\n", 1)[1] if "\n\n" in __doc__ else "",
    )
    p.add_argument("--base-dir", required=True, type=Path,
                   help="Project root directory (contains the .kicad_pro file)")
    p.add_argument("--pair", action="append", default=[],
                   metavar="OLD=NEW",
                   help="Find/replace pair applied to BOTH filenames and "
                        "editable file contents. Repeat for multiple pairs. "
                        "Pairs are applied in order.")
    p.add_argument("--filename-pair", action="append", default=[],
                   metavar="OLD=NEW",
                   help="Find/replace pair applied ONLY to filenames.")
    p.add_argument("--content-pair", action="append", default=[],
                   metavar="OLD=NEW",
                   help="Find/replace pair applied ONLY to file contents.")
    p.add_argument("--dry-run", action="store_true",
                   help="Show the plan without modifying anything.")
    p.add_argument("--no-content", action="store_true",
                   help="Skip all content edits — rename files only. The "
                        "project will not open cleanly until references are "
                        "fixed by some other means; only use this if you "
                        "intend to do that yourself.")
    p.add_argument("--no-git", action="store_true",
                   help="Use plain mv even if files are tracked in git. "
                        "Default is to use 'git mv' for tracked files so "
                        "rename history is preserved.")
    p.add_argument("--allow-project-id-change", action="store_true",
                   help="Permit pairs that would alter a PTXXXY project-ID "
                        "token. Off by default to protect against accidents.")
    p.add_argument("--exclude-glob", action="append", default=[],
                   metavar="PATTERN",
                   help="Skip any path matching this glob. Repeatable. "
                        "Matched against path relative to --base-dir.")
    p.add_argument("--extra-content-glob", action="append", default=[],
                   metavar="PATTERN",
                   help="Treat any path matching this glob as content-editable "
                        "(in addition to the built-in text extensions). "
                        "Repeatable.")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress per-file progress; show only summary.")
    return p


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    args = build_parser().parse_args(argv)

    if not args.pair and not args.filename_pair and not args.content_pair:
        raise SystemExit(
            "error: at least one --pair / --filename-pair / --content-pair "
            "is required"
        )

    pairs: list[Pair] = []
    for spec in args.pair:
        pairs.append(parse_pair(spec, filename=True, contents=True))
    for spec in args.filename_pair:
        pairs.append(parse_pair(spec, filename=True, contents=False))
    for spec in args.content_pair:
        pairs.append(parse_pair(spec, filename=False, contents=True))
    args.pairs = pairs

    if not args.base_dir.is_dir():
        raise SystemExit(f"error: --base-dir {args.base_dir} is not a directory")
    args.base_dir = args.base_dir.resolve()

    return args


# ---------------------------------------------------------------------------
# PTXXXY guard
# ---------------------------------------------------------------------------

def project_id_safety_check(pairs: Iterable[Pair],
                            allow_change: bool) -> list[str]:
    """Return a list of warning strings; raise SystemExit if blocked."""
    warnings: list[str] = []
    for pair in pairs:
        old_ids = set(m.group(0).upper() for m in PROJECT_ID_RE.finditer(pair.old))
        new_ids = set(m.group(0).upper() for m in PROJECT_ID_RE.finditer(pair.new))
        if old_ids and old_ids != new_ids:
            msg = (
                f"pair {pair.old!r}={pair.new!r} alters project-ID prefix: "
                f"{sorted(old_ids)} -> {sorted(new_ids) or '(none)'}"
            )
            if allow_change:
                warnings.append("WARN " + msg)
            else:
                raise SystemExit(
                    "error: " + msg
                    + "\n       Pass --allow-project-id-change if this is "
                    "intentional. The project-ID prefix is normally treated "
                    "as immutable to prevent accidents."
                )
    return warnings


# ---------------------------------------------------------------------------
# File walking and classification
# ---------------------------------------------------------------------------

def is_skipped(rel_path: str, exclude_globs: Iterable[str]) -> bool:
    for pat in SKIP_PATTERNS:
        if fnmatch.fnmatch(rel_path, pat):
            return True
        if fnmatch.fnmatch(rel_path, "*/" + pat):
            return True
    for pat in exclude_globs:
        if fnmatch.fnmatch(rel_path, pat):
            return True
    return False


def is_text_editable(path: Path, extra_globs: Iterable[str],
                     base_dir: Path) -> bool:
    if path.suffix.lower() in TEXT_EDIT_EXTS:
        return True
    if path.name in TEXT_EDIT_BARE_NAMES:
        return True
    rel = str(path.relative_to(base_dir))
    for pat in extra_globs:
        if fnmatch.fnmatch(rel, pat):
            return True
    return False


def is_binary_artifact(path: Path) -> bool:
    return path.suffix.lower() in BINARY_RENAME_ONLY_EXTS


def walk_project(base_dir: Path, exclude_globs: list[str]) -> list[Path]:
    """Yield every file under base_dir that isn't on the skip list."""
    found: list[Path] = []
    for root, dirs, files in os.walk(base_dir):
        # Prune .git and .history for speed (huge perf win on big repos)
        dirs[:] = [d for d in dirs if d not in (".git", ".history")]
        for fname in files:
            full = Path(root) / fname
            rel = str(full.relative_to(base_dir))
            if is_skipped(rel, exclude_globs):
                continue
            found.append(full)
    return sorted(found)


# ---------------------------------------------------------------------------
# Rename / edit application
# ---------------------------------------------------------------------------

def apply_pairs_to_string(s: str, pairs: list[Pair],
                          *, scope: str) -> tuple[str, int]:
    """Apply pairs in order. Return (new_string, total_replacement_count).

    scope is "filename" or "contents".
    """
    out = s
    n = 0
    for pair in pairs:
        if scope == "filename" and not pair.apply_to_filenames:
            continue
        if scope == "contents" and not pair.apply_to_contents:
            continue
        if pair.old in out:
            n += out.count(pair.old)
            out = out.replace(pair.old, pair.new)
    return out, n


def plan_file(path: Path, base_dir: Path, pairs: list[Pair],
              *, edit_contents: bool, extra_content_globs: list[str]
              ) -> FileAction:
    rel = path.relative_to(base_dir)
    new_name, _ = apply_pairs_to_string(path.name, pairs, scope="filename")
    rename = (new_name != path.name)
    dst = path.with_name(new_name) if rename else path

    action = FileAction(src=path, dst=dst, rename=rename)

    # Content edits only happen on files we recognise as text-editable.
    if edit_contents and is_text_editable(path, extra_content_globs, base_dir):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            action.skip_reason = (
                f"could not decode {rel} as UTF-8; left untouched"
            )
            return action

        new_text, n = apply_pairs_to_string(text, pairs, scope="contents")
        action.content_edits = n
        if n > 0:
            # Keep a small preview of distinct (old -> new) snippets that
            # actually fired, so the dry-run report is informative.
            seen: set[str] = set()
            for pair in pairs:
                if not pair.apply_to_contents:
                    continue
                if pair.old in text and pair.old not in seen:
                    seen.add(pair.old)
                    action.content_preview.append((pair.old, pair.new))

    return action


def is_tracked_by_git(path: Path, base_dir: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(path)],
            cwd=base_dir, capture_output=True, text=True, check=False,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def in_git_repo(base_dir: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=base_dir, capture_output=True, text=True, check=False,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"
    except FileNotFoundError:
        return False


def do_rename(action: FileAction, base_dir: Path,
              *, use_git: bool, dry_run: bool) -> None:
    if action.dst.exists() and action.dst != action.src:
        raise SystemExit(
            f"error: destination {action.dst} already exists; refusing to "
            f"overwrite. Resolve the collision and re-run."
        )
    if dry_run:
        return
    if use_git and is_tracked_by_git(action.src, base_dir):
        subprocess.run(
            ["git", "mv", str(action.src), str(action.dst)],
            cwd=base_dir, check=True,
        )
    else:
        action.src.rename(action.dst)


def do_content_edit(action: FileAction, pairs: list[Pair],
                    *, dry_run: bool) -> None:
    if action.content_edits == 0:
        return
    target = action.dst if action.rename else action.src
    if dry_run:
        return
    text = target.read_text(encoding="utf-8")
    new_text, _ = apply_pairs_to_string(text, pairs, scope="contents")
    target.write_text(new_text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify_kicad_pro(base_dir: Path) -> list[str]:
    """Sanity-check that any .kicad_pro file in the tree still parses as JSON.

    Returns a list of error strings (empty == all good).
    """
    errors: list[str] = []
    for p in base_dir.rglob("*.kicad_pro"):
        if any(part in (".git", ".history") for part in p.parts):
            continue
        try:
            json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"{p.relative_to(base_dir)}: invalid JSON ({exc})")
        except UnicodeDecodeError as exc:
            errors.append(f"{p.relative_to(base_dir)}: bad encoding ({exc})")
    return errors


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # Safety: project-ID guard first, before any FS operations.
    pid_warnings = project_id_safety_check(
        args.pairs, allow_change=args.allow_project_id_change
    )
    for w in pid_warnings:
        print(w, file=sys.stderr)

    use_git = (not args.no_git) and in_git_repo(args.base_dir)

    files = walk_project(args.base_dir, args.exclude_glob)
    actions: list[FileAction] = []
    for f in files:
        action = plan_file(
            f, args.base_dir, args.pairs,
            edit_contents=not args.no_content,
            extra_content_globs=args.extra_content_glob,
        )
        actions.append(action)

    # ---- Plan summary ----
    rename_count = sum(1 for a in actions if a.rename)
    edit_count = sum(1 for a in actions if a.content_edits > 0)
    total_edits = sum(a.content_edits for a in actions)

    print(f"Project root : {args.base_dir}")
    print(f"Git mode     : {'on (git mv)' if use_git else 'off (plain mv)'}")
    print(f"Pairs:")
    for pair in args.pairs:
        scope = []
        if pair.apply_to_filenames:
            scope.append("filename")
        if pair.apply_to_contents:
            scope.append("contents")
        print(f"  {pair.old!r} -> {pair.new!r}  [{','.join(scope)}]")
    print()
    print(f"Plan: {rename_count} rename(s), "
          f"{edit_count} file(s) with content changes "
          f"({total_edits} total replacements)")

    # ---- Per-file detail ----
    if not args.quiet:
        print()
        print("Renames:")
        for a in actions:
            if a.rename:
                src = a.src.relative_to(args.base_dir)
                dst = a.dst.relative_to(args.base_dir)
                cls = "edit" if (a.content_edits > 0) else (
                    "binary" if is_binary_artifact(a.src) else "rename-only"
                )
                print(f"  [{cls}] {src} -> {dst}")
        print()
        print("Content edits:")
        for a in actions:
            if a.content_edits > 0:
                target = (a.dst if a.rename else a.src).relative_to(args.base_dir)
                print(f"  {target}: {a.content_edits} replacement(s)")
                for old, new in a.content_preview[:3]:
                    print(f"      {old!r} -> {new!r}")
        if any(a.skip_reason for a in actions):
            print()
            print("Skipped:")
            for a in actions:
                if a.skip_reason:
                    print(f"  {a.skip_reason}")

    if args.dry_run:
        print()
        print("Dry-run only. No changes written.")
        return 0

    # ---- Execute ----
    # Rename first (so collisions abort before any edits land), then edit
    # contents at the destination path. If a crash happens between the two
    # passes, the worst-case state is a renamed file with old contents —
    # rerunning the same command picks up where we left off because the
    # pairs are idempotent (string.replace on already-replaced text is a
    # no-op).
    for a in actions:
        if a.rename:
            do_rename(a, args.base_dir, use_git=use_git, dry_run=False)
    for a in actions:
        do_content_edit(a, args.pairs, dry_run=False)

    # ---- Verify ----
    errors = verify_kicad_pro(args.base_dir)
    if errors:
        print()
        print("WARNING: post-rename verification found problems:", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1

    print()
    print(f"Done. {rename_count} renamed, {total_edits} text replacements "
          f"across {edit_count} file(s).")
    print("Next: open the project in KiCad and re-save it (KiCad will "
          "rewrite a few internal caches; commit that as a separate commit).")
    return 0


if __name__ == "__main__":  # pragma: no cover
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"unexpected error: {exc}", file=sys.stderr)
        sys.exit(2)