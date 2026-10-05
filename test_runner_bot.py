"""
test_runner_bot.py — Standalone Slack bot for running automated test suites.

A separate app from the main Landed bot (which is at the 25-command limit).
Only exposes two commands:
  /run-tests [unit | api | slack | all | ui-anon | ui | ui-admin]
  /test-status

Access is restricted to the Slack user ID set in TEST_RUNNER_SLACK_USER_ID.

Environment variables required:
  TEST_RUNNER_BOT_TOKEN       xoxb-... token for this app
  TEST_RUNNER_SIGNING_SECRET  signing secret for this app
  TEST_RUNNER_APP_TOKEN       xapp-... for Socket Mode
  TEST_RUNNER_SLACK_USER_ID   Slack user ID allowed to run commands

Environment variables (for UI tests — already set as Fly secrets):
  UI_BASE_URL         Target app URL (default: https://apply.cdlav.us)
  UI_TEST_EMAIL       Standard test user email
  UI_TEST_PASSWORD    Standard test user password
  UI_ADMIN_EMAIL      Admin test user email
  UI_ADMIN_PASSWORD   Admin test user password
"""

import os
import re
import subprocess
import sys
import threading

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BOT_TOKEN      = os.environ["TEST_RUNNER_BOT_TOKEN"]
SIGNING_SECRET = os.environ["TEST_RUNNER_SIGNING_SECRET"]
APP_TOKEN      = os.environ.get("TEST_RUNNER_APP_TOKEN", "")
ALLOWED_USER   = os.environ.get("TEST_RUNNER_SLACK_USER_ID", "")
UI_BASE_URL    = os.environ.get("UI_BASE_URL", "https://apply.cdlav.us")

app = App(token=BOT_TOKEN, signing_secret=SIGNING_SECRET)

# ---------------------------------------------------------------------------
# Test suites
# ---------------------------------------------------------------------------

_SUITES = {
    "unit": {
        "label": "Unit tests (session / storage / webhooks / apply utils)",
        "paths": [
            "tests/test_session.py",
            "tests/test_storage.py",
            "tests/test_webhooks.py",
            "tests/test_apply_utils.py",
        ],
    },
    "api": {
        "label": "API / integration tests",
        "paths": [
            "tests/test_auth.py",
            "tests/test_profile.py",
            "tests/test_health.py",
            "tests/test_runs.py",
            "tests/test_admin.py",
            "tests/test_security_headers.py",
            "tests/test_rate_limiting.py",
        ],
    },
    "slack": {
        "label": "Slack bot tests",
        "paths": ["tests/slack/"],
    },
    "all": {
        "label": "Full backend suite (unit + API + Slack)",
        "paths": ["tests/", "--ignore=tests/ui"],
    },
    "ui-anon": {
        "label": "UI tests — anonymous (no credentials required)",
        "paths": ["tests/ui/test_login.py", "tests/ui/test_register.py"],
        "browser": True,
    },
    "ui": {
        "label": "UI tests — authenticated",
        "paths": ["tests/ui/"],
        "browser": True,
        "needs_creds": True,
    },
    "ui-admin": {
        "label": "UI tests — admin",
        "paths": ["tests/ui/test_admin.py"],
        "browser": True,
        "needs_creds": True,
    },
}

_ALIASES = {
    "":            "all",
    "full":        "all",
    "everything":  "all",
    "u":           "unit",
    "units":       "unit",
    "a":           "api",
    "apis":        "api",
    "s":           "slack",
    "web":         "ui",
    "anon":        "ui-anon",
    "admin":       "ui-admin",
}

_SUITE_KEYS_DISPLAY = " | ".join(f"`{k}`" for k in _SUITES)

# ---------------------------------------------------------------------------
# Active-run guard
# ---------------------------------------------------------------------------

_active: dict | None = None
_active_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

def _run_pytest(paths: list[str], extra_args: list[str] | None = None,
                env_overrides: dict | None = None) -> dict:
    cmd = [
        sys.executable, "-m", "pytest",
        "--no-cov", "--tb=short", "-q",
        *paths,
        *(extra_args or []),
    ]
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)

    import time
    t0 = time.time()
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            cwd="/app", timeout=300, env=env,
        )
        duration = time.time() - t0
        output = (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired:
        return {"passed": 0, "failed": 0, "errors": 1, "output": "Timed out after 5 minutes.",
                "duration": 300, "returncode": 1}

    passed = failed = errors = 0
    for line in output.splitlines()[::-1]:
        if m := re.search(r"(\d+) passed", line):
            passed = int(m.group(1))
        if m := re.search(r"(\d+) failed", line):
            failed = int(m.group(1))
        if m := re.search(r"(\d+) error", line):
            errors = int(m.group(1))
        if any(w in line for w in ("passed", "failed", "error")):
            break

    return {
        "passed": passed, "failed": failed, "errors": errors,
        "output": output, "duration": duration,
        "returncode": result.returncode,
    }


def _format_results(suite_label: str, r: dict) -> list[dict]:
    total  = r["passed"] + r["failed"] + r["errors"]
    ok     = r["returncode"] == 0 and r["failed"] == 0 and r["errors"] == 0
    icon   = ":white_check_mark:" if ok else ":x:"
    status = "All tests passed" if ok else f"{r['failed']} failed, {r['errors']} errors"
    header = (
        f"{icon}  *{suite_label}* — {status}"
        f"  ·  {r['passed']}/{total} passed"
        f"  ·  _{r['duration']:.1f}s_"
    )

    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
    ]

    if not ok:
        lines = r["output"].splitlines()
        failure_lines: list[str] = []
        capturing = False
        for line in lines:
            if line.startswith(("FAILED ", "ERROR ")):
                capturing = True
            if capturing:
                failure_lines.append(line)
            if capturing and line == "":
                capturing = False

        snippet = "\n".join(failure_lines[:40])
        if len(snippet) > 2800:
            snippet = snippet[:2800] + "\n…(truncated)"
        if snippet:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"```{snippet}```"},
            })

    return blocks


# ---------------------------------------------------------------------------
# /run-tests
# ---------------------------------------------------------------------------

@app.command("/run-tests")
def run_tests(ack, body, respond, client):
    global _active
    ack()

    # Access control
    caller = body.get("user_id", "")
    if not ALLOWED_USER:
        respond(":lock: `TEST_RUNNER_SLACK_USER_ID` is not set — commands disabled.")
        return
    if caller != ALLOWED_USER:
        respond(":no_entry: You are not authorised to run tests.")
        return

    # Resolve suite
    raw = body.get("text", "").strip().lower()
    key = _ALIASES.get(raw, raw) or "all"
    suite = _SUITES.get(key)
    if not suite:
        respond(f":x: Unknown suite `{raw}`. Available: {_SUITE_KEYS_DISPLAY}")
        return

    # Concurrency guard
    with _active_lock:
        if _active:
            respond(f":hourglass: Already running `{_active['key']}`. Try again shortly.")
            return
        _active = {"key": key}

    # Credentials check for suites that need them
    if suite.get("needs_creds") and not os.environ.get("UI_TEST_PASSWORD"):
        with _active_lock:
            _active = None
        respond(
            ":lock: UI test credentials not configured.\n"
            "Set `UI_TEST_PASSWORD` and `UI_ADMIN_PASSWORD` as Fly secrets."
        )
        return

    # Post placeholder message
    channel = body.get("channel_id")
    msg = client.chat_postMessage(
        channel=channel,
        text=f":test_tube: Running *{suite['label']}*…",
        blocks=[{"type": "section", "text": {"type": "mrkdwn",
            "text": f":test_tube: Running *{suite['label']}*…  _(~10–60s)_"}}],
    )
    ts = msg["ts"]

    def _worker():
        global _active
        try:
            extra_args: list[str] = []
            env_overrides: dict[str, str] = {}

            if suite.get("browser"):
                extra_args += [
                    "--base-url", UI_BASE_URL,
                    "--browser", "chromium",
                ]
                for var in ("UI_BASE_URL", "UI_TEST_EMAIL", "UI_TEST_PASSWORD",
                            "UI_ADMIN_EMAIL", "UI_ADMIN_PASSWORD"):
                    val = os.environ.get(var, "")
                    if val:
                        env_overrides[var] = val
                env_overrides.setdefault("UI_BASE_URL", UI_BASE_URL)

            result = _run_pytest(suite["paths"], extra_args=extra_args,
                                 env_overrides=env_overrides or None)
            blocks = _format_results(suite["label"], result)
        except Exception as exc:
            blocks = [{"type": "section", "text": {"type": "mrkdwn",
                "text": f":x: Test runner crashed: `{exc}`"}}]
        finally:
            with _active_lock:
                _active = None

        try:
            client.chat_update(channel=channel, ts=ts,
                               blocks=blocks, text=blocks[0]["text"]["text"])
        except Exception as exc:
            client.chat_postMessage(channel=channel,
                                    text=f":x: Could not update result: {exc}")

    threading.Thread(target=_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# /test-status
# ---------------------------------------------------------------------------

@app.command("/test-status")
def test_status(ack, body, respond):
    ack()

    caller = body.get("user_id", "")
    if ALLOWED_USER and caller != ALLOWED_USER:
        respond(":no_entry: You are not authorised.")
        return

    with _active_lock:
        active = _active

    if active:
        respond(f":hourglass_flowing_sand: Test run in progress: `{active['key']}`")
    else:
        respond(":white_check_mark: No test run active.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not APP_TOKEN:
        raise RuntimeError("TEST_RUNNER_APP_TOKEN is required for Socket Mode")
    print("Test runner bot starting (Socket Mode)…")
    SocketModeHandler(app, APP_TOKEN).start()
