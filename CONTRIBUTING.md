# Development notes

## Layout

The repository is expected to live next to a checkout of Electrum:

```
<workdir>/
├── electrum/                 # git clone https://github.com/spesmilo/electrum
├── electrum-thorchain-swap/  # this repository
└── dev-data/                 # Electrum data directory used by run_dev.sh (created)
```

- `thorchain_swap/` – the plugin package (what goes into the zip)
  - `thornode.py` – THORNode REST client and response validation
  - `memo.py` – swap memo construction and parsing
  - `swap_tx.py` – building and verifying the Bitcoin transaction
  - `thorchain_swap.py` – GUI-independent core: quotes, offers, swap status, records
  - `swap_tab.py`, `qt.py` – the Qt tab, dialogs and plugin lifecycle
  - `icons/` – bundled asset icons, see below
- `tests/` – unit tests (no network, no GUI)
- `contrib/` – build and development scripts

## Running from source

`contrib/run_dev.sh` symlinks the plugin package into the Electrum checkout as
an internal plugin and starts Electrum from source with the separate
`dev-data` directory, so a code change is picked up on the next start without
re-installing a zip. `contrib/run_dev.sh --unlink` removes the symlink.

This is a different load path from the zip users install (internal plugins
are not hash-pinned and are imported from the file system), so test the zip
through Tools → Plugins → Add before a release.

## Tests

```
PYTHONPATH=.:../electrum python3 -m unittest discover -s tests -t .
```

The tests cover the memo grammar, transaction construction and verification
against an offline wallet, quote validation with a fake THORNode client, and
swap status interpretation from recorded THORNode responses. They run in well
under a second.

## Building the zip

```
./contrib/make_plugin thorchain_swap
```

prints the path and the SHA-256 of `thorchain_swap-<version>.zip`. The build is
reproducible (sorted members, fixed timestamps and attributes), so a hash
built from the same source tree matches on any machine. Publish the hash in
the release notes; Electrum shows it when the zip is installed.

## Asset icons

Icons are fetched once at development time and shipped inside the zip:

```
./contrib/fetch_icons.py --extra XMR.XMR
```

reads the current THORChain pools and downloads logos from
[trustwallet/assets](https://github.com/trustwallet/assets) and
[spothq/cryptocurrency-icons](https://github.com/spothq/cryptocurrency-icons),
recording the source of every file in `icons/SOURCES.json`. Re-run it when
THORChain lists new pools. Assets without an icon get the grey placeholder.

## Configuration keys

All keys live under `plugins.thorchain_swap.` in Electrum's config file:

| key | default | meaning |
|---|---|---|
| `thornode_url` | `https://gateway.liquify.com/chain/thorchain_api` | THORNode to query; https only, except localhost and `.onion` |
| `verify_url` | `https://public-thornode.nativeswap.io` | second THORNode that must confirm the inbound vault (it only receives the vault's public key); empty disables the check |
| `slippage_bps` | `100` | price protection, basis points |
| `streaming_interval` | `0` | blocks between sub-swaps |
| `streaming_quantity` | `0` | number of sub-swaps, 0 = automatic |
| `affiliate_thorname` | `el` | THORName that receives the plugin fee; empty disables it |
| `affiliate_bps` | `10` | plugin fee in basis points |
| `affiliate_min_usd` | `100` | swaps below this USD value pay no plugin fee |
| `last_asset` | | asset preselected in the list |

Values outside the valid ranges are clamped and logged.

## Code style

Follow Electrum's own style: four-space indents, `_()` for user-facing
strings, type hints, English everywhere in code, comments and messages.
