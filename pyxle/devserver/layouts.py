"""Utilities for composing nested layout/template wrappers."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence

from .settings import DevServerSettings


@dataclass(frozen=True)
class WrapperSpec:
    kind: str  # "layout" or "template"
    client_path: Path
    relative_path: Path


_LAYOUT_FILENAMES = {
    "layout": "layout",
    "template": "template",
}


def compose_layout_templates(settings: DevServerSettings) -> None:
    """Generate composed client modules for pages with layouts/templates."""

    metadata_dir = settings.metadata_build_dir / "pages"
    if not metadata_dir.exists():
        return

    for metadata_path, page_relative in _iter_page_metadata(metadata_dir):
        wrappers = _discover_wrappers(page_relative.parent, settings)
        _apply_wrappers(metadata_path, page_relative, wrappers, settings)


def _iter_page_metadata(metadata_dir: Path) -> Iterable[tuple[Path, Path]]:
    for metadata_path in sorted(metadata_dir.rglob("*.json")):
        relative_page = metadata_path.relative_to(metadata_dir).with_suffix(".pyxl")
        name = relative_page.name.lower()
        if name in {"layout.pyxl", "template.pyxl"}:
            continue
        yield metadata_path, relative_page


def _discover_wrappers(relative_dir: Path, settings: DevServerSettings) -> List[WrapperSpec]:
    """Every layout and template that wraps a page, **outermost first**.

    That is the order they nest in, and the order the caller composes them in:
    the root ``layout.pyxl`` comes first and the page's own directory last. (It
    used to say "closest first", which is backwards — ``_ancestor_dirs`` walks
    root-downwards and each wrapper is appended as it is found.)

    A wrapper declaring ``STANDALONE = True`` — a ``layout.pyxl`` **or** a
    ``template.pyxl`` — becomes the root of its own chain, so nothing above it
    applies. Mechanically it *discards* what has been collected rather than
    stopping the walk: everything found before it is an ancestor it has declared
    it does not want, and the walk still has to continue downwards to collect
    the wrappers between it and the page.

    The case is a section of a site that is not part of the app around it — a
    public status page inside an admin console, a print view, an embedded widget
    — where the alternative is teaching the outer layout to recognise each such
    section and render nothing, a conditional that grows a branch per child and
    puts knowledge of every one of them in the parent.
    """
    client_pages_root = settings.client_build_dir / "pages"
    ancestors = _ancestor_dirs(relative_dir)
    wrappers: List[WrapperSpec] = []

    for ancestor in ancestors:
        # Outermost first, because that is the order they nest in. So a
        # standalone layout *discards* what has been collected rather than
        # stopping the walk — everything before it is an ancestor it has
        # declared it does not want.
        if _is_standalone(ancestor, settings):
            wrappers.clear()

        for kind, base_name in _LAYOUT_FILENAMES.items():
            candidate = _client_component_path(client_pages_root, ancestor, base_name)
            if candidate.exists():
                try:
                    relative = candidate.relative_to(client_pages_root)
                except ValueError:
                    relative = candidate.name
                wrappers.append(WrapperSpec(kind=kind, client_path=candidate, relative_path=Path(relative)))

    return wrappers


def _is_standalone(ancestor: Path, settings: DevServerSettings) -> bool:
    """Whether a layout **or template** in *ancestor* is the root of its chain.

    Both kinds carry the directive -- the parser reads ``STANDALONE`` from any
    wrapper -- and both must be read here. This walk used to consult only
    ``layout.json``, while the head walk
    (:func:`~pyxle.devserver.registry.find_layout_head_contributions`) and the
    loader walk (:func:`~pyxle.devserver.registry.find_layout_loaders`) each
    iterate ``("layout.pyxl", "template.pyxl")``. So a ``template.pyxl``
    declaring ``STANDALONE`` had its head dropped and its ancestors' loaders
    skipped, while those same ancestors' **markup still wrapped the page** --
    the page rendered inside a layout whose loader never ran and whose head was
    discarded, which surfaces as a component reading its own loader data and
    finding nothing.
    """
    for base_name in _LAYOUT_FILENAMES.values():
        relative = Path(f"{base_name}.json")
        if ancestor != Path("."):
            relative = ancestor / relative
        metadata_path = settings.metadata_build_dir / "pages" / relative
        try:
            if json.loads(metadata_path.read_text()).get("standalone") is True:
                return True
        except (OSError, ValueError):
            continue
    return False


def _apply_wrappers(
    metadata_path: Path,
    page_relative: Path,
    wrappers: Sequence[WrapperSpec],
    settings: DevServerSettings,
) -> None:
    routes_root = settings.client_build_dir / "routes"
    page_rel_with_suffix = page_relative.with_suffix(".jsx")
    base_client_path = f"/pages/{page_rel_with_suffix.as_posix()}"
    composed_client_path = f"/routes/{page_rel_with_suffix.as_posix()}"

    composed_path = routes_root / page_rel_with_suffix

    if not wrappers:
        _update_metadata(metadata_path, base_client_path, None)
        _remove_composed_module(composed_path, routes_root)
        return

    _write_composed_module(
        output_path=composed_path,
        page_relative=page_relative,
        wrappers=wrappers,
        settings=settings,
    )
    _update_metadata(metadata_path, composed_client_path, wrappers)


def _write_composed_module(
    output_path: Path,
    page_relative: Path,
    wrappers: Sequence[WrapperSpec],
    settings: DevServerSettings,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    client_pages_root = settings.client_build_dir / "pages"
    page_target = client_pages_root / page_relative.with_suffix(".jsx")

    imports: List[str] = [
        "import React from 'react';",
        "import { SlotProvider, normalizeSlots, mergeSlotLayers } from 'pyxle/client/slot.jsx';",
        f"import * as PageModule from '{_relative_import(output_path, page_target)}';",
    ]

    statements: List[str] = [
        "function getModuleExport(module, exportName) {",
        "  if (!module || typeof module !== 'object') {",
        "    return undefined;",
        "  }",
        "  return module[exportName];",
        "}",
        "const PageComponent = typeof PageModule?.default === 'function'",
        "  ? PageModule.default",
        "  : function PyxlePageFallback() {",
        "      return null;",
        "    };",
        "const PAGE_STATIC_SLOTS = normalizeSlots(getModuleExport(PageModule, 'slots'));",
        "const PAGE_SLOT_EXPORT = getModuleExport(PageModule, 'createSlots');",
        "const PAGE_SLOT_FACTORY = typeof PAGE_SLOT_EXPORT === 'function'",
        "  ? PAGE_SLOT_EXPORT",
        "  : null;",
        "function resolvePageSlots(props) {",
        "  if (PAGE_SLOT_FACTORY) {",
        "    return normalizeSlots(PAGE_SLOT_FACTORY(props));",
        "  }",
        "  return PAGE_STATIC_SLOTS;",
        "}",
    ]

    wrapper_entries: List[str] = []
    for index, wrapper in enumerate(wrappers):
        module_alias = f"WrapperModule{index}"
        component_alias = f"WrapperComponent{index}"
        static_slots_alias = f"WrapperStaticSlots{index}"
        slots_factory_alias = f"WrapperSlotsFactory{index}"
        resolver_name = f"resolveWrapperSlots{index}"
        slots_value_alias = f"{module_alias}SlotsValue"
        slots_factory_value_alias = f"{module_alias}SlotsFactoryValue"

        imports.append(f"import * as {module_alias} from '{_relative_import(output_path, wrapper.client_path)}';")
        statements.extend(
            [
                f"const {component_alias} = typeof {module_alias}?.default === 'function'",
                f"  ? {module_alias}.default",
                "  : function PyxleAnonymousLayout(props) {",
                "      return React.createElement(React.Fragment, null, props?.children ?? null);",
                "    };",
            f"const {slots_value_alias} = getModuleExport({module_alias}, 'slots');",
            f"const {static_slots_alias} = normalizeSlots({slots_value_alias});",
            f"const {slots_factory_value_alias} = getModuleExport({module_alias}, 'createSlots');",
            f"const {slots_factory_alias} = typeof {slots_factory_value_alias} === 'function'",
            f"  ? {slots_factory_value_alias}",
                "  : null;",
                f"function {resolver_name}(props) {{",
                f"  if ({slots_factory_alias}) {{",
                f"    return normalizeSlots({slots_factory_alias}(props));",
                "  }",
                f"  return {static_slots_alias};",
                "}",
            ]
        )
        wrapper_entries.append(
            "  {"
            + f" kind: '{wrapper.kind}',"
            + f" component: {component_alias},"
            + f" resolveSlots: {resolver_name},"
            + (" reset: true," if wrapper.kind == "template" else " reset: false,")
            + " },"
        )

    wrappers_literal = ["const WRAPPERS = ["] + wrapper_entries + ["];"] if wrapper_entries else ["const WRAPPERS = [];"]

    statements.extend(
        wrappers_literal
        + [
            "function buildWrapperLayers(props) {",
            "  if (!WRAPPERS.length) {",
            "    return [];",
            "  }",
            "  return WRAPPERS.map((wrapper) => ({",
            "    kind: wrapper.kind,",
            "    reset: wrapper.reset,",
            "    slots: wrapper.resolveSlots(props),",
            "  }));",
            "}",
            "export default function PyxleWrappedPage(props) {",
            "  const wrapperLayers = buildWrapperLayers(props);",
            "  const mergedSlots = mergeSlotLayers(wrapperLayers, resolvePageSlots(props));",
            "  let tree = React.createElement(PageComponent, { ...props, slots: mergedSlots });",
            "  for (let i = WRAPPERS.length - 1; i >= 0; i -= 1) {",
            "    const wrapper = WRAPPERS[i];",
            "    // A layout/template that declares its own `@server` loader receives",
            "    // that loader's result on the intuitive `data` prop (same as a page).",
            "    // `layoutData` is only set when some layout in the chain has a loader,",
            "    // so loader-less layouts fall back to the page's data (back-compat).",
            "    const wrapperProps = { ...props, slots: mergedSlots };",
            "    if (props.layoutData !== undefined) { wrapperProps.data = props.layoutData; }",
            "    tree = React.createElement(wrapper.component, wrapperProps, tree);",
            "  }",
            "  return React.createElement(SlotProvider, { slots: mergedSlots }, tree);",
            "}",
            f"export * from '{_relative_import(output_path, page_target)}';",
        ]
    )

    content = "\n".join([*imports, "", *statements, ""])
    if output_path.exists() and output_path.read_text(encoding="utf-8") == content:
        return
    output_path.write_text(content, encoding="utf-8")


def _update_metadata(
    metadata_path: Path,
    client_path: str,
    wrappers: Sequence[WrapperSpec] | None,
) -> None:
    try:
        with metadata_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):  # pragma: no cover - defensive guard
        return

    changed = False
    if payload.get("client_path") != client_path:
        payload["client_path"] = client_path
        changed = True

    if wrappers:
        serialized = [
            {
                "kind": wrapper.kind,
                "client_path": f"/pages/{wrapper.relative_path.as_posix()}",
            }
            for wrapper in wrappers
        ]
        if payload.get("wrappers") != serialized:
            payload["wrappers"] = serialized
            changed = True
    elif "wrappers" in payload:
        payload.pop("wrappers", None)
        changed = True

    if not changed:
        return

    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _remove_composed_module(path: Path, routes_root: Path) -> None:
    if not path.exists():
        return
    path.unlink()
    _prune_empty_parents(path.parent, stop_at=routes_root)


def _prune_empty_parents(path: Path, *, stop_at: Path) -> None:
    current = path
    stop_at = stop_at.resolve()
    while True:
        if not current.exists() or not current.is_dir():
            break
        if any(current.iterdir()):
            break
        if current.resolve() == stop_at:
            break
        current.rmdir()
        current = current.parent


def _client_component_path(root: Path, ancestor: Path, base_name: str) -> Path:
    if ancestor == Path('.'):
        relative = Path(f"{base_name}.jsx")
    else:
        relative = ancestor / f"{base_name}.jsx"
    return root / relative


def _relative_import(from_file: Path, to_file: Path) -> str:
    rel = os.path.relpath(to_file, start=from_file.parent)
    if not rel.startswith('.'):
        rel = f"./{rel}"
    return Path(rel).as_posix()


def _ancestor_dirs(relative_dir: Path) -> List[Path]:
    parts = list(relative_dir.parts)
    ancestors: List[Path] = [Path('.')]
    for index in range(1, len(parts) + 1):
        ancestors.append(Path(*parts[:index]))
    return ancestors


__all__ = ["compose_layout_templates"]
