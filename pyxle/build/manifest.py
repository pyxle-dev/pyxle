"""Page manifest loading for production builds."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


def load_manifest(path: Path | str) -> Dict[str, Any]:
    """Load and validate a page-manifest.json file.

    Returns the parsed manifest dictionary. Raises ``ValueError`` when the
    file does not contain a valid JSON object or when any asset path
    contains path-traversal sequences.
    """
    manifest_path = Path(path)
    with manifest_path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    if not isinstance(data, dict):
        raise ValueError(
            f"page-manifest.json at '{manifest_path}' must be a JSON object, "
            f"got {type(data).__name__}"
        )

    validate_asset_paths(data, manifest_path)

    return data


def validate_asset_paths(data: Dict[str, Any], manifest_path: Path) -> None:
    """Ensure no asset file path escapes the expected build directory.

    Rejects paths containing ``../`` (path traversal) or starting with ``/``
    to prevent a compromised Vite build from referencing arbitrary filesystem
    locations.  Note: we check for ``../`` rather than ``..`` because Vite
    may produce filenames like ``__...slug__-hash.js`` for catch-all routes,
    which legitimately contain ``..`` as a substring.

    Public because the production build applies the *same* rule to the manifest
    it is about to write: a ``dist/`` that ``pyxle serve`` would reject must
    never be reported as a successful build. See
    :func:`pyxle.build.pipeline.run_build`.
    """
    for route_key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        client = entry.get("client")
        if not isinstance(client, dict):
            continue
        file_val = client.get("file", "")
        if isinstance(file_val, str):
            _check_safe_path(file_val, route_key, "file", manifest_path)
        for css_asset in client.get("css", []):
            if isinstance(css_asset, str):
                _check_safe_path(css_asset, route_key, "css", manifest_path)


def _check_safe_path(
    value: str, route_key: str, field: str, manifest_path: Path
) -> None:
    if "/../" in value or value.startswith("../") or value.startswith("/"):
        raise ValueError(
            f"Manifest {field} entry for '{route_key}' in "
            f"'{manifest_path}' contains unsafe path: '{value}'"
        )
