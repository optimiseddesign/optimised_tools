#!/usr/bin/env python3
"""
Scan a folder of invoice PDFs and emit JSON describing each file.

For each PDF in the folder (non-recursive by default), outputs:
    - path:            absolute path
    - filename:        basename
    - already_renamed: bool — filename matches the full naming pattern
    - not_ready:       bool — filename starts with a "not yet ready" marker
                               (TO GET, GET-INVOICE, TODO, DRAFT, TEMP, ...)
    - text:            first ~6000 chars of extracted text (empty on error)
    - text_empty:      bool — extraction succeeded but the PDF carries no text
                               layer (i.e. a scan/photo). The skill handles
                               these by reading the PDF directly rather than
                               skipping them.
    - error:           extraction error message, or null
    - windows_path:    best-effort Windows path (for computer:// links),
                       resolved from the symlink target when possible
                       — null if it couldn't be determined

Extension matching is case-insensitive: both .pdf and .PDF are scanned.
Files with any other extension are silently omitted (this is deliberate —
the skill ignores .txt notes, image receipts and other clutter).

Usage:
    python3 scan_pdfs.py "<folder>"                 # scan folder
    python3 scan_pdfs.py "<folder>" --recursive     # include subfolders
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterator

# Full naming pattern: YYYY-MM-DD Company [optional seller] - Description - Invoice NUMBER.pdf
# We use a fairly lax check here; the skill prompt does the "is every field actually
# correct" judgement call because that needs semantic understanding.
_RENAMED_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2} .+ - .+ - Invoice \S.*\.pdf$",
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
_PDF_SUFFIXES = (".pdf",)

_MAX_TEXT_CHARS = 10000  # plenty for extracting header fields without bloating context
_MAX_PAGES = 6  # invoice header fields are always on page 1-2
_MIN_USEFUL_CHARS = 30  # below this, treat the text layer as absent, not merely short
_PDFTOTEXT_TIMEOUT_S = 20


def _has_usable_text(text: str) -> bool:
    return len(text.strip()) >= _MIN_USEFUL_CHARS


def _extract_text(pdf_path: Path) -> tuple[str, str | None]:
    """
    Return (text, error), text truncated to _MAX_TEXT_CHARS.

    An empty text with error=None means extraction worked but the PDF has no
    text layer (a scan or photo) — the caller reports that as text_empty.
    """
    errors: list[str] = []
    # Text from a tool that ran fine but found nothing. Held back so that a
    # later pdfplumber failure doesn't get misreported as an extraction error.
    empty_but_ok: str | None = None

    # Prefer pdftotext -layout: fastest, preserves column structure that helps the
    # LLM see where "Sold by" / "Invoice number" etc. sit on the page.
    if shutil.which("pdftotext"):
        try:
            proc = subprocess.run(
                ["pdftotext", "-layout", "-l", str(_MAX_PAGES), str(pdf_path), "-"],
                capture_output=True,
                timeout=_PDFTOTEXT_TIMEOUT_S,
                check=True,
            )
            text = proc.stdout.decode("utf-8", errors="replace")
            if _has_usable_text(text):
                return text[:_MAX_TEXT_CHARS], None
            # pdftotext exits 0 on an image-only PDF and prints nothing, so an
            # empty result here is not an error — but pdfplumber occasionally
            # recovers text it misses, so fall through and try anyway.
            empty_but_ok = text
        except subprocess.CalledProcessError as e:
            errors.append(
                f"pdftotext failed: {e.stderr.decode('utf-8', errors='replace')[:200]}"
            )
        except subprocess.TimeoutExpired:
            errors.append(f"pdftotext timed out after {_PDFTOTEXT_TIMEOUT_S}s")
        except Exception as e:  # noqa: BLE001
            errors.append(f"pdftotext error: {e}")
    else:
        errors.append("pdftotext not available")

    # Fallback: pdfplumber
    try:
        import pdfplumber  # type: ignore

        with pdfplumber.open(pdf_path) as pdf:
            chunks = [(page.extract_text() or "") for page in pdf.pages[:_MAX_PAGES]]
        return "\n".join(chunks)[:_MAX_TEXT_CHARS], None
    except Exception as e:  # noqa: BLE001
        if empty_but_ok is not None:
            # pdftotext ran cleanly; the PDF simply has no text layer.
            return empty_but_ok[:_MAX_TEXT_CHARS], None
        errors.append(f"pdfplumber error: {e}")

    return "", "; ".join(errors)


def _looks_not_ready(filename: str) -> bool:
    upper = filename.upper().lstrip()
    return any(upper.startswith(p) for p in _NOT_READY_PREFIXES)


def _looks_renamed(filename: str) -> bool:
    return bool(_RENAMED_RE.match(filename))


def _best_effort_windows_path(pdf_path: Path) -> str | None:
    """
    In Cowork, user-selected folders are mounted under /sessions/<session>/mnt/...
    The mount itself is often a symlink to the real Windows path or a host
    path; readlink gives us something useful when it is. Otherwise we return
    None and let the caller fall back to the mount path.
    """
    try:
        # Walk up to find the mount root (first symlink ancestor)
        for ancestor in [pdf_path, *pdf_path.parents]:
            if ancestor.is_symlink():
                target = os.readlink(ancestor)
                rel = pdf_path.relative_to(ancestor)
                joined = os.path.join(target, *rel.parts)
                # If target looks like a Windows path, keep backslashes
                if re.match(r"^[A-Za-z]:[\\/]", target):
                    return joined.replace("/", "\\")
                return joined
    except Exception:  # noqa: BLE001
        pass
    return None


def _iter_pdfs(folder: Path, recursive: bool) -> Iterator[Path]:
    """
    Yield PDF files, matching the extension case-insensitively.

    glob("*.pdf") is case-sensitive on Linux and would silently miss the .PDF
    files that some suppliers hand out, so filter on the suffix instead.
    """
    entries = folder.rglob("*") if recursive else folder.iterdir()
    for entry in entries:
        if entry.is_file() and entry.suffix.lower() in _PDF_SUFFIXES:
            yield entry


def scan(folder: Path, recursive: bool = False) -> list[dict]:
    results: list[dict] = []
    # sorted for stable output
    for pdf in sorted(_iter_pdfs(folder, recursive), key=lambda p: p.name.lower()):
        text, err = _extract_text(pdf)
        results.append(
            {
                "path": str(pdf.resolve()),
                "filename": pdf.name,
                "already_renamed": _looks_renamed(pdf.name),
                "not_ready": _looks_not_ready(pdf.name),
                "text": text,
                "text_empty": err is None and not _has_usable_text(text),
                "error": err,
                "windows_path": _best_effort_windows_path(pdf),
            }
        )
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("folder", help="Folder containing invoice PDFs")
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
