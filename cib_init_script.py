import log_setup  # must be first — sets up stdout/stderr tee to log file

from rossum_api import SyncRossumAPIClient
from rossum_api.dtos import Token
from helpers import check_script_version, clean_org, handle_hooks, json_to_dict, init_prd_release, handle_memorisation_datasets

_config = json_to_dict('config.json')
ROSSUM = _config["rossum"]
COUPA = _config["coupa"]

check_script_version()


def deploy_cib():
    client = SyncRossumAPIClient(credentials=Token(ROSSUM["target_org_token"]), base_url=ROSSUM["api_base_url"])
    init_prd_release(client, ROSSUM, COUPA)
    handle_hooks(ROSSUM, COUPA, client)
    handle_memorisation_datasets(token=ROSSUM["target_org_token"], base_api_url=ROSSUM["api_base_url"])

deploy_cib()