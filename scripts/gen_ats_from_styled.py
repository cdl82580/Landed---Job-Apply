#!/usr/bin/env python3
"""
Generate an ATS-optimized resume DOCX from an already-styled resume DOCX.

Reads content verbatim from the styled DOCX's XML so there is no drift
between the two documents.

Usage:
    python3 scripts/gen_ats_from_styled.py <styled.docx> <output_ats.docx>
"""

import os
import re
import subprocess
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W14 = "http://schemas.microsoft.com/office/word/2010/wordml"


def w(tag: str) -> str:
    return f"{{{W}}}{tag}"


def get_text(elem: ET.Element) -> str:
    """Concatenate all w:t text under elem, preserving xml:space='preserve'."""
    parts = []
    for t in elem.iter(w("t")):
        parts.append(t.text or "")
    return "".join(parts)


def is_bold(para: ET.Element) -> bool:
    rpr = para.find(f".//{w('rPr')}/{w('b')}")
    return rpr is not None


def is_italic(para: ET.Element) -> bool:
    rpr = para.find(f".//{w('rPr')}/{w('i')}")
    return rpr is not None


def has_bottom_border(para: ET.Element) -> bool:
    return para.find(f".//{w('pBdr')}/{w('bottom')}") is not None


def is_bullet(para: ET.Element) -> bool:
    """True for a real Word list item (numPr) or this app's own JS-based DOCX
    generators' convention of a literal "•  " text prefix instead of real
    list numbering — both appear across resumes this app itself produces."""
    if para.find(f".//{w('numPr')}") is not None:
        return True
    return get_text(para).lstrip().startswith("•")


def clean_bullet_text(text: str) -> str:
    """Collapse whitespace and strip a literal "•" prefix if present.

    Real Word list items (numPr) never have the bullet character in their
    text at all — the glyph is drawn by list formatting. This app's own
    generators write a literal "•" instead, so both conventions need to
    normalize to the same clean field text.
    """
    cleaned = " ".join(text.split())
    return cleaned[1:].strip() if cleaned.startswith("•") else cleaned


def is_section_header(para: ET.Element) -> bool:
    """Bold paragraph with a bottom border — section headers, color-agnostic."""
    text = get_text(para).strip()
    return bool(text) and is_bold(para) and has_bottom_border(para)


def get_style(para: ET.Element) -> Optional[str]:
    pstyle = para.find(f".//{w('pStyle')}")
    if pstyle is not None:
        return pstyle.get(w("val"))
    return None


def parse_xml(docx_path: Path) -> dict:
    """Unzip the DOCX and parse word/document.xml to extract all resume content."""
    with zipfile.ZipFile(docx_path) as zf:
        xml_bytes = zf.read("word/document.xml")

    root = ET.fromstring(xml_bytes)
    body = root.find(w("body"))

    # Collect top-level body children (paragraphs and tables)
    children = list(body)

    # ── State machine ────────────────────────────────────────────────────────
    # We walk through and identify:
    #   tagline, summary, competencies, experience jobs, education, certs

    result = {
        "name": "",
        "contact_line": "",
        "tagline": "",
        "summary_items": [],   # list of {text, bullet}; empty if no summary section
        "competencies": [],
        "jobs": [],            # list of {company, title, dates, bullets}
        "education": [],       # list of {degree, school}
        "certifications": [],
        "projects": [],        # list of {name, url, description}
    }

    section = "HEADER"
    current_job = None
    current_project = None

    for child in children:
        tag = child.tag

        if tag == w("p"):
            text = get_text(child).strip()
            if not text:
                continue

            # Section header detection
            if is_section_header(child):
                if current_job:
                    result["jobs"].append(current_job)
                    current_job = None
                if current_project:
                    result["projects"].append(current_project)
                    current_project = None
                # Case-insensitive: this app's own three wizard-generated
                # resume templates use Title Case headings ("Professional
                # Summary"), not the ALL-CAPS convention the repo's own
                # master.docx happens to use — a case-sensitive match would
                # silently fail to parse them at all.
                text_upper = text.upper()
                if "PROFESSIONAL SUMMARY" in text_upper:
                    section = "SUMMARY"
                elif "CORE COMPETENCIES" in text_upper:
                    section = "COMPETENCIES"
                elif "PROFESSIONAL EXPERIENCE" in text_upper:
                    section = "EXPERIENCE"
                elif "EDUCATION" in text_upper:
                    section = "EDUCATION"
                elif "PROJECTS" in text_upper:
                    section = "PROJECTS"
                elif "CERTIFICATIONS" in text_upper:
                    section = "CERTIFICATIONS"
                continue

            # Name: the very first non-empty paragraph in the document. Every
            # template this app produces (and every conventional resume)
            # leads with the candidate's full name, so this never needs the
            # bold/border heuristics used elsewhere — it's just paragraph #1.
            if section == "HEADER" and not result["name"]:
                result["name"] = " ".join(text.split())
                continue

            # Contact line: first HEADER paragraph containing "|" (the field
            # separator between phone / email / location / links).
            if section == "HEADER" and not result["contact_line"] and "|" in text:
                result["contact_line"] = " ".join(text.split())
                continue

            # Tagline: in HEADER, non-bold paragraph with a bottom border that does
            # NOT contain "|" — the contact bar also has a border but uses "|"
            # as a field separator, so this distinguishes the two reliably.
            if (section == "HEADER" and has_bottom_border(child)
                    and not is_bold(child) and "|" not in text):
                result["tagline"] = " ".join(text.split())
                continue

            # Summary
            if section == "SUMMARY":
                result["summary_items"].append(
                    {"text": clean_bullet_text(text), "bullet": is_bullet(child)}
                )

            # Experience: bullets, plus a paragraph-based job-header layout
            # (bold "Company [| dates]" line, then an italic title line) —
            # this app's own wizard-generated resumes use plain paragraphs
            # here rather than the 2-column table master.docx uses (handled
            # separately below), and plenty of real uploaded resumes will
            # too, so both layouts need to be recognized.
            elif section == "EXPERIENCE" and is_bullet(child):
                if current_job is not None:
                    current_job["bullets"].append(clean_bullet_text(text))
            elif section == "EXPERIENCE" and is_bold(child) and not is_italic(child):
                if current_job:
                    result["jobs"].append(current_job)
                clean = " ".join(text.split())
                parts = re.split(r"\s+\|\s+", clean, maxsplit=1)
                current_job = {
                    "company": parts[0],
                    "title": "",
                    "dates": parts[1] if len(parts) == 2 else "",
                    "bullets": [],
                }
            elif (section == "EXPERIENCE" and is_italic(child)
                    and current_job is not None and not current_job["title"]):
                current_job["title"] = " ".join(text.split())

            # Education
            elif section == "EDUCATION":
                # Each education paragraph: "Degree — School, Location"
                # We store the whole line and split on " — " or " – "
                result["education"].append(" ".join(text.split()))

            # Projects: a bold "Name — url" paragraph starts a new project,
            # followed by a plain (non-bold) description paragraph.
            elif section == "PROJECTS":
                clean = " ".join(text.split())
                if is_bold(child):
                    if current_project:
                        result["projects"].append(current_project)
                    parts = re.split(r"\s+[—–]\s+", clean, maxsplit=1)
                    current_project = {
                        "name": parts[0],
                        "url": parts[1] if len(parts) == 2 else "",
                        "description": "",
                    }
                elif current_project is not None:
                    current_project["description"] = clean

            # Certifications
            elif section == "CERTIFICATIONS":
                if is_bullet(child):
                    result["certifications"].append(clean_bullet_text(text))

        elif tag == w("tbl"):
            rows = child.findall(w("tr"))

            # ── Competency table: 9 columns (5 data + 4 spacers) ─────────────
            # Identified by having exactly 9 grid columns — color-agnostic
            grid_cols = child.findall(f".//{w('gridCol')}")

            if section == "COMPETENCIES" or len(grid_cols) == 9:
                section = "COMPETENCIES"
                for row in rows:
                    cells = row.findall(w("tc"))
                    for i, cell in enumerate(cells):
                        # Skip spacer cells (width=100)
                        tcw = cell.find(f".//{w('tcW')}")
                        if tcw is not None and tcw.get(w("w")) == "100":
                            continue
                        cell_text = get_text(cell).strip()
                        if cell_text:
                            result["competencies"].append(" ".join(cell_text.split()))
                continue

            # ── Company header table: 2 columns ──────────────────────────────
            # The left cell has company name (bold) + title (italic)
            # The right cell has dates + location
            if section == "EXPERIENCE" and len(rows) == 1 and len(rows[0].findall(w("tc"))) == 2:
                row = rows[0]
                cells = row.findall(w("tc"))
                left_cell = cells[0]
                right_cell = cells[1]

                # Save previous job
                if current_job:
                    result["jobs"].append(current_job)

                # Extract company and title from left cell paragraphs
                company = ""
                title = ""
                left_paras = left_cell.findall(w("p"))
                for lp in left_paras:
                    lp_text = get_text(lp).strip()
                    if not lp_text:
                        continue
                    if is_bold(lp):
                        company = " ".join(lp_text.split())
                    elif is_italic(lp):
                        title = " ".join(lp_text.split())

                # Extract dates from right cell (first non-empty paragraph)
                dates = ""
                right_paras = right_cell.findall(w("p"))
                for rp in right_paras:
                    rp_text = get_text(rp).strip()
                    if rp_text and not is_italic(rp):
                        dates = " ".join(rp_text.split())
                        break

                current_job = {
                    "company": company,
                    "title": title,
                    "dates": dates,
                    "bullets": [],
                }

    # Flush last job / project
    if current_job:
        result["jobs"].append(current_job)
    if current_project:
        result["projects"].append(current_project)

    return result


def escape_js(s: str) -> str:
    s = s.replace("\\", "\\\\")
    s = s.replace("`", "\\`")
    s = s.replace("${", "\\${")
    s = s.replace('"', '\\"')
    return s


def tr(text: str, bold: bool = False, italic: bool = False, size: int = 22) -> str:
    cleaned = " ".join(text.split())
    escaped = escape_js(cleaned)
    props = [f'text: "{escaped}"', 'font: "Calibri"', f'size: {size}', 'color: "000000"']
    if bold:
        props.append("bold: true")
    if italic:
        props.append("italics: true")
    return "new TextRun({ " + ", ".join(props) + " })"


def build_ats(data: dict, output_path: Path):
    paras = []

    def add(children_strs, before=0, after=80, left=0):
        spacing = f"before: {before}, after: {after}"
        indent = f", indent: {{ left: {left} }}" if left else ""
        paras.append(
            f"      new Paragraph({{ spacing: {{ {spacing} }}{indent}, "
            f"children: [{', '.join(children_strs)}] }})"
        )

    def heading(text):
        add([tr(text, bold=True, size=24)], before=240, after=60)

    def body(text, after=80):
        add([tr(text)], after=after)

    def bullet(text):
        add([tr("•  " + text)], after=40, left=360)

    def job_header(company_name, title, dates):
        children = [tr(company_name, bold=True)]
        if dates:
            children.append(tr("  |  " + dates))
        add(children, before=200, after=0)
        if title:
            add([tr(title, italic=True)], after=40)

    # Name + contact — read verbatim from the styled resume's own header so
    # this never shows a different candidate's identity. Fail loudly rather
    # than silently falling back to any placeholder if extraction came up
    # empty, since a wrong-or-missing name on an ATS resume is worse than a
    # build error.
    if not data.get("name"):
        raise RuntimeError(
            "Could not find the candidate's name in the styled resume's header "
            "— refusing to generate an ATS resume with a missing/placeholder identity."
        )
    add([tr(data["name"], bold=True, size=40)], after=0)
    if data.get("contact_line"):
        add([tr(data["contact_line"], size=20)], after=120)

    # Tagline
    add([tr(data["tagline"], italic=True)], after=160)

    # Summary
    if data["summary_items"]:
        heading("Professional Summary")
        for item in data["summary_items"]:
            bullet(item["text"]) if item["bullet"] else body(item["text"], after=0)

    # Competencies
    heading("Core Competencies")
    comps = data["competencies"]
    for i in range(0, len(comps), 5):
        body(" | ".join(comps[i:i+5]), after=40)

    # Experience
    heading("Professional Experience")
    for job in data["jobs"]:
        job_header(job["company"], job["title"], job["dates"])
        for b in job["bullets"]:
            bullet(b)

    # Education — parse "Degree — School" or keep as-is
    heading("Education")
    for edu_line in data["education"]:
        # Split on em-dash or en-dash to separate degree from school
        parts = re.split(r"\s+[—–]\s+", edu_line, maxsplit=1)
        if len(parts) == 2:
            children = [tr(parts[0], bold=True), tr("  —  " + parts[1])]
        else:
            children = [tr(edu_line, bold=True)]
        add(children, before=60, after=40)

    # Projects (parsed verbatim from the styled resume's own Projects
    # section — omitted entirely if that resume doesn't have one)
    projects = data.get("projects", [])
    if projects:
        heading("Projects")
        for proj in projects:
            children = [tr(proj["name"], bold=True)]
            if proj.get("url"):
                children.append(tr("  |  " + proj["url"]))
            add(children, before=100, after=0)
            if proj.get("description"):
                body(proj["description"], after=60)

    # Certifications
    heading("Certifications")
    for cert in data["certifications"]:
        body(cert, after=40)

    children_js = ",\n".join(paras)
    out_str = str(output_path).replace("\\", "/")

    js = f"""\
const {{ Document, Packer, Paragraph, TextRun }} = require('docx');
const fs = require('fs');

const doc = new Document({{
  styles: {{ default: {{ document: {{ run: {{ font: "Calibri", size: 22 }} }} }} }},
  sections: [{{
    properties: {{
      page: {{
        size: {{ width: 12240, height: 15840 }},
        margin: {{ top: 720, right: 1080, bottom: 720, left: 1080 }}
      }}
    }},
    children: [
{children_js}
    ]
  }}]
}});

Packer.toBuffer(doc).then(buffer => {{
  fs.writeFileSync('{out_str}', buffer);
  console.log('ATS resume written to {out_str}');
}});
"""

    js_path = output_path.parent / f"_ats_gen_{os.urandom(4).hex()}.js"
    js_path.write_text(js, encoding="utf-8")

    try:
        result = subprocess.run(
            ["node", str(js_path)], capture_output=True, text=True
        )
    finally:
        js_path.unlink(missing_ok=True)

    if result.returncode != 0:
        raise RuntimeError(f"Node error generating ATS resume:\n{result.stderr}")

    print(result.stdout.strip())


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <styled.docx> <output_ats.docx>")
        sys.exit(1)

    styled = Path(sys.argv[1])
    output = Path(sys.argv[2])

    if not styled.exists():
        print(f"Error: {styled} not found")
        sys.exit(1)

    output.parent.mkdir(exist_ok=True)

    print(f"Parsing {styled}...")
    data = parse_xml(styled)

    print(f"  Tagline   : {data['tagline'][:60]}...")
    print(f"  Summary   : {len(data['summary_items'])} item(s)")
    print(f"  Comps     : {len(data['competencies'])} cells")
    print(f"  Jobs      : {len(data['jobs'])} ({[j['company'] for j in data['jobs']]})")
    print(f"  Education : {len(data['education'])} entries")
    print(f"  Certs     : {len(data['certifications'])} entries")

    try:
        build_ats(data, output)
    except RuntimeError as exc:
        print(exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
