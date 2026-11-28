"""API-contract defects on the instrumentation routes.

``contracts/openapi.yaml`` declares 404 and 409 for
``PUT /instrumentation/profiles/{profile_id}`` and the routes are expected to
answer with the contract error envelope. Two reachable paths violate that, and
the import route turns a malformed body into a generic 500 instead of a
validation error.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from avdfleet.api.app import create_app
from avdfleet.core.config import Config
from avdfleet.core.paths import Paths
from avdfleet.instrumentation import (
    DeviceProber,
    InstrumentationRepository,
    InstrumentationService,
    ModuleLibrary,
)
from avdfleet.store import Store
from tests.qa.qa_support import magisk_script, target

ORIGIN = "http://127.0.0.1:8765"
TOKEN = "qa-instrumentation-token-0123456789"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
VERIFY_STEP = {"action": "verify_root"}


class _Container:
    """Duck-typed container exposing only the instrumentation service."""

    def __init__(self, service: InstrumentationService, config: Config, paths: Paths) -> None:
        self.config = config
        self.paths = paths
        self.instrumentation = service

    def close(self) -> None:
        return None


@pytest.fixture()
def client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    store = Store(":memory:")
    store.initialize()
    monkeypatch.setenv("AVDFLEET_TOKEN", TOKEN)
    config = Config(
        auth_required=True,
        allow_origins=[ORIGIN],
        trusted_hosts=["127.0.0.1", "localhost"],
        serve_ui=False,
    )
    paths = Paths(root=tmp_path / "home", portable=True).ensure()
    service = InstrumentationService(
        InstrumentationRepository(store.database),
        ModuleLibrary(tmp_path / "library"),
        DeviceProber(runner=magisk_script()),
        lambda name: target(),
        settings=store.settings,
    )
    app = create_app(
        config=config, container=_Container(service, config, paths), paths=paths
    )
    try:
        with TestClient(app, base_url=ORIGIN, raise_server_exceptions=False) as test_client:
            yield test_client
    finally:
        store.close()


def _profile_payload(name: str) -> dict[str, object]:
    return {"name": name, "description": "", "steps": [VERIFY_STEP]}


def test_updating_an_unknown_profile_creates_it_instead_of_returning_404(
    client: TestClient,
) -> None:
    """``PUT /profiles/{id}`` is an upsert: an unknown id is created with 200.

    The contract declares 404 for the operation. In the GUI, editing a profile
    that was deleted in another tab therefore reports "Profile saved" and
    silently creates a new profile with a client-chosen id.
    """
    response = client.put(
        "/api/v1/instrumentation/profiles/prof_does_not_exist",
        json=_profile_payload("Ghost profile"),
        headers=HEADERS,
    )

    assert response.status_code == 404, (
        "PUT with an unknown profile id returned "
        f"{response.status_code} and created the profile: {response.text[:300]}"
    )


def test_renaming_a_profile_to_an_existing_name_returns_a_500_instead_of_409(
    client: TestClient,
) -> None:
    """The name unique index raises ``sqlite3.IntegrityError`` out of the service.

    ``save_profile`` checks uniqueness only on create (``add_profile``); the
    update path relies on the DB index, so the API answers ``internal_error``
    (500) where the contract declares 409.
    """
    first = client.post(
        "/api/v1/instrumentation/profiles", json=_profile_payload("QA One"), headers=HEADERS
    )
    second = client.post(
        "/api/v1/instrumentation/profiles", json=_profile_payload("QA Two"), headers=HEADERS
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    profile_id = second.json()["id"]

    response = client.put(
        f"/api/v1/instrumentation/profiles/{profile_id}",
        json=_profile_payload("QA One"),
        headers=HEADERS,
    )

    assert response.status_code == 409, (
        "renaming a profile to an existing name returned "
        f"{response.status_code}: {response.text[:300]}"
    )


def test_import_with_an_empty_body_returns_a_500_instead_of_a_validation_error(
    client: TestClient,
) -> None:
    """``await request.json()`` raises ``JSONDecodeError`` with no error envelope.

    A client that posts without a body (or with malformed JSON) gets a generic
    ``internal_error`` 500 instead of the contract's validation response.
    """
    response = client.post(
        "/api/v1/instrumentation/modules/import", headers=HEADERS
    )

    assert response.status_code == 422, (
        "an empty import body returned "
        f"{response.status_code}: {response.text[:300]}"
    )
