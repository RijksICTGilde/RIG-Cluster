"""Cache headers on the /static mount, measured on the wire and not read from the code."""

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opi.core.static_files import IMMUTABLE_CACHE_CONTROL, REVALIDATE_CACHE_CONTROL, CacheControlledStaticFiles
from opi.core.template_helpers import STATIC_DIR, static_url
from opi.core.templates_lotc import templates_lotc


@pytest.fixture
def static_client() -> TestClient:
    """A minimal app with only the /static mount, so nothing else can colour the headers."""
    app = FastAPI()
    app.mount("/static", CacheControlledStaticFiles(directory=str(STATIC_DIR)), name="static")
    return TestClient(app)


def test_versioned_request_is_immutable_for_a_year(static_client: TestClient) -> None:
    """A URL with ?v= identifies its own contents, so it may be pinned."""
    response = static_client.get("/static/js/wizard.js?v=deadbeef")
    assert response.status_code == 200
    assert response.headers["cache-control"] == IMMUTABLE_CACHE_CONTROL


def test_unversioned_request_must_revalidate(static_client: TestClient) -> None:
    """Without ?v= the browser must ask again - a forgotten reference is suboptimal, never broken."""
    response = static_client.get("/static/js/wizard.js")
    assert response.status_code == 200
    assert response.headers["cache-control"] == REVALIDATE_CACHE_CONTROL


def test_empty_version_parameter_is_not_versioned(static_client: TestClient) -> None:
    """An empty ?v= says nothing about the contents and must not pin the file."""
    response = static_client.get("/static/js/wizard.js?v=")
    assert response.headers["cache-control"] == REVALIDATE_CACHE_CONTROL


def test_other_query_parameters_do_not_pin(static_client: TestClient) -> None:
    """Only ?v= earns the long lifetime."""
    response = static_client.get("/static/js/wizard.js?cachebust=1")
    assert response.headers["cache-control"] == REVALIDATE_CACHE_CONTROL


def test_etag_revalidation_still_returns_304(static_client: TestClient) -> None:
    """The correctness machinery we already had keeps working next to the new header."""
    first = static_client.get("/static/js/wizard.js")
    etag = first.headers["etag"]

    second = static_client.get("/static/js/wizard.js", headers={"if-none-match": etag})
    assert second.status_code == 304
    assert second.headers["cache-control"] == REVALIDATE_CACHE_CONTROL


def test_url_from_helper_is_served_and_pinned(static_client: TestClient) -> None:
    """End to end: what static_url() hands to the browser is served with the long lifetime."""
    url = static_url("js/wizard.js")
    response = static_client.get(url)
    assert response.status_code == 200
    assert response.headers["cache-control"] == IMMUTABLE_CACHE_CONTROL


@pytest.fixture
def lotc_client() -> TestClient:
    """The real app, because the /static/lotc/ route lives inside create_app."""
    from opi.server import app

    return TestClient(app)


def _lotc_url(name: str) -> str:
    """The URL a rendered page uses for ``name``, version directory included."""
    html = templates_lotc.env.from_string('<c-page title="m"><c-paragraph>x</c-paragraph></c-page>').render()
    return next(u for u in re.findall(r'(?:href|src)="(/static/lotc/[^"]+)"', html) if u.endswith(f"/{name}"))


def test_lotc_asset_in_a_version_directory_is_immutable(lotc_client: TestClient) -> None:
    """Every LOTC release is a new directory, so everything inside it may be pinned."""
    response = lotc_client.get(_lotc_url("nldd/lotc-nldd.js"))
    assert response.status_code == 200
    assert response.headers["cache-control"] == IMMUTABLE_CACHE_CONTROL


def test_a_partial_global_css_imports_is_immutable_too(lotc_client: TestClient) -> None:
    """The gap of 2 November 2026: global.css carried ?v=, but the eight files it imports had
    a fixed name, and a browser kept running the old fouc.css. They share its directory now."""
    global_css = _lotc_url("nldd/dist/css/global.css")
    fouc = global_css.removesuffix("global.css") + "fouc.css"
    response = lotc_client.get(fouc)
    assert response.status_code == 200
    assert response.headers["cache-control"] == IMMUTABLE_CACHE_CONTROL


def test_the_old_unversioned_url_is_gone(lotc_client: TestClient) -> None:
    """A stable name must not quietly serve whatever is there now: that is a stale-cache trap."""
    assert lotc_client.get("/static/lotc/nldd/dist/css/fouc.css").status_code == 404


def test_lotc_file_outside_a_version_directory_must_revalidate(lotc_client: TestClient) -> None:
    """The coat of arms keeps a stable URL on purpose (an author writes it by hand)."""
    response = lotc_client.get("/static/lotc/nldd/rijkswapen.svg")
    assert response.status_code == 200
    assert response.headers["cache-control"] == REVALIDATE_CACHE_CONTROL
