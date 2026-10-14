"""Tests for verify_imports. Run with:  python3 -m pytest test_verify_imports.py -q"""

from datetime import datetime, timezone
from types import SimpleNamespace

import helpers
from helpers import _parse_ts, verify_imports

ROSSUM = {"api_base_url": "https://acme.rossum.app/api/v1", "target_org_token": "t"}
STARTED = datetime(2026, 10, 13, 7, 47, tzinfo=timezone.utc)
EARLIER = "2026-10-12T15:29:12.808000Z"  # an earlier deploy of the same organisation
NOW = "2026-10-13T07:55:00.123Z"


def _op(status, create_ts=NOW, error=None):
    return {"operation_id": status, "status": status, "create_ts": create_ts, "error": error}


class _Resp:
    ok = True

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


def _run(tmp_path, monkeypatch, docs, ops):
    """docs: dataset -> row count; ops: dataset -> operation log (newest first)."""
    names = sorted(set(docs) | set(ops))
    csv = tmp_path / "hooks.csv"
    csv.write_text("hook_name,invoke\n" + "".join(f"{n} hook,true\n" for n in names))
    hooks = [SimpleNamespace(name=f"{n} hook", settings={"import_config": {"dataset_name": n}}) for n in names]
    monkeypatch.setattr(helpers.requests, "get",
                        lambda url, params, **kw: _Resp({"operations": ops.get(params["dataset_name"], [])}))
    monkeypatch.setattr(helpers.requests, "post",
                        lambda url, json, **kw: _Resp({"result": [{"_id": 1}] * min(docs.get(json["collectionName"], 0), 1)}))
    monkeypatch.setattr(helpers.time, "sleep", lambda s: None)
    verify_imports(ROSSUM, SimpleNamespace(list_hooks=lambda: hooks), SimpleNamespace(hooks_csv=str(csv)),
                   STARTED, wait_s=0)


def test_all_datasets_with_rows_pass(tmp_path, monkeypatch, capsys):
    _run(tmp_path, monkeypatch, {"suppliers": 785, "tax_codes": 12}, {})
    assert "All Coupa import datasets contain data." in capsys.readouterr().out


def test_a_finished_import_with_no_rows_is_a_warning(tmp_path, monkeypatch, capsys):
    """The redeploy case the old check passed: the collection exists, the import finished, 0 rows."""
    _run(tmp_path, monkeypatch, {"suppliers": 785, "purchase_orders": 0},
         {"purchase_orders": [_op("finished"), _op("finished", EARLIER)]})
    out = capsys.readouterr().out
    assert "finished but wrote NO rows" in out
    assert "- purchase_orders  (hook: purchase_orders hook)" in out
    assert "1 of 2 Coupa import dataset(s) contain data." in out


def test_an_earlier_deploys_operation_does_not_count(tmp_path, monkeypatch, capsys):
    _run(tmp_path, monkeypatch, {}, {"suppliers": [_op("finished", EARLIER)]})
    assert "no import operation was recorded" in capsys.readouterr().out


def test_failed_and_running_imports_are_reported_apart(tmp_path, monkeypatch, capsys):
    _run(tmp_path, monkeypatch, {}, {"suppliers": [_op("failed", error="401 Unauthorized")],
                                     "purchase_orders": [_op("processing")]})
    out = capsys.readouterr().out
    assert "imports FAILED" in out and "- suppliers  (hook: suppliers hook): 401 Unauthorized" in out
    assert "NOTE: still importing" in out and "- purchase_orders" in out


def test_parse_ts_accepts_the_mdh_formats():
    assert _parse_ts("2026-10-12T15:29:12.808000Z") == datetime(2026, 10, 12, 15, 29, 12, tzinfo=timezone.utc)
    assert _parse_ts("2026-10-12T15:29:12.8+00:00") == datetime(2026, 10, 12, 15, 29, 12, tzinfo=timezone.utc)
