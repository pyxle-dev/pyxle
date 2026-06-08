from __future__ import annotations

import json
from pathlib import Path

from pyxle.devserver.builder import build_once
from pyxle.devserver.settings import DevServerSettings


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def create_project(tmp_path: Path) -> DevServerSettings:
    root = tmp_path / "project"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()
    return DevServerSettings.from_project_root(root)


def test_compose_layout_templates_generates_wrapped_module(tmp_path: Path) -> None:
    settings = create_project(tmp_path)

    write(
        settings.pages_dir / "layout.pyxl",
        """import React from 'react';\nimport { Slot } from 'pyxle/client';\n\nexport default function SiteLayout({ children }) {\n    return (\n        <div className=\"site-layout\">\n            <Slot name=\"hero\" fallback={<p>fallback</p>} />\n            {children}\n        </div>\n    );\n}\n\nexport const slots = {\n    hero: () => <p>site hero</p>,\n};\n""",
    )
    write(
        settings.pages_dir / "blog/template.pyxl",
        """import React from 'react';\n\nexport default function BlogTemplate({ children }) {\n    return <section className=\"blog-template\">{children}</section>;\n}\n\nexport const slots = {\n    hero: () => <p>blog hero</p>,\n};\n""",
    )
    write(
        settings.pages_dir / "blog/post.pyxl",
        """import React from 'react';\n\nexport default function BlogPost() {\n    return <article>Post</article>;\n}\n\nexport const slots = {\n    hero: () => <h1>Post hero</h1>,\n};\n""",
    )

    build_once(settings, force_rebuild=True)

    composed_path = settings.client_build_dir / "routes" / "blog" / "post.jsx"
    metadata_path = settings.metadata_build_dir / "pages" / "blog" / "post.json"

    assert composed_path.exists()
    composed_source = composed_path.read_text(encoding="utf-8")
    assert "SlotProvider" in composed_source
    assert "kind: 'layout'" in composed_source
    assert "kind: 'template'" in composed_source
    assert "function getModuleExport" in composed_source
    assert "PageModule?.slots" not in composed_source
    assert "getModuleExport(PageModule, 'slots')" in composed_source

    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert payload["client_path"] == "/routes/blog/post.jsx"
    assert payload["wrappers"] == [
        {"kind": "layout", "client_path": "/pages/layout.jsx"},
        {"kind": "template", "client_path": "/pages/blog/template.jsx"},
    ]


def test_composed_module_exposes_layout_loader_data_as_data_prop(tmp_path: Path) -> None:
    """A layout/template that declares its own ``@server`` loader receives that
    loader's result on the intuitive ``data`` prop (same as a page), not only
    the ``layoutData`` prop. The composition overrides ``data`` for wrapper
    components when layout-loader data is present, and falls back to the page's
    data for loader-less layouts (back-compat)."""
    settings = create_project(tmp_path)

    write(
        settings.pages_dir / "layout.pyxl",
        "import React from 'react';\nexport default function Layout({ children }) { return <div>{children}</div>; }\n",
    )
    write(
        settings.pages_dir / "blog" / "post.pyxl",
        "import React from 'react';\nexport default function BlogPost() { return <article>Post</article>; }\n",
    )

    build_once(settings, force_rebuild=True)

    composed = (settings.client_build_dir / "routes" / "blog" / "post.jsx").read_text(encoding="utf-8")
    # Wrapper components receive the layout-loader data on `data` when present.
    assert "props.layoutData !== undefined" in composed
    assert "wrapperProps.data = props.layoutData" in composed


def test_compose_layout_templates_handles_pages_without_slots(tmp_path: Path) -> None:
    settings = create_project(tmp_path)

    write(
        settings.pages_dir / "layout.pyxl",
        "import React from 'react';\nexport default function Layout({ children }) { return <div>{children}</div>; }\n",
    )
    write(
        settings.pages_dir / "blog" / "post.pyxl",
        "import React from 'react';\n\nexport default function BlogPost() {\n    return <article>Post</article>;\n}\n",
    )

    build_once(settings, force_rebuild=True)

    composed_path = settings.client_build_dir / "routes" / "blog" / "post.jsx"
    assert composed_path.exists()
    composed_source = composed_path.read_text(encoding="utf-8")

    assert "PageModule?.slots" not in composed_source
    assert "PageModule?.createSlots" not in composed_source
    assert "getModuleExport(PageModule, 'slots')" in composed_source
    assert "getModuleExport(PageModule, 'createSlots')" in composed_source


def test_compose_layout_templates_removes_wrappers_when_no_files(tmp_path: Path) -> None:
    settings = create_project(tmp_path)

    write(settings.pages_dir / "layout.pyxl", "import React from 'react';\nexport default function Layout({ children }) { return <div>{children}</div>; }")
    write(settings.pages_dir / "nested/template.pyxl", "import React from 'react';\nexport default function Template({ children }) { return <>{children}</>; }")
    write(settings.pages_dir / "nested/page.pyxl", "import React from 'react';\nexport default function Page() { return <p>nested</p>; }")

    build_once(settings, force_rebuild=True)

    composed_path = settings.client_build_dir / "routes" / "nested" / "page.jsx"
    metadata_path = settings.metadata_build_dir / "pages" / "nested" / "page.json"

    assert composed_path.exists()
    assert json.loads(metadata_path.read_text(encoding="utf-8"))["client_path"] == "/routes/nested/page.jsx"

    # Remove wrapper files and rebuild to ensure metadata resets.
    (settings.pages_dir / "layout.pyxl").unlink()
    (settings.pages_dir / "nested/template.pyxl").unlink()

    build_once(settings, force_rebuild=True)

    assert not composed_path.exists()
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert payload["client_path"] == "/pages/nested/page.jsx"
    assert "wrappers" not in payload