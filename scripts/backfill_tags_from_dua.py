#!/usr/bin/env python3
"""
scripts/backfill_tags_from_dua.py

One-off migration: replace the legacy boolean `dua` field on every application
with a "DUA Reported" tag (when dua was true) and drop the `dua` key from the
record and the per-user index. Idempotent. Does not bump last_updated or write
audit entries.

Run on the Fly machine:
  fly ssh console -C "python3 scripts/backfill_tags_from_dua.py [--dry-run]"
"""
from __future__ import annotations
import json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts import storage
from scripts.applications import _app_key, _index_key

TAG = "DUA Reported"


def _add_tag(tags: list[str]) -> list[str]:
    if any(t.lower() == TAG.lower() for t in tags):
        return tags
    return [*tags, TAG]


def migrate_user(user_id: str, dry_run: bool) -> tuple[int, int]:
    """Returns (records migrated, records tagged)."""
    raw = storage.get_text(_index_key(user_id))
    index = json.loads(raw) if raw else []
    migrated = tagged = 0

    for entry in index:
        rec_raw = storage.get_text(_app_key(user_id, entry["id"]))
        if not rec_raw:
            continue
        rec = json.loads(rec_raw)
        if "dua" not in rec and "dua" not in entry:
            continue
        had_dua = bool(rec.get("dua"))
        if had_dua:
            rec["tags"] = _add_tag(rec.get("tags", []))
            entry["tags"] = _add_tag(entry.get("tags", []))
            tagged += 1
        rec.pop("dua", None)
        entry.pop("dua", None)
        migrated += 1
        if not dry_run:
            storage.put_text(_app_key(user_id, rec["id"]), json.dumps(rec))

    if migrated and not dry_run:
        storage.put_text(_index_key(user_id), json.dumps(index))
    return migrated, tagged


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    total_m = total_t = 0
    for user in storage.list_all_users():
        m, t = migrate_user(user["user_id"], dry_run)
        if m:
            print(f"{user.get('email', user['user_id'])}: migrated {m}, tagged {t}")
        total_m += m
        total_t += t
    print(f"{'[dry-run] ' if dry_run else ''}Total migrated {total_m}, tagged {total_t}")


if __name__ == "__main__":
    main()
