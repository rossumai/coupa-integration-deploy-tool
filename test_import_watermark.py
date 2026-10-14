"""Tests for the redeploy import seed. Run with:  python3 -m pytest test_import_watermark.py -q"""

import copy

import pytest

import import_watermark as iw

ROSSUM = {"api_base_url": "https://acme.rossum.app/api/v1", "target_org_token": "t"}
OLD = {"operation_id": "old", "status": "finished", "create_ts": "2026-10-12T15:29:12.808000Z"}
STARTED = iw._ts("2026-10-13T07:47:00Z")
REMIT = "remit-to-addresses[updated-at][gt_or_eq]"


def import_hook(hook_id, dataset, key="updated-at[gt_or_eq]"):
    return {"id": hook_id, "name": f"{dataset} import",
            "settings": {"import_config": {"dataset_name": dataset, "query": {"order_by": "id", key: iw.ANCHOR}}}}


class _Resp:
    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body

    def raise_for_status(self):
        pass


class FakeOrg:
    """Hooks as the API holds them, and the MDH operation log; an invoke starts a job."""

    def __init__(self, hooks, ops, invoke_starts_job=True, fail_invoke=False):
        self.hooks = {h["id"]: copy.deepcopy(h) for h in hooks}
        self.ops, self.starts, self.fail_invoke = ops, invoke_starts_job, fail_invoke
        self.invoked_with = {}

    # rossum-api client
    def request_json(self, method, url, json=None):
        if method == "GET":
            return {"results": copy.deepcopy(list(self.hooks.values())), "pagination": {"next": None}}
        self.hooks[int(url.rsplit("/", 1)[-1])].update(copy.deepcopy(json))

    def request(self, method, url):
        if self.fail_invoke:
            raise iw.APIClientError(method, url, 429, "Too many requests")
        settings = self.hooks[int(url.split("/")[1])]["settings"]
        dataset = settings["import_config"]["dataset_name"]
        self.invoked_with[dataset] = copy.deepcopy(settings["import_config"]["query"])
        if self.starts:
            self.ops.setdefault(dataset, []).insert(0, {"operation_id": f"new-{dataset}", "status": "processing",
                                                        "create_ts": "2026-10-13T07:55:00Z"})

    # requests.get on the operation log
    def get(self, url, params, **kwargs):
        assert url == "https://acme.rossum.app/svc/master-data-hub/api/v1/operation/"
        return _Resp({"operations": list(self.ops.get(params["dataset_name"], []))})


@pytest.fixture
def org(monkeypatch):
    def make(*args, **kwargs):
        fake = FakeOrg(*args, **kwargs)
        monkeypatch.setattr(iw.requests, "get", fake.get)
        monkeypatch.setattr(iw.time, "sleep", lambda s: None)
        return fake
    return make


def test_anchor_keys_finds_both_coupa_filter_keys():
    assert iw.anchor_keys(import_hook(1, "a")["settings"]) == ["updated-at[gt_or_eq]"]
    assert iw.anchor_keys(import_hook(1, "r", REMIT)["settings"]) == [REMIT]
    assert iw.anchor_keys({"import_config": {"query": {"updated-at[gt_or_eq]": iw.EPOCH}}}) == []
    assert iw.anchor_keys(None) == []


def test_had_history_counts_only_finished_runs_before_the_deploy():
    assert iw.had_history([OLD], STARTED)
    assert not iw.had_history([], STARTED)
    assert not iw.had_history([{**OLD, "status": "failed"}], STARTED)  # failed runs set no watermark
    assert not iw.had_history([{**OLD, "create_ts": "2026-10-13T07:50:00Z"}], STARTED)  # this deploy's own run


def test_full_load_only_where_history_exists_and_anchors_are_restored(org):
    hooks = [import_hook(1, "suppliers"), import_hook(2, "remit_to", REMIT), import_hook(3, "fresh")]
    fake = org(hooks, {"suppliers": [OLD], "remit_to": [OLD]})
    iw.seed_imports(fake, ROSSUM, STARTED, poll_s=0)
    assert fake.invoked_with == {"suppliers": {"order_by": "id", "updated-at[gt_or_eq]": iw.EPOCH},
                                 "remit_to": {"order_by": "id", REMIT: iw.EPOCH}}
    assert [fake.hooks[i]["settings"] for i in (1, 2, 3)] == [h["settings"] for h in hooks]


def test_nothing_happens_in_an_organisation_without_history(org, capsys):
    fake = org([import_hook(1, "suppliers")], {})
    iw.seed_imports(fake, ROSSUM, STARTED, poll_s=0)
    assert fake.invoked_with == {}
    assert "No earlier import history" in capsys.readouterr().out


def test_anchor_is_restored_when_no_operation_appears(org, capsys):
    hooks = [import_hook(1, "suppliers")]
    fake = org(hooks, {"suppliers": [OLD]}, invoke_starts_job=False)
    iw.seed_imports(fake, ROSSUM, STARTED, wait_s=0, poll_s=0)
    assert fake.hooks[1]["settings"] == hooks[0]["settings"]
    assert "WARNING: suppliers: no full import seen" in capsys.readouterr().out


def test_anchor_is_restored_when_the_invoke_fails(org, capsys):
    hooks = [import_hook(1, "suppliers")]
    fake = org(hooks, {"suppliers": [OLD]}, fail_invoke=True)
    iw.seed_imports(fake, ROSSUM, STARTED, poll_s=0)
    assert fake.hooks[1]["settings"] == hooks[0]["settings"]
    assert "seeding the full imports stopped" in capsys.readouterr().out
