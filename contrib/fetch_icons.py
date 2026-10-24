#!/usr/bin/env python3
"""Download icons for every THORChain pool asset into thorchain_swap/icons/.

The icons ship inside the plugin so that the wallet never contacts an icon
CDN at runtime. Re-run this script when THORChain adds pools, then release.

Sources, in order of preference:
  - trustwallet/assets on GitHub: native coins and tokens
  - spothq/cryptocurrency-icons on GitHub (CC0): native coins missing above
Assets without an icon get the grey placeholder at runtime.

Usage:
  contrib/fetch_icons.py [--extra XMR.XMR ...] [--thornode URL] [--size 64]

Requires: Pillow, pycryptodome (EIP-55 checksums for EVM token paths).
"""
import argparse
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Dict, Iterable, List, Optional, Tuple

from PIL import Image, ImageDraw, UnidentifiedImageError

THORNODE_DEFAULT = 'https://gateway.liquify.com/chain/thorchain_api'
TW_RAW = 'https://raw.githubusercontent.com/trustwallet/assets/master/blockchains'
TW_API = 'https://api.github.com/repos/trustwallet/assets'
SPOTHQ = 'https://raw.githubusercontent.com/spothq/cryptocurrency-icons/master/128/color'
MAX_DOWNLOAD_BYTES = 5_000_000

# THORChain chain id -> directory under trustwallet/assets/blockchains
TW_CHAIN_DIRS = {
    'BTC': 'bitcoin', 'ETH': 'ethereum', 'BSC': 'smartchain', 'AVAX': 'avalanchec', 'BASE': 'base',
    'GAIA': 'cosmos', 'LTC': 'litecoin', 'BCH': 'bitcoincash', 'DOGE': 'doge', 'XRP': 'ripple',
    'SOL': 'solana', 'TRON': 'tron', 'ZEC': 'zcash', 'THOR': 'thorchain',
}
EVM_CHAINS = {'ETH', 'BSC', 'AVAX', 'BASE'}
# chains whose token addresses are case-sensitive but THORNode upper-cases them;
# resolved by listing the Trust Wallet directory
CASE_LOOKUP_CHAINS = {'TRON', 'SOL'}
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'thorchain_swap', 'icons')
PLACEHOLDER = '_unknown.png'
_ASSET_RE = re.compile(r'^[A-Z0-9]+\.[A-Z0-9~_\-+]+$')

_tw_dir_cache = {}  # type: Dict[str, Dict[str, str]]


def http_get(url: str, *, headers: Optional[dict] = None) -> Optional[bytes]:
    """GET with a size cap; None on 404/403 (403 from the GitHub API means rate limiting)."""
    req = urllib.request.Request(url, headers={'User-Agent': 'electrum-thorchain-swap/fetch_icons', **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read(MAX_DOWNLOAD_BYTES + 1)
    except urllib.error.HTTPError as e:
        if e.code == 403 and 'api.github.com' in url:
            raise SystemExit('GitHub API refused the request (rate limit?); try again later')
        if e.code in (403, 404):
            return None
        raise
    if len(data) > MAX_DOWNLOAD_BYTES:
        print(f'skipping {url}: larger than {MAX_DOWNLOAD_BYTES} bytes', file=sys.stderr)
        return None
    return data


def eip55(address: str) -> str:
    from Crypto.Hash import keccak  # pycryptodome
    addr = address.lower().removeprefix('0x')
    digest = keccak.new(digest_bits=256, data=addr.encode('ascii')).hexdigest()
    return '0x' + ''.join(c.upper() if int(digest[i], 16) >= 8 else c for i, c in enumerate(addr))


def tw_dir_entries(path: str) -> Dict[str, str]:
    """lower-cased name -> actual name of the entries of a trustwallet/assets directory."""
    if path in _tw_dir_cache:
        return _tw_dir_cache[path]
    parent, _, name = path.rpartition('/')
    listing = json.loads(http_get(f'{TW_API}/contents/{parent}', headers={'Accept': 'application/vnd.github+json'}) or b'[]')
    sha = next((x['sha'] for x in listing if x['name'] == name), None)
    entries = {}
    if sha:
        tree = json.loads(http_get(f'{TW_API}/git/trees/{sha}', headers={'Accept': 'application/vnd.github+json'}) or b'{}')
        if tree.get('truncated'):
            print(f'warning: listing of {path} truncated', file=sys.stderr)
        entries = {x['path'].lower(): x['path'] for x in tree.get('tree', [])}
    _tw_dir_cache[path] = entries
    return entries


def candidate_urls(asset: str) -> Iterable[Tuple[str, str]]:
    chain, _, symbol = asset.upper().partition('.')
    ticker, _, address = symbol.partition('-')
    tw_dir = TW_CHAIN_DIRS.get(chain)
    if not address:
        if chain == 'THOR' and ticker != 'RUNE':
            # THORChain-native assets (RUJI, TCY, ...) have no icons in the public sets;
            # the THORChain logo would misrepresent them, so only spothq is tried
            yield f'{SPOTHQ}/{ticker.lower()}.png', 'spothq'
            return
        if tw_dir:
            yield f'{TW_RAW}/{tw_dir}/info/logo.png', 'trustwallet'
        yield f'{SPOTHQ}/{ticker.lower()}.png', 'spothq'
        return
    if tw_dir:
        if chain in EVM_CHAINS:
            yield f'{TW_RAW}/{tw_dir}/assets/{eip55(address)}/logo.png', 'trustwallet'
        elif chain in CASE_LOOKUP_CHAINS:
            actual = tw_dir_entries(f'blockchains/{tw_dir}/assets').get(address.lower())
            if actual:
                yield f'{TW_RAW}/{tw_dir}/assets/{actual}/logo.png', 'trustwallet'
        else:
            yield f'{TW_RAW}/{tw_dir}/assets/{address}/logo.png', 'trustwallet'


def icon_filename(asset: str) -> str:
    # must match thorchain_swap/swap_tab.py; the asset format is validated first so the
    # name can never contain path separators
    if not _ASSET_RE.match(asset.upper()):
        raise ValueError(f'unexpected asset name {asset!r}')
    return re.sub(r'[^a-z0-9.-]', '_', asset.lower()) + '.png'


def save_icon(data: bytes, path: str, size: int) -> bool:
    """Decode, resize and save; False if the data is not a usable image."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert('RGBA')
            im = im.resize((size, size), Image.LANCZOS)
            # drop embedded metadata (ICC profiles, text chunks) from the sources
            im.info.clear()
            im.save(path, format='PNG', optimize=True)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as e:
        print(f'  not a usable image ({e})', file=sys.stderr)
        return False
    return True


def write_placeholder(path: str, size: int) -> None:
    im = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(im)
    draw.ellipse((1, 1, size - 2, size - 2), fill=(128, 128, 128, 255))
    draw.ellipse((size * 0.35, size * 0.35, size * 0.65, size * 0.65), fill=(230, 230, 230, 255))
    im.save(path, optimize=True)


def fetch_pool_assets(thornode: str) -> List[str]:
    try:
        pools = json.loads(http_get(f'{thornode}/thorchain/pools') or b'[]')
    except ValueError:
        raise SystemExit(f'{thornode} did not return JSON for /thorchain/pools; try again later')
    assets = sorted(p['asset'] for p in pools if p.get('status') in ('Available', 'Staged'))
    return [a for a in assets if _ASSET_RE.match(a.upper())]


def write_sources(path: str, sources: dict) -> None:
    with open(path, 'w') as f:
        json.dump(dict(sorted(sources.items())), f, indent=2)
        f.write('\n')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--thornode', default=THORNODE_DEFAULT)
    parser.add_argument('--extra', nargs='*', default=[], help='additional assets, e.g. XMR.XMR for an upcoming pool')
    parser.add_argument('--size', type=int, default=64)
    parser.add_argument('--out', default=OUT_DIR)
    parser.add_argument('--force', action='store_true', help='re-download icons that already exist')
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    sources_path = os.path.join(args.out, 'SOURCES.json')
    sources = {}
    if os.path.exists(sources_path):
        with open(sources_path) as f:
            sources = json.load(f)

    assets = fetch_pool_assets(args.thornode)
    assets += [a.upper() for a in args.extra if a.upper() not in assets]
    missing = []
    for asset in assets:
        filename = icon_filename(asset)
        path = os.path.join(args.out, filename)
        if os.path.exists(path) and not args.force:
            print(f'{asset}: kept')
            continue
        for url, source in candidate_urls(asset):
            data = http_get(url)
            if data and save_icon(data, path, args.size):
                sources[filename] = {'asset': asset, 'url': url, 'source': source}
                write_sources(sources_path, sources)  # provenance survives a later crash
                print(f'{asset}: {source}')
                break
        else:
            missing.append(asset)
            print(f'{asset}: NOT FOUND (placeholder will be shown)', file=sys.stderr)

    placeholder = os.path.join(args.out, PLACEHOLDER)
    if not os.path.exists(placeholder):
        write_placeholder(placeholder, args.size)
    write_sources(sources_path, sources)
    if missing:
        print(f'\n{len(missing)} asset(s) without icon: {", ".join(missing)}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
