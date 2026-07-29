"""Tests for scripts/js_object_parser — the eval()-free JS-object-literal
parser used to extract frontend/kb.html's `const KB = {...}` data.

The central property under test isn't just "parses valid input correctly"
but "anything resembling executable JS is rejected rather than run" — that's
the whole reason this module replaced eval() in routers/kb.py.
"""

import pytest

from scripts.js_object_parser import (
    JSObjectParseError,
    extract_balanced_braces,
    parse_js_object,
)


class TestParseValues:
    def test_flat_object(self):
        assert parse_js_object("{ a: 'x', b: \"y\" }") == {"a": "x", "b": "y"}

    def test_backtick_string_treated_as_plain_text(self):
        assert parse_js_object("{ body: `hello <b>world</b>` }") == {
            "body": "hello <b>world</b>"
        }

    def test_multiline_backtick_string(self):
        result = parse_js_object("{ body: `line one\nline two` }")
        assert result == {"body": "line one\nline two"}

    def test_nested_objects_and_arrays(self):
        src = "{ a: [ { id: 1 }, { id: 2 } ], b: { c: [1,2,3] } }"
        assert parse_js_object(src) == {"a": [{"id": 1}, {"id": 2}], "b": {"c": [1, 2, 3]}}

    def test_numbers(self):
        assert parse_js_object("{ n: [1, -2, 3.5, -0.5, 1e3, 2.5e-2] }") == {
            "n": [1, -2, 3.5, -0.5, 1000.0, 0.025]
        }

    def test_booleans_and_null(self):
        assert parse_js_object("{ a: true, b: false, c: null }") == {
            "a": True, "b": False, "c": None,
        }

    def test_trailing_commas(self):
        assert parse_js_object("{ a: 1, b: [1, 2,], }") == {"a": 1, "b": [1, 2]}

    def test_unquoted_keys(self):
        assert parse_js_object("{ someKey: 1, _priv: 2, $x: 3 }") == {
            "someKey": 1, "_priv": 2, "$x": 3,
        }

    def test_quoted_keys(self):
        assert parse_js_object("{ 'a-b': 1, \"c d\": 2 }") == {"a-b": 1, "c d": 2}

    def test_line_and_block_comments_outside_strings(self):
        src = """{
            // leading comment
            a: 1, /* inline
            block comment */ b: 2
        }"""
        assert parse_js_object(src) == {"a": 1, "b": 2}

    def test_comment_like_text_inside_string_is_preserved(self):
        assert parse_js_object("{ url: 'https://example.com' }") == {
            "url": "https://example.com"
        }

    def test_escaped_quotes_and_backslash(self):
        assert parse_js_object(r"{ a: 'it\'s a \"test\"', b: 'back\\slash' }") == {
            "a": "it's a \"test\"", "b": "back\\slash",
        }

    def test_unicode_escape(self):
        assert parse_js_object(r"{ a: 'é' }") == {"a": "é"}

    def test_top_level_array(self):
        assert parse_js_object("[1, 2, {a: 3}]") == [1, 2, {"a": 3}]


class TestRejectsNonData:
    """Anything that isn't a plain literal must fail to parse — never
    execute. This is the property that makes this safe to run on untrusted
    or corrupted input, unlike eval()."""

    @pytest.mark.parametrize("payload", [
        "{ a: (function(){ return 1 })() }",
        "{ a: require('child_process') }",
        "{ a: this.constructor.constructor('return process')() }",
        "{ a: 1 + 1 }",
        "{ a: undefined_identifier }",
        "not even an object",
        "{ unterminated: 'string",
        "{ a: 1",  # unbalanced brace
    ])
    def test_rejected(self, payload):
        with pytest.raises(JSObjectParseError):
            parse_js_object(payload)

    def test_interpolation_syntax_is_kept_as_literal_text_not_evaluated(self):
        # ${...} has no special meaning to this parser — inside a backtick
        # string it's just characters, never evaluated as an expression the
        # way real JS (or eval()) would evaluate it to "2".
        result = parse_js_object("{ a: `${1+1}` }")
        assert result == {"a": "${1+1}"}


class TestExtractBalancedBraces:
    def test_simple(self):
        text = "prefix { a: 1 } suffix"
        assert extract_balanced_braces(text, text.index("{")) == "{ a: 1 }"

    def test_braces_inside_strings_do_not_confuse_depth(self):
        text = "const KB = { a: 'text with } inside', b: 2 }"
        start = text.index("{")
        extracted = extract_balanced_braces(text, start)
        assert extracted == "{ a: 'text with } inside', b: 2 }"
        assert parse_js_object(extracted) == {"a": "text with } inside", "b": 2}

    def test_braces_inside_backtick_template_do_not_confuse_depth(self):
        text = "const KB = { body: `<div>{not json}</div>` }"
        start = text.index("{")
        extracted = extract_balanced_braces(text, start)
        assert parse_js_object(extracted) == {"body": "<div>{not json}</div>"}

    def test_no_matching_close_raises(self):
        with pytest.raises(JSObjectParseError):
            extract_balanced_braces("{ a: 1", 0)

    def test_requires_open_brace_at_start(self):
        with pytest.raises(JSObjectParseError):
            extract_balanced_braces("not a brace", 0)
