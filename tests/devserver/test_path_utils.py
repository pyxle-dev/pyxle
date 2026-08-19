from __future__ import annotations

import pytest

from pyxle.devserver.path_utils import url_path_is_under


@pytest.mark.parametrize(
    ("path", "prefix", "expected"),
    [
        ("/client", "/client", True),
        ("/client/", "/client", True),
        ("/client/dist/assets/index-a1b2c3.js", "/client", True),
        # The bug this helper exists for: a public file whose name merely
        # begins with the namespace's letters is not inside the namespace.
        ("/client-logo.svg", "/client", False),
        ("/clients.json", "/client", False),
        ("/clientele/team.png", "/client", False),
        ("/", "/client", False),
        ("/__pyxle/studio", "/__pyxle", True),
        ("/__pyxle__/overlay", "/__pyxle", False),
    ],
)
def test_url_path_is_under_compares_whole_segments(
    path: str, prefix: str, expected: bool
) -> None:
    assert url_path_is_under(path, prefix) is expected
