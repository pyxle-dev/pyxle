"""Implementation of the ``pyxle init`` command."""

from __future__ import annotations

import json
import re
import secrets
from pathlib import Path
from typing import Mapping

import typer

from pyxle import __version__

from .assets import default_favicon_bytes
from .logger import ConsoleLogger
from .scaffold import (
    FilesystemWriter,
    InvalidImportAlias,
    InvalidProjectName,
    validate_import_alias,
    validate_project_name,
)
from .templates import ScaffoldingTemplate, TemplateRegistry

SUPPORTED_TEMPLATES = {"default"}

DEFAULT_IMPORT_ALIAS = "@/*"

_MAJOR_MINOR_RE = re.compile(r"^(\d+)\.(\d+)")


def framework_requirement(version: str) -> str:
    """Return the ``pyxle-framework`` requirement line for a scaffolded project.

    The specifier is derived from the *running* framework version so a scaffold
    never pins an older release than the CLI that generated it: running
    ``0.7.0`` emits ``pyxle-framework>=0.7.0,<0.8`` (current version up to, but
    excluding, the next minor). When the version metadata is unavailable —
    e.g. an uninstalled source checkout reports ``"unknown"`` — the requirement
    is left unpinned rather than emitting an unsatisfiable specifier.
    """

    match = _MAJOR_MINOR_RE.match(version)
    if match is None:
        return "pyxle-framework"
    major, minor = int(match.group(1)), int(match.group(2))
    return f"pyxle-framework>={version},<{major}.{minor + 1}"


# Extra ``package.json`` dependency lines injected when a feature is selected.
# Each entry is emitted verbatim after the always-present dependencies, so it
# starts with the leading comma + newline that keeps the JSON valid.
_TAILWIND_DEV_DEPENDENCIES = (
    ',\n    "@tailwindcss/vite": "^4.1.0"'
    ',\n    "tailwindcss": "^4.1.0"'
)
_SHADCN_RUNTIME_DEPENDENCIES = (
    ',\n    "class-variance-authority": "^0.7.1"'
    ',\n    "clsx": "^2.1.1"'
    ',\n    "lucide-react": "^0.400.0"'
    ',\n    "tailwind-merge": "^3.0.0"'
)


def _alias_prefix(import_alias: str) -> str:
    """Return the bare prefix of an import alias (``@`` for ``@/*``)."""

    return import_alias[: -len("/*")]


def build_template_registry(
    *,
    tailwind: bool,
    shadcn: bool,
) -> TemplateRegistry:
    """Compose the scaffold template set for the selected feature flags.

    Files that do not depend on a choice are always registered. Tailwind and
    shadcn/ui pull in their own files (and swap the starter page + stylesheet)
    only when selected, so a project never ships configuration it does not use.
    """

    registry = TemplateRegistry()
    # Always-present files.
    registry.register(".gitignore", ScaffoldingTemplate(".gitignore"))
    registry.register("README.md", ScaffoldingTemplate("README.md"))
    registry.register("AGENTS.md", ScaffoldingTemplate("AGENTS.md"))
    registry.register("package.json", ScaffoldingTemplate("package.json"))
    registry.register("requirements.txt", ScaffoldingTemplate("requirements.txt"))
    registry.register("pyxle.config.json", ScaffoldingTemplate("pyxle.config.json"))
    registry.register("jsconfig.json", ScaffoldingTemplate("jsconfig.json"))
    registry.register("vite.config.js", ScaffoldingTemplate("vite.config.js"))
    registry.register("pages/layout.pyxl", ScaffoldingTemplate("pages/layout.pyxl"))
    registry.register("pages/api/pulse.py", ScaffoldingTemplate("pages/api/pulse.py"))
    registry.register(
        "public/branding/pyxle-mark.svg",
        ScaffoldingTemplate("public/branding/pyxle-mark.svg"),
    )

    if tailwind:
        registry.register("pages/index.pyxl", ScaffoldingTemplate("pages/index.tailwind.pyxl"))
        if shadcn:
            registry.register(
                "pages/styles/app.css", ScaffoldingTemplate("pages/styles/app.shadcn.css")
            )
            registry.register("components.json", ScaffoldingTemplate("components.json"))
            registry.register("lib/utils.js", ScaffoldingTemplate("lib/utils.js"))
        else:
            registry.register(
                "pages/styles/app.css", ScaffoldingTemplate("pages/styles/app.tailwind.css")
            )
    else:
        # Plain-CSS baseline: a global stylesheet plus a CSS-Modules example, so
        # both `.css` and `*.module.css` imports are proven to work out of the box.
        registry.register("pages/index.pyxl", ScaffoldingTemplate("pages/index.plain.pyxl"))
        registry.register(
            "pages/styles/app.css", ScaffoldingTemplate("pages/styles/app.plain.css")
        )
        registry.register(
            "pages/components/Badge.jsx", ScaffoldingTemplate("pages/components/Badge.jsx")
        )
        registry.register(
            "pages/components/Badge.module.css",
            ScaffoldingTemplate("pages/components/Badge.module.css"),
        )

    return registry


def render_templates(
    writer: FilesystemWriter,
    registry: TemplateRegistry,
    context: Mapping[str, str],
    *,
    overwrite: bool = False,
) -> None:
    for output_path, template in registry.items():
        payload = template.render(context)
        writer.write(output_path, payload, binary=template.binary, overwrite=overwrite)


def log_next_steps(
    logger: ConsoleLogger,
    target_path: Path,
    *,
    include_install_hint: bool,
    in_place: bool = False,
) -> None:
    logger.info("Next steps:")
    step = 1
    if not in_place:
        logger.info("  %d. cd %s" % (step, target_path.as_posix()))
        step += 1
    if include_install_hint:
        logger.info("  %d. pyxle install   # installs Python + Node dependencies" % step)
        logger.info("     (or run 'pip install -r requirements.txt' and 'npm install')")
        step += 1
    logger.info("  %d. pyxle dev" % step)


def _resolve_target(project_name: str) -> tuple[str, Path, bool]:
    """Resolve the requested project name into a slug, target path, and mode.

    Returns ``(project_slug, target_path, in_place)``. Passing ``"."`` (or an
    empty name) scaffolds into the current working directory and derives the
    project name from that directory's name; any other value creates a new
    sibling directory named after the slug.
    """

    stripped = project_name.strip()
    if stripped in ("", "."):
        cwd = Path.cwd()
        derived = cwd.name
        try:
            project_slug = validate_project_name(derived)
        except InvalidProjectName as exc:
            raise typer.BadParameter(
                f"Cannot derive a valid project name from the current directory "
                f"'{derived}': {exc}",
                param_hint="name",
            ) from exc
        return project_slug, Path("."), True

    try:
        project_slug = validate_project_name(project_name)
    except InvalidProjectName as exc:
        raise typer.BadParameter(str(exc), param_hint="name") from exc
    return project_slug, Path(project_slug), False


def run_init(
    project_name: str,
    force: bool,
    template: str,
    logger: ConsoleLogger,
    *,
    tailwind: bool = False,
    shadcn: bool = False,
    import_alias: str = DEFAULT_IMPORT_ALIAS,
    log_steps: bool = True,
) -> Path:
    if template not in SUPPORTED_TEMPLATES:
        raise typer.BadParameter(
            f"Unsupported template '{template}'. Available: {', '.join(sorted(SUPPORTED_TEMPLATES))}"
        )

    # Selecting shadcn/ui implies Tailwind — its components are styled with
    # Tailwind utilities and the theme lives in the Tailwind stylesheet.
    if shadcn:
        tailwind = True

    try:
        import_alias = validate_import_alias(import_alias)
    except InvalidImportAlias as exc:
        raise typer.BadParameter(str(exc), param_hint="import-alias") from exc

    project_slug, target_path, in_place = _resolve_target(project_name)

    writer = FilesystemWriter(target_path)

    try:
        writer.ensure_root(force=force, keep_root=in_place)
    except FileExistsError:
        if in_place:
            logger.error(
                "Current directory is not empty. Re-run with --force to scaffold "
                "into it anyway."
            )
        else:
            logger.error(
                "Target directory already exists. Re-run with --force to overwrite."
            )
        raise typer.Exit(code=1)

    logger.step("Creating project", target_path.as_posix())
    writer.touch_directory("pages/api")
    writer.touch_directory("pages/styles")
    writer.touch_directory("public/branding")

    display_name = project_name if not in_place else project_slug
    context = {
        "package_name": project_slug,
        "project_name": display_name,
        # JSON-encoded (quotes included) so a name with a quote or backslash in
        # it can't produce an unparseable pyxle.config.json.
        "project_name_json": json.dumps(display_name),
        "pyxle_version": __version__,
        "pyxle_framework_requirement": framework_requirement(__version__),
        "runtime_dependencies": _SHADCN_RUNTIME_DEPENDENCIES if shadcn else "",
        "dev_dependencies": _TAILWIND_DEV_DEPENDENCIES if tailwind else "",
        "import_alias": import_alias,
        "alias_prefix": _alias_prefix(import_alias),
    }
    registry = build_template_registry(tailwind=tailwind, shadcn=shadcn)
    render_templates(writer, registry, context, overwrite=force)
    writer.write("public/favicon.ico", default_favicon_bytes(), binary=True, overwrite=force)

    # Generate a per-project development secret in .env.local (gitignored) so
    # CSRF token HMAC is enabled out of the box — no "PYXLE_SECRET_KEY unset"
    # warning on first `pyxle dev`. Production supplies its own via the
    # environment; this file is never committed.
    env_local = (
        "# Local development overrides — gitignored, never commit secrets.\n"
        "# A unique dev key so CSRF token HMAC is enabled in `pyxle dev`.\n"
        "# In production, set PYXLE_SECRET_KEY in the environment instead.\n"
        f"PYXLE_SECRET_KEY={secrets.token_hex(32)}\n"
    )
    writer.write(".env.local", env_local, overwrite=force)

    location = "current directory" if in_place else target_path.as_posix()
    logger.success(f"Project scaffolded at {location}")
    if tailwind:
        detail = "Tailwind CSS v4" + (" + shadcn/ui" if shadcn else "")
        logger.info(f"  Styling: {detail}")
    else:
        logger.info("  Styling: plain CSS + CSS Modules")
    if log_steps:
        log_next_steps(
            logger, target_path, include_install_hint=True, in_place=in_place
        )

    return target_path
