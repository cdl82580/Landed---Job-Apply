"""Unit tests for utility functions in apply.py."""

import pytest


class TestSafeFilename:
    @pytest.fixture(autouse=True)
    def fn(self):
        from apply import safe_filename
        self.fn = safe_filename

    def test_basic(self):
        assert self.fn("Acme Corp") == "AcmeCorp"

    def test_hyphens_preserved(self):
        # Hyphens are allowed in safe filenames
        result = self.fn("Go-To-Market")
        assert result == "Go-To-Market"

    def test_parens_stripped(self):
        result = self.fn("GTM (AI)")
        assert "(" not in result and ")" not in result

    def test_slashes_stripped(self):
        result = self.fn("VP/Engineering")
        assert "/" not in result

    def test_dots_stripped(self):
        result = self.fn("v1.2.3")
        assert "." not in result

    def test_empty_string(self):
        assert self.fn("") == ""

    def test_all_special(self):
        assert self.fn("!@#$%^&*()") == ""

    def test_preserves_alphanumeric(self):
        assert self.fn("ABC123") == "ABC123"


class TestEscapeJsString:
    @pytest.fixture(autouse=True)
    def fn(self):
        from apply import escape_js_string
        self.fn = escape_js_string

    def test_plain_text_unchanged(self):
        assert self.fn("hello world") == "hello world"

    def test_escapes_backslash(self):
        result = self.fn("path\\to\\file")
        # Result should have escaped backslashes (double) or no raw backslash
        assert "\\" in result

    def test_escapes_double_quote(self):
        result = self.fn('say "hello"')
        # The raw double quote should be escaped in JS output
        assert '"' not in result or '\\"' in result

    def test_newlines_safe_in_output(self):
        # A literal newline would terminate a JS double-quoted string early
        # and crash the generated script — it must be escaped, not passed through.
        result = self.fn("line1\nline2")
        assert "\n" not in result
        assert "\\n" in result
        assert 'const x = "%s";' % result  # must be a syntactically plausible one-liner

    def test_carriage_return_escaped(self):
        result = self.fn("line1\r\nline2")
        assert "\r" not in result and "\n" not in result
        assert "\\n" in result

    def test_tabs_safe_in_output(self):
        result = self.fn("col1\tcol2")
        assert isinstance(result, str)

    def test_empty_string(self):
        assert self.fn("") == ""


class TestBrandColorFetch:
    """get_brand_color should always return a dict with at least a primary color."""

    def test_returns_dict(self, monkeypatch):
        # Mock requests so no real network call is made
        import apply as _apply
        monkeypatch.setattr(_apply, "get_brand_color", lambda company: {"primary": "1A3C5E"})
        result = _apply.get_brand_color("Acme")
        assert isinstance(result, dict)

    def test_unknown_company_returns_defaults(self, monkeypatch):
        import apply as _apply
        # Patch _fetch_brand_color to simulate a 404
        monkeypatch.setattr(_apply, "get_brand_color", lambda c: {"primary": "1A3C5E", "secondary": "4A7FA5"})
        result = _apply.get_brand_color("UnknownXYZCorp123")
        assert "primary" in result


class TestPrepDocxBuild:
    """Smoke test that _build_prep_docx_js returns valid JS string."""

    def test_returns_nonempty_js(self):
        from apply import _build_prep_docx_js
        from pathlib import Path

        data = {
            "elevator_pitch": "I'm a solutions engineer with 5+ years building integrations.",
            "pitch_timing": "Timing: ~40-45 seconds. Practice out loud, don't read verbatim.",
            "pitch_adapt": "Adapt live: if they name a specific product, swap the close to name it.",
            "snapshot_role": "Role: Solutions Engineer · Remote · ~$120K",
            "snapshot_company": "Company: Series B, ~50 people, API-first product.",
            "snapshot_leadership": "Leadership: Jane Smith (VP Eng).",
            "snapshot_stack": "Stack: Python, FastAPI, Kubernetes.",
            "snapshot_read": "How to read this: fast-moving, expect ambiguity.",
            "pillars": [
                {"name": "API Reliability", "bullets": ["Deployed on Fly.io with Docker."]},
            ],
            "likely_questions": [
                {"question": "Walk me through your approach.", "answer": "I start with discovery workshops."},
            ],
            "questions_to_ask": ["What does success look like in 90 days?"],
            "before_interview": ["Re-skim their API docs before the call."],
        }

        js = _build_prep_docx_js(
            data, "Acme", "Solutions Engineer", "Jane Smith",
            Path("/tmp/test_out.docx"), {},
        )
        assert isinstance(js, str)
        assert "require('docx')" in js
        assert "Acme" in js
        assert "Solutions Engineer" in js
        assert len(js) > 1000  # substantive output

    def test_blank_interviewer_shows_placeholder(self):
        from apply import _build_prep_docx_js
        from pathlib import Path

        js = _build_prep_docx_js(
            {}, "Acme", "Solutions Engineer", "",
            Path("/tmp/test_out.docx"), {},
        )
        assert "[Name]" in js and "[Title/Role]" in js

    def test_multiline_interviewer_becomes_separate_paragraphs(self):
        from apply import _build_prep_docx_js
        from pathlib import Path

        js = _build_prep_docx_js(
            {}, "Acme", "Solutions Engineer",
            "Jane Smith - VP Eng\nJohn Doe - Peer",
            Path("/tmp/test_out.docx"), {},
        )
        # Two distinct interviewer lines should show up as two separate TextRuns,
        # not collapsed into one line by whitespace normalization.
        assert js.count("new Paragraph(") >= 2
        assert "Interviewers:" in js
        assert "Jane Smith" in js and "John Doe" in js

    def test_single_interviewer_uses_singular_label(self):
        from apply import _build_prep_docx_js
        from pathlib import Path

        js = _build_prep_docx_js(
            {}, "Acme", "Solutions Engineer", "Jane Smith",
            Path("/tmp/test_out.docx"), {},
        )
        assert "Interviewer:" in js
        assert "Interviewers:" not in js

    def test_logistics_line_shows_only_provided_parts(self):
        from apply import _build_prep_docx_js
        from pathlib import Path

        js = _build_prep_docx_js(
            {}, "Acme", "Solutions Engineer", "",
            Path("/tmp/test_out.docx"), {},
            interview_date="2026-07-20",
        )
        assert "Date: 2026-07-20" in js
        assert "Time:" not in js
        assert "Location:" not in js

    def test_logistics_line_omitted_when_all_blank(self):
        from apply import _build_prep_docx_js
        from pathlib import Path

        js = _build_prep_docx_js(
            {}, "Acme", "Solutions Engineer", "",
            Path("/tmp/test_out.docx"), {},
        )
        assert "Date:" not in js
        assert "Time:" not in js
        assert "Location:" not in js

    def test_logo_embeds_image_run(self):
        from apply import _build_prep_docx_js
        from pathlib import Path

        js = _build_prep_docx_js(
            {}, "Acme", "Solutions Engineer", "",
            Path("/tmp/test_out.docx"), {},
            logo={"bytes": b"fakepngbytes", "format": "png", "width": 200, "height": 60},
        )
        assert "new ImageRun(" in js
        assert "Buffer.from(" in js

    def test_no_logo_skips_image_run(self):
        from apply import _build_prep_docx_js
        from pathlib import Path

        js = _build_prep_docx_js(
            {}, "Acme", "Solutions Engineer", "",
            Path("/tmp/test_out.docx"), {}, logo=None,
        )
        assert "new ImageRun(" not in js

    def test_interviewer_url_becomes_hyperlink(self):
        from apply import _build_prep_docx_js
        from pathlib import Path

        js = _build_prep_docx_js(
            {}, "Acme", "Solutions Engineer",
            "Jane Smith - VP Eng - https://www.linkedin.com/in/janesmith/",
            Path("/tmp/test_out.docx"), {},
        )
        assert "new ExternalHyperlink(" in js
        assert 'link: "https://www.linkedin.com/in/janesmith/"' in js
        # Trailing text stays outside the hyperlink run
        assert "Jane Smith - VP Eng -" in js

    def test_location_url_becomes_hyperlink(self):
        from apply import _build_prep_docx_js
        from pathlib import Path

        js = _build_prep_docx_js(
            {}, "Acme", "Solutions Engineer", "",
            Path("/tmp/test_out.docx"), {},
            location="https://meet.google.com/abc-defg-hij",
        )
        assert "new ExternalHyperlink(" in js
        assert 'link: "https://meet.google.com/abc-defg-hij"' in js

    def test_trailing_punctuation_excluded_from_link(self):
        from apply import _build_prep_docx_js
        from pathlib import Path

        js = _build_prep_docx_js(
            {}, "Acme", "Solutions Engineer", "",
            Path("/tmp/test_out.docx"), {},
            location="See https://example.com/path, thanks.",
        )
        assert 'link: "https://example.com/path"' in js

    def test_no_url_skips_hyperlink(self):
        from apply import _build_prep_docx_js
        from pathlib import Path

        js = _build_prep_docx_js(
            {}, "Acme", "Solutions Engineer", "Jane Smith - VP Eng",
            Path("/tmp/test_out.docx"), {},
        )
        assert "new ExternalHyperlink(" not in js

    def test_space_before_hyperlink_is_preserved(self):
        """Regression test: splitting text around a URL used to strip the
        boundary space, running 'Location:' straight into the link with no gap."""
        from apply import _build_prep_docx_js
        from pathlib import Path

        js = _build_prep_docx_js(
            {}, "Acme", "Solutions Engineer", "",
            Path("/tmp/test_out.docx"), {},
            location="https://meet.google.com/abc-defg-hij",
        )
        assert '"Location: "' in js

    def test_space_between_words_and_hyperlink_is_preserved(self):
        from apply import _build_prep_docx_js
        from pathlib import Path

        js = _build_prep_docx_js(
            {}, "Acme", "Solutions Engineer",
            "Jane Smith - VP Eng - https://www.linkedin.com/in/janesmith/",
            Path("/tmp/test_out.docx"), {},
        )
        assert '"Interviewer: Jane Smith - VP Eng - "' in js

    def test_all_text_runs_disable_proofing(self):
        """All prep-doc text is machine-generated — noProof suppresses Word's
        spell-check squiggles on proper nouns like the company name."""
        from apply import _build_prep_docx_js
        from pathlib import Path

        data = {"elevator_pitch": "Chipply is a great fit."}
        js = _build_prep_docx_js(
            data, "Chipply", "Solutions Engineer", "Jane Smith",
            Path("/tmp/test_out.docx"), {},
        )
        assert js.count("new TextRun(") > 0
        assert js.count("noProof: true") == js.count("new TextRun(")
