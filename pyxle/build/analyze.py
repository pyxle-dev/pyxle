"""Dependency-free bundle-size analysis for ``pyxle build --analyze``.

Walks the built client directory, measures each JS/CSS asset's raw and gzipped
size, and renders a sorted report. No third-party tooling — just the file sizes
the browser will actually download.
"""

from __future__ import annotations

import gzip
from dataclasses import dataclass
from pathlib import Path

_ASSET_SUFFIXES = (".js", ".mjs", ".css")


@dataclass(frozen=True, slots=True)
class AssetSize:
    """One built asset's on-disk and gzipped size."""

    path: str
    raw_bytes: int
    gzip_bytes: int


def analyze_bundle(client_dir: Path) -> list[AssetSize]:
    """Return JS/CSS assets under ``client_dir``, largest (raw) first."""
    assets: list[AssetSize] = []
    if not client_dir.is_dir():
        return assets
    for path in client_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in _ASSET_SUFFIXES:
            continue
        data = path.read_bytes()
        assets.append(
            AssetSize(
                path=path.relative_to(client_dir).as_posix(),
                raw_bytes=len(data),
                gzip_bytes=len(gzip.compress(data, compresslevel=6)),
            )
        )
    assets.sort(key=lambda asset: (asset.raw_bytes, asset.path), reverse=True)
    return assets


def human_size(num_bytes: int) -> str:
    """Render a byte count as a short human string (e.g. ``12.3KB``)."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)}B" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"  # pragma: no cover - unreachable, loop returns first


def format_bundle_report(assets: list[AssetSize]) -> str:
    """Render the asset list as a multi-line report (raw / gzip per asset)."""
    if not assets:
        return "Bundle analysis: no JS/CSS assets found."
    width = max(len(asset.path) for asset in assets)
    lines = ["Bundle analysis (raw / gzip):"]
    for asset in assets:
        lines.append(
            f"  {asset.path.ljust(width)}  "
            f"{human_size(asset.raw_bytes):>9} / {human_size(asset.gzip_bytes):>9} gzip"
        )
    total_raw = sum(asset.raw_bytes for asset in assets)
    total_gzip = sum(asset.gzip_bytes for asset in assets)
    lines.append(
        f"  {'─ total'.ljust(width)}  "
        f"{human_size(total_raw):>9} / {human_size(total_gzip):>9} gzip "
        f"({len(assets)} file(s))"
    )
    return "\n".join(lines)


__all__ = ["AssetSize", "analyze_bundle", "format_bundle_report", "human_size"]
