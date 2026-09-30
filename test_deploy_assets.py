"""Tests for the version-aware deploy-asset plumbing.

Run with:  python3 -m pytest test_deploy_assets.py -q

These cover the parts that decide WHICH files a deploy reads and HOW hook
settings are recognised — the places where getting it wrong produces a deploy
that half-works rather than an obvious failure. Anything needing a live Rossum
or Coupa API is out of scope here and has to be covered by a real test deploy.
"""

import json
import os

import pytest

from cib_assets import (DEPLOY_FILE_NAME, HOOKS_CSV_NAME, REQUIRED_SCOPES_NAME, SECRETS_FILE_NAME,
                        AssetResolutionError, resolve_assets)
from helpers import (_first_configuration_source, check_hook_templates, check_target_org_empty, is_newer,
                     neutralize_function_hook_templates, parse_version,
                     restore_release_to_pristine, schema_section_children)

ASSET_NAMES = (DEPLOY_FILE_NAME, SECRETS_FILE_NAME, HOOKS_CSV_NAME, REQUIRED_SCOPES_NAME)


def _write_assets(directory, names=ASSET_NAMES):
    os.makedirs(directory, exist_ok=True)
    for name in names:
        with open(os.path.join(directory, name), "w") as f:
            f.write("{}" if name.endswith(".json") else "")


# --------------------------------------------------------------------------
# resolve_assets
# --------------------------------------------------------------------------

def test_release_with_deploy_dir_wins_over_legacy_config(tmp_path):
    """A self-describing release must never be paired with _config/'s v1 IDs."""
    release, script = tmp_path / "rel", tmp_path / "script"
    _write_assets(release / "deploy")
    _write_assets(script / "_config")

    assets = resolve_assets(str(release), "v2.0.0", str(script))

    assert assets.shipped_with_release
    assert assets.deploy_file == str(release / "deploy" / DEPLOY_FILE_NAME)
    assert "v2.0.0 release" in assets.source


def test_legacy_release_falls_back_to_config(tmp_path):
    """v1.0.0 and v1.1.0 ship no deploy/ folder and share one _config/ set."""
    release, script = tmp_path / "rel", tmp_path / "script"
    os.makedirs(release)
    _write_assets(script / "_config")

    assets = resolve_assets(str(release), "v1.1.0", str(script))

    assert not assets.shipped_with_release
    assert assets.hooks_csv == str(script / "_config" / HOOKS_CSV_NAME)


def test_all_four_assets_come_from_the_same_place(tmp_path):
    """Mixing a release deploy file with legacy hooks.csv would silently misconfigure."""
    release, script = tmp_path / "rel", tmp_path / "script"
    _write_assets(release / "deploy")
    _write_assets(script / "_config")

    assets = resolve_assets(str(release), "v2.0.0", str(script))

    directories = {os.path.dirname(p) for p in
                   (assets.deploy_file, assets.secrets_file, assets.hooks_csv, assets.required_scopes)}
    assert directories == {str(release / "deploy")}


def test_incomplete_release_assets_abort(tmp_path):
    """Better a clear stop than a deploy missing its scope map or secrets."""
    release, script = tmp_path / "rel", tmp_path / "script"
    _write_assets(release / "deploy", names=(DEPLOY_FILE_NAME, SECRETS_FILE_NAME))
    _write_assets(script / "_config")

    with pytest.raises(AssetResolutionError) as excinfo:
        resolve_assets(str(release), "v2.0.0", str(script))

    assert HOOKS_CSV_NAME in excinfo.value.message()
    assert "generate_deploy_assets" in excinfo.value.message()


def test_unknown_version_with_no_fallback_aborts(tmp_path):
    """A future release without deploy/ must not silently get v1's object IDs."""
    release, script = tmp_path / "rel", tmp_path / "script"
    os.makedirs(release)
    os.makedirs(script)

    with pytest.raises(AssetResolutionError) as excinfo:
        resolve_assets(str(release), "v3.0.0", str(script))

    assert "v1.0.0 and v1.1.0" in excinfo.value.message()


# --------------------------------------------------------------------------
# _first_configuration_source — the KeyError that CIB 2.0 would have hit
# --------------------------------------------------------------------------

def test_mdh_lookup_source_is_found():
    settings = {"configurations": [{"name": "dup check",
                                    "source": {"auth": {"url": "x"}, "queries": [{"url": "y"}]}}]}
    assert _first_configuration_source(settings)["auth"] == {"url": "x"}


@pytest.mark.parametrize("settings", [
    # Coupa E-Invoicing: configurations entries are XML field maps, no 'source'.
    {"configurations": [{"//": "", "fields": {}, "trigger_condition": {}}]},
    # Duplicate Handling: same key, different structure again.
    {"configurations": [{"logic": {}, "trigger_events": [], "trigger_actions": []}]},
    {"configurations": []},
    {"configurations": None},
    {"configurations": ["not-a-dict"]},
    {"configurations": [{"source": "not-a-dict"}]},
    {},
])
def test_configuration_shapes_without_a_source_return_empty(settings):
    """These used to raise KeyError on settings['configurations'][0]['source']."""
    assert _first_configuration_source(settings) == {}


# --------------------------------------------------------------------------
# Version ordering — decides whether the "newer CIB available" prompt appears
# --------------------------------------------------------------------------

@pytest.mark.parametrize("latest,configured,expected,why", [
    ("v1.1.0", "v1.0.0", True, "ordinary upgrade"),
    ("v2.0.0", "v1.1.0", True, "major upgrade"),
    ("v1.1.0", "v1.1.0", False, "same version"),
    ("v1.10.0", "v1.9.0", True, "compared numerically, not lexically"),
    ("v2.0.0", "v2.0.0-rc1", True, "a final release beats its own rc"),
    ("v2.0.0-rc2", "v2.0.0-rc1", True, "later rc"),
    ("v2.0.0-rc1", "v2.0.0", False, "an rc must not supersede the final"),
])
def test_is_newer(latest, configured, expected, why):
    assert is_newer(latest, configured) is expected, why


def test_configured_version_ahead_of_published_is_not_an_upgrade():
    """Pinning an unpublished version must not offer the older published tag.

    Accepting that prompt would silently downgrade the deploy AND rewrite
    cib_version in config.json — exactly the case hit while testing 2.0 before
    it is released.
    """
    assert is_newer("v1.1.0", "v2.0.0") is False
    assert is_newer("v1.1.0", "v2.0.0-rc1") is False


@pytest.mark.parametrize("tag", ["", None, "latest", "2.0", "v2.0.0.1"])
def test_unparseable_versions_fall_back_to_inequality(tag):
    """Unknown tag shapes keep the old behaviour: offer it rather than hide it."""
    assert parse_version(tag) is None
    assert is_newer(tag, "v1.1.0") is (tag != "v1.1.0")


def test_parse_version_accepts_bare_and_v_prefixed():
    assert parse_version("2.0.0") == parse_version("v2.0.0")


# --------------------------------------------------------------------------
# schema_section_children — decides which attribute overrides are safe to emit
# --------------------------------------------------------------------------

CREDENTIAL_FIELDS = {"oauth_client_id", "coupa_api_base_url"}


def _schema(path, sections):
    path.write_text(json.dumps({"content": [
        {"id": sid, "category": "section", "children": [{"id": c, "category": "datapoint"} for c in kids]}
        for sid, kids in sections
    ]}))
    return str(path)


def test_finds_fields_under_any_top_level_section(tmp_path):
    p = _schema(tmp_path / "s.json", [("general", ["document_id"]),
                                      ("export_pipeline", ["oauth_client_id", "coupa_api_base_url"])])
    assert schema_section_children(p, CREDENTIAL_FIELDS) == CREDENTIAL_FIELDS


def test_reports_only_the_fields_actually_present(tmp_path):
    p = _schema(tmp_path / "s.json", [("export_pipeline", ["oauth_client_id"])])
    assert schema_section_children(p, CREDENTIAL_FIELDS) == {"oauth_client_id"}


def test_schema_without_credential_fields_yields_nothing(tmp_path):
    """The CIB 2.0 E-invoicing Inbox: it never exports, so it has no Coupa fields.

    A blanket override on this schema aborts the whole deploy, because prd2's
    perform_search() raises on a JMESPath that matches nothing.
    """
    p = _schema(tmp_path / "s.json", [("general", ["document_id"])])
    assert schema_section_children(p, CREDENTIAL_FIELDS) == set()


def test_nested_fields_are_not_matched(tmp_path):
    """The override JMESPath only reaches direct children of a top-level section."""
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"content": [{"id": "sec", "category": "section", "children": [
        {"id": "line_items", "category": "multivalue",
         "children": {"id": "row", "category": "tuple",
                      "children": [{"id": "oauth_client_id", "category": "datapoint"}]}}]}]}))
    assert schema_section_children(str(path), CREDENTIAL_FIELDS) == set()


def test_unreadable_schema_degrades_to_no_override(tmp_path):
    assert schema_section_children(str(tmp_path / "missing.json"), CREDENTIAL_FIELDS) == set()


# --------------------------------------------------------------------------
# Hook templates — prd2 opens an interactive picker whenever nothing matched
# --------------------------------------------------------------------------

class FakeClient:
    """Resolves only the template ids it was told about; 404s on the rest."""

    def __init__(self, visible=()):
        self.visible = {str(v) for v in visible}
        self.asked = []

    def request_json(self, method, path):
        template_id = path.rsplit("/", 1)[-1]
        self.asked.append(template_id)
        if template_id not in self.visible:
            raise RuntimeError("[GET] .../hook_templates - HTTP 404 - not found")
        return {"id": int(template_id)}


def _hook(directory, name, hook_type, template, private=None):
    directory.mkdir(parents=True, exist_ok=True)
    config = {"private": private} if private is not None else {}
    (directory / f"{name}.json").write_text(json.dumps(
        {"name": name, "type": hook_type, "hook_template": template, "config": config}))


def _template_of(directory, name):
    return json.loads((directory / f"{name}.json").read_text())["hook_template"]


def test_function_templates_are_stripped(tmp_path):
    """Avoids prd2 duplicating the hook while the function is still provisioning."""
    hooks = tmp_path / "cib-org" / "default" / "hooks"
    _hook(hooks, "Export Pipeline - 1", "function", "https://x/api/v1/hook_templates/50")

    neutralize_function_hook_templates(str(tmp_path))

    assert _template_of(hooks, "Export Pipeline - 1") is None


def test_private_nonfunction_templates_are_never_stripped(tmp_path):
    """Stripping one CAUSES the prompt it looks like it avoids.

    prd2 only enters the matching branch when the hook still has a template;
    the picker is a separate top-level step reached whenever nothing matched.
    So removing the reference does not skip the picker, it guarantees it.
    """
    hooks = tmp_path / "cib-org" / "default" / "hooks"
    _hook(hooks, "Master Data Import", "job", "https://x/api/v1/hook_templates/55", private=True)
    _hook(hooks, "MDH - Main", "webhook", "https://x/api/v1/hook_templates/39", private=True)

    neutralize_function_hook_templates(str(tmp_path))

    assert _template_of(hooks, "Master Data Import").endswith("/55")
    assert _template_of(hooks, "MDH - Main").endswith("/39")


def test_invisible_template_on_a_private_hook_aborts(tmp_path):
    """No workaround exists, so fail up front naming the template."""
    hooks = tmp_path / "cib-org" / "default" / "hooks"
    _hook(hooks, "Master Data Import", "job", "https://x/api/v1/hook_templates/55", private=True)

    with pytest.raises(SystemExit) as excinfo:
        check_hook_templates(FakeClient(visible=[]), str(tmp_path))

    assert excinfo.value.code == 1


def test_visible_template_passes(tmp_path):
    hooks = tmp_path / "cib-org" / "default" / "hooks"
    _hook(hooks, "Master Data Import", "job", "https://x/api/v1/hook_templates/55", private=True)

    check_hook_templates(FakeClient(visible=[55]), str(tmp_path))  # must not raise


def test_function_hooks_do_not_gate_the_deploy(tmp_path):
    """get_hook_template_from_user() returns immediately for a function."""
    hooks = tmp_path / "cib-org" / "default" / "hooks"
    _hook(hooks, "Export Pipeline - 1", "function", "https://x/api/v1/hook_templates/50")

    check_hook_templates(FakeClient(visible=[]), str(tmp_path))  # must not raise


def test_each_template_is_checked_once(tmp_path):
    hooks = tmp_path / "cib-org" / "default" / "hooks"
    for i in range(12):
        _hook(hooks, f"Import {i:02d}", "job", "https://x/api/v1/hook_templates/55", private=True)

    client = FakeClient(visible=[55])
    check_hook_templates(client, str(tmp_path))

    assert client.asked == ["55"]


def test_an_unclear_api_failure_does_not_block(tmp_path, capsys):
    """An expired token is not evidence that a template is missing."""
    class Flaky(FakeClient):
        def request_json(self, method, path):
            raise RuntimeError("HTTP 401 - Invalid token.")

    hooks = tmp_path / "cib-org" / "default" / "hooks"
    _hook(hooks, "Master Data Import", "job", "https://x/api/v1/hook_templates/55", private=True)

    check_hook_templates(Flaky(), str(tmp_path))  # must not raise

    assert "could not determine" in capsys.readouterr().out


# --------------------------------------------------------------------------
# Release safety — the entry point must never destroy the target organisation
# --------------------------------------------------------------------------

def test_entrypoint_never_calls_clean_org():
    """clean_org() wipes every queue, hook, rule, engine and document in the org.

    It exists for resetting a throwaway test org and is documented as
    manual-only. A call left at module level runs before deploy_cib() on every
    single run, so shipping one would destroy a customer's organisation the
    first time they used the tool -- and it is invisible to secret scanning,
    because it is behaviour rather than a credential. It reached a release
    branch exactly this way once, copied in from a working tree mid-test.
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(__file__).with_name("cib_init_script.py").read_text())
    called = [
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert "clean_org" not in called, (
        "cib_init_script.py calls clean_org(), which would wipe the target "
        "organisation on every run. Remove it before releasing."
    )


def test_destructive_helpers_are_not_imported_by_the_entrypoint():
    """Nothing destructive should be one typo away from running."""
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(__file__).with_name("cib_init_script.py").read_text())
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert not imported & {"clean_org", "delete_annotations", "delete_queues",
                           "delete_hooks", "delete_workspaces", "delete_schemas",
                           "delete_engines", "delete_rules", "delete_inboxes"}


# --------------------------------------------------------------------------
# restore_release_to_pristine — a cached release must not carry edits forward
# --------------------------------------------------------------------------

def _release(tmp_path, hook_template="https://x/api/v1/hook_templates/55"):
    """A cached release shaped like download_cib_release() leaves it."""
    import subprocess
    hooks = tmp_path / "cib-org" / "default" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "Import.json").write_text(json.dumps(
        {"name": "Import", "type": "job", "hook_template": hook_template,
         "config": {"private": True}}))
    (tmp_path / ".gitignore").write_text("cib-org/credentials.yaml\ndeploy_secrets/\n")
    q = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    git = ["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.call(["git", "-C", str(tmp_path), "init", "-q"], **q)
    subprocess.call(git + ["add", "."], **q)
    subprocess.call(git + ["commit", "-m", "CIB", "-q", "--no-gpg-sign"], **q)
    return hooks / "Import.json"


def test_an_earlier_runs_edit_is_undone(tmp_path, capsys):
    """v1.3.0 stripped hook_template and the edit persisted in the cache.

    The next run — even against an organization where the template WAS visible —
    then had no reference for prd2 to match, so prd2 fell through to an
    interactive picker. Restoring first is what makes upgrading the script
    enough to recover, without anyone having to delete the cache by hand.
    """
    hook = _release(tmp_path)
    poisoned = json.loads(hook.read_text())
    poisoned["hook_template"] = None
    hook.write_text(json.dumps(poisoned))

    restore_release_to_pristine(str(tmp_path))

    assert json.loads(hook.read_text())["hook_template"].endswith("/55")
    assert "Restored" in capsys.readouterr().out


def test_a_clean_release_is_left_alone_and_silent(tmp_path, capsys):
    _release(tmp_path)
    restore_release_to_pristine(str(tmp_path))
    assert capsys.readouterr().out == ""


def test_runtime_directories_survive_the_restore(tmp_path):
    """deploy_files/, deploy_states/ and target/ are written after the commit."""
    _release(tmp_path)
    for name in ("deploy_files", "deploy_states", "target"):
        d = tmp_path / name
        d.mkdir()
        (d / "keep.txt").write_text("written at run time")
    (tmp_path / "cib-org" / "credentials.yaml").write_text("token: secret")

    restore_release_to_pristine(str(tmp_path))

    for name in ("deploy_files", "deploy_states", "target"):
        assert (tmp_path / name / "keep.txt").exists(), f"{name} was destroyed"
    assert (tmp_path / "cib-org" / "credentials.yaml").read_text() == "token: secret"


def test_a_release_without_a_baseline_warns_rather_than_failing(tmp_path, capsys):
    (tmp_path / "cib-org").mkdir(parents=True)
    restore_release_to_pristine(str(tmp_path))
    assert "no pristine baseline" in capsys.readouterr().out


# --------------------------------------------------------------------------
# check_target_org_empty — the pre-flight has to cover everything
# verify_deployment counts, or the run aborts after the deploy instead of
# before it
# --------------------------------------------------------------------------

class _Obj:
    def __init__(self, name, status=None):
        self.name = name
        self.status = status


class OrgClient:
    """Lists whatever it was given, per object type."""

    def __init__(self, workspaces=(), queues=(), hooks=(), rules=(), schemas=()):
        self._by_kind = {"workspaces": workspaces, "queues": queues,
                         "hooks": hooks, "rules": rules, "schemas": schemas}
        self.listed = []

    def _list(self, kind):
        self.listed.append(kind)
        return [o if isinstance(o, _Obj) else _Obj(o) for o in self._by_kind[kind]]

    def list_workspaces(self):
        return self._list("workspaces")

    def list_queues(self):
        return self._list("queues")

    def list_hooks(self):
        return self._list("hooks")

    def list_rules(self):
        return self._list("rules")

    def list_schemas(self):
        return self._list("schemas")


def test_leftover_rules_alone_stop_the_deploy(capsys):
    """The exact shape of a UI cleanup: workspaces and queues gone, rules left.

    This passed the old check, so the deploy ran, doubled every rule name and
    only then failed verification — with the whole CIB already created and its
    hooks still pointing at the CIB source Coupa instance.
    """
    client = OrgClient(rules=["Duplicate Detected (CIB)", "Invoice Number Missing (CIB)"])
    with pytest.raises(SystemExit) as exit_info:
        check_target_org_empty(client)
    assert exit_info.value.code == 1
    out = capsys.readouterr().out
    assert "2 rules" in out
    assert "Only rules are left over" in out


def test_rules_are_checked_alongside_the_other_object_types():
    client = OrgClient()
    check_target_org_empty(client)
    assert "rules" in client.listed


def test_leftover_schemas_do_not_block_a_deploy(capsys):
    """A schema whose queue is still being deleted cannot be removed for 24h.

    prd2 creates its own schemas and verify_deployment never counts them, so
    blocking here would cost a day for nothing.
    """
    check_target_org_empty(OrgClient(schemas=["AP Documents - BE"]))
    out = capsys.readouterr().out
    assert "Target org check OK" in out
    assert "1 CIB schema(s)" in out


def test_a_clean_org_passes_quietly(capsys):
    check_target_org_empty(OrgClient(workspaces=["Some other workspace"],
                                     rules=["A customer's own rule"]))
    out = capsys.readouterr().out
    assert "Target org check OK" in out
    assert "NOTE" not in out


def test_rules_awaiting_deletion_are_not_counted():
    client = OrgClient(rules=[_Obj("Duplicate Detected (CIB)", status="deletion_requested")])
    check_target_org_empty(client)  # must not raise
