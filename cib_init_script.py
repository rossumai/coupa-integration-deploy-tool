import log_setup  # must be first — sets up stdout/stderr tee to log file

import os
import sys

from rossum_api import SyncRossumAPIClient
from rossum_api.dtos import Token

from cib_assets import AssetResolutionError, resolve_assets
from helpers import (check_org_features, check_prd2_available, check_region, check_script_version,
                     check_rossum_api_version, check_target_org_empty,
                     download_cib_release, handle_hooks, handle_memorisation_datasets,
                     init_prd_release, json_to_dict, normalize_base_url, select_cib_version, verify_credentials,
                     verify_deployment, verify_imports)
# clean_org wipes the whole target organisation and is never called automatically.
# See its docstring in helpers.py before using it, and only in a test org:
#     from helpers import clean_org

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

_config = json_to_dict('config.json')
ROSSUM = _config["rossum"]
COUPA = _config["coupa"]
COUPA["coupa_base_api_url"] = normalize_base_url(COUPA["coupa_base_api_url"])

check_script_version()
check_prd2_available()
check_rossum_api_version()


def deploy_cib():
    # The release is downloaded before anything is validated: from CIB 2.0 each
    # release carries its own deploy file, hooks.csv and Coupa scope map, and the
    # credential check needs that scope map. A wasted download on a bad
    # credential is cheap and cached; validating against the wrong version's
    # data is not.
    version = select_cib_version(ROSSUM)
    prd_path = download_cib_release(version)
    try:
        assets = resolve_assets(prd_path, version, SCRIPT_DIR)
    except AssetResolutionError as e:
        print(e.message())
        sys.exit(1)
    print(f"Deploy assets: {assets.source}")

    verify_credentials(COUPA, assets)
    check_region(ROSSUM)

    client = SyncRossumAPIClient(credentials=Token(ROSSUM["target_org_token"]), base_url=ROSSUM["api_base_url"])
    check_target_org_empty(client)
    check_org_features(client, ROSSUM, prd_path)

    init_prd_release(client, ROSSUM, COUPA, prd_path, assets)
    verify_deployment(client, prd_path)
    handle_hooks(ROSSUM, COUPA, client, assets)
    handle_memorisation_datasets(token=ROSSUM["target_org_token"], base_api_url=ROSSUM["api_base_url"])
    verify_imports(ROSSUM, client, assets)


deploy_cib()
