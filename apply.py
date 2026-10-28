#!/usr/bin/env python3
"""
apply.py — Job Application Agent for Corey Laverdiere

Public API (for a UI layer):
    from apply import run_workflow, WorkflowConfig, WorkflowResult, WorkflowError

    result = run_workflow(
        job_posting="...",
        company="Acme",
        role="Solutions Engineer",
        config=WorkflowConfig(progress=my_callback),
    )

CLI:
    python apply.py --job jobs/job.txt --company "Acme" --role "Solutions Engineer"
    python apply.py --job jobs/job.txt --company "Acme" --role "SE" --dry-run
"""

import argparse
import base64
import html
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

# Load .env before anything else so LOGODEV_API_KEY / ANTHROPIC_API_KEY are set
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

from scripts.brand_color import get_brand_color, get_brand_logo
from scripts.gen_ats_from_styled import parse_xml as _parse_styled_resume, build_ats as _build_ats_resume
from scripts.ssrf import is_ssrf_url

try:
    import anthropic
except ImportError as _e:
    raise ImportError("anthropic package not installed. Run: pip install anthropic") from _e

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError as _e:
    raise ImportError("Pillow not installed. Run: pip install Pillow") from _e

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MASTER_RESUME = Path("resumes/master.docx")
PROFILE_FILE  = Path("profile.md")
UNPACK_DIR    = Path("unpacked")
OUTPUT_DIR    = Path("output")
SCRIPTS_DIR   = Path("scripts/office")

DEFAULT_MODEL = "claude-sonnet-5"

ROUND_TYPES = (
    "Phone Screen",
    "Hiring Manager",
    "Peer",
    "Technical",
    "Executive",
    "Panel",
)

# ---------------------------------------------------------------------------
# WorkflowError / WorkflowConfig / WorkflowResult
# ---------------------------------------------------------------------------

class WorkflowError(Exception):
    """Raised when the workflow cannot continue due to an unrecoverable error."""


@dataclass
class WorkflowConfig:
    """Runtime settings for a single workflow run."""
    model:         str                    = DEFAULT_MODEL
    progress:      Callable[[str], None]  = field(default=print)
    debug:         bool                   = False
    dry_run:       bool                   = False
    # Per-user overrides — set by the server for multi-user deployments.
    # CLI single-user runs leave these as None and fall back to module constants.
    master_resume: Path | None            = None
    profile_text:  str | None             = None
    # User identity — used to scope output dirs and Drive folders.
    # CLI runs leave these None (outputs go to output/ directly).
    user_id:       str | None             = None   # UUID, used for local path
    user_label:    str | None             = None   # email, used for Drive folder name


@dataclass
class WorkflowResult:
    """Paths and metadata produced by a completed workflow run."""
    run_dir:              Path
    resume_path:          Path
    ats_path:             Path
    cover_letter_path:    Path
    framing_angle:        str
    folder_url:           str | None = None
    replacements_warning: str | None = None


@dataclass
class InterviewPrepConfig:
    """Settings for a single interview-prep run."""
    round_type:    str
    focus:         str
    model:         str                    = DEFAULT_MODEL
    progress:      Callable[[str], None]  = field(default=print)
    profile_text:  str | None             = None
    master_resume: Path | None            = None
    user_id:       str | None             = None
    user_label:    str | None             = None
    interviewer:   str                    = ""
    interview_date: str                   = ""
    interview_time: str                   = ""
    location:       str                   = ""
    domain:         str                   = ""


@dataclass
class InterviewPrepResult:
    """Paths produced by a completed interview-prep run."""
    prep_path:  Path
    run_dir:    Path
    folder_url: str | None = None


@dataclass
class AppQuestionConfig:
    """Settings for answering a job application question."""
    question:       str
    job_posting:    str
    company:        str
    role:           str
    tone:           str                    = "professional"
    char_limit:     int | None             = None
    clarifications: dict | None            = None
    model:          str                    = DEFAULT_MODEL
    progress:       Callable[[str], None]  = field(default=print)
    profile_text:   str | None             = None
    master_resume:  Path | None            = None
    user_id:        str | None             = None
    user_label:     str | None             = None


@dataclass
class AppQuestionResult:
    """Result from answering an application question."""
    answer:                  str
    char_count:              int
    follow_ups:              list[str]
    needs_clarification:     bool       = False
    clarification_questions: list[str]  = field(default_factory=list)


@dataclass
class ThankYouConfig:
    """Settings for generating an interview thank-you email."""
    job_posting:    str
    company:        str
    role:           str
    round_type:     str
    interviewer:    str                    = ""
    topics:         str                    = ""
    tone:           str                    = "professional"
    model:          str                    = DEFAULT_MODEL
    progress:       Callable[[str], None]  = field(default=print)
    profile_text:   str | None             = None
    master_resume:  Path | None            = None
    user_id:        str | None             = None
    user_label:     str | None             = None


@dataclass
class ThankYouResult:
    """Result from generating a thank-you email."""
    email_text:   str
    subject:      str
    run_dir:      Path
    docx_path:    Path
    folder_url:   str | None = None


@dataclass
class OptimizeConfig:
    """Settings for optimizing an existing run's documents in place."""
    folder_id:             str                    # Drive run folder to optimize
    instruction:           str                    # user's free-text optimization prompt
    company:               str
    role:                  str
    optimize_resume:       bool                   = True
    optimize_cover_letter: bool                   = True
    model:                 str                    = DEFAULT_MODEL
    progress:              Callable[[str], None]  = field(default=print)
    user_id:               str | None             = None
    user_label:            str | None             = None
    domain:                str                    = ""


@dataclass
class OptimizeResult:
    """Paths and metadata produced by a completed optimize run."""
    run_dir:              Path
    folder_url:           str | None = None
    resume_path:          Path | None = None
    ats_path:             Path | None = None
    cover_letter_path:    Path | None = None
    change_summary:       str = ""
    replacements_warning: str | None = None

# ---------------------------------------------------------------------------
# Anthropic client — lazy init so import never fails on missing API key
# ---------------------------------------------------------------------------

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def claude(system: str, user: str, max_tokens: int = 4096,
           config: WorkflowConfig | None = None) -> str:
    """Single-turn Claude call. Returns the text response.

    Uses the streaming endpoint even though callers get back a plain string —
    the non-streaming endpoint refuses any call whose max_tokens implies it
    could run past 10 minutes (SDK-enforced), which large-budget calls like
    match scoring can trip even when the actual response is fast.

    Extended thinking is explicitly disabled: every caller here wants a
    single structured (usually JSON) text response, no reasoning trace, and
    Claude 5-family models otherwise emit a thinking block by default that
    silently eats most of max_tokens before any output text is written —
    a 41-field resume-edit prompt measured at 15k+ output tokens of
    thinking alone (vs. ~1.8k for the actual JSON) and blew through a
    16000-token budget with none of it visible to the caller."""
    model = config.model if config else DEFAULT_MODEL
    with _get_client().messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=system,
        thinking={"type": "disabled"},
        messages=[{"role": "user", "content": user}],
    ) as stream:
        response = stream.get_final_message()
    if response.stop_reason == "max_tokens":
        # The response (JSON or otherwise) is truncated mid-output — never
        # usable as-is, and a truncated JSON payload otherwise surfaces
        # downstream as an opaque json.JSONDecodeError ("Unterminated
        # string...") with no hint that the real cause was hitting the
        # token budget. Fail loudly here instead so it's fixable on sight.
        raise WorkflowError(
            f"Claude response was cut off at the max_tokens={max_tokens} limit "
            "before finishing — increase max_tokens for this call and retry."
        )
    for block in response.content:
        if block.type == "text":
            return block.text
    raise WorkflowError(
        "Claude response contained no text block "
        f"(stop_reason={response.stop_reason}, "
        f"block_types={[b.type for b in response.content]}, "
        "likely extended thinking consumed the full max_tokens budget — increase max_tokens)"
    )


def _repair_unescaped_quotes(text: str) -> str:
    """Escape stray literal quote marks and raw control characters that
    Claude sometimes leaves inside JSON string values — e.g. quoting a
    phrase from the job posting verbatim ("Governance-by-Design") without
    escaping the inner quotes. json.loads doesn't report that at the quote
    itself; it fails later with a misleading "Expecting ',' delimiter" once
    the unterminated string collides with the next token.

    Heuristic: while inside a string, a `"` only closes the string if the
    next non-whitespace character is a structural one (`,`, `}`, `]`, `:`,
    or end of text); otherwise it's an embedded quote and gets escaped.
    """
    out = []
    in_str = False
    esc = False
    n = len(text)
    i = 0
    while i < n:
        c = text[i]
        if in_str:
            if esc:
                out.append(c)
                esc = False
            elif c == '\\':
                out.append(c)
                esc = True
            elif c == '"':
                j = i + 1
                while j < n and text[j] in ' \t\r\n':
                    j += 1
                if j >= n or text[j] in ',}]:':
                    in_str = False
                    out.append(c)
                else:
                    out.append('\\"')
            elif c in '\n\r\t':
                out.append({'\n': '\\n', '\r': '\\r', '\t': '\\t'}[c])
            else:
                out.append(c)
        else:
            if c == '"':
                in_str = True
            out.append(c)
        i += 1
    return ''.join(out)


def _parse_self_correcting_json(
    raw: str, required_key: str
) -> tuple[dict | None, json.JSONDecodeError | ValueError | None]:
    """Extract the last balanced top-level {...} object in `raw` that parses
    and carries `required_key`.

    Claude occasionally second-guesses itself mid-response on a large
    schema — emits a complete-but-partial JSON object, some visible "wait,
    let me redo that" text, then a second, fuller object. A plain
    json.loads() chokes on that trailing text. Scanning for every balanced
    top-level object and taking the last usable one recovers the
    self-corrected final answer. Each candidate also gets one retry through
    _repair_unescaped_quotes before being given up on.
    """
    candidates = []
    depth, in_str, esc, start = 0, False, False, -1
    for i, c in enumerate(raw):
        if esc:
            esc = False
        elif c == '\\' and in_str:
            esc = True
        elif c == '"':
            in_str = not in_str
        elif not in_str:
            if c == '{':
                if depth == 0:
                    start = i
                depth += 1
            elif c == '}':
                depth = max(depth - 1, 0)
                if depth == 0 and start != -1:
                    candidates.append(raw[start:i + 1])
                    start = -1

    last_err: json.JSONDecodeError | ValueError | None = None
    for candidate in reversed(candidates):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as e:
            try:
                parsed = json.loads(_repair_unescaped_quotes(candidate))
            except json.JSONDecodeError:
                last_err = e
                continue
        if required_key in parsed:
            return parsed, None
        last_err = ValueError("parsed JSON object missing required keys")
    return None, last_err

# ---------------------------------------------------------------------------
# Tagline width validation
# ---------------------------------------------------------------------------

# Calibri regular from the Word app bundle — same font Word uses to render the resume
_CALIBRI_PATH = (
    "/Applications/Microsoft Word.app/Contents/Resources/DFonts/Calibri.ttf"
)
_MEASURE_PT = 110
_MASTER_TAGLINE = (
    "Delivering AI-Powered Integrations, Workflow Automations "
    "& Agentic Solutions Across the Full Enterprise Stack"
)
_MAX_TAGLINE_PX: float | None = None


def _measure_width(text: str) -> float:
    """Return rendered pixel width of text at Calibri _MEASURE_PT."""
    font = ImageFont.truetype(_CALIBRI_PATH, size=_MEASURE_PT)
    img  = Image.new("RGB", (1, 1))
    draw = ImageDraw.Draw(img)
    bbox = draw.textbbox((0, 0), text, font=font)
    return float(bbox[2] - bbox[0])


def tagline_fits(text: str) -> bool:
    """Return True if text fits on one printed line at Calibri 11pt.
    Returns True without checking when Calibri is unavailable (no Word install)."""
    global _MAX_TAGLINE_PX
    try:
        if _MAX_TAGLINE_PX is None:
            _MAX_TAGLINE_PX = _measure_width(_MASTER_TAGLINE)
        return _measure_width(text) <= _MAX_TAGLINE_PX
    except OSError:
        return True  # Calibri font not found — skip validation

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(
    cmd: str | list,
    check: bool = True,
    config: WorkflowConfig | None = None,
) -> subprocess.CompletedProcess:
    """Run a command. Raises WorkflowError on failure when check=True.
    Pass a list for safe argument handling; strings run through shell=True."""
    shell = isinstance(cmd, str)
    result = subprocess.run(cmd, shell=shell, capture_output=True, text=True)
    if check and result.returncode != 0:
        cmd_display = cmd if shell else " ".join(shlex.quote(str(c)) for c in cmd)
        # Build a detailed error that includes all output so the cause is always visible
        detail_parts = [f"Command failed: {cmd_display}"]
        if result.stdout.strip():
            detail_parts.append(result.stdout.strip())
        if result.stderr.strip():
            detail_parts.append(result.stderr.strip())
        detail = "\n\n".join(detail_parts)
        progress = config.progress if config else print
        progress(detail)
        raise WorkflowError(detail)
    return result


def read_file(path: Path) -> str:
    return path.read_text(encoding="utf-8")

def write_file(path: Path, content: str):
    path.write_text(content, encoding="utf-8")

def safe_filename(s: str) -> str:
    """Strip characters that are not safe for use in filenames."""
    return re.sub(r"[^A-Za-z0-9_-]", "", s)

def title_case_name(name: str) -> str:
    """Normalize a name to Title Case for use in filenames.

    Master resumes style the name header however the user likes (e.g. ALL
    CAPS) — that's fine for on-page display, but copied verbatim into a
    filename it reads as "COREYLAVERDIERE" instead of "CoreyLaverdiere".
    Capitalizes only the first letter of each letter-run, so hyphens and
    apostrophes stay as word boundaries (e.g. "O'BRIEN" -> "O'Brien").
    """
    return re.sub(r"[A-Za-z]+", lambda m: m.group(0)[:1].upper() + m.group(0)[1:].lower(), name)

def print_step(n: str | int, title: str, config: WorkflowConfig | None = None):
    progress = config.progress if config else print
    progress(f"\n{'='*60}")
    progress(f"  STEP {n}: {title}")
    progress(f"{'='*60}")


def extract_resume_text(config: WorkflowConfig | None = None) -> str:
    """Extract plain text from the master resume using pandoc."""
    resume = (config.master_resume if config and config.master_resume else MASTER_RESUME)
    result = run(["pandoc", str(resume), "-t", "plain"], config=config)
    return result.stdout


def read_document_xml() -> str:
    return (UNPACK_DIR / "word" / "document.xml").read_text(encoding="utf-8")

def write_document_xml(content: str):
    (UNPACK_DIR / "word" / "document.xml").write_text(content, encoding="utf-8")

# ---------------------------------------------------------------------------
# Step 1: Read inputs
# ---------------------------------------------------------------------------

def step1_read_inputs(
    job_posting: str,
    config: WorkflowConfig,
) -> tuple[str, str, str]:
    """Validate prerequisites, read master resume and profile.
    Returns (job_posting, resume_text, profile)."""
    print_step(1, "Reading Inputs", config)

    resume = config.master_resume if config.master_resume else MASTER_RESUME
    if not resume.exists():
        raise WorkflowError(f"Master resume not found at {resume}")
    if config.profile_text is None and not PROFILE_FILE.exists():
        raise WorkflowError(f"Profile not found at {PROFILE_FILE} and no profile_text provided")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise WorkflowError("ANTHROPIC_API_KEY environment variable not set")

    resume_text = extract_resume_text(config)
    profile     = config.profile_text if config.profile_text is not None else read_file(PROFILE_FILE)

    config.progress(f"  ✓ Job posting ({len(job_posting)} chars)")
    config.progress(f"  ✓ Master resume ({len(resume_text)} chars)")
    config.progress(f"  ✓ Profile ({len(profile)} chars)")

    return job_posting, resume_text, profile

# ---------------------------------------------------------------------------
# Step 2: Analysis
# ---------------------------------------------------------------------------

ANALYSIS_SYSTEM = """\
You are a professional resume strategist specializing in tailoring resumes and
cover letters for technical roles (Integration Engineer, Solutions Engineer,
Professional Services, Technical Account Manager, AI/ML roles, etc.).

You will be given:
1. A job posting
2. The candidate's master resume (extracted text)
3. A profile guide describing the candidate's voice, stories, and preferences

Your job is to produce a structured analysis that will drive all downstream
resume and cover letter edits. Be specific and opinionated. Do not hedge.

CRITICAL TAGLINE CONSTRAINT: The resume tagline must fit on a single printed line.
Keep it under 100 characters. Do not use long compound phrases or stack multiple
clauses — one punchy sentence that captures the framing angle is enough.

CRITICAL COVER LETTER CONSTRAINT: The cover letter must fit on a single page.
Write each paragraph (P1-P5) to be 2-4 sentences maximum. Total word count
across all five paragraphs must not exceed 300 words. Be concise and punchy -
every sentence must earn its place. Cut anything that restates what the previous
sentence already said.

VOICE: Write with a stronger point of view. Let personality, opinions, and natural
imperfections come through. Make it sound like one real person — not a generic writer,
not a polished AI summary. Vary sentence length, let some sentences be blunt and short.
If a phrase sounds like it could appear in any candidate's resume, rewrite it until it
couldn't.

Emit the JSON object exactly once — do not think out loud, self-correct, or restart
mid-response; if you notice a mistake, just get the object right the first time.

If a string value needs to quote a phrase (from the job posting or elsewhere),
escape the inner quote marks as \\" or paraphrase instead of quoting — a literal
unescaped quote inside a JSON string breaks the parser.
"""


def step2_analyze(
    job_posting: str,
    resume_text: str,
    profile: str,
    company: str,
    role: str,
    contact: str | None,
    field_map: dict[str, str],
    jobs_legend: str,
    config: WorkflowConfig,
) -> dict:
    """Run the analysis pass. Returns a structured dict driving all downstream edits.

    field_map / jobs_legend come from parsing *this user's own* master resume
    (see _build_resume_field_map) — the model tailors whatever fields that
    resume actually has (however many jobs, however many bullets or
    competency cells each one carries), never a fixed set of employer names
    or a fixed competency-grid shape.
    """
    print_step(2, "Analysis", config)

    editable_fields = {k: v for k, v in field_map.items() if k != "tagline"}

    prompt = f"""
Job Posting:
---
{job_posting}
---

Master Resume (full text, for context):
---
{resume_text}
---

Profile Guide:
---
{profile}
---

Company: {company}
Role: {role}

Jobs on this resume (read-only context for the field ids below):
{jobs_legend}

Editable resume fields (field id -> current text). Tailor these to the job
posting above — only include a field in resume_edits if you're actually
changing it; leave everything else out:
{json.dumps(editable_fields, indent=2)}

Tailoring guidance:
- Prioritize the most recent and most relevant jobs — rewrite most or all of
  their bullets to surface the work that matches this job's requirements.
- For jobs roughly 10+ years old, or clearly less relevant to this role,
  make minimal changes or none at all.
- summary_N fields are the Professional Summary, one field per existing
  bullet or paragraph. Rewrite them in place in the candidate's own voice
  (per the profile guide), keeping each one's current form and roughly its
  length: a bullet stays a single bullet-style line, a paragraph stays a
  paragraph. Never add, merge, or convert items. If there are no summary_N
  fields, the resume has no summary — do not invent one.
- Rewrite each competency cell (competency_N) to match the JD's own language
  and priorities, keeping roughly the same length as the current text.
- Only rewrite a jobN_title if the JD suggests a different subtitle/title
  framing is more accurate to how this role should be presented.
- Never invent employers, dates, numbers, tools, or accomplishments that
  aren't already present somewhere in the master resume.

Produce a JSON object with exactly these keys:
{{
  "role_type": "string - one of: PS/Delivery, Solutions Engineer, TAM, Integration Engineer, Agent Platform, AI Solutions, Customer Success, Forward Deployed Engineer, Other",
  "framing_angle": "string - 1-2 sentences describing the single narrative thread to run through the entire resume and cover letter",
  "tagline": "string - new tagline for the resume header (1 sentence, punchy, matches framing angle, MUST be under 100 characters)",
  "top_jd_requirements": ["string", "string", "string", "string", "string"],
  "resume_edits": [
    {{"field": "<field id from the editable fields map above>", "new": "<replacement text>"}}
  ],
  "cover_letter_hook": "string - the opening angle for the cover letter P1 (what JD language to echo, what story to lead with)",
  "cover_letter_p1": "string - full text of P1 (max 3 sentences)",
  "cover_letter_p2": "string - full text of P2, primary evidence, most quantified (max 4 sentences)",
  "cover_letter_p3": "string - full text of P3, secondary evidence (max 3 sentences)",
  "cover_letter_p4": "string - full text of P4, differentiator specific to this role/company (max 3 sentences)",
  "cover_letter_p5": "string - full text of P5, short close (1-2 sentences only)",
  "contact_name": "string - hiring manager name if determinable from the posting, otherwise 'Hiring Team'"
}}

Return ONLY valid JSON. No preamble, no markdown fences, no commentary.
"""
    # Retry the call once if nothing usable turns up — see
    # _parse_self_correcting_json for why a plain json.loads() isn't enough.
    last_err: json.JSONDecodeError | ValueError | None = None
    data = None
    raw = ""
    for attempt in range(2):
        raw = claude(ANALYSIS_SYSTEM, prompt, max_tokens=24000, config=config)
        raw = re.sub(r"^```json\s*", "", raw.strip())
        raw = re.sub(r"\s*```$", "", raw.strip())

        data, last_err = _parse_self_correcting_json(raw, "resume_edits")
        if data is not None:
            break

    if data is None:
        raise WorkflowError(f"Failed to parse analysis JSON: {last_err}\n\nRaw response:\n{raw[:2000]}")

    # Caller-supplied contact overrides anything the model inferred
    if contact:
        data["contact_name"] = contact

    # Validate tagline width — retry up to 2 times if it overflows one line
    for attempt in range(2):
        tagline = data.get("tagline", "")
        if tagline_fits(tagline):
            break
        try:
            ratio = _measure_width(tagline) / _MAX_TAGLINE_PX if _MAX_TAGLINE_PX else 1.0
        except OSError:
            ratio = 1.0
        config.progress(f"\n  ⚠  Tagline too wide ({len(tagline)} chars, {ratio:.0%} of max line):")
        config.progress(f"     {tagline}")
        config.progress(f"     Requesting shorter version (attempt {attempt + 1}/2)...")
        shortened = claude(
            "You are a resume copywriter. Shorten the given tagline so it fits on one "
            "printed line of a resume. Keep the core meaning and active voice. "
            "Return only the shortened tagline — no quotes, no explanation.",
            f'Tagline to shorten: {tagline}\n\nConstraint: must be under 100 characters.',
            max_tokens=120,
            config=config,
        )
        data["tagline"] = shortened.strip().strip('"').strip("'")
    else:
        tagline = data.get("tagline", "")
        if not tagline_fits(tagline):
            config.progress(f"  ⚠  Tagline still too wide after 2 retries — proceeding anyway.")
            config.progress(f"     {tagline}")

    config.progress(f"\n  Role type:      {data.get('role_type')}")
    config.progress(f"  Framing angle:  {data.get('framing_angle')}")
    config.progress(f"  Tagline:        {data.get('tagline')}")
    config.progress(f"\n  Top JD requirements:")
    for i, req in enumerate(data.get("top_jd_requirements", []), 1):
        config.progress(f"    {i}. {req}")
    config.progress(f"\n  Resume field edits: {len(data.get('resume_edits', []))}")

    return data

# ---------------------------------------------------------------------------
# Step 2b: Brand colors
# ---------------------------------------------------------------------------

def step2b_brand_colors(company: str, config: WorkflowConfig, domain: str = "") -> dict:
    print_step("2b", "Fetching Brand Colors", config)
    return get_brand_color(company, domain=domain or None)

# ---------------------------------------------------------------------------
# Steps 3–5: Resume build
# ---------------------------------------------------------------------------

def step3_unpack(config: WorkflowConfig):
    print_step(3, "Unpacking Master Resume", config)
    resume = config.master_resume if config.master_resume else MASTER_RESUME
    if UNPACK_DIR.exists():
        shutil.rmtree(UNPACK_DIR)
    run(
        ["python3", str(SCRIPTS_DIR / "unpack.py"), str(resume), str(UNPACK_DIR) + "/"],
        config=config,
    )
    config.progress("  ✓ Unpacked")


def _xml_attr_escape(text: str) -> str:
    """Escape text for safe insertion inside an XML attribute value (handles quotes too)."""
    return html.escape(str(text), quote=True)


def apply_brand_colors(xml: str, colors: dict) -> str:
    """Replace the three hardcoded palette hex values with the brand colors."""
    primary = _xml_attr_escape(colors["primary"])
    border = _xml_attr_escape(colors["border"])
    fill = _xml_attr_escape(colors["fill"])
    xml = xml.replace('w:val="1A3C5E"',  f'w:val="{primary}"')
    xml = xml.replace('w:color="1A3C5E"', f'w:color="{primary}"')
    xml = xml.replace('w:color="2B6CB0"', f'w:color="{border}"')
    xml = xml.replace('w:fill="EEF4FB"',  f'w:fill="{fill}"')
    return xml


def _xml_escape(text: str) -> str:
    """Escape text for safe insertion as XML character data.
    Resolves any pre-escaped entities first to avoid double-encoding, then
    re-escapes cleanly — so callers can write & or &amp; and both work."""
    for entity, char in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                         ("&apos;", "'"), ("&quot;", '"')]:
        text = text.replace(entity, char)
    return html.escape(text, quote=False)


def _extract_xml_field(xml: str, prefix: str) -> str | None:
    """Return the exact substring of *xml* that should be used as the `old`
    argument to `str.replace()` for this field.

    Normally that is just the text content of the matching `<w:t>` element.
    When Word has split a single logical run across a `<w:lastRenderedPageBreak/>`
    element the returned string spans both `<w:t>` contents plus the break tag,
    so that one `str.replace()` call collapses the split back to a single run.

    `prefix` must be the first few characters of the field as they appear
    *inside the XML* (entity-escaped: & → &amp;, etc.).  Returns None if not
    found.

    Tolerates (but does not capture) a literal "•" bullet prefix in the XML
    — this app's own JS-based DOCX generators write bullets as literal text
    rather than real list numbering (see clean_bullet_text in
    scripts/gen_ats_from_styled.py, which strips it when reading field
    values), so `prefix` itself never includes it. Leaving it out of the
    captured/replaced group means a plain str.replace() naturally leaves
    that marker in place in the output.
    """
    escaped = re.escape(prefix)
    pat = r'<w:t(?:\s[^>]*)?>(?:•\s*)?(' + escaped + r'(?:(?!</w:t>).)*)</w:t>'
    m = re.search(pat, xml, re.S)
    if not m:
        return None
    first_text = m.group(1)

    # Check whether a <w:lastRenderedPageBreak/> immediately follows and
    # introduces a continuation <w:t>.  If so, include the break + second run
    # so the caller's replace() collapses both into one clean <w:t>.
    after = xml[m.end():]
    pb = re.match(
        r'^(\s*<w:lastRenderedPageBreak/>)(\s*<w:t[^>]*>)((?:(?!</w:t>).)*)',
        after, re.S,
    )
    if pb:
        return first_text + '</w:t>' + pb.group(1) + pb.group(2) + pb.group(3)
    return first_text


def step4_apply_edits(
    analysis: dict,
    field_map: dict[str, str],
    colors: dict | None,
    config: WorkflowConfig,
) -> tuple[int, int]:
    """Apply all content edits and brand colors to the unpacked XML.

    field_map (from _build_resume_field_map, parsed from this user's own
    master resume) drives which fields exist and what their current text is
    — the same generic, structure-agnostic mechanism optimize_run() uses, so
    this works for any resume layout, not just a fixed set of employer names
    and a fixed competency-grid shape. The `old` search string is always
    derived from the field's *current* text (_apply_optimize_edits), never a
    hardcoded literal.

    Returns (succeeded, attempted).
    """
    print_step(4, "Applying Resume Edits", config)

    xml = read_document_xml()

    # Tagline + competency/job-bullet edits: uniform field-based replace.
    edits = list(analysis.get('resume_edits', []))
    if analysis.get('tagline'):
        edits.append({'field': 'tagline', 'new': analysis['tagline']})
    xml, total_success, total_attempted, total_unchanged = _apply_optimize_edits(
        xml, edits, field_map, config.progress
    )

    if colors:
        xml = apply_brand_colors(xml, colors)
        config.progress(f"  ✓ Brand colors applied (primary=#{colors['primary']})")

    write_document_xml(xml)

    # Fields Claude deliberately left unchanged (already fit the JD) aren't
    # failures — exclude them from the ratio so they can't drag a fully
    # successful run below the warning threshold.
    real_attempted = total_attempted - total_unchanged
    unchanged_note = f" ({total_unchanged} already matched — left unchanged)" if total_unchanged else ""
    config.progress(f"\n  Result: {total_success}/{real_attempted} replacements succeeded{unchanged_note}")

    if real_attempted > 0 and total_success < real_attempted * 0.7:
        config.progress(f"\n⚠️  Warning: fewer than 70% of replacements succeeded.")
        config.progress(f"   Check the XML manually or re-run with --debug flag.")

    return total_success, real_attempted


def step5_pack(resume_out: Path, config: WorkflowConfig):
    print_step(5, "Packing Resume", config)
    resume = config.master_resume if config.master_resume else MASTER_RESUME
    run(
        ["python3", str(SCRIPTS_DIR / "pack.py"), str(UNPACK_DIR) + "/",
         str(resume_out), "--original", str(resume)],
        config=config,
    )
    config.progress(f"  ✓ Resume written to {resume_out}")


def step7_cleanup(config: WorkflowConfig):
    print_step(7, "Cleanup", config)
    if not config.debug and UNPACK_DIR.exists():
        shutil.rmtree(UNPACK_DIR)

# ---------------------------------------------------------------------------
# JS string escaping (shared by ATS resume and cover letter builders)
# ---------------------------------------------------------------------------

def escape_js_string(s: str) -> str:
    """Escape a string for embedding in a JS double-quoted string.

    Claude's JSON responses can legitimately contain embedded newlines
    (valid inside a JSON string, decoded to real \\n/\\r by json.loads) —
    left unescaped here they'd terminate the JS string literal early and
    crash the generated Node script, so they're escaped like any other
    special character rather than relying on callers to strip them first.
    """
    s = s.replace("\\", "\\\\")
    s = s.replace("`", "\\`")
    s = s.replace("${", "\\${")
    s = s.replace('"', '\\"')
    s = s.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")
    return s

# ---------------------------------------------------------------------------
# Step 5b: ATS Resume
# ---------------------------------------------------------------------------

def step5b_ats_resume(
    styled_resume_path: Path,
    output_path: Path,
    config: WorkflowConfig,
):
    """Generate a clean, ATS-optimized single-column DOCX.

    Parses the just-packed styled resume's XML verbatim (same helper
    optimize_run() uses to keep the two documents in sync after an edit) so
    the ATS resume can never drift from what's actually in the tailored
    resume — no separate LLM-generated copy of the experience section, and
    no separately hand-maintained copy of the Projects section either.
    """
    print_step("5b", "Generating ATS Resume", config)

    data = _parse_styled_resume(styled_resume_path)
    try:
        _build_ats_resume(data, output_path)
    except RuntimeError as exc:
        raise WorkflowError(str(exc))

    config.progress(f"  ✓ ATS resume written to {output_path}")


def _identity_from_resume(resume_path: Path, config: WorkflowConfig) -> tuple[str, str]:
    """Extract (name, contact_line) verbatim from a resume's own header.

    Every generated document that needs to show the applicant's identity
    (cover letter, interview prep) reads it from here — always the actual
    candidate's own resume, never a hardcoded default — so a document never
    ends up signed with a different person's name and contact info.
    """
    try:
        data = _parse_styled_resume(resume_path)
    except Exception as exc:
        raise WorkflowError(f"Could not read the applicant's resume header: {exc}")
    name = data.get("name", "")
    if not name:
        raise WorkflowError(
            "Could not find the candidate's name in the resume header — "
            "refusing to generate a document with a missing/placeholder identity."
        )
    config.progress(f"  ✓ Applicant identity read from resume: {name}")
    return name, data.get("contact_line", "")


def _applicant_name_for_filenames(resume_path: Path | str | None) -> str:
    """Best-effort filename-safe applicant name, read from their own resume.

    Used only for output filenames — never for document content, which goes
    through the strict _identity_from_resume() instead. A parse miss here
    falls back to a generic "Applicant" label (never another user's real
    name) rather than blocking file generation.
    """
    try:
        name = _parse_styled_resume(Path(resume_path) if resume_path else MASTER_RESUME).get("name", "")
        if name:
            return safe_filename(title_case_name(name)) or "Applicant"
    except Exception:
        pass
    return "Applicant"

# ---------------------------------------------------------------------------
# Step 6: Cover letter
# ---------------------------------------------------------------------------

# docx size units are half-points: 22 = 11pt, 24 = 12pt, 40 = 20pt
# twip spacing: 80 = tight gap, 240 = 1.0 line height, 720 = 0.5in margin

COVER_LETTER_JS_TEMPLATE = """\
const {{ Document, Packer, Paragraph, TextRun, BorderStyle }} = require('docx');
const fs = require('fs');

const doc = new Document({{
  styles: {{ default: {{ document: {{ run: {{ font: "Calibri", size: 22 }} }} }} }},
  sections: [{{
    properties: {{
      page: {{
        size: {{ width: 12240, height: 15840 }},
        margin: {{ top: 1080, right: 1080, bottom: 1080, left: 1080 }}
      }}
    }},
    children: [
      new Paragraph({{
        spacing: {{ after: 0 }},
        children: [new TextRun({{ text: "{applicant_name_upper}", font: "Calibri", size: 40, bold: true, color: "{primary_color}" }})]
      }}),
      new Paragraph({{
        border: {{ bottom: {{ style: BorderStyle.SINGLE, size: 6, color: "{border_color}", space: 4 }} }},
        spacing: {{ after: 160 }},
        children: [new TextRun({{
          text: "{contact_line}",
          font: "Calibri", size: 20, color: "6B7280"
        }})]
      }}),
      new Paragraph({{
        spacing: {{ before: 160, after: 60 }},
        children: [new TextRun({{ text: "{today}", font: "Calibri", size: 22, color: "111827" }})]
      }}),
      new Paragraph({{
        spacing: {{ after: 0 }},
        children: [new TextRun({{ text: "{contact_name}", font: "Calibri", size: 22, color: "111827" }})]
      }}),
      new Paragraph({{
        spacing: {{ after: 60 }},
        children: [new TextRun({{ text: "{company}", font: "Calibri", size: 22, color: "111827" }})]
      }}),
      new Paragraph({{
        spacing: {{ before: 60, after: 160 }},
        children: [new TextRun({{ text: "Re: {role}", font: "Calibri", size: 22, bold: true, color: "111827" }})]
      }}),
      new Paragraph({{
        spacing: {{ after: 160 }},
        children: [new TextRun({{ text: "Dear {salutation},", font: "Calibri", size: 22, color: "111827" }})]
      }}),
      {body_paragraphs}
      new Paragraph({{
        spacing: {{ after: 40 }},
        children: [new TextRun({{ text: "Sincerely,", font: "Calibri", size: 22, color: "111827" }})]
      }}),
      new Paragraph({{
        spacing: {{ after: 40 }},
        children: [new TextRun({{ text: "{applicant_name}", font: "Calibri", size: 22, bold: true, color: "{primary_color}" }})]
      }}),
      new Paragraph({{
        spacing: {{ after: 0 }},
        children: [new TextRun({{ text: "{sign_off_contact}", font: "Calibri", size: 22, color: "6B7280" }})]
      }})
    ]
  }}]
}});

Packer.toBuffer(doc).then(buffer => {{
  fs.writeFileSync('{output_path}', buffer);
  console.log('Cover letter written.');
}});
"""


def step6_cover_letter(
    analysis: dict,
    company: str,
    role: str,
    output_path: Path,
    config: WorkflowConfig,
    applicant_name: str,
    applicant_contact_line: str,
    colors: dict | None = None,
):
    """Generate the cover letter DOCX.

    applicant_name / applicant_contact_line must come from the actual user's
    own resume (see _identity_from_resume) — never a hardcoded default —
    so this document is never signed with someone else's name.
    """
    print_step(6, "Generating Cover Letter", config)

    if not applicant_name:
        raise WorkflowError(
            "Could not determine the applicant's name from their resume — "
            "refusing to generate a cover letter with a missing identity."
        )

    palette      = colors or {"primary": "1A3C5E", "border": "2B6CB0"}
    today        = date.today().strftime("%B %-d, %Y")
    contact_name = analysis.get("contact_name", "Hiring Team")
    salutation   = contact_name if contact_name != "Hiring Team" else "Hiring Team"

    body_keys = ["cover_letter_p1", "cover_letter_p2", "cover_letter_p3",
                 "cover_letter_p4", "cover_letter_p5"]

    body_paragraphs = []
    for i, key in enumerate(body_keys):
        text    = analysis.get(key, "")
        escaped = escape_js_string(text)
        after   = 200 if i == len(body_keys) - 1 else 160
        body_paragraphs.append(
            f'      new Paragraph({{\n'
            f'        spacing: {{ after: {after} }},\n'
            f'        children: [new TextRun({{ text: "{escaped}", font: "Calibri", size: 22, color: "111827" }})]\n'
            f'      }}),'
        )

    # Sign-off uses only phone | email (not full contact line) when both are
    # present; falls back to whatever the contact line actually has otherwise.
    contact_fields    = [f.strip() for f in applicant_contact_line.split("  |  ") if f.strip()]
    sign_off_contact  = escape_js_string("  |  ".join(contact_fields[:2]) or applicant_contact_line)

    js = COVER_LETTER_JS_TEMPLATE.format(
        today=today,
        contact_name=escape_js_string(contact_name),
        company=escape_js_string(company),
        role=escape_js_string(role),
        salutation=escape_js_string(salutation),
        body_paragraphs="\n".join(body_paragraphs),
        output_path=str(output_path).replace("\\", "/"),
        primary_color=escape_js_string(palette["primary"]),
        border_color=escape_js_string(palette["border"]),
        applicant_name=escape_js_string(applicant_name),
        applicant_name_upper=escape_js_string(applicant_name.upper()),
        contact_line=escape_js_string(applicant_contact_line),
        sign_off_contact=sign_off_contact,
    )

    js_path = output_path.parent / f"cover_letter_gen_{os.urandom(4).hex()}.js"
    write_file(js_path, js)
    result = run(["node", str(js_path)], check=False, config=config)
    js_path.unlink(missing_ok=True)  # always clean up, even on failure
    if result.returncode != 0:
        raise WorkflowError(f"Cover letter JS failed:\n{result.stderr}")

    config.progress(f"  ✓ Cover letter written to {output_path}")

# ---------------------------------------------------------------------------
# Step 8: Google Drive upload
# ---------------------------------------------------------------------------

GDRIVE_PARENT_FOLDER_ID = os.environ.get("GDRIVE_PARENT_FOLDER_ID", "")
GDRIVE_TOKEN_PATH       = Path.home() / ".config" / "job-apply" / "gdrive_token.json"
GDRIVE_CREDS_PATH       = Path(__file__).parent / "gdrive_credentials.json"
_MIME_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_SCOPES    = ["https://www.googleapis.com/auth/drive.file"]


_GDRIVE_TOKEN_TIGRIS_KEY = "system/gdrive_token.json"


def _write_gdrive_token(content: str) -> None:
    """Write the Drive OAuth token to disk restricted to the owner — it
    contains a long-lived refresh token, so it shouldn't be world/group
    readable under a permissive umask."""
    GDRIVE_TOKEN_PATH.write_text(content)
    try:
        os.chmod(GDRIVE_TOKEN_PATH, 0o600)
    except OSError:
        pass


def _seed_gdrive_token() -> None:
    """Materialize the Drive token to disk, preferring the Tigris-persisted copy.

    Priority: Tigris (always up-to-date after refreshes) → GDRIVE_TOKEN_JSON
    env var (set at deploy time, may have a stale access token but valid
    refresh token) → nothing (Drive disabled).
    """
    if GDRIVE_TOKEN_PATH.exists():
        return
    GDRIVE_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Try Tigris first — it has the latest refreshed token
    try:
        from scripts import storage
        tigris_token = storage.get_text(_GDRIVE_TOKEN_TIGRIS_KEY)
        if tigris_token:
            _write_gdrive_token(tigris_token)
            return
    except Exception:
        pass
    # Fall back to the env var set at deploy time; persist it to Tigris immediately
    # so future restarts use Tigris (and get refreshes) rather than the stale secret.
    token_json = os.environ.get("GDRIVE_TOKEN_JSON", "").strip()
    if token_json:
        _write_gdrive_token(token_json)
        _persist_gdrive_token()


def _persist_gdrive_token() -> None:
    """Write the current on-disk token back to Tigris so it survives restarts."""
    try:
        if not GDRIVE_TOKEN_PATH.exists():
            return
        from scripts import storage
        storage.put_text(_GDRIVE_TOKEN_TIGRIS_KEY, GDRIVE_TOKEN_PATH.read_text())
    except Exception:
        pass


def _gdrive_service(config: WorkflowConfig):
    """Return an authenticated Drive v3 service, or None if credentials are missing."""
    try:
        from googleapiclient.discovery import build
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
    except ImportError:
        config.progress("  ⚠ google-api-python-client not installed — skipping Drive upload")
        return None

    _seed_gdrive_token()

    creds = None
    if GDRIVE_TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(GDRIVE_TOKEN_PATH), _SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                _write_gdrive_token(creds.to_json())
                _persist_gdrive_token()
            except Exception as refresh_err:
                # invalid_grant means the token is permanently revoked — remove it
                # so the next run doesn't hit the same error, and tell the user.
                GDRIVE_TOKEN_PATH.unlink(missing_ok=True)
                config.progress(f"  ⚠ Drive token expired/revoked: {refresh_err}")
                config.progress("    To fix: run locally then update the secret:")
                config.progress("      rm ~/.config/job-apply/gdrive_token.json")
                config.progress("      python3 setup_gdrive.py")
                config.progress('      fly secrets set GDRIVE_TOKEN_JSON="$(cat ~/.config/job-apply/gdrive_token.json)"')
                return None
        elif GDRIVE_CREDS_PATH.exists():
            flow  = InstalledAppFlow.from_client_secrets_file(str(GDRIVE_CREDS_PATH), _SCOPES)
            creds = flow.run_local_server(port=0)
            GDRIVE_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
            _write_gdrive_token(creds.to_json())
        else:
            config.progress("  ⚠ Drive upload skipped — set GDRIVE_TOKEN_JSON secret to enable")
            return None

    return build("drive", "v3", credentials=creds)


def _gdrive_get_or_create_folder(service, name: str, parent_id: str) -> tuple[str, str, bool]:
    """Return (folder_id, webViewLink, created) for a named subfolder.

    created=True when the folder was just made; False when it already existed.
    """
    # Escape single quotes in name to prevent Drive query injection
    safe_name = name.replace("\\", "\\\\").replace("'", "\\'")
    existing = service.files().list(
        q=(
            f"name='{safe_name}' and '{parent_id}' in parents and "
            "mimeType='application/vnd.google-apps.folder' and trashed=false"
        ),
        fields="files(id, webViewLink)",
        pageSize=1,
    ).execute().get("files", [])

    if existing:
        return existing[0]["id"], existing[0]["webViewLink"], False

    created = service.files().create(
        body={"name": name, "mimeType": "application/vnd.google-apps.folder",
              "parents": [parent_id]},
        fields="id, webViewLink",
    ).execute()
    return created["id"], created["webViewLink"], True


def _ensure_run_folder(service, company_safe: str, role_safe: str, config: WorkflowConfig) -> tuple[str, str]:
    """Resolve (and create if needed) the Drive folder for a company/role pair.

    Drive structure:
      Job Applications/
        {user_label}/          ← created when config.user_label is set
          {Company}_{Role}/

    Returns (folder_id, webViewLink). Idempotent — safe to call repeatedly.
    """
    if config.user_label:
        user_folder_id, _, _ = _gdrive_get_or_create_folder(
            service, config.user_label, GDRIVE_PARENT_FOLDER_ID
        )
        config.progress(f"  ✓ Drive user folder: {config.user_label}")
        run_parent_id = user_folder_id
    else:
        run_parent_id = GDRIVE_PARENT_FOLDER_ID

    run_folder_name = f"{company_safe}_{role_safe}"
    run_folder_id, folder_url, run_created = _gdrive_get_or_create_folder(
        service, run_folder_name, run_parent_id
    )
    config.progress(f"  ✓ Drive run folder: {run_folder_name}")
    if run_created:
        # Share only this run's folder, not the parent user folder — the parent
        # accumulates every application the user ever generates, so sharing it
        # (which Drive permissions inherit to all children) would let one leaked
        # link expose the user's entire application history instead of just this run.
        _set_link_viewer(service, run_folder_id, config.progress)
    return run_folder_id, folder_url


def ensure_application_gdrive_folder(company: str, role: str, config: WorkflowConfig) -> tuple[str, str] | None:
    """Get-or-create the Drive folder for an application's company/role, outside of a full run.

    Returns (folder_id, folder_url), or None if Drive isn't configured/reachable.
    """
    service = _gdrive_service(config)
    if service is None:
        return None
    try:
        return _ensure_run_folder(service, safe_filename(company), safe_filename(role), config)
    except Exception as exc:
        config.progress(f"  ⚠ Could not resolve Drive folder: {exc}")
        return None


def _set_link_viewer(service, folder_id: str, progress: callable) -> None:
    """Grant 'anyone with the link' viewer access to a Drive folder.

    Silently ignores errors — the most common cause is the permission
    already existing (Drive returns a 409 in that case).
    """
    try:
        service.permissions().create(
            fileId=folder_id,
            body={"type": "anyone", "role": "reader"},
            fields="id",
        ).execute()
        progress("  ✓ Drive folder set to 'anyone with the link' viewer access")
    except Exception as exc:
        # 409 = permission already exists; any other error is non-fatal
        progress(f"  ⚠ Could not set Drive folder permissions: {exc}")


def _convert_docx_to_pdf_via_drive(
    service,
    docx_path: Path,
    pdf_name: str,
    folder_id: str,
    progress: callable,
) -> None:
    """Convert a local DOCX to PDF using Drive's conversion pipeline.

    Steps:
      1. Upload the DOCX with mimeType=Google Doc — Drive converts on ingest.
      2. Export the resulting Google Doc as PDF bytes.
      3. Upload the PDF to the run folder.
      4. Delete the temporary Google Doc.

    Best-effort: any exception is logged and swallowed so the caller is
    never blocked by a PDF conversion failure.
    """
    try:
        import io
        from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload

        # Step 1: upload DOCX as Google Doc (Drive handles the conversion)
        gdoc = service.files().create(
            body={"name": f"_tmp_{docx_path.stem}",
                  "mimeType": "application/vnd.google-apps.document"},
            media_body=MediaFileUpload(str(docx_path), mimetype=_MIME_DOCX),
            fields="id",
        ).execute()
        gdoc_id = gdoc["id"]

        try:
            # Step 2: export as PDF
            pdf_bytes = service.files().export(
                fileId=gdoc_id,
                mimeType="application/pdf",
            ).execute()

            # Step 3: upload PDF to the run folder
            service.files().create(
                body={"name": pdf_name, "parents": [folder_id]},
                media_body=MediaIoBaseUpload(
                    io.BytesIO(pdf_bytes), mimetype="application/pdf"
                ),
                fields="id",
            ).execute()
            progress(f"  ✓ Generated PDF: {pdf_name}")

        finally:
            # Step 4: always clean up the temp Google Doc
            try:
                service.files().delete(fileId=gdoc_id).execute()
            except Exception:
                pass

    except Exception as exc:
        progress(f"  ⚠ PDF generation skipped: {exc}")


def step8_upload(
    run_dir: Path,
    company_safe: str,
    role_safe: str,
    config: WorkflowConfig,
) -> str | None:
    """Upload output files to Google Drive. Returns the run folder URL or None.

    Drive structure:
      Job Applications/
        {user_label}/          ← created when config.user_label is set
          {Company}_{Role}/
    """
    try:
        from googleapiclient.http import MediaFileUpload
    except ImportError:
        config.progress("  ⚠ google-api-python-client not installed — skipping Drive upload")
        return None

    print_step(8, "Uploading to Google Drive", config)

    try:
        service = _gdrive_service(config)
        if service is None:
            return None

        run_folder_id, folder_url = _ensure_run_folder(service, company_safe, role_safe, config)

        for f in sorted(run_dir.iterdir()):
            if f.name.startswith("~$"):
                continue
            if f.suffix == ".docx":
                mime = _MIME_DOCX
            elif f.suffix == ".pdf":
                mime = "application/pdf"
            else:
                continue
            # Upsert by document TYPE pattern (not just an exact name match) —
            # re-running apply_run for the same company/role must replace the
            # prior files in place, not pile up stale duplicates alongside
            # the fresh ones. A pattern match also self-heals a Drive file
            # left over from before a naming-convention change (e.g. an
            # ALL-CAPS applicant name baked into an older run) by renaming it
            # to today's name instead of leaving it stuck under the old one.
            if f.name.endswith("_ATS.docx"):
                _gdrive_upsert_file_by_pattern(
                    service, run_folder_id, r"^Resume_.*_ATS\.docx$", f.name, f, mime=mime)
            elif re.match(r"^Resume_.*\.docx$", f.name):
                _gdrive_upsert_file_by_pattern(
                    service, run_folder_id, r"^Resume_(?!.*_ATS\.docx$).*\.docx$", f.name, f, mime=mime)
            elif re.match(r"^CoverLetter_.*\.docx$", f.name):
                _gdrive_upsert_file_by_pattern(
                    service, run_folder_id, r"^CoverLetter_.*\.docx$", f.name, f, mime=mime)
            else:
                _gdrive_upsert_file(service, run_folder_id, f.name, f, mime=mime)
            config.progress(f"  ✓ Uploaded {f.name}")

        # Convert the styled (non-ATS) resume to PDF via Drive. Looked up by
        # glob rather than reconstructed from a filename pattern — the actual
        # name uses whatever run_workflow() read from this user's own resume.
        styled_resume = next(
            (f for f in run_dir.glob("Resume_*.docx") if not f.name.endswith("_ATS.docx")),
            None,
        )
        if styled_resume:
            pdf_name = styled_resume.stem + ".pdf"
            _gdrive_delete_by_pattern(service, run_folder_id, r"^Resume_.*\.pdf$")
            _convert_docx_to_pdf_via_drive(
                service,
                styled_resume,
                pdf_name,
                run_folder_id,
                config.progress,
            )

        return folder_url

    except Exception as exc:
        config.progress(f"  ⚠ Drive upload failed: {exc}")
        config.progress("    Files are still available for download below.")
        return None

# ---------------------------------------------------------------------------
# Drive: targeted single-file upload (used by interview prep)
# ---------------------------------------------------------------------------

def _upload_single_to_drive(
    file_path: Path,
    folder_name: str,
    config: WorkflowConfig,
) -> str | None:
    """Upload one file into the correct user → run subfolder in Drive."""
    try:
        from googleapiclient.http import MediaFileUpload
    except ImportError:
        config.progress("  ⚠ google-api-python-client not installed — skipping Drive upload")
        return None

    try:
        service = _gdrive_service(config)
        if service is None:
            return None

        if config.user_label:
            user_folder_id, _, _ = _gdrive_get_or_create_folder(
                service, config.user_label, GDRIVE_PARENT_FOLDER_ID
            )
            run_parent_id = user_folder_id
        else:
            run_parent_id = GDRIVE_PARENT_FOLDER_ID

        folder_id, folder_url, folder_created = _gdrive_get_or_create_folder(
            service, folder_name, run_parent_id
        )
        config.progress(f"  ✓ Drive folder: {folder_name}")
        if folder_created:
            # Share only this run's folder — see _ensure_run_folder for why the
            # parent user folder is deliberately left unshared.
            _set_link_viewer(service, folder_id, config.progress)

        media = MediaFileUpload(str(file_path), mimetype=_MIME_DOCX, resumable=False)
        service.files().create(
            body={"name": file_path.name, "parents": [folder_id]},
            media_body=media,
            fields="id",
        ).execute()
        config.progress(f"  ✓ Uploaded {file_path.name}")

        return folder_url

    except Exception as exc:
        config.progress(f"  ⚠ Drive upload failed: {exc}")
        config.progress("    File is still available for download below.")
        return None


# ---------------------------------------------------------------------------
# Drive: per-file helpers (used by optimize_run)
# ---------------------------------------------------------------------------

def _gdrive_query_escape(name: str) -> str:
    """Escape a value for use inside a Drive API query string literal."""
    return name.replace("\\", "\\\\").replace("'", "\\'")


def _gdrive_list_files(service, folder_id: str) -> list[dict]:
    """Return [{id, name}] for all non-trashed files in a Drive folder."""
    return service.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        fields="files(id, name)",
        pageSize=100,
    ).execute().get("files", [])


def _gdrive_download_file(service, file_id: str) -> bytes:
    """Download a Drive file's content as bytes."""
    import io
    from googleapiclient.http import MediaIoBaseDownload
    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def _gdrive_upsert_file(
    service,
    folder_id: str,
    name: str,
    local_path: Path,
    mime: str = _MIME_DOCX,
) -> str:
    """Upload a file into a Drive folder, replacing the existing file's content
    in place when a file with the same name exists (keeps the file ID stable so
    existing share links continue to work). Returns the file ID."""
    from googleapiclient.http import MediaFileUpload

    existing = service.files().list(
        q=(
            f"name='{_gdrive_query_escape(name)}' and '{folder_id}' in parents "
            "and trashed=false"
        ),
        fields="files(id)",
        pageSize=1,
    ).execute().get("files", [])

    media = MediaFileUpload(str(local_path), mimetype=mime, resumable=False)
    if existing:
        return service.files().update(
            fileId=existing[0]["id"], media_body=media, fields="id",
        ).execute()["id"]
    return service.files().create(
        body={"name": name, "parents": [folder_id]},
        media_body=media,
        fields="id",
    ).execute()["id"]


def _gdrive_delete_by_name(service, folder_id: str, name: str) -> None:
    """Best-effort delete of all files with this name in a Drive folder."""
    try:
        files = service.files().list(
            q=(
                f"name='{_gdrive_query_escape(name)}' and '{folder_id}' in parents "
                "and trashed=false"
            ),
            fields="files(id)",
            pageSize=10,
        ).execute().get("files", [])
        for f in files:
            service.files().delete(fileId=f["id"]).execute()
    except Exception:
        pass


def _gdrive_upsert_file_by_pattern(
    service,
    folder_id: str,
    name_pattern: str,
    target_name: str,
    local_path: Path,
    mime: str = _MIME_DOCX,
) -> str:
    """Upload local_path into a Drive folder as target_name, replacing the
    content of any existing file whose CURRENT name matches name_pattern
    (not just an exact match on target_name).

    Keeps the matched file's ID stable (so existing share links keep
    working) but renames it to target_name when the current name doesn't
    already match — self-heals a filename left over from before a naming
    convention changed (e.g. an ALL-CAPS applicant name baked into an
    earlier run's Drive file), instead of leaving it stuck under the old
    name forever or piling up a duplicate under the new one.
    """
    from googleapiclient.http import MediaFileUpload

    files = service.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        fields="files(id, name)",
        pageSize=100,
    ).execute().get("files", [])
    existing = next((f for f in files if re.match(name_pattern, f["name"])), None)

    media = MediaFileUpload(str(local_path), mimetype=mime, resumable=False)
    if existing:
        body = {} if existing["name"] == target_name else {"name": target_name}
        return service.files().update(
            fileId=existing["id"], body=body, media_body=media, fields="id",
        ).execute()["id"]
    return service.files().create(
        body={"name": target_name, "parents": [folder_id]},
        media_body=media, fields="id",
    ).execute()["id"]


def _gdrive_delete_by_pattern(service, folder_id: str, name_pattern: str) -> None:
    """Best-effort delete of all files in a Drive folder whose name matches
    name_pattern — used to clear out a stale-cased PDF before regenerating
    one under the current naming convention."""
    try:
        files = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="files(id, name)",
            pageSize=100,
        ).execute().get("files", [])
        for f in files:
            if re.match(name_pattern, f["name"]):
                service.files().delete(fileId=f["id"]).execute()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Drive: list run folders + fetch job posting (used by /api/gdrive/runs)
# ---------------------------------------------------------------------------

_FOLDER_MIME = "application/vnd.google-apps.folder"


def list_gdrive_run_folders(user_label: str, config: WorkflowConfig) -> list[dict]:
    """Return all run folders visible to this user from Google Drive.

    Checks two locations:
      1. Job Applications/{user_label}/  — current per-user structure
      2. Job Applications/ root          — legacy flat runs (skips email-named subfolders)

    Each entry: {name, id, web_view_link, source ("user" | "legacy")}
    Returns [] if Drive is not configured or an error occurs.
    """
    service = _gdrive_service(config)
    if service is None:
        return []

    results: list[dict] = []
    seen_ids: set[str]  = set()

    try:
        # ── 1. User's personal subfolder ──────────────────────────────
        safe_user_label = user_label.replace("\\", "\\\\").replace("'", "\\'")
        user_roots = service.files().list(
            q=(
                f"name='{safe_user_label}' and '{GDRIVE_PARENT_FOLDER_ID}' in parents and "
                f"mimeType='{_FOLDER_MIME}' and trashed=false"
            ),
            fields="files(id)",
            pageSize=1,
        ).execute().get("files", [])

        if user_roots:
            user_root_id = user_roots[0]["id"]
            for f in service.files().list(
                q=f"'{user_root_id}' in parents and mimeType='{_FOLDER_MIME}' and trashed=false",
                fields="files(id, name, webViewLink)",
                orderBy="modifiedTime desc",
                pageSize=100,
            ).execute().get("files", []):
                results.append({
                    "name":          f["name"],
                    "id":            f["id"],
                    "web_view_link": f.get("webViewLink", ""),
                    "source":        "user",
                })
                seen_ids.add(f["id"])

        # ── 2. Legacy flat root ────────────────────────────────────────
        for f in service.files().list(
            q=(
                f"'{GDRIVE_PARENT_FOLDER_ID}' in parents and "
                f"mimeType='{_FOLDER_MIME}' and trashed=false"
            ),
            fields="files(id, name, webViewLink)",
            orderBy="modifiedTime desc",
            pageSize=100,
        ).execute().get("files", []):
            if f["id"] in seen_ids:
                continue
            # Skip user account folders (named like emails)
            if "@" in f["name"]:
                continue
            results.append({
                "name":          f["name"],
                "id":            f["id"],
                "web_view_link": f.get("webViewLink", ""),
                "source":        "legacy",
            })

    except Exception:
        pass  # best-effort; return whatever we collected

    return results


def get_gdrive_job_posting(folder_id: str, config: WorkflowConfig) -> str | None:
    """Fetch job description from a Drive folder. Prefers job_description.md, falls back to job_posting.txt."""
    service = _gdrive_service(config)
    if service is None:
        return None
    try:
        for name in ("job_description.md", "job_posting.txt"):
            files = service.files().list(
                q=f"name='{name}' and '{folder_id}' in parents and trashed=false",
                fields="files(id)",
                pageSize=1,
            ).execute().get("files", [])
            if files:
                content = service.files().get_media(fileId=files[0]["id"]).execute()
                return content.decode("utf-8") if isinstance(content, bytes) else str(content)
        return None
    except Exception:
        return None


def save_gdrive_job_posting(folder_id: str, markdown: str, config: WorkflowConfig) -> bool:
    """Upsert job_description.md in a Drive folder. Returns True on success."""
    try:
        from googleapiclient.http import MediaInMemoryUpload
    except ImportError:
        return False
    service = _gdrive_service(config)
    if service is None:
        return False
    try:
        # Delete any existing job_description.md first
        existing = service.files().list(
            q=f"name='job_description.md' and '{folder_id}' in parents and trashed=false",
            fields="files(id)",
            pageSize=1,
        ).execute().get("files", [])
        for f in existing:
            service.files().delete(fileId=f["id"]).execute()
        # Upload fresh copy
        media = MediaInMemoryUpload(markdown.encode("utf-8"), mimetype="text/markdown", resumable=False)
        service.files().create(
            body={"name": "job_description.md", "parents": [folder_id]},
            media_body=media,
            fields="id",
        ).execute()
        return True
    except Exception:
        return False


def get_latest_gdrive_resume_text(folder_id: str, config: WorkflowConfig) -> str | None:
    """Return the plain text of the most recent tailored resume in a Drive folder.

    Picks the most recently modified styled resume (``Resume_*.docx``, excluding
    the ATS variant); falls back to the ATS resume if that is all that's present.
    Returns ``None`` when Drive is unreachable or the folder holds no resume yet —
    callers should fall back to the user's master resume in that case.
    Best-effort: never raises.
    """
    service = _gdrive_service(config)
    if service is None:
        return None
    try:
        files = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="files(id, name, modifiedTime)",
            orderBy="modifiedTime desc",
            pageSize=100,
        ).execute().get("files", [])
    except Exception:
        return None

    resumes = [f for f in files if re.match(r"^Resume_.*\.docx$", f["name"])]
    if not resumes:
        return None
    # `resumes` is already newest-first; prefer the styled resume over the ATS one.
    chosen = next((f for f in resumes if not f["name"].endswith("_ATS.docx")), resumes[0])

    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".docx", delete=False, dir="/tmp")
    try:
        tmp.write(_gdrive_download_file(service, chosen["id"]))
        tmp.close()
        text = extract_resume_text(
            WorkflowConfig(progress=config.progress, master_resume=Path(tmp.name))
        )
        config.progress(f"  ✓ Scoring against latest Drive resume: {chosen['name']}")
        return text
    except Exception:
        return None
    finally:
        try:
            Path(tmp.name).unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Auto-capture: extract job description from a posting URL via Claude
# ---------------------------------------------------------------------------

_JD_EXTRACTION_SYSTEM = """You extract a single job posting from raw webpage HTML/text.

Return ONLY the content of THIS job posting, as clean plain text, organized under
these sections where present: Title, Company, Location, Compensation, About the
Role / Summary, Responsibilities, Requirements / Qualifications, Benefits, How to
Apply. Preserve the original wording — do not paraphrase or summarize.

Aggressively strip out everything that is not part of this specific posting's
content, including but not limited to: cookie/consent banners, site navigation
and menus, headers and footers, "related jobs" / "similar postings" / "other
openings" lists, social-share links, sign-in/account prompts, ads, tracking
scripts, legal boilerplate (privacy policy, terms of use, EEO statements that
aren't part of the actual posting body), and any company marketing content not
specific to this role.

If the page does not contain a job posting, respond with exactly: NONE"""


def extract_job_description_from_url(url: str, config: WorkflowConfig) -> str | None:
    """Extract the job description text from a posting URL by fetching the page
    and asking Claude to pull out just the posting content.

    Returns the extracted text, or None on fetch failure / no posting found.
    Best-effort — never raises.
    """
    if is_ssrf_url(url):
        config.progress("  ⚠ Posting URL resolves to a private/internal address, skipping fetch")
        return None
    try:
        import requests
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; JobApplyBot/1.0)"},
            timeout=20,
        )
        resp.raise_for_status()
        page_text = resp.text[:60_000]
    except Exception as exc:
        config.progress(f"  ⚠ Could not fetch posting URL: {exc}")
        return None

    try:
        text = claude(_JD_EXTRACTION_SYSTEM, page_text, max_tokens=4096, config=config).strip()
        if not text or text == "NONE":
            config.progress("  ⚠ Claude found no extractable job description")
            return None
        return text
    except Exception as exc:
        config.progress(f"  ⚠ Job description extraction failed: {exc}")
        return None


def auto_capture_job_description(company: str, role: str, url: str, config: WorkflowConfig) -> tuple[str, str] | None:
    """Best-effort pipeline: ensure the application's Drive folder exists, extract
    the JD text from its posting URL via Claude, and save it as job_description.md.

    Returns (folder_id, folder_url) once the folder is resolved — regardless of
    whether extraction itself succeeded — so callers can link the folder to the
    application record either way. Returns None only if the folder couldn't be
    resolved at all. Never raises.
    """
    config.progress(f"\n📄 Auto-capturing job description for {company} / {role}")

    folder = ensure_application_gdrive_folder(company, role, config)
    if folder is None:
        config.progress("  ⚠ Could not resolve Drive folder — aborting auto-capture")
        return None
    folder_id, folder_url = folder

    text = extract_job_description_from_url(url, config)
    if text and save_gdrive_job_posting(folder_id, text, config):
        config.progress("  ✓ Saved job_description.md to Drive")
    else:
        config.progress("  ⚠ Could not save job_description.md to Drive")

    return folder_id, folder_url


# ---------------------------------------------------------------------------
# Resume <-> job match scoring
# ---------------------------------------------------------------------------

MATCH_CATEGORIES = (
    (80, "Strong Match"),
    (60, "Good Match"),
    (40, "Stretch"),
    (0,  "Long Shot"),
)


def _match_category(score: int) -> str:
    for threshold, label in MATCH_CATEGORIES:
        if score >= threshold:
            return label
    return MATCH_CATEGORIES[-1][1]


_MATCH_SCORING_SYSTEM = """You are a hiring-fit analyst. Compare a candidate's resume \
and profile against a job posting and produce an honest, calibrated match score.

Score four dimensions, each on a 0-100 scale, then combine them into an overall \
score using these weights:
- skills (40%): overlap between the JD's required/preferred skills and the \
candidate's demonstrated technical stack and experience
- role_type (25%): how well the JD's role archetype (e.g. delivery, platform \
engineering, solutions engineering, AI/agentic, customer success) matches the \
roles and narrative threads the candidate's resume/profile emphasize
- seniority (20%): whether the JD's level and scope of ownership match the \
candidate's career trajectory and proof points (don't penalize lightly for being \
slightly over- or under-leveled; penalize heavily for a large mismatch)
- differentiators (15%): whether the JD calls out things the candidate \
specifically excels at or has unusual proof points for

Be honest and calibrated against this specific candidate — someone intentionally \
applying to roles that match their background. A posting where the candidate's \
experience directly addresses the JD's core requirements should score 75-90. \
Reserve 90+ for an unusually tight fit (rare). Use the low end (below 50) when \
there is a genuine mismatch in role type, required skills, or seniority — not \
just because some preferred qualifications are missing.

Return ONLY a JSON object with exactly these keys:
{
  "dimensions": {
    "skills": <int 0-100>,
    "role_type": <int 0-100>,
    "seniority": <int 0-100>,
    "differentiators": <int 0-100>
  },
  "score": <int 0-100, the weighted overall score, rounded>,
  "rationale": "<1-2 sentences: the strongest alignment, then the biggest gap>"
}

Write the rationale in your own words — do not quote phrases verbatim out of the \
job posting or resume. If you must reference exact wording, escape any double \
quote character inside the JSON string with a backslash (\\") so the JSON stays valid.

No preamble, no markdown fences, no commentary — JSON only."""


def score_application_match(
    jd_text: str,
    resume_text: str,
    profile_text: str,
    config: WorkflowConfig | None = None,
) -> dict:
    """Ask Claude to score how well a candidate's resume/profile matches a job
    posting. Returns {dimensions, score, category, rationale}. Raises on
    failure — callers decide how to surface that (this is not best-effort)."""
    user = f"""\
Job Posting:
---
{jd_text}
---

Candidate Resume:
---
{resume_text}
---

Candidate Profile Guide:
---
{profile_text}
---
"""
    # Claude reliably stops at end_turn (not max_tokens) but occasionally omits
    # the final closing brace/quote on this prompt's free-form rationale field —
    # confirmed via repeated live sampling (stop_reason=end_turn, output_tokens
    # far under budget, text otherwise well-formed). Repair a dangling unclosed
    # string/object before falling back to a full retry.
    last_err: json.JSONDecodeError | None = None
    data = None
    for attempt in range(2):
        raw = claude(_MATCH_SCORING_SYSTEM, user, max_tokens=8000, config=config)
        raw = raw.strip()
        raw = re.sub(r"^```json\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw.strip())
        # Extract the outermost JSON object, handling braces inside string values
        start = raw.find("{")
        if start != -1:
            depth, in_str, esc, end = 0, False, False, -1
            for i in range(start, len(raw)):
                c = raw[i]
                if esc:
                    esc = False
                elif c == '\\' and in_str:
                    esc = True
                elif c == '"':
                    in_str = not in_str
                elif not in_str:
                    if c == '{':
                        depth += 1
                    elif c == '}':
                        depth -= 1
                        if depth == 0:
                            end = i
                            break
            if end != -1:
                raw = raw[start:end + 1]
            else:
                # No balanced close found — the object is likely just missing
                # its trailing quote/braces. Close whatever's left open.
                repaired = raw[start:] + ('"' if in_str else '') + ('}' * max(depth, 0))
                try:
                    data = json.loads(repaired)
                    break
                except json.JSONDecodeError:
                    pass
        try:
            data = json.loads(raw)
            break
        except json.JSONDecodeError as e:
            try:
                data = json.loads(_repair_unescaped_quotes(raw))
                break
            except json.JSONDecodeError:
                last_err = e
                continue
    if data is None:
        raise WorkflowError(
            f"Match scoring: Claude returned malformed JSON twice in a row "
            f"({last_err}).\n\nRaw:\n{raw[:2000]}"
        )

    score = max(0, min(100, int(round(float(data["score"])))))
    return {
        "score":      score,
        "category":   _match_category(score),
        "dimensions": data.get("dimensions", {}),
        "rationale":  data.get("rationale", ""),
    }


# ---------------------------------------------------------------------------
# Public workflow entry point
# ---------------------------------------------------------------------------

def run_workflow(
    job_posting: str,
    company: str,
    role: str,
    contact: str | None = None,
    config: WorkflowConfig | None = None,
    domain: str | None = None,
) -> WorkflowResult:
    """
    Run the full job-application workflow.

    Args:
        job_posting: Full text of the job posting.
        company:     Company name (used in filenames and cover letter).
        role:        Role title.
        contact:     Hiring manager name, or None to let analysis infer it.
        config:      WorkflowConfig for model, progress callback, debug, dry_run.
        domain:      Company domain, if already known (e.g. from the application
                     tracker record) — passed straight to Brandfetch instead of a
                     fuzzy name search, which can match the wrong company for a
                     common/ambiguous name.

    Returns:
        WorkflowResult with paths to generated files and optional Drive URL.

    Raises:
        WorkflowError on any unrecoverable error.
    """
    if config is None:
        config = WorkflowConfig()

    OUTPUT_DIR.mkdir(exist_ok=True)

    company_safe = safe_filename(company)
    role_safe    = safe_filename(role)
    # Scope to user subfolder when running via the server; CLI runs go to output/ directly.
    if config.user_id:
        run_dir = OUTPUT_DIR / safe_filename(config.user_id) / f"{company_safe}_{role_safe}"
    else:
        run_dir = OUTPUT_DIR / f"{company_safe}_{role_safe}"
    run_dir.mkdir(parents=True, exist_ok=True)

    applicant_name_safe = _applicant_name_for_filenames(config.master_resume)
    resume_out = run_dir / f"Resume_{applicant_name_safe}_{company_safe}_{role_safe}.docx"
    ats_out    = run_dir / f"Resume_{applicant_name_safe}_{company_safe}_{role_safe}_ATS.docx"
    cover_out  = run_dir / f"CoverLetter_{applicant_name_safe}_{company_safe}_{role_safe}.docx"

    # Purge stale same-company/role outputs left in run_dir from an earlier
    # run under a different applicant-name casing (e.g. a run from before
    # title_case_name() existed) — otherwise they'd sit alongside today's
    # correctly-named files, get uploaded to Drive as orphaned duplicates,
    # and make step8_upload's Resume_*.docx glob (used to pick which resume
    # to convert to PDF) non-deterministic.
    for stale in [*run_dir.glob("Resume_*.docx"), *run_dir.glob("CoverLetter_*.docx"),
                  *run_dir.glob("Resume_*.pdf")]:
        if stale not in (resume_out, ats_out, cover_out):
            stale.unlink(missing_ok=True)

    config.progress(f"\n\U0001f680 Job Application Agent")
    config.progress(f"   Company : {company}")
    config.progress(f"   Role    : {role}")
    config.progress(f"   Run dir : {run_dir}")
    config.progress(f"   Outputs : {resume_out.name}, {ats_out.name}, {cover_out.name}")

    # Step 1
    job_posting, resume_text, profile = step1_read_inputs(job_posting, config)

    # Step 1b: parse this user's own master resume into editable fields —
    # drives step2's output shape and step4's edits, so tailoring works for
    # whatever jobs/bullets/competency cells that resume actually has.
    master_resume_path = Path(config.master_resume) if config.master_resume else MASTER_RESUME
    resume_data = _parse_styled_resume(master_resume_path)
    field_map   = _build_resume_field_map(resume_data)
    jobs_legend = "\n".join(
        f"  job{j}: {job.get('company', '?')} ({job.get('dates', '')})"
        for j, job in enumerate(resume_data.get("jobs", []), start=1)
    )

    # Step 2
    analysis = step2_analyze(
        job_posting, resume_text, profile, company, role, contact,
        field_map, jobs_legend, config,
    )

    if config.dry_run:
        config.progress("\n  [dry-run] Skipping file generation — analysis complete.")
        return WorkflowResult(
            run_dir=run_dir,
            resume_path=resume_out,
            ats_path=ats_out,
            cover_letter_path=cover_out,
            framing_angle=analysis.get("framing_angle", ""),
        )

    # Step 2b
    colors = step2b_brand_colors(company, config, domain=domain or "")

    # Steps 3–5: styled resume
    step3_unpack(config)
    edits_ok, edits_total = step4_apply_edits(analysis, field_map, colors, config)
    replacements_warning = (
        f"Only {edits_ok}/{edits_total} XML replacements succeeded — "
        "some resume sections may not be fully tailored."
    ) if edits_total > 0 and edits_ok < edits_total * 0.7 else None
    step5_pack(resume_out, config)

    # Step 5b: ATS resume
    step5b_ats_resume(resume_out, ats_out, config)

    # Step 6: cover letter
    applicant_name, applicant_contact_line = _identity_from_resume(resume_out, config)
    step6_cover_letter(
        analysis, company, role, cover_out, config,
        applicant_name=applicant_name, applicant_contact_line=applicant_contact_line,
        colors=colors,
    )

    # Step 7: cleanup
    step7_cleanup(config)

    # Step 8: Drive upload
    folder_url = step8_upload(run_dir, company_safe, role_safe, config)

    return WorkflowResult(
        run_dir=run_dir,
        resume_path=resume_out,
        ats_path=ats_out,
        cover_letter_path=cover_out,
        framing_angle=analysis.get("framing_angle", ""),
        folder_url=folder_url,
        replacements_warning=replacements_warning,
    )

# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Interview Prep
# ---------------------------------------------------------------------------

PREP_SYSTEM = """\
You are an expert interview coach preparing a candidate for a specific interview.
You'll be given their resume and a profile/voice guide — use those, and only those,
to learn their background deeply.

Your job is to produce a concise, 2-page interview prep document — a working
reference the candidate will skim right before the call, not a comprehensive report.
Be ruthlessly specific: name tools, quote numbers, reference real projects by name.
Never merge two distinct projects or accomplishments into one bullet or one story,
even if they're thematically similar — attribute every fact to the specific
project it actually came from.

All content must be in the candidate's own voice, as established by their profile
guide: direct, first-person, no corporate filler. Every prepared answer must be
specific enough that it couldn't apply to any other candidate. If something about
the company or role is uncertain, state that plainly rather than inventing detail.

NATURAL FLOW: Make every answer sound like something the candidate would actually
say out loud, not a script. Vary sentence length and avoid repetitive sentence
patterns.

Return ONLY valid JSON. No preamble, no markdown fences. Emit the JSON object
exactly once — do not think out loud, self-correct, or restart mid-response;
if you notice a mistake, just get the object right the first time.

If a string value needs to quote a phrase (from the job posting or elsewhere),
escape the inner quote marks as \\" or paraphrase instead of quoting — a literal
unescaped quote inside a JSON string breaks the parser.
"""


def _build_prep_docx_js(
    data: dict,
    company: str,
    role: str,
    interviewer: str,
    output_path: Path,
    colors: dict,
    candidate_name: str = "",
    interview_date: str = "",
    interview_time: str = "",
    location: str = "",
    logo: dict | None = None,
) -> str:
    """Return a Node.js script that produces the interview prep DOCX.

    Single-column, flowing 2-page document: header block, then 6 numbered
    sections separated by horizontal rules. Bullets over prose everywhere
    except the elevator pitch itself.
    """

    colors = colors or {}
    NAVY  = colors.get("primary")   or "1F4E79"
    TEAL  = colors.get("secondary") or "00695C"
    GRAY  = "555555"
    DARK  = "1A1A1A"

    def esc(text: str) -> str:
        return escape_js_string(" ".join(str(text).split()))

    def _run(text: str, bold: bool = False, italic: bool = False,
             size: int = 19, color: str = DARK, underline: bool = False,
             _pre_escaped: str | None = None) -> str:
        escaped = _pre_escaped if _pre_escaped is not None else esc(text)
        props = [f'text: "{escaped}"', 'font: "Arial"',
                 f'size: {size}', f'color: "{esc(color)}"', 'noProof: true']
        if bold:      props.append("bold: true")
        if italic:    props.append("italics: true")
        if underline: props.append("underline: {}")
        return "new TextRun({ " + ", ".join(props) + " })"

    _URL_RE = re.compile(r'(https?://[^\s]+|www\.[^\s]+)')
    _URL_TRAILING_PUNCT = ".,;:)]}”’"

    def tr(text: str, bold: bool = False, italic: bool = False,
           size: int = 19, color: str = DARK) -> list[str]:
        """Split text on URL-looking substrings and render each as a real
        clickable hyperlink, everything else as plain text — so a pasted
        LinkedIn profile or video-call link becomes clickable automatically."""
        parts = _URL_RE.split(str(text))
        if len(parts) == 1:
            return [_run(text, bold=bold, italic=italic, size=size, color=color)]
        runs: list[str] = []
        last_idx = len(parts) - 1
        for i, part in enumerate(parts):
            if not part:
                continue
            if _URL_RE.fullmatch(part):
                trail = ""
                while part and part[-1] in _URL_TRAILING_PUNCT:
                    trail = part[-1] + trail
                    part = part[:-1]
                link_text = esc(part)
                runs.append(
                    'new ExternalHyperlink({ link: "' + link_text + '", children: ['
                    + _run(part, bold=bold, italic=italic, size=size, color="0563C1", underline=True)
                    + '] })'
                )
                if trail:
                    runs.append(_run(trail, bold=bold, italic=italic, size=size, color=color))
            else:
                # Collapse internal whitespace but only strip at the very edges
                # of the whole text — a boundary space next to a hyperlink
                # (e.g. "Location: <link>") must survive so words don't run
                # together with the link.
                collapsed = re.sub(r"\s+", " ", part)
                if i == 0:
                    collapsed = collapsed.lstrip()
                if i == last_idx:
                    collapsed = collapsed.rstrip()
                if not collapsed:
                    continue
                runs.append(_run(part, bold=bold, italic=italic, size=size, color=color,
                                  _pre_escaped=escape_js_string(collapsed)))
        return runs

    def para(children: list[str], before: int = 0, after: int = 80,
             left: int = 0, border_bottom_color: str = "") -> str:
        spacing = f"before: {before}, after: {after}"
        indent  = f", indent: {{ left: {left} }}" if left else ""
        border  = (
            f', border: {{ bottom: {{ style: BorderStyle.SINGLE, size: 4, '
            f'color: "{esc(border_bottom_color)}", space: 4 }} }}'
        ) if border_bottom_color else ""
        return (f'new Paragraph({{ spacing: {{ {spacing} }}{indent}{border}, '
                f'children: [{", ".join(children)}] }})')

    def bullet(text: str, size: int = 18) -> str:
        return para(tr(f"•  {text}", size=size), before=15, after=15, left=200)

    def section_header(number: int, title: str, color: str) -> str:
        return para(tr(f"{number}. {title}", bold=True, size=21, color=color),
                    before=200, after=70, border_bottom_color=color)

    def logo_dims(width, height, max_w: int = 160, max_h: int = 50) -> tuple[int, int]:
        if width and height and width > 0 and height > 0:
            scale = min(max_w / width, max_h / height, 2.0)
            return max(1, round(width * scale)), max(1, round(height * scale))
        return 120, 40

    paras: list[str] = []

    # =========================================================================
    # HEADER BLOCK
    # =========================================================================
    if logo and logo.get("bytes"):
        logo_b64  = base64.b64encode(logo["bytes"]).decode("ascii")
        fmt_raw   = str(logo.get("format") or "png").lower()
        docx_type = "jpg" if fmt_raw in ("jpeg", "jpg") else fmt_raw
        out_w, out_h = logo_dims(logo.get("width"), logo.get("height"))
        image_run = (
            f'new ImageRun({{ type: "{docx_type}", '
            f'data: Buffer.from("{logo_b64}", "base64"), '
            f'transformation: {{ width: {out_w}, height: {out_h} }} }})'
        )
        paras.append(para([image_run], before=0, after=200))

    paras.append(para(tr(f"{company} — {role}", bold=True, size=30, color=NAVY),
                       before=0, after=40))
    prep_sheet_label = f"Interview Prep Sheet · {candidate_name}" if candidate_name else "Interview Prep Sheet"
    paras.append(para(tr(prep_sheet_label, size=21, color=GRAY),
                       before=0, after=30))

    interviewer_lines = [ln.strip() for ln in (interviewer or "").splitlines() if ln.strip()]
    logistics_parts = []
    if interview_date and interview_date.strip():
        logistics_parts.append(f"Date: {interview_date.strip()}")
    if interview_time and interview_time.strip():
        logistics_parts.append(f"Time: {interview_time.strip()}")
    if location and location.strip():
        logistics_parts.append(f"Location: {location.strip()}")
    logistics_line = " · ".join(logistics_parts)

    header_lines: list[tuple[list[str], dict]] = []
    if interviewer_lines:
        label = "Interviewers:" if len(interviewer_lines) > 1 else "Interviewer:"
        header_lines.append(
            (tr(f"{label} {interviewer_lines[0]}", italic=True, size=19, color=GRAY), {})
        )
        for line in interviewer_lines[1:]:
            header_lines.append(
                (tr(line, italic=True, size=19, color=GRAY), {"left": 200})
            )
    else:
        header_lines.append(
            (tr("Interviewer: [Name] — [Title/Role]", italic=True, size=19, color=GRAY), {})
        )
    if logistics_line:
        header_lines.append((tr(logistics_line, italic=True, size=19, color=GRAY), {}))

    for i, (children, kwargs) in enumerate(header_lines):
        is_last = i == len(header_lines) - 1
        paras.append(para(children, before=0, after=(120 if is_last else 4),
                           border_bottom_color=(NAVY if is_last else ""), **kwargs))

    # =========================================================================
    # 1 — Elevator Pitch
    # =========================================================================
    paras.append(section_header(1, 'Your Elevator Pitch — “Tell Me About Yourself”', TEAL))
    pitch = data.get("elevator_pitch", "")
    if pitch:
        paras.append(para(tr(f"“{pitch}”", size=19), before=40, after=60))
    timing = data.get("pitch_timing", "")
    if timing:
        paras.append(para(tr(timing, italic=True, size=17, color=GRAY), before=0, after=20))
    adapt = data.get("pitch_adapt", "")
    if adapt:
        paras.append(para(tr(adapt, italic=True, size=17, color=GRAY), before=0, after=80))

    # =========================================================================
    # 2 — Role & Company Snapshot
    # =========================================================================
    paras.append(section_header(2, "Role & Company Snapshot", NAVY))
    for key in ("snapshot_role", "snapshot_company", "snapshot_leadership",
                "snapshot_stack", "snapshot_read"):
        val = data.get(key, "")
        if val:
            paras.append(bullet(val))

    # =========================================================================
    # 3 — Story Mapped to Their Priorities
    # =========================================================================
    paras.append(section_header(3, "Your Story, Mapped to Their Priorities", TEAL))
    for pillar in data.get("pillars", []):
        name = pillar.get("name", "")
        if name:
            paras.append(para(tr(name, bold=True, size=19, color=NAVY), before=60, after=25))
        for b in pillar.get("bullets", []):
            paras.append(bullet(b))

    # =========================================================================
    # 4 — Likely Questions + Talking Points
    # =========================================================================
    paras.append(section_header(4, "Likely Questions + Talking Points", NAVY))
    for item in data.get("likely_questions", []):
        q = item.get("question", "")
        a = item.get("answer", "")
        if q:
            paras.append(para(tr(f"“{q}”", bold=True, size=18), before=50, after=10))
        if a:
            paras.append(para(tr(a, size=18), before=0, after=15, left=100))

    # =========================================================================
    # 5 — Smart Questions to Ask Them
    # =========================================================================
    paras.append(section_header(5, "Smart Questions to Ask Them", TEAL))
    for q in data.get("questions_to_ask", []):
        paras.append(bullet(q))

    # =========================================================================
    # 6 — Before the Interview
    # =========================================================================
    paras.append(section_header(6, "Before the Interview", NAVY))
    for item in data.get("before_interview", []):
        paras.append(bullet(item))

    children_js = ",\n      ".join(paras)
    out_path_str = str(output_path).replace("\\", "/")
    MARGIN = 720   # 0.5 inches

    return f"""\
const {{
  Document, Packer, Paragraph, TextRun, BorderStyle, ImageRun, ExternalHyperlink
}} = require('docx');
const fs = require('fs');

const doc = new Document({{
  styles: {{ default: {{ document: {{ run: {{ font: "Arial", size: 19 }} }} }} }},
  sections: [{{
    properties: {{
      page: {{
        size: {{ width: 12240, height: 15840 }},
        margin: {{ top: {MARGIN}, right: {MARGIN}, bottom: {MARGIN}, left: {MARGIN} }}
      }}
    }},
    children: [
      {children_js}
    ]
  }}]
}});

Packer.toBuffer(doc).then(buffer => {{
  fs.writeFileSync('{out_path_str}', buffer);
  console.log('Interview prep document written.');
}});
"""


def generate_interview_prep(
    job_posting: str,
    company: str,
    role: str,
    config: InterviewPrepConfig,
) -> InterviewPrepResult:
    """
    Generate a tailored interview prep DOCX.

    Args:
        job_posting: Full text of the job posting.
        company:     Company name.
        role:        Role title.
        config:      InterviewPrepConfig — round type, focus, model, profile, resume path.

    Returns:
        InterviewPrepResult with the path to the generated DOCX.

    Raises:
        WorkflowError on any unrecoverable error.
    """
    wfc = WorkflowConfig(
        model=config.model,
        progress=config.progress,
        master_resume=config.master_resume,
        profile_text=config.profile_text,
        user_id=config.user_id,
        user_label=config.user_label,
    )

    OUTPUT_DIR.mkdir(exist_ok=True)
    company_safe = safe_filename(company)
    role_safe    = safe_filename(role)
    round_safe   = safe_filename(config.round_type.replace(" ", ""))
    if config.user_id:
        run_dir = OUTPUT_DIR / safe_filename(config.user_id) / f"{company_safe}_{role_safe}"
    else:
        run_dir = OUTPUT_DIR / f"{company_safe}_{role_safe}"
    run_dir.mkdir(parents=True, exist_ok=True)

    # Best-effort candidate name, read once from their own resume — used for
    # both the output filename (sanitized) and the doc header (as-is). This
    # is a lower-stakes, working personal reference than the cover letter/ATS
    # resume, so a parse miss just falls back to a generic label instead of
    # blocking generation.
    try:
        resume_path_for_identity = Path(config.master_resume) if config.master_resume else MASTER_RESUME
        candidate_name = _parse_styled_resume(resume_path_for_identity).get("name", "")
    except Exception:
        candidate_name = ""
    applicant_name_safe = safe_filename(title_case_name(candidate_name)) or "Applicant"

    prep_out = run_dir / (
        f"InterviewPrep_{applicant_name_safe}_{company_safe}_{role_safe}_{round_safe}.docx"
    )

    config.progress(f"\n\U0001f4cb Interview Prep Generator")
    config.progress(f"   Company : {company}")
    config.progress(f"   Role    : {role}")
    config.progress(f"   Round   : {config.round_type}")
    if config.focus:
        config.progress(f"   Focus   : {config.focus}")

    # Step 1: Read inputs
    print_step(1, "Reading Inputs", wfc)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise WorkflowError("ANTHROPIC_API_KEY environment variable not set")
    resume_text = extract_resume_text(wfc)
    profile     = wfc.profile_text if wfc.profile_text is not None else read_file(PROFILE_FILE)
    config.progress(
        f"  ✓ Inputs loaded "
        f"({len(resume_text)} chars resume, {len(profile)} chars profile)"
    )

    # Step 2: Generate content with Claude
    print_step(2, "Generating Interview Prep Content", wfc)

    focus_note   = config.focus or "General — cover the most likely topics for this round type"
    interviewer  = config.interviewer or "Hiring Team"

    prompt = f"""
Job Posting:
---
{job_posting}
---

Candidate Resume:
---
{resume_text[:6000]}
---

Profile & Voice Guide:
---
{profile}
---

Company: {company}
Role: {role}
Interviewer: {interviewer}
Interview Round: {config.round_type}
Focus / Slant: {focus_note}

This is a working document the candidate will skim right before the call — concise,
not comprehensive. It must fit in 2 pages max. WORD LIMITS ARE HARD CONSTRAINTS.
Count the words. Do not exceed them. A long answer is a wrong answer.

If the candidate's resume or profile guide mentions personal/side projects (e.g. a
Projects section, GitHub links, apps they built on their own time), treat those as
legitimate additional proof points alongside their work history — but only ones
actually present in the resume or profile; never invent a project.

Produce a JSON object with EXACTLY these keys (no extras, no omissions):
{{
  "elevator_pitch": "string — 130 to 150 WORDS. A single first-person, quotable paragraph (~40-45 seconds spoken). Structure: background/years of experience -> most relevant recent role -> one flagship, quantifiable accomplishment -> a second distinct accomplishment or project (never merge two different projects into one example — if a build and a metric/outcome came from DIFFERENT initiatives, name them separately) -> outside-of-work projects if relevant -> close with why this specific role. No bullet points, one flowing paragraph.",
  "pitch_timing": "string — MAX 20 WORDS. Suggested delivery timing plus a reminder to practice out loud rather than read it verbatim.",
  "pitch_adapt": "string — MAX 25 WORDS. One line on how to adapt the closing line live if the interviewer already named a specific product, pillar, or priority.",
  "snapshot_role": "string — MAX 20 WORDS. Starts with \\"Role:\\" — title, location, comp if known from the posting.",
  "snapshot_company": "string — MAX 35 WORDS. Starts with \\"Company:\\" — size and funding status stated PLAINLY (bootstrapped/no funding found vs. VC-backed — this changes pace/risk calibration). Name the company's own stated pillars/priorities if any.",
  "snapshot_leadership": "string — MAX 25 WORDS. Starts with \\"Leadership:\\" — named leaders and titles if known, otherwise say plainly that none were found.",
  "snapshot_stack": "string — MAX 25 WORDS. Starts with \\"Stack:\\" — named tools/platforms from the posting or known about the company.",
  "snapshot_read": "string — MAX 30 WORDS. One line of \\"how to read this company\\" synthesis — what the size/stage/funding combination actually means for how the candidate should show up.",
  "pillars": [
    {{
      "name": "string — pulled from the company's OWN stated pillar/priority/value language in the posting, not an invented generic category",
      "bullets": [
        "string — MAX 30 WORDS. ONE accomplishment, correctly attributed to the specific project/system it came from. Never combine two different initiatives into a single bullet even if thematically similar."
      ]
    }}
  ],
  "likely_questions": [
    {{
      "question": "string — MAX 20 WORDS.",
      "answer": "string — MAX 45 WORDS. Names the specific project/example by name and follows problem -> action -> outcome."
    }}
  ],
  "questions_to_ask": [
    "string — MAX 25 WORDS. Specific to this company's actual model, stage, and product — not generic culture-fit filler. Prioritize questions whose answers would change whether the candidate accepts an offer."
  ],
  "before_interview": [
    "string — MAX 20 WORDS. One concrete prep action — what to pull up/rehearse, what to re-skim, or a due-diligence question worth having ready."
  ]
}}

Constraints:
- elevator_pitch: 130–150 words exactly; calibrated to the round type — include technical depth for Peer/Technical/Hiring Manager, keep higher-level for Phone Screen/Executive
- pillars: 2-4 groupings, matching the number of pillars/priorities the company itself states — do not invent categories the posting doesn't support
- pillars[].bullets: 2-5 bullets each, one accomplishment per bullet
- likely_questions: 4-6 items weighted toward {config.round_type} and focus: {focus_note}. The LAST item must be the single hardest/most probing question this specific transition invites (seniority mismatch, company-size change, industry switch) — its answer should be honest about the trade-off, not oversold.
- questions_to_ask: 4-6 items calibrated for {interviewer} at {config.round_type} level
- before_interview: 3-5 items

Round-specific guidance for "{config.round_type}":
- Phone Screen: culture fit, career motivation, logistics, high-level experience. QTA: team structure, 90-day success, next steps.
- Hiring Manager: role vision, leadership alignment, team dynamics, growth. QTA: biggest current challenges, how success is measured, what the team needs now.
- Peer: collaboration style, day-to-day workflow, technical problem-solving. QTA: team dynamics, tooling, what they wish they'd known before joining.
- Technical: system design, architecture tradeoffs, specific technical depth. QTA: stack decisions, engineering culture, biggest technical challenges.
- Executive: strategic impact, ROI, company direction, big-picture fit. QTA: company priorities, how AI/automation fits the roadmap, 3-year bet.
- Panel: multiple angles — mix role-fit, technical, and cultural questions.

Proof point recency rule:
- Only draw examples from roles/projects dated within the last 10 years, per the
  dates in the candidate's own resume, plus any undated personal/side projects.
- Do NOT reference roles or experience older than 10 years.

No invented facts — if the posting and your own knowledge of {company} don't cover
something (funding, team size, tooling, leadership), say so plainly in that field
rather than guessing.

Return ONLY valid JSON. No preamble, no markdown fences.
"""

    # Retry the call once if nothing usable turns up — see
    # _parse_self_correcting_json for why a plain json.loads() isn't enough.
    last_err: json.JSONDecodeError | ValueError | None = None
    data = None
    raw = ""
    for attempt in range(2):
        raw = claude(PREP_SYSTEM, prompt, max_tokens=16000, config=wfc)
        raw = re.sub(r"^```json\s*", "", raw.strip())
        raw = re.sub(r"\s*```$",     "", raw.strip())

        data, last_err = _parse_self_correcting_json(raw, "elevator_pitch")
        if data is not None:
            break

    if data is None:
        raise WorkflowError(f"Failed to parse prep JSON: {last_err}\n\nRaw:\n{raw[:2000]}")

    config.progress(
        f"  ✓ Generated: {len(data.get('pillars', []))} priority pillars, "
        f"{len(data.get('likely_questions', []))} questions, "
        f"{len(data.get('questions_to_ask', []))} questions to ask"
    )

    # Step 2b: Brand colors + logo
    print_step("2b", "Fetching Brand Colors", wfc)
    colors = get_brand_color(company, domain=config.domain or None)
    logo   = get_brand_logo(company, domain=config.domain or None)

    # Step 3: Build DOCX
    print_step(3, "Building Interview Prep DOCX", wfc)
    js      = _build_prep_docx_js(
        data, company, role, config.interviewer, prep_out, colors,
        candidate_name=candidate_name,
        interview_date=config.interview_date,
        interview_time=config.interview_time,
        location=config.location,
        logo=logo,
    )
    js_path = run_dir / f"interview_prep_gen_{os.urandom(4).hex()}.js"
    write_file(js_path, js)
    result  = run(["node", str(js_path)], check=False, config=wfc)
    js_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise WorkflowError(f"Interview prep JS failed:\n{result.stderr}")
    config.progress(f"  ✓ Interview prep written to {prep_out}")

    # Step 4: Upload to Drive
    print_step(8, "Uploading to Google Drive", wfc)
    folder_url = _upload_single_to_drive(prep_out, f"{company_safe}_{role_safe}", wfc)

    return InterviewPrepResult(
        prep_path=prep_out,
        run_dir=run_dir,
        folder_url=folder_url,
    )


# ---------------------------------------------------------------------------
# Optimize Run — targeted edits to an existing run's documents
# ---------------------------------------------------------------------------

_OPTIMIZE_RESUME_SYSTEM = """\
You are a resume editor making TARGETED edits to an already-tailored resume.
You receive the resume's editable fields as a JSON map of field-id -> current text,
the job description (when available), and the user's instruction.

Rules:
- Only include fields you are actually changing. Leave everything else alone.
- Keep each replacement within roughly 20% of the current text's length — the
  resume must stay a single page.
- The tagline must remain one short line of similar length to the current one.
- Never invent experience, employers, dates, certifications, or numbers that are
  not present in the current resume.
- Preserve the candidate's voice: direct, specific, first-person, no corporate
  filler. No "passion for", "leverage", "synergy", "results-driven".
- AI PATTERN REMOVAL: Before returning, scan every edited field for phrases,
  structures, and wording that sound AI-generated — overly balanced clauses,
  "leveraged X to achieve Y" patterns, suspiciously parallel bullet structures,
  or anything too polished. Rewrite those to feel spontaneous and authentic.

Return ONLY a JSON object with exactly these keys:
{
  "edits": [{"field": "<field id from the map>", "new": "<replacement text>"}],
  "change_summary": "<2-3 sentences describing what changed and why>"
}

No preamble, no markdown fences."""

_OPTIMIZE_COVER_SYSTEM = """\
You are editing an existing cover letter according to the user's instruction.
You receive the current body paragraphs, the job description (when available),
and the instruction.

Rules:
- Return exactly 5 body paragraphs.
- Keep any paragraph the user did not ask about as close to the original as possible.
- Preserve the original voice: first person, direct, no corporate filler. Never
  start a paragraph with "I am excited to...". No "passion for", "leverage",
  "synergy", "results-driven".
- Never invent facts, numbers, or experience not present in the current letter
  or the job description.
- HUMAN REWRITE: Remove anything that sounds overly polished, corporate, or
  written to impress. Make every paragraph direct, natural, and conversational.
  If a sentence sounds like it was written to check a box, rewrite it so it
  sounds like something a real person would actually say.

Return ONLY a JSON object with exactly these keys:
{
  "paragraphs": ["<p1>", "<p2>", "<p3>", "<p4>", "<p5>"],
  "change_summary": "<1-2 sentences describing what changed>"
}

No preamble, no markdown fences."""


AQ_SYSTEM = """\
You are a job application assistant. The candidate is applying for a role and needs
to answer an application question. Write in first person as the candidate. Your job
is to craft an authentic, specific answer grounded in the candidate's actual resume
and profile — never invent experience or numbers.

Tone rules:
- First person, direct, no corporate filler
- Never start with "I am excited to..." or "I am passionate about..."
- No "leverage", "synergy", "results-driven", "passion for"
- Specific > general. Quantified > vague. Honest > impressive-sounding.
- Write like the candidate talks, not like a LinkedIn summary
- AUTHENTICITY CHECK: Go through every sentence before returning. Rewrite anything
  that feels too perfect, too formal, or overly optimized. Make it sound like
  something someone would actually say out loud — not something that was clearly
  crafted by an AI to hit every keyword.

You will be given the candidate's resume, profile/voice guide, and the job description
for context. Use them to tailor the answer to the specific role."""


def generate_app_question_answer(config: AppQuestionConfig) -> AppQuestionResult:
    """Generate an answer to a job application question.

    Phase 1: Assess whether the question can be answered well with available context.
              If not, return clarification questions.
    Phase 2: Generate the answer (called again with clarifications if needed).
    """
    wfc = WorkflowConfig(
        model=config.model,
        progress=config.progress,
        master_resume=config.master_resume,
        profile_text=config.profile_text,
        user_id=config.user_id,
        user_label=config.user_label,
    )

    config.progress("\n\U0001f4dd Application Question Agent")
    config.progress(f"   Company : {config.company}")
    config.progress(f"   Role    : {config.role}")
    config.progress(f"   Tone    : {config.tone}")
    if config.char_limit:
        config.progress(f"   Limit   : {config.char_limit} characters")

    # Read inputs
    config.progress("  Reading inputs…")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise WorkflowError("ANTHROPIC_API_KEY environment variable not set")
    resume_text = extract_resume_text(wfc)
    profile = wfc.profile_text if wfc.profile_text is not None else read_file(PROFILE_FILE)
    config.progress(
        f"  ✓ Inputs loaded "
        f"({len(resume_text)} chars resume, {len(profile)} chars profile)"
    )

    tone_instructions = {
        "professional": "Write in a polished, professional tone — confident but not stiff.",
        "conversational": "Write in a warm, conversational tone — approachable and genuine.",
        "technical": "Write in a precise, technical tone — emphasize depth and specifics.",
        "concise": "Write as concisely as possible — every word must earn its place.",
    }
    tone_note = tone_instructions.get(config.tone, tone_instructions["professional"])

    char_limit_note = ""
    if config.char_limit:
        char_limit_note = (
            f"\n\nIMPORTANT: The answer MUST be {config.char_limit} characters or fewer "
            f"(including spaces). Count carefully. This is a hard limit enforced by the "
            f"application form."
        )

    clarification_context = ""
    if config.clarifications:
        clarification_context = "\n\nThe candidate provided these additional details:\n"
        for q, a in config.clarifications.items():
            clarification_context += f"- Q: {q}\n  A: {a}\n"

    prompt = f"""Job Description:
---
{config.job_posting}
---

Candidate Resume:
---
{resume_text[:6000]}
---

Candidate Profile & Voice Guide:
---
{profile[:4000]}
---

Application Question:
---
{config.question}
---
{clarification_context}
Tone: {tone_note}{char_limit_note}

Instructions:
1. First, assess whether you have enough context from the resume, profile, and job
   description to write a strong, specific answer to this question. Consider:
   - Does the question ask about a specific experience you can find in the resume?
   - Is the question open-ended enough that you need to know which angle to take?
   - Would knowing the candidate's preference help (e.g., which project to highlight)?

2. If you do NOT have enough context, return:
{{
  "needs_clarification": true,
  "clarification_questions": ["<question 1>", "<question 2>"],
  "draft_answer": null,
  "follow_ups": []
}}
   Keep clarification questions to 2-3 max. Be specific about what you need.

3. If you DO have enough context (or clarifications were provided), write the answer and return:
{{
  "needs_clarification": false,
  "clarification_questions": [],
  "draft_answer": "<the complete answer>",
  "follow_ups": ["<optional suggestion 1>", "<optional suggestion 2>"]
}}
   Follow-ups are optional refinement suggestions (e.g., "Want me to emphasize the
   technical leadership angle more?" or "I could swap in your eHealth migration story instead").

Return ONLY a JSON object. No preamble, no markdown fences."""

    if config.clarifications:
        config.progress("  Generating answer with your clarifications…")
    else:
        config.progress("  Analyzing question…")

    raw = claude(AQ_SYSTEM, prompt, max_tokens=12000, config=wfc)
    data = _parse_claude_json(raw)

    needs_clarification = data.get("needs_clarification", False)
    answer = data.get("draft_answer") or ""
    clarification_questions = data.get("clarification_questions", [])
    follow_ups = data.get("follow_ups", [])

    if needs_clarification and not config.clarifications:
        config.progress("  ❓ Need more context — asking follow-up questions")
        return AppQuestionResult(
            answer="",
            char_count=0,
            follow_ups=[],
            needs_clarification=True,
            clarification_questions=clarification_questions,
        )

    # Enforce character limit with a trim pass if needed
    if config.char_limit and len(answer) > config.char_limit:
        config.progress(
            f"  ✂ Answer is {len(answer)} chars, trimming to {config.char_limit}…"
        )
        trim_prompt = (
            f"The following answer must be shortened to EXACTLY {config.char_limit} "
            f"characters or fewer (currently {len(answer)} chars). Preserve the key "
            f"points and tone. Return ONLY the shortened text, nothing else.\n\n{answer}"
        )
        answer = claude(
            "You shorten text to fit character limits. Return only the shortened text.",
            trim_prompt,
            max_tokens=4096,
            config=wfc,
        ).strip()

    char_count = len(answer)
    config.progress(f"  ✓ Answer generated ({char_count} characters)")

    return AppQuestionResult(
        answer=answer,
        char_count=char_count,
        follow_ups=follow_ups,
    )


THANKYOU_SYSTEM = """\
You are a job application assistant helping the candidate write a post-interview
thank-you email. Write in first person as the candidate. The email should feel
genuine and specific to the conversation that just happened — not a template.

Tone rules:
- First person, direct, no corporate filler
- Never start with "I wanted to reach out to express my gratitude"
- No "passion for", "leverage", "synergy", "results-driven"
- Reference specific topics from the interview — the reader should be able to
  tell this email was written after THIS conversation, not any conversation
- Keep it short: 3-4 paragraphs max, under 200 words total
- Close with something forward-looking but not pushy

VOICE: Write with a stronger point of view. Let personality come through. Make it
sound like one real person writing a quick, genuine note — not a carefully crafted
follow-up optimized to hit every keyword. Vary sentence length. If a sentence
sounds like it could appear in any candidate's thank-you email, rewrite it until
it couldn't. Remove anything that sounds overly polished or written to impress."""


def generate_thank_you_email(config: ThankYouConfig) -> ThankYouResult:
    """Generate a post-interview thank-you email."""
    wfc = WorkflowConfig(
        model=config.model,
        progress=config.progress,
        master_resume=config.master_resume,
        profile_text=config.profile_text,
        user_id=config.user_id,
        user_label=config.user_label,
    )

    config.progress("\n\U0001f4e7 Thank You Email Agent")
    config.progress(f"   Company     : {config.company}")
    config.progress(f"   Role        : {config.role}")
    config.progress(f"   Round       : {config.round_type}")
    if config.interviewer:
        config.progress(f"   Interviewer : {config.interviewer}")

    config.progress("  Reading inputs…")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise WorkflowError("ANTHROPIC_API_KEY environment variable not set")
    resume_text = extract_resume_text(wfc)
    profile = wfc.profile_text if wfc.profile_text is not None else read_file(PROFILE_FILE)
    config.progress(
        f"  ✓ Inputs loaded "
        f"({len(resume_text)} chars resume, {len(profile)} chars profile)"
    )

    tone_instructions = {
        "professional": "Write in a polished, professional tone — confident but not stiff.",
        "conversational": "Write in a warm, conversational tone — approachable and genuine.",
        "concise": "Write as concisely as possible — every word must earn its place.",
    }
    tone_note = tone_instructions.get(config.tone, tone_instructions["professional"])

    interviewer_note = ""
    if config.interviewer:
        interviewer_note = f"\nInterviewer name(s): {config.interviewer}"

    topics_note = ""
    if config.topics:
        topics_note = f"\n\nKey topics discussed in the interview:\n{config.topics}"

    prompt = f"""Job Description:
---
{config.job_posting[:6000]}
---

Candidate Resume:
---
{resume_text[:6000]}
---

Candidate Profile & Voice Guide:
---
{profile[:4000]}
---

Interview Round: {config.round_type}{interviewer_note}{topics_note}

Tone: {tone_note}

Write a thank-you email for the interview. Return a JSON object:
{{
  "subject": "<email subject line>",
  "email_body": "<the full email body including greeting and sign-off>"
}}

Return ONLY valid JSON. No preamble, no markdown fences."""

    config.progress("  Generating thank-you email…")
    raw = claude(THANKYOU_SYSTEM, prompt, max_tokens=8000, config=wfc)
    data = _parse_claude_json(raw)

    subject = data.get("subject", f"Thank you — {config.role} interview")
    email_body = data.get("email_body", "")
    if not email_body:
        raise WorkflowError("Claude returned an empty email body")

    config.progress(f"  ✓ Email generated ({len(email_body)} chars)")

    # Build output directory
    company_safe = re.sub(r"[^A-Za-z0-9]+", "", config.company)
    role_safe = re.sub(r"[^A-Za-z0-9]+", "", config.role)
    base_dir = Path("output")
    if config.user_id:
        base_dir = base_dir / config.user_id
    run_dir = base_dir / f"{company_safe}_{role_safe}"
    run_dir.mkdir(parents=True, exist_ok=True)

    # Save as DOCX
    config.progress("  Generating DOCX…")
    applicant_name_safe = _applicant_name_for_filenames(config.master_resume)
    docx_name = f"ThankYou_{applicant_name_safe}_{company_safe}_{role_safe}.docx"
    docx_path = run_dir / docx_name

    js_script = _build_thankyou_docx_js(
        subject=subject,
        email_body=email_body,
        company=config.company,
        role=config.role,
        round_type=config.round_type,
        interviewer=config.interviewer,
        output_path=docx_path,
    )
    script_path = run_dir / f"thankyou_gen_{os.urandom(4).hex()}.js"
    script_path.write_text(js_script, encoding="utf-8")
    run(["node", str(script_path)], config=wfc)
    script_path.unlink(missing_ok=True)
    config.progress(f"  ✓ Saved {docx_path.name}")

    # Upload to Drive
    folder_url = None
    try:
        folder_url = step8_upload(run_dir, company_safe, role_safe, wfc)
    except Exception as exc:
        config.progress(f"  ⚠ Drive upload failed: {exc}")

    return ThankYouResult(
        email_text=email_body,
        subject=subject,
        run_dir=run_dir,
        docx_path=docx_path,
        folder_url=folder_url,
    )


def _build_thankyou_docx_js(
    subject: str,
    email_body: str,
    company: str,
    role: str,
    round_type: str,
    interviewer: str,
    output_path: Path,
) -> str:
    """Return a Node.js script that produces the thank-you email DOCX."""
    def esc(text: str) -> str:
        return escape_js_string(" ".join(str(text).split()))

    paragraphs_js = []
    for para in email_body.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        paragraphs_js.append(
            f'    new Paragraph({{\n'
            f'      spacing: {{ after: 160 }},\n'
            f'      children: [new TextRun({{ text: "{esc(para)}", font: "Calibri", size: 22, color: "111827" }})]\n'
            f'    }}),'
        )

    body_paragraphs = "\n".join(paragraphs_js)

    return f"""const {{ Document, Packer, Paragraph, TextRun, HeadingLevel, BorderStyle }} = require("docx");
const fs = require("fs");

(async () => {{
  const doc = new Document({{
    sections: [{{
      properties: {{ page: {{ margin: {{ top: 720, bottom: 720, left: 1080, right: 1080 }} }} }},
      children: [
    new Paragraph({{
      spacing: {{ after: 40 }},
      children: [new TextRun({{ text: "Subject: {esc(subject)}", font: "Calibri", size: 22, bold: true, color: "1A3C5E" }})]
    }}),
    new Paragraph({{
      spacing: {{ after: 300 }},
      border: {{ bottom: {{ style: BorderStyle.SINGLE, size: 1, color: "D1D5DB" }} }},
      children: [new TextRun({{ text: "{esc(company)} · {esc(role)} · {esc(round_type)}", font: "Calibri", size: 18, color: "6B7280" }})]
    }}),
{body_paragraphs}
      ],
    }}],
  }});
  const buf = await Packer.toBuffer(doc);
  fs.writeFileSync("{str(output_path).replace(chr(92), '/')}", buf);
}})();"""


def _parse_claude_json(raw: str) -> dict:
    """Strip optional markdown fences and parse Claude's JSON response."""
    raw = re.sub(r"^```json\s*", "", raw.strip())
    raw = re.sub(r"\s*```$",     "", raw.strip())
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        try:
            return json.loads(_repair_unescaped_quotes(raw))
        except json.JSONDecodeError:
            raise WorkflowError(f"Failed to parse Claude JSON: {e}\n\nRaw:\n{raw[:2000]}")


def _build_resume_field_map(data: dict) -> dict[str, str]:
    """Flatten parse_xml() output into editable field-id -> current-text pairs."""
    fields: dict[str, str] = {}
    if data.get("tagline"):
        fields["tagline"] = data["tagline"]
    for i, item in enumerate(data.get("summary_items", []), start=1):
        fields[f"summary_{i}"] = item["text"]
    for i, comp in enumerate(data.get("competencies", []), start=1):
        fields[f"competency_{i}"] = comp
    for j, job in enumerate(data.get("jobs", []), start=1):
        if job.get("title"):
            fields[f"job{j}_title"] = job["title"]
        for k, bullet in enumerate(job.get("bullets", []), start=1):
            fields[f"job{j}_bullet{k}"] = bullet
    return fields


def _entity_safe_prefix(escaped: str, limit: int = 60) -> str:
    """Truncate entity-escaped text without cutting through an entity."""
    prefix = escaped[:limit]
    amp = prefix.rfind("&")
    if amp != -1 and ";" not in prefix[amp:]:
        prefix = prefix[:amp]
    return prefix


def _apply_optimize_edits(
    xml: str,
    edits: list[dict],
    field_map: dict[str, str],
    progress: Callable[[str], None],
) -> tuple[str, int, int, int]:
    """Apply Claude's field-level edits to the raw document XML.

    Claude only names fields and replacement text — the `old` search string is
    always derived here from the field's current text (entity-escaped, with
    page-break splits handled by _extract_xml_field), never guessed by Claude.

    A field where Claude's proposed text is identical to what's already there
    is a deliberate no-op (that field already fit), not a matching failure —
    it's tracked separately in `unchanged` so callers can exclude it from
    their success-ratio math instead of it dragging down a fully-successful
    run's percentage and tripping a false "fewer than 70%" warning.

    Returns (xml, succeeded, attempted, unchanged).
    """
    succeeded = 0
    attempted = 0
    unchanged = 0

    for edit in edits:
        field = (edit.get("field") or "").strip()
        new   = " ".join((edit.get("new") or "").split())
        attempted += 1

        cur = field_map.get(field)
        if not cur or not new:
            progress(f"  ✗ Skipped: unknown field or empty replacement ({field!r})")
            continue
        if new == cur:
            unchanged += 1
            progress(f"  – {field}: unchanged, skipping")
            continue
        if field == "tagline" and not tagline_fits(new):
            progress(f"  ✗ Skipped tagline edit: replacement does not fit on one line")
            continue

        escaped = _xml_escape(cur)
        # Try the full text first, then progressively shorter prefixes — a
        # page-break split can land anywhere, so shorter prefixes catch fields
        # whose first <w:t> segment is short. 32 chars is the floor to keep
        # prefixes unique within the document.
        old = None
        for limit in (len(escaped), 60, 32):
            old = _extract_xml_field(xml, _entity_safe_prefix(escaped, limit))
            if old is not None:
                break
        if old is None and xml.count(escaped) == 1:
            old = escaped

        if not old or old not in xml:
            progress(f"  ✗ NOT FOUND in document XML: {field} ({cur[:60]!r}...)")
            continue

        xml = xml.replace(old, _xml_escape(new), 1)
        succeeded += 1
        progress(f"  ✓ {field}: {new[:70]!r}...")

    return xml, succeeded, attempted, unchanged


def _parse_cover_letter_text(plain: str) -> dict:
    """Parse pandoc-plain output of a generated cover letter into its parts.

    The letter layout is fixed by COVER_LETTER_JS_TEMPLATE: name, contact bar,
    date, addressee, company, "Re:" line, "Dear ...", body paragraphs,
    "Sincerely,", signature, contact. Returns {contact_name, paragraphs}.
    """
    blocks = [" ".join(b.split()) for b in re.split(r"\n\s*\n", plain.strip())]
    blocks = [b for b in blocks if b]

    dear_idx = next((i for i, b in enumerate(blocks) if b.startswith("Dear ")), None)
    if dear_idx is None:
        raise WorkflowError("Could not parse cover letter: no 'Dear ...' salutation found")
    sinc_idx = next(
        (i for i in range(dear_idx + 1, len(blocks)) if blocks[i].startswith("Sincerely")),
        None,
    )
    if sinc_idx is None:
        raise WorkflowError("Could not parse cover letter: no 'Sincerely,' sign-off found")

    paragraphs = blocks[dear_idx + 1:sinc_idx]
    if not 1 <= len(paragraphs) <= 8:
        raise WorkflowError(
            f"Could not parse cover letter: expected 1-8 body paragraphs, found {len(paragraphs)}"
        )

    re_idx = next((i for i, b in enumerate(blocks) if b.startswith("Re: ")), None)
    contact_name = blocks[re_idx - 2] if re_idx is not None and re_idx >= 2 else "Hiring Team"

    return {"contact_name": contact_name, "paragraphs": paragraphs}


def optimize_run(config: OptimizeConfig) -> OptimizeResult:
    """Optimize an existing run's documents in place, per a user instruction.

    Downloads the tailored resume and/or cover letter from the run's Drive
    folder, applies targeted Claude-driven edits, and overwrites the Drive
    files (same names, same file IDs). The ATS resume is regenerated from the
    optimized styled resume whenever the resume is edited.

    Raises WorkflowError on any unrecoverable error — nothing is uploaded
    unless the edit + validation pipeline for that document succeeded.
    """
    wfc = WorkflowConfig(
        model=config.model,
        progress=config.progress,
        user_id=config.user_id,
        user_label=config.user_label,
    )

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise WorkflowError("ANTHROPIC_API_KEY environment variable not set")
    if not config.optimize_resume and not config.optimize_cover_letter:
        raise WorkflowError("Nothing to optimize — select the resume and/or the cover letter")

    config.progress(f"\n\U0001f527 Optimize Run")
    config.progress(f"   Company : {config.company}")
    config.progress(f"   Role    : {config.role}")
    config.progress(f"   Ask     : {config.instruction[:200]}")

    # ── Step 1: connect to Drive and inventory the run folder ───────────
    print_step(1, "Reading the Run Folder", wfc)
    service = _gdrive_service(wfc)
    if service is None:
        raise WorkflowError("Google Drive is not configured — cannot optimize an existing run")

    try:
        meta = service.files().get(
            fileId=config.folder_id, fields="name, webViewLink",
        ).execute()
    except Exception as exc:
        raise WorkflowError(f"Could not access the Drive run folder: {exc}")
    folder_name = meta.get("name", "run")
    folder_url  = meta.get("webViewLink")
    config.progress(f"  ✓ Drive folder: {folder_name}")

    company_safe = safe_filename(config.company)
    role_safe    = safe_filename(config.role)

    files = _gdrive_list_files(service, config.folder_id)
    styled = next(
        (f for f in files
         if re.match(r"^Resume_.*\.docx$", f["name"]) and not f["name"].endswith("_ATS.docx")),
        None,
    )
    ats   = next((f for f in files if f["name"].endswith("_ATS.docx")), None)
    cover = next((f for f in files if re.match(r"^CoverLetter_.*\.docx$", f["name"])), None)

    if config.optimize_resume and styled is None:
        raise WorkflowError(
            f"No tailored resume (Resume_*.docx) found in Drive folder '{folder_name}'"
        )
    if config.optimize_cover_letter and cover is None:
        raise WorkflowError(
            f"No cover letter (CoverLetter_*.docx) found in Drive folder '{folder_name}'"
        )

    jd = get_gdrive_job_posting(config.folder_id, wfc) or ""
    config.progress(
        f"  ✓ Job description: {'found (' + str(len(jd)) + ' chars)' if jd else 'not found — optimizing without it'}"
    )

    OUTPUT_DIR.mkdir(exist_ok=True)
    if config.user_id:
        run_dir = OUTPUT_DIR / safe_filename(config.user_id) / safe_filename(folder_name)
    else:
        run_dir = OUTPUT_DIR / safe_filename(folder_name)
    run_dir.mkdir(parents=True, exist_ok=True)

    src_dir    = run_dir / f"_optimize_src_{os.urandom(4).hex()}"
    unpack_dir = run_dir / f"unpacked_opt_{os.urandom(4).hex()}"
    src_dir.mkdir()

    result = OptimizeResult(run_dir=run_dir, folder_url=folder_url)
    summaries: list[str] = []
    jd_block = f"Job Description:\n---\n{jd[:6000]}\n---\n\n" if jd else ""
    applicant_name: str | None = None
    applicant_contact_line = ""

    try:
        # ── Step 2: resume ───────────────────────────────────────────────
        if config.optimize_resume:
            print_step(2, "Optimizing Resume", wfc)
            src_docx = src_dir / styled["name"]
            src_docx.write_bytes(_gdrive_download_file(service, styled["id"]))
            config.progress(f"  ✓ Downloaded {styled['name']}")

            data      = _parse_styled_resume(src_docx)
            field_map = _build_resume_field_map(data)
            jobs_legend = "\n".join(
                f"  job{j}: {job.get('company', '?')} ({job.get('dates', '')})"
                for j, job in enumerate(data.get("jobs", []), start=1)
            )

            raw = claude(
                _OPTIMIZE_RESUME_SYSTEM,
                f"{jd_block}"
                f"User instruction:\n---\n{config.instruction}\n---\n\n"
                f"Jobs legend (read-only context for the field ids):\n{jobs_legend}\n\n"
                f"Editable fields (field id -> current text):\n"
                f"{json.dumps(field_map, indent=2)}",
                max_tokens=16000,
                config=wfc,
            )
            parsed = _parse_claude_json(raw)
            edits  = parsed.get("edits", [])
            if not edits:
                raise WorkflowError(
                    "Claude proposed no resume edits for this instruction — "
                    "try a more specific prompt"
                )
            if parsed.get("change_summary"):
                summaries.append(f"Resume: {parsed['change_summary']}")

            run(
                ["python3", str(SCRIPTS_DIR / "unpack.py"), str(src_docx), str(unpack_dir) + "/"],
                config=wfc,
            )
            xml_path = unpack_dir / "word" / "document.xml"
            xml = xml_path.read_text(encoding="utf-8")

            xml, ok, total, unchanged = _apply_optimize_edits(xml, edits, field_map, config.progress)
            # Fields Claude deliberately left unchanged (already matched the
            # instruction) aren't failures — exclude them from the ratio.
            real_total = total - unchanged
            unchanged_note = f" ({unchanged} already matched — left unchanged)" if unchanged else ""
            config.progress(f"\n  Result: {ok}/{real_total} replacements succeeded{unchanged_note}")
            if ok == 0 and real_total > 0:
                raise WorkflowError(
                    "None of the proposed edits matched the document — "
                    "nothing was changed in Drive"
                )
            if real_total > 0 and ok < real_total:
                result.replacements_warning = (
                    f"Only {ok}/{real_total} resume edits could be applied — "
                    "some requested changes may be missing."
                )
            xml_path.write_text(xml, encoding="utf-8")

            out_path = run_dir / styled["name"]
            run(
                ["python3", str(SCRIPTS_DIR / "pack.py"), str(unpack_dir) + "/",
                 str(out_path), "--original", str(src_docx)],
                config=wfc,
            )
            config.progress(f"  ✓ Optimized resume written to {out_path.name}")

            applicant_name, applicant_contact_line = _identity_from_resume(out_path, wfc)

            # Re-derive the canonical filename from the resume's own name
            # header rather than reusing styled["name"] verbatim — a Drive
            # folder created before title_case_name() existed would otherwise
            # keep regenerating an ALL-CAPS filename forever, since Optimize
            # runs are the only touchpoint most applications get after the
            # initial Run. Renaming (not just re-uploading under the old
            # name) keeps the file ID stable, so this self-heals in place.
            applicant_name_safe = safe_filename(title_case_name(applicant_name))
            canonical_resume_name = f"Resume_{applicant_name_safe}_{company_safe}_{role_safe}.docx"
            if out_path.name != canonical_resume_name:
                renamed = run_dir / canonical_resume_name
                out_path.replace(renamed)
                out_path = renamed

            _gdrive_upsert_file_by_pattern(
                service, config.folder_id, r"^Resume_(?!.*_ATS\.docx$).*\.docx$",
                out_path.name, out_path,
            )
            config.progress(f"  ✓ Updated {out_path.name} in Drive")
            result.resume_path = out_path

            # Keep the Drive PDF in sync (best-effort, like step8_upload)
            pdf_name = out_path.stem + ".pdf"
            _gdrive_delete_by_pattern(service, config.folder_id, r"^Resume_.*\.pdf$")
            _convert_docx_to_pdf_via_drive(
                service, out_path, pdf_name, config.folder_id, config.progress,
            )

            # ── Step 3: regenerate the ATS resume from the optimized resume
            print_step(3, "Regenerating ATS Resume", wfc)
            ats_name = out_path.stem + "_ATS.docx"
            ats_path = run_dir / ats_name
            try:
                _build_ats_resume(_parse_styled_resume(out_path), ats_path)
            except RuntimeError as exc:
                raise WorkflowError(str(exc))
            _gdrive_upsert_file_by_pattern(
                service, config.folder_id, r"^Resume_.*_ATS\.docx$", ats_name, ats_path,
            )
            config.progress(f"  ✓ Updated {ats_name} in Drive")
            result.ats_path = ats_path

        # ── Step 4: cover letter ─────────────────────────────────────────
        if config.optimize_cover_letter:
            print_step(4, "Optimizing Cover Letter", wfc)

            # Resume wasn't touched this run — read identity from the
            # existing styled resume in Drive instead of re-deriving it.
            if applicant_name is None:
                if styled is None:
                    raise WorkflowError(
                        f"No tailored resume (Resume_*.docx) found in Drive folder "
                        f"'{folder_name}' — cannot determine the applicant's identity "
                        f"for the cover letter"
                    )
                identity_src = src_dir / styled["name"]
                if not identity_src.exists():
                    identity_src.write_bytes(_gdrive_download_file(service, styled["id"]))
                applicant_name, applicant_contact_line = _identity_from_resume(identity_src, wfc)

            src_cover = src_dir / cover["name"]
            src_cover.write_bytes(_gdrive_download_file(service, cover["id"]))
            config.progress(f"  ✓ Downloaded {cover['name']}")

            plain  = run(["pandoc", str(src_cover), "-t", "plain"], config=wfc).stdout
            letter = _parse_cover_letter_text(plain)

            current = "\n\n".join(
                f"Paragraph {i}: {p}" for i, p in enumerate(letter["paragraphs"], start=1)
            )
            raw = claude(
                _OPTIMIZE_COVER_SYSTEM,
                f"{jd_block}"
                f"Company: {config.company}\nRole: {config.role}\n\n"
                f"User instruction:\n---\n{config.instruction}\n---\n\n"
                f"Current cover letter body paragraphs:\n---\n{current}\n---",
                max_tokens=8000,
                config=wfc,
            )
            parsed     = _parse_claude_json(raw)
            paragraphs = parsed.get("paragraphs", [])
            if len(paragraphs) != 5 or not all(isinstance(p, str) and p.strip() for p in paragraphs):
                raise WorkflowError(
                    f"Cover letter rewrite returned {len(paragraphs)} paragraphs (expected 5)"
                )
            if parsed.get("change_summary"):
                summaries.append(f"Cover letter: {parsed['change_summary']}")

            analysis = {"contact_name": letter["contact_name"]}
            for i, p in enumerate(paragraphs, start=1):
                analysis[f"cover_letter_p{i}"] = " ".join(p.split())

            # See the resume branch above for why this is re-derived rather
            # than reusing cover["name"] verbatim.
            applicant_name_safe = safe_filename(title_case_name(applicant_name))
            cover_name = f"CoverLetter_{applicant_name_safe}_{company_safe}_{role_safe}.docx"
            cover_out = run_dir / cover_name
            step6_cover_letter(
                analysis, config.company, config.role, cover_out, wfc,
                applicant_name=applicant_name, applicant_contact_line=applicant_contact_line,
                colors=get_brand_color(config.company, domain=config.domain or None),
            )
            _gdrive_upsert_file_by_pattern(
                service, config.folder_id, r"^CoverLetter_.*\.docx$", cover_name, cover_out,
            )
            config.progress(f"  ✓ Updated {cover_name} in Drive")
            result.cover_letter_path = cover_out

    finally:
        shutil.rmtree(src_dir, ignore_errors=True)
        shutil.rmtree(unpack_dir, ignore_errors=True)

    result.change_summary = " ".join(summaries)
    return result


def _print_result(result: WorkflowResult):
    print(f"\n{'='*60}")
    print(f"  DONE")
    print(f"{'='*60}")
    print(f"\n  \U0001f4c1 Output folder : {result.run_dir}")
    print(f"  \U0001f4c4 Resume (DOCX) : {result.resume_path.name}")
    print(f"  \U0001f916 ATS Resume    : {result.ats_path.name}")
    print(f"  \U0001f4dd Cover letter  : {result.cover_letter_path.name}")
    if result.folder_url:
        print(f"  ☁️  Drive folder  : {result.folder_url}")
    print(f"\n  Framing angle used:")
    print(f"  {textwrap.fill(result.framing_angle, width=56, initial_indent='  ', subsequent_indent='  ')}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Job Application Agent - Corey Laverdiere")
    parser.add_argument("--job",      required=True,        help="Path to job posting text file")
    parser.add_argument("--company",  required=True,        help="Company name (used in filenames)")
    parser.add_argument("--role",     required=True,        help="Role title (used in filenames and cover letter)")
    parser.add_argument("--contact",  default=None,         help="Hiring manager name if known")
    parser.add_argument("--model",    default=DEFAULT_MODEL, help=f"Anthropic model (default: {DEFAULT_MODEL})")
    parser.add_argument("--debug",    action="store_true",  help="Keep unpacked/ and gen scripts for inspection")
    parser.add_argument("--dry-run",  action="store_true",  help="Run analysis only; skip file generation")
    args = parser.parse_args()

    job_path = Path(args.job)
    if not job_path.exists():
        print(f"❌ Job file not found at {job_path}")
        sys.exit(1)

    config = WorkflowConfig(
        model=args.model,
        debug=args.debug,
        dry_run=args.dry_run,
    )

    try:
        result = run_workflow(
            job_posting=job_path.read_text(encoding="utf-8"),
            company=args.company,
            role=args.role,
            contact=args.contact,
            config=config,
        )
        _print_result(result)
    except WorkflowError as e:
        print(f"\n❌ {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
