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
from helpers import (_first_configuration_source, is_newer, neutralize_unresolvable_hook_templates,
                     parse_version, schema_section_children)

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
# neutralize_unresolvable_hook_templates — avoids two distinct deploy failures
# --------------------------------------------------------------------------

class FakeClient:
    """Resolves only the template ids it was told about."""

    def __init__(self, visible=()):
        self.visible = {str(v) for v in visible}
        self.asked = []

    def request_json(self, method, path):
        template_id = path.rsplit("/", 1)[-1]
        self.asked.append(template_id)
        if template_id not in self.visible:
            raise RuntimeError("404")
        return {"id": int(template_id)}


def _hook(directory, name, hook_type, template, private=None):
    directory.mkdir(parents=True, exist_ok=True)
    config = {"private": private} if private is not None else {}
    (directory / f"{name}.json").write_text(json.dumps(
        {"name": name, "type": hook_type, "hook_template": template, "config": config}))


def _template_of(directory, name):
    return json.loads((directory / f"{name}.json").read_text())["hook_template"]


def test_function_template_always_stripped_even_when_it_resolves(tmp_path):
    """The pending-provisioning race happens whether or not the template exists."""
    hooks = tmp_path / "cib-org" / "default" / "hooks"
    _hook(hooks, "Export Pipeline - 1", "function", "https://x/api/v1/hook_templates/50")

    neutralize_unresolvable_hook_templates(FakeClient(visible=[50]), str(tmp_path))

    assert _template_of(hooks, "Export Pipeline - 1") is None


def test_private_nonfunction_stripped_only_when_template_is_invisible(tmp_path):
    """An unresolvable template sends prd2 to an interactive picker it cannot use."""
    hooks = tmp_path / "cib-org" / "default" / "hooks"
    _hook(hooks, "Master Data Import", "job", "https://x/api/v1/hook_templates/55", private=True)

    neutralize_unresolvable_hook_templates(FakeClient(visible=[]), str(tmp_path))

    assert _template_of(hooks, "Master Data Import") is None


def test_resolvable_private_template_is_preserved(tmp_path):
    """MDH and Duplicate Handling must keep creating from their Store template."""
    hooks = tmp_path / "cib-org" / "default" / "hooks"
    _hook(hooks, "MDH - Main", "webhook", "https://x/api/v1/hook_templates/39", private=True)

    neutralize_unresolvable_hook_templates(FakeClient(visible=[39]), str(tmp_path))

    assert _template_of(hooks, "MDH - Main").endswith("/39")


def test_non_private_nonfunction_is_left_alone(tmp_path):
    """Without config.private prd2 never opens the picker, so there is nothing to avoid."""
    hooks = tmp_path / "cib-org" / "default" / "hooks"
    _hook(hooks, "Public webhook", "webhook", "https://x/api/v1/hook_templates/99", private=False)

    neutralize_unresolvable_hook_templates(FakeClient(visible=[]), str(tmp_path))

    assert _template_of(hooks, "Public webhook").endswith("/99")


def test_each_template_is_only_looked_up_once(tmp_path):
    """Twelve hooks share one template; do not issue twelve identical API calls."""
    hooks = tmp_path / "cib-org" / "default" / "hooks"
    for i in range(12):
        _hook(hooks, f"Import {i:02d}", "job", "https://x/api/v1/hook_templates/55", private=True)

    client = FakeClient(visible=[])
    neutralize_unresolvable_hook_templates(client, str(tmp_path))

    assert client.asked == ["55"]


def test_is_idempotent(tmp_path):
    hooks = tmp_path / "cib-org" / "default" / "hooks"
    _hook(hooks, "Master Data Import", "job", "https://x/api/v1/hook_templates/55", private=True)
    client = FakeClient(visible=[])

    neutralize_unresolvable_hook_templates(client, str(tmp_path))
    neutralize_unresolvable_hook_templates(client, str(tmp_path))

    assert _template_of(hooks, "Master Data Import") is None
    assert client.asked == ["55"], "second pass should find nothing left to check"


def test_missing_hooks_directory_is_not_fatal(tmp_path):
    neutralize_unresolvable_hook_templates(FakeClient(), str(tmp_path))


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
