#!/usr/bin/env python3
"""
scripts/import_csv.py — One-time bulk import from PIPELINED_DATA CSV.

Usage (run inside the Fly container):
  python3 /app/scripts/import_csv.py /tmp/import.csv

Will not re-import rows that already exist (checks original_id in audit_log).
"""
from __future__ import annotations

import csv
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts import storage
from scripts.applications import _app_key, _index_key, _to_index_entry

LOGODEV_TOKEN = "YOUR_LOGODEV_KEY_HERE"
IMPORT_ACTOR = "cdl825@gmail.com"
IMPORT_TS    = "2026-06-01T00:00:00Z"   # timestamp of the import run itself

VALID_STATUSES  = {"Not Applying","Researching","Applied","Phone Screen","Interviewing","On Hold","Offer","Rejected"}
VALID_PRIORITIES = {"Low","Medium","High"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_date(s: str) -> str | None:
    """'M/D/YY' → 'YYYY-MM-DDTHH:MM:SSZ', or None."""
    if not s or not s.strip():
        return None
    try:
        parts = s.strip().split("/")
        if len(parts) == 3:
            m, d, y = parts
            year = int(y)
            year += 2000 if year < 100 else 0
            return f"{year:04d}-{int(m):02d}-{int(d):02d}T00:00:00Z"
    except Exception:
        pass
    return None


def clean_url(s: str) -> str:
    """Strip trailing non-URL text (e.g. 'Check out this job…')."""
    if not s:
        return ""
    token = s.strip().split()[0] if s.strip() else ""
    return token if token.startswith("http") else s.strip()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    csv_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/import.csv"

    if not storage.is_configured():
        print("ERROR: Storage not configured — missing AWS credentials")
        sys.exit(1)

    user = storage.get_user_by_email(IMPORT_ACTOR)
    if not user:
        print(f"ERROR: User {IMPORT_ACTOR} not found in storage")
        sys.exit(1)

    user_id = user["user_id"]
    print(f"Importing as: {IMPORT_ACTOR}  (user_id={user_id})")

    # Load existing index so we can check for prior imports
    existing_raw = storage.get_text(_index_key(user_id))
    index: list[dict] = json.loads(existing_raw) if existing_raw else []

    # Collect original_ids already imported to avoid dupes
    existing_orig_ids: set[str] = set()
    for entry in index:
        record_raw = storage.get_text(_app_key(user_id, entry["id"]))
        if record_raw:
            record = json.loads(record_raw)
            for ev in record.get("audit_log", []):
                oid = ev.get("details", {}).get("original_id", "")
                if oid:
                    existing_orig_ids.add(oid)

    print(f"Already-imported original IDs: {len(existing_orig_ids)}")
    print(f"Existing index entries:        {len(index)}")
    print()

    imported = 0
    skipped  = 0

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            company = (row.get("Company") or "").strip()
            if not company:
                continue   # blank row

            original_id = (row.get("ID") or "").strip()

            # Skip already-imported rows
            if original_id and original_id in existing_orig_ids:
                print(f"  SKIP (already imported): {company}")
                skipped += 1
                continue

            domain      = (row.get("Domain") or "").strip().lower()
            role_title  = (row.get("Role Title") or "").strip()
            status      = (row.get("Status") or "Researching").strip()
            date_app    = parse_date(row.get("Date Applied") or "")
            last_upd    = parse_date(row.get("Last Updated") or "")
            tags        = ["DUA Reported"] if (row.get("DUA?") or "").strip().upper() == "TRUE" else []
            job_source  = (row.get("Job Source") or "").strip()
            location    = (row.get("Location / Remote") or "").strip()
            salary      = (row.get("Salary Range") or "").strip()
            priority    = (row.get("Priority") or "Medium").strip()
            rec_name    = (row.get("Recruiter Name") or "").strip()
            rec_email   = (row.get("Recruiter Email") or "").strip()
            notes       = (row.get("Notes / Next Steps") or "").strip()
            url         = clean_url(row.get("URL") or "")

            # Normalise enums
            if status not in VALID_STATUSES:
                status = "Researching"
            if priority not in VALID_PRIORITIES:
                priority = "Medium"

            # created_at = earlier of the two dates; updated_at = later
            date_candidates = [d for d in [date_app, last_upd] if d]
            created_at = min(date_candidates) if date_candidates else IMPORT_TS
            updated_at = last_upd or date_app or IMPORT_TS

            logo_url = (
                f"https://img.logo.dev/{domain}?token={LOGODEV_TOKEN}"
                if domain else ""
            )

            app_id = str(uuid.uuid4())

            record: dict = {
                "id":               app_id,
                "user_id":          user_id,
                "company":          company,
                "domain":           domain,
                "company_logo_url": logo_url,
                "role_title":       role_title,
                "status":           status,
                "date_applied":     date_app,
                "last_updated":     updated_at,   # stored verbatim (bypasses auto-now)
                "created_at":       created_at,
                "created_by":       IMPORT_ACTOR,
                "updated_at":       updated_at,
                "updated_by":       IMPORT_ACTOR,
                "tags":             tags,
                "job_source":       job_source,
                "location":         location,
                "salary_range":     salary,
                "priority":         priority,
                "recruiter_name":   rec_name,
                "recruiter_email":  rec_email,
                "url":              url,
                "comments":         [],
                "audit_log": [
                    {
                        "id":        str(uuid.uuid4()),
                        "action":    "imported",
                        "actor":     IMPORT_ACTOR,
                        "timestamp": IMPORT_TS,
                        "ip":        None,
                        "details": {
                            "source":      "CSV import 2026-06-01",
                            "original_id": original_id,
                        },
                    }
                ],
            }

            # Notes column → first comment, dated at last_updated
            if notes:
                record["comments"].append({
                    "id":         str(uuid.uuid4()),
                    "text":       notes,
                    "created_at": updated_at,
                    "updated_at": updated_at,
                    "author":     IMPORT_ACTOR,
                })

            # Write directly — bypasses save_application()'s auto-timestamp
            storage.put_text(_app_key(user_id, app_id), json.dumps(record))
            index.append(_to_index_entry(record))
            imported += 1
            print(f"  ✓  {company:40s}  {role_title[:35]:35s}  [{status}]")

    # Persist updated index
    storage.put_text(_index_key(user_id), json.dumps(index))

    print()
    print(f"Done — {imported} imported, {skipped} skipped (already present)")


if __name__ == "__main__":
    main()
