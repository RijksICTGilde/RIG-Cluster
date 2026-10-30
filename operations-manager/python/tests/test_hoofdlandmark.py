"""Every portal page has exactly one main landmark.

Since the app shell (fbc5a2489) the content sat in a plain div, so a screen reader had
no landmark to jump to the content.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

PAGE = "/lotc/bg/dashboard"


def test_the_page_has_one_main_landmark(test_client: TestClient) -> None:
    response = test_client.get(PAGE)
    assert response.status_code == 200, response.text[:300]
    landmarks = re.findall(r"<main\b[^>]*>", response.text)
    assert len(landmarks) == 1, landmarks
    assert 'id="hoofdinhoud"' in landmarks[0]
