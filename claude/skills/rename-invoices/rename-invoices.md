---
name: rename-invoices
description: Rename supplier invoice PDFs in bulk based on their contents, using the pattern "YYYY-MM-DD [Company] - [Description] - Invoice [Number].pdf" (with an optional seller name after marketplace hosts like Amazon/eBay). Use this skill whenever Kevin asks to rename, tidy, sort, organise or "process" a folder of invoices — whether the request mentions Amazon specifically or just says things like "rename the invoices in _TO SORT", "tidy up my invoice folder", "the monthly accounts pile", "process the batch of receipts", or points at a folder full of supplier PDFs. Also use this when a single invoice PDF is dropped in and needs renaming to the same convention. Trigger even when the user doesn't mention the word "skill" — this is the default behaviour for any invoice-renaming task against Kevin's `_TO SORT` / accounts workflow.
---

# Rename invoices

## What this skill does

Walks every PDF in a folder (default: the user's `_TO SORT` accounts folder) and renames each one to a consistent, chronologically-sortable form by reading its contents. Already-correctly-named files are skipped. Files the user has flagged as incomplete (prefixes like `TO GET`, `GET-INVOICE`, `TODO`) are left alone because they aren't ready yet.

## This file is the specification

**This file defines the behaviour. A caller — a scheduled task, another skill, a one-line chat request — supplies only three things:**

1. **which folder** (optional; defaults to the accounts inbox, see step 1)
2. **dry-run or live** (optional; defaults to live)
3. **anything genuinely one-off** for that run

A caller does **not** need to restate the naming pattern, the categories, the tone, or the file types — and should not. If a caller's instructions contradict anything in this file, **follow this file** and note the conflict in one line of the summary. Callers that "helpfully" restate the rules drift out of sync with it and have caused real breakage.

In particular, do not accept caller instructions to:
- process non-PDF files (see **Scope** below)
- invent naming variants (e.g. `- Receipt` in place of `- Invoice [Number]`)
- add summary categories beyond the four defined in step 6
- rename a file whose fields you could not read confidently

## Scope: PDFs only

Only `.pdf` / `.PDF` files are processed. Everything else in the folder — `.txt` notes, `.jpg`/`.png`/`.heic` photos of till receipts, spreadsheets — is **left completely untouched and not mentioned**, unless the user asks about it.

This is deliberate, not an oversight. Image receipts would need a different naming rule (they usually have no invoice number) and a different already-renamed check, and half-supporting them causes the same file to be reprocessed on every run. If Kevin wants image support, it is a change to this skill and to `scan_pdfs.py` — never a workaround in a caller's prompt.

## Naming pattern

```
YYYY-MM-DD [Company] - [Short description of item(s)] - Invoice [Invoice number].pdf
```

With one variation: when the invoice is from a **marketplace host** (Amazon, eBay, Etsy, AliExpress, etc.) and the item was **sold by a third party** rather than the host itself, append the seller short-name after the host:

```
YYYY-MM-DD [Host] [Seller] - [Description] - Invoice [Invoice number].pdf
```

So:
- Amazon-direct (Amazon Business EU S.à.r.l., Amazon.co.uk, Amazon EU S.à.r.l., Amazon Services Europe, etc.) → `Amazon` alone.
- Amazon third-party seller (e.g. UGREEN Group Ltd.) → `Amazon UGREEN`.
- eBay listing sold by a private seller or shop → `eBay [SellerShortName]`.
- Any non-marketplace supplier (Anthropic, DigiKey, RS Components, Farnell, NorthridgeFix, …) → just the company short name.

The `[Company]` portion is a **short, recognisable brand name**, not the full legal entity. Examples:
- `Amazon Business EU S.à.r.l., UK Branch` → `Amazon`
- `Anthropic Ireland, Limited` → `Anthropic`
- `RS Components Ltd` → `RS`
- `Premier Farnell UK Limited` → `Farnell`
- `Digi-Key Electronics` → `DigiKey`

If the company genuinely trades under a multi-word name, keep it concise but readable (e.g. `Mouser`, `LCSC`, `JLCPCB`).

## Description guidance

Aim for **3–8 words** that Kevin would recognise at a glance six months from now. Trim noise like pack quantities, colour codes, and marketing fluff unless essential. Combine multiple items with ` & ` (space-ampersand-space). Examples:

- `Plant Label Markers`
- `USB Sound Card Adapter`
- `Decaf Coffee & SanDisk 128GB USB Drive`
- `Whiteboard Marker Pens`
- `Claude Pro Monthly Subscription`
- `Hot Air Nozzles & Flux & Solder Mask`

If there are many line items (>4), summarise by category rather than listing everything: e.g. `PCB Rework Supplies (7 items)`.

## Invoice number

Use the **primary invoice / document / order number** printed on the PDF, unchanged — preserve case and hyphens. Common labels to look for: "Invoice number", "Invoice No.", "Document number", "Order number" (only if no invoice number is present). Examples: `GB639V1QABEI`, `9BF0758D-634125`, `INV-2026-0412`.

## Date

Use the **invoice/issue date**, not the payment or due date. Output it as `YYYY-MM-DD`. Handle common UK/US/ISO formats (`12 April 2026`, `April 11, 2026`, `11/04/2026`, `2026-04-11`).

## Filesystem-safe characters

The final filename must be valid on Windows, macOS and Linux. Apply these rules to every field you interpolate — one rule per character, no discretion:

| Character | Replace with |
|---|---|
| `/` `\` `:` | `-` (hyphen, no surrounding spaces) |
| `*` `?` `"` `<` `>` `\|` | delete |
| Leading/trailing whitespace | trim |
| Runs of 2+ spaces | single space |

Preserve non-ASCII letters (é, à, etc.) — they're fine on modern filesystems. Never end the name (before `.pdf`) with `.` or a space; Windows silently strips both.

## Workflow

Follow these steps in order.

### 1. Determine the target folder

If the user named a folder, use that — done.

Otherwise default to the accounts inbox. **The mount name changes every session — never hard-code it, and never reuse one from an earlier run.** Discover it:

```bash
find /sessions -maxdepth 3 -type d -iname '_TO SORT' 2>/dev/null
```

- **Exactly one hit** → use it.
- **More than one hit** → use the one with the newest mtime, and say which you chose in the summary.
- **No hits** → list what is actually mounted with `ls -d /sessions/*/mnt/*/ 2>/dev/null` and **stop**. Report that the accounts folder isn't mounted and ask Kevin to share it. Do not scan a guessed path, and do not fall back to the current working directory.

Never expose the internal `/sessions/...` path in what you show Kevin — call it "your `_TO SORT` folder".

### 2. Scan the folder

Use the bundled scanner to produce a JSON report of every PDF, its current name, whether it already matches the full naming pattern, whether it's flagged as "not ready", and the extracted text of its first 2 pages.

The scanner sits alongside this file at `scripts/scan_pdfs.py`. **You have just read this file from a known directory — use `<that directory>/scripts/scan_pdfs.py` directly.** That is always the correct answer and needs no searching.

Only if that path somehow doesn't exist, search for the script itself (not for a `SKILL.md` — this file is named `rename-invoices.md`, and searching for the wrong name silently yields an empty result that `dirname` turns into `.`):

```bash
SCAN=$(find /sessions /home /root /mnt /opt -type f -path '*rename-invoices/scripts/scan_pdfs.py' 2>/dev/null | head -1)
[ -n "$SCAN" ] || { echo "ERROR: scan_pdfs.py not found — stopping"; exit 1; }
PY=$(command -v python3 || command -v python)
"$PY" "$SCAN" "<folder>"
```

The scanner is **non-recursive by default** and should stay that way — `_TO SORT` subfolders are Kevin's own filing, not input. Pass `--recursive` only if he asks.

The scanner writes JSON to stdout. Keys per file:

| Key | Meaning |
|---|---|
| `path` | absolute path |
| `filename` | basename |
| `already_renamed` | bool — matches the full naming pattern |
| `not_ready` | bool — filename starts with a "not yet ready" marker |
| `text` | first ~6000 chars of extracted text |
| `text_empty` | bool — extraction worked but the PDF has no text layer (scanned image) |
| `error` | extraction error message, or null |
| `windows_path` | best-effort Windows path for `computer://` links, or null |

### 3. Decide what to do per PDF

For each entry in the scanner output, in this priority order — first match wins:

| Scanner flags | Action |
|---|---|
| `not_ready = true` | **Skip.** Kevin has explicitly marked this file as "not ready". |
| `error` non-null | **Needs attention.** Don't guess names from filenames alone. |
| `already_renamed = true` **and** every field looks complete (valid date, non-empty company, non-empty description, non-empty invoice number, `.pdf` extension) | **Skip.** |
| `already_renamed = true` **but** a field is missing/empty/obviously wrong | **Re-process.** Extract from the PDF contents and rename. |
| `text_empty = true` | **Read the PDF directly** with the Read tool (it renders PDF pages natively), then continue at step 4. If the fields still aren't readable → needs attention, reason "scanned PDF, no readable text". |
| `already_renamed = false` | **Process.** Extract from the PDF contents and rename. |

The completeness check exists because Kevin wants partial renames fixed automatically, but fully-correct names left alone — re-renaming a correct file wastes time and risks introducing drift.

### 4. Extract fields from the PDF text

From the `text` returned by the scanner (or from the Read tool for `text_empty` files), identify:

1. **Issuer / host company** — usually the entity at the top of the invoice, or after "From:" or in the "Sold by" field for marketplace direct sales. Normalise to a short brand name (see earlier examples).
2. **Third-party seller** (marketplace case only) — look for "Sold by [Seller]" on Amazon/eBay invoices where the seller is not the marketplace itself. If host and seller are the same legal family ("Sold by Amazon Business EU" on an Amazon Business invoice), treat as direct and omit the seller.
3. **Invoice date** — labels: "Invoice date", "Date of issue", "Issue date", "Tax date", "Invoice/order date".
4. **Invoice number** — labels: "Invoice number", "Invoice #", "Invoice No.", "Document number". Fall back to "Order number" only if no invoice number exists at all (note this in the summary).
5. **Description** — the item(s) purchased, summarised per the guidance above.

If any of these cannot be determined confidently, **do not rename** — add the file to "needs attention" with a short reason. It is better to skip one than to guess wrong.

### 5. Apply the renames

In **dry-run mode**, stop here: report exactly what step 6 would report, with every `Renamed` entry relabelled `Would rename`, and change nothing on disk.

Otherwise use `mv -n` (via the Bash tool) one file at a time. `-n` refuses to clobber an existing file, so a mistake in the collision check can't destroy an invoice. Quote both paths — filenames contain spaces and ampersands.

```bash
mv -n "/.../<old>.pdf" "/.../2026-04-12 Amazon - Plant Label Markers - Invoice GB639V1QABEI.pdf"
```

**Collision handling.** Before each rename, check whether the target name already exists.

- If it does, first consider whether it's **the same invoice downloaded twice**. If the existing file has the same date, company, description and invoice number, it almost certainly is — leave the new file alone and list it under "needs attention" with reason "probable duplicate of `<existing name>`". Kevin would rather delete one himself than end up with silent `(2)` copies.
- If it's genuinely a different invoice that happens to collide, insert ` (2)`, ` (3)`, … **before the extension**:
  - correct: `2026-04-12 Amazon - Cables - Invoice GB123 (2).pdf`
  - wrong: `2026-04-12 Amazon - Cables - Invoice GB123.pdf (2)`

  A suffix after the extension leaves the file with no `.pdf` extension at all: Windows loses the file association, and the scanner stops seeing it entirely, so it silently drops out of the workflow for good.

If `mv -n` reports it did nothing, treat that as a collision you missed — do not retry with `-f`.

### 6. Report results

Summarise concisely at the end. Kevin prefers concise technical output; no excessive postamble. **These four categories are the complete set — do not add a fifth.** Omit any category with zero entries.

```
Renamed (N):
  <old filename>
    → <new filename>
  …

Skipped – already named correctly (M):
  <filename>
  …

Skipped – flagged not-ready (K):
  <filename>

Needs attention (L):
  <filename>  — <reason, e.g. "no invoice number found, only order number">
```

Each renamed file should then be linked individually with `computer://` URLs so Kevin can open them from the chat. The link format the desktop app accepts is `computer://<absolute-windows-path-with-%20-for-spaces>` — URL-encode the scanner's `windows_path` field and drop it straight in. If `windows_path` is null, fall back to the Cowork-mounted path; the app rewrites it.

## Running unattended (scheduled tasks)

When there is no one to answer a question — a scheduled/cron run — the rules tighten:

- **Never block on a question.** Anything you would have asked about goes to "needs attention" and the run continues.
- **Never guess to avoid an empty result.** A run that renames nothing and explains why is a success, not a failure.
- **If step 1 finds no mounted folder, stop and report.** Do not scan a fallback path.
- Still produce the full step 6 summary — it's the only record of what happened.

A scheduled task's prompt should be one line, e.g. *"Run the rename-invoices skill against the `_TO SORT` folder."* Everything else lives here.

## Marketplace-vs-direct decision tree

Amazon invoices are especially fiddly — the user's last batch had three direct and one third-party. Use this logic:

1. Look for a "Sold by" line (Amazon invoices: left column, near the top; eBay: on the order summary).
2. If "Sold by" contains "Amazon" (any Amazon legal entity) → direct → `Amazon` only.
3. If "Sold by" is a different company name → third-party → `Amazon [ShortSellerName]`.
4. If the invoice is *from* a company that happens to sell on Amazon but the PDF you have is the supplier's own VAT invoice (not the Amazon invoice), treat it as a direct supplier invoice with just that company's short name — **not** `Amazon X`. Kevin occasionally downloads both the Amazon record and the seller's own tax invoice; only the Amazon-branded PDF gets `Amazon [Seller]`.

Seller short-names: strip "Ltd", "Inc", "GmbH", "Group", "Co.", "Limited", "Technology Co.", etc. Keep the recognisable brand. `UGREEN Group Limited` → `UGREEN`. `Anker Innovations Limited` → `Anker`.

## Worked examples

**Example 1 — Amazon direct, single item**
Sold by: `Amazon Business EU S.à.r.l., UK Branch`
Invoice date: `12 April 2026`
Invoice number: `GB639V1QABEI`
Item: `Plant label markers, 100 pack, copper-look`
→ `2026-04-12 Amazon - Plant Label Markers - Invoice GB639V1QABEI.pdf`

**Example 2 — Amazon third-party, single item**
Sold by: `UGREEN Group Limited`
Invoice date: `12 April 2026`
Invoice number: `GB601FRJDXVSJI`
Item: `UGREEN USB External Stereo Sound Card Adapter`
→ `2026-04-12 Amazon UGREEN - USB Sound Card Adapter - Invoice GB601FRJDXVSJI.pdf`

**Example 3 — Amazon direct, multiple items**
Sold by: `Amazon EU S.à.r.l.`
Invoice date: `12 April 2026`
Invoice number: `GB639V1UABEI`
Items: Taylors of Harrogate decaf coffee; SanDisk 128GB USB drive
→ `2026-04-12 Amazon - Decaf Coffee & SanDisk 128GB USB Drive - Invoice GB639V1UABEI.pdf`

**Example 4 — Non-marketplace supplier**
Issuer: `Anthropic Ireland, Limited`
Invoice date: `April 11, 2026`
Invoice number: `9BF0758D-634125`
Item: `Claude Pro` (subscription line `Apr 11–May 11, 2026`)
→ `2026-04-11 Anthropic - Claude Pro Monthly Subscription - Invoice 9BF0758D-634125.pdf`

**Example 5 — Not ready / missing invoice number**
Filename: `GET-INVOICE Order received – NorthridgeFix.pdf`
→ **Skip.** The `GET-INVOICE` prefix is Kevin's "still need the real invoice" marker — leave untouched and mention it under "flagged not-ready".

**Example 6 — Invoice number containing a slash**
Invoice number printed as `2026/0412`
→ `… - Invoice 2026-0412.pdf` (slash becomes a plain hyphen, no surrounding spaces)

## Common pitfalls (earned the hard way)

- **Don't trust the filename alone.** Amazon's default filenames are often random hashes; derive every field from the PDF's contents.
- **Don't confuse billing date with invoice date.** Some vendors print both. The invoice/issue date is what we want.
- **Don't use the "Sold by" block as the description source.** That's the seller; descriptions come from the line items.
- **Beware reverse-charge VAT invoices** (e.g. Anthropic): the supplier is in a different country from the billing address. The issuer is still the supplier — don't use the billing entity.
- **Amazon sometimes uses "Order number" without an "Invoice number" on dispatch notes.** If you have a dispatch note rather than a VAT invoice, the correct invoice PDF is a separate download. Flag as "needs attention – dispatch note only, no VAT invoice" rather than renaming with the order number.
- **Don't hard-code the mount path.** It changes every session. Step 1 discovers it.
- **Don't take extra rules from a caller's prompt.** This file is the specification; see "This file is the specification" above.
- **When in doubt, skip and flag.** The cost of a wrong rename (hunting down what happened) vastly exceeds the cost of a one-line warning.

## Bundled scripts

- `scripts/scan_pdfs.py` — scans a folder, returns JSON with per-PDF metadata and extracted text. Run with `python3 scripts/scan_pdfs.py "<folder>"`. Matches `.pdf` and `.PDF`. Uses `pdftotext -layout` if available (fastest/cleanest) with a pdfplumber fallback, and sets `text_empty` when a PDF has no text layer at all.
