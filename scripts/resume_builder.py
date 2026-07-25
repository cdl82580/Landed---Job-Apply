"""
scripts/resume_builder.py — build a brand-new master resume DOCX from
structured Q&A wizard data (for users who don't have a master resume yet).

Unlike the rest of the agent (which edits an *existing* resumes/master.docx
via unpack/pack XML string replacement), this composes a document from
scratch, so it follows the other from-scratch-DOCX precedent already used in
this repo: build a JS snippet using the `docx` npm package, write it to a
temp .js file, run it with `node`, read back the bytes. See
apply.py:step5b_ats_resume / step6_cover_letter for the same technique.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from apply import WorkflowConfig, WorkflowError, claude, escape_js_string

# ---------------------------------------------------------------------------
# Data shape shared between polish_resume_data() and render_resume()
# ---------------------------------------------------------------------------
#
# {
#   "full_name": str, "email": str, "phone": str, "location": str,
#   "linkedin": str, "website": str, "summary": str, "skills": [str, ...],
#   "jobs": [
#     {"company": str, "title": str, "location": str, "start": str,
#      "end": str, "current": bool, "bullets": [str, ...]}, ...
#   ],
#   "education": [
#     {"degree": str, "institution": str, "location": str,
#      "graduated": str}, ...
#   ],
#   "certs": [{"name": str, "issuer": str, "year": str}, ...],
# }

TEMPLATES = ("technical", "traditional", "executive")

_POLISH_SYSTEM_PROMPT = """\
You are a resume editor. You will be given a JSON object describing a
person's work history, education, and skills, typed by them in rough form.

Rewrite it into polished, resume-quality text:
- Tighten wording, fix grammar, use strong active verbs (built, led, shipped,
  reduced, designed) instead of weak/passive phrasing.
- Never start a bullet with "Responsible for" or "Helped to"/"Assisted with".
- Keep each bullet roughly the same length as the input — don't pad.
- If "summary" is empty, write a 2-3 sentence professional summary using
  ONLY facts already present in the job entries (titles, companies, skills).

CRITICAL — do not invent anything:
- Never add a company name, job title, number, metric, tool, technology, or
  achievement that is not already present in the input.
- Never change dates, company names, or job titles.
- If a bullet has no quantifiable result in the input, do not add one.
- If you are unsure whether something is safe to add, leave it out.

Return ONLY the same JSON shape you were given (same keys, same array
lengths and ordering), with polished text in place of the rough text. No
markdown fences, no commentary, no extra keys.
"""


def _parse_json_response(raw: str) -> dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned.strip())
    return json.loads(cleaned)


def polish_resume_data(data: dict[str, Any], model: str, progress) -> dict[str, Any]:
    """One Claude call to tighten wording without inventing facts.

    Falls back to the original, unpolished data on any failure — a
    malformed AI response should never block resume generation.
    """
    config = WorkflowConfig(model=model, progress=progress)
    try:
        raw = claude(
            system=_POLISH_SYSTEM_PROMPT,
            user=json.dumps(data),
            max_tokens=8192,
            config=config,
        )
        polished = _parse_json_response(raw)
        if not isinstance(polished, dict) or "jobs" not in polished:
            raise ValueError("polished response missing expected shape")
        return polished
    except Exception as exc:
        progress(f"  ⚠ AI polish failed ({exc}) — using your original text instead")
        return data


# ---------------------------------------------------------------------------
# Shared JS-snippet builders (mirrors apply.py:step5b_ats_resume's approach)
# ---------------------------------------------------------------------------

def _contact_line(data: dict[str, Any]) -> str:
    parts = [
        data.get("phone", "").strip(),
        data.get("email", "").strip(),
        data.get("location", "").strip(),
        data.get("linkedin", "").strip(),
        data.get("website", "").strip(),
    ]
    return "  |  ".join(p for p in parts if p)


def _date_range(job: dict[str, Any]) -> str:
    start = job.get("start", "").strip()
    end = "Present" if job.get("current") else job.get("end", "").strip()
    if start and end:
        return f"{start} – {end}"
    return start or end


class _Style:
    """Per-template visual settings."""

    def __init__(self, *, name_size, name_color, name_align, header_border,
                 heading_color, heading_border_color, accent, competency_table,
                 page_margin=720):
        self.name_size = name_size
        self.name_color = name_color
        self.name_align = name_align  # "left" | "center"
        self.header_border = header_border
        self.heading_color = heading_color
        self.heading_border_color = heading_border_color
        self.accent = accent
        self.competency_table = competency_table
        self.page_margin = page_margin


_STYLES = {
    # Pure black/gray, no color, no borders — maximum ATS-parser safety.
    "technical": _Style(
        name_size=40, name_color="000000", name_align="left", header_border=False,
        heading_color="000000", heading_border_color=None, accent="000000",
        competency_table=False,
    ),
    # Classic single-column business resume, understated navy accent.
    "traditional": _Style(
        name_size=36, name_color="1F2937", name_align="left", header_border=False,
        heading_color="1F2937", heading_border_color="9CA3AF", accent="1F2937",
        competency_table=False,
    ),
    # More polished centered header + a competency grid, like a staff/exec resume.
    "executive": _Style(
        name_size=44, name_color="1F3A5F", name_align="center", header_border=True,
        heading_color="1F3A5F", heading_border_color="1F3A5F", accent="1F3A5F",
        competency_table=True,
    ),
}


def _build_paragraphs(data: dict[str, Any], style: _Style) -> list[str]:
    paras: list[str] = []

    def tr(text: str, bold: bool = False, italic: bool = False, size: int = 22,
           color: str = "111827") -> str:
        # Collapse whitespace runs but keep leading/trailing single spaces —
        # header lines join separate runs like tr("Company") + tr("  |  " + dates)
        # and a plain .split()/" ".join() would eat that leading separator space.
        cleaned = re.sub(r"\s+", " ", text)
        escaped = escape_js_string(cleaned)
        props = [f'text: "{escaped}"', 'font: "Calibri"', f'size: {size}', f'color: "{color}"']
        if bold:
            props.append("bold: true")
        if italic:
            props.append("italic: true")
        return "new TextRun({ " + ", ".join(props) + " })"

    def add(children_strs: list[str], before: int = 0, after: int = 80,
            left: int = 0, align: str | None = None, border: str | None = None) -> None:
        spacing = f"before: {before}, after: {after}"
        indent = f", indent: {{ left: {left} }}" if left else ""
        alignment = f", alignment: AlignmentType.{align.upper()}" if align else ""
        border_js = (
            f", border: {{ bottom: {{ style: BorderStyle.SINGLE, size: 6, "
            f'color: "{border}", space: 4 }} }}'
            if border else ""
        )
        paras.append(
            f"      new Paragraph({{ spacing: {{ {spacing} }}{indent}{alignment}{border_js}, "
            f"children: [{', '.join(children_strs)}] }})"
        )

    def heading(text: str) -> None:
        add([tr(text, bold=True, size=24, color=style.heading_color)],
            before=240, after=100, border=style.heading_border_color)

    def body(text: str, after: int = 80) -> None:
        if text:
            add([tr(text)], after=after)

    def bullet(text: str) -> None:
        add([tr("•  " + text)], after=40, left=360)

    # Name + contact header
    add([tr(data.get("full_name", ""), bold=True, size=style.name_size, color=style.name_color)],
        after=0, align=style.name_align)
    contact = _contact_line(data)
    if contact:
        add([tr(contact, size=20, color="6B7280")], after=160,
            align=style.name_align, border="9CA3AF" if style.header_border else None)

    # Professional Summary
    if data.get("summary"):
        heading("Professional Summary")
        body(data["summary"], after=0)

    # Core Competencies / Skills
    skills = [s for s in data.get("skills", []) if s]
    if skills and not style.competency_table:
        heading("Core Competencies")
        for i in range(0, len(skills), 5):
            body(" | ".join(skills[i:i + 5]), after=40)
    # (the executive template's table is spliced in separately — see render_resume)

    # Professional Experience
    jobs = data.get("jobs", [])
    if jobs:
        heading("Professional Experience")
        for job in jobs:
            header_children = [tr(job.get("company", ""), bold=True)]
            dates = _date_range(job)
            if dates:
                header_children.append(tr("  |  " + dates))
            add(header_children, before=200, after=0)
            title_loc = job.get("title", "")
            if job.get("location"):
                title_loc = f"{title_loc}  —  {job['location']}" if title_loc else job["location"]
            if title_loc:
                add([tr(title_loc, italic=True)], after=40)
            for b in job.get("bullets", []):
                if b:
                    bullet(b)

    # Education
    education = data.get("education", [])
    if education:
        heading("Education")
        for edu in education:
            children = [tr(edu.get("degree", ""), bold=True)]
            rest = "  —  ".join(p for p in [edu.get("institution", ""), edu.get("graduated", "")] if p)
            if rest:
                children.append(tr("  —  " + rest))
            add(children, before=60, after=40)

    # Certifications
    certs = data.get("certs", [])
    if certs:
        heading("Certifications")
        for cert in certs:
            line = cert.get("name", "")
            if cert.get("issuer"):
                line += f" — {cert['issuer']}"
            if cert.get("year"):
                line += f" ({cert['year']})"
            body(line, after=40)

    return paras


def _competency_table_js(skills: list[str]) -> str:
    """A 2-column skills table for the executive template, docx Table/TableRow/TableCell."""
    rows = []
    for i in range(0, len(skills), 2):
        pair = skills[i:i + 2]
        cells = []
        for s in pair:
            escaped = escape_js_string(s)
            cells.append(
                "new TableCell({ children: [new Paragraph({ spacing: { after: 40 }, "
                f'children: [new TextRun({{ text: "{escaped}", font: "Calibri", size: 22, '
                'color: "111827" })] })], width: { size: 50, type: WidthType.PERCENTAGE } })'
            )
        if len(cells) == 1:
            cells.append(
                'new TableCell({ children: [new Paragraph({})], '
                "width: { size: 50, type: WidthType.PERCENTAGE } })"
            )
        rows.append("new TableRow({ children: [" + ", ".join(cells) + "] })")
    return (
        "new Table({ width: { size: 100, type: WidthType.PERCENTAGE }, "
        "borders: { top: { style: BorderStyle.NONE }, bottom: { style: BorderStyle.NONE }, "
        "left: { style: BorderStyle.NONE }, right: { style: BorderStyle.NONE }, "
        "insideHorizontal: { style: BorderStyle.NONE }, insideVertical: { style: BorderStyle.NONE } }, "
        "rows: [" + ", ".join(rows) + "] })"
    )


def render_resume(data: dict[str, Any], template: str, output_path: Path,
                   progress=print) -> None:
    """Render `data` into a DOCX at output_path using the given template style.

    output_path must live under the project root (e.g. output/{user_id}/...)
    so the generated script's `require('docx')` can resolve node_modules by
    walking up from its own directory — a /tmp path won't have that.
    """
    if template not in _STYLES:
        raise WorkflowError(f"Unknown resume template: {template!r}")
    style = _STYLES[template]
    output_path.parent.mkdir(parents=True, exist_ok=True)

    paras = _build_paragraphs(data, style)

    # Splice a "Core Competencies" heading + table right after the summary
    # for the executive template (the plain-text list above is skipped for it).
    skills = [s for s in data.get("skills", []) if s]
    if skills and style.competency_table:
        heading_js = (
            f'new Paragraph({{ spacing: {{ before: 240, after: 100 }}, '
            f'border: {{ bottom: {{ style: BorderStyle.SINGLE, size: 6, '
            f'color: "{style.heading_border_color}", space: 4 }} }}, '
            f'children: [new TextRun({{ text: "Core Competencies", font: "Calibri", '
            f'size: 24, bold: true, color: "{style.heading_color}" }})] }})'
        )
        table_js = _competency_table_js(skills)
        # Index 2: after name header (0) + contact line (1)
        paras.insert(2, heading_js)
        paras.insert(3, table_js)

    children_js = ",\n".join(paras)
    out_path_str = str(output_path).replace("\\", "/")

    js = f"""\
const {{ Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
         BorderStyle, AlignmentType, WidthType }} = require('docx');
const fs = require('fs');

const doc = new Document({{
  styles: {{ default: {{ document: {{ run: {{ font: "Calibri", size: 22 }} }} }} }},
  sections: [{{
    properties: {{
      page: {{
        size: {{ width: 12240, height: 15840 }},
        margin: {{ top: {style.page_margin}, right: 1080, bottom: {style.page_margin}, left: 1080 }}
      }}
    }},
    children: [
{children_js}
    ]
  }}]
}});

Packer.toBuffer(doc).then(buffer => {{
  fs.writeFileSync('{out_path_str}', buffer);
  console.log('Master resume written.');
}});
"""

    js_path = output_path.parent / f"resume_builder_gen_{os.urandom(4).hex()}.js"
    js_path.write_text(js, encoding="utf-8")
    try:
        result = subprocess.run(["node", str(js_path)], capture_output=True, text=True)
    finally:
        js_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise WorkflowError(f"Resume generation JS failed:\n{result.stderr}")
    progress(f"  ✓ Master resume written to {output_path}")
