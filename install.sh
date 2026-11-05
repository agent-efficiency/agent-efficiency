#!/usr/bin/env bash
# Install Agent Efficiency for Claude Code, and the agent-efficiency command.
#
# Usage: bash install.sh [--data-dir DIR] [--vault PATH]... [--ref TAG] [--no-command]
#
# Safe to run again: each run brings the plugin, the command, and the vault
# hooks up to date. Run it with --help for details.
set -euo pipefail

DEFAULT_REF="v0.2.2"
REPOSITORY="agent-efficiency/agent-efficiency"
PLUGIN="agent-efficiency@agent-efficiency"
MARKETPLACE="agent-efficiency"

ref="$DEFAULT_REF"
data_dir=""
source_dir=""
install_command=1
vaults=()

usage() {
  cat <<EOF
Usage: bash install.sh [--data-dir DIR] [--vault PATH]... [--ref TAG] [--no-command]

Installs the Agent Efficiency plugin for Claude Code and the agent-efficiency
command, then runs agent-efficiency doctor.

  --data-dir DIR   Keep Agent Efficiency data in DIR instead of the default
                   folder. Sets AGENT_EFFICIENCY_DATA in Claude Code settings.
  --vault PATH     Register a vault tree and update its git hooks. Repeat for
                   each tree. Create trees first with agent-efficiency vault init.
  --ref TAG        Release tag for both the plugin and command (default $DEFAULT_REF).
  --no-command     Do not install the command. One must already be on PATH.
  --source PATH    For development only: install the plugin and the command
                   from a local checkout instead of GitHub.
  -h, --help       Show this help.
EOF
}

step() { printf '\n==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die() {
  printf '\ninstall.sh: %s\n' "$*" >&2
  exit 1
}

absolute() {
  python3 -c 'import os, sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$1"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --data-dir)
      [ $# -ge 2 ] || die "--data-dir needs a folder."
      data_dir="$2"
      shift 2
      ;;
    --vault)
      [ $# -ge 2 ] || die "--vault needs a path."
      vaults+=("$2")
      shift 2
      ;;
    --ref)
      [ $# -ge 2 ] || die "--ref needs a tag, such as $DEFAULT_REF."
      ref="$2"
      shift 2
      ;;
    --source)
      [ $# -ge 2 ] || die "--source needs a folder."
      source_dir="$2"
      shift 2
      ;;
    --no-command)
      install_command=0
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      die "unknown option: $1"
      ;;
  esac
done

step "Checking what this machine has"

command -v python3 >/dev/null 2>&1 ||
  die "python3 is not on PATH. Install Python 3.11 or newer, then run this again."
python_version=$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' ||
  die "python3 is Python $python_version. Agent Efficiency needs Python 3.11 or newer as python3 on PATH."
note "python3 is Python $python_version"

command -v claude >/dev/null 2>&1 ||
  die "the claude command is not on PATH. Install Claude Code first, then run this again. For Codex or Cursor, follow docs/install.md."
note "claude is $(command -v claude)"

if [ -n "$data_dir" ]; then
  data_dir=$(absolute "$data_dir")
fi
if [ -n "$source_dir" ]; then
  source_dir=$(absolute "$source_dir")
  [ -f "$source_dir/.claude-plugin/marketplace.json" ] ||
    die "$source_dir is not an Agent Efficiency checkout."
  note "development source: $source_dir"
fi

vault_paths=()
for vault in ${vaults[@]+"${vaults[@]}"}; do
  path=$(absolute "$vault")
  [ -d "$path" ] || die "the vault folder $path does not exist."
  [ -f "$path/.vault.json" ] ||
    die "$path is not a vault tree. Create it with: agent-efficiency vault init $path --classification CLASS"
  vault_paths+=("$path")
done

if [ "$install_command" -eq 1 ]; then
  if command -v pipx >/dev/null 2>&1; then
    pipx_command=(pipx)
  elif command -v uvx >/dev/null 2>&1; then
    pipx_command=(uvx --from pipx pipx)
  else
    die "neither pipx nor uvx is on PATH, so the command cannot be installed. Install pipx (https://pipx.pypa.io/), then run this again. Or pass --no-command if agent-efficiency is already installed."
  fi
  note "the command will be installed with: ${pipx_command[*]}"
elif ! command -v agent-efficiency >/dev/null 2>&1; then
  die "--no-command needs agent-efficiency on PATH, and it is not there."
fi

step "Updating Claude Code settings"

config_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
settings="$config_dir/settings.json"
mkdir -p "$config_dir"
if [ ! -f "$settings" ]; then
  printf '{}\n' >"$settings"
  note "created $settings"
fi
backup="$settings.agent-efficiency-backup-$(date -u +%Y%m%dT%H%M%SZ)"
cp -p "$settings" "$backup"
note "backed up the settings to $backup"

if [ -n "$data_dir" ]; then
  if [ ! -d "$data_dir" ]; then
    mkdir -p "$data_dir"
    chmod 700 "$data_dir"
    note "created the data folder $data_dir"
  fi
fi

python3 - "$settings" "$data_dir" "$MARKETPLACE" <<'PY'
import json
import os
import stat
import sys
import tempfile

path, data_dir, marketplace = sys.argv[1:4]
try:
    with open(path, encoding="utf-8") as handle:
        settings = json.load(handle)
except ValueError as error:
    sys.exit(f"install.sh: {path} is not valid JSON ({error}). Fix it, then run this again.")
if not isinstance(settings, dict):
    sys.exit(f"install.sh: {path} must hold a JSON object. Fix it, then run this again.")
changed = []
if data_dir:
    env = settings.setdefault("env", {})
    if env.get("AGENT_EFFICIENCY_DATA") != data_dir:
        env["AGENT_EFFICIENCY_DATA"] = data_dir
        changed.append(f"set env.AGENT_EFFICIENCY_DATA to {data_dir}")
known = settings.get("extraKnownMarketplaces")
entry = known.get(marketplace) if isinstance(known, dict) else None
source = entry.get("source") if isinstance(entry, dict) else None
if (
    isinstance(source, dict)
    and source.get("source") == "directory"
    and not os.path.isdir(str(source.get("path") or ""))
):
    del known[marketplace]
    changed.append(f"removed the {marketplace} marketplace entry for the missing folder {source.get('path')}")
if changed:
    mode = stat.S_IMODE(os.stat(path).st_mode)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".agent-efficiency-settings-", dir=os.path.dirname(path)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(settings, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fchmod(handle.fileno(), mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
for line in changed or ["no settings needed changing"]:
    print(f"    {line}")
PY

step "Installing the Claude Code plugin"

if claude plugin marketplace list --json 2>/dev/null | grep -q "\"name\": \"$MARKETPLACE\""; then
  if claude plugin list --json 2>/dev/null | grep -q "\"id\": \"$PLUGIN\""; then
    # --keep-data keeps any history an older release stored in Claude Code's
    # plugin data folder, which removing the marketplace would delete.
    claude plugin uninstall "$PLUGIN" --scope user --keep-data >/dev/null 2>&1 || true
  fi
  claude plugin marketplace remove "$MARKETPLACE"
fi
if [ -n "$source_dir" ]; then
  claude plugin marketplace add "$source_dir"
else
  claude plugin marketplace add "$REPOSITORY#$ref"
fi
claude plugin install "$PLUGIN" --scope user

if [ "$install_command" -eq 1 ]; then
  step "Installing the agent-efficiency command"
  if [ -n "$source_dir" ]; then
    spec="$source_dir"
  else
    spec="git+https://github.com/$REPOSITORY@$ref"
  fi
  "${pipx_command[@]}" install --force "$spec"
fi

if command -v agent-efficiency >/dev/null 2>&1; then
  ae=$(command -v agent-efficiency)
elif [ -x "${PIPX_BIN_DIR:-$HOME/.local/bin}/agent-efficiency" ]; then
  ae="${PIPX_BIN_DIR:-$HOME/.local/bin}/agent-efficiency"
  note "$ae is not on your PATH yet. Run: pipx ensurepath, then open a new terminal."
else
  die "the agent-efficiency command was not found after the install."
fi

data_flag=()
if [ -n "$data_dir" ]; then
  data_flag=(--data-dir "$data_dir")
fi

if [ ${#vault_paths[@]} -gt 0 ]; then
  step "Registering vault trees"
  for path in "${vault_paths[@]}"; do
    classification=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["classification"])' "$path/.vault.json")
    # vault init on an existing tree keeps its classification and remote, and
    # rewrites its git hooks and index.
    "$ae" vault init "$path" --classification "$classification" >/dev/null
    note "updated the git hooks of the $classification tree at $path"
    "$ae" ${data_flag[@]+"${data_flag[@]}"} vault register "$path"
    if git -C "$path" rev-parse --git-dir >/dev/null 2>&1 &&
      [ "$(git -C "$path" config --local --get core.hooksPath || true)" != ".githooks" ]; then
      note "the vault hooks are off in $path. Turn them on with: git -C $path config --local core.hooksPath .githooks"
    fi
  done
fi

step "Checking the installation"

status=0
if [ -n "$data_dir" ]; then
  AGENT_EFFICIENCY_DATA="$data_dir" "$ae" doctor --cwd "$HOME" || status=$?
else
  "$ae" doctor --cwd "$HOME" || status=$?
fi

printf '\n'
if [ "$status" -ne 0 ]; then
  printf 'doctor found a problem. Read the lines above, or see docs/troubleshooting.md.\n'
fi
printf 'Run /reload-plugins in any open Claude Code session, or start a new session.\n'
exit "$status"
