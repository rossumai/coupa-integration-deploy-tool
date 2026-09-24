"""Locate the four per-version deploy assets for a downloaded CIB release.

Every CIB release pins its own object IDs, hook set and Coupa scopes, so the
deploy file, secrets template, hooks.csv and required_scopes.json are version
data, not tool data. From CIB 2.0 they ship inside the release itself, in a
`deploy/` folder. Releases cut before that (v1.0.0, v1.1.0) predate the
convention, so for those we fall back to the copies kept in this repo's
`_config/` — those two releases share one object inventory, so a single
fallback set covers both.

Keeping the assets with the release is what lets one script deploy several CIB
versions: the deploy logic never branches on version, it just reads whatever
the resolved AssetSet points at.
"""

import os
from dataclasses import dataclass

# Filenames are the same in both locations; only the directory differs.
DEPLOY_FILE_NAME = "cib_target.yaml"
SECRETS_FILE_NAME = "cib_target_secrets.json"
HOOKS_CSV_NAME = "hooks.csv"
REQUIRED_SCOPES_NAME = "required_scopes.json"

RELEASE_ASSET_DIR = "deploy"
LEGACY_ASSET_DIR = "_config"


@dataclass(frozen=True)
class AssetSet:
    """The four version-specific files, resolved to absolute paths."""

    deploy_file: str
    secrets_file: str
    hooks_csv: str
    required_scopes: str
    source: str  # human-readable origin, for logging and error messages
    shipped_with_release: bool


def _asset_set(directory, source, shipped_with_release):
    return AssetSet(
        deploy_file=os.path.join(directory, DEPLOY_FILE_NAME),
        secrets_file=os.path.join(directory, SECRETS_FILE_NAME),
        hooks_csv=os.path.join(directory, HOOKS_CSV_NAME),
        required_scopes=os.path.join(directory, REQUIRED_SCOPES_NAME),
        source=source,
        shipped_with_release=shipped_with_release,
    )


def _missing(assets):
    return [
        p
        for p in (assets.deploy_file, assets.secrets_file, assets.hooks_csv, assets.required_scopes)
        if not os.path.exists(p)
    ]


def resolve_assets(release_path, version, script_dir):
    """Return the AssetSet for a downloaded release.

    A release carrying `deploy/cib_target.yaml` is self-describing and supplies
    all four files. Otherwise the release is legacy and `_config/` supplies
    them. Partial sets abort: applying one version's deploy file to another
    version's tree produces a deploy that half-works, which is worse than a
    clear failure here.
    """
    release_dir = os.path.join(release_path, RELEASE_ASSET_DIR)
    legacy_dir = os.path.join(script_dir, LEGACY_ASSET_DIR)

    if os.path.exists(os.path.join(release_dir, DEPLOY_FILE_NAME)):
        assets = _asset_set(release_dir, f"CIB {version} release (deploy/)", True)
    else:
        assets = _asset_set(legacy_dir, f"this script's {LEGACY_ASSET_DIR}/ (legacy release)", False)

    missing = _missing(assets)
    if missing:
        raise AssetResolutionError(version, assets, missing)

    return assets


class AssetResolutionError(Exception):
    """Raised when a release resolves to an incomplete set of deploy assets."""

    def __init__(self, version, assets, missing):
        self.version = version
        self.assets = assets
        self.missing = missing
        super().__init__(self.message())

    def message(self):
        lines = [
            f"\nERROR: cannot find the deploy assets for CIB {self.version}.",
            f"Resolved to {self.assets.source}, but these files are missing:",
        ]
        lines += [f"    - {p}" for p in self.missing]
        if self.assets.shipped_with_release:
            lines.append(
                "The release ships a deploy/ folder but it is incomplete. Re-cut the CIB "
                "release with `python3 tools/generate_deploy_assets.py`."
            )
        else:
            lines.append(
                f"This CIB version ships no deploy/ folder, so the script fell back to "
                f"{LEGACY_ASSET_DIR}/, which only covers v1.0.0 and v1.1.0. Set "
                f'"cib_version" in config.json to a release the script can deploy.'
            )
        return "\n".join(lines)
