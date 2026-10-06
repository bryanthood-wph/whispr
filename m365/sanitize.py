"""The allowlist sanitizer for a draft's HTML body (tasks.m365.sanitize; §5 "keep").

Only tags listed in `tags` survive, each with only its `attributes`; a URL attribute
keeps only a scheme in `url_schemes` (a relative or schemeless URL is dropped). Any
other tag is dropped but its text kept, except inside `drop_content` tags, which go
whole. Text and attribute values are re-escaped, comments and declarations dropped, and
every tag left open is closed, so the fragment cannot reach outside where it is inserted.
"""

from __future__ import annotations

import html
from html.parser import HTMLParser

# HTML elements with no end tag.
VOID = frozenset({"br", "hr", "img", "wbr"})
URL_ATTRIBUTES = frozenset({"href", "src"})


class _Cleaner(HTMLParser):
    def __init__(self, rules: dict):
        super().__init__(convert_charrefs=True)
        self.tags = {t.casefold() for t in rules["tags"]}
        self.drop = {t.casefold() for t in rules["drop_content"]}
        self.attrs = {t.casefold(): {a.casefold() for a in names} for t, names in rules["attributes"].items()}
        self.schemes = {s.casefold() for s in rules["url_schemes"]}
        self.out: list[str] = []
        self.open: list[str] = []
        self.dropping: list[str] = []

    def _url_ok(self, value: str) -> bool:
        scheme, colon, _ = value.strip().partition(":")
        return bool(colon) and scheme.casefold() in self.schemes and "/" not in scheme

    def handle_starttag(self, tag, attrs):
        tag = tag.casefold()
        if tag in self.drop:
            if tag not in VOID:
                self.dropping.append(tag)
            return
        if self.dropping or tag not in self.tags:
            return
        kept = []
        for name, value in attrs:
            name = name.casefold()
            if name not in self.attrs.get(tag, ()) or value is None:
                continue
            if name in URL_ATTRIBUTES and not self._url_ok(value):
                continue
            kept.append(f' {name}="{html.escape(value, quote=True)}"')
        self.out.append(f"<{tag}{''.join(kept)}>")
        if tag not in VOID:
            self.open.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.casefold() not in VOID and self.open and self.open[-1] == tag.casefold():
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.casefold()
        if self.dropping:
            if tag == self.dropping[-1]:
                self.dropping.pop()
            return
        if tag in self.open:
            while self.open:
                last = self.open.pop()
                self.out.append(f"</{last}>")
                if last == tag:
                    break

    def handle_data(self, data):
        if not self.dropping:
            self.out.append(html.escape(data, quote=False))

    def result(self) -> str:
        self.close()
        return "".join(self.out) + "".join(f"</{t}>" for t in reversed(self.open))


def sanitize_html(fragment: str, rules: dict) -> str:
    """`fragment` with everything outside the allowlist removed (see the module doc)."""
    cleaner = _Cleaner(rules)
    cleaner.feed(fragment)
    return cleaner.result()


def text_to_html(text: str) -> str:
    """Plain text as HTML paragraphs: escaped, blank lines between paragraphs, line breaks kept."""
    paras = [p for p in text.replace("\r\n", "\n").split("\n\n") if p.strip()]
    return "".join("<p>" + html.escape(p.strip("\n"), quote=False).replace("\n", "<br>") + "</p>" for p in paras)
