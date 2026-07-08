"""Production build pipeline for Pyxle projects."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

from pyxle.devserver.builder import BuildSummary, build_once
from pyxle.devserver.registry import MetadataRegistry, build_metadata_registry
from pyxle.devserver.settings import DevServerSettings
from pyxle.devserver.tailwind import detect_postcss_config


@dataclass(frozen=True, slots=True)
class BuildResult:
    """Outcome of a full production build."""

    dist_dir: Path
    client_dir: Path
    server_dir: Path
    metadata_dir: Path
    public_dir: Path
    client_manifest_path: Path | None
    page_manifest: Dict[str, Any]
    page_manifest_path: Path | None
    summary: BuildSummary
    registry: MetadataRegistry


def run_build(
    settings: DevServerSettings,
    *,
    logger: Any = None,
    dist_dir: Path | None = None,
    force_rebuild: bool = True,
) -> BuildResult:
    """Execute the full production build pipeline.

    Steps:
    1. Compile all .pyxl pages and copy API modules into .pyxle-build
    2. Run ``npm run build`` (Vite + Tailwind) to produce hashed client bundles
    3. Copy server modules, metadata, public assets, and client bundles to dist/
    4. Generate a page-manifest.json mapping routes to their assets
    """
    resolved_dist = dist_dir or (settings.project_root / "dist")
    resolved_dist = resolved_dist.resolve()

    _log(logger, "info", "Compiling sources")
    summary = build_once(settings, force_rebuild=force_rebuild)

    _log(logger, "info", "Running npm build")
    _run_npm_build(settings.project_root, logger, settings=settings)

    vite_manifest = _load_vite_manifest(settings)

    _log(logger, "info", f"Assembling production artifacts in {resolved_dist}")
    _prepare_dist(settings, resolved_dist)

    registry = build_metadata_registry(settings)
    page_manifest = _build_page_manifest(settings, registry, vite_manifest=vite_manifest)

    page_manifest_path = resolved_dist / "page-manifest.json"
    page_manifest_path.write_text(
        json.dumps(page_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    client_manifest_path = _copy_client_manifest(settings, resolved_dist)

    return BuildResult(
        dist_dir=resolved_dist,
        client_dir=resolved_dist / "client",
        server_dir=resolved_dist / "server",
        metadata_dir=resolved_dist / "metadata",
        public_dir=resolved_dist / "public",
        client_manifest_path=client_manifest_path,
        page_manifest=page_manifest,
        page_manifest_path=page_manifest_path,
        summary=summary,
        registry=registry,
    )


def _log(logger: Any, level: str, message: str) -> None:
    if logger is None:
        return
    fn = getattr(logger, level, None)
    if callable(fn):
        fn(message)


def _package_json_has_script(project_root: Path, script: str) -> bool:
    """Return ``True`` when the project's ``package.json`` declares *script*.

    Reading the manifest lets the build avoid invoking ``npm run <script>`` for
    a script that doesn't exist — which exits non-zero and would otherwise log a
    misleading failure warning on every build of a modern (Vite-managed CSS)
    scaffold. A missing or unreadable manifest is treated as "no such script".
    """
    try:
        data = json.loads((project_root / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    scripts = data.get("scripts")
    return isinstance(scripts, dict) and script in scripts


def _run_npm_build(project_root: Path, logger: Any, *, settings: DevServerSettings) -> None:
    package_json = project_root / "package.json"
    if not package_json.exists():
        _log(logger, "warning", "No package.json found; skipping npm build")
        return

    # Step 1: Run the standalone Tailwind CSS build (legacy v3 path) only when
    # the project actually declares a ``build:css`` script *and* has no PostCSS
    # config. Modern scaffolds (Tailwind v4 via ``@tailwindcss/vite``, or no
    # Tailwind) let Vite own CSS during the bundle step below and declare no
    # ``build:css`` script — so this is skipped, keeping the build output quiet
    # instead of logging a misleading "script failed" warning for a script that
    # never existed. When PostCSS is wired up, Vite runs CSS through it during
    # the bundle step, so ``build:css`` would be duplicate work — also skipped.
    if detect_postcss_config(project_root) is None and _package_json_has_script(
        project_root, "build:css"
    ):
        _run_npm_script(project_root, "build:css", logger, required=False)

    # Step 2: Run Vite build with explicit --config pointing to
    # the generated config inside .pyxle-build/client/.  The scaffold's
    # default "vite build" would fail because it looks for vite.config.js
    # and index.html in the project root, but Pyxle generates them inside
    # the client build directory.
    vite_config = settings.client_build_dir / "vite.config.js"
    if not vite_config.exists():
        raise RuntimeError(
            f"Vite config not found at {vite_config}; "
            "ensure sources are compiled before running the build"
        )

    # Set PYXLE_VITE_BASE so Vite generates absolute asset URLs
    # matching the static mount path.  The client build dir's
    # Vite output lands in a ``dist/`` sub-directory which is then
    # served under ``/client/``.
    env = dict(os.environ)
    env.setdefault("PYXLE_VITE_BASE", "/client/dist/")

    try:
        subprocess.run(
            ["npx", "vite", "build", "--config", str(vite_config), "--manifest"],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
            env=env,
            start_new_session=True,
        )
    except FileNotFoundError:
        _log(logger, "warning", "npx/vite not found; skipping client build")
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else ""
        raise RuntimeError(f"Vite build failed (exit {exc.returncode}): {stderr}") from exc


def _run_npm_script(project_root: Path, script: str, logger: Any, *, required: bool = True) -> None:
    """Run a single npm script.  If *required* is False, silently skip on failure."""
    try:
        subprocess.run(
            ["npm", "run", script],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
            start_new_session=True,
        )
    except FileNotFoundError:
        if required:
            raise
        _log(logger, "warning", f"npm not found; skipping '{script}' script")
    except subprocess.CalledProcessError as exc:
        if required:
            stderr = exc.stderr.strip() if exc.stderr else ""
            raise RuntimeError(f"npm script '{script}' failed (exit {exc.returncode}): {stderr}") from exc
        _log(logger, "warning", f"npm script '{script}' failed; continuing")


def _prepare_dist(settings: DevServerSettings, dist_dir: Path) -> None:
    server_dest = dist_dir / "server"
    metadata_dest = dist_dir / "metadata"
    public_dest = dist_dir / "public"
    client_dest = dist_dir / "client"

    for dest in (server_dest, metadata_dest, client_dest):
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True, exist_ok=True)

    # Copy server modules
    if settings.server_build_dir.exists():
        shutil.copytree(settings.server_build_dir, server_dest, dirs_exist_ok=True)

    # Copy metadata
    if settings.metadata_build_dir.exists():
        shutil.copytree(settings.metadata_build_dir, metadata_dest, dirs_exist_ok=True)

    # Copy public assets
    if settings.public_dir.exists():
        if public_dest.exists():
            shutil.rmtree(public_dest)
        shutil.copytree(settings.public_dir, public_dest)

    # Copy client build output (Vite puts assets in .pyxle-build/client/)
    if settings.client_build_dir.exists():
        shutil.copytree(settings.client_build_dir, client_dest, dirs_exist_ok=True)


def _load_vite_manifest(settings: DevServerSettings) -> Dict[str, Any] | None:
    """Load the Vite build manifest generated by ``--manifest``."""
    candidates = (
        settings.client_build_dir / "dist" / ".vite" / "manifest.json",
        settings.client_build_dir / "dist" / "manifest.json",
    )
    for candidate in candidates:
        if candidate.exists():
            return json.loads(candidate.read_text(encoding="utf-8"))
    return None


def _collect_css_from_vite_entry(
    vite_manifest: Dict[str, Any],
    entry_key: str,
) -> list[str]:
    """Transitively collect every CSS asset reachable from a Vite manifest entry.

    Vite's `manifest.json` stores each chunk's CSS dependencies on the chunk
    that directly imports the stylesheet. When a stylesheet is imported from
    a layout or shared component (not from the page file itself), the CSS
    ends up on the layout/shared chunk and only the layout's `imports` chain
    connects it back to the page. This walker follows the `imports` chain of
    a page entry, deduplicates, and returns the union of every reachable
    chunk's `css` array so the SSR template can emit one `<link>` tag per
    stylesheet regardless of where the import originated.

    Order is preserved: the direct entry's CSS appears first, followed by
    CSS from imports in depth-first order. Duplicates are removed while
    preserving first-seen order.
    """

    collected: list[str] = []
    seen: set[str] = set()
    visited_keys: set[str] = set()
    stack: list[str] = [entry_key]

    while stack:
        key = stack.pop(0)
        if key in visited_keys:
            continue
        visited_keys.add(key)

        entry = vite_manifest.get(key)
        if not isinstance(entry, dict):
            continue

        css_list = entry.get("css")
        if isinstance(css_list, list):
            for asset in css_list:
                if isinstance(asset, str) and asset not in seen:
                    seen.add(asset)
                    collected.append(asset)

        imports = entry.get("imports")
        if isinstance(imports, list):
            for imported_key in imports:
                if isinstance(imported_key, str) and imported_key not in visited_keys:
                    stack.append(imported_key)

    return collected


def _collect_js_imports_from_vite_entry(
    vite_manifest: Dict[str, Any],
    entry_key: str,
) -> list[str]:
    """Collect the JS chunk files an entry statically imports (transitively).

    The entry's own file is loaded via ``<script type="module">`` at the end of
    the body; its imported chunks would otherwise be discovered only after the
    browser parses the entry. Returning them here lets the SSR template emit a
    ``<link rel="modulepreload">`` for each, so they download in parallel with
    the entry. Files are de-duplicated, preserving first-seen (import) order.
    """
    collected: list[str] = []
    seen: set[str] = set()
    visited_keys: set[str] = {entry_key}

    entry = vite_manifest.get(entry_key)
    stack: list[str] = []
    if isinstance(entry, dict):
        first = entry.get("imports")
        if isinstance(first, list):
            stack = [k for k in first if isinstance(k, str)]

    while stack:
        key = stack.pop(0)
        if key in visited_keys:
            continue
        visited_keys.add(key)

        chunk = vite_manifest.get(key)
        if not isinstance(chunk, dict):
            continue

        chunk_file = chunk.get("file")
        if isinstance(chunk_file, str) and chunk_file and chunk_file not in seen:
            seen.add(chunk_file)
            collected.append(chunk_file)

        imports = chunk.get("imports")
        if isinstance(imports, list):
            for imported_key in imports:
                if isinstance(imported_key, str) and imported_key not in visited_keys:
                    stack.append(imported_key)

    return collected


def _build_page_manifest(
    settings: DevServerSettings,
    registry: MetadataRegistry,
    *,
    vite_manifest: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    manifest: Dict[str, Any] = {}
    for page in registry.pages:
        client_file = page.client_asset_path
        css_assets: list[str] = []
        js_imports: list[str] = []

        if vite_manifest is not None:
            vite_key = client_file.lstrip("/")
            vite_entry = vite_manifest.get(vite_key)
            if isinstance(vite_entry, dict):
                # Vite manifest paths are relative to Vite's outDir
                # (.pyxle-build/client/dist/), which gets copied as
                # dist/client/dist/.  Prefix with "dist/" so the
                # static file middleware resolves the correct path
                # under dist/client/.
                vite_file = vite_entry.get("file", "")
                if vite_file:
                    client_file = f"dist/{vite_file}"

                # Walk the imports chain so CSS imported from layouts or
                # shared component chunks still lands on this page's entry.
                vite_css = _collect_css_from_vite_entry(vite_manifest, vite_key)
                css_assets = [f"dist/{c}" for c in vite_css]

                # The JS chunks the entry imports — preloaded by the template.
                vite_js = _collect_js_imports_from_vite_entry(vite_manifest, vite_key)
                js_imports = [f"dist/{j}" for j in vite_js]

        entry: Dict[str, Any] = {
            "client": {
                "file": client_file,
                "imports": js_imports if vite_manifest is not None else [],
                "css": css_assets,
            },
            "server": {
                "file": page.server_asset_path,
                "module_key": page.module_key,
            },
        }
        if page.loader_name:
            entry["loader"] = {
                "name": page.loader_name,
                "line": page.loader_line,
            }
        if page.websocket_name:
            entry["websocket"] = {
                "name": page.websocket_name,
                "line": page.websocket_line,
            }
        if page.head_elements:
            entry["head"] = list(page.head_elements)
        if page.head_jsx_blocks:
            entry["head_jsx_blocks"] = list(page.head_jsx_blocks)

        manifest[page.route_path] = entry
        for alias in page.alternate_route_paths:
            manifest[alias] = entry

    for api in registry.apis:
        manifest[api.route_path] = {
            "type": "api",
            "server": {
                "file": api.source_relative_path.as_posix(),
                "module_key": api.module_key,
            },
        }

    return manifest


def _copy_client_manifest(settings: DevServerSettings, dist_dir: Path) -> Path | None:
    # Vite outputs its manifest inside the build output directory (dist/)
    candidates = (
        settings.client_build_dir / "dist" / ".vite" / "manifest.json",
        settings.client_build_dir / "dist" / "manifest.json",
        settings.client_build_dir / ".vite" / "manifest.json",
        settings.client_build_dir / "manifest.json",
    )
    vite_manifest = next((c for c in candidates if c.exists()), None)
    if vite_manifest is None:
        return None

    dest = dist_dir / "client" / "manifest.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(vite_manifest, dest)
    return dest
