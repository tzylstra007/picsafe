#!/usr/bin/env python3
"""
picsafe_bridge_v2_appsheet.py
─────────────────────────────
PicSafe v2  —  Front-Half Bridge (AppSheet Edition)

Runs the 7-Step PicSafe Pipeline against your Apple Photos library,
then syncs minted/ready assets into AppSheet as PENDING records for the
nightly Google Photos publisher to pick up.

Seven-Step Pipeline
───────────────────
  1. Heart <-> Keyword Sync       — Favorite flag ↔ "PicSafe Favorited" keyword
  2. Face Recognition Bridge      — Expose internal face data as keywords
  3. Missing Faces Detector (QA)  — Flag unnamed faces (quality ≥ 0.4 only)
  4. AI Scene Analysis            — Add high-confidence scene/object labels
  5. GPS Audit                    — Flag photos missing location data
  6. Enhancement Audit            — Flag unedited / raw photos
  7. Metadata Write-Back          — Apply all keyword changes via AppleScript

Then (post-scan):
  8.  Mint PicSafe IDs            — Sequential IDs for all 2+ Star photos
  9.  AppSheet Sync               — Create PENDING records / update existing
  10. Smartsheet Dashboard        — Update AP + PicSafe-Ready counts per person
  11. Log Run                     — Write run summary to AppSheet run_history

Usage:
    cd ~/PicSafe
    source venv/bin/activate
    python picsafe_bridge_v2_appsheet.py
"""

import argparse
import os
import sys
import time
import datetime
import subprocess
import logging
import requests

import osxphotos
import smartsheet

# ── Configuration ─────────────────────────────────────────────────────────────
SCRIPT_NAME   = "picsafe_bridge_v2_appsheet"
LIBRARY_PATH  = "/Volumes/SharkTerra/zData/Photos Library.photoslibrary"
SEQUENCE_FILE = os.path.expanduser("~/PicSafe/picsafe_sequence.txt")
DASHBOARD_SHEET_ID = 8077434218827652

# Face quality threshold (0.0-1.0). Faces below this score are treated as
# background/blurry and are NOT counted for the facesmissing check.
FACE_QUALITY_THRESHOLD = 0.4

# Rating keywords that trigger PicSafe ID minting (2+ stars)
MINTING_RATING_KEYWORDS = {"2 Star", "3 Star", "4 Star", "5 Star"}

# PicSafe ID sequence bounds
SEQUENCE_MAX = 99999     # Highest mintable value → PicSafe_099999

# Rating keywords required for PicSafe Ready status (3+ stars)
READY_RATING_KEYWORDS = {"3 Star", "4 Star", "5 Star"}

# Tags that BLOCK PicSafe Ready status
BLOCKER_TAGS = {"!Audit: Missing GPS", "!Audit: Not Enhanced", "facesmissing"}

# Scene label junk-filter: skip these generic AI labels
JUNK_LABELS = {"media", "shot", "photography", "image", "photo", "photograph"}

# Dates to exclude (placeholder dates for imports without a real date)
EXCLUDED_DATES = {datetime.date(1961, 7, 21), datetime.date(1964, 5, 14)}

# AppSheet batch size (stay well under the 499-row documented max)
APPSHEET_BATCH = 100

# ── Load credentials ───────────────────────────────────────────────────────────
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
try:
    import picsafe_secrets
    SMARTSHEET_TOKEN = picsafe_secrets.SMARTSHEET_ACCESS_TOKEN.strip()
    APPSHEET_APP_ID  = picsafe_secrets.APPSHEET_APP_ID
    APPSHEET_API_KEY = picsafe_secrets.APPSHEET_API_KEY
except (ImportError, AttributeError) as e:
    print(f"CREDENTIAL ERROR: {e}")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger()


# ══════════════════════════════════════════════════════════════════════════════
# PicSafe ID Sequence
# ══════════════════════════════════════════════════════════════════════════════

def get_next_sequence() -> str:
    """Mint the next sequential PicSafe ID, e.g. PicSafe_042731.

    Range: PicSafe_034500 – PicSafe_099999  (SEQUENCE_MAX = 99999).
    Seed:  if the sequence file is absent it is initialised to 034499 so
           the very first mint produces PicSafe_034500.
    Raises RuntimeError when the sequence is exhausted.
    """
    if not os.path.exists(SEQUENCE_FILE):
        with open(SEQUENCE_FILE, "w") as f:
            f.write("034499")   # first mint → PicSafe_034500
    with open(SEQUENCE_FILE, "r") as f:
        try:
            current = int(f.read().strip())
        except ValueError:
            current = 34499
    next_val = current + 1
    if next_val > SEQUENCE_MAX:
        raise RuntimeError(
            f"PicSafe ID sequence exhausted — all IDs up to "
            f"PicSafe_{str(SEQUENCE_MAX).zfill(6)} have been used."
        )
    with open(SEQUENCE_FILE, "w") as f:
        f.write(str(next_val).zfill(6))
    return f"PicSafe_{str(next_val).zfill(6)}"


# ══════════════════════════════════════════════════════════════════════════════
# Apple Photos Write-Back via AppleScript  (Step 7)
# ══════════════════════════════════════════════════════════════════════════════

def _run_applescript(script_lines: list) -> bool:
    try:
        subprocess.run(
            ["osascript", "-e", "\n".join(script_lines)],
            check=True, capture_output=True, text=True
        )
        return True
    except subprocess.CalledProcessError as e:
        logger.warning(f"AppleScript error: {e.stderr.strip()[:120]}")
        return False


def write_metadata_to_photo(uuid: str,
                             new_title: str = None,
                             tags_add: list = None,
                             tags_remove: list = None) -> bool:
    """
    Write keyword and title changes back to an Apple Photos asset.
    Tries the bare UUID first, then the /L0/001 variant for edited copies.
    """
    if not tags_add:    tags_add = []
    if not tags_remove: tags_remove = []

    for target_uuid in [uuid, f"{uuid}/L0/001"]:
        sl = [
            'tell application "Photos"',
            f'  set targetItem to media item id "{target_uuid}"',
        ]
        if new_title:
            sl.append(f'  set name of targetItem to "{new_title}"')

        if tags_add or tags_remove:
            sl.append("  set currentTags to keywords of targetItem")
            sl.append("  if currentTags is missing value then set currentTags to {}")
            for t in tags_remove:
                sl.extend([
                    f'  if currentTags contains "{t}" then',
                    "    set newTags to {}",
                    "    repeat with kw in currentTags",
                    f'      if kw as string is not equal to "{t}" then set end of newTags to kw',
                    "    end repeat",
                    "    set currentTags to newTags",
                    "  end if",
                ])
            for t in tags_add:
                sl.append(
                    f'  if currentTags does not contain "{t}" '
                    f'then set end of currentTags to "{t}"'
                )
            sl.append("  set keywords of targetItem to currentTags")

        sl.append("end tell")
        if _run_applescript(sl):
            return True

    return False


# ══════════════════════════════════════════════════════════════════════════════
# Face Quality Analysis  (Steps 2 & 3)
# ══════════════════════════════════════════════════════════════════════════════

def get_face_status(photo) -> str:
    """
    Determine face completeness using quality-filtered detection.

    Logic (matches the PDF spec):
      Ignore any face whose quality score < 0.4 (blurry / background).
      - No face_info at all              -> 'facesfree'
      - No significant faces (all <0.4)  -> 'facesfree'
      - All significant faces are named  -> 'facescomplete'
      - Any significant face is unnamed  -> 'facesmissing'
    """
    if not photo.face_info:
        return "facesfree"

    significant = [
        f for f in photo.face_info
        if getattr(f, "quality", 1.0) >= FACE_QUALITY_THRESHOLD
    ]
    if not significant:
        return "facesfree"

    return "facescomplete" if all(f.name for f in significant) else "facesmissing"


# ══════════════════════════════════════════════════════════════════════════════
# Smartsheet Dashboard
# ══════════════════════════════════════════════════════════════════════════════

def get_go_live_people(ss) -> set:
    """Fetch the set of Person Names that have 'Go Live' checked."""
    print("   Loading 'Go Live' list from Smartsheet dashboard...")
    go_live = set()
    try:
        sheet = ss.Sheets.get_sheet(DASHBOARD_SHEET_ID)
        name_col    = next((c.id for c in sheet.columns if c.title == "Person Name"), None)
        go_live_col = next((c.id for c in sheet.columns if c.title == "Go Live"),    None)
        if name_col and go_live_col:
            for row in sheet.rows:
                name = next((c.value for c in row.cells if c.column_id == name_col),    None)
                gl   = next((c.value for c in row.cells if c.column_id == go_live_col), False)
                if name and gl:
                    go_live.add(str(name).strip())
    except Exception as e:
        logger.warning(f"Could not fetch Go Live list: {e}")
    print(f"      {len(go_live)} people marked 'Go Live'.")
    return go_live


def update_smartsheet_dashboard(ss, per_person: dict):
    """
    Update the Smartsheet dashboard with PicSafe-Ready counts from Apple Photos.

    per_person format:
      { "Person Name": {"ap_photos": int, "ap_videos": int,
                        "ready_photos": int, "ready_videos": int} }

    Column mapping:
      "Photos - AP"  ← ready_photos  (PicSafe Ready images in Apple Photos)
      "Videos - AP"  ← ready_videos  (PicSafe Ready videos in Apple Photos)

    Note: "Photos - Google" and "Videos - Google" are written by the publisher
    (picsafe_gphotos_publisher_v1.py) which has access to the Google Photos API.
    """
    if not per_person:
        return
    print(f"   Updating Smartsheet dashboard for {len(per_person)} people...")
    try:
        sheet = ss.Sheets.get_sheet(DASHBOARD_SHEET_ID)
        cols  = {c.title: c.id for c in sheet.columns}

        name_col      = cols.get("Person Name")
        ap_photos_col = cols.get("Photos - AP")   # ← PicSafe Ready photo count
        ap_videos_col = cols.get("Videos - AP")   # ← PicSafe Ready video count

        # Build name -> row-id index
        row_map = {}
        for row in sheet.rows:
            n = next((c.value for c in row.cells if c.column_id == name_col), None)
            if n:
                row_map[str(n).strip()] = row.id

        updates = []
        for person, pstats in per_person.items():
            if person not in row_map:
                continue
            cells = []
            # Write PicSafe Ready counts (not raw AP totals) to the AP columns
            if ap_photos_col:
                cells.append(smartsheet.models.Cell(
                    {"column_id": ap_photos_col, "value": pstats.get("ready_photos", 0)}))
            if ap_videos_col:
                cells.append(smartsheet.models.Cell(
                    {"column_id": ap_videos_col, "value": pstats.get("ready_videos", 0)}))
            if cells:
                updates.append(smartsheet.models.Row(
                    {"id": row_map[person], "cells": cells}))

        # Smartsheet batch limit = 100 rows per call
        for i in range(0, len(updates), 100):
            ss.Sheets.update_rows(DASHBOARD_SHEET_ID, updates[i:i + 100])

        print(f"      Dashboard updated for {len(updates)} people.")
    except Exception as e:
        logger.error(f"Smartsheet dashboard update failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# AppSheet REST API helpers
# ══════════════════════════════════════════════════════════════════════════════

def _appsheet_action(table: str, action: str, rows: list,
                     selector: str = None) -> list:
    """
    Low-level AppSheet Database REST call.
    action: "Find" | "Add" | "Edit" | "Delete"

    AppSheet quirk:
      Find  -> returns bare JSON array  [ {...}, {...} ]
      Add / Edit / Delete -> returns    { "Rows": [...] }
    """
    url     = (f"https://api.appsheet.com/api/v2/apps/{APPSHEET_APP_ID}"
               f"/tables/{table}/Action")
    headers = {
        "applicationAccessKey": APPSHEET_API_KEY,
        "Content-Type": "application/json",
    }
    props = {"Locale": "en-US"}
    if selector:
        props["Selector"] = selector

    payload = {"Action": action, "Properties": props, "Rows": rows}
    resp = requests.post(url, headers=headers, json=payload, timeout=45)
    resp.raise_for_status()

    # AppSheet sometimes returns 200 OK with an empty body on Edit/Add/Delete
    if not resp.content or not resp.content.strip():
        return []
    data = resp.json()
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("Rows", [])
    return []


def load_appsheet_assets() -> dict:
    """Load all existing AppSheet assets into {apple_uuid: row_dict}."""
    print("   Loading existing AppSheet assets...")
    existing = {}
    try:
        rows = _appsheet_action("assets", "Find", [])
        for r in rows:
            uuid = r.get("apple_uuid", "").strip()
            if uuid:
                existing[uuid] = r
        print(f"      {len(existing):,} existing assets loaded.")
    except Exception as e:
        logger.warning(f"Could not load AppSheet assets (continuing): {e}")
    return existing


def batch_appsheet_write(table: str, action: str, rows: list) -> int:
    """Write rows to AppSheet in safe batches. Returns count written.
    Includes a short sleep between batches to avoid AppSheet rate-limit 400s."""
    written = 0
    total_batches = (len(rows) + APPSHEET_BATCH - 1) // APPSHEET_BATCH
    for i in range(0, len(rows), APPSHEET_BATCH):
        chunk = rows[i:i + APPSHEET_BATCH]
        batch_num = i // APPSHEET_BATCH + 1
        try:
            _appsheet_action(table, action, chunk)
            written += len(chunk)
        except Exception as e:
            logger.error(
                f"AppSheet {action} batch {batch_num}/{total_batches} failed: {e}"
            )
        if batch_num < total_batches:
            time.sleep(1.0)  # 1s between batches to stay under AppSheet rate limit
    return written


def log_run_appsheet(photos_scanned: int, photos_modified: int,
                     errors: int, summary: str):
    """Append a run_history record to AppSheet."""
    run_id   = "run_" + datetime.datetime.now(datetime.UTC).strftime("%Y%m%d_%H%M%S_%f")
    run_date = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S")
    status   = "SUCCESS" if errors == 0 else "PARTIAL"
    try:
        batch_appsheet_write("run_history", "Add", [{
            "run_id":          run_id,
            "script_name":     SCRIPT_NAME,
            "run_date":        run_date,
            "photos_scanned":  str(photos_scanned),
            "photos_modified": str(photos_modified),
            "photos_uploaded": "0",
            "errors_count":    str(errors),
            "status":          status,
            "summary":         summary,
        }])
        print(f"   Run logged: {run_id}  [{status}]")
    except Exception as e:
        logger.error(f"Failed to log run: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="PicSafe v2 Bridge")
    parser.add_argument("--dry-run", action="store_true",
                        help="Scan Photos and compute changes but do NOT write anything "
                             "(no AppleScript, no AppSheet, no Smartsheet, no run log)")
    args = parser.parse_args()
    dry_run = args.dry_run

    start_time = time.time()
    print(f"\n{'='*62}")
    print(f"  {SCRIPT_NAME}")
    print(f"  {datetime.datetime.now().strftime('%Y-%m-%d  %H:%M:%S')}")
    if dry_run:
        print(f"  *** DRY-RUN MODE — no writes will occur ***")
    print(f"{'='*62}\n")

    # ── 1. External connections ────────────────────────────────────────────
    ss = smartsheet.Smartsheet(SMARTSHEET_TOKEN)
    go_live_people    = get_go_live_people(ss)
    appsheet_existing = load_appsheet_assets()

    # ── 2. Open Apple Photos library ──────────────────────────────────────
    print(f"\n   Opening Apple Photos library...")
    db_path = os.path.join(LIBRARY_PATH, "database/Photos.sqlite")
    if not os.path.exists(db_path):
        db_path = os.path.join(LIBRARY_PATH, "Photos.sqlite")
    if not os.path.exists(db_path):
        print(f"   ERROR: Photos.sqlite not found under {LIBRARY_PATH}")
        sys.exit(1)

    photosdb   = osxphotos.PhotosDB(dbfile=db_path)
    all_photos = [p for p in photosdb.photos() if not p.intrash]
    print(f"   {len(all_photos):,} photos to scan.\n")

    # ── 3. Per-item scan ──────────────────────────────────────────────────
    per_person: dict = {}   # {name: {ap_photos, ap_videos, ready_photos, ready_videos}}
    to_add:     list = []   # New AppSheet records
    to_update:  list = []   # Changed AppSheet records

    stats = {
        "scanned": 0, "skipped": 0,
        "minted": 0, "ready": 0,
        "metadata_written": 0, "errors": 0,
    }

    today_str = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")

    for p in all_photos:

        # Skip read-only / irrelevant items
        if p.shared or p.syndicated or (p.burst and not p.burst_selected):
            stats["skipped"] += 1
            continue

        stats["scanned"] += 1
        current_keywords = set(p.keywords)
        tags_add:    list = []
        tags_remove: list = []

        # ── Step 1: Heart <-> Keyword Sync ────────────────────────────────
        if p.favorite:
            if "PicSafe Favorited" not in current_keywords:     tags_add.append("PicSafe Favorited")
        else:
            if "PicSafe Favorited" in current_keywords:          tags_remove.append("PicSafe Favorited")

        # ── Steps 2 & 3: Face Recognition + QA (quality-filtered) ────────
        face_status = get_face_status(p)
        if face_status not in current_keywords:        tags_add.append(face_status)
        for fs in ("facesfree", "facesmissing", "facescomplete"):
            if fs != face_status and fs in current_keywords:
                tags_remove.append(fs)

        # ── Step 4: AI Scene Analysis ──────────────────────────────────────
        if hasattr(p, "labels_normalized"):
            for label in (p.labels_normalized or []):
                if label and label.lower() not in JUNK_LABELS:
                    if label not in current_keywords:
                        tags_add.append(label)

        # ── Step 5: GPS Audit ──────────────────────────────────────────────
        has_gps = bool(p.location and p.location[0] is not None)
        if has_gps:
            if "!Audit: Missing GPS" in current_keywords:
                tags_remove.append("!Audit: Missing GPS")
        else:
            if "!Audit: Missing GPS" not in current_keywords:
                tags_add.append("!Audit: Missing GPS")

        # ── Step 6: Enhancement Audit ──────────────────────────────────────
        is_enhanced = bool(getattr(p, "hasadjustments", False))
        if is_enhanced:
            if "Enhanced"             not in current_keywords: tags_add.append("Enhanced")
            if "!Audit: Not Enhanced" in    current_keywords:  tags_remove.append("!Audit: Not Enhanced")
        else:
            if "!Audit: Not Enhanced" not in current_keywords: tags_add.append("!Audit: Not Enhanced")
            if "Enhanced"             in    current_keywords:  tags_remove.append("Enhanced")

        # ── Simulate final tag set ─────────────────────────────────────────
        sim_tags = current_keywords.copy()
        sim_tags.update(tags_add)
        for t in tags_remove:
            sim_tags.discard(t)

        # ── Mint PicSafe ID (2+ Star rating AND Favorite / heart) ────────
        # Both conditions must be true for a new ID to be assigned:
        #   • MINTING_RATING_KEYWORDS  →  2 Star | 3 Star | 4 Star | 5 Star
        #   • p.favorite               →  Favorited ("heart") in Apple Photos
        # Photos that already have a PicSafe_ title are skipped (idempotent).
        has_mint_rating = not sim_tags.isdisjoint(MINTING_RATING_KEYWORDS)
        current_title   = p.title or ""
        new_title: str  = None

        if has_mint_rating and p.favorite and not current_title.startswith("PicSafe_"):
            new_title = get_next_sequence()
            stats["minted"] += 1

        picsafe_id = (
            new_title if new_title
            else (current_title if current_title.startswith("PicSafe_") else None)
        )

        # ── PicSafe Ready assessment ───────────────────────────────────────
        persons_in_photo  = [n for n in (p.persons or []) if n and not n.startswith("_")]
        has_go_live       = any(n in go_live_people for n in persons_in_photo)
        has_ready_rating  = not sim_tags.isdisjoint(READY_RATING_KEYWORDS)
        no_blockers       = sim_tags.isdisjoint(BLOCKER_TAGS)
        not_excluded_date = (p.date is None) or (p.date.date() not in EXCLUDED_DATES)

        is_ready = (
            picsafe_id
            and has_ready_rating
            and has_go_live
            and no_blockers
            and not_excluded_date
        )

        if is_ready:
            if "PicSafe Ready" not in sim_tags:
                tags_add.append("PicSafe Ready")
                sim_tags.add("PicSafe Ready")
            stats["ready"] += 1
        else:
            if "PicSafe Ready" in sim_tags:
                tags_remove.append("PicSafe Ready")
                sim_tags.discard("PicSafe Ready")

        # ── Step 7: Write metadata back to Apple Photos ───────────────────
        if new_title or tags_add or tags_remove:
            if dry_run:
                logging.debug(f"DRY-RUN: would write to {p.uuid[:8]}… "
                              f"title={new_title!r} add={tags_add} remove={tags_remove}")
                ok = True  # simulate success in dry-run
            else:
                ok = write_metadata_to_photo(p.uuid, new_title, tags_add, tags_remove)
                time.sleep(0.1)   # throttle: prevent Photos.app crashes
            if ok:
                stats["metadata_written"] += 1
            else:
                stats["errors"] += 1

        # ── Per-person stats for Smartsheet dashboard ─────────────────────
        is_video = not bool(getattr(p, "isphoto", True))
        for name in persons_in_photo:
            if name not in per_person:
                per_person[name] = {
                    "ap_photos": 0, "ap_videos": 0,
                    "ready_photos": 0, "ready_videos": 0,
                }
            if is_video:
                per_person[name]["ap_videos"] += 1
                if is_ready: per_person[name]["ready_videos"] += 1
            else:
                per_person[name]["ap_photos"] += 1
                if is_ready: per_person[name]["ready_photos"] += 1

        # ── Queue AppSheet sync ────────────────────────────────────────────
        if picsafe_id:
            capture_date    = p.date.strftime("%Y-%m-%d") if p.date else ""
            gps_status      = "OK" if has_gps else "MISSING"
            people_str      = ", ".join(sorted(persons_in_photo))
            kw_str          = ", ".join(sorted(sim_tags))
            # Use title-case values matching AppSheet's stored enum format
            enhancement_str = "Enhanced" if is_enhanced else "Not Enhanced"

            if p.uuid in appsheet_existing:
                existing = appsheet_existing[p.uuid]
                # AppSheet's row key is "Row ID" (with space), not "_RowID"
                row_id   = existing.get("Row ID", "")
                changed  = (
                    existing.get("face_status")        != face_status
                    or existing.get("gps_status")      != gps_status
                    or existing.get("people_list")     != people_str
                    or existing.get("picsafe_id")      != picsafe_id
                    or existing.get("enhancement_status") != enhancement_str
                )
                if changed and row_id:
                    to_update.append({
                        "Row ID":             row_id,
                        "picsafe_id":         picsafe_id,
                        "face_status":        face_status,
                        "gps_status":         gps_status,
                        "people_list":        people_str,
                        "keywords":           kw_str,
                        "enhancement_status": enhancement_str,
                        "last_audit_date":    today_str,
                    })
            else:
                # New: create a PENDING record in AppSheet
                to_add.append({
                    "picsafe_id":         picsafe_id,
                    "apple_uuid":         p.uuid,
                    "capture_date":       capture_date,
                    "people_list":        people_str,
                    "keywords":           kw_str,
                    "face_status":        face_status,
                    "gps_status":         gps_status,
                    "enhancement_status": enhancement_str,
                    "status_export":      "PENDING",
                    "status_gphotos":     "PENDING",
                    "is_public":          "No",
                    "last_audit_date":    today_str,
                })

        # Progress indicator every 500 photos
        if stats["scanned"] % 500 == 0:
            print(
                f"   ... {stats['scanned']:,} scanned  |  "
                f"{stats['minted']:,} minted  |  "
                f"{len(to_add):,} queued for AppSheet ..."
            )

    # ── 4. AppSheet batch sync ─────────────────────────────────────────────
    if dry_run:
        added   = 0
        updated = 0
        print(f"\n   DRY-RUN: would Add {len(to_add):,} / Edit {len(to_update):,} AppSheet records (skipped)")
    else:
        print(f"\n   AppSheet sync: {len(to_add):,} new  +  {len(to_update):,} updates ...")
        added   = batch_appsheet_write("assets", "Add",  to_add)
        updated = batch_appsheet_write("assets", "Edit", to_update)
        print(f"      Added {added:,}  |  Updated {updated:,}")

    # ── 5. Smartsheet dashboard update ────────────────────────────────────
    if dry_run:
        print(f"   DRY-RUN: Smartsheet dashboard update skipped")
    else:
        update_smartsheet_dashboard(ss, per_person)

    # ── 6. Log run ─────────────────────────────────────────────────────────
    elapsed = int(time.time() - start_time)
    mins, s = divmod(elapsed, 60)
    summary = (
        f"Scanned {stats['scanned']:,} | "
        f"Minted {stats['minted']:,} | "
        f"Ready {stats['ready']:,} | "
        f"AppSheet new {added:,} / updated {updated:,} | "
        f"Metadata writes {stats['metadata_written']:,} | "
        f"Errors {stats['errors']}"
    )
    if dry_run:
        print(f"   DRY-RUN: run log skipped  ({summary})")
    else:
        log_run_appsheet(stats["scanned"], stats["metadata_written"],
                         stats["errors"], summary)

    # ── Final summary ──────────────────────────────────────────────────────
    print(f"\n{'='*62}")
    print(f"  COMPLETE  ({mins}m {s}s)")
    print(f"  {'─'*58}")
    print(f"  Photos scanned:       {stats['scanned']:>8,}")
    print(f"  Skipped (read-only):  {stats['skipped']:>8,}")
    print(f"  IDs minted:           {stats['minted']:>8,}")
    print(f"  PicSafe Ready:        {stats['ready']:>8,}")
    print(f"  Metadata writes:      {stats['metadata_written']:>8,}")
    print(f"  AppSheet new:         {added:>8,}")
    print(f"  AppSheet updated:     {updated:>8,}")
    print(f"  Errors:               {stats['errors']:>8,}")
    print(f"{'='*62}\n")


if __name__ == "__main__":
    main()
