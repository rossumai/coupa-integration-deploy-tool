import csv
import io
import json
import logging
import requests
import shutil
import yaml
import zipfile
from rossum_api import APIClientError
from urllib.parse import urlparse
import subprocess
import os

HOOKS = os.path.join('_config', 'hooks.csv')

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



def update_prd_mapping(client, rossum_api_url, org_id, admin_user, path, client_id, client_secret, coupa_base_url):
    deploy_file_path = os.path.join(path, "deploy_files", CIB_DEPLOY_FILE)
    with open(deploy_file_path) as f:
        data = yaml.safe_load(f)

    data["token_owner_id"] = get_user_id_by_name(client, admin_user)
    data["deployed_org_id"] = None
    data["patch_target_org"] = False
    data["target_url"] = rossum_api_url
    data["source_dir"] = CIB_SOURCE_DIR

    for queue in data["queues"]:
        queue["ignore_deploy_warnings"] = True
        queue["targets"][0]["id"] = None
        queue["inbox"]["targets"][0]["id"] = None
        queue["base_path"] = queue["base_path"].replace("cib/cib", CIB_SOURCE_DIR)
        queue["schema"]["targets"] = [{
            "id": None,
            "attribute_override": {
                "content[].children[?id=='oauth_client_id'].default_value": client_id,
                "content[].children[?id=='oauth_client_id'].formula": f"'{client_id}'",
                "content[].children[?id=='coupa_api_base_url'].default_value": coupa_base_url,
                "content[].children[?id=='coupa_api_base_url'].formula": f"'{coupa_base_url}'"
            }
        }]

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
        if latest == current:
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


def init_prd_release(client, rossum, coupa):
    version = rossum["cib_version"]

    try:
        latest = get_latest_cib_version()
        if latest != version:
            print(f"\nA newer CIB version is available: {latest} (configured: {version})")
            print(f"Download and use {latest} instead? [y/N]: ", end='', flush=True)
            answer = sys.stdin.readline().strip().lower()
            if answer == 'y':
                version = latest
                config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
                with open(config_path) as f:
                    cfg = json.load(f)
                cfg["rossum"]["cib_version"] = latest
                with open(config_path, "w") as f:
                    json.dump(cfg, f, indent=2)
                print(f"config.json updated to {latest}\n")
            else:
                print(f"Continuing with configured version {version}\n")
    except Exception as e:
        print(f"Could not check for latest CIB version: {e}")

    prd_path = download_cib_release(version)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(os.path.join(prd_path, "deploy_files"), exist_ok=True)
    os.makedirs(os.path.join(prd_path, "deploy_secrets"), exist_ok=True)
    os.makedirs(os.path.join(prd_path, "deploy_states"), exist_ok=True)
    shutil.copy(os.path.join(script_dir, "_config", CIB_DEPLOY_FILE), os.path.join(prd_path, "deploy_files", CIB_DEPLOY_FILE))
    shutil.copy(os.path.join(script_dir, "_config", CIB_SECRETS_FILE), os.path.join(prd_path, "deploy_secrets", CIB_SECRETS_FILE))

    update_prd_credentials(rossum["target_org_token"], prd_path)
    update_prd_mapping(client, rossum["api_base_url"], rossum["org_id"], rossum["token_owner_username"], prd_path, coupa["client_id"], coupa["client_secret"], coupa["coupa_base_api_url"])

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
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
    proc.wait()


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


def handle_hooks(rossum, coupa, client):
    print("\nConfiguring hooks...")
    hooks = csv_to_dict(HOOKS)
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
                    if target_url and '/svc/scheduled-imports/' in target_url:
                        target_url = base_url(rossum['api_base_url']) + urlparse(target_url).path
                    if target_url:
                        client.update_part_hook(hook_rossum.id, {"config": {"url": target_url}})
                        print(f"    -> URL: {target_url}")
                if 'credentials' in settings and 'client_id' in settings['credentials']:
                    settings['credentials']['client_id'] = coupa['client_id']
                    settings['credentials']['base_api_url'] = coupa['coupa_base_api_url']
                    client.update_part_hook(hook_rossum.id, {"settings": settings})
                    print(f"    -> Coupa credentials updated")
                if 'configurations' in settings and 'auth' in settings['configurations'][0]['source']:
                    settings['configurations'][0]['source']['auth']['url'] = f"{coupa['coupa_base_api_url']}oauth2/token"
                    settings['configurations'][0]['source']['queries'][0]['url'] = f"{coupa['coupa_base_api_url']}api/invoices/"
                    settings['configurations'][0]['source']['auth']['body']['client_id'] = coupa['client_id']
                    client.update_part_hook(hook_rossum.id, {"settings": settings})
                    print(f"    -> Import source updated")
                if hook["patch_secret"] == 'true':
                    secrets = {"secrets": {"client_secret": coupa["client_secret"]}}
                    client.update_part_hook(hook_rossum.id, secrets)
                    print(f"    -> Secret patched")
                if hook['invoke'] == 'true':
                    client.request("POST", url=f"{rossum['api_base_url']}/hooks/{hook_rossum.id}/invoke")
                    print(f"    -> Invoked")
    print(f"Hooks done ({matched} configured).")


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


def delete_hooks(client):
    hooks = client.list_hooks()
    for hook in hooks:
        client.delete_hook(hook.id)


def delete_queues(client):
    queues = client.list_queues()
    for queue in queues:
        try:
            client.delete_queue(queue.id)
        except APIClientError:
            continue


def delete_workspaces(client):
    workspaces = client.list_workspaces()
    for workspace in workspaces:
        client.delete_workspace(workspace.id)


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
            logging.warning(f"Could not delete engine {engine['id']} ({engine.get('name', '')}): {response.status_code} {response.text}")


def delete_rules(client):
    rules = client.list_rules()
    for rule in rules:
        try:
            client.delete_rule(rule.id)
        except APIClientError:
            continue


def delete_schemas(client):
    schemas = client.list_schemas()
    for schema in schemas:
        try:
            client.delete_schema(schema.id)
        except APIClientError:
            continue


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