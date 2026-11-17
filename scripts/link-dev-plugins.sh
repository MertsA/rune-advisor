#!/usr/bin/env bash
# Symlink plugins/* into the plugin-beta client's dev_plugins folder.
#
# Unmoderated (dev) plugins only load on the plugin beta world, whose client keeps its data
# in a separate "RuneScape-BETA" folder. For the Steam install it sits next to the live one:
# <steam library>/steamapps/common/RuneScape/RuneScape-BETA
# Pass it explicitly or set RS_BETA_DIR; otherwise we search for it.
# Steam launch option for the beta: %command% --configURI rs-launch://www.runescape.com/jav_config_beta.ws
#
# Then in the game console:  plugins_refresh ; plugins_list ; pon rune_advisor_export ; pon rune_advisor_dev
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
beta="${1:-${RS_BETA_DIR:-}}"

if [[ -z "$beta" ]]; then
    echo "searching for a RuneScape-BETA folder..." >&2
    beta="$(find /srv/games/steam ~/.local/share/Steam -maxdepth 9 -type d -name 'RuneScape-BETA' 2>/dev/null | head -n1 || true)"
fi
if [[ -z "$beta" || ! -d "$beta" ]]; then
    echo "usage: $0 /path/to/RuneScape-BETA   (or set RS_BETA_DIR)" >&2
    echo "Log into the plugin beta once so the client creates it." >&2
    exit 1
fi
if ! compgen -G "$beta/*.jcache" >/dev/null; then
    echo "warning: no *.jcache files in $beta - is this the right folder?" >&2
fi

mkdir -p "$beta/dev_plugins"
for dir in "$repo"/plugins/*/; do
    name="$(basename "$dir")"
    ln -sfn "${dir%/}" "$beta/dev_plugins/$name"
    echo "linked $name -> $beta/dev_plugins/$name"
done
