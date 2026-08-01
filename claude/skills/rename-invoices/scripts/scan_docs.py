#!/usr/bin/env python3
"""
Inventory a folder of invoice PDFs and image receipts, and emit JSON.

This script does the deterministic half of the job: find the files, classify
them by filename, and hand back a stable, sorted list. It deliberately does
NOT read file contents — Claude does that with the Read tool, which handles
PDFs and images natively. That split is why this has no dependencies at all:
Python standard library only, no pdftotext, no pypdf, no OCR engine, nothing
to install. It behaves identically on Windows and Linux.

For each matching file in the folder (non-recursive by default), outputs:
    - path:            absolute path, native to this OS
    - filename:        basename
    - kind:            "pdf" or "image"
    - already_renamed: bool — filename matches the full naming pattern
    - not_ready:       bool — filename starts with a "not yet ready" marker
                               (TO GET, GET-INVOICE, TODO, DRAFT, TEMP, ...)
    - size_bytes:      int — 0 means a broken/empty file, not worth reading

Extension matching is case-insensitive: .pdf/.PDF, .jpg/.jpeg, .png. Files
with any other extension are silently omitted (this is deliberate — the skill
ignores .txt notes, .heic photos and other clutter).

Usage:
    python scan_docs.py "<folder>"                 # inventory folder
    python scan_docs.py "<folder>" --recursive     # include subfolders
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Iterator

# Full naming pattern, for both document types:
#   YYYY-MM-DD Company [optional seller] - Description - Invoice NUMBER.<ext>
#   YYYY-MM-DD Company [optional seller] - Description - Receipt[ NUMBER].<ext>
# The trailing token describes the document, not the file type, so a scanned
# invoice saved as .jpg still ends "- Invoice NNN.jpg". Receipts may legitimately
# carry no number at all, which is why theirs is optional.
# This check is deliberately lax; the skill prompt makes the "is every field
# actually correct" judgement call because that needs semantic understanding.
_RENAMED_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2} .+ - .+ - (?:Invoice \S.*|Receipt(?: \S.*)?)\.(?:pdf|jpe?g|png)$",
    re.IGNORECASE,
)

# Prefixes Kevin uses to flag "this isn't ready to rename yet — I still need to
# download the real invoice". Add more if his conventions grow.
_NOT_READY_PREFIXES = (
    "TO GET",
    "GET-INVOICE",
    "GET INVOICE",
    "TODO",
    "DRAFT",
    "TEMP",
    "WIP",
)

# Only these extensions are scanned, compared case-insensitively.
# .jpeg is included as a spelling of .jpg; .heic/.webp/.tif are deliberately out.
_PDF_SUFFIXES = (".pdf",)
_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png")
_SCAN_SUFFIXES = _PDF_SUFFIXES + _IMAGE_SUFFIXES


def _looks_not_ready(filename: str) -> bool:
    upper = filename.upper().lstrip()
    return any(upper.startswith(p) for p in _NOT_READY_PREFIXES)


def _looks_renamed(filename: str) -> bool:
    return bool(_RENAMED_RE.match(filename))


def _iter_scannable(folder: Path, recursive: bool) -> Iterator[Path]:
    """
    Yield PDFs and images, matching the extension case-insensitively.

    glob("*.pdf") is case-sensitive on Linux and would silently miss the .PDF
    files that some suppliers hand out, so filter on the suffix instead.
    """
    entries = folder.rglob("*") if recursive else folder.iterdir()
    for entry in entries:
        if entry.is_file() and entry.suffix.lower() in _SCAN_SUFFIXES:
            yield entry


def scan(folder: Path, recursive: bool = False) -> list[dict]:
    results: list[dict] = []
    # sorted by name for stable, diffable output
    for path in sorted(_iter_scannable(folder, recursive), key=lambda p: p.name.lower()):
        results.append(
            {
                "path": str(path.resolve()),
                "filename": path.name,
                "kind": "image" if path.suffix.lower() in _IMAGE_SUFFIXES else "pdf",
                "already_renamed": _looks_renamed(path.name),
                "not_ready": _looks_not_ready(path.name),
                "size_bytes": path.stat().st_size,
            }
        )
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("folder", help="Folder containing invoice PDFs and image receipts")
    ap.add_argument("--recursive", action="store_true", help="Recurse into subfolders")
    args = ap.parse_args()

    folder = Path(args.folder).expanduser()
    if not folder.exists():
        print(json.dumps({"error": f"Folder not found: {folder}"}), file=sys.stderr)
        return 2
    if not folder.is_dir():
        print(json.dumps({"error": f"Not a directory: {folder}"}), file=sys.stderr)
        return 2

    results = scan(folder, recursive=args.recursive)
    json.dump(
        {"folder": str(folder.resolve()), "count": len(results), "files": results},
        sys.stdout,
        indent=2,
        ensure_ascii=False,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
