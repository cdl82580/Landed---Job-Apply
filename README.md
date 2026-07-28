# job-apply — Landed

A Claude-powered web app (and Slack bot) that takes a job posting and produces a
tailored resume, ATS resume, cover letter, and interview prep doc in under 2 minutes.
Includes a full-featured application tracker, calendar, admin dashboard, webhook system, and audit logging.

**Live app:** https://apply.cdlav.us/

---

## Features

### Agent
- **Master Resume Builder** — guided Q&A wizard for users who don't have a master resume yet (or want to rebuild one): repeatable job/education/certification entries with add/remove bullet-point rows, one of three original DOCX templates (Technical/ATS-Clean, Traditional/Chronological, Executive/Leadership with a competency grid), optional single Claude polish pass that tightens wording without inventing facts or numbers. Smart input fields: native email/URL validation, live phone number formatting as-you-type (`libphonenumber-js`, national format per country), month/year date pickers for job dates, and search-as-you-type autocomplete for city fields (OpenStreetMap Nominatim, proxied server-side) and the education institution field (US Dept of Education College Scorecard, falling back to the Hipolabs universities API for schools outside the US — results show a logo.dev thumbnail, name/alias, and city/state/country). Generating a new resume saves it directly as the user's live master resume; if one already exists, generating a new one requires explicit overwrite confirmation and automatically keeps the replaced version as a one-slot backup (downloadable and restorable from the Profile page — see Auth & Accounts below). Pinned as the first card on the Agent page since it's the prerequisite for every other agent.
- **Tailored resume** — styled DOCX with brand colors, targeted bullets, competency grid
- **ATS resume** — plain single-column DOCX, no tables or text boxes, parser-safe
- **Cover letter** — voice-matched DOCX tailored to the role and hiring manager
- **Application Questions** — answer freeform application questions (e.g. "Describe a time you led a cross-functional team") using tailored resume, JD, and profile context; tone selector (professional/conversational/technical/concise), optional character limit, two-phase clarification flow (agent can ask follow-ups before answering), editable answer with copy-to-clipboard and refinement chips
- **Thank You Email** — post-interview thank-you email generator with app picker, round/tone selectors, optional interviewer name and key topics discussed; outputs editable email with subject line, copy-to-clipboard, DOCX download, and Google Drive upload
- **Interview Prep** — single-column, 2-page flowing DOCX (0.5" margins) with 6 numbered sections separated by rules: elevator pitch (~45-second spoken script + delivery/adapt-live notes), role & company snapshot (role, size/funding, leadership, stack, "how to read this company"), your story mapped to the company's own stated pillars/priorities, likely Q&A (ending on the single hardest/most probing question), smart questions to ask, and before-the-interview prep actions. Content is calibrated internally to the round type and focus/slant. Header shows the company logo (via logo.dev — same source used for logos everywhere else in the app, and it renders a light-on-dark card so a company whose only mark is a light/white logo still shows up against the doc's white background), interviewer(s) — one per line, freeform (name/role/LinkedIn, etc.) — and a Date · Time · Location logistics line, each part shown only if provided. Any `http(s)://` or `www.` URL anywhere in the header (LinkedIn profile, video-call link) is automatically rendered as a real clickable hyperlink, not plain text. Headers and dividers are tinted with the company's brand colors (primary + a real secondary accent, both via Brandfetch, falling back to Corey's default navy/teal). When the run is tied to a tracked application (`app_id`), Brandfetch is queried using that application's already-resolved domain rather than a fuzzy company-name search — a name search can match the wrong company entirely for a common/ambiguous name. Proof points restricted to last 10 years (Applause 2016+, ProdPerfect, HSP Group, eHealth, GitHub projects). Fidelity excluded.
- **Humanizer prompts** — each text-producing agent has a tailored humanizer directive: Voice Builder (resume/cover letter), Natural Flow Editor (interview prep), AI Pattern Remover (optimize resume), Human Rewrite (optimize cover letter), Authenticity Check (application questions), Voice Builder (thank you email)
- **GitHub portfolio** — FlowShift, task-api, and job-apply repos injected into every prep prompt as additional proof points
- **JD persistence** — job description saved as `job_description.md` to Google Drive on every run; when a JD is pasted and the run completes the file is written to the output folder and a `job_description` run is linked to the application so it auto-loads on subsequent runs and prep
- **Google Drive sync** — all output files uploaded automatically to your Drive folder; PDF version generated via Drive conversion
- **SSE progress streaming** — live log output while the agent runs; `done` event includes `replacements_warning` if XML edits partially failed
- **Machine pinning** — `machine_id` returned from POST endpoints; client sets `fly-force-instance-id` cookie before opening EventSource to guarantee SSE stream hits the same Fly.io machine
- **bfcache prevention** — Web Lock acquired on page load keeps all HTML pages ineligible for Chrome's back/forward cache; server-side middleware also injects `no-store` headers, `<meta>` cache tags, and a `pageshow` reload script into every HTML response
- **Branding asset cache-busting** — `favicon.png` and `img/{logo.png,logo-dark.png,logo-light.png,chat-icon.png,teams-color-icon.png,teams-outline-icon.png,slack-app-icon.png}` get the same `no-store` treatment as HTML (see `_NO_CACHE_PATHS` in `api.py`), since these filenames get overwritten in place whenever the logo/icon is redesigned and would otherwise serve stale cached bytes indefinitely

### Application Tracker
- Full CRUD for job applications — company (via Logo.dev search), role, status, recruiter, salary, DUA tracking
- **Match scoring** — Claude-powered resume↔JD fit score (0–100) with category badge (Strong Match / Good Match / Stretch / Long Shot) and per-dimension breakdown; Rescore button per row
- **DUA indicator** — tag and filter applications reported to unemployment (DUA)
- **Auto `date_applied`** — setting status to "Applied" automatically sets today's date if not already present
- **Run agent from tracker** — ▶ icon on each row opens the agent page with the app pre-selected and JD loaded from Drive
- **Setup Drive folder** — ⊕ icon on rows without a Drive folder creates the folder and attempts JD capture from the posting URL in the background
- Comments/notes system per application with timestamped history
- Linked agent runs — automatically links generated resumes/prep docs to applications; editing the posting URL re-captures the JD
- Sorting, filtering (status, match score, DUA, search), pagination
- CSV and formatted Excel export with frozen headers and alternating rows

### Calendar
- Create, view, update, and delete calendar events (interviews, deadlines, follow-ups, custom)
- Reminders via email (Resend) and/or Slack DM — configurable offset in minutes, multi-channel
- Per-user event cap (1,000); per-event reminder cap (10)
- Events linkable to application tracker records and run IDs
- Accessible from the web UI (`/calendar.html`) and all `/cal-*` Slack commands

### Auth & Accounts
- Email/password auth with scrypt hashing and HMAC-signed stateless session cookies (30-day TTL)
- **Google OAuth** — sign in with Google; auto-links to existing email/password accounts
- **Email verification** via Resend — verification banner shown until confirmed; all emails sent from `hello@cdlav.us`
- **Email change** — requires current password; triggers re-verification; invalidates existing session
- Role-based access: `user` and `admin` roles
- Admin accounts restricted to the admin dashboard only
- Per-request session validation checks `active` flag and password-change fingerprint (`pwv`)
- **Master resume backup** — every time the master resume is overwritten (manual upload, Slack/Teams DM, or the Resume Builder wizard), the previous version is automatically kept in a single backup slot; the Profile page shows a download link and a "Restore this version" button (restoring is a symmetric swap, not a delete, so it can be undone by restoring again)
- **Profile & Voice Guide wizard** — the profile guide (`profile.md`) is built through a 6-step guided wizard (Identity, Contact, Voice & Tone Rules, Key Stories, Preferences, Review) instead of a raw textarea, shared between `profile.html` and the registration flow (`register.html`, minus AI suggestions — no resume is on the server yet at signup). Identity and Key Stories steps have a "✨ Suggest" button that drafts from the user's actual master resume via `POST /api/profile/suggest`; Voice & Tone Rules comes prefilled with sensible defaults. An existing profile's markdown is best-effort parsed back into wizard fields on first open (anything that doesn't map to a known section, e.g. custom text or old Career Timeline/Skills sections, is preserved verbatim in an "Additional Notes" block rather than dropped). A "raw markdown" toggle on `profile.html` remains for power users; switching modes round-trips losslessly. Structured wizard answers are cached separately (`profile_fields.json`) so re-opening the wizard doesn't need to re-parse markdown — any `profile_text`-only write (raw-markdown save, or the Slack/Teams `/profile-guide` commands) clears that cache so it can't go stale.

### Security
- `Strict-Transport-Security`, `X-Frame-Options`, `X-Content-Type-Options`, `Content-Security-Policy`, `Referrer-Policy` on every response
- Rate limiting on login (10/min), register (5/hr), resend-verification (3/hr), change-password (5/hr), change-email (5/hr), resume-upload (10/hr), forgot-password (3/hr), reset-password (5/hr), company search (30/min)
- SSRF guard on webhook URLs (DNS resolution + private-net check, shared via `scripts/ssrf.py`), re-applied at delivery time; outbound webhook delivery does not follow redirects
- Webhook HMAC secrets encrypted at rest with AES-256-GCM (key derived from `SESSION_SECRET`; fails fast if unset)
- `safeHref()`/`esc()` applied to all user-supplied URLs and text rendered as `href`/`src`/`innerHTML` in the frontend; KB article HTML sanitized client-side with DOMPurify (`frontend/dompurify.min.js`) before rendering
- Password-reset tokens are single-use (bound to a password-hash fingerprint at issuance; invalidated the moment the password changes)
- Google Drive folder IDs are verified against the caller's actual Drive folders before being accepted into an application's linked runs
- Uploaded DOCX files are capped at 100 MB decompressed and hard-rejected on any DOCTYPE/malformed XML (no silent fallback)
- Audit log stored as individual S3 objects (atomic writes, no cross-machine race condition)
- Per-user record cache (30s TTL) avoids S3 round-trips on every authenticated request

### Admin Dashboard
- **Users** — manage all accounts, email verification, role, active/deactivated status; view runs count, last login, joined date; search, filter, sort, paginate
- **All Applications** — cross-user application oversight with full filtering, sorting, pagination, and Excel/CSV export
- **All Agent Runs** — persistent `AgentRun` records in S3 (`agent-runs/users/{user_id}/{run_id}.json`) with type, status, timing, Drive links, and scoring data; filters, sort, export
- **Audit Log** — unified event log across user and application events; server-side pagination, filter by event ID, action, actor, source, date range
- **Webhooks** — create and manage outbound webhooks for event streaming to Slack, MS Teams, Grafana Loki, and custom endpoints
- **Knowledge Base** — create, edit, and delete KB articles and categories; Quill WYSIWYG editor with Source/Preview toggle; seed KB from frontend constants; filter by category or search
- Admin pages have a dedicated header nav (wrench icon → admin dashboard, no tracker/agents/calendar links); lazy loading and mobile card layout on the users table

### Webhooks
- Event-driven delivery for every audit action
- Payload formats: Generic JSON, Slack Block Kit, MS Teams MessageCard, Grafana Loki
- Delivery filters: actor (email/user ID), source, action category, application ID
- HMAC-SHA256 signing (`X-Hub-Signature-256`) for receiver verification; secret encrypted at rest
- Per-webhook delivery history (last 25), stats, test button
- SSRF guard re-applied at delivery time (DNS rebinding protection)

### Knowledge Base
- Public KB at `/kb.html` — searchable article library with category sidebar; articles rendered from HTML body (Quill output)
- Admin-managed via the Knowledge Base tab in `/admin.html`
- Quill WYSIWYG editor with Source/Preview toggle in the article drawer
- Categories with icon (emoji), label, description, and optional `adminOnly` flag
- Seed endpoint (`POST /api/admin/kb/seed-from-file`) re-extracts the built-in article set from `frontend/kb.html` via Node.js
- Stored as a single JSON blob in Tigris (`kb/data.json`); seed data auto-applied if no blob exists yet

### Support Chatbot
- Floating chat widget (Chainlit Copilot) on every authenticated app page — `agents.html`, `tracking.html`, `admin.html`, `profile.html`, `calendar.html`, `api-docs.html`, and `kb.html` (gated behind a login check there, since that page is public)
- Mounted in-process at `/chat` via `chainlit.utils.mount_chainlit` — same FastAPI process/machine as the rest of the web app, no separate Fly process group
- Auth bridges the existing `session` cookie: `chainlit_app.py`'s `header_auth_callback` re-verifies it with the same HMAC secret as `api.py` (`scripts/session.py`) and carries the user's role into the chat session
- Answers are grounded in the Knowledge Base only — visible articles are filtered by the same `adminOnly` rule as `kb.html` (admin-only KB content never reaches a non-admin chat session), then stuffed into the system prompt fresh at chat start (no vector store)
- Admin sessions additionally get the full `README.md` (architecture, deployment, Slack/Teams command reference) as reference material — regular users never see it
- Branded with the app's brand colors (`public/theme.json`); the default Chainlit header logo is hidden since it didn't fit the widget's compact size
- `public/copilot-overrides.css` (loaded via `mountChainlitWidget`'s `customCssUrl`, served by Chainlit itself at `/chat/public/copilot-overrides.css`) fixes the vendored widget's `100vh`-based sizing — mobile Safari's address bar makes that unreliable in portrait — and widens the panel on small screens
- Requires `CHAINLIT_AUTH_SECRET` (Chainlit's own JWT signing secret — see Deployment) in addition to the existing `ANTHROPIC_API_KEY`
- Silent escalation: the model has a `flag_for_team` tool it calls when it can't answer from the KB, or the user reports a bug or leaves feedback. Before calling it for a bug or unanswerable question, it asks one short clarifying follow-up first (page/action, whether it's reproducible, error text, screenshot); straightforward feedback skips that. This fires a Resend email (to `SUPPORT_NOTIFY_EMAIL`, default `cdl825@gmail.com`) with the reason, a summary, and the full conversation transcript so far — the bot never mentions this to the user, it just acknowledges naturally. `reply_to` is set to the user's own email, so replying in the team's inbox reaches them directly; repeated escalations in the same chat session are threaded into one email via `Message-ID`/`In-Reply-To`/`References`. Rate-limited separately (3/hour/user) from the general chat rate limit
- Screenshots/files the user attaches are read into memory immediately (Chainlit's on-disk copies don't outlive the session) and forwarded as email attachments on escalation (capped at 5 files / 8MB each); images are also passed to Claude as vision content so it can actually see what the user attached
- Every conversation is persisted to Tigris (`chat-logs/{user_id}/{conversation_id}.json` + a per-user `_index.json` for fast listing — see `scripts/chat_logs.py`) and viewable admin-only via the **Chat Logs** tab in `/admin.html` (`routers/chat_logs.py`), including the full transcript, any escalations, and a hint pointing you at the threaded escalation email in your inbox to reply from (a mailto link can't carry `Message-ID`/`In-Reply-To` headers, so it can't thread — replying to the actual notification email does, via the `reply_to` set above)

### Slack Bot
See [Slack Commands](#slack-commands) section below.

### Teams Bot
See [Teams Commands](#teams-commands) section below. Unlike the Slack bot (which
always acts as the single primary account), the Teams bot resolves *which*
Landed account it's acting on behalf of per Teams user — see that section
for the identity-linking flow. `frontend/privacy.html` and `frontend/terms.html`
are served unauthenticated at `/privacy` and `/terms` — Teams' permission-consent
dialog fetches the manifest's `privacyUrl`/`termsOfUseUrl` directly and gets stuck
in a loop if either 404s or redirects to login.

### Test Runner Bot
`test_runner_bot.py` is a standalone Slack app (own bot token, signing secret,
and Socket Mode app token — see `TEST_RUNNER_BOT_TOKEN`/`TEST_RUNNER_SIGNING_SECRET`/
`TEST_RUNNER_APP_TOKEN`), kept separate because the main bot is at Slack's
25-command limit. Restricted to the single Slack user ID in
`TEST_RUNNER_SLACK_USER_ID`. Two commands:
- `/run-tests [unit | api | slack | all | ui-anon | ui | ui-admin]` — runs the
  matching pytest suite in a background thread and streams a live-updating
  result message (one run at a time; a concurrency guard rejects overlapping
  runs). UI suites drive Playwright against `UI_BASE_URL` using `UI_TEST_EMAIL`/
  `UI_TEST_PASSWORD`/`UI_ADMIN_EMAIL`/`UI_ADMIN_PASSWORD`.
- `/test-status` — reports whether a suite is currently running.

---

## Agents at a Glance

Seven distinct Claude-powered agents. Six are triggerable from the web app plus
Slack/Teams (CLI is resume-only); the Master Resume Builder is web-only since it's
a first-time setup flow, not something you'd run mid-conversation in a bot:

| Agent | What it produces | Web (Agent tab) | Slack | Teams | API |
|---|---|---|---|---|---|
| **Master Resume Builder** | A brand-new master resume DOCX from a guided Q&A wizard, saved as your live master resume | Build Master Resume | — | — | `POST /api/resume-builder` |
| **Resume + ATS + Cover Letter** | Tailored resume DOCX, ATS-safe resume DOCX, cover letter DOCX | Generate | `/apply` | `apply` | `POST /api/run` |
| **Application Questions** | Answer to a freeform application question, with clarification follow-ups | Application Questions | `/aq` | `aq` | `POST /api/aq` |
| **Interview Prep** | 2-page interview prep reference DOCX | Interview Prep | `/prep` | `prep` | `POST /api/prep` |
| **Thank You Email** | Post-interview thank-you email + DOCX | Thank You Email | `/thankyou` | `thankyou` | `POST /api/thankyou` |
| **Optimize** | In-place revision of an existing run's resume/cover letter from a freeform instruction | Optimize | `/optimize` | `optimize` | `POST /api/optimize` |
| **Rescore (Match Scoring)** | Resume↔JD fit score (0–100), category, and per-dimension breakdown | Tracker row → Score/Rescore button | `/rescore` | `rescore` | `POST /api/applications/{id}/score` |

The six job-application agents require a job description resolvable from a tracked
application (either a saved `job_description.md` in its Drive folder, or its posting
URL) except the first run of `apply`, which also accepts a pasted job posting directly.
The Master Resume Builder needs no JD at all — it only needs your work history, entered
directly in the wizard. See the KB's
["All the Agents"](https://apply.cdlav.us/kb.html#gs-agents-overview) article
for a plain-language walkthrough, or the per-command sections below ([Slack Commands](#slack-commands),
[Teams Commands](#teams-commands)) for exact syntax.

---

## Project Structure

```
job-apply/
├── api.py                     ← FastAPI backend (auth, runs, SSE, Drive proxy)
├── apply.py                   ← Core workflow engine + CLI entry point
├── chainlit_app.py            ← KB support chatbot (mounted at /chat via mount_chainlit)
├── chainlit.md                ← Support chatbot welcome text
├── .chainlit/config.toml      ← Chainlit app config
├── public/                    ← Chainlit branding (theme.json, logo_light/dark.png, favicon.png)
├── slack_bot.py               ← Slack bot (all slash commands)
├── slack_manifest.yml         ← Slack app manifest (copy into app config)
├── slack_manifest.json        ← Same manifest in JSON format
├── teams_bot/                 ← Microsoft Teams Bot Framework integration
│   ├── app.py                 ← aiohttp entry point (webhook receiver)
│   ├── bot.py                 ← ActivityHandler (commands + Adaptive Cards)
│   ├── api_client.py          ← HTTP client for FastAPI backend
│   ├── cards/                 ← Adaptive Card JSON templates
│   └── manifest/              ← Teams app manifest
├── CLAUDE.md                  ← Agent workflow instructions (Claude Code reads this)
├── profile.md                 ← Corey's voice, stories, metrics, do-not-use phrases
├── frontend/
│   ├── index.html             ← Public marketing landing page (redirects logged-in users to tracking.html/admin.html)
│   ├── agents.html            ← Agent SPA (run form, prep form, progress, results)
│   ├── tracking.html          ← Application tracker
│   ├── calendar.html          ← Calendar view
│   ├── admin.html             ← Admin dashboard (users, apps, runs, audit, webhooks, KB)
│   ├── kb.html                ← Public Knowledge Base (searchable, category sidebar)
│   ├── api-docs.html          ← API reference (rendered from Postman collection; ⬇ download button)
│   ├── login.html             ← Login + Google OAuth
│   ├── register.html          ← Signup, incl. the Profile & Voice Guide wizard (AI suggestions off)
│   ├── profile.html           ← Profile settings, incl. the Profile & Voice Guide wizard (+ raw markdown toggle)
│   ├── profile-wizard.js      ← Shared step-by-step Profile & Voice Guide wizard (profile.html + register.html)
│   ├── privacy.html           ← Privacy policy (public, unauthenticated — required by the Teams manifest's privacyUrl)
│   ├── terms.html             ← Terms of use (public, unauthenticated — required by the Teams manifest's termsOfUseUrl)
│   ├── marked.min.js          ← Bundled marked.js (used by profile.html, register.html, profile-wizard.js's review step)
│   ├── libphonenumber-min.js  ← Vendored libphonenumber-js UMD bundle (Resume Builder phone formatting)
│   └── img/                   ← logo.png + landing page assets (Slack/Teams brand icons, Unsplash photos, teams-color-icon.png + teams-outline-icon.png packaged into teams_bot/manifest, slack-app-icon.png for manual upload to the Slack app dashboard)
├── routers/
│   ├── applications.py        ← Tracker CRUD + comments + linked runs
│   ├── calendar.py            ← Calendar event + reminder CRUD
│   ├── companies.py           ← Logo.dev company search proxy
│   ├── lookups.py             ← Location (Nominatim) + institution (College Scorecard/Hipolabs) search proxies
│   ├── auth_google.py         ← Google OAuth flow
│   ├── admin.py               ← Admin-only endpoints + webhooks + audit
│   └── kb.py                  ← Knowledge Base CRUD (public list + admin create/update/delete/seed)
├── scripts/
│   ├── storage.py             ← Tigris S3 adapter
│   ├── resume_builder.py      ← Master Resume Builder: AI polish + Node/docx template rendering
│   ├── applications.py        ← Application storage layer
│   ├── calendar.py            ← Calendar + reminder storage layer
│   ├── session.py             ← Shared HMAC session token helpers
│   ├── user_audit.py          ← Per-user audit event log (per-event S3 objects)
│   ├── agent_runs.py          ← Persistent AgentRun records (per-run S3 objects)
│   ├── webhooks.py            ← Webhook storage + delivery engine
│   ├── email_verification.py  ← One-time verification tokens
│   └── office/                ← DOCX unpack / pack / validate
├── resumes/
│   └── master.docx            ← Source-of-truth resume (never use an output file)
├── output/                    ← Generated files (gitignored)
├── requirements.txt
├── Dockerfile
└── fly.toml
```

---

## Web App Usage

1. Go to https://apply.cdlav.us/ — public marketing/landing page (`frontend/index.html`); logged-in visitors are auto-redirected to `/tracking.html` (or `/admin.html`)
2. Register (email/password or Google), upload `master.docx` (or use the **Build Master Resume** wizard on the Agent tab if you don't have one yet), and answer the guided **Profile & Voice Guide** wizard
3. **Agent tab** — paste a job posting, enter company + role, hit **Generate**; use **Application Questions** to draft answers to supplemental app questions; or use **Interview Prep** for a prep doc
4. **Tracker tab** — track applications, add notes, link to agent runs
5. **Calendar tab** — view and manage interview events and deadlines with Slack/email reminders
6. **Knowledge Base** (`/kb.html`) — searchable help articles; admin-managed via the KB tab in the admin dashboard
7. **API Reference** (`/api-docs.html`) — full endpoint browser rendered from the Postman collection, with a ⬇ download button for the collection JSON
8. **Profile** — update display name, email, password, profile guide (guided wizard, or raw markdown), and resume
9. Admins are redirected to `/admin.html` automatically

### Build Master Resume
- For users with no master resume on file yet — the card auto-expands and is pinned first on the Agent tab until you have one
- Guided wizard: résumé style (Technical/ATS-Clean, Traditional/Chronological, or Executive/Leadership), contact info, repeatable work-experience entries (each with add/remove bullet-point rows), education, certifications, skills
- Smart fields: native email/URL validation, live phone formatting as you type, month/year pickers for job dates, and search-as-you-type autocomplete for city fields and the education institution field (shows a logo, name, and city/state/country — pick a result or keep typing freely, e.g. "Remote")
- Optional single AI polish pass tightens wording without inventing facts, numbers, or achievements you didn't provide
- On completion the DOCX is both offered as a direct download and saved as your live master resume — every other agent can use it immediately, no separate upload step
- If you already have a master resume, generating a new one asks for confirmation and automatically keeps the replaced version as a downloadable/restorable backup (see Profile page)

### Application Questions
- Select an existing tracker application — requires a saved `job_description.md` in the app's Drive folder
- Paste the question from the application form, choose a tone, and optionally set a character limit
- The agent may ask clarifying questions before generating (e.g., "Which project should I highlight?") — answer them and it refines
- Output: editable answer with live character count, copy to clipboard, and follow-up refinement chips

### Interview Prep
- Select an existing tracker application — requires a saved `job_description.md` in the app's Drive folder (run the resume agent first if it doesn't exist)
- Enter interview round and optional focus/slant; company and role auto-fill from the selected application
- Optional: interviewer(s) (freeform, one per line — name/role/LinkedIn, etc.), date, time, location (address or video call link) — each shown in the doc header only if provided
- Output: single-column 2-page DOCX uploaded to Drive and available for download, with company logo and brand colors in the header when Brandfetch has data for the company

---

## Slack Commands

| Category | Command | Description |
|---|---|---|
| 🤖 Agent | `/apply` | Generate resume + ATS resume + cover letter (picks a tracked application) |
| 🤖 Agent | `/aq` | Answer an application question using your resume & JD (picks a tracked application) |
| 🤖 Agent | `/prep` | Generate interview prep document (picks a tracked application) |
| 🤖 Agent | `/thankyou` | Generate a post-interview thank-you email |
| 🤖 Agent | `/optimize` | Refine an existing run's documents from a prompt (picks most recent Drive folder) |
| 🤖 Agent | `/rescore` | Re-score resume/JD match for an application |
| 🤖 Agent | `/runs` | List recent Drive run folders |
| 📅 Calendar | `/cal-today` | Show today's events |
| 📅 Calendar | `/cal-week` | Show next 7 days |
| 📅 Calendar | `/cal-add` | Add a calendar event (modal — type, date, time, timezone, reminders, linked app) |
| 📅 Calendar | `/cal-view` | View full details of an event |
| 📅 Calendar | `/cal-delete` | Delete an event (two-step confirm) |
| 📋 Tracker | `/tracker` | Pipeline summary by status |
| 📋 Tracker | `/track-list [status]` | List applications (optional status filter) |
| 📋 Tracker | `/track-view` | View full details of an application, including a link to its linked Google Drive output folder (most recently linked run) if one exists |
| 📋 Tracker | `/track-add` | Add a new application (Logo.dev company search, all fields except priority) |
| 📋 Tracker | `/track-update` | Two-step: pick app → edit all fields pre-filled (setting status to Applied auto-sets date applied) |
| 📋 Tracker | `/track-note` | Add a comment to an application |
| 📋 Tracker | `/track-delete` | Delete an application (two-step confirm) |
| 🔍 Lookup | `/company [name]` | Search company info via Logo.dev |
| 🔍 Lookup | `/whoami` | Show your account details |
| 👤 Profile | `/profile-resume` | Instructions for uploading a new master resume via DM |
| 👤 Profile | `/profile-guide` | Edit your profile & voice guide (modal, pre-filled) |
| 👤 Profile | `/notifications` | View and toggle email notification preferences |
| 🛠️ System | `/help` | Full command reference |

The bot also publishes a dynamic **App Home tab** showing live pipeline stats, upcoming calendar events, and a quick command reference — opens when you click the app's Home tab in Slack.

**`/apply`/`/prep`/`/aq` only run against a tracked application** — same constraint as the Teams bot. Each modal's "Application" field is a `static_select` populated from `/api/applications` instead of free-text company/role. Submitting looks up a saved job posting from the application's most recently linked Drive folder (`_get_saved_job_posting`); if one exists, the run starts immediately, otherwise Slack updates the same modal in place (`response_action: "update"`, with the already-collected fields carried forward via `private_metadata`) to ask for the JD once.

**Resume upload via DM:** Drop a `.docx` file into a DM with the bot to update your master resume. The bot validates the file is a valid ZIP archive and runs it through `pandoc` to verify it can extract usable text (≥200 characters) — the same path the agents use at runtime. If validation fails, you'll get a warning with instructions to re-export.

---

## Teams Commands

| Category | Command | Description |
|---|---|---|
| 🤖 Agent | `apply` | Generate resume + ATS resume + cover letter |
| 🤖 Agent | `aq` | Answer an application question |
| 🤖 Agent | `prep` | Generate interview prep doc |
| 🤖 Agent | `thankyou` | Generate a post-interview thank-you email |
| 🤖 Agent | `optimize` | Refine existing run documents |
| 🤖 Agent | `rescore` | Re-score resume/JD match for an application |
| 📋 Tracker | `tracker` | Pipeline summary |
| 📋 Tracker | `track list [status]` | List applications |
| 📋 Tracker | `track add` | Add a new application |
| 📋 Tracker | `track view` | View application details, including a link to its linked Google Drive output folder (most recently linked run) if one exists |
| 📋 Tracker | `track update` | Two-step: pick app → edit all fields pre-filled |
| 📋 Tracker | `track note` | Add a comment to an application |
| 📋 Tracker | `track delete` | Delete an application (two-step confirm) |
| 📅 Calendar | `cal today` | Show today's events |
| 📅 Calendar | `cal week` | Show events in the next 7 days |
| 📅 Calendar | `cal add` | Add a calendar event (with an optional email reminder) |
| 📅 Calendar | `cal view` | View full details of an event |
| 📅 Calendar | `cal delete` | Delete an event (two-step confirm) |
| 🔍 Lookup | `company [name]` | Search company info via Logo.dev |
| 👤 Profile | `profile resume` | Instructions for uploading a new master resume (attach a `.docx` directly to the chat) |
| 👤 Profile | `profile guide` | Edit your profile & voice guide |
| 👤 Profile | `notifications` | View and toggle email notification preferences |
| 🔑 Account | `confirm` | Link your Teams identity to a Landed account |
| 🔑 Account | `whoami` | Show which account you're linked as |
| 🔑 Account | `unlink` | Remove your Teams identity's link |
| 🛠️ System | `runs` | List recent Drive run folders |
| 🛠️ System | `help` | Command reference |

**Identity linking:** the Teams bot has no built-in notion of "logged in." The first
time a Teams user runs any command other than `help`/`confirm`/`unlink`, the bot
looks up a `teams_links/{aad_object_id}.json` record in Tigris (`scripts/teams_links.py`).
If missing or expired, it fetches the caller's email via the Bot Framework's
`TeamsInfo.get_member()` roster API, checks whether a Landed account exists for
that email, and — if so — asks the user to reply `confirm`. Only after that explicit
confirmation does it persist the link (30-day expiry, then re-confirmation is
required). Every subsequent API call the bot makes on that user's behalf carries an
`X-Teams-User-Email` header so `api.py:_bot_user()` resolves that specific account
instead of the shared primary account the Slack bot uses.

**`apply`/`prep`/`aq`/`thankyou` only run against a tracked application** —
there's no free-text company/role entry. Each form's "Application" field is
an Adaptive Card dynamic typeahead searching the caller's own tracked
applications, backed by an `application/search` invoke handler
(`teams_bot/bot.py:_search_my_applications`) since the Bot Framework Python SDK
doesn't dispatch that invoke name itself. Teams' dynamic-search response
schema only supports `{title, value}` per result (no icon/image field), so
the company logo can't appear in the dropdown itself — it shows once an
application is picked, on whichever card the selection lands on. Submitting
the application picks up a saved `job_description.md` from the application's
most recently linked Drive folder automatically if one exists; otherwise a
follow-up card asks the user to paste the job posting once, and that gets
used for the run.

**`track update`/`track note`/`track delete`** mirror the Slack modal flows:
`track update` is two steps (pick an application, then edit a form pre-filled
from the current record — status, date applied, job source, location, salary,
URL, DUA flag, recruiter, plus an optional note) via `PUT /api/applications/{id}`;
`track note` posts a comment via `POST /api/applications/{id}/comments`; `track
delete` shows a destructive-styled confirm card before calling
`DELETE /api/applications/{id}`.

**`cal today`/`cal week`/`cal add`/`cal view`/`cal delete`** mirror Slack's
`/cal-*` commands against the same `/api/calendar` endpoints. `cal add`
simplifies Slack's reminder-channel checkbox (email + Slack) down to a single
"Email reminder" toggle, since there's no Teams-side reminder delivery channel
implemented server-side to pair with it. Local date/time + IANA timezone are
converted to UTC client-side (`_local_to_utc_iso`, same approach as
slack_bot.py's `_local_to_utc_iso`) before being sent to the API.

**`profile resume` uploads via a direct file attachment**, not a slash-command
argument — attach a `.docx` to the chat and `_handle_file_upload` picks it up
automatically. This requires `"supportsFiles": true` in
`teams_bot/manifest/manifest.json` (already set); Teams sends a
`FileDownloadInfo` attachment with a pre-authenticated `downloadUrl` that the
bot can fetch directly, no bearer token needed (unlike Slack, whose file
download requires the bot token). `company`, `profile guide`, and
`notifications` mirror their Slack counterparts directly against
`/api/companies/search` and `PUT /api/profile`.

---

## CLI Usage

```bash
pip install -r requirements.txt && npm install
export ANTHROPIC_API_KEY=sk-ant-...

python apply.py --job jobs/job.txt --company "Acme" --role "Solutions Engineer"
python apply.py --job jobs/job.txt --company "Acme" --role "SE" --contact "Jane Smith"
python apply.py --job jobs/job.txt --company "Acme" --role "SE" --debug
python apply.py --job jobs/job.txt --company "Acme" --role "SE" --dry-run
```

Output files land in `output/[Company]_[Role]/`:
- `Resume_CoreyLaverdiere_[Company]_[Role].docx`
- `Resume_CoreyLaverdiere_[Company]_[Role]_ATS.docx`
- `CoverLetter_CoreyLaverdiere_[Company]_[Role].docx`
- `job_description.md` — saved for future JD auto-load

---

## Local Development

```bash
pip install -r requirements.txt && npm install

export ANTHROPIC_API_KEY=sk-ant-...
export SESSION_SECRET=any-random-string
export CHAINLIT_AUTH_SECRET=...  # generate with: python -m chainlit create-secret

# Tigris S3 (user accounts, resumes, profiles, tracker data)
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_ENDPOINT_URL_S3=https://fly.storage.tigris.dev
export BUCKET_NAME=job-apply-corey

uvicorn api:app --reload --port 8000
open http://localhost:8000
```

---

## Google Drive Setup (one-time)

```bash
# Download OAuth credentials from Google Cloud Console
# APIs & Services → Credentials → Create → OAuth client ID → Desktop app
# Save as: gdrive_credentials.json (project root)
python3 setup_gdrive.py

# On Fly.io — push token as a secret:
fly secrets set GDRIVE_TOKEN_JSON="$(cat ~/.config/job-apply/gdrive_token.json)"
```

---

## Google OAuth Setup (one-time)

1. Google Cloud Console → APIs & Services → Credentials → OAuth 2.0 Client → Web application
2. Authorized redirect URI: `https://apply.cdlav.us/api/auth/google/callback`
3. `fly secrets set GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=...`

---

## Deployment (Fly.io)

```bash
fly deploy --app job-apply-corey
```

The app runs as **three process groups** on Fly.io (defined in `fly.toml`), each scaled to **1 machine**:

| Process | Command | Machine | Notes |
|---|---|---|---|
| `web` | `uvicorn api:app …` | 1 GB, auto-stop | FastAPI web server — 1 machine required (SSE state is in-memory) |
| `bot` | `python slack_bot.py` | 512 MB, always-on | Slack Socket Mode bot (needs Playwright/Chromium for UI tests) |
| `testrunner` | `python test_runner_bot.py` | 512 MB, always-on | Standalone test-runner bot — separate Slack app, see [Test Runner Bot](#test-runner-bot) |

> **Important:** Keep `web` scaled to exactly 1 machine. Run and prep state is held
> in-memory; multiple web machines will cause SSE streams to 404 on the wrong instance.
> If you need to scale, replace the in-memory `_runs`/`_preps`/`_app_questions` dicts with a shared store (Redis, etc.).

All three process groups share the same Docker image and all Fly secrets.

**Required secrets:**

| Secret | Description |
|--------|-------------|
| `ANTHROPIC_API_KEY` | Claude API key |
| `SESSION_SECRET` | HMAC signing key for session tokens (also used to derive webhook secret encryption key) |
| `CHAINLIT_AUTH_SECRET` | JWT signing secret for the mounted Chainlit support chatbot — generate with `chainlit create-secret` |
| `AWS_ACCESS_KEY_ID` | Tigris key |
| `AWS_SECRET_ACCESS_KEY` | Tigris secret |
| `AWS_ENDPOINT_URL_S3` | `https://fly.storage.tigris.dev` |
| `BUCKET_NAME` | Tigris bucket name |
| `RESEND_API_KEY` | Resend — email verification, password-change, and calendar reminder emails |
| `RESEND_FROM` | Sender address (default: `Landed <hello@cdlav.us>`) |
| `SUPPORT_NOTIFY_EMAIL` | Where the KB chatbot's `flag_for_team` tool sends its silent escalation emails (default: `cdl825@gmail.com`) |
| `APP_URL` | Public app URL (default: `https://apply.cdlav.us`) |
| `APP_USER_EMAIL` | Primary user email — used by the Slack bot (always) and Teams bot (fallback, before/without a per-user link) to resolve API identity |
| `GOOGLE_CLIENT_ID` | Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | Google OAuth client secret |
| `LOGODEV_API_KEY` | Logo.dev secret key (`sk_`) for company search API |
| `COLLEGE_SCORECARD_API_KEY` | US Dept of Education College Scorecard API key — powers the Resume Builder's institution search (`GET /api/lookups/institutions`); falls back to the (keyless) Hipolabs universities API for non-US schools or if unset |
| `BOT_API_KEY` | Shared secret between the Slack bot, Teams bot, and web API |
| `SLACK_BOT_TOKEN` | Slack bot token (`xoxb-...`) |
| `SLACK_SIGNING_SECRET` | Slack signing secret |
| `SLACK_APP_TOKEN` | App-level token (`xapp-...`) — **required** for Socket Mode |
| `SLACK_NOTIFY_USER_ID` | Slack user ID to DM for calendar reminders |
| `MICROSOFT_APP_ID` | Azure Bot Framework app ID (Teams bot channel auth) |
| `MICROSOFT_APP_PASSWORD` | Azure Bot Framework app password |
| `MICROSOFT_APP_TENANT_ID` | Azure AD tenant ID (single-tenant Teams app) |
| `GDRIVE_TOKEN_JSON` | Google Drive OAuth token JSON |
| `GDRIVE_PARENT_FOLDER_ID` | Drive folder ID for run output (`Job Applications`) |
| `TEST_RUNNER_SLACK_USER_ID` | Slack user ID authorised to run `/run-tests` (falls back to `SLACK_NOTIFY_USER_ID`) |

---

## Admin Dashboard

Navigate to `/admin.html` (admins are redirected there automatically on login).

Use `?tab=` to deep-link to a specific tab:
- `/admin.html?tab=users`
- `/admin.html?tab=applications`
- `/admin.html?tab=runs`
- `/admin.html?tab=auditlog`
- `/admin.html?tab=webhooks`
- `/admin.html?tab=kb`
- `/admin.html?tab=chatlogs`

---

## API

See `JobApply.postman_collection.json` for the full request/response reference.

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/health` | — | Liveness check (full details only for authenticated users) |
| POST | `/api/auth/register` | — | Create account + upload resume |
| POST | `/api/auth/login` | — | Get session cookie |
| POST | `/api/auth/logout` | cookie | Clear session |
| GET | `/api/auth/me` | cookie | Current user info + role + email_verified + active model |
| GET | `/api/auth/google` | — | Start Google OAuth flow |
| GET | `/api/auth/google/callback` | — | Google OAuth callback |
| GET | `/api/auth/verify-email?token=` | — | Consume email verification token |
| POST | `/api/auth/resend-verification` | cookie | Resend verification email |
| POST | `/api/auth/forgot-password` | — | Send password-reset link to email (always returns 200; rate-limited: 3/hr) |
| POST | `/api/auth/reset-password` | — | Set new password using a one-time reset token (expires in 1 hour; rate-limited: 5/hr) |
| GET | `/api/profile` | cookie | Profile + resume metadata, including `profile_fields_json` (structured Profile Wizard answers, if any) |
| PUT | `/api/profile` | cookie | Update display name, profile text, and/or structured `profile_fields_json`. `profile_fields_json` is derived strictly from this request — a dict persists it, anything else (omitted or `null`) clears it, so raw-markdown edits (including the Slack/Teams `/profile-guide` commands, which only ever send `profile_text`) can't leave a stale wizard snapshot behind |
| POST | `/api/profile/suggest` | cookie | AI-draft one Profile Wizard section (`section: "identity"` or `"stories"`) grounded in the user's master resume (rate-limited: 30/hr) |
| POST | `/api/profile/resume` | cookie | Replace master resume (rate-limited: 10/hr); the previous version is automatically kept as a one-slot backup |
| GET | `/api/profile/resume/previous` | cookie | Download the previous master resume backup (404 if none) |
| POST | `/api/profile/resume/restore` | cookie | Swap the backup back to being the live master resume (symmetric swap, not a delete; rate-limited: 10/hr) |
| POST | `/api/profile/password` | cookie | Change password (rate-limited: 5/hr) |
| POST | `/api/profile/email` | cookie | Change email — requires current password, sends re-verification, invalidates session (rate-limited: 5/hr) |
| GET | `/api/audit/me` | cookie | Current user's audit event log |
| GET | `/api/calendar` | cookie | List events (optional `?from=&to=` ISO range filter) |
| POST | `/api/calendar` | cookie | Create event with optional reminders |
| GET | `/api/calendar/upcoming` | cookie | Next 7 days (used by Slack home tab) |
| GET | `/api/calendar/{id}` | cookie | Get single event |
| PUT | `/api/calendar/{id}` | cookie | Update event (reminders recalculated if datetime changes) |
| DELETE | `/api/calendar/{id}` | cookie | Delete event + all its reminders |
| GET | `/api/applications` | cookie | List applications (paginated) |
| POST | `/api/applications` | cookie | Create application |
| GET | `/api/applications/{id}` | cookie | Get full application record |
| PUT | `/api/applications/{id}` | cookie | Update application — auto-sets `date_applied` on status→Applied; re-captures JD if `url` changes |
| DELETE | `/api/applications/{id}` | cookie | Delete application |
| GET | `/api/applications/{id}/audit` | cookie | Application-level audit log |
| POST | `/api/applications/{id}/comments` | cookie | Add comment |
| PUT | `/api/applications/{id}/comments/{cid}` | cookie | Edit comment |
| DELETE | `/api/applications/{id}/comments/{cid}` | cookie | Delete comment |
| GET | `/api/applications/{id}/runs` | cookie | List Drive runs linked to an application |
| POST | `/api/applications/{id}/runs` | cookie | Link a Drive run to an application |
| DELETE | `/api/applications/{id}/runs/{lid}` | cookie | Unlink a run |
| POST | `/api/applications/{id}/score` | cookie | Run (or re-run) resume↔JD match scoring; persists result to record |
| POST | `/api/applications/{id}/extract-jd` | cookie | Extract JD text from the app's posting URL via Claude |
| POST | `/api/applications/{id}/setup-folder` | cookie | Create Drive folder + attempt JD capture in background; returns 202 immediately |
| POST | `/api/applications/{id}/pipeline/jd` | cookie | Re-run the setup pipeline with a manually pasted JD, for when automatic extraction from the posting URL fails; returns 202 |
| GET | `/api/applications/{id}/pipeline/stream` | cookie | SSE stream of setup-folder/pipeline-jd progress (Drive folder → JD capture → match scoring); 404 once evicted or never started |
| GET | `/api/companies/search?q=` | — | Logo.dev company search — returns `name`, `domain`, `description`; logos constructed client-side via `img.logo.dev` |
| GET | `/api/lookups/locations?q=` | cookie | City/place autocomplete (OpenStreetMap Nominatim) for the Resume Builder's location fields — returns `label`, `city`, `state`, `country`, `country_code` |
| GET | `/api/lookups/institutions?q=` | cookie | School autocomplete for the Resume Builder's education fields — US Dept of Education College Scorecard, falling back to the Hipolabs universities API for non-US schools (only on a zero-match) — returns `name`, `alias`, `city`, `state`, `country`, `domain`; add `&source=international` to search Hipolabs directly, since a Scorecard hit that just isn't the *right* school (e.g. "Oxford") would otherwise never fall through |
| POST | `/api/resume-builder` | cookie | Build a new master resume from the wizard's structured Q&A data → returns `{run_id, machine_id}`; 409 if a master resume already exists and `confirm_overwrite` isn't set; admin accounts cannot use this |
| GET | `/api/resume-builder/{id}/stream` | cookie | SSE progress stream (`done` event includes `files.resume`) |
| GET | `/api/resume-builder/{id}/status` | cookie | Poll resume-builder run status |
| GET | `/api/resume-builder/{id}/files/{name}` | cookie | Download the generated master resume DOCX |
| POST | `/api/run` | cookie | Start resume generation run → returns `{run_id, machine_id}`; accepts `jd_folder_id` to load JD server-side from Drive |
| GET | `/api/run/{id}/stream` | cookie | SSE progress stream (`done` event includes `replacements_warning` if < 70% XML edits succeeded) |
| GET | `/api/run/{id}/status` | cookie | Poll run status |
| GET | `/api/run/{id}/files/{name}` | cookie | Download output file |
| POST | `/api/prep` | cookie | Start interview prep run → returns `{prep_id, machine_id}` |
| GET | `/api/prep/{id}/stream` | cookie | SSE prep progress stream |
| GET | `/api/prep/{id}/status` | cookie | Poll prep status |
| GET | `/api/prep/{id}/files/{name}` | cookie | Download prep DOCX |
| POST | `/api/aq` | cookie | Start application question run → returns `{aq_id, machine_id}`; agent may emit `clarification` SSE event |
| POST | `/api/aq/{id}/clarify` | cookie | Submit clarification answers to unblock a paused AQ run |
| GET | `/api/aq/{id}/stream` | cookie | SSE stream: `progress`, `clarification`, `done` (answer + char_count + follow_ups), `error` |
| GET | `/api/aq/{id}/status` | cookie | Poll AQ status |
| POST | `/api/thankyou` | cookie | Start thank-you email run → returns `{ty_id, machine_id}` |
| GET | `/api/thankyou/{id}/stream` | cookie | SSE stream: `progress`, `done` (email_text + subject + files), `error` |
| GET | `/api/thankyou/{id}/status` | cookie | Poll thank-you status |
| GET | `/api/thankyou/{id}/files/{name}` | cookie | Download thank-you DOCX |
| POST | `/api/optimize` | cookie | Optimize an existing run's resume/cover letter in place per a user instruction → returns `{optimize_id, machine_id}`; folder ownership verified via Tigris app records; rate-limited to one active optimize per user |
| GET | `/api/optimize/{id}/stream` | cookie | SSE optimize progress stream (`done` event includes `change_summary` list + `replacements_warning`) |
| GET | `/api/optimize/{id}/status` | cookie | Poll optimize status: `queued | running | done | error` |
| GET | `/api/optimize/{id}/files/{name}` | cookie | Download optimized DOCX |
| POST | `/api/jd/format` | cookie | AI-format a raw job description (returns cleaned Markdown) |
| GET | `/api/postman` | — | Download the Postman collection JSON |
| POST | `/api/messages` | Bot Framework JWT | Microsoft Teams Bot Framework webhook (Azure Bot → here) |
| POST | `/api/teams/link-status` | bot key | Has this Teams identity (`aad_object_id`) been linked to a Landed account? |
| POST | `/api/teams/account-lookup` | bot key | Does a Landed account exist for this email? |
| POST | `/api/teams/link-confirm` | bot key | Link a Teams identity to the account for this email (404 if no such account) |
| POST | `/api/teams/link-token` | bot key | Issue a short-lived token for `/teams-link.html` — lets a user with no account under their Teams email sign in (password or Google) to link an existing account under a different one |
| POST | `/api/teams/link-claim` | cookie | Claim a `link-token` for whichever account the caller is currently signed in as (called by `/teams-link.html` after login) |
| POST | `/api/teams/unlink` | bot key | Remove a Teams identity's link |
| GET | `/api/agent-runs` | cookie | List structured agent run records for the current user (type, status, timing, Drive links) |
| GET | `/api/gdrive/runs` | cookie | List Drive run folders |
| GET | `/api/gdrive/runs/{folder_id}/job_posting` | cookie | Fetch saved JD from Drive — prefers `job_description.md`, falls back to `job_posting.txt`; ownership verified via Tigris app records |
| PUT | `/api/gdrive/runs/{folder_id}/job_posting` | cookie | Upsert `job_description.md` in Drive folder |
| GET | `/api/runs` | cookie | List local run folders by user |
| GET | `/api/runs/{folder}/job_posting` | cookie | Fetch saved JD from local run folder |
| GET | `/api/config/model` | cookie | Get active Claude model |
| PUT | `/api/config/model` | admin | Set active Claude model |
| GET | `/api/config/models` | admin | List allowed models |
| GET | `/api/kb/articles` | cookie | List all KB articles + categories |
| GET | `/api/kb/articles/{id}` | cookie | Get one KB article |
| GET | `/api/kb/categories` | cookie | List KB categories |
| POST | `/api/admin/kb/articles` | admin | Create KB article |
| PUT | `/api/admin/kb/articles/{id}` | admin | Update KB article |
| DELETE | `/api/admin/kb/articles/{id}` | admin | Delete KB article |
| POST | `/api/admin/kb/categories` | admin | Create KB category |
| PUT | `/api/admin/kb/categories/{id}` | admin | Update KB category |
| DELETE | `/api/admin/kb/categories/{id}` | admin | Delete KB category |
| POST | `/api/admin/kb/seed` | admin | Replace entire KB from JSON payload |
| POST | `/api/admin/kb/seed-from-file` | admin | Re-extract KB from `frontend/kb.html` via Node.js and seed to Tigris |
| GET | `/api/notifications/action?token=` | — | Consume a signed one-time notification token (from nudge/follow-up emails) — executes `status` or `snooze` action; redirects to tracker on success |
| GET | `/api/notifications/confirm-applied?token=` | — | Signed-token date picker page for "Applied" status without a pre-set date |
| POST | `/api/notifications/confirm-applied` | — | Submit the applied date from the confirm-applied form |
| GET | `/api/admin/users` | admin | List all users |
| PUT | `/api/admin/users/{id}` | admin | Edit user (name, email, role, active, verified) — invalidates user cache |
| PUT | `/api/admin/users/{id}/role` | admin | Set user role only (`user`/`admin`) — Slack bot compat; invalidates user cache |
| POST | `/api/admin/users/{id}/resend-verification` | admin | Resend verification as admin |
| GET | `/api/admin/users/{id}/applications` | admin | List all applications for a specific user |
| GET | `/api/admin/applications` | admin | All applications across all users |
| GET | `/api/admin/applications/{uid}/{aid}` | admin | Full application record |
| PUT | `/api/admin/applications/{uid}/{aid}` | admin | Admin update application |
| DELETE | `/api/admin/applications/{uid}/{aid}` | admin | Admin delete application |
| POST | `/api/admin/applications/{uid}/{aid}/comments` | admin | Admin add comment |
| GET | `/api/admin/runs` | admin | All agent run records across all users |
| GET | `/api/admin/audit` | admin | Unified audit log (paginated) |
| GET | `/api/admin/audit/export` | admin | Full audit log (no pagination, for export) |
| GET | `/api/admin/audit/action-types` | admin | Known audit action type list |
| POST | `/api/admin/log-activity` | admin | Log a custom admin activity event |
| GET | `/api/admin/webhooks` | admin | List webhooks |
| POST | `/api/admin/webhooks` | admin | Create webhook (secret encrypted at rest) |
| GET | `/api/admin/webhooks/{id}` | admin | Get webhook details (secret redacted) |
| PUT | `/api/admin/webhooks/{id}` | admin | Update webhook |
| DELETE | `/api/admin/webhooks/{id}` | admin | Delete webhook |
| POST | `/api/admin/webhooks/{id}/test` | admin | Send test delivery |
| GET | `/api/admin/webhooks/{id}/deliveries` | admin | Last 25 deliveries |
| GET | `/api/admin/chat-logs` | admin | All KB support-chatbot conversations across all users, newest first |
| GET | `/api/admin/chat-logs/{user_id}/{conversation_id}` | admin | Full transcript for one conversation |

---

## Maintaining the Agent

### If XML replacements start failing
Run with `--debug` to inspect `unpacked/word/document.xml`. Section text drifts
when `master.docx` is edited in Word — update known strings in `profile.md`. The
SSE `done` event now includes a `replacements_warning` field if < 70% of edits
succeeded, so the web UI can surface it without requiring a log review.

### If cover letter voice drifts
Edit `profile.md` → "Voice & Tone Rules" and "DO NOT" sections.

### If framing angle is consistently wrong for a role type
Edit `CLAUDE.md` → "Common Role Type → Framing Angle Reference" table.

### If interview prep content is too verbose
The prompt in `apply.py` (`generate_interview_prep`) has hard `MAX N WORDS` limits
per field. Tighten these if Claude is still over-generating.

### If interview prep proof points reference old roles
The recency rule is enforced in the prompt: only Applause (2016+), ProdPerfect,
HSP Group, eHealth, and GitHub projects are allowed. Fidelity is explicitly excluded.

### If Google Drive token expires
The token is refreshed automatically and persisted to Tigris (`system/gdrive_token.json`) so it survives container restarts. If it's fully revoked (e.g. after revoking app access in Google Account settings), re-authorize:
```bash
rm ~/.config/job-apply/gdrive_token.json
python3 setup_gdrive.py
fly secrets set GDRIVE_TOKEN_JSON="$(cat ~/.config/job-apply/gdrive_token.json)"
# Then clear the stale Tigris copy so the new token takes precedence on next boot:
fly ssh console -C "python3 -c \"from scripts import storage; storage.delete_text('system/gdrive_token.json')\""
fly deploy --app job-apply-corey
```

### If webhook deliveries are being blocked
The SSRF guard runs at both write time and delivery time. A `Delivery blocked` error
in the delivery log means the URL resolved to a private/internal IP at delivery time
(possible DNS rebinding). Update the webhook URL to a public endpoint.

### Webhook secret rotation
Webhook secrets are encrypted with AES-256-GCM using a key derived from `SESSION_SECRET`.
If you rotate `SESSION_SECRET`, existing webhook secrets will fail to decrypt (delivery
will silently send unsigned requests). Re-save each webhook via `PUT /api/admin/webhooks/{id}`
with the secret to re-encrypt under the new key.
