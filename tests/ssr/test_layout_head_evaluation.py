"""A layout's ``HEAD`` is evaluated, in every form a page's ``HEAD`` supports.

The matrix here is taken from what ``docs/guides/head-management.md`` promises,
not from any one bug report. The promises are:

* a ``HEAD`` may be a string, a list of strings, or a callable taking loader
  data (``docs/guides/head-management.md``, "The ``HEAD`` variable");
* "site-wide JSON-LD or a critical-CSS ``<style>`` in a root ``layout.pyxl``'s
  ``HEAD`` reaches every page below it exactly as written";
* the layout tier sits below the page tier in the precedence table.

None of that said "only if you wrote a literal". It used to be true anyway: the
layout path read the compiler's static AST extraction and dropped everything
else — an f-string, a concatenation, ``json.dumps(...)``, a ``def HEAD(data)``
— with no warning, no log line and no build error, while the identical ``HEAD``
on a page rendered in full. Site-wide JSON-LD, the single most likely thing to
be built with ``json.dumps``, was exactly the shape that vanished.

Every test drives the real pipeline: real ``.pyxl`` sources, the real compiler,
the real registry walk and the real head merge. Only the Node render boundary
is stubbed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.requests import Request

from pyxle.devserver.settings import DevServerSettings
from pyxle.ssr.renderer import RenderResult
from pyxle.ssr.view import build_page_navigation_response, build_page_response

from .test_view import StubRenderer, _read_response_body

PLAIN_PAGE = (
    "import React from 'react';\n"
    "\n"
    "export default function Home() {\n"
    "    return <div>home</div>;\n"
    "}\n"
)

LAYOUT_COMPONENT = (
    "\n"
    "import React from 'react';\n"
    "\n"
    "export default function Layout({ children }) {\n"
    "    return <div>{children}</div>;\n"
    "}\n"
)


@pytest.fixture
def anyio_backend() -> str:  # pragma: no cover - fixture wiring
    return "asyncio"


@pytest.fixture
def settings(tmp_path: Path) -> DevServerSettings:
    project = tmp_path / "project"
    (project / "pages").mkdir(parents=True)
    (project / "public").mkdir()
    return DevServerSettings.from_project_root(project)


def _write(settings: DevServerSettings, relative: str, source: str) -> None:
    path = settings.pages_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _build(settings: DevServerSettings):
    """Compile the project and return its route table."""
    from pyxle.devserver.builder import build_once
    from pyxle.devserver.registry import load_metadata_registry
    from pyxle.devserver.routes import build_route_table

    build_once(settings)
    return build_route_table(load_metadata_registry(settings))


def _request(path: str = "/") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "query_string": b"",
            "headers": [],
            "server": ("localhost", 8000),
        }
    )


async def _render_html(settings: DevServerSettings, route_path: str = "/") -> str:
    """Render *route_path* through the ordinary buffered page pipeline."""
    page = _build(settings).find_page(route_path)
    assert page is not None

    renderer = StubRenderer()
    renderer.responses.append(RenderResult(html="<div>home</div>"))
    response = await build_page_response(
        request=_request(route_path),
        settings=settings,
        page=page,
        renderer=renderer,
        overlay=None,
    )
    assert response.status_code == 200
    return (await _read_response_body(response)).decode("utf-8")


# ---------------------------------------------------------------------------
# Every documented HEAD form, at layout level
# ---------------------------------------------------------------------------


LAYOUT_HEAD_FORMS = {
    "literal_list": "HEAD = ['<meta name=\"probe\" content=\"pass\" />']\n",
    "literal_string": "HEAD = '<meta name=\"probe\" content=\"pass\" />'\n",
    "implicit_concat": (
        "HEAD = (\n"
        "    '<meta name=\"probe\" '\n"
        "    'content=\"pass\" />'\n"
        ")\n"
    ),
    "fstring": 'SITE = "pass"\nHEAD = f\'<meta name="probe" content="{SITE}" />\'\n',
    "explicit_concat": (
        "HEAD = ['<meta name=\"probe\" content=\"pass\" />'] + "
        "['<meta name=\"probe2\" content=\"pass\" />']\n"
    ),
    "comprehension": (
        "HEAD = [f'<meta name=\"probe\" content=\"{v}\" />' for v in ['pass']]\n"
    ),
    "callable": (
        "def HEAD(data):\n"
        "    return ['<meta name=\"probe\" content=\"pass\" />']\n"
    ),
    "callable_returning_string": (
        "def HEAD(data):\n"
        "    return '<meta name=\"probe\" content=\"pass\" />'\n"
    ),
}


@pytest.mark.anyio
@pytest.mark.parametrize("form", sorted(LAYOUT_HEAD_FORMS))
async def test_every_head_form_reaches_the_document_from_a_layout(
    settings: DevServerSettings, form: str
) -> None:
    """The docs offer a string, a list, or a callable — with no "must be a
    literal" caveat anywhere. Each form has to arrive on the page below."""
    _write(settings, "layout.pyxl", LAYOUT_HEAD_FORMS[form] + LAYOUT_COMPONENT)
    _write(settings, "index.pyxl", PLAIN_PAGE)

    html = await _render_html(settings)

    assert '<meta name="probe" content="pass"/>' in html, (
        f"a layout's {form} HEAD never reached the document"
    )


@pytest.mark.anyio
async def test_a_layouts_computed_json_ld_reaches_every_page(
    settings: DevServerSettings,
) -> None:
    """The headline promise: "site-wide JSON-LD ... in a root layout.pyxl's HEAD
    reaches every page below it exactly as written".

    Site-wide JSON-LD is built with ``json.dumps``, so the value is a call
    result — precisely what a static AST read cannot see. Two pages at different
    depths, because "every page below it" is the claim.
    """
    _write(
        settings,
        "layout.pyxl",
        "import json\n"
        "\n"
        "SCHEMA = {'@context': 'https://schema.org', '@type': 'Organization', "
        "'name': 'Acme'}\n"
        "\n"
        "HEAD = [\n"
        "    '<script type=\"application/ld+json\">' + json.dumps(SCHEMA) + '</script>',\n"
        "    '<style>' + '.hero{color:red}' + '</style>',\n"
        "]\n" + LAYOUT_COMPONENT,
    )
    _write(settings, "index.pyxl", PLAIN_PAGE)
    _write(settings, "deep/nested/page.pyxl", PLAIN_PAGE)

    for route in ("/", "/deep/nested/page"):
        html = await _render_html(settings, route)
        assert '"@type": "Organization"' in html, f"JSON-LD missing on {route}"
        assert ".hero{color:red}" in html, f"critical CSS missing on {route}"


@pytest.mark.anyio
async def test_a_layouts_callable_head_receives_its_own_loader_data(
    settings: DevServerSettings,
) -> None:
    """A page's callable ``HEAD`` receives that page's loader data, so a
    layout's receives that layout's. Anything else would make the same syntax
    mean two different things depending on the filename."""
    _write(
        settings,
        "layout.pyxl",
        "@server\n"
        "async def layout_load(request):\n"
        "    return {'site_name': 'Acme'}\n"
        "\n"
        "def HEAD(data):\n"
        "    return [f'<meta name=\"site\" content=\"{data[\"site_name\"]}\" />']\n"
        + LAYOUT_COMPONENT,
    )
    _write(settings, "index.pyxl", PLAIN_PAGE)

    html = await _render_html(settings)

    assert '<meta name="site" content="Acme"/>' in html


@pytest.mark.anyio
async def test_a_callable_head_in_a_loaderless_layout_gets_an_empty_mapping(
    settings: DevServerSettings,
) -> None:
    """No loader is not an error — ``data.get(...)`` is the documented idiom
    there, so the callable must be handed a mapping rather than ``None``."""
    _write(
        settings,
        "layout.pyxl",
        "def HEAD(data):\n"
        "    return [f'<meta name=\"probe\" content=\"{data.get(\"x\", \"empty\")}\" />']\n"
        + LAYOUT_COMPONENT,
    )
    _write(settings, "index.pyxl", PLAIN_PAGE)

    html = await _render_html(settings)

    assert '<meta name="probe" content="empty"/>' in html


@pytest.mark.anyio
async def test_each_layout_in_a_chain_gets_its_own_loader_data(
    settings: DevServerSettings,
) -> None:
    """Two layouts, two loaders, two callables. Each must see its own data —
    handing both the merged chain data would let an outer layout's key silently
    change what an inner layout's head says."""
    _write(
        settings,
        "layout.pyxl",
        "@server\n"
        "async def root_load(request):\n"
        "    return {'who': 'root'}\n"
        "\n"
        "def HEAD(data):\n"
        "    return [f'<meta name=\"root\" content=\"{data[\"who\"]}\" />']\n"
        + LAYOUT_COMPONENT,
    )
    _write(
        settings,
        "section/layout.pyxl",
        "@server\n"
        "async def section_load(request):\n"
        "    return {'who': 'section'}\n"
        "\n"
        "def HEAD(data):\n"
        "    return [f'<meta name=\"section\" content=\"{data[\"who\"]}\" />']\n"
        + LAYOUT_COMPONENT,
    )
    _write(settings, "section/index.pyxl", PLAIN_PAGE)

    html = await _render_html(settings, "/section")

    assert '<meta name="root" content="root"/>' in html
    assert '<meta name="section" content="section"/>' in html


@pytest.mark.anyio
async def test_a_page_head_still_outranks_an_evaluated_layout_head(
    settings: DevServerSettings,
) -> None:
    """The precedence table puts the layout tier below the page tier. Making the
    layout tier evaluated must not promote it past the page."""
    _write(
        settings,
        "layout.pyxl",
        "SITE = 'Acme'\n"
        "HEAD = f'<title>{SITE}</title>'\n" + LAYOUT_COMPONENT,
    )
    _write(
        settings,
        "index.pyxl",
        "HEAD = ['<title>The page wins</title>']\n\n" + PLAIN_PAGE,
    )

    html = await _render_html(settings)

    assert "<title>The page wins</title>" in html
    assert "<title>Acme</title>" not in html


@pytest.mark.anyio
async def test_an_evaluated_layout_head_is_sanitised_like_every_other(
    settings: DevServerSettings,
) -> None:
    """Evaluation must not open a hole around the head sanitiser: a computed
    layout head is developer code, but its interpolated values may not be."""
    _write(
        settings,
        "layout.pyxl",
        "EVIL = '\\\" onload=\\\"alert(1)'\n"
        "HEAD = f'<meta name=\"probe\" content=\"{EVIL}\" />'\n" + LAYOUT_COMPONENT,
    )
    _write(settings, "index.pyxl", PLAIN_PAGE)

    html = await _render_html(settings)

    assert "onload" not in html
    assert 'name="probe"' in html


@pytest.mark.anyio
async def test_a_computed_layout_head_survives_client_navigation(
    settings: DevServerSettings,
) -> None:
    """A client-side navigation swaps the document head from this payload. If it
    resolved the layout chain differently from the first load, the site-wide
    tags would silently disappear on the second page a visitor opens."""
    _write(
        settings,
        "layout.pyxl",
        "import json\n"
        "\n"
        "HEAD = ['<script type=\"application/ld+json\">'"
        " + json.dumps({'@type': 'Organization'}) + '</script>']\n" + LAYOUT_COMPONENT,
    )
    _write(settings, "index.pyxl", PLAIN_PAGE)

    page = _build(settings).find_page("/")
    assert page is not None

    renderer = StubRenderer()
    renderer.responses.append(RenderResult(html="<div>home</div>"))
    response = await build_page_navigation_response(
        request=_request("/"),
        settings=settings,
        page=page,
        renderer=renderer,
        overlay=None,
    )

    payload = (await _read_response_body(response)).decode("utf-8")
    assert '@type' in payload and 'Organization' in payload


@pytest.mark.anyio
async def test_a_standalone_layout_still_stops_an_evaluated_head(
    settings: DevServerSettings,
) -> None:
    """``STANDALONE`` stops the chain. The evaluated channel has to stop at the
    same place as the literal one, or a section that opted out of the app shell
    starts inheriting its analytics snippet again."""
    _write(
        settings,
        "layout.pyxl",
        "APP = 'app'\nHEAD = f'<meta name=\"{APP}\" content=\"1\" />'\n" + LAYOUT_COMPONENT,
    )
    _write(
        settings,
        "public/layout.pyxl",
        "STANDALONE = True\n"
        "PUB = 'public'\n"
        "HEAD = f'<meta name=\"{PUB}\" content=\"1\" />'\n" + LAYOUT_COMPONENT,
    )
    _write(settings, "public/index.pyxl", PLAIN_PAGE)

    html = await _render_html(settings, "/public")

    assert 'name="public"' in html
    assert 'name="app"' not in html, (
        "a standalone section inherited the app shell's computed head"
    )


@pytest.mark.anyio
async def test_a_broken_layout_head_fails_loudly(
    settings: DevServerSettings, caplog: pytest.LogCaptureFixture
) -> None:
    """Silence is the failure mode this whole file exists to remove.

    A layout ``HEAD`` that cannot be evaluated takes the same route a broken
    page ``HEAD`` takes — a 500 and a logged server fault that names the layout
    file — instead of a 200 whose head is quietly missing the site's tags.
    """
    _write(
        settings,
        "layout.pyxl",
        "def HEAD(data):\n    return [object()]\n" + LAYOUT_COMPONENT,
    )
    _write(settings, "index.pyxl", PLAIN_PAGE)

    page = _build(settings).find_page("/")
    assert page is not None

    renderer = StubRenderer()
    renderer.responses.append(RenderResult(html="<div>home</div>"))

    with caplog.at_level("ERROR"):
        response = await build_page_response(
            request=_request(),
            settings=settings,
            page=page,
            renderer=renderer,
            overlay=None,
        )

    assert response.status_code == 500
    assert "layout.pyxl" in caplog.text, (
        "the failing layout was never named — the developer has nothing to go on"
    )


# ---------------------------------------------------------------------------
# A computed HEAD entry holding two elements: loud, but not fatal
# ---------------------------------------------------------------------------


TWO_IN_ONE_LAYOUT = (
    "SITE = 'FIRST-9001'\n"
    "HEAD = [\n"
    "    f'<meta name=\"twin-a\" content=\"{SITE}\" />"
    "<meta name=\"twin-b\" content=\"SECOND-9002\" />',\n"
    "]\n"
) + LAYOUT_COMPONENT


@pytest.fixture(autouse=True)
def _reset_head_warning_memo():
    """Each test starts with an empty report memo, so dedup state from one test
    cannot make another look silent."""
    from pyxle.ssr import view as ssr_view

    ssr_view._reported_discarded_head.clear()
    yield
    ssr_view._reported_discarded_head.clear()


@pytest.mark.anyio
async def test_a_computed_two_element_entry_warns_and_still_renders(
    settings: DevServerSettings, caplog: pytest.LogCaptureFixture
) -> None:
    """The value is only known at render time, so this cannot be a build error.
    It must not be a 500 either: a second `<meta>` appearing for one row of data
    would take the page down for exactly the visitors who reach that row, having
    passed every test. The tag is lost, the page is not, and the log says so.
    """
    _write(settings, "layout.pyxl", TWO_IN_ONE_LAYOUT)
    _write(settings, "index.pyxl", PLAIN_PAGE)

    with caplog.at_level("WARNING"):
        html = await _render_html(settings)

    assert 'content="FIRST-9001"' in html, "the surviving element was lost too"
    assert "twin-b" not in html, "the sanitiser's boundary moved"
    assert "layout.pyxl" in caplog.text, "the warning does not name the file"
    assert "twin-b" in caplog.text, "the warning does not say what was dropped"
    assert "Split it into separate list entries" in caplog.text


@pytest.mark.anyio
async def test_the_warning_does_not_repeat_on_every_render(
    settings: DevServerSettings, caplog: pytest.LogCaptureFixture
) -> None:
    """A computed HEAD is re-evaluated per request. One line per render is the
    noise that gets a warning filtered out and the bug ignored — which would put
    us back at silence by another route."""
    _write(settings, "layout.pyxl", TWO_IN_ONE_LAYOUT)
    _write(settings, "index.pyxl", PLAIN_PAGE)

    with caplog.at_level("WARNING"):
        for _ in range(8):
            await _render_html(settings)

    warnings = [r for r in caplog.records if "more than one element" in r.getMessage()]
    assert len(warnings) == 1, f"warned {len(warnings)} times across 8 renders"


def test_a_different_mistake_in_the_same_file_is_still_reported() -> None:
    """Deduplicating per file alone would hide the second bug in a file until
    the process restarted. The key carries the dropped content as well."""
    from pyxle.ssr import view as ssr_view

    ssr_view._warn_discarded_head_content(
        "layout.pyxl", ('<meta name="a" content="1" /><meta name="first" />',)
    )
    ssr_view._warn_discarded_head_content(
        "layout.pyxl", ('<meta name="a" content="1" /><meta name="second" />',)
    )

    assert len(ssr_view._reported_discarded_head) == 2


def test_the_report_memo_cannot_grow_without_bound() -> None:
    """If an app manages to vary the dropped content per request, the memo must
    not become a leak — and must not go permanently silent either."""
    from pyxle.ssr import view as ssr_view

    limit = ssr_view._DISCARDED_HEAD_REPORT_LIMIT
    for index in range(limit * 2 + 5):
        ssr_view._warn_discarded_head_content(
            "layout.pyxl", (f'<meta name="a" /><meta name="n{index}" />',)
        )

    assert len(ssr_view._reported_discarded_head) <= limit


@pytest.mark.anyio
async def test_a_page_level_computed_entry_is_reported_too(
    settings: DevServerSettings, caplog: pytest.LogCaptureFixture
) -> None:
    """Pages and layouts share one evaluation path, so they share the check."""
    _write(
        settings,
        "index.pyxl",
        "TITLE = 'Home'\n"
        "HEAD = [f'<title>{TITLE}</title><meta name=\"page-twin\" content=\"x\" />']\n\n"
        + PLAIN_PAGE,
    )

    with caplog.at_level("WARNING"):
        html = await _render_html(settings)

    assert "<title>Home</title>" in html
    assert "page-twin" not in html
    assert "index.pyxl" in caplog.text or "/" in caplog.text
    assert "page-twin" in caplog.text
