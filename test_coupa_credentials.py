"""Tests for the Coupa credential check. Run with:  python3 -m pytest test_coupa_credentials.py -q"""

import base64
import json
from types import SimpleNamespace

import pytest
import requests

import helpers
from helpers import UNKNOWN_COUPA_CLIENT_ID, verify_credentials

COUPA = {"coupa_base_api_url": "https://acme.coupahost.com/", "client_id": "a" * 32, "client_secret": "s"}
SCOPES = {"core.common.read": "tax_codes", "core.supplier.read": "suppliers", "core.invoice.write": "export"}


class FakeResponse:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body
        self.ok = status < 400

    def json(self):
        if self._body is None:
            raise ValueError("not JSON")  # Coupa's 500 is an HTML page
        return self._body


def _token(scopes):
    claim = base64.urlsafe_b64encode(json.dumps({"scope": " ".join(scopes)}).encode()).decode().rstrip("=")
    return FakeResponse(200, {"access_token": f"h.{claim}.s"})


class FakeCoupa:
    """Token endpoint that answers like the tenant did for each misconfiguration."""

    def __init__(self, granted=(), broken_client=False, bad_secret=False, down=False):
        self.granted, self.broken_client, self.bad_secret, self.down = set(granted), broken_client, bad_secret, down
        self.requests = []

    def post(self, url, data, timeout):
        self.requests.append(data)
        if self.down:
            return FakeResponse(500)
        if data["client_id"] == UNKNOWN_COUPA_CLIENT_ID or self.bad_secret:
            return FakeResponse(401, {"error": "invalid_client"})
        scope = data.get("scope")
        if scope and not set(scope.split()) <= self.granted:
            return FakeResponse(400, {"error": "invalid_scope"})
        if self.broken_client:
            return FakeResponse(500)
        return _token(self.granted)


@pytest.fixture
def assets(tmp_path):
    path = tmp_path / "required_scopes.json"
    path.write_text(json.dumps({"required_scopes": SCOPES}))
    return SimpleNamespace(required_scopes=str(path))


def _run(monkeypatch, assets, coupa):
    monkeypatch.setattr(helpers.requests, "post", coupa.post)
    with pytest.raises(SystemExit):
        verify_credentials(COUPA, assets)


def test_valid_client_passes_with_a_single_request(monkeypatch, assets, capsys):
    coupa = FakeCoupa(granted=SCOPES)
    monkeypatch.setattr(helpers.requests, "post", coupa.post)
    verify_credentials(COUPA, assets)
    assert "Coupa credential OK" in capsys.readouterr().out
    assert len(coupa.requests) == 1  # the diagnosis only runs on failure


def test_wrong_secret_is_reported_as_such_without_probing(monkeypatch, assets, capsys):
    coupa = FakeCoupa(granted=SCOPES, bad_secret=True)
    _run(monkeypatch, assets, coupa)
    assert "rejected the client id or secret (HTTP 401, invalid_client)" in capsys.readouterr().out
    assert len(coupa.requests) == 1


def test_missing_scopes_behind_a_500_are_named(monkeypatch, assets, capsys):
    """The tenant that answered the bare request with HTML 500 and lacked most scopes."""
    _run(monkeypatch, assets, FakeCoupa(granted={"core.common.read"}, broken_client=True))
    out = capsys.readouterr().out
    assert "missing 2 of the 3 scope(s)" in out
    assert "core.supplier.read  -> suppliers" in out
    assert "core.invoice.write  -> export" in out
    assert "core.common.read  ->" not in out
    assert "Check coupa.client_id" not in out  # the misleading old advice


def test_wrong_client_type_is_detected_when_every_scope_is_accepted(monkeypatch, assets, capsys):
    """An Authorization code client: scopes pass, token issuance 500s, unknown client gets 401."""
    _run(monkeypatch, assets, FakeCoupa(granted=SCOPES, broken_client=True))
    out = capsys.readouterr().out
    assert "accepts this client and all its CIB scopes" in out
    assert "'Client credentials'" in out and "redirect URLs" in out and "JWKS URI" in out


def test_instance_failure_is_not_blamed_on_the_client(monkeypatch, assets, capsys):
    _run(monkeypatch, assets, FakeCoupa(down=True))
    out = capsys.readouterr().out
    assert "fails the same way for an\nunknown client id (HTTP 500)" in out
    assert "Client credentials" not in out


def test_network_failure_during_diagnosis_still_aborts(monkeypatch, assets, capsys):
    calls = []

    def post(url, data, timeout):
        calls.append(data)
        if len(calls) == 1:
            return FakeResponse(500)
        raise requests.ConnectionError("reset")

    monkeypatch.setattr(helpers.requests, "post", post)
    with pytest.raises(SystemExit):
        verify_credentials(COUPA, assets)
    assert "follow-up diagnosis\nfailed (reset)" in capsys.readouterr().out
