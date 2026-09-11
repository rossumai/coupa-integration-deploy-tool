#!/usr/bin/env python3
"""Stage a CIB release into the local cache from a working copy, without publishing.

`download_cib_release()` returns the cached directory untouched when
~/.cib_releases/<version>/ already exists. Pre-populating it from a local prd2
project therefore lets the whole deploy run against an unreleased CIB — which is
how CIB 2.0 gets tested before it is tagged on GitHub.

    python3 tools/stage_local_release.py ~/Documents/Rossum/cib20 v2.0.0-rc1

Copies exactly what the published zipball would contain (see the v1.1.0 release:
.gitignore, CHANGELOG.md, prd_config.yaml, cib-org/**, plus deploy/ from 2.0),
and nothing else — no credentials, no sample documents, no caches. Then inits a
git repo the same way download_cib_release() does, so prd2 can commit deploy
state.

Set rossum.cib_version in config.json to the staged version and run the script
as normal. Delete the cache directory to discard it.
"""

import argparse
import os
import shutil
import subprocess
import sys

# Mirrors what the published GitHub zipball contains. Anything not listed is
# deliberately excluded: working files must not reach a release-shaped tree,
# because a passing test against extra files proves nothing about the release.
RELEASE_CONTENTS = ["cib-org", "deploy", "prd_config.yaml", "CHANGELOG.md", ".gitignore"]

# Never copy, even from inside an included directory.
EXCLUDE = shutil.ignore_patterns(
    "credentials.yaml", ".rossum-cache", "__pycache__", "*.pyc", ".DS_Store",
)

if sys.platform == "win32":
    CACHE = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "cib_releases")
else:
    CACHE = os.path.join(os.path.expanduser("~"), ".cib_releases")


def stage(source, version, force=False):
    source = os.path.abspath(os.path.expanduser(source))
    target = os.path.join(CACHE, version)

    if not os.path.isdir(os.path.join(source, "cib-org")):
        sys.exit(f"ERROR: {source} does not look like a CIB prd2 project (no cib-org/).")

    if os.path.exists(target):
        if not force:
            sys.exit(f"ERROR: {target} already exists. Re-run with --force to replace it, "
                     f"or delete it to fall back to downloading the published release.")
        shutil.rmtree(target)

    os.makedirs(target)
    copied, skipped = [], []
    for name in RELEASE_CONTENTS:
        src = os.path.join(source, name)
        if not os.path.exists(src):
            skipped.append(name)
            continue
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(target, name), ignore=EXCLUDE)
        else:
            shutil.copy(src, os.path.join(target, name))
        copied.append(name)

    # download_cib_release() writes this .gitignore before `git init` so local
    # credentials can never be committed; do the same here.
    with open(os.path.join(target, ".gitignore"), "w") as f:
        f.write("cib-org/credentials.yaml\ntarget/credentials.yaml\ndeploy_secrets/\n")
    quiet = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    git = ["git", "-c", "user.email=noreply@rossum.ai", "-c", "user.name=CIB"]
    subprocess.call(["git", "init", "-q"], cwd=target, **quiet)
    subprocess.call(git + ["add", "."], cwd=target, **quiet)
    subprocess.call(git + ["commit", "-m", f"CIB {version} (staged locally)", "-q", "--no-gpg-sign"],
                    cwd=target, **quiet)

    print(f"Staged CIB {version} at {target}")
    print(f"  copied : {', '.join(copied)}")
    if skipped:
        print(f"  absent : {', '.join(skipped)}")
    if "deploy" not in copied:
        print("  NOTE: no deploy/ folder — the script will fall back to _config/, which only "
              "describes CIB v1.0.0/v1.1.0. Run tools/generate_deploy_assets.py in the source "
              "project first.")
    print(f'\nSet "cib_version": "{version}" in config.json, then run: python cib_init_script.py')


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", help="path to the CIB prd2 project (e.g. ~/Documents/Rossum/cib20)")
    parser.add_argument("version", help="version tag to stage it as (e.g. v2.0.0-rc1)")
    parser.add_argument("--force", action="store_true", help="replace an existing cached version")
    args = parser.parse_args()
    stage(args.source, args.version, args.force)


if __name__ == "__main__":
    main()
