"""
A minimal, safe parser for the restricted subset of JS-object-literal syntax
used by frontend/kb.html's `const KB = {...}` data block: objects, arrays,
single/double-quoted strings, backtick template strings (treated as plain
text — no ${} interpolation support, since the data never uses it), numbers,
booleans, null, bare-identifier keys, trailing commas, and // and /* */
comments outside of strings.

Deliberately does NOT evaluate JS expressions — unlike eval(), a malformed
or hostile input can only fail to parse, never execute code.
"""

from __future__ import annotations

_WHITESPACE = " \t\r\n"
_IDENT_START = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_$")
_IDENT_CONT = _IDENT_START | set("0123456789")

_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v",
    "0": "\0", "'": "'", '"': '"', "`": "`", "\\": "\\",
}


class JSObjectParseError(ValueError):
    pass


class _Parser:
    def __init__(self, text: str):
        self.s = text
        self.n = len(text)
        self.i = 0

    def _err(self, msg: str) -> JSObjectParseError:
        line = self.s.count("\n", 0, self.i) + 1
        return JSObjectParseError(f"{msg} at offset {self.i} (line {line})")

    def _peek(self) -> str:
        return self.s[self.i] if self.i < self.n else ""

    def skip_ws_and_comments(self) -> None:
        while self.i < self.n:
            c = self.s[self.i]
            if c in _WHITESPACE:
                self.i += 1
                continue
            if c == "/" and self.i + 1 < self.n:
                nxt = self.s[self.i + 1]
                if nxt == "/":
                    end = self.s.find("\n", self.i)
                    self.i = self.n if end == -1 else end + 1
                    continue
                if nxt == "*":
                    end = self.s.find("*/", self.i + 2)
                    if end == -1:
                        raise self._err("Unterminated block comment")
                    self.i = end + 2
                    continue
            break

    def parse_value(self):
        self.skip_ws_and_comments()
        c = self._peek()
        if c == "{":
            return self.parse_object()
        if c == "[":
            return self.parse_array()
        if c in ("'", '"', "`"):
            return self.parse_string()
        if c.isdigit() or (c == "-" and self.i + 1 < self.n and self.s[self.i + 1].isdigit()):
            return self.parse_number()
        if self.s.startswith("true", self.i):
            self.i += 4
            return True
        if self.s.startswith("false", self.i):
            self.i += 5
            return False
        if self.s.startswith("null", self.i):
            self.i += 4
            return None
        if self.s.startswith("undefined", self.i):
            self.i += 9
            return None
        raise self._err(f"Unexpected character {c!r} while parsing value")

    def parse_object(self) -> dict:
        assert self._peek() == "{"
        self.i += 1
        obj: dict = {}
        while True:
            self.skip_ws_and_comments()
            if self._peek() == "}":
                self.i += 1
                return obj
            key = self.parse_key()
            self.skip_ws_and_comments()
            if self._peek() != ":":
                raise self._err("Expected ':' after object key")
            self.i += 1
            value = self.parse_value()
            obj[key] = value
            self.skip_ws_and_comments()
            if self._peek() == ",":
                self.i += 1
                self.skip_ws_and_comments()
                if self._peek() == "}":
                    self.i += 1
                    return obj
                continue
            if self._peek() == "}":
                self.i += 1
                return obj
            raise self._err("Expected ',' or '}' in object")

    def parse_array(self) -> list:
        assert self._peek() == "["
        self.i += 1
        arr: list = []
        while True:
            self.skip_ws_and_comments()
            if self._peek() == "]":
                self.i += 1
                return arr
            arr.append(self.parse_value())
            self.skip_ws_and_comments()
            if self._peek() == ",":
                self.i += 1
                self.skip_ws_and_comments()
                if self._peek() == "]":
                    self.i += 1
                    return arr
                continue
            if self._peek() == "]":
                self.i += 1
                return arr
            raise self._err("Expected ',' or ']' in array")

    def parse_key(self) -> str:
        c = self._peek()
        if c in ("'", '"', "`"):
            return self.parse_string()
        if c in _IDENT_START:
            start = self.i
            self.i += 1
            while self.i < self.n and self.s[self.i] in _IDENT_CONT:
                self.i += 1
            return self.s[start:self.i]
        raise self._err(f"Unexpected character {c!r} while parsing key")

    def parse_string(self) -> str:
        quote = self._peek()
        self.i += 1
        out: list[str] = []
        while True:
            if self.i >= self.n:
                raise self._err("Unterminated string")
            c = self.s[self.i]
            if c == "\\":
                self.i += 1
                if self.i >= self.n:
                    raise self._err("Unterminated escape sequence")
                esc = self.s[self.i]
                if esc == "u":
                    hex4 = self.s[self.i + 1:self.i + 5]
                    if len(hex4) != 4:
                        raise self._err("Invalid \\u escape")
                    out.append(chr(int(hex4, 16)))
                    self.i += 5
                    continue
                if esc == "x":
                    hex2 = self.s[self.i + 1:self.i + 3]
                    if len(hex2) != 2:
                        raise self._err("Invalid \\x escape")
                    out.append(chr(int(hex2, 16)))
                    self.i += 3
                    continue
                if esc == "\n":
                    # backslash-newline line continuation — produces nothing
                    self.i += 1
                    continue
                out.append(_ESCAPES.get(esc, esc))
                self.i += 1
                continue
            if c == quote:
                self.i += 1
                return "".join(out)
            out.append(c)
            self.i += 1

    def parse_number(self):
        start = self.i
        if self._peek() == "-":
            self.i += 1
        while self.i < self.n and self.s[self.i].isdigit():
            self.i += 1
        is_float = False
        if self._peek() == ".":
            is_float = True
            self.i += 1
            while self.i < self.n and self.s[self.i].isdigit():
                self.i += 1
        if self._peek() in ("e", "E"):
            is_float = True
            self.i += 1
            if self._peek() in ("+", "-"):
                self.i += 1
            while self.i < self.n and self.s[self.i].isdigit():
                self.i += 1
        text = self.s[start:self.i]
        return float(text) if is_float else int(text)


def parse_js_object(text: str):
    """Parse a JS object/array literal (the restricted subset described above)
    into Python data. Raises JSObjectParseError on anything it can't parse —
    it never executes code, unlike eval()."""
    parser = _Parser(text)
    parser.skip_ws_and_comments()
    value = parser.parse_value()
    parser.skip_ws_and_comments()
    if parser.i != parser.n:
        raise parser._err("Unexpected trailing content after top-level value")
    return value


def extract_balanced_braces(text: str, start: int) -> str:
    """Return the substring of `text` from `start` (which must point at '{')
    through its matching closing brace — skipping over braces that appear
    inside single/double-quoted strings or backtick template literals so
    they don't throw off the depth count. Does not parse or execute the
    contents; use parse_js_object() on the result for that."""
    if start >= len(text) or text[start] != "{":
        raise JSObjectParseError("extract_balanced_braces: expected '{' at start index")
    depth = 0
    i = start
    in_str = False
    str_char = ""
    in_template = False
    n = len(text)
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == str_char:
                in_str = False
            i += 1
            continue
        if c == "`":
            in_template = not in_template
            i += 1
            continue
        if in_template:
            if c == "\\":
                i += 2
                continue
            i += 1
            continue
        if c in ("'", '"'):
            in_str = True
            str_char = c
            i += 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        i += 1
    raise JSObjectParseError("extract_balanced_braces: no matching '}' found")
