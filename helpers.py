import base64
import csv
import io
import json
import logging
import re
import requests
import shutil
import yaml
import zipfile
from rossum_api import APIClientError
from urllib.parse import urlparse
import subprocess
import socket
import time
import os

GITHUB_REPO = "rossumai/rossum-coupa-integration"
DEPLOY_TOOL_REPO = "rossumai/coupa-integration-deploy-tool"
CIB_ORG_DIR = "cib-org"
CIB_SOURCE_DIR = "cib-org/default"
CIB_TARGET_DIR = "target"
CIB_DEPLOY_FILE = "cib_target.yaml"
CIB_SECRETS_FILE = "cib_target_secrets.json"
import sys
if sys.platform == "win32":
    CIB_RELEASES_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "cib_releases")
else:
    CIB_RELEASES_DIR = os.path.join(os.path.expanduser("~"), ".cib_releases")

def base_url(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _load_required_scopes(path):
    """Load the minimal CIB scope map: {scope -> datasets it unlocks}.

    The map is version data — it must describe the hooks of the release being
    deployed — so it travels with the release (see cib_assets.resolve_assets)
    rather than being hardcoded here. Returns the map, or None if the file is
    missing or malformed; callers degrade to a best-effort skip rather than
    aborting.
    """
    try:
        with open(path, encoding="utf-8") as f:
            scopes = json.load(f).get("required_scopes")
        if isinstance(scopes, dict) and scopes:
            return scopes
    except (OSError, ValueError):
        pass
    return None


def _decode_token_scopes(access_token):
    """Return the set of scopes granted in a Coupa access token.

    Coupa returns the granted scopes in the access_token JWT's `scope` claim,
    NOT in the token response body (where `scope` is null). Decode the JWT
    payload (the middle segment, base64url) and split the claim.
    """
    payload = access_token.split(".")[1]
    payload += "=" * (-len(payload) % 4)  # restore base64url padding
    claim = json.loads(base64.urlsafe_b64decode(payload)).get("scope", "")
    return set(claim.split())


def verify_credentials(coupa, assets):
    """Fail fast if the Coupa OAuth credentials are invalid or under-scoped.

    A wrong or under-provisioned credential is invisible at run time: the Rossum
    import hook returns HTTP 202/"completed" regardless, and an empty import
    dataset looks identical whether the tenant has no rows or the credential
    simply lacks that endpoint's scope. So verify here, before any resources are
    created:
      - request a client_credentials token (catches wrong key/secret -> 401),
      - decode the granted scopes from the access_token JWT,
      - diff against REQUIRED_CIB_SCOPES.
    Missing scopes abort the deploy; extra scopes are an advisory (least
    privilege). A probe error we cannot interpret (network failure, opaque
    non-JWT token) warns and proceeds, matching check_region's best-effort stance.
    """
    scope_unlocks = _load_required_scopes(assets.required_scopes)
    if not scope_unlocks:
        print(f"  WARNING: could not load {assets.required_scopes}; "
              f"skipping Coupa credential scope check.")
        return
    required = set(scope_unlocks)

    token_url = f"{coupa['coupa_base_api_url']}oauth2/token"
    try:
        resp = requests.post(token_url, data={
            "grant_type": "client_credentials",
            "client_id": coupa["client_id"],
            "client_secret": coupa["client_secret"],
        }, timeout=30)
    except requests.RequestException as e:
        print(f"  WARNING: could not reach the Coupa token endpoint ({e}); "
              f"skipping credential check. Verify Coupa creds manually.")
        return

    if not resp.ok:
        print(f"\nERROR: Coupa rejected the OAuth credentials "
              f"(HTTP {resp.status_code}): {resp.text.strip()[:300]}")
        print("Check coupa.client_id / coupa.client_secret in config.json and re-run.")
        sys.exit(1)

    try:
        granted = _decode_token_scopes(resp.json().get("access_token", ""))
    except Exception as e:
        print(f"  WARNING: could not decode granted scopes from the Coupa token "
              f"({e}); credentials are valid but scope was not verified.")
        return

    missing = required - granted
    extra = granted - required

    if missing:
        print("\nERROR: the Coupa credential is missing scope(s) CIB requires. The "
              "affected datasets would import/export NOTHING while the hook still "
              "returns HTTP 202:")
        for s in sorted(missing):
            print(f"    - {s}  -> {scope_unlocks.get(s, '?')}")
        print("Grant the missing scope(s) to the Coupa OAuth app and re-run.")
        sys.exit(1)

    if extra:
        print(f"  NOTE: the Coupa credential holds {len(extra)} scope(s) beyond what "
              f"CIB uses (least-privilege): {' '.join(sorted(extra))}")
    print("  Coupa credential OK: valid and scoped for all CIB imports + export.")

# Canonical cluster host -> target_rossum_instance region key.
CLUSTER_HOSTS = {
    "elis.rossum.ai": "prod-eu",
    "shared-eu2.rossum.app": "prod-eu2",
    "us.app.rossum.ai": "prod-us2",
    "shared-jp.app.rossum.ai": "prod-jp",
}


def detect_region(api_base_url):
    """Best-effort detect the org's cluster/region from its API URL via DNS.

    A real org's domain (incl. vanity domains) resolves, via CNAME, to its
    cluster's canonical host; for anything else we fall back to matching the
    resolved IP set against the known cluster hosts (resolved live, since the
    load-balancer IPs rotate). Returns a region key or None if undetermined.
    """
    host = urlparse(api_base_url).netloc.split("@")[-1].split(":")[0]
    try:
        canonical, _, ips = socket.gethostbyname_ex(host)
    except OSError:
        return None
    if canonical in CLUSTER_HOSTS:
        return CLUSTER_HOSTS[canonical]
    org_ips = set(ips)
    for cluster_host, region in CLUSTER_HOSTS.items():
        try:
            if org_ips & set(socket.gethostbyname_ex(cluster_host)[2]):
                return region
        except OSError:
            continue
    return None


def check_region(rossum):
    """Fail fast if target_rossum_instance does not match the org's real region.

    Deploying to the wrong region silently breaks Coupa imports (the
    scheduled-imports service 202s but cannot write back) and mis-points the
    per-cluster export hooks. Run this before anything is created. If the region
    cannot be auto-detected (e.g. a custom domain), warn and proceed.
    """
    configured = rossum.get("target_rossum_instance")
    detected = detect_region(rossum["api_base_url"])
    if detected is None:
        print(f"  WARNING: could not auto-detect the region for {rossum['api_base_url']} "
              f"via DNS; proceeding with configured '{configured}'. Verify it is correct.")
        return
    if detected != configured:
        print(f"\nERROR: target_rossum_instance is '{configured}', but {rossum['api_base_url']} "
              f"resolves to region '{detected}'.")
        print("Deploying to the wrong region silently breaks Coupa imports and mis-points "
              "export hooks.")
        print(f"Set \"target_rossum_instance\": \"{detected}\" in config.json and re-run.")
        sys.exit(1)
    print(f"  Region check OK: '{configured}' matches the org's API domain.")

 
def check_org_features(client, rossum, prd_path):
    """Fail fast when the target organization group cannot host this CIB release.

    CIB's own organization group carries feature flags that a customer org may
    not have, and the resulting failures are late and misleading:

      - `maximum_hook_timeout`: CIB 2.0's five export-pipeline hooks declare
        config.timeout_s 360 because the Coupa draft-creation call alone allows
        320s. Without the flag the org caps at 60 and POST /hooks returns
        400 "Ensure this value is less than or equal to 60" -- per hook, which
        prd2 logs inline and then carries on, exiting 0 with no export chain.
      - `einvoicing`: needed for the e-invoicing inbox. Advisory, because the
        AP queues work without it. Note that the XML mime types the inbox needs
        are carried in its own queue.accepted_mime_types and deploy with the
        queue, so they are NOT an org-group prerequisite.

    Everything here is best-effort: a token that cannot read the organization
    group warns and proceeds rather than blocking a deploy that might be fine.
    """
    # The TARGET org from config.json, not the token owner's own organization:
    # a support/group-admin token belongs to a different org entirely.
    try:
        org = client.request_json("GET", f"organizations/{rossum['org_id']}")
        group = client.request_json("GET", org["organization_group"])
    except Exception as e:
        print(f"  WARNING: could not read the target organization group ({e}); "
              f"skipping the feature pre-check.")
        return
    features = group.get("features") or {}

    needed = _max_hook_timeout_in_release(prd_path)
    allowed_feature = features.get("maximum_hook_timeout") or {}
    allowed = allowed_feature.get("seconds", 60) if allowed_feature.get("enabled") else 60
    if needed > allowed:
        print(f"\nERROR: this CIB release needs hooks with config.timeout_s up to {needed}s, but "
              f"organization group '{group.get('name')}' ({group.get('id')}) allows at most "
              f"{allowed}s.")
        print("Every hook above the cap is rejected with HTTP 400 'Ensure this value is less than "
              "or equal to 60'. prd2 logs that per hook and still exits 0, so the deploy would")
        print("look successful while silently leaving the org with no export pipeline.")
        print(f"\nAsk Rossum support to enable 'maximum_hook_timeout' ({needed}s) for that "
              f"organization group, then re-run.")
        sys.exit(1)

    einvoicing = features.get("einvoicing")
    if not (einvoicing or {}).get("enabled"):
        state = "absent" if einvoicing is None else f"present but not enabled ({json.dumps(einvoicing)})"
        print(f"  NOTE: organization group '{group.get('name')}': 'einvoicing' is {state}. "
              f"The AP queues are unaffected; the e-invoicing inbox will not work until "
              f"Rossum support enables it.")
    print(f"  Org feature check OK: hook timeout up to {allowed}s available "
          f"(release needs {needed}s).")


def _max_hook_timeout_in_release(prd_path):
    """Highest config.timeout_s declared by any hook in the release."""
    hooks_dir = os.path.join(prd_path, CIB_SOURCE_DIR, "hooks")
    highest = 0
    if not os.path.isdir(hooks_dir):
        return highest
    for filename in os.listdir(hooks_dir):
        if not filename.endswith(".json"):
            continue
        try:
            with open(os.path.join(hooks_dir, filename), encoding="utf-8") as f:
                timeout = (json.load(f).get("config") or {}).get("timeout_s")
        except (OSError, ValueError):
            continue
        if isinstance(timeout, int):
            highest = max(highest, timeout)
    return highest


def normalize_base_url(url: str) -> str:
    """Normalise a Coupa base API URL: ensure a scheme and exactly one trailing slash.

    The hook URLs are built by raw f-string concatenation (e.g. f"{url}oauth2/token"),
    so a scheme-less or non-slash-terminated value silently produces an invalid URL that
    only fails much later when the import runs. Normalise once, on load.
    """
    url = (url or "").strip()
    if not url:
        return url
    if not urlparse(url).scheme:
        url = "https://" + url
    return url.rstrip("/") + "/"


def check_prd2_available():
    """Verify prd2 is installed and supports the --ld (local deploy) flag.

    This script runs `prd2 deploy run ... --ld`; the --ld flag first ships in prd2
    v2.18.1. Abort early with a clear message rather than letting the deploy fail mid-run.
    """
    try:
        proc = subprocess.run(["prd2", "deploy", "run", "--help"], capture_output=True, text=True)
    except FileNotFoundError:
        print(
            "Error: prd2 is not installed or not on PATH.\n"
            "Install it with:\n"
            "  pipx install git+https://github.com/rossumai/deployment-manager.git@v2.18.1"
        )
        sys.exit(1)
    if "--ld" not in (proc.stdout + proc.stderr):
        try:
            ver = subprocess.run(["prd2", "--version"], capture_output=True, text=True).stdout.strip()
        except Exception:
            ver = "unknown"
        print(
            f"Error: your prd2 ({ver}) does not support the --ld flag required by this script.\n"
            "Upgrade to prd2 v2.18.1 or later:\n"
            "  pipx install --force git+https://github.com/rossumai/deployment-manager.git@v2.18.1"
        )
        sys.exit(1)

MIN_ROSSUM_API = "3.16.1"


def check_rossum_api_version():
    """Abort if rossum-api is too old to page an organization's list endpoints.

    Rossum is retiring page-count pagination: an organization whose group lacks
    the `old_pagination_count` feature returns `pagination` as
    {next, previous} with no `total_pages`. Clients before 3.16.1 read
    data["pagination"]["total_pages"] unguarded and every list_* call dies with

        KeyError: 'total_pages'

    which surfaces as a traceback from deep inside the client and looks nothing
    like a dependency problem. 3.16.1 sends `include_total=true` on list
    requests, which makes the API return the count again.

    Newer organizations are the ones without the feature, so this bites exactly
    on fresh customer orgs -- the case that matters most.
    """
    try:
        import importlib.metadata as importlib_metadata
        installed = importlib_metadata.version("rossum-api")
    except Exception:
        return  # cannot determine; let the run proceed rather than block on metadata

    if not is_newer(MIN_ROSSUM_API, installed):
        return  # installed >= minimum

    print(f"\nError: rossum-api {installed} is too old; this script needs "
          f"{MIN_ROSSUM_API} or later.")
    print("Organizations without the 'old_pagination_count' feature omit "
          "'total_pages' from list responses, and older clients fail every")
    print("list call with KeyError: 'total_pages'. Upgrade with:")
    print("  pip install --upgrade 'rossum-api>=" + MIN_ROSSUM_API + "'")
    print("  (or: pipenv sync, after pulling the current Pipfile.lock)")
    sys.exit(1)


def update_prd_credentials(target_token, path):
    # Source credentials — placeholder token, --ld skips source API validation
    source_cred_path = os.path.join(path, CIB_ORG_DIR, "credentials.yaml")
    with open(source_cred_path, "w") as f:
        yaml.dump({"token": "local"}, f)

    # Target credentials
    target_dir = os.path.join(path, CIB_TARGET_DIR)
    os.makedirs(target_dir, exist_ok=True)
    with open(os.path.join(target_dir, "credentials.yaml"), "w") as f:
        yaml.dump({"token": target_token}, f)

    # Remove stale target data from any previous run
    org_json = os.path.join(target_dir, "organization.json")
    if os.path.exists(org_json):
        os.remove(org_json)
    target_subdir = os.path.join(target_dir, CIB_TARGET_DIR)
    if os.path.exists(target_subdir):
        shutil.rmtree(target_subdir)



# Mirrors prd2's FORBIDDEN_CHARS_REGEX (utils/functions.py). Object directories
# are named with the stripped form, so this has to match or paths will not resolve.
FORBIDDEN_PATH_CHARS = re.compile(r"[/\\\"'`]")


def templatize_name_id(name, id_):
    """prd2's on-disk directory/file naming: '<name sans forbidden chars>_[<id>]'."""
    return f"{re.sub(FORBIDDEN_PATH_CHARS, '', name)}_[{id_}]"


def schema_section_children(schema_path, field_ids):
    """Return which of `field_ids` sit directly under a top-level schema section.

    Mirrors what the JMESPath "content[].children[?id=='X']" would match, so a
    caller can build attribute overrides that are guaranteed to resolve. An
    unreadable schema yields nothing, which degrades to "no override" rather
    than to a failed deploy.
    """
    try:
        with open(schema_path, encoding="utf-8") as f:
            content = json.load(f).get("content") or []
    except (OSError, ValueError):
        return set()

    present = set()
    for section in content:
        if not isinstance(section, dict):
            continue
        for child in section.get("children") or []:
            if isinstance(child, dict) and child.get("id") in field_ids:
                present.add(child["id"])
    return present


def update_prd_mapping(client, rossum_api_url, org_id, admin_user, path, client_id, client_secret, coupa_base_url):
    deploy_file_path = os.path.join(path, "deploy_files", CIB_DEPLOY_FILE)
    with open(deploy_file_path) as f:
        data = yaml.safe_load(f)

    data["token_owner_id"] = resolve_token_owner_id(client, admin_user)
    data["deployed_org_id"] = None
    data["patch_target_org"] = False
    data["target_url"] = rossum_api_url
    data["source_dir"] = CIB_SOURCE_DIR

    for queue in data["queues"]:
        queue["ignore_deploy_warnings"] = True
        queue["targets"][0]["id"] = None
        if queue.get("inbox"):
            queue["inbox"]["targets"][0]["id"] = None
        queue["base_path"] = queue["base_path"].replace("cib/cib", CIB_SOURCE_DIR)

        # Only override fields the queue's schema actually has. prd2's
        # perform_search() RAISES on a JMESPath that matches nothing, aborting
        # the whole deploy during planning -- so a blanket override breaks any
        # queue without these datapoints. CIB 2.0's E-invoicing Inbox is exactly
        # that: it never exports, so it carries no Coupa credential fields.
        schema_path = os.path.join(
            path, queue["base_path"], "queues",
            templatize_name_id(queue["name"], queue["id"]), "schema.json")
        present = schema_section_children(schema_path, {"oauth_client_id", "coupa_api_base_url"})
        values = {"oauth_client_id": client_id, "coupa_api_base_url": coupa_base_url}
        override = {}
        for field_id in sorted(present):
            value = values[field_id]
            override[f"content[].children[?id=='{field_id}'].default_value"] = value
            override[f"content[].children[?id=='{field_id}'].formula"] = f"'{value}'"

        target = {"id": None}
        if override:
            target["attribute_override"] = override
        else:
            print(f"  NOTE: queue '{queue['name']}' has no Coupa credential fields in its "
                  f"schema; skipping the credential override for it.")
        queue["schema"]["targets"] = [target]

    for hook in data["hooks"]:
        hook["targets"][0]["id"] = None

    for workspace in data["workspaces"]:
        workspace["targets"][0]["id"] = None

    for rule in data.get("rules", []):
        rule["targets"][0]["id"] = None

    for engine in data.get("engines", []):
        engine["targets"][0]["id"] = None
        engine["base_path"] = engine["base_path"].replace("cib/cib", CIB_SOURCE_DIR)
        for engine_field in engine.get("engine_fields", []):
            engine_field["targets"][0]["id"] = None

    with open(deploy_file_path, "w") as f:
        yaml.dump(data, f)

    prd_config_path = os.path.join(path, "prd_config.yaml")
    with open(prd_config_path) as f:
        prd_config = yaml.safe_load(f)
    prd_config.setdefault("directories", {})["target"] = {
        "api_base": rossum_api_url,
        "org_id": str(org_id),
        "subdirectories": {"target": {"regex": ""}}
    }
    with open(prd_config_path, "w") as f:
        yaml.dump(prd_config, f)

    secrets_path = os.path.join(path, "deploy_secrets", CIB_SECRETS_FILE)
    with open(secrets_path) as f:
        secrets = json.load(f)
    for key in secrets:
        secrets[key] = {"client_secret": client_secret}
    with open(secrets_path, "w") as f:
        json.dump(secrets, f)

    deploy_files_dir = os.path.join(path, "deploy_files")
    for f in os.listdir(deploy_files_dir):
        if f.endswith("_deployed.yaml"):
            os.remove(os.path.join(deploy_files_dir, f))


def check_script_version():
    try:
        version_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")
        with open(version_path) as f:
            current = f.read().strip()
        resp = requests.get(f"https://api.github.com/repos/{DEPLOY_TOOL_REPO}/releases/latest", timeout=10)
        resp.raise_for_status()
        latest = resp.json()["tag_name"]
        # Ordered comparison, not inequality: while a version is in development
        # the local VERSION runs ahead of the newest published release, and
        # offering that as an "update" would git-pull the work away.
        if not is_newer(latest, current):
            return
        print(f"\nA newer version of this deploy script is available: {latest} (you have {current})")
        print(f"Update now? [y/N]: ", end='', flush=True)
        answer = sys.stdin.readline().strip().lower()
        if answer != 'y':
            print("Continuing with current version.\n")
            return
        script_dir = os.path.dirname(os.path.abspath(__file__))
        if os.path.exists(os.path.join(script_dir, ".git")):
            print("Running git pull...")
            result = subprocess.call(["git", "pull"], cwd=script_dir)
            if result == 0:
                print(f"\nUpdated to {latest}. Please restart the script.")
            else:
                print(f"\ngit pull failed. Download the latest version from:\nhttps://github.com/{DEPLOY_TOOL_REPO}/releases")
        else:
            print(f"\nDownload the latest version from:\nhttps://github.com/{DEPLOY_TOOL_REPO}/releases")
        sys.exit(0)
    except Exception as e:
        print(f"Could not check script version: {e}")


def get_latest_cib_version():
    resp = requests.get(f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest", timeout=10)
    resp.raise_for_status()
    return resp.json()["tag_name"]


def download_cib_release(version):
    release_dir = os.path.join(CIB_RELEASES_DIR, version)
    if os.path.exists(release_dir):
        print(f"Using cached CIB release {version}")
        return release_dir

    print(f"Downloading CIB release {version} from GitHub...")
    url = f"https://api.github.com/repos/{GITHUB_REPO}/zipball/{version}"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()

    tmp_dir = release_dir + "_tmp"
    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        top = z.namelist()[0].split("/")[0]
        z.extractall(tmp_dir)

    shutil.move(os.path.join(tmp_dir, top), release_dir)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    # Init a git repo so prd2 can commit deploy state after each run.
    # .gitignore must be written first so credentials are never committed.
    with open(os.path.join(release_dir, ".gitignore"), "w") as _f:
        _f.write("cib-org/credentials.yaml\ntarget/credentials.yaml\ndeploy_secrets/\n")
    subprocess.call(["git", "init", "-q"], cwd=release_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.call(["git", "-c", "user.email=noreply@rossum.ai", "-c", "user.name=CIB", "add", "."], cwd=release_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.call(["git", "-c", "user.email=noreply@rossum.ai", "-c", "user.name=CIB", "commit", "-m", f"CIB {version}", "-q", "--no-gpg-sign"], cwd=release_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"CIB release {version} ready at {release_dir}")
    return release_dir


def parse_version(tag):
    """Parse 'v2.0.0' or 'v2.0.0-rc1' into a sortable tuple, or None if unparseable.

    Semver ordering: a pre-release sorts BEFORE its final release, so
    v2.0.0-rc1 < v2.0.0. Encoded as (major, minor, patch, 0, prerelease) vs
    (major, minor, patch, 1, "").
    """
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:-(.+))?", (tag or "").strip())
    if not match:
        return None
    major, minor, patch, prerelease = match.groups()
    return (int(major), int(minor), int(patch), 0 if prerelease else 1, prerelease or "")


def is_newer(candidate, current):
    """True only when `candidate` is strictly newer than `current`.

    Unparseable tags fall back to "different means newer", matching the
    behaviour this check had before versions could run ahead of the published
    ones — better to offer a pointless upgrade than to hide a real one.
    """
    a, b = parse_version(candidate), parse_version(current)
    if a is None or b is None:
        return candidate != current
    return a > b


def select_cib_version(rossum):
    """Return the CIB version to deploy, offering the latest release if newer.

    Accepting the offer is safe whatever the version: from CIB 2.0 each release
    carries its own deploy assets, and resolve_assets() aborts rather than
    pairing a release with another version's object IDs.

    The comparison is ordered, not just inequality. A config pinned to a version
    that is not published yet — an unreleased 2.0.0 during testing, or a release
    candidate — must not be offered the older published tag as an "upgrade",
    because accepting would silently downgrade the deploy and rewrite config.json.
    """
    version = rossum["cib_version"]
    try:
        latest = get_latest_cib_version()
    except Exception as e:
        print(f"Could not check for latest CIB version: {e}")
        return version

    if not is_newer(latest, version):
        return version

    print(f"\nA newer CIB version is available: {latest} (configured: {version})")
    print(f"Download and use {latest} instead? [y/N]: ", end='', flush=True)
    if sys.stdin.readline().strip().lower() != 'y':
        print(f"Continuing with configured version {version}\n")
        return version

    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    with open(config_path) as f:
        cfg = json.load(f)
    cfg["rossum"]["cib_version"] = latest
    with open(config_path, "w") as f:
        json.dump(cfg, f, indent=2)
    print(f"config.json updated to {latest}\n")
    return latest


def init_prd_release(client, rossum, coupa, prd_path, assets):
    """Stage the resolved deploy assets into the release and run prd2."""
    os.makedirs(os.path.join(prd_path, "deploy_files"), exist_ok=True)
    os.makedirs(os.path.join(prd_path, "deploy_secrets"), exist_ok=True)
    os.makedirs(os.path.join(prd_path, "deploy_states"), exist_ok=True)
    # Copy rather than edit in place: update_prd_mapping mutates the deploy file
    # (null target IDs, token owner, Coupa credentials), and the resolved assets
    # must stay pristine so re-runs start from the same baseline.
    shutil.copy(assets.deploy_file, os.path.join(prd_path, "deploy_files", CIB_DEPLOY_FILE))
    shutil.copy(assets.secrets_file, os.path.join(prd_path, "deploy_secrets", CIB_SECRETS_FILE))

    update_prd_credentials(rossum["target_org_token"], prd_path)
    update_prd_mapping(client, rossum["api_base_url"], rossum["org_id"], rossum["token_owner_username"], prd_path, coupa["client_id"], coupa["client_secret"], coupa["coupa_base_api_url"])

    neutralize_function_hook_templates(prd_path)

    deploy_file_path = os.path.join("deploy_files", CIB_DEPLOY_FILE)
    proc = subprocess.Popen(
        ["prd2", "deploy", "run", deploy_file_path, "--auto-apply", "--ld"],
        cwd=prd_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding='utf-8',
        errors='replace',
    )
    # prd2 prints a "Planning failed" banner and STILL exits 0 when a deploy
    # aborts during planning (e.g. an invalid token owner or a 403 creating
    # engines). Relying on the return code alone lets the script march on and
    # configure hooks against a half-built deploy. Detect the banner too.
    planning_failed = False
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        if "Planning failed" in line:
            planning_failed = True
    proc.wait()
    if proc.returncode != 0 or planning_failed:
        detail = f"exit code {proc.returncode}" + ("; planning failed" if planning_failed else "")
        print(f"\nError: 'prd2 deploy run' failed ({detail}). Aborting before hook configuration.")
        sys.exit(1)


def verify_deployment(client, prd_path):
    """Abort if prd2 created only part of what the deploy file asked for.

    prd2 reports a per-object create failure inline and then CARRIES ON: the run
    still exits 0 and prints no "Planning failed" banner, so the existing return
    code and banner checks both pass. On prod-eu the five Export Pipeline hooks
    were rejected (config.timeout_s above that organization's 60s cap) and the
    deploy looked entirely successful while leaving an org with no export chain
    at all -- every later step then reported OK because it only ever looks at
    the objects that do exist.

    Compare the deploy file against the target by name. Names are reproduced
    verbatim by the deploy, and target IDs are not knowable up front, so name is
    the right key. Duplicates matter as much as absences: a hook created twice
    is a hook whose configuration went to the wrong copy.
    """
    deploy_file_path = os.path.join(prd_path, "deploy_files", CIB_DEPLOY_FILE)
    try:
        with open(deploy_file_path) as f:
            data = yaml.safe_load(f)
    except (OSError, ValueError) as e:
        print(f"  WARNING: could not re-read {deploy_file_path} ({e}); skipping deploy verification.")
        return

    def live(fetch):
        return [o.name for o in fetch()
                if o.name and getattr(o, "status", None) != "deletion_requested"]

    groups = [
        ("workspaces", [w["name"] for w in data.get("workspaces", [])], client.list_workspaces),
        ("queues", [q["name"] for q in data.get("queues", [])], client.list_queues),
        ("hooks", [h["name"] for h in data.get("hooks", [])], client.list_hooks),
        ("rules", [r["name"] for r in data.get("rules", [])], client.list_rules),
    ]

    problems = []
    for label, expected, fetch in groups:
        if not expected:
            continue
        try:
            actual = live(fetch)
        except APIClientError as e:
            print(f"  WARNING: could not list {label} to verify the deploy ({e}).")
            continue
        counts = {}
        for name in actual:
            counts[name] = counts.get(name, 0) + 1
        missing = sorted(n for n in set(expected) if n not in counts)
        # The deploy file may legitimately name the same object once, so any
        # count above the number of times it was requested is a duplicate.
        requested = {}
        for name in expected:
            requested[name] = requested.get(name, 0) + 1
        duplicated = sorted(n for n in set(expected) if counts.get(n, 0) > requested[n])
        if missing or duplicated:
            problems.append((label, len(expected), missing, duplicated))

    if not problems:
        print("  Deploy verification OK: every object in the deploy file exists in the target org.")
        return

    print("\nERROR: the deploy is incomplete. prd2 reports per-object failures inline and still")
    print("exits 0, so this would otherwise look like a successful deploy.")
    for label, total, missing, duplicated in problems:
        if missing:
            print(f"\n  {len(missing)} of {total} {label} were NOT created:")
            for name in missing:
                print(f"    - {name}")
        if duplicated:
            print(f"\n  {len(duplicated)} {label} exist more than once:")
            for name in duplicated:
                print(f"    - {name}")
    print("\nSearch the log above for 'Error while creating' to see why. A frequent cause is an")
    print("organization limit the source org does not have -- e.g. CIB 2.0's export pipeline")
    print("needs config.timeout_s of 360s, which orgs without the extended hook timeout reject")
    print("with 'Ensure this value is less than or equal to 60'. Those are enabled by Rossum")
    print("support, not by this script.")
    print("\nFix the cause, clean the target organization, and re-run. Continuing now would")
    print("configure only the objects that exist and report success.")
    sys.exit(1)


def csv_to_dict(csv_file_path):
    with open(csv_file_path, mode='r', encoding='utf-8') as csv_file:
        # Read CSV data
        csv_reader = csv.DictReader(csv_file)
        # Convert to list of dictionaries
        data = [row for row in csv_reader]

    return data


def json_to_dict(json_file_path):
    with open(json_file_path, mode='r', encoding='utf-8') as json_file:
        # Load JSON data into a dictionary
        data = json.load(json_file)

    return data


def _first_configuration_source(settings):
    """Return settings.configurations[0].source, or {} if it isn't shaped that way.

    'configurations' is used by several unrelated extensions: MDH lookups nest a
    'source' dict under each configuration, while Duplicate Handling and Coupa
    E-Invoicing use the same key for entirely different structures with no
    'source' at all.
    """
    configurations = settings.get('configurations')
    if not isinstance(configurations, list) or not configurations:
        return {}
    first = configurations[0]
    if not isinstance(first, dict):
        return {}
    source = first.get('source')
    return source if isinstance(source, dict) else {}


def handle_hooks(rossum, coupa, client, assets):
    print("\nConfiguring hooks...")
    hooks = csv_to_dict(assets.hooks_csv)
    hooks_rossum = client.list_hooks()
    matched = 0
    for hook_rossum in hooks_rossum:
        for hook in hooks:
            if hook_rossum.name == hook["hook_name"]:
                matched += 1
                print(f"  {hook_rossum.name}")
                settings = hook_rossum.settings
                if hook["prod-eu-url"]:
                    target_url = None
                    if rossum['target_rossum_instance'] == 'prod-eu':
                        target_url = hook["prod-eu-url"]
                    elif rossum['target_rossum_instance'] == 'prod-eu2':
                        target_url = hook["prod-eu2-url"]
                    elif rossum['target_rossum_instance'] == 'prod-us2':
                        target_url = hook["prod-us2-url"]
                    elif rossum['target_rossum_instance'] == 'prod-jp':
                        target_url = hook["prod-jp-url"]
                    # The scheduled-imports service is served on the org's cluster
                    # gateway (e.g. us.app.rossum.ai) — NOT on the org's vanity API
                    # domain (e.g. <org>.rossum.app), which only fronts /api and
                    # returns nginx 405 for the /svc/scheduled-imports/ POST. Use the
                    # per-cluster host from hooks.csv keyed by target_rossum_instance;
                    # check_region() has already aborted the deploy if that instance
                    # does not match the org's real region, so this URL is correct.
                    if target_url:
                        client.update_part_hook(hook_rossum.id, {"config": {"url": target_url}})
                        print(f"    -> URL: {target_url}")
                # Coupa credentials live in three different shapes depending on the
                # extension. Each branch is keyed on the shape rather than on the
                # hook name, so a new hook using a known shape is handled for free.
                #
                # 1. Import webhooks: settings.credentials.{client_id,base_api_url}
                if isinstance(settings.get('credentials'), dict) and 'client_id' in settings['credentials']:
                    settings['credentials']['client_id'] = coupa['client_id']
                    settings['credentials']['base_api_url'] = coupa['coupa_base_api_url']
                    client.update_part_hook(hook_rossum.id, {"settings": settings})
                    print(f"    -> Coupa credentials updated")
                # 2. MDH lookup against the Coupa API: settings.configurations[0].source.auth.
                #    Other 'configurations' extensions (Duplicate Handling, Coupa
                #    E-Invoicing) have no 'source' key at all, so walk the shape
                #    defensively — a bare subscript here used to raise KeyError.
                source = _first_configuration_source(settings)
                if isinstance(source.get('auth'), dict):
                    source['auth']['url'] = f"{coupa['coupa_base_api_url']}oauth2/token"
                    source['auth'].setdefault('body', {})['client_id'] = coupa['client_id']
                    queries = source.get('queries')
                    if isinstance(queries, list) and queries:
                        queries[0]['url'] = f"{coupa['coupa_base_api_url']}api/invoices/"
                    client.update_part_hook(hook_rossum.id, {"settings": settings})
                    print(f"    -> Import source updated")
                # 3. E-invoicing status sync on the inbox queue: the credentials sit at
                #    the top level of settings, because that queue's schema has no
                #    coupa_api_base_url / oauth_client_id datapoints to read them from.
                if 'client_id' in settings or 'coupa_base_url' in settings:
                    if 'client_id' in settings:
                        settings['client_id'] = coupa['client_id']
                    if 'coupa_base_url' in settings:
                        settings['coupa_base_url'] = coupa['coupa_base_api_url']
                    client.update_part_hook(hook_rossum.id, {"settings": settings})
                    print(f"    -> Coupa settings updated")
                if hook["patch_secret"] == 'true':
                    secrets = {"secrets": {"client_secret": coupa["client_secret"]}}
                    client.update_part_hook(hook_rossum.id, secrets)
                    print(f"    -> Secret patched")
                if hook['invoke'] == 'true':
                    try:
                        client.request("POST", url=f"{rossum['api_base_url']}/hooks/{hook_rossum.id}/invoke")
                        print(f"    -> Invoked")
                    except APIClientError as e:
                        # Best-effort "kick off an initial import now". Never abort the
                        # whole deploy on one hook — the import also runs on its cron
                        # schedule, and verify_imports() checks the data actually landed.
                        print(f"    -> WARNING: invoke failed, continuing ({e})")
    print(f"Hooks done ({matched} configured).")


def neutralize_function_hook_templates(prd_path):
    """Drop `hook_template` from serverless-function hooks before deploying.

    When a hook carries a hook_template, prd2 creates it with
    POST /hooks/create (name + template + owner + events only) and then PATCHes
    the real configuration in. That is fine for a webhook, but Rossum provisions
    a serverless function asynchronously: the new hook sits in status "pending"
    and the immediate PATCH is rejected with

        400 Function couldn't be updated. Function is in status pending

    prd2 records the create as failed, and its second deploy pass creates
    ANOTHER hook, which fails the same way. The result is two orphans with no
    queues and no run_after. In CIB 2.0 that silently breaks the entire export
    chain, because stages 2-5 all run_after stage 1.

    Without a template prd2 uses the plain POST /hooks path, which creates the
    function complete in one call. Export Pipeline stages 2-5 already carry no
    template and deploy correctly, so this only makes stage 1 behave like its
    siblings. Webhook templates (MDH, Duplicate Handling) are deliberately left
    alone: they have no provisioning delay, and the store link is worth keeping.
    """
    hooks_dir = os.path.join(prd_path, CIB_SOURCE_DIR, "hooks")
    if not os.path.isdir(hooks_dir):
        return

    patched = []
    for filename in sorted(os.listdir(hooks_dir)):
        if not filename.endswith(".json"):
            continue
        hook_path = os.path.join(hooks_dir, filename)
        try:
            with open(hook_path, encoding="utf-8") as f:
                hook = json.load(f)
        except (OSError, ValueError):
            continue
        if hook.get("type") != "function" or not hook.get("hook_template"):
            continue
        hook["hook_template"] = None
        with open(hook_path, "w", encoding="utf-8") as f:
            json.dump(hook, f, indent=2)
        patched.append(hook.get("name", filename))

    if patched:
        print(f"  Removed hook_template from {len(patched)} function hook(s) so prd2 creates "
              f"them in one call (Rossum rejects a PATCH while a function is provisioning):")
        for name in patched:
            print(f"    - {name}")


# Object-name markers that mean "a CIB deploy already happened here". Matching on
# the "(CIB)" suffix would miss the queues and workspace, which carry no suffix.
CIB_MARKERS = ("(CIB)", "Coupa Integration Baseline", "AP Documents")


def check_target_org_empty(client):
    """Abort if the target organisation already contains CIB objects.

    Every run is a fresh create: update_prd_mapping() nulls deployed_org_id and
    every target id, so a second run against the same organisation builds a
    duplicate set of queues, hooks and rules rather than updating the first.
    CIB 2.0 has no in-place upgrade path from 1.x, so the safe behaviour is to
    stop and let the operator choose a clean org.
    """
    def names(fetch):
        try:
            # Deleting a queue only marks it 'deletion_requested'; Rossum keeps the
            # tombstone for up to 24 hours and still lists it. Counting those as
            # "already deployed" would make an org un-redeployable for a day after
            # any cleanup, so ignore anything already on its way out.
            return [o.name for o in fetch()
                    if o.name and getattr(o, "status", None) != "deletion_requested"]
        except APIClientError:
            return []

    found = {
        "workspaces": [n for n in names(client.list_workspaces) if any(m in n for m in CIB_MARKERS)],
        "queues": [n for n in names(client.list_queues) if any(m in n for m in CIB_MARKERS)],
        "hooks": [n for n in names(client.list_hooks) if any(m in n for m in CIB_MARKERS)],
    }
    total = sum(len(v) for v in found.values())
    if not total:
        print("  Target org check OK: no existing CIB objects found.")
        return

    print(f"\nERROR: the target organisation already contains {total} CIB object(s):")
    for kind, items in found.items():
        if items:
            shown = ", ".join(sorted(items)[:4])
            more = f" (+{len(items) - 4} more)" if len(items) > 4 else ""
            print(f"    {len(items)} {kind}: {shown}{more}")
    print("\nThis script only performs fresh installs — it would create a second, parallel")
    print("copy of everything rather than updating what is there. There is no in-place")
    print("upgrade path from CIB 1.x to 2.0; deploy 2.0 into a clean organisation instead.")
    sys.exit(1)


def prd_release_org(org):
    #TBD
    pass


# DANGER — use with caution !!!
# Permanently deletes ALL hooks, rules, queues, schemas, engines, workspaces,
# annotations, and inboxes in the target organisation. There is no undo.
# Uncomment the call in cib_init_script.py only in test environments before a fresh deploy.
# To call it add the following to he main script: clean_org(SyncRossumAPIClient(credentials=Token(ROSSUM["target_org_token"]), base_url=ROSSUM["api_base_url"]),
#           ROSSUM["target_org_token"], ROSSUM["api_base_url"])
def clean_org(client, token, api_base_url):
    delete_hooks(client)
    delete_rules(client)
    delete_annotations(client, token, api_base_url)
    delete_queues(client)
    delete_workspaces(client)
    delete_inboxes(token, api_base_url)
    delete_schemas(client)
    delete_engines(token, api_base_url)


def _delete_all(list_fn, delete_fn, label):
    """Delete every object from a paginated listing.

    The list endpoints return a LAZY paginated iterator. Deleting while
    iterating it shifts the remaining pages under the cursor, so a plain
    `for x in client.list_x(): client.delete_x(x.id)` silently stops after the
    first page -- on a 144-rule org exactly 100 were removed and 44 survived a
    "full" wipe. Materialise each sweep before deleting, and repeat until the
    listing is empty or a sweep makes no progress (objects the API refuses to
    delete, e.g. schemas still attached to a queue awaiting deletion).
    """
    while True:
        try:
            batch = list(list_fn())
        except APIClientError:
            return
        if not batch:
            return
        deleted = 0
        for obj in batch:
            try:
                delete_fn(obj.id)
                deleted += 1
            except APIClientError:
                continue
        if not deleted:
            logging.warning(f"clean_org: {len(batch)} {label} could not be deleted; "
                            f"they are most likely attached to a queue that is still "
                            f"scheduled for deletion (up to 24 hours).")
            return


def delete_hooks(client):
    _delete_all(client.list_hooks, client.delete_hook, "hooks")


def delete_queues(client):
    # Queues never disappear immediately -- delete only marks them
    # 'deletion_requested' -- so filter those out or the sweep never converges.
    _delete_all(lambda: [q for q in client.list_queues() if q.status != "deletion_requested"],
                client.delete_queue, "queues")


def delete_workspaces(client):
    _delete_all(client.list_workspaces, client.delete_workspace, "workspaces")


def delete_inboxes(token, base_api_url):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }

    response = requests.get(f"{base_api_url}/inboxes", headers=headers)

    inboxes = json.loads(response.text)['results']

    for inbox in inboxes:
        requests.delete(f"{base_api_url}/inboxes/{inbox['id']}", headers=headers)


def delete_engines(token, base_api_url):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }
    response = requests.get(f"{base_api_url}/engines?page_size=100", headers=headers)
    engines = json.loads(response.text).get('results', [])
    for engine in engines:
        response = requests.delete(f"{base_api_url}/engines/{engine['id']}", headers=headers)
        if not response.ok:
            # Expected on a full wipe: deleting a queue only schedules it for
            # deletion, and Rossum refuses to drop an engine still attached to
            # one ("engine_attached_to_queues_waiting_for_deletion") until the
            # queue is really gone, up to 24 hours later. The orphaned engines
            # are harmless -- a redeploy creates its own -- but they do linger.
            logging.warning(f"Could not delete engine {engine['id']} ({engine.get('name', '')}): {response.status_code} {response.text}")


def delete_rules(client):
    _delete_all(client.list_rules, client.delete_rule, "rules")


def delete_schemas(client):
    _delete_all(client.list_schemas, client.delete_schema, "schemas")


def delete_annotations(client, token, base_api_url):
    annotations = client.list_annotations()
    annotations_list = []
    for annotation in annotations:
        client.delete_annotation(annotation.id)
        annotations_list.append(annotation.url)

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }
    requests.post(f"{base_api_url}/annotations/purge_deleted",
                  headers=headers,
                  json={"annotations": annotations_list})

def get_queue_id_by_name(client, queue_name):
    queues = client.list_queues()
    for queue in queues:
        if queue.name == queue_name and queue.status == 'active':
            return queue.id


def get_user_id_by_name(client, user_name):
    users = client.list_users()
    for user in users:
        if user.username == user_name:
            return user.id


def resolve_token_owner_id(client, user_name):
    """Resolve the hook token owner, or abort with an actionable message.

    CIB requires a LOCAL admin of the target organization as hook token owner;
    an external/support account cannot own hook tokens. prd2 validates a
    configured token_owner_id with GET /users/{id} and, when it is null or not
    retrievable, falls back to an interactive questionary picker. This tool pipes
    prd2's stdout, so stdin is not a TTY: that picker dies with
    OSError [Errno 22], prd2 prints "Planning failed" -- and still exits 0. Fail
    here instead, while the message can still say what to fix.
    """
    user_id = get_user_id_by_name(client, user_name)
    if user_id is None:
        print(f"\nERROR: token owner '{user_name}' is not a user in the target "
              f"organization, so prd2 cannot set the hook token owner.")
        print("CIB needs a local admin user in the target organization. Create it in "
              "Rossum, point rossum.token_owner_username in config.json at its "
              "username, and re-run.")
        sys.exit(1)

    # prd2's own picker only offers admin / organization_group_admin users, but it
    # does not re-check an explicitly configured id -- a non-admin owner is accepted
    # at deploy time and only shows up later as hooks failing. Advisory only.
    try:
        owner = client.retrieve_user(user_id)
        admin_urls = {r.url for r in client.list_user_roles()
                      if r.name in ("admin", "organization_group_admin")}
        if not admin_urls.intersection(owner.groups):
            print(f"  WARNING: token owner '{user_name}' is not an admin in the target "
                  f"organization; CIB expects a local admin, hooks may fail at run time.")
    except Exception as e:
        print(f"  NOTE: could not verify that '{user_name}' is an admin ({e}).")

    return user_id

def handle_memorisation_datasets(token, base_api_url):
    print("\nCreating memorisation datasets...")
    create_dataset_url = base_url(base_api_url) + "/svc/data-storage/api/v1/collections/create"
    create_index_url = base_url(base_api_url) + "/svc/data-storage/api/v1/indexes/create"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    collections = [
        "_supplier_memorization_test",
        "_customer_memorization_test",
        "_tax_code_memorization",
    ]
    for name in collections:
        resp = requests.post(create_dataset_url, headers=headers, json={"collectionName": name})
        print(f"  Collection {name}: {'created' if resp.ok else f'skipped ({resp.status_code})'}")

    for name in collections:
        resp = requests.post(create_index_url, headers=headers, json={
            "collectionName": name,
            "indexName": "__dynamic_index",
            "keys": {"$**": 1},
        })
        print(f"  Index {name}/__dynamic_index: {'created' if resp.ok else f'skipped ({resp.status_code})'}")

    print("Memorisation datasets done.")


def verify_imports(rossum, client, assets, wait_s=180, poll_s=20):
    """Smoke-check that every invoked Coupa import actually wrote data.

    The scheduled-imports service returns HTTP 202 even when it cannot write
    back to the org (e.g. a target_rossum_instance that does not match the
    org's region), so a misconfigured deploy looks successful while importing
    nothing. A dataset collection only exists once rows are written, so the
    presence of each import's dataset_name in data storage is the signal.
    Warns loudly on anything missing; does not abort (imports are async).
    """
    invoked = {h["hook_name"] for h in csv_to_dict(assets.hooks_csv) if h.get("invoke") == "true"}
    datasets = {}
    for hook_rossum in client.list_hooks():
        if hook_rossum.name in invoked:
            import_config = (hook_rossum.settings or {}).get("import_config") or {}
            name = import_config.get("dataset_name")
            if name:
                datasets[name] = hook_rossum.name
    if not datasets:
        return

    list_url = base_url(rossum["api_base_url"]) + "/svc/data-storage/api/v1/collections/list"
    headers = {"Authorization": f"Bearer {rossum['target_org_token']}"}
    print(f"\nVerifying {len(datasets)} Coupa import dataset(s) landed (up to {wait_s}s)...")

    def landed(existing, name):
        # collection present, or still importing (svc writes a __tmp_<name> first)
        return name in existing or any(c.startswith(f"__tmp_{name}") for c in existing)

    pending = set(datasets)
    waited = 0
    while True:
        try:
            resp = requests.post(list_url, headers=headers, json={}, timeout=30)
            existing = set(resp.json().get("result", [])) if resp.ok else set()
        except requests.RequestException:
            existing = set()
        pending = {d for d in pending if not landed(existing, d)}
        if not pending or waited >= wait_s:
            break
        time.sleep(poll_s)
        waited += poll_s

    if pending:
        print("\n  WARNING: no data found for these Coupa import dataset(s):")
        for name in sorted(pending):
            print(f"    - {name}  (hook: {datasets[name]})")
        print("  The import returns HTTP 202 even when the scheduled-imports service")
        print("  cannot write back (commonly a target_rossum_instance that does not")
        print("  match the org's region). Check target_rossum_instance and the hook")
        print("  logs, then re-run. (Imports are async — re-check if still in progress.)")
    else:
        print("  All Coupa import datasets present.")