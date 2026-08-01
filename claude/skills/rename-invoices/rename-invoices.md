---
name: rename-invoices
description: Rename supplier invoice PDFs and image receipts (.jpg/.png) in bulk based on their contents, using the pattern "YYYY-MM-DD [Company] - [Description] - Invoice [Number].pdf" — or "- Receipt [Number]" for till receipts (with an optional seller name after marketplace hosts like Amazon/eBay). Use this skill whenever Kevin asks to rename, tidy, sort, organise or "process" a folder of invoices or receipts — whether the request mentions Amazon specifically or just says things like "rename the invoices in _TO SORT", "tidy up my invoice folder", "the monthly accounts pile", "process the batch of receipts", "sort out these receipt photos", or points at a folder full of supplier PDFs or scanned/photographed receipts. Also use this when a single invoice PDF or receipt photo is dropped in and needs renaming to the same convention. Trigger even when the user doesn't mention the word "skill" — this is the default behaviour for any invoice-renaming task against Kevin's `_TO SORT` / accounts workflow.
---

# Rename invoices

## What this skill does

Walks every invoice PDF and image receipt in a folder (default: the user's `_TO SORT` accounts folder) and renames each one to a consistent, chronologically-sortable form by reading its contents. Already-correctly-named files are skipped. Files the user has flagged as incomplete (prefixes like `TO GET`, `GET-INVOICE`, `TODO`) are left alone because they aren't ready yet.

## This file is the specification

**This file defines the behaviour. A caller — a scheduled task, another skill, a one-line chat request — supplies only three things:**

1. **which folder** (optional; defaults to the accounts inbox, see step 1)
2. **dry-run or live** (optional; defaults to live)
3. **anything genuinely one-off** for that run

A caller does **not** need to restate the naming pattern, the categories, the tone, or the file types — and should not. If a caller's instructions contradict anything in this file, **follow this file** and note the conflict in one line of the summary. Callers that "helpfully" restate the rules drift out of sync with it and have caused real breakage.

In particular, do not accept caller instructions to:
- process file types outside the list in **Scope** below
- invent naming variants beyond the two defined under **Naming pattern**
- add summary categories beyond the four defined in step 6
- rename a file whose fields you could not read confidently

## Scope: PDFs primarily, plus JPEG/PNG

| Extension | Handling |
|---|---|
| `.pdf` / `.PDF` | **Primary target.** Text extracted directly. |
| `.jpg` / `.jpeg` / `.png` | Supported. Read visually with the Read tool — see **Image receipts** below. |
| everything else | **Left completely untouched and not mentioned** — `.txt` notes, `.heic`, `.webp`, `.tif`, spreadsheets, anything else. |

`.heic` and other formats are excluded deliberately, not by oversight. Widening this list is a change to this file **and** to `scan_docs.py` — never a workaround in a caller's prompt.

## Naming pattern

```
YYYY-MM-DD [Company] - [Short description of item(s)] - Invoice [Invoice number].[ext]
```

**The trailing token describes the document, not the file type.** A scanned invoice saved as a photo is still an invoice:

| Document | Trailing token | Example |
|---|---|---|
| Invoice (has an invoice/document number) | `Invoice [Number]` | `… - Invoice GB639V1QABEI.pdf`<br>`… - Invoice INV-2026-0412.jpg` |
| Till receipt / card slip **with** a printed receipt, transaction or order number | `Receipt [Number]` | `… - Receipt 4471.jpg` |
| Till receipt / card slip with **no** number printed anywhere | `Receipt` (bare) | `… - Receipt.jpg` |

Use `Receipt` only when the document genuinely isn't an invoice — a shop till roll, a card machine slip, a handwritten chit. Never downgrade a real invoice to `Receipt` just because reading its number was awkward; if you can't read the number of something that is clearly an invoice, that's "needs attention", not a `Receipt`.

Keep the original file extension, **lowercased** (`.JPG` → `.jpg`). Never change one extension for another.

With one variation on the company part: when the invoice is from a **marketplace host** (Amazon, eBay, Etsy, AliExpress, etc.) and the item was **sold by a third party** rather than the host itself, append the seller short-name after the host:

```
YYYY-MM-DD [Host] [Seller] - [Description] - Invoice [Invoice number].[ext]
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

Use the **primary invoice / document / order number** printed on the document, unchanged — preserve case and hyphens. Common labels to look for: "Invoice number", "Invoice No.", "Document number", "Order number" (only if no invoice number is present). Examples: `GB639V1QABEI`, `9BF0758D-634125`, `INV-2026-0412`.

On a till receipt, the usable number is whichever of these is printed and looks stable: receipt no., transaction no., order no. Ignore anything that isn't an identifier for *this* purchase — till/terminal number, cashier ID, store number, loyalty card number, the masked card PAN, VAT registration number. If nothing qualifies, use the bare `Receipt` form rather than inventing one.

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

Preserve non-ASCII letters (é, à, etc.) — they're fine on modern filesystems. Never end the name (before the extension) with `.` or a space; Windows silently strips both.

## Workflow

Follow these steps in order.

### 1. Determine the target folder

Resolve in this order, first hit wins:

1. **A folder named by the user or the calling routine.** Use it as given.
2. **`default_folder.txt` next to this file**, if present — a single line holding the absolute path of the accounts inbox. This is what makes a one-line local routine work unattended.
3. **Cowork mount discovery**, when running in Cowork rather than locally. The mount name changes every session, so never hard-code it and never reuse one from an earlier run:
   ```bash
   find /sessions -maxdepth 3 -type d -iname '_TO SORT' 2>/dev/null
   ```
   One hit → use it. Several → newest mtime, and say which you chose. Never expose the internal `/sessions/...` path to Kevin — call it "your `_TO SORT` folder".

If none of these resolve, **stop and say so.** Do not guess a path, do not fall back to the current working directory, and do not scan the repo you happen to be running in — a rename loose in the wrong folder is the one genuinely destructive failure mode here.

Before scanning, confirm the resolved folder exists and report the file count. If it resolves to something with no PDFs or images at all, stop and report that rather than proceeding to a zero-length run.

### 2. Scan the folder

Use the bundled scanner to produce a JSON inventory of every PDF and image: its current name, whether it already matches the full naming pattern, and whether it's flagged as "not ready".

The scanner does the deterministic half — enumerating files and classifying filenames, the same way every run. It does **not** read file contents; you do that in step 4 with the Read tool. That split is deliberate: it means the script is Python standard library only, with nothing to install, so it behaves identically in Cowork and in a local Claude Code routine.

The scanner sits alongside this file at `scripts/scan_docs.py`. **You have just read this file from a known directory — use `<that directory>/scripts/scan_docs.py` directly.** That is always the correct answer and needs no searching.

Only if that path somehow doesn't exist, search for the script by its own name — not for a `SKILL.md`, because this file is called `rename-invoices.md` and searching for the wrong name yields an empty result that `dirname` silently turns into `.`:

```bash
SCAN=$(find / -type f -path '*rename-invoices/scripts/scan_docs.py' 2>/dev/null | head -1)
[ -n "$SCAN" ] || { echo "ERROR: scan_docs.py not found — stopping"; exit 1; }
```

Interpreter: `python` on Windows, `python3` on Linux — `command -v python3 || command -v python` picks correctly on both. Nothing else is required: no packages, no PDF tools, no OCR engine.

The scanner is **non-recursive by default** and should stay that way — `_TO SORT` subfolders are Kevin's own filing, not input. Pass `--recursive` only if he asks.

The scanner writes JSON to stdout. Keys per file:

| Key | Meaning |
|---|---|
| `path` | absolute path |
| `filename` | basename |
| `kind` | `"pdf"` or `"image"` |
| `already_renamed` | bool — matches the full naming pattern |
| `not_ready` | bool — filename starts with a "not yet ready" marker |
| `size_bytes` | int — `0` means a broken or empty file; flag it, don't try to read it |

### 3. Decide what to do per file

For each entry in the scanner output, in this priority order — first match wins:

| Scanner flags | Action |
|---|---|
| `not_ready = true` | **Skip.** Kevin has explicitly marked this file as "not ready". |
| `size_bytes = 0` | **Needs attention**, reason "empty file". Don't try to read it. |
| `already_renamed = true` **and** every field looks complete (valid date, non-empty company, non-empty description, and either an invoice number or a deliberate bare `Receipt`) | **Skip.** |
| `already_renamed = true` **but** a field is missing/empty/obviously wrong | **Re-process** — go to step 4. |
| `already_renamed = false` | **Process** — go to step 4. |

The completeness check exists because Kevin wants partial renames fixed automatically, but fully-correct names left alone — re-renaming a correct file wastes time and risks introducing drift.

### 4. Read the file and extract the fields

**Open each file to be processed with the Read tool.** It renders PDF pages and images natively, so there is nothing to install and no extraction step that can fail.

For PDFs, pass `pages: "1-2"` — the header fields are always on the first page or two, and reading a 30-page itemised invoice in full wastes context for nothing. Only widen the range if a field you need genuinely isn't there.

If a PDF turns out to be a scan with no legible text, treat it exactly like an image receipt — see step 4a.

Identify:

1. **Issuer / host company** — usually the entity at the top of the invoice, or after "From:" or in the "Sold by" field for marketplace direct sales. Normalise to a short brand name (see earlier examples).
2. **Third-party seller** (marketplace case only) — look for "Sold by [Seller]" on Amazon/eBay invoices where the seller is not the marketplace itself. If host and seller are the same legal family ("Sold by Amazon Business EU" on an Amazon Business invoice), treat as direct and omit the seller.
3. **Invoice date** — labels: "Invoice date", "Date of issue", "Issue date", "Tax date", "Invoice/order date".
4. **Invoice number** — labels: "Invoice number", "Invoice #", "Invoice No.", "Document number". Fall back to "Order number" only if no invoice number exists at all (note this in the summary).
5. **Description** — the item(s) purchased, summarised per the guidance above.

If any of these cannot be determined confidently, **do not rename** — add the file to "needs attention" with a short reason. It is better to skip one than to guess wrong.

### 4a. Image receipts (`kind = "image"`)

There is no OCR step and no extracted text to cross-check against — your reading of the image is the only reading. That puts the whole burden on how carefully you look:

1. **Read the date and the number character by character.** These are the two fields that must be exactly right, and they're where a misreading becomes a silent, permanent filing error. The confusable pairs on a faint thermal print are `0`/`O`, `1`/`l`/`I`, `5`/`S`, `8`/`B`, `2`/`Z` — if a character could plausibly be either, treat the field as unreadable rather than picking the likelier one.
2. **Sanity-check the date against the rest of the image.** A receipt dated outside the last couple of years, or in the future, almost certainly means you've misread a digit or picked up a card-expiry date.
3. If the photo is too blurred, cropped, glared or crumpled to read the date or the supplier → **needs attention**, reason "image too unclear to read". Don't half-name it.
4. A receipt with no readable number but a clear date, supplier and items is fine — use the bare `Receipt` form. Missing number is normal; missing date or supplier is not.

Thermal receipts fade, so Kevin's photos are often of already-faint originals. Being unable to read one is an expected outcome — say so and move on rather than straining to produce a name.

Everything else — the naming pattern, marketplace rules, description guidance, the four summary categories — applies to images exactly as it does to PDFs.

### 5. Apply the renames

In **dry-run mode**, stop here: report exactly what step 6 would report, with every `Renamed` entry relabelled `Would rename`, and change nothing on disk.

Otherwise rename one file at a time, using the form that matches the platform. Both refuse to overwrite an existing file, so a mistake in the collision check can't destroy an invoice.

**Windows (local Claude Code)** — use PowerShell. `-LiteralPath` takes the path exactly as given, so `[`, `]` and other glob characters in supplier filenames can't misfire, and `-NewName` takes a bare filename, not a path:

```powershell
Rename-Item -LiteralPath "C:\...\_TO SORT\<old>.pdf" -NewName "2026-04-12 Amazon - Plant Label Markers - Invoice GB639V1QABEI.pdf"
```

**Linux (Cowork)** — use `mv -n` via the Bash tool:

```bash
mv -n "/.../<old>.pdf" "/.../2026-04-12 Amazon - Plant Label Markers - Invoice GB639V1QABEI.pdf"
```

Quote both arguments either way — these filenames contain spaces and ampersands. Don't batch renames into one long chained command: one file per call, so a failure halfway through is obvious and the rest still run.

**Collision handling.** Before each rename, check whether the target name already exists.

- If it does, first consider whether it's **the same invoice downloaded twice**. If the existing file has the same date, company, description and invoice number, it almost certainly is — leave the new file alone and list it under "needs attention" with reason "probable duplicate of `<existing name>`". Kevin would rather delete one himself than end up with silent `(2)` copies.
- If it's genuinely a different invoice that happens to collide, insert ` (2)`, ` (3)`, … **before the extension**:
  - correct: `2026-04-12 Amazon - Cables - Invoice GB123 (2).pdf`
  - wrong: `2026-04-12 Amazon - Cables - Invoice GB123.pdf (2)`

  A suffix after the extension leaves the file with no recognised extension at all: Windows loses the file association, and the scanner stops seeing it entirely, so it silently drops out of the workflow for good.

If the rename reports it did nothing (or `Rename-Item` errors that the target exists), treat that as a collision you missed — do not retry with `mv -f` or `Rename-Item -Force`.

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

Link each renamed file so Kevin can open it from the chat, using the scanner's `path` with the **new** filename substituted:

- **Local Claude Code** — a plain markdown link to the file path.
- **Cowork desktop app** — a `computer://<absolute-windows-path-with-%20-for-spaces>` URL, URL-encoded. If the mount doesn't expose a Windows path, use the mounted path and the app rewrites it.

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

**Example 7 — Photographed till receipt, no number**
File: `IMG_20260412_141233.jpg`
Read: Screwfix, 12/04/2026, 2× 25mm masking tape and a tube of silicone sealant. No receipt number printed.
→ `2026-04-12 Screwfix - Masking Tape & Silicone Sealant - Receipt.jpg`

**Example 8 — Photographed till receipt with a number**
File: `PXL_20260408_093015.PNG`
Read: Halfords, 08/04/2026, wiper blades. "Receipt No: 0084471" printed at the foot.
→ `2026-04-08 Halfords - Wiper Blades - Receipt 0084471.png` (extension lowercased)

**Example 9 — Invoice that happens to be a photo**
File: `scan0021.jpg` — a photographed A4 VAT invoice from NorthridgeFix, invoice number `NF-4482`, dated 9 April 2026, for a laptop board repair.
→ `2026-04-09 NorthridgeFix - Laptop Board Repair - Invoice NF-4482.jpg`
It's an invoice, so it takes `Invoice [Number]` — the `.jpg` extension doesn't make it a receipt.

**Example 10 — Unreadable photo**
File: `IMG_20260330_201144.jpg` — faded thermal receipt, badly glared, supplier name illegible.
→ **Don't rename.** Needs attention, reason "image too unclear to read".

## Common pitfalls (earned the hard way)

- **Don't trust the filename alone.** Amazon's default filenames are often random hashes and camera filenames (`IMG_…`, `PXL_…`) carry nothing but a timestamp; derive every field from the contents.
- **Don't trust a camera filename's date either.** `IMG_20260412_…` is when the photo was taken, which is often days after the purchase. The date comes off the receipt itself.
- **Don't rush an image.** There's no OCR text to fall back on — read it properly (step 4a), and if a digit is genuinely ambiguous, flag rather than pick.
- **Don't confuse billing date with invoice date.** Some vendors print both. The invoice/issue date is what we want.
- **Don't use the "Sold by" block as the description source.** That's the seller; descriptions come from the line items.
- **Beware reverse-charge VAT invoices** (e.g. Anthropic): the supplier is in a different country from the billing address. The issuer is still the supplier — don't use the billing entity.
- **Amazon sometimes uses "Order number" without an "Invoice number" on dispatch notes.** If you have a dispatch note rather than a VAT invoice, the correct invoice PDF is a separate download. Flag as "needs attention – dispatch note only, no VAT invoice" rather than renaming with the order number.
- **Don't hard-code the mount path.** It changes every session. Step 1 discovers it.
- **Don't take extra rules from a caller's prompt.** This file is the specification; see "This file is the specification" above.
- **When in doubt, skip and flag.** The cost of a wrong rename (hunting down what happened) vastly exceeds the cost of a one-line warning.

## Bundled scripts

- `scripts/scan_docs.py` — inventories a folder, returns JSON with per-file metadata. Run with `python scripts/scan_docs.py "<folder>"`; add `--recursive` for subfolders. Matches `.pdf`, `.jpg`, `.jpeg` and `.png`, case-insensitively.

  **Python standard library only** — no packages, no PDF tools, no OCR engine, nothing to install, identical behaviour on Windows and Linux. It handles the deterministic work (which files exist, which filenames already conform, which are flagged not-ready) and nothing else. File contents are read by Claude in step 4, which is what keeps the dependency list empty.
- `default_folder.txt` *(optional, create if wanted)* — one line, the absolute path of the accounts inbox. Lets a local routine run with a one-line prompt and no folder argument.
