#!/usr/bin/env python3
"""
picsafe_export_v2.py  —  PicSafe Front-Half: Export
====================================================
Exports photos/videos whose AppSheet record has status_export == 'PENDING'
to the local export directory, ready for the nightly Google Photos publisher.

Pipeline per PENDING asset:
  1. Find photo in Apple Photos by apple_uuid
  2. Re-validate PicSafe Ready (3+ Star, Go Live person, no blockers)
  3. Export as highest-quality JPEG (photos) or MP4/MOV (videos)
  4. File saved as {uuid}.jpg / {uuid}.mp4 in EXPORT_PATH
  5. Update AppSheet: status_export = 'DONE', last_export_date = today
  6. Log run summary to AppSheet run_history

Usage:
  python picsafe_export_v2.py            # normal run
  python picsafe_export_v2.py --dry-run  # scan only, no file writes
"""

import os
import sys
import subprocess
import datetime
import argparse
import requests
import osxphotos

# ── PATH SETUP ─────────────────────────────────────────────────────────────────
PICSAFE_DIR = os.path.expanduser("~/PicSafe")
sys.path.insert(0, PICSAFE_DIR)
from picsafe_secrets import SMARTSHEET_ACCESS_TOKEN, APPSHEET_APP_ID, APPSHEET_API_KEY  # noqa: E402

# ── CONFIGURATION ──────────────────────────────────────────────────────────────
EXPORT_PATH    = "/Volumes/SharkTerra/zData/PicSafe_Exported"
LIBRARY_PATH   = "/Volumes/SharkTerra/zData/Photos Library.photoslibrary"
SCRIPT_NAME    = "picsafe_export_v2"
JPEG_QUALITY   = 1.0   # 0.0–1.0 for sips conversion
APPSHEET_BATCH = 50    # rows per AppSheet API call

# PicSafe Ready criteria — must match bridge_v2
READY_RATING_KEYWORDS = {"3 Star", "4 Star", "5 Star"}
BLOCKER_TAGS          = {"!Audit: Missing GPS", "!Audit: Not Enhanced", "facesmissing"}
EXCLUDED_DATES        = {datetime.date(1961, 7, 21), datetime.date(1964, 5, 14)}

VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".avi"}
PHOTO_EXTS = {".jpg", ".jpeg", ".heic", ".png", ".tiff", ".tif"}

# Smartsheet column IDs (sheet 8077434218827652) — loaded lazily
SMARTSHEET_SHEET_ID = 8077434218827652

# ── CLI ARGS ───────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="PicSafe Export v2")
parser.add_argument("--dry-run", action="store_true",
                    help="Scan and report but do not write files or update AppSheet")
args = parser.parse_args()
DRY_RUN = args.dry_run


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  APPSHEET HELPERS                                                            ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def _appsheet_action(table: str, action: str, rows: list, selector: str = None) -> list:
    """Call AppSheet REST API.  Returns list of rows regardless of Add/Edit/Find format."""
    url = (f"https://api.appsheet.com/api/v2/apps/{APPSHEET_APP_ID}"
           f"/tables/{table}/Action")
    headers = {"applicationAccessKey": APPSHEET_API_KEY,
               "Content-Type": "application/json"}
    props = {"Locale": "en-US"}
    if selector:
        props["Selector"] = selector
    payload = {"Action": action, "Properties": props, "Rows": rows}
    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("Rows", [])
    return []


def batch_appsheet_write(table: str, action: str, rows: list) -> list:
    """Chunk rows into APPSHEET_BATCH-sized calls to stay within API limits."""
    results = []
    for i in range(0, len(rows), APPSHEET_BATCH):
        chunk = rows[i: i + APPSHEET_BATCH]
        try:
            results.extend(_appsheet_action(table, action, chunk))
        except Exception as e:
            print(f"   ⚠️  AppSheet {action} error (chunk {i // APPSHEET_BATCH}): {e}")
    return results


def load_pending_assets() -> dict:
    """Return {apple_uuid: row_dict} for all assets with status_export == 'PENDING'."""
    rows = _appsheet_action(
        "assets", "Find", [],
        selector='Filter(assets, [status_export] = "PENDING")'
    )
    result = {}
    for r in rows:
        uuid = (r.get("apple_uuid") or "").strip()
        if uuid:
            result[uuid] = r
    return result


def log_run(photos_exported: int, videos_exported: int, errors: int):
    """Write a run summary row to AppSheet run_history."""
    total   = photos_exported + videos_exported
    status  = "SUCCESS" if errors == 0 else ("FAILED" if total == 0 else "PARTIAL")
    summary = (f"Exported {total} files ({photos_exported} photos, "
               f"{videos_exported} videos). {errors} errors.")
    if DRY_RUN:
        print(f"   [DRY-RUN] Would log: {summary}")
        return
    try:
        _appsheet_action("run_history", "Add", [{
            "script_name":     SCRIPT_NAME,
            "run_date":        datetime.datetime.now().isoformat(),
            "photos_scanned":  total,
            "photos_modified": total,
            "photos_uploaded": 0,    # export only; upload happens in publisher
            "errors_count":    errors,
            "status":          status,
            "summary":         summary,
        }])
        print(f"   ✅  Run logged to AppSheet run_history ({status})")
    except Exception as e:
        print(f"   ⚠️  run_history log failed: {e}")


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  SMARTSHEET — Go Live people                                                 ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def get_go_live_people() -> set:
    """Return set of person names whose 'Go Live' checkbox is ticked in Smartsheet."""
    import smartsheet
    ss     = smartsheet.Smartsheet(SMARTSHEET_ACCESS_TOKEN)
    sheet  = ss.Sheets.get_sheet(SMARTSHEET_SHEET_ID)
    name_col_id = None
    live_col_id = None
    for c in sheet.columns:
        if c.title == "Person Name":
            name_col_id = c.id
        elif c.title == "Go Live":
            live_col_id = c.id

    result = set()
    for row in sheet.rows:
        name_cell = next((c for c in row.cells if c.column_id == name_col_id), None)
        live_cell = next((c for c in row.cells if c.column_id == live_col_id), None)
        if name_cell and live_cell and live_cell.value:
            name = (name_cell.value or "").strip()
            if name:
                result.add(name)
    return result


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  READINESS RE-VALIDATION                                                     ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def is_picsafe_ready(photo: osxphotos.PhotoInfo, go_live_people: set) -> bool:
    """Return True if photo currently qualifies as PicSafe Ready."""
    kw            = set(photo.keywords or [])
    has_rating    = not kw.isdisjoint(READY_RATING_KEYWORDS)
    no_blockers   = kw.isdisjoint(BLOCKER_TAGS)
    persons       = [n for n in (photo.persons or []) if n and not n.startswith("_")]
    has_go_live   = any(n in go_live_people for n in persons)
    not_excluded  = (photo.date is None) or (photo.date.date() not in EXCLUDED_DATES)
    return has_rating and no_blockers and has_go_live and not_excluded


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  EXPORT HELPERS                                                              ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def sips_to_jpeg(src: str, dest: str, quality: float = JPEG_QUALITY) -> bool:
    """Convert image at src to JPEG at dest using macOS sips. Returns True on success."""
    quality_pct = str(int(quality * 100))
    try:
        result = subprocess.run(
            ["sips", "-s", "format", "jpeg",
             "-s", "formatOptions", quality_pct,
             src, "--out", dest],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        return os.path.exists(dest)
    except subprocess.CalledProcessError as e:
        print(f"     sips error: {e.stderr.decode().strip()}")
        return False
    except subprocess.TimeoutExpired:
        print(f"     sips timed out for {os.path.basename(src)}")
        return False


def export_asset(photo: osxphotos.PhotoInfo) -> tuple:
    """
    Export one photo/video to EXPORT_PATH.

    Returns:
        (final_path: str, is_video: bool)  on success
        (None, None)                        on failure
    """
    uuid     = photo.uuid
    is_video = getattr(photo, "ismovie", False)

    if is_video:
        # ── VIDEO ──────────────────────────────────────────────────────────────
        dest = os.path.join(EXPORT_PATH, f"{uuid}.mp4")
        if os.path.exists(dest):
            return dest, True  # idempotent

        tmp_files = photo.export(
            EXPORT_PATH,
            filename=uuid,
            overwrite=True,
        )
        if not tmp_files:
            return None, None

        raw = tmp_files[0]
        _, ext = os.path.splitext(raw)
        if os.path.abspath(raw) != os.path.abspath(dest):
            # Rename whatever extension Photos gave us to .mp4
            os.rename(raw, dest)
        return dest, True

    else:
        # ── PHOTO ──────────────────────────────────────────────────────────────
        dest = os.path.join(EXPORT_PATH, f"{uuid}.jpg")
        if os.path.exists(dest):
            return dest, False  # idempotent

        # Try a direct export; this may produce HEIC, JPEG, PNG etc.
        tmp_files = photo.export(
            EXPORT_PATH,
            filename=uuid,
            overwrite=True,
        )
        if not tmp_files:
            return None, None

        raw = tmp_files[0]
        _, ext = os.path.splitext(raw)

        if ext.lower() in {".jpg", ".jpeg"}:
            # Already JPEG — just rename to canonical {uuid}.jpg if needed
            if os.path.abspath(raw) != os.path.abspath(dest):
                os.rename(raw, dest)
            return dest, False

        # Need sips conversion (HEIC, PNG, TIFF, …)
        ok = sips_to_jpeg(raw, dest)
        if ok:
            try:
                os.remove(raw)   # remove the intermediate non-JPEG
            except OSError:
                pass
            return dest, False
        else:
            # sips failed — keep whatever we have as a fallback
            print(f"     ⚠️  sips failed; keeping {os.path.basename(raw)}")
            return raw, False


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  MAIN                                                                        ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def main():
    tag = " [DRY-RUN]" if DRY_RUN else ""
    print("=" * 62)
    print(f"🚀  PicSafe Export v2{tag}  —  {datetime.datetime.now():%Y-%m-%d %H:%M}")
    print("=" * 62)

    # ── 1. Verify export volume is mounted ────────────────────────────────────
    if not os.path.isdir(EXPORT_PATH) and not DRY_RUN:
        print(f"\n❌  Export path not found: {EXPORT_PATH}")
        print("   Is the SharkTerra drive mounted?  Mount it and re-run.")
        sys.exit(1)

    if not DRY_RUN:
        os.makedirs(EXPORT_PATH, exist_ok=True)

    # ── 2. Load PENDING records from AppSheet ─────────────────────────────────
    print(f"\n{'Step 1':>6}: Loading PENDING records from AppSheet …")
    try:
        pending_by_uuid = load_pending_assets()
    except Exception as e:
        print(f"   ❌  AppSheet error: {e}")
        sys.exit(1)

    if not pending_by_uuid:
        print("   ✅  Nothing to export — all caught up!")
        log_run(0, 0, 0)
        return

    print(f"   Found {len(pending_by_uuid):,} PENDING UUIDs")

    # ── 3. Load Go Live people from Smartsheet ────────────────────────────────
    print(f"\n{'Step 2':>6}: Loading Go Live people from Smartsheet …")
    try:
        go_live_people = get_go_live_people()
        print(f"   {len(go_live_people)} Go Live: {sorted(go_live_people)}")
    except Exception as e:
        print(f"   ⚠️  Smartsheet error: {e} — proceeding without Go Live filter")
        go_live_people = set()

    # ── 4. Open Apple Photos Library ──────────────────────────────────────────
    print(f"\n{'Step 3':>6}: Opening Apple Photos Library …")
    try:
        photosdb = osxphotos.PhotosDB(dbfile=LIBRARY_PATH)
    except Exception as e:
        print(f"   ❌  Cannot open library at {LIBRARY_PATH}: {e}")
        sys.exit(1)

    # Build lookup: uuid → PhotoInfo (only for PENDING set to keep memory lean)
    photo_lookup: dict = {}
    for p in photosdb.photos():
        if p.uuid in pending_by_uuid and not p.intrash:
            photo_lookup[p.uuid] = p

    print(f"   Matched {len(photo_lookup):,} / {len(pending_by_uuid):,} UUIDs in library")

    # ── 5. Export loop ────────────────────────────────────────────────────────
    print(f"\n{'Step 4':>6}: Exporting …\n")
    today_str       = datetime.date.today().isoformat()
    photos_exported = 0
    videos_exported = 0
    skipped_ready   = 0
    skipped_missing = 0
    errors          = 0
    to_update       = []   # AppSheet rows to mark DONE

    for uuid, appsheet_row in pending_by_uuid.items():
        photo = photo_lookup.get(uuid)
        if not photo:
            skipped_missing += 1
            continue

        # Re-validate readiness
        if not is_picsafe_ready(photo, go_live_people):
            skipped_ready += 1
            continue

        picsafe_id = (appsheet_row.get("picsafe_id") or uuid[:12])
        is_video   = getattr(photo, "ismovie", False)
        symbol     = "🎥" if is_video else "📸"

        if DRY_RUN:
            label = "video" if is_video else "photo"
            print(f"   {symbol}  [DRY-RUN] Would export {label}: {picsafe_id}")
            if is_video:
                videos_exported += 1
            else:
                photos_exported += 1
            continue

        try:
            final_path, actually_video = export_asset(photo)
            if final_path:
                basename = os.path.basename(final_path)
                print(f"   {symbol}  {picsafe_id}  →  {basename}")
                if actually_video:
                    videos_exported += 1
                else:
                    photos_exported += 1

                row_id = appsheet_row.get("_RowID", "")
                if row_id:
                    to_update.append({
                        "_RowID":            row_id,
                        "status_export":     "DONE",
                        "last_export_date":  today_str,
                    })
            else:
                print(f"   ❌  Export failed for {picsafe_id} ({uuid})")
                errors += 1

        except Exception as exc:
            print(f"   ❌  Error exporting {picsafe_id}: {exc}")
            errors += 1

    # ── 6. AppSheet batch update ──────────────────────────────────────────────
    if to_update and not DRY_RUN:
        print(f"\n{'Step 5':>6}: Marking {len(to_update):,} records DONE in AppSheet …")
        batch_appsheet_write("assets", "Edit", to_update)
        print(f"   ✅  {len(to_update):,} records updated")

    # ── 7. Summary ───────────────────────────────────────────────────────────
    total = photos_exported + videos_exported
    print("\n" + "=" * 62)
    print(f"{'✅' if errors == 0 else '⚠️ '}  Export Complete{tag}")
    print(f"   📸  Photos exported  : {photos_exported:,}")
    print(f"   🎥  Videos exported  : {videos_exported:,}")
    print(f"   ⏭   Not ready        : {skipped_ready:,}")
    print(f"   ❓  UUID not found   : {skipped_missing:,}")
    print(f"   ❌  Errors           : {errors:,}")
    print("=" * 62)

    log_run(photos_exported, videos_exported, errors)


if __name__ == "__main__":
    main()
