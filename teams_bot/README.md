# Job Apply — Microsoft Teams Bot

A Microsoft Bot Framework integration that brings the Job Apply agent platform
to Microsoft Teams. Built with the Bot Framework SDK for Python (`botbuilder`).

## Features

| Command | Description |
|---------|-------------|
| `apply` | Generate a tailored resume, ATS resume, and cover letter |
| `aq` | Answer an application question using resume + JD context |
| `prep` | Generate an interview prep reference card |
| `thankyou` | Generate a post-interview thank-you email |
| `optimize` | Refine existing run documents (resume/cover letter) |
| `rescore` | Re-score resume/JD match for an application |
| `tracker` | Pipeline summary (counts by status) |
| `track list [status]` | List applications, optionally filtered |
| `track add` | Add a new application (Adaptive Card form) |
| `track view` | View full application details, including a link to its linked Google Drive output folder if one exists |
| `track update` | Two-step: pick an application → edit all fields pre-filled |
| `track note` | Add a comment to an application |
| `track delete` | Delete an application (two-step confirm) |
| `cal today` / `cal week` | Show today's / next 7 days' calendar events |
| `cal add` / `cal view` / `cal delete` | Add, view, or delete a calendar event |
| `company [name]` | Search company info via Logo.dev |
| `profile resume` | Instructions for uploading a new master resume (attach a `.docx` directly to the chat) |
| `profile guide` | Edit your profile & voice guide |
| `notifications` | View and toggle email notification preferences |
| `confirm` | Link your Teams identity to a Job Apply account |
| `whoami` | Show which account you're linked as |
| `unlink` | Remove your Teams identity's link |
| `runs` | List recent agent runs (structured records with type, status, Drive links) |
| `help` | Command reference |

Every command above except `help`/`confirm`/`unlink` requires the caller's Teams
identity to already be linked to a Job Apply account — see **Identity Linking**
below — and `apply`/`prep`/`aq`/`thankyou` only run against a tracked application
(no free-text company/role entry). Full per-command details live in the main
[README.md](../README.md#teams-commands) `Teams Commands` section; this file
covers bot architecture and deployment.

## Architecture

In production, this bot is **mounted directly onto the main FastAPI app**
(`api.py`, the `web` Fly process) via `routers/teams.py` — it is not a
separate Fly machine or port. `routers/teams.py` adds `teams_bot/` to
`sys.path` and imports `bot.py`/`config.py` as flat top-level modules (the
same way `app.py` does when run standalone), then exposes `POST
/api/messages` as a FastAPI route. This means the bot rides on the app's
existing public domain (`https://apply.cdlav.us`) and TLS cert — no extra
Azure-facing infrastructure to stand up or keep alive.

```
Teams Client
    │
    ▼
Azure Bot Service (webhook relay)
    │
    ▼
POST https://apply.cdlav.us/api/messages
    │
    ▼
routers/teams.py (FastAPI route, part of the `web` Fly process)
    │
    ▼
bot.py (ActivityHandler — command routing + Adaptive Cards)
    │
    ▼
api_client.py (HTTP client → FastAPI backend, same process)
```

`teams_bot/app.py` (the standalone aiohttp server on port 3978) still exists
for **local development only** — it's the fastest way to iterate with the
Bot Framework Emulator without touching the deployed app. It is not used in
production.

- **Adaptive Cards** replace Slack's Block Kit modals for rich form input
- **Proactive messaging** via `ConversationReference` for long-running agent
  jobs (apply, prep, aq, thankyou, optimize) — same thread-and-poll pattern as
  the Slack bot
- **Per-user identity linking** — unlike the Slack bot (which always acts as
  the single primary account), the Teams bot resolves which Job Apply account
  each Teams user is acting on behalf of; see **Identity Linking** below
- File upload via direct chat attachment (`profile resume`) instead of a
  slash-command argument
- All state lives in the FastAPI backend — the Teams bot is stateless

## Setup

This is a step-by-step walkthrough — follow it in order. Each step depends on
the one before it.

### Single tenant vs. multi-tenant

**Single-tenant works fine.** Most personal/Microsoft 365 work or school
accounts can't create multi-tenant Azure AD app registrations (org policy
blocks it), so single-tenant is actually the common path here. The only
difference: a single-tenant app registration only issues tokens for *your*
Azure AD tenant, and the bot needs to know that tenant ID so it validates
incoming requests correctly. The code now supports this via the
`MICROSOFT_APP_TENANT_ID` env var (see step 3) — without it, a single-tenant
app registration will fail auth with a "multi-tenant token used for
single-tenant app" type error from the Bot Framework adapter. You do not need
multi-tenant for this to work in your own Teams org.

### 1. Create the Azure AD App Registration

This is the identity the bot uses to authenticate with Microsoft. Do this
*before* creating the Azure Bot resource — the Bot resource needs an existing
App ID.

1. Go to the [Azure Portal](https://portal.azure.com) → search **"App registrations"** → **New registration**
2. Name it (e.g. `job-apply-teams-bot`)
3. Under **Supported account types**, choose **"Accounts in this organizational directory only (Single tenant)"** — this is the option that works without special org permissions
4. Click **Register**
5. On the app's **Overview** page, copy and save two values — you'll need them in step 3:
   - **Application (client) ID** → this is `MICROSOFT_APP_ID`
   - **Directory (tenant) ID** → this is `MICROSOFT_APP_TENANT_ID`
6. Go to **Certificates & secrets** → **New client secret** → give it a description and expiry → **Add**
7. **Copy the secret's "Value" immediately** (not the Secret ID) — it's only shown once. This is `MICROSOFT_APP_PASSWORD`.

### 2. Create the Azure Bot Resource

1. In the Azure Portal, **Create a resource** → search **"Azure Bot"** → **Create**
2. **Bot handle**: any unique name
3. **Type of App**: choose **"Use existing app registration"**
4. Paste in the **App ID** and **Tenant ID** from step 1
5. Create the resource
6. Once created, open it → **Settings → Configuration** → set the **Messaging endpoint** to `https://apply.cdlav.us/api/messages` (this is the production endpoint — see step 4 below; it's already live once the secrets are deployed)
7. Under **Settings → Channels**, add the **Microsoft Teams** channel and accept the terms

### 3. Set Secrets on Fly

The bot runs as part of the deployed `web` process (see Architecture above),
so credentials go in as Fly secrets, not local env vars:

```bash
fly secrets set --app job-apply-corey \
  MICROSOFT_APP_ID="<Application (client) ID from step 1>" \
  MICROSOFT_APP_PASSWORD="<client secret VALUE from step 1>" \
  MICROSOFT_APP_TENANT_ID="<Directory (tenant) ID from step 1>"
```

`BOT_API_KEY` is already set on Fly (shared with the Slack bot) — no action
needed there. `MICROSOFT_APP_TENANT_ID` is required for single-tenant app
registrations (the common case); omit it only for a true multi-tenant app.
Setting secrets triggers a redeploy automatically.

### 4. Verify

Once the deploy finishes:
1. `curl https://apply.cdlav.us/api/health` → should return a healthy response (confirms the `web` machine is up)
2. Go to the Azure Bot resource → **Test in Web Chat** and send a message (e.g. `help`) → confirms Azure can reach `/api/messages` and the bot responds, before touching Teams at all. This isolates "is the bot reachable and authenticating" from "is Teams sideloading working" — much easier to debug one at a time.

### 5. Local Development (optional)

You don't need any of this to use the bot in Teams — it's only for iterating
on bot logic without redeploying:

1. `cd teams_bot && pip install -r requirements.txt`
2. Either:
   - **Bot Framework Emulator**: download it from the [releases page](https://github.com/microsoft/BotFramework-Emulator/releases), run `python app.py` with `MICROSOFT_APP_ID`/`MICROSOFT_APP_PASSWORD` left empty (unauthenticated mode), connect the emulator to `http://localhost:3978/api/messages`. Validates bot logic only — not real Azure AD auth.
   - **ngrok tunnel**: `python app.py` (with real credentials exported as env vars), then `ngrok http 3978`, then temporarily point the Azure Bot resource's messaging endpoint at the ngrok URL to test against real Teams traffic without touching production.

### 6. Build and Sideload the Teams App Package

Only do this once step 4's Web Chat test works.

1. In `manifest/manifest.json`, replace **both** `{{MICROSOFT_APP_ID}}` placeholders (the `id` field and `bots[0].botId`) with your actual Application (client) ID from step 1
2. Add a 32×32 `outline.png` and a 192×192 `color.png` to `manifest/` (transparent background, simple icon — Teams will reject the upload without both files present)
3. Zip **the contents** of the manifest folder (not the folder itself):
   ```bash
   cd manifest && zip ../jobapply-teams.zip manifest.json outline.png color.png && cd ..
   ```
4. In Teams: **Apps** (left rail) → **Manage your apps** → **Upload an app** → **Upload a custom app**
   - If you don't see "Upload a custom app", your Teams admin has custom app uploads disabled org-wide — ask them to enable it in the Teams Admin Center under **Teams apps → Setup policies**, or have them upload/approve it centrally instead
5. Select `jobapply-teams.zip` — Teams installs it and opens a chat with the bot
6. Send `help` to confirm

### Common failure points

| Symptom | Likely cause |
|---|---|
| "Unauthorized" / 401 in bot logs when messaging from Teams | Missing `MICROSOFT_APP_TENANT_ID` on a single-tenant app registration |
| Web Chat test in Azure works, but Teams sideload fails to even install | `{{MICROSOFT_APP_ID}}` placeholder not replaced in `manifest.json`, or zip contains a parent folder instead of the files directly |
| Bot installs in Teams but never responds | Messaging endpoint in Azure Bot config doesn't match `https://apply.cdlav.us/api/messages`, or (if testing locally) a stale ngrok URL — those expire/rotate |
| "Upload a custom app" option missing in Teams | Org policy blocks custom app uploads — needs a Teams admin to enable it |
| Cards/forms don't render, plain text does | Adaptive Card JSON schema version mismatch with the Teams client — check `cards/*.json` against the [Adaptive Cards schema explorer](https://adaptivecards.io/explorer/) |
| Re-consenting after a manifest version bump gets stuck in a loop instead of completing | The manifest's `privacyUrl`/`termsOfUseUrl` must be publicly reachable without auth — confirm `/privacy` and `/terms` return the pages directly rather than redirecting to `/login.html` |
| Completion messages (apply/prep/aq/thankyou/optimize) never arrive after the run finishes successfully | `_proactive_message()` must pass `bot_id=Config.APP_ID` to `adapter.continue_conversation()` — a `None` bot_id is rejected outright by the SDK and fails silently in the background thread |

## How It Works

### Identity Linking
The bot has no built-in notion of "logged in." The first time a Teams user
runs any command other than `help`/`confirm`/`unlink`, the bot looks up a
`teams_links/{aad_object_id}.json` record in Tigris (`scripts/teams_links.py`).
If missing or expired, it fetches the caller's email via the Bot Framework's
`TeamsInfo.get_member()` roster API, checks whether a Job Apply account exists
for that email, and — if so — asks the user to reply `confirm`. Only after
that explicit confirmation does it persist the link (30-day expiry, then
re-confirmation is required). Every subsequent API call the bot makes on that
user's behalf carries an `X-Teams-User-Email` header so `api.py:_bot_user()`
resolves that specific account instead of the shared primary account the
Slack bot uses. `bot-key`-authenticated `/api/teams/*` endpoints back this
flow (`link-status`, `account-lookup`, `link-confirm`, `link-token`,
`link-claim`, `unlink`) — see the `Teams Bot` folder in the Postman
collection.

### Command Routing
Users type commands as plain messages (e.g., `apply`, `track list interviewing`).
The bot parses the text, strips @mentions in group chats, and routes to the
appropriate handler. A message carrying an attachment only short-circuits
into file-upload handling if that attachment is actually a `.docx` the bot
processed — Teams attaches non-file metadata (mentions, rich-text elements)
to plenty of ordinary text messages, and those still fall through to normal
command dispatch.

### Adaptive Cards
Form-based commands (`apply`, `aq`, `prep`, `track add`) respond with an
Adaptive Card containing input fields. When submitted, the card payload comes
back as a message activity with `activity.value` populated — the bot routes
based on `data.action`.

### Long-Running Jobs
Agent commands (apply, prep, aq, optimize) use the same async pattern as the Slack bot:
1. Send an immediate "⏳ Starting…" message
2. Spawn a background thread that POSTs to the API, then polls for completion
3. When done, send a proactive message back to the conversation using the
   saved `ConversationReference`

### Backend Integration
All data flows through `api_client.py` → the FastAPI backend (`JOB_APPLY_API_URL`,
default `https://apply.cdlav.us`). In production this is a same-process
call since `routers/teams.py` mounts the bot onto the same FastAPI app — the
HTTP round trip only matters for local/standalone runs of `teams_bot/app.py`.
The Teams bot authenticates with the same `BOT_API_KEY` Bearer token as the
Slack bot. No separate user accounts or auth flow needed.

## File Structure

```
teams_bot/
├── app.py                 # standalone aiohttp entry point — local dev only, not used in production
├── bot.py                 # ActivityHandler (command routing + card handling)
├── api_client.py          # HTTP client for FastAPI backend
├── config.py              # Environment variable configuration
├── requirements.txt       # Python dependencies (for standalone/local runs)
├── cards/                 # Adaptive Card JSON templates
│   ├── apply_form.json
│   ├── aq_form.json
│   ├── optimize_form.json
│   ├── prep_form.json
│   ├── thankyou_form.json
│   └── track_add_form.json
├── manifest/              # Teams app manifest
│   ├── manifest.json
│   ├── outline.png / color.png    # required icons — Teams rejects the upload without both
│   └── landed-teams-app.zip       # built sideload package (manifest.json + icons)
└── README.md

routers/teams.py           # production mount point — POST /api/messages on the main FastAPI app
frontend/privacy.html, frontend/terms.html  # served unauthenticated at /privacy, /terms —
                                             # required by the manifest's privacyUrl/termsOfUseUrl
                                             # for the Teams permission-consent dialog to complete
```
