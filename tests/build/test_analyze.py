"""Tests for the dependency-free bundle-size analyzer."""

from __future__ import annotations

from pathlib import Path

from pyxle.build.analyze import (
    AssetSize,
    analyze_bundle,
    format_bundle_report,
    human_size,
)


def test_analyze_bundle_measures_sorts_and_filters(tmp_path: Path) -> None:
    assets_dir = tmp_path / "client" / "assets"
    assets_dir.mkdir(parents=True)
    (assets_dir / "big.js").write_bytes(b"x" * 5000)
    (assets_dir / "small.css").write_bytes(b"y" * 100)
    # Non-JS/CSS files are ignored.
    (tmp_path / "client" / "robots.txt").write_bytes(b"z" * 9999)

    assets = analyze_bundle(tmp_path / "client")
    names = [asset.path for asset in assets]
    # Sorted by raw size descending; the .txt is excluded.
    assert names == ["assets/big.js", "assets/small.css"]
    assert assets[0].raw_bytes == 5000
    # Highly repetitive content compresses well.
    assert assets[0].gzip_bytes < assets[0].raw_bytes


def test_analyze_bundle_missing_dir_is_empty(tmp_path: Path) -> None:
    assert analyze_bundle(tmp_path / "does-not-exist") == []


def test_format_bundle_report_has_rows_and_total() -> None:
    report = format_bundle_report(
        [AssetSize("a.js", 2048, 800), AssetSize("b.css", 1024, 400)]
    )
    assert "Bundle analysis" in report
    assert "a.js" in report and "b.css" in report
    assert "total" in report
    assert "gzip" in report


def test_format_bundle_report_empty() -> None:
    assert "no JS/CSS" in format_bundle_report([])


def test_human_size() -> None:
    assert human_size(512) == "512B"
    assert human_size(2048) == "2.0KB"
    assert human_size(5 * 1024 * 1024) == "5.0MB"
    assert human_size(3 * 1024 * 1024 * 1024) == "3.0GB"
