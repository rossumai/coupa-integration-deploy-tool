# CIB Init Script

Deploys the [Coupa Integration Baseline (CIB)](https://knowledge-base.rossum.ai/docs/coupa-integration-baseline-cib) to a target Rossum organisation.

## What it does

The script automatically:
- Downloads the configured CIB release from the [public GitHub repository](https://github.com/rossumai/rossum-coupa-integration) and caches it locally
- Notifies you if a newer CIB release is available
- Deploys two queues (line-level and header-level taxation) with all extensions, formula fields, rules, and MDH matching configuration
- Configures the connection to your Coupa environment
- Initiates data replication for the standard master data sets (suppliers, purchase orders, tax codes, payment terms, etc.)

## Prerequisites

- **Python 3.10 or later** — [python.org](https://www.python.org/downloads/)
- **pipx** — used to install the prd2 deployment tool
- **prd2 v2.18.1 or later** — the Rossum deployment CLI. This script requires the `--ld` (local deploy) flag, which first ships in **v2.18.1**. Earlier versions fail with `Error: No such option: --ld`.

### Install pipx

**Mac / Linux:**
```bash
pip install pipx
pipx ensurepath
```

**Windows** (run in PowerShell):
```powershell
pip install pipx
pipx ensurepath
```
Restart your terminal after running `ensurepath`.

### Install prd2

prd2 is not published on public PyPI — install it from the GitHub repository, pinning the minimum supported tag:

```bash
pipx install git+https://github.com/rossumai/deployment-manager.git@v2.18.1
```

Verify the installation (must report **2.18.1 or later**):
```bash
prd2 --version
```

## Setup

### 1. Clone or download this repository

```bash
git clone https://github.com/rossumai/coupa-integration-deploy-tool.git
cd coupa-integration-deploy-tool
```

### 2. Install Python dependencies

**Mac / Linux:**
```bash
pip install -r requirements.txt
```

**Windows:**
```powershell
pip install -r requirements.txt
```

Alternatively, if you use `pipenv`:
```bash
pipenv sync
```

### 3. Configure the script

Edit `config.json` with your environment details. See [Configuration](#configuration) below.

Then tell git to stop tracking your local changes to it (so credentials are never committed accidentally):

```bash
git update-index --skip-worktree config.json
```

Run this once after cloning. To re-enable tracking (e.g. to commit a structural change to the file), run `git update-index --no-skip-worktree config.json` first.

### 4. Run the script

**Mac / Linux:**
```bash
python cib_init_script.py
```

**Windows:**
```powershell
python cib_init_script.py
```

The script will download the configured CIB release on the first run and cache it locally:
- **Mac / Linux:** `~/.cib_releases/<version>/`
- **Windows:** `%LOCALAPPDATA%\cib_releases\<version>\`

Subsequent runs reuse the cached release. To force a fresh download, delete the version folder from the cache directory.

## Versioning

The script version is tracked in the `VERSION` file. When upgrading to a newer version of this script, check the release notes — some releases require updates to the internal configuration files in the `_config/` folder (`cib_target.yaml`, `cib_target_secrets.json`, `hooks.csv`). These files are managed by Rossum and should not be edited manually unless instructed.

---

## Configuration

Edit `config.json` with your environment details:

```json
{
  "rossum": {
    "org_id": 12345,
    "api_base_url": "https://elis.rossum.ai/api/v1",
    "target_rossum_instance": "prod-eu",
    "token_owner_username": "admin@yourcompany.com",
    "target_org_token": "<your-rossum-api-token>",
    "cib_version": "v1.0.0"
  },
  "coupa": {
    "coupa_base_api_url": "https://your-instance.coupacloud.com/",
    "client_id": "<coupa-oauth-client-id>",
    "client_secret": "<coupa-oauth-client-secret>"
  }
}
```

### rossum section

| Parameter | Description |
|---|---|
| `org_id` | ID of the target Rossum organisation |
| `api_base_url` | Rossum API base URL, ending with the API version — e.g. `https://elis.rossum.ai/api/v1` |
| `target_rossum_instance` | Target Rossum cluster. One of: `prod-eu`, `prod-eu2`, `prod-us2`, `prod-jp` |
| `token_owner_username` | Username of an existing admin user in the target organisation. This user will be set as the token owner on all deployed hooks. |
| `target_org_token` | Valid API token for the target Rossum organisation |
| `cib_version` | CIB release version to deploy — e.g. `v1.0.0`. Available releases: [github.com/rossumai/rossum-coupa-integration/releases](https://github.com/rossumai/rossum-coupa-integration/releases) |

### coupa section

| Parameter | Description |
|---|---|
| `coupa_base_api_url` | Base URL of the Coupa instance, ending with `/` |
| `client_id` | Coupa OAuth client ID — provided by the customer ([setup guide](https://rossum.university/docs/learn/coupa/integration-setup)) |
| `client_secret` | Coupa OAuth client secret — provided by the customer ([setup guide](https://rossum.university/docs/learn/coupa/integration-setup)) |
