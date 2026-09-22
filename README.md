# CIB Init Script

Deploys the [Coupa Integration Baseline (CIB)](https://knowledge-base.rossum.ai/docs/coupa-integration-baseline-cib) to a target Rossum organisation.

## What it does

The script automatically:
- Downloads the configured CIB release from the [public GitHub repository](https://github.com/rossumai/rossum-coupa-integration) and caches it locally
- Notifies you if a newer CIB release is available
- Checks the target organisation before touching it: Coupa credential scopes, cluster region, that no CIB objects already exist, and that the organisation group allows the hook timeouts the release needs (see [Target organization prerequisites](#target-organization-prerequisites))
- Deploys the queues, extraction engines, extensions, formula fields, rules and MDH matching configuration for that release (see [CIB versions](#cib-versions))
- Configures the connection to your Coupa environment
- Initiates data replication for the standard master data sets (suppliers, purchase orders, tax codes, payment terms, etc.)
- Verifies afterwards that every object in the deploy file really exists in the target — prd2 reports per-object failures inline but still exits 0, so a partial deploy would otherwise look like a successful one

> **Fresh installs only.** Every run creates a new set of objects. The script
> stops if the target organisation already contains CIB objects, because a
> second run would build a parallel copy rather than update the first. There is
> no in-place upgrade path from CIB 1.x to 2.0 — deploy 2.0 into a clean
> organisation.

## CIB versions

One script deploys any CIB release; `rossum.cib_version` in `config.json` picks
which. Keep an older version pinned if a newer one causes trouble.

| Version | What lands in the target org |
|---|---|
| **v2.0.0+** | Six workspaces / seven queues: the `Coupa Integration Baseline` pair (line-level and header-level taxation), an `E-invoicing Inbox`, and the BE / PL / FR / DE country queues. Two dedicated extraction engines, 33 extensions, 53 rules. |
| **v1.1.0**, **v1.0.0** | One workspace, two queues (line-level and header-level taxation). Two dedicated extraction engines, 32 extensions, 48 rules. |

**CIB 2.0 deploys everything, every time.** A customer who does not process
e-invoices, or who operates in only some of those countries, gets queues they
will not use. Deleting the unused country workspaces after the deploy is a
normal part of the handover — do it before uploading any documents. Each
country queue is self-contained; the rules that route documents into them live
on the `E-invoicing Inbox` queue and can be deleted with it.

Each release from v2.0.0 carries its own deploy configuration in a `deploy/`
folder. Releases predating that convention (v1.0.0, v1.1.0) are covered by the
copies in this repository's `_config/`. The script picks whichever applies, so
a new CIB release generally needs no new version of this script.

## Target organization prerequisites

These are **organization-group settings that only Rossum can change** — they are
not writable with an admin API token, so arrange them with Rossum support
*before* the deploy. The script checks the first one up front and refuses to
start without it; the rest surface as advisories or at run time.

| Requirement | Needed for | Symptom if missing |
|---|---|---|
| `maximum_hook_timeout` ≥ **360s** | CIB 2.0 export pipeline | Every export-pipeline hook is rejected with `400 Ensure this value is less than or equal to 60`. prd2 logs that per hook and still exits 0 — the deploy looks successful but the organisation has **no export chain at all**. |
| **External HTTPS egress** from hook functions | Any call to Coupa | Connect timeouts to the Coupa tenant. The deploy succeeds; export fails at run time with `Failed to fetch or parse auth token … timed out`. Arrange it ahead of the deploy — the change takes a few hours to propagate. |
| `einvoicing` enabled | E-invoicing inbox | The inbox cannot process e-invoice XML. The AP queues are unaffected. |
| **Store template visibility** for every template CIB uses | Creating hooks from Store extensions | prd2 cannot resolve the template and opens an interactive picker. This script pipes prd2's output, so the prompt cannot be answered: the run stalls, then dies with `OSError [Errno 22]` behind a `Planning failed` banner and exit code 0. The script works around it by dropping the reference and creating the hook directly, but the Store-backed path is the correct one. |
| A **local admin user** in the target organisation | Hook token owner | The deploy aborts up front: an external or support account cannot own hook tokens. |

All but the last are properties of the **organization group**, not the
organisation — check them with `GET /api/v1/organization_groups/<id>` and
compare against the CIB source group. `maximum_hook_timeout` is a commercial
entitlement (it governs Lambda runtime cost), which is why no customer-side
token can set it.

Which templates a release needs varies with its hooks. Find them with:

```bash
grep -ho '"hook_template": "[^"]*"' <release>/cib-org/default/hooks/*.json | sort -u
```

then check each id resolves in the target: `GET /api/v1/hook_templates/<id>`.
CIB 2.0 uses 28 (Duplicate Handling), 39 (Master Data Hub), 50 (Export Pipeline
— Request Processor) and 55 (Coupa master data import). Note the XML mime types
the e-invoicing inbox needs are **not** an organization-group setting: they
travel in the queue's own `accepted_mime_types` and deploy with it.

CIB **v1.x** needs only external egress and the local admin — its export runs on
webhook extensions rather than serverless functions, so the timeout cap does not
apply.

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

The script version is tracked in the `VERSION` file.

`_config/` holds the deploy configuration for the CIB releases that predate
per-release assets — v1.0.0 and v1.1.0, which share one set of object IDs.
These files are managed by Rossum and should not be edited manually unless
instructed. From CIB v2.0.0 onwards the equivalent files ship inside the CIB
release itself, so `_config/` is frozen and no longer changes when a new CIB is
published.

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
    "cib_version": "v2.0.0"
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
| `cib_version` | CIB release version to deploy — e.g. `v2.0.0`. See [CIB versions](#cib-versions) for what each deploys. Available releases: [github.com/rossumai/rossum-coupa-integration/releases](https://github.com/rossumai/rossum-coupa-integration/releases) |

### coupa section

| Parameter | Description |
|---|---|
| `coupa_base_api_url` | Base URL of the Coupa instance, ending with `/` |
| `client_id` | Coupa OAuth client ID — provided by the customer ([setup guide](https://rossum.university/docs/learn/coupa/integration-setup)) |
| `client_secret` | Coupa OAuth client secret — provided by the customer ([setup guide](https://rossum.university/docs/learn/coupa/integration-setup)) |
