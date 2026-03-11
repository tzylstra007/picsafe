"""
picsafe_appsheet_client.py
--------------------------
Reusable AppSheet Database REST API client for PicSafe v2.
All AppSheet interaction flows through this module.

Tables: assets | albums | audit_log | run_history
"""

import requests
import datetime
import sys
import os

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import picsafe_secrets

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_APP_ID  = getattr(picsafe_secrets, 'APPSHEET_APP_ID',  '')
_API_KEY = getattr(picsafe_secrets, 'APPSHEET_API_KEY', '')

BASE_URL = f"https://api.appsheet.com/api/v2/apps/{_APP_ID}/tables"
HEADERS  = {
    "applicationAccessKey": _API_KEY,
    "Content-Type": "application/json",
}
BATCH_SIZE = 499  # AppSheet documented max rows per API call


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _action(table: str, action: str, rows: list) -> dict:
    """
    Low-level AppSheet REST API call.
    action: "Add" | "Edit" | "Delete" | "Find"
    Raises requests.HTTPError on non-2xx responses.
    Returns {} if the response body is empty (AppSheet sometimes returns 200 OK
    with no body on successful Edit/Add operations).
    """
    url = f"{BASE_URL}/{table}/Action"
    payload = {
        "Action": action,
        "Properties": {"Locale": "en-US"},
        "Rows": rows,
    }
    resp = requests.post(url, headers=HEADERS, json=payload, timeout=30)
    resp.raise_for_status()
    if not resp.content or not resp.content.strip():
        return {}
    return resp.json()


# ---------------------------------------------------------------------------
# Batch write utilities
# ---------------------------------------------------------------------------

def batch_write(table: str, action: str, rows: list) -> int:
    """
    Write rows to an AppSheet table in chunks of up to BATCH_SIZE.
    Returns total number of rows successfully submitted.
    """
    if not rows:
        return 0

    submitted = 0
    total_batches = (len(rows) + BATCH_SIZE - 1) // BATCH_SIZE

    for i in range(0, len(rows), BATCH_SIZE):
        chunk = rows[i:i + BATCH_SIZE]
        batch_num = (i // BATCH_SIZE) + 1
        try:
            _action(table, action, chunk)
            submitted += len(chunk)
            if total_batches > 1:
                print(f"   📤 AppSheet {table}/{action}: batch {batch_num}/{total_batches} ({len(chunk)} rows)")
        except requests.HTTPError as e:
            print(f"   ❌ AppSheet {action} on '{table}' batch {batch_num} failed: {e.response.status_code} {e.response.text[:200]}")

    return submitted


# ---------------------------------------------------------------------------
# Table-specific helpers
# ---------------------------------------------------------------------------

def _fetch_row_ids(picsafe_ids: list) -> dict:
    """
    Look up AppSheet 'Row ID' values for a list of picsafe_ids.
    Returns {picsafe_id: row_id}.  Missing rows are omitted.

    AppSheet's row key is the auto-generated 'Row ID' column, NOT picsafe_id,
    so Edit operations must include 'Row ID' in each row dict.
    """
    result = {}
    for pid in picsafe_ids:
        try:
            data = requests.post(
                f"{BASE_URL}/assets/Action",
                headers=HEADERS,
                json={
                    "Action": "Find",
                    "Properties": {
                        "Locale": "en-US",
                        "Selector": f'Filter(assets, [picsafe_id] = "{pid}")',
                    },
                    "Rows": [],
                },
                timeout=30,
            )
            data.raise_for_status()
            if data.content and data.content.strip():
                rows = data.json()
                if rows and isinstance(rows, list):
                    # AppSheet returns the key as "Row ID" (with space)
                    result[pid] = rows[0].get("Row ID", "")
        except Exception:
            pass
    return result


def upsert_assets(asset_rows: list) -> int:
    """
    Edit asset rows in AppSheet, populating 'Row ID' for each row first.
    AppSheet requires the row key ('Row ID') for Edit operations; rows that
    cannot be resolved are skipped with a warning.
    """
    if not asset_rows:
        return 0

    # Extract the base picsafe_id (strip _edited suffix if present) for lookup
    base_ids = [r["picsafe_id"].replace("_edited", "") for r in asset_rows]
    row_id_map = _fetch_row_ids(list(set(base_ids)))

    enriched = []
    for row in asset_rows:
        base = row["picsafe_id"].replace("_edited", "")
        rid = row_id_map.get(base, "")
        if not rid:
            print(f"   ⚠️  upsert_assets: no Row ID found for {row['picsafe_id']}, skipping")
            continue
        # Use base picsafe_id (strip _edited) so we never overwrite the AppSheet
        # picsafe_id field with the "_edited" filename variant.
        enriched.append({**row, "picsafe_id": base, "Row ID": rid})

    if not enriched:
        return 0
    return batch_write("assets", "Edit", enriched)


def add_assets(asset_rows: list) -> int:
    """Insert new asset rows (first-time registration)."""
    return batch_write("assets", "Add", asset_rows)


def add_audit_log(log_entries: list) -> int:
    """Append a list of entries to the audit_log table."""
    return batch_write("audit_log", "Add", log_entries)


def log_single(picsafe_id: str, action: str, details: str, script_name: str) -> bool:
    """
    Write a single audit log entry immediately (not batched).
    Use for critical events like STARTUP, PREFLIGHT_FAIL, RUN_COMPLETE.
    """
    entry = make_log_entry(picsafe_id, action, details, script_name)
    try:
        _action("audit_log", "Add", [entry])
        return True
    except requests.HTTPError as e:
        print(f"   ❌ AppSheet single log failed: {e}")
        return False


def add_run_history(run_row: dict) -> bool:
    """Log a single run summary row to the run_history table."""
    try:
        _action("run_history", "Add", [run_row])
        return True
    except requests.HTTPError as e:
        print(f"   ❌ AppSheet run_history write failed: {e}")
        return False


def upsert_album(album_row: dict) -> bool:
    """Upsert a single album row by album_id key."""
    try:
        _action("albums", "Edit", [album_row])
        return True
    except requests.HTTPError as e:
        # If Edit fails because the row doesn't exist yet, try Add
        try:
            _action("albums", "Add", [album_row])
            return True
        except requests.HTTPError as e2:
            print(f"   ❌ AppSheet album upsert failed: {e2}")
            return False


def find_asset(picsafe_id: str) -> dict | None:
    """
    Look up a single asset row by picsafe_id.
    Returns the row dict, or None if not found.
    """
    try:
        result = _action("assets", "Find", [{"picsafe_id": picsafe_id}])
        rows = result.get("Rows", [])
        return rows[0] if rows else None
    except requests.HTTPError:
        return None


# ---------------------------------------------------------------------------
# Row builder helpers
# ---------------------------------------------------------------------------

def now_ts() -> str:
    """Return current UTC timestamp as ISO string for AppSheet DateTime columns."""
    return datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def make_log_entry(picsafe_id: str, action: str, details: str, script_name: str) -> dict:
    """Build a well-formed audit_log row dict."""
    return {
        "picsafe_id":   picsafe_id,
        "action":       action,
        "details":      str(details)[:1000],   # AppSheet text field cap
        "script_name":  script_name,
        "timestamp":    now_ts(),
    }


def make_asset_row(
    picsafe_id:          str,
    apple_uuid:          str  = "",
    capture_date:        str  = "",
    people_list:         str  = "",
    keywords:            str  = "",
    face_status:         str  = "facesfree",
    gps_status:          str  = "UNKNOWN",
    enhancement_status:  str  = "Not Enhanced",
    status_export:       str  = "PENDING",
    status_gphotos:      str  = "PENDING",
    gphotos_media_id:    str  = "",
    gphotos_album_id:    str  = "",
    is_public:           str  = "No",
    last_audit_date:     str  = "",
    last_export_date:    str  = "",
    last_upload_date:    str  = "",
) -> dict:
    """
    Build a well-formed assets row dict.
    All fields are optional except picsafe_id.
    """
    return {
        "picsafe_id":         picsafe_id,
        "apple_uuid":         apple_uuid,
        "capture_date":       capture_date,
        "people_list":        people_list,
        "keywords":           keywords,
        "face_status":        face_status,
        "gps_status":         gps_status,
        "enhancement_status": enhancement_status,
        "status_export":      status_export,
        "status_gphotos":     status_gphotos,
        "gphotos_media_id":   gphotos_media_id,
        "gphotos_album_id":   gphotos_album_id,
        "is_public":          is_public,
        "last_audit_date":    last_audit_date or now_ts(),
        "last_export_date":   last_export_date,
        "last_upload_date":   last_upload_date,
    }


def make_run_row(
    script_name:     str,
    photos_scanned:  int = 0,
    photos_modified: int = 0,
    photos_uploaded: int = 0,
    errors_count:    int = 0,
    status:          str = "SUCCESS",
    summary:         str = "",
) -> dict:
    """Build a well-formed run_history row dict."""
    now = datetime.datetime.utcnow()
    run_id = now.strftime("run_%Y%m%d_%H%M%S_") + f"{now.microsecond:06d}"
    return {
        "run_id":          run_id,
        "script_name":     script_name,
        "run_date":        now.strftime("%Y-%m-%d %H:%M:%S"),
        "photos_scanned":  photos_scanned,
        "photos_modified": photos_modified,
        "photos_uploaded": photos_uploaded,
        "errors_count":    errors_count,
        "status":          status,
        "summary":         summary[:2000],
    }
