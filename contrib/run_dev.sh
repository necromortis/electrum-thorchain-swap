#!/usr/bin/env bash
# Start Electrum from the sibling source checkout with this plugin linked in
# as an internal plugin. Uses a separate data directory so the developer's
# real wallets and config are never touched.
#
# Note: this exercises the internal-plugin load path. Users run the zip built
# by contrib/make_plugin (hash-pinned, loaded through the zip importer), so
# test the zip through Tools > Plugins > Add before a release.
#
# Layout expected:
#   <workdir>/electrum/                 upstream Electrum checkout
#   <workdir>/electrum-thorchain-swap/  this repository
#   <workdir>/dev-data/                 Electrum data dir for development (created)
#
#   run_dev.sh [electrum args...]   link the plugin and start Electrum
#   run_dev.sh --unlink             remove the symlink from the checkout again
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
workdir="$(dirname "$here")"
electrum_src="$workdir/electrum"
datadir="$workdir/dev-data"
plugin_name="thorchain_swap"
link="$electrum_src/electrum/plugins/$plugin_name"

if [ "${1:-}" = "--unlink" ]; then
    if [ -L "$link" ]; then
        rm "$link"
        echo "removed $link"
    else
        echo "no symlink at $link"
    fi
    exit 0
fi

if [ ! -f "$electrum_src/run_electrum" ]; then
    echo "Electrum checkout not found at $electrum_src" >&2
    exit 1
fi

if [ -e "$link" ] && [ ! -L "$link" ]; then
    echo "$link exists and is not a symlink; refusing to touch it" >&2
    exit 1
fi
# replace a stale or dangling symlink (e.g. after the repository was moved)
if [ -L "$link" ] && [ "$(readlink -f "$link" || true)" != "$here/$plugin_name" ]; then
    rm "$link"
    echo "replaced stale symlink $link"
fi
if [ ! -L "$link" ]; then
    ln -s "$here/$plugin_name" "$link"
    echo "linked $link (remove with: $0 --unlink)"
fi
# keep the upstream checkout's git status clean (only for a regular .git directory)
exclude="$electrum_src/.git/info/exclude"
if [ -d "$electrum_src/.git" ]; then
    grep -qx "electrum/plugins/$plugin_name" "$exclude" 2>/dev/null \
        || echo "electrum/plugins/$plugin_name" >> "$exclude"
fi

mkdir -p "$datadir"
exec python3 "$electrum_src/run_electrum" -D "$datadir" "$@"
