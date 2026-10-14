"""Tests for the rule action host rewrite. Run with:  python3 -m pytest test_rule_hosts.py -q"""

import copy

from helpers import APIClientError, _rehost, _source_api_bases, fix_rule_hosts

BASE = "https://acme.rossum.app/api/v1"
SRC = "https://cib.rossum.app/api/v1"
ROSSUM = {"api_base_url": BASE + "/"}


def _release(tmp_path):
    (tmp_path / "prd_config.yaml").write_text(
        f"directories:\n  cib-org:\n    api_base: {SRC}\n  target:\n    api_base: {BASE}/\n")
    return str(tmp_path)


def _rule(id_, queue_url):
    return {"id": id_, "name": f"Move to {id_}", "url": f"{BASE}/rules/{id_}",
            "actions": [{"id": "a", "type": "change_queue", "payload": {"queue": queue_url, "reimport": True}}]}


class FakeClient:
    def __init__(self, rules, fail_patch=False):
        self.rules = {r["id"]: copy.deepcopy(r) for r in rules}
        self.fail_patch, self.patched = fail_patch, []

    def request_json(self, method, url, json=None):
        if method == "GET":
            return {"results": copy.deepcopy(list(self.rules.values())), "pagination": {"next": None}}
        if self.fail_patch:
            raise APIClientError(method, url, 400, "nope")
        rule_id = int(url.rsplit("/", 1)[-1])
        self.rules[rule_id].update(json)
        self.patched.append(rule_id)


def test_rehost_rewrites_only_source_urls():
    actions = [{"payload": {"queue": f"{SRC}/queues/9", "reimport": True}},
               {"payload": {"labels": [f"{SRC}/labels/7"], "note": "see cib.rossum.app"}}]
    out = _rehost(actions, SRC, BASE)
    assert out[0]["payload"] == {"queue": f"{BASE}/queues/9", "reimport": True}
    assert out[1]["payload"] == {"labels": [f"{BASE}/labels/7"], "note": "see cib.rossum.app"}
    assert actions[0]["payload"]["queue"].startswith(SRC)  # input untouched


def test_source_bases_are_the_release_hosts_other_than_the_target(tmp_path):
    assert _source_api_bases(_release(tmp_path), BASE) == [SRC]


def test_only_rules_on_the_source_host_are_patched(tmp_path):
    client = FakeClient([_rule(1, f"{SRC}/queues/9"), _rule(2, f"{BASE}/queues/9")])
    fix_rule_hosts(client, ROSSUM, _release(tmp_path))
    assert client.patched == [1]
    assert client.rules[1]["actions"][0]["payload"] == {"queue": f"{BASE}/queues/9", "reimport": True}


def test_a_rejected_patch_warns_and_does_not_abort(tmp_path, capsys):
    client = FakeClient([_rule(1, f"{SRC}/queues/9")], fail_patch=True)
    fix_rule_hosts(client, ROSSUM, _release(tmp_path))
    assert "WARNING: rule 'Move to 1' still points at the source host" in capsys.readouterr().out


def test_missing_prd_config_warns_and_skips(tmp_path, capsys):
    client = FakeClient([_rule(1, f"{SRC}/queues/9")])
    fix_rule_hosts(client, ROSSUM, str(tmp_path))
    assert client.patched == []
    assert "rule action hosts not checked" in capsys.readouterr().out
