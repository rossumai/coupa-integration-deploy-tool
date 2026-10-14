"""Full first Coupa import for organisations that had CIB before.

Coupa import hooks ask only for records changed since `${last_modified_date}`.
The import service resolves that to the start of the newest *finished*
operation for (organisation, dataset_name) in the MDH operation log, and falls
back to the epoch only when there is none. Deleting the hooks and dropping the
collections cannot clear that log, and every CIB deploy reuses the same dataset
names, so a redeploy into a cleaned organisation resumes from the earlier
deploy's watermark: each import finishes cleanly, into an empty dataset, with
only the handful of records changed since. Seen on two orgs on 13 Oct 2026
(CIB v1.1.0 and v2.0.1); all 12 datasets reached their reference counts after
one import from a literal epoch anchor.

seed_imports() does exactly that for each dataset whose log has a finished
operation from before this deploy, then puts `${last_modified_date}` back.
"""
import copy
import re
import time
from datetime import datetime

import requests
from rossum_api import APIClientError

ANCHOR = "${last_modified_date}"
EPOCH = "1970-01-01T00:00:00Z"


def _ts(value):
    """MDH timestamps ('2026-10-12T15:29:12.808000Z'); fractions dropped so Python 3.10 parses them."""
    return datetime.fromisoformat(re.sub(r"\.\d+", "", value).replace("Z", "+00:00"))


def anchor_keys(settings):
    """Query keys that carry ${last_modified_date}: 'updated-at[gt_or_eq]' for most Coupa imports,
    'remit-to-addresses[updated-at][gt_or_eq]' for Remit To Addresses. Matched by value, not by key."""
    query = (((settings or {}).get("import_config") or {}).get("query")) or {}
    return [k for k, v in query.items() if v == ANCHOR]


def had_history(ops, started):
    """True when an operation finished before this deploy: ${last_modified_date} would resume from it.
    A failed operation sets no watermark."""
    return any(o.get("status") == "finished" and _ts(o["create_ts"]) < started for o in ops)


def operations(rossum, dataset):
    """MDH operation log of one dataset, newest first ([] for a name that never ran)."""
    host = re.sub(r"/api/v1/?$", "", rossum["api_base_url"].rstrip("/"))
    resp = requests.get(f"{host}/svc/master-data-hub/api/v1/operation/",
                        headers={"Authorization": f"Bearer {rossum['target_org_token']}"},
                        params={"dataset_name": dataset, "limit": 100}, timeout=30)
    resp.raise_for_status()
    return resp.json()["operations"]


def _hooks(client):
    url, out = "hooks?page_size=100", []
    while url:
        page = client.request_json("GET", url)
        out += page["results"]
        url = (page.get("pagination") or {}).get("next")
    return out


def _restore(client, hook, dataset):
    try:
        client.request_json("PATCH", f"hooks/{hook['id']}", json={"settings": hook["settings"]})
        return True
    except APIClientError as e:
        print(f"  WARNING: {dataset}: could not put {ANCHOR} back on hook '{hook['name']}' ({e}). Set "
              f"settings.import_config.query back to {ANCHOR} by hand, or every scheduled run stays a full load.")
        return False


def seed_imports(client, rossum, started, wait_s=900, poll_s=15):
    """Run one import from the epoch for every dataset an earlier deploy left a watermark for.

    `started` is when this deploy began (timezone-aware UTC). The original
    settings are restored as soon as the seeded job shows up in the operation
    log -- a dispatched job carries its own copy of the settings, and a large
    full load (purchase order lines: 25-90 min) would otherwise hold the deploy
    -- and always in a finally block, so an error or timeout never leaves a hook
    on the epoch anchor.
    """
    print("\nCoupa imports: checking for an earlier deploy's import watermark...")
    todo = []
    for hook in _hooks(client):
        keys = anchor_keys(hook.get("settings"))
        dataset = ((hook.get("settings") or {}).get("import_config") or {}).get("dataset_name")
        if not (keys and dataset):
            continue
        try:
            ops = operations(rossum, dataset)
        except (requests.RequestException, ValueError, KeyError) as e:
            print(f"  WARNING: {dataset}: could not read the MDH operation log ({e}); not checked.")
            continue
        if had_history(ops, started):
            todo.append((hook, dataset, keys))
    if not todo:
        print("  No earlier import history: the first imports are full loads.")
        return

    print(f"  {len(todo)} dataset(s) have import history from before this deploy; their first import "
          f"would skip every record older than it. Running one full import each.")
    pending = {}
    try:
        deadline = time.time() + wait_s
        # handle_hooks() already invoked each import, from the old watermark. A
        # second job for a dataset whose job is still running is dropped as a
        # duplicate, so let those end first.
        while time.time() < deadline and any(o.get("status") == "processing" and _ts(o["create_ts"]) >= started
                                             for _, d, _ in todo for o in operations(rossum, d)):
            time.sleep(poll_s)
        for hook, dataset, keys in todo:
            seeded = copy.deepcopy(hook["settings"])
            for k in keys:
                seeded["import_config"]["query"][k] = EPOCH
            before = {o["operation_id"] for o in operations(rossum, dataset)}
            pending[hook["id"]] = (hook, dataset, before)  # before the PATCH, so finally restores it
            client.request_json("PATCH", f"hooks/{hook['id']}", json={"settings": seeded})
            client.request("POST", f"hooks/{hook['id']}/invoke")
            print(f"  {dataset}: full import from {EPOCH} invoked")
        deadline = time.time() + wait_s
        while pending and time.time() < deadline:
            for hook_id, (hook, dataset, before) in list(pending.items()):
                new = [o for o in operations(rossum, dataset) if o["operation_id"] not in before]
                if not new:
                    continue
                op = new[-1]  # the oldest new one: the job invoked above
                del pending[hook_id]
                if _restore(client, hook, dataset):
                    print(f"  {dataset}: full import {op['status']}, {ANCHOR} restored")
                if op["status"] == "failed":
                    print(f"  WARNING: {dataset}: the full import failed: {op.get('error')}")
            if pending:
                time.sleep(poll_s)
    except (APIClientError, requests.RequestException, ValueError, KeyError) as e:
        # Never abort the deploy over this: what is left is restored below.
        print(f"  WARNING: seeding the full imports stopped ({e}).")
    finally:
        for hook, dataset, _ in pending.values():
            _restore(client, hook, dataset)
            print(f"  WARNING: {dataset}: no full import seen; {ANCHOR} restored, so the dataset may hold only "
                  f"records changed since the earlier deploy. Run one import from a literal {EPOCH} anchor "
                  f"by hand.")
