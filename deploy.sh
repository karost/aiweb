#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"

PLUGIN_SRC="$ROOT/plugins"
SKILL_SRC="$ROOT/skills/aiweb-memory"
PLUGIN_DST="$HERMES_HOME/plugins/aiweb"
SKILL_DST="$HERMES_HOME/skills/aiweb-memory"

# Fix nested plugins/plugins → plugins/
if [[ -f "$PLUGIN_SRC/plugins/plugin.yaml" ]]; then
  shopt -s dotglob nullglob
  mv "$PLUGIN_SRC/plugins"/* "$PLUGIN_SRC"/
  rm -rf "$PLUGIN_SRC/plugins"
  shopt -u dotglob nullglob
fi

if [[ ! -f "$PLUGIN_SRC/plugin.yaml" ]]; then
  echo "error: missing $PLUGIN_SRC/plugin.yaml" >&2
  exit 1
fi
if [[ ! -f "$PLUGIN_SRC/__init__.py" ]]; then
  echo "error: missing $PLUGIN_SRC/__init__.py" >&2
  exit 1
fi
if [[ ! -f "$SKILL_SRC/SKILL.md" ]]; then
  echo "error: missing $SKILL_SRC/SKILL.md" >&2
  exit 1
fi

mkdir -p "$HERMES_HOME/plugins" "$HERMES_HOME/skills"

ln -sfn "$PLUGIN_SRC" "$PLUGIN_DST"
ln -sfn "$SKILL_SRC"  "$SKILL_DST"

echo "OK  plugin: $PLUGIN_DST -> $PLUGIN_SRC"
echo "OK  skill:  $SKILL_DST -> $SKILL_SRC"
echo
echo "Next:"
echo "  hermes plugins enable aiweb"
echo "  hermes plugins list | grep -i aiweb"
echo "  # then in chat: /aiweb-status"