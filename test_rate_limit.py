"""Tests for the HTTP 429 handling. Run with:  python3 -m pytest test_rate_limit.py -q"""

import httpx
import pytest

import helpers
from helpers import API_RETRIES, MAX_RETRY_AFTER_S, make_client, retry_after_s, send

ROSSUM = {"target_org_token": "t", "api_base_url": "https://acme.rossum.app/api/v1"}


def _resp(status, retry_after=None):
    return httpx.Response(status, headers={"Retry-After": retry_after} if retry_after else {})


@pytest.mark.parametrize("response, expected", [
    (_resp(429, "7"), 7),
    (_resp(429, "999"), MAX_RETRY_AFTER_S),       # capped, a hostile header cannot stall a deploy
    (_resp(429), None),                            # no header: caller's backoff applies
    (_resp(429, "Wed, 21 Oct 2026 07:28:00 GMT"), None),
    (_resp(503, "7"), None),                       # only 429 asks us to slow down
])
def test_retry_after_s(response, expected):
    assert retry_after_s(response) == expected


def test_client_waits_as_told_and_retries_past_three_429s(monkeypatch):
    """The 429 burst that aborted handle_hooks: four 429s, then the PATCH goes through."""
    slept, calls = [], []
    monkeypatch.setattr(helpers.time, "sleep", slept.append)  # also catches tenacity's own backoff waits

    def handler(request):
        calls.append(request.method)
        return httpx.Response(429, headers={"Retry-After": "3"}) if len(calls) <= 4 else httpx.Response(200, json={})

    client = make_client(ROSSUM)
    client.internal_client.client = httpx.Client(transport=httpx.MockTransport(handler))
    client.request_json("PATCH", "hooks/1", json={"active": True})

    assert calls == ["PATCH"] * 5
    assert slept[0::2] == [3, 3, 3, 3]  # Retry-After first, then tenacity's backoff, per 429


def test_client_still_gives_up_eventually(monkeypatch):
    monkeypatch.setattr(helpers.time, "sleep", lambda s: None)
    client = make_client(ROSSUM)
    client.internal_client.client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429)))
    with pytest.raises(helpers.APIClientError):
        client.request_json("GET", "hooks/1")


class _Requests:
    def __init__(self, statuses):
        self.statuses, self.calls = list(statuses), 0

    def __call__(self, method, url, **kwargs):
        self.calls += 1
        status = self.statuses.pop(0)
        r = helpers.requests.Response()
        r.status_code = status
        if status == 429:
            r.headers["Retry-After"] = "2"
        return r


def test_send_honours_retry_after(monkeypatch):
    slept, fake = [], _Requests([429, 429, 200])
    monkeypatch.setattr(helpers.requests, "request", fake)
    monkeypatch.setattr(helpers.time, "sleep", slept.append)
    assert send("POST", "https://x/svc/data-storage/api/v1/collections/create").status_code == 200
    assert slept == [2, 2]


def test_send_returns_the_last_429_when_retries_run_out(monkeypatch):
    fake = _Requests([429] * API_RETRIES)
    monkeypatch.setattr(helpers.requests, "request", fake)
    monkeypatch.setattr(helpers.time, "sleep", lambda s: None)
    assert send("GET", "https://x").status_code == 429
    assert fake.calls == API_RETRIES
