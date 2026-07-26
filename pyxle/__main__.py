"""Entry point for ``python -m pyxle``.

Mirrors the ``pyxle`` console script so tooling that must run the CLI through a
Python interpreter — most importantly the VS Code debugger, which launches
``python -m pyxle dev`` under debugpy — has a stable module target. Keeps the
CLI definition in one place (:mod:`pyxle.cli`).
"""

from __future__ import annotations

from pyxle.cli import app


def main() -> None:
    app()


if __name__ == "__main__":
    main()
