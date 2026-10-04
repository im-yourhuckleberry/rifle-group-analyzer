"""T1 tests for the UI shell: base.html renders the nav and blocks, and the web assets are offline-only."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jinja2 import DictLoader, Environment, FileSystemLoader, select_autoescape

from rga.config import Settings
from rga.web.app import create_app

WEB_DIR = Path(__file__).resolve().parents[2] / "src" / "rga" / "web"
TEMPLATES = WEB_DIR / "templates"
STATIC = WEB_DIR / "static"

NAV_HREFS = ["/", "/rifles", "/groups", "/analysis", "/compare", "/dope", "/settings"]


def _env() -> Environment:
    return Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))


def _nav_hrefs(html: str) -> list[str]:
    nav = re.search(r"<nav\b.*?</nav>", html, re.S)
    assert nav, "base.html has no <nav>"
    return re.findall(r'href="([^"]+)"', nav.group(0))


def _web_asset_files() -> list[Path]:
    files = [
        p
        for p in WEB_DIR.rglob("*")
        if p.is_file() and ("templates" in p.parts or "static" in p.parts)
    ]
    assert files, "no templates/static files found"
    return files


def test_base_renders_nav_links_for_every_static_page() -> None:
    html = _env().get_template("base.html").render()
    assert _nav_hrefs(html) == NAV_HREFS


def test_base_links_stylesheet_from_local_static() -> None:
    html = _env().get_template("base.html").render()
    assert 'href="/static/css/app.css"' in html


def test_stylesheet_and_api_module_exist() -> None:
    assert (STATIC / "css" / "app.css").stat().st_size > 0
    assert (STATIC / "js" / "api.js").stat().st_size > 0


def test_child_template_fills_content_and_scripts_blocks() -> None:
    child = (
        '{% extends "base.html" %}'
        "{% block title %}Child{% endblock %}"
        '{% block content %}<p id="c">body</p>{% endblock %}'
        '{% block scripts %}<script type="module" src="/static/js/x.js"></script>{% endblock %}'
    )
    env = Environment(
        loader=DictLoader(
            {"child.html": child, "base.html": (TEMPLATES / "base.html").read_text()}
        ),
        autoescape=True,
    )
    html = env.get_template("child.html").render()
    assert "<title>Child</title>" in html
    assert '<p id="c">body</p>' in html
    assert html.index('src="/static/js/x.js"') > html.index('id="c"')


def test_active_page_is_marked_in_nav() -> None:
    html = _env().get_template("base.html").render(active="rifles")
    assert re.search(r'<a href="/rifles" class="active" aria-current="page">', html)
    assert html.count('aria-current="page"') == 1


def test_user_supplied_title_is_autoescaped() -> None:
    env = Environment(
        loader=DictLoader(
            {
                "child.html": '{% extends "base.html" %}{% block title %}{{ t }}{% endblock %}',
                "base.html": (TEMPLATES / "base.html").read_text(),
            }
        ),
        autoescape=True,
    )
    html = env.get_template("child.html").render(t="<script>alert(1)</script>")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


XML_NAMESPACE_URIS = frozenset(
    {"http://www.w3.org/2000/svg", "http://www.w3.org/1999/xlink", "http://www.w3.org/1999/xhtml"}
)
_SCHEME = r"(?:[a-z][a-z0-9+.-]*:)?"
EXTERNAL_PATTERNS: dict[str, re.Pattern[str]] = {
    "absolute-url": re.compile(r"""(?:https?|wss?|ftp)://[^\s"'<>)\\]*""", re.I),
    "attribute-url": re.compile(
        rf"""\b(?:src|href|action|formaction|poster|data|srcset)\s*=\s*["']?\s*{_SCHEME}//""", re.I
    ),
    "css-url": re.compile(rf"""url\(\s*["']?\s*{_SCHEME}//""", re.I),
    "css-import": re.compile(rf"""@import\s+(?:url\(\s*)?["']?\s*{_SCHEME}//""", re.I),
    "js-import": re.compile(rf"""\bimport\b[^;\n]*["'`]\s*{_SCHEME}//""", re.I),
    "js-string-host": re.compile(r"""["'`]//[A-Za-z0-9\[]"""),
}


def external_references(text: str) -> list[tuple[str, str]]:
    """Return (pattern name, matched text) for every outbound reference; XML namespace URIs are exempt."""
    hits: list[tuple[str, str]] = []
    for name, pattern in EXTERNAL_PATTERNS.items():
        for match in pattern.finditer(text):
            if name == "absolute-url" and match.group(0) in XML_NAMESPACE_URIS:
                continue
            hits.append((name, match.group(0)))
    return hits


EXTERNAL_NEGATIVES = [
    ("absolute-url", '<link rel="stylesheet" href="https://cdn.example.com/x.css">'),
    ("absolute-url", 'fetch("http://example.com/api")'),
    ("absolute-url", 'new WebSocket("wss://example.com/live")'),
    ("absolute-url", "ftp://example.com/file"),
    ("absolute-url", '<svg xmlns="http://www.w3.org/2000/svg.evil">'),
    ("attribute-url", '<script src="//unpkg.com/htmx.org"></script>'),
    ("attribute-url", "<script src='//unpkg.com/htmx.org'></script>"),
    ("attribute-url", '<link href = "//fonts.example.com/css">'),
    ("attribute-url", "<script src=//cdn.example.com/a.js></script>"),
    ("attribute-url", '<form action="//evil.example/post">'),
    ("attribute-url", '<img src="https://x.example/a.png">'),
    (
        "attribute-url",
        '<svg xmlns="http://www.w3.org/2000/svg"><image href="http://www.w3.org/2000/svg">',
    ),
    ("attribute-url", 'a.href = "//evil.example/x"'),
    ("css-url", "body { background: url(//cdn.example.com/bg.png); }"),
    ("css-url", "body { background: url( '//cdn.example.com/bg.png' ); }"),
    ("css-url", '@font-face { src: url("//fonts.example.com/f.woff2"); }'),
    ("css-import", "@import url(//fonts.example.com/x.css);"),
    ("css-import", '@import "//fonts.example.com/x.css";'),
    ("css-import", "@import '//fonts.example.com/x.css';"),
    ("js-import", 'import { x } from "//cdn.example.com/m.js";'),
    ("js-import", 'const m = await import("//cdn.example.com/m.js");'),
    ("js-string-host", 'fetch("//evil.example/steal")'),
    ("js-string-host", "new URL('//evil.example/x')"),
]

EXTERNAL_CLEAN = [
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"></svg>',
    'document.createElementNS("http://www.w3.org/2000/svg", "circle")',
    '<a href="/rifles">Rifles</a>',
    '<link rel="stylesheet" href="/static/css/app.css">',
    "body { background: url(/static/img/bg.png); }",
    "// a line comment about //hosts",
    "/* comment with // inside */",
    'if (path.startsWith("//")) throw new Error("bad");',
    '<script type="module" src="/static/js/x.js"></script>',
    "const ratio = a / b; // trailing comment",
]


@pytest.mark.parametrize(("pattern", "snippet"), EXTERNAL_NEGATIVES, ids=lambda v: str(v)[:48])
def test_no_cdn_check_flags_each_external_pattern(pattern: str, snippet: str) -> None:
    assert pattern in {name for name, _ in external_references(snippet)}


def test_every_external_pattern_has_a_failing_negative_example() -> None:
    covered = {pattern for pattern, _ in EXTERNAL_NEGATIVES}
    assert covered == set(EXTERNAL_PATTERNS)


@pytest.mark.parametrize("snippet", EXTERNAL_CLEAN)
def test_no_cdn_check_allows_namespaces_local_paths_and_comments(snippet: str) -> None:
    assert external_references(snippet) == []


@pytest.mark.parametrize("path", _web_asset_files(), ids=lambda p: str(p.relative_to(WEB_DIR)))
def test_no_external_urls_in_templates_or_static(path: Path) -> None:
    assert external_references(path.read_text(encoding="utf-8")) == [], (
        f"external reference in {path}"
    )


def test_no_cdn_check_catches_injection_into_the_real_shell_files() -> None:
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    assert external_references(base) == [] and external_references(css) == []
    injected_html = base.replace("</head>", '<script src="//unpkg.com/htmx.org"></script></head>')
    assert "attribute-url" in {n for n, _ in external_references(injected_html)}
    injected_css = css + "\n@import url(//fonts.example.com/x.css);\n"
    assert "css-import" in {n for n, _ in external_references(injected_css)}


def test_api_js_never_uses_innerhtml() -> None:
    source = (STATIC / "js" / "api.js").read_text(encoding="utf-8")
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write", source)


def test_api_js_exports_the_three_wrappers() -> None:
    source = (STATIC / "js" / "api.js").read_text(encoding="utf-8")
    for name in ("apiGet", "apiSend", "apiUpload"):
        assert re.search(rf"export\s+(async\s+)?function\s+{name}\s*\(", source), name


def test_api_js_error_contract_and_same_origin_guards() -> None:
    source = (STATIC / "js" / "api.js").read_text(encoding="utf-8")
    assert "export class ApiError" in source
    assert "this.detail" in source and "this.code" in source
    assert 'credentials: "same-origin"' in source
    assert (
        '"Content-Type": "application/json"' in source
        or 'headers["Content-Type"] = "application/json"' in source
    )
    assert 'startsWith("//")' in source
    assert "if (!response.ok) throw" in source


def test_api_js_upload_does_not_force_a_content_type() -> None:
    source = (STATIC / "js" / "api.js").read_text(encoding="utf-8")
    upload = source[source.index("export function apiUpload") :]
    assert "Content-Type" not in upload
    assert "FormData" in upload


# --- the Settings tab and the Offline/Online mode switch (B-51, B-60) --------------------------

ONLINE_CAPTION = "Online mode — coming later"
STATIC_PAGES = ["/", "/rifles", "/analysis", "/compare", "/dope", "/settings"]


class _StartTags(HTMLParser):
    """Collects (tag, attributes) for every start tag, in document order."""

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))


def _start_tags(fragment: str) -> list[tuple[str, dict[str, str | None]]]:
    parser = _StartTags()
    parser.feed(fragment)
    return parser.tags


def _mode_switch(html: str) -> str:
    """The mode-switch element's markup (it contains no nested div)."""
    found = re.findall(r'<div class="mode-switch".*?</div>', html, re.S)
    assert len(found) == 1, f"expected exactly one mode switch, found {len(found)}"
    return found[0]


def _radios(html: str) -> dict[str, dict[str, str | None]]:
    return {
        attrs["value"] or "": attrs
        for tag, attrs in _start_tags(_mode_switch(html))
        if tag == "input" and attrs.get("type") == "radio"
    }


def _header(html: str) -> str:
    found = re.search(r"<header\b.*?</header>", html, re.S)
    assert found, "page has no <header>"
    return found.group(0)


def _base_html(active: str | None = None) -> str:
    return _env().get_template("base.html").render(active=active)


MODE_SWITCH_MARKERS = ("mode-switch", "mode-option", "ui-mode", "mode-online-note")


def test_the_header_no_longer_has_the_mode_switch() -> None:
    html = _base_html()
    assert not any(marker in html for marker in MODE_SWITCH_MARKERS)
    assert not re.search(r"<input\b", html)
    assert {tag for tag, _ in _start_tags(_header(html))} == {"header", "a", "nav"}


def test_settings_is_the_last_navigation_link_and_is_marked_when_active() -> None:
    html = _base_html(active="settings")
    assert _nav_hrefs(html) == NAV_HREFS
    assert NAV_HREFS[-1] == "/settings"
    assert re.search(r'<a href="/settings" class="active" aria-current="page">Settings</a>', html)
    assert html.count('aria-current="page"') == 1
    assert 'href="/settings" class="active"' not in _base_html(active="rifles")


@pytest.fixture
def pages_client(tmp_path: Path):
    with TestClient(
        create_app(Settings(data_dir=tmp_path / "data")), base_url="http://127.0.0.1"
    ) as client:
        yield client


@pytest.mark.parametrize("page", STATIC_PAGES)
def test_every_navigation_page_links_to_settings_and_has_no_mode_switch_in_its_header(
    pages_client: TestClient, page: str
) -> None:
    response = pages_client.get(page)
    assert response.status_code == 200
    assert _nav_hrefs(response.text) == NAV_HREFS
    header = _header(response.text)
    assert 'href="/settings"' in header
    assert not any(marker in header for marker in MODE_SWITCH_MARKERS)


_MACRO_TAG = re.compile(r"\{%-?\s*(macro|endmacro)\b[^%]*?-?%\}")
_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.S)
_IMPORT_TAG = re.compile(r"\{%-?\s*(?:from|import)\b[^%]*?-?%\}")


def _is_macro_only_partial(source: str) -> bool:
    """True for a template that only defines macros (imported by pages, never rendered itself).

    Structural test: at least one `{% macro %}` block, no `{% extends %}`, and nothing at the top
    level except macro blocks, Jinja comments, import tags and whitespace (no markup, no block).
    """
    if re.search(r"\{%-?\s*extends\b", source):
        return False
    depth, start, saw_macro, outside = 0, 0, False, []
    for tag in _MACRO_TAG.finditer(source):
        if tag.group(1) == "macro":
            if depth == 0:
                outside.append(source[start : tag.start()])
                saw_macro = True
            depth += 1
        else:
            depth -= 1
            if depth < 0:
                return False
            if depth == 0:
                start = tag.end()
    if depth != 0 or not saw_macro:
        return False
    outside.append(source[start:])
    rest = _IMPORT_TAG.sub("", _JINJA_COMMENT.sub("", "".join(outside)))
    return rest.strip() == ""


def _templates_that_are_neither_pages_nor_macro_partials(templates: Path) -> list[str]:
    offenders = []
    for path in sorted(templates.glob("*.html")):
        source = path.read_text(encoding="utf-8")
        if path.name == "base.html" or '{% extends "base.html" %}' in source:
            continue
        if not _is_macro_only_partial(source):
            offenders.append(path.name)
    return offenders


MACRO_ONLY = [
    "{% macro a(x) %}<p>{{ x }}</p>{% endmacro %}",
    '{# partial #}\n{% from "other.html" import b %}\n{% macro a(x) %}\n<p>{{ x }}</p>\n{% endmacro %}\n\n'
    "{%- macro c() -%}c{%- endmacro -%}\n",
    "{% macro outer() %}{% macro inner() %}i{% endmacro %}{{ inner() }}{% endmacro %}",
]
NOT_MACRO_ONLY = [
    "<html><body>a real page with no base</body></html>",
    "",
    "{# only a comment #}",
    '{% extends "base.html" %}{% macro a() %}x{% endmacro %}',
    "{% macro a() %}x{% endmacro %}<p>stray markup after the macro</p>",
    "<p>stray markup before</p>{% macro a() %}x{% endmacro %}",
    "{% macro a() %}x{% endmacro %}{% block content %}b{% endblock %}",
    "{% macro a() %}x{% endmacro %}{{ a() }}",
    "{% macro a() %}x",
    "{% endmacro %}",
]


@pytest.mark.parametrize("source", MACRO_ONLY)
def test_a_template_made_only_of_macros_is_a_partial(source: str) -> None:
    assert _is_macro_only_partial(source)


@pytest.mark.parametrize("source", NOT_MACRO_ONLY)
def test_anything_else_is_not_mistaken_for_a_macro_partial(source: str) -> None:
    assert not _is_macro_only_partial(source)


def test_a_real_page_without_base_and_a_macro_file_with_stray_content_are_both_reported(
    tmp_path: Path,
) -> None:
    (tmp_path / "base.html").write_text("base", encoding="utf-8")
    (tmp_path / "page.html").write_text(
        '{% extends "base.html" %}{% block content %}x{% endblock %}'
    )
    (tmp_path / "partial.html").write_text("{% macro a() %}x{% endmacro %}")
    assert _templates_that_are_neither_pages_nor_macro_partials(tmp_path) == []
    (tmp_path / "standalone.html").write_text("<html><body>no base</body></html>")
    (tmp_path / "leaky.html").write_text("{% macro a() %}x{% endmacro %}<p>stray</p>")
    assert _templates_that_are_neither_pages_nor_macro_partials(tmp_path) == [
        "leaky.html",
        "standalone.html",
    ]


def test_every_page_template_extends_base_and_only_settings_holds_the_mode_switch() -> None:
    assert _templates_that_are_neither_pages_nor_macro_partials(TEMPLATES) == []
    holders = [
        p.name for p in TEMPLATES.glob("*.html") if "ui-mode" in p.read_text(encoding="utf-8")
    ]
    assert holders == ["settings.html"]


def test_base_template_gained_no_url_script_or_form() -> None:
    html = _base_html()
    references = set(re.findall(r'(?:href|src|action)="([^"]*)"', html))
    assert references == {"/static/css/app.css", *NAV_HREFS}
    assert not re.search(r"<(?:form|script)\b", html, re.I)
    assert external_references(html) == []


def test_mode_switch_has_no_script_route_or_setting_behind_it() -> None:
    needles = ("ui-mode", "mode-switch", "mode-option", "mode-online")
    js = [
        p.name
        for p in (STATIC / "js").glob("*.js")
        if any(n in p.read_text(encoding="utf-8") for n in needles)
    ]
    assert js == []
    src = WEB_DIR.parents[1]
    python = [
        str(p.relative_to(src))
        for p in src.rglob("*.py")
        if any(n in p.read_text(encoding="utf-8") for n in (*needles, ONLINE_CAPTION))
    ]
    assert python == []


def test_stylesheet_greys_out_the_unavailable_mode() -> None:
    css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    rule = re.search(r"\.mode-option\.unavailable\s*\{([^}]*)\}", css)
    assert rule and "opacity" in rule.group(1) and "cursor: not-allowed" in rule.group(1)
    assert re.search(r"\.mode-option\.current\s*\{", css)
