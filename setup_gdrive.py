#!/usr/bin/env python3
"""
setup_gdrive.py — One-time Google Drive OAuth setup for apply.py

Run this once to authorize the app and cache a refresh token.
After this, apply.py uploads automatically with no browser prompts.

Usage:
    python3 setup_gdrive.py
"""

import os
import sys
from pathlib import Path

CREDS_PATH = Path(__file__).parent / "gdrive_credentials.json"
TOKEN_PATH  = Path.home() / ".config" / "job-apply" / "gdrive_token.json"
SCOPES      = ["https://www.googleapis.com/auth/drive.file"]
PARENT_ID   = os.environ.get("GDRIVE_PARENT_FOLDER_ID", "")

SETUP_INSTRUCTIONS = """
gdrive_credentials.json not found.

To create it:
  1. Go to https://console.cloud.google.com/
  2. Create or select a project (e.g. "job-apply")
  3. Enable the Google Drive API:
       APIs & Services → Library → search "Google Drive API" → Enable
  4. Create OAuth credentials:
       APIs & Services → Credentials → Create Credentials → OAuth client ID
       Application type: Desktop app
       Name: job-apply (or anything)
  5. Click Download JSON → save the file as:
       {creds_path}
  6. Re-run this script.
""".format(creds_path=CREDS_PATH)


def main():
    # Check dependencies
    try:
        from googleapiclient.discovery import build
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
    except ImportError:
        print("ERROR: Google API packages not installed. Run:")
        print("  pip install google-api-python-client google-auth-oauthlib google-auth-httplib2")
        sys.exit(1)

    # Check credentials file
    if not CREDS_PATH.exists():
        print(SETUP_INSTRUCTIONS)
        sys.exit(1)

    # Load or refresh token
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("Refreshing existing token...")
            creds.refresh(Request())
        else:
            print("Opening browser for Google authorization...")
            flow  = InstalledAppFlow.from_client_secrets_file(str(CREDS_PATH), SCOPES)
            creds = flow.run_local_server(port=0)

        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(creds.to_json())
        # Contains a long-lived refresh token — restrict to the owner.
        try:
            os.chmod(TOKEN_PATH, 0o600)
        except OSError:
            pass
        print(f"Token saved to {TOKEN_PATH}")
    else:
        print(f"Existing token is valid — no browser needed.")

    # Smoke-test: list the Job Applications folder
    service = build("drive", "v3", credentials=creds)
    result  = service.files().list(
        q=f"'{PARENT_ID}' in parents and trashed=false",
        fields="files(id, name)",
        pageSize=5,
    ).execute()
    files = result.get("files", [])

    print(f"\n✓ Connected to Google Drive.")
    print(f"  Job Applications folder contents (up to 5):")
    if files:
        for f in files:
            print(f"    • {f['name']}")
    else:
        print("    (empty)")

    print("\n✓ Setup complete. apply.py will upload automatically from now on.")


if __name__ == "__main__":
    main()
