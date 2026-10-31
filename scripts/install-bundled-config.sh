#!/usr/bin/env bash
# Point nightwright's XDG config at this release tree's bundled ideas/ slice (SPEC-0136).
# Optional — run deliberately after extracting a nightwright release-archive tarball.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IDEAS_DIR="${ROOT}/ideas"
IDEAS_ORG="${IDEAS_DIR}/ideas.org"
SPECS_DIR="${ROOT}/docs/specs"

if [[ ! -f "${IDEAS_ORG}" ]]; then
  echo "error: missing ${IDEAS_ORG}" >&2
  echo "  extract a release-archive (nw release-archive) first, then run this from that tree." >&2
  exit 1
fi

CONFIG_HOME="${XDG_CONFIG_HOME:-${HOME}/.config}"
CONFIG_DIR="${CONFIG_HOME}/nightwright"
CONFIG="${CONFIG_DIR}/config.yaml"
mkdir -p "${CONFIG_DIR}"

if [[ -f "${CONFIG}" ]]; then
  BAK="${CONFIG}.bak.$(date +%Y%m%d%H%M%S)"
  cp -a "${CONFIG}" "${BAK}"
  echo "backed up existing config → ${BAK}"
fi

# Merge org_* / specs_dir when PyYAML is available; otherwise write a minimal stub
# (prior config already backed up).
export NW_INSTALL_CONFIG="${CONFIG}"
export NW_INSTALL_IDEAS_DIR="${IDEAS_DIR}"
export NW_INSTALL_IDEAS_ORG="${IDEAS_ORG}"
export NW_INSTALL_SPECS_DIR="${SPECS_DIR}"

if python3 - <<'PY'
import os, sys
from pathlib import Path

cfg_path = Path(os.environ["NW_INSTALL_CONFIG"])
ideas_dir = os.environ["NW_INSTALL_IDEAS_DIR"]
ideas_org = os.environ["NW_INSTALL_IDEAS_ORG"]
specs_dir = os.environ["NW_INSTALL_SPECS_DIR"]

try:
    import yaml
except ImportError:
    sys.exit(2)

data = {}
if cfg_path.is_file():
    loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    if isinstance(loaded, dict):
        data = loaded

data["org_files"] = [ideas_dir]
data["org_ideas_file"] = ideas_org
data["specs_dir"] = specs_dir
cfg_path.write_text(
    yaml.safe_dump(data, sort_keys=False, default_flow_style=False),
    encoding="utf-8",
)
print(f"updated {cfg_path} (merged org_files / org_ideas_file / specs_dir)")
PY
then
  :
else
  cat > "${CONFIG}" <<EOF
# Written by scripts/install-bundled-config.sh (SPEC-0136).
# Points nightwright at this release tree's bundled Meta-Tools idea slice.
org_files:
  - ${IDEAS_DIR}
org_ideas_file: ${IDEAS_ORG}
specs_dir: ${SPECS_DIR}
EOF
  echo "wrote minimal ${CONFIG} (PyYAML not available for merge)"
fi

echo
echo "next:"
echo "  cd ${ROOT}"
echo "  uv sync"
echo "  uv run nw ideas --all"
echo
echo "config: ${CONFIG}"
