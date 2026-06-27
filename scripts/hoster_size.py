#!/usr/bin/env python3
"""
Sondage de la taille d'un fichier chez les hébergeurs (sizeBytes exact).
=======================================================================
Beaucoup de pages dlpsgame/superpsx n'annoncent pas la taille en texte. On la
récupère alors directement auprès de l'hébergeur, en octets exacts, SANS
télécharger le fichier.

Hébergeurs implémentés (recettes vérifiées, sans clé / friction minimale) :
  - vikingfile.com : POST /api/check-files (hash) -> size   [CANDIDAT #1, batch]
  - mega.nz        : POST g.api.mega.co.nz/cs cmd 'g' -> 's' (octets)
  - gofile.io      : token invité + wt -> GET /contents -> size (ou somme)

Les autres hôtes (akirabox, mediafire, datanodes, buzzheavier, datavaults,
filekeeper, rootz, 1cloudfile, 1fichier) nécessitent soit une clé API, soit du
parsing HTML derrière Cloudflare (JA3) — non implémentés ici pour ne pas écrire
de tailles non fiables. Le point d'extension `RESOLVERS` permet de les ajouter.

API : probe_size(url) -> int | None  (octets).
Le transport HTTP est injectable (`fetcher=`) pour tester sans réseau et pour
router via curl/FlareSolverr en CI si besoin.

CLI :
  python hoster_size.py "https://vikingfile.com/f/HASH"
  python hoster_size.py --catalog dlpsgame-ps5.json --max 300   # remplit sizeBytes manquants
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

USER_AGENT = "dlpsgame-pegasus-size/1.0"
HTTP_TIMEOUT = 25
CACHE_DIR = Path(".scrape_cache_sizes")
CACHE_ENABLED = True
MAX_SANE_BYTES = 900 * 1024 ** 3  # garde-fou : > 900 Go = aberrant (cf. pegasus_finalize)


# ---------------------------------------------------------------------------
# Transport HTTP (injectable)
# ---------------------------------------------------------------------------
def _default_fetch(url: str, *, method: str = "GET", data: bytes | None = None,
                   headers: dict | None = None, timeout: int = HTTP_TIMEOUT) -> tuple[int, bytes]:
    req_headers = {"User-Agent": USER_AGENT}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
        try:
            return exc.code, exc.read()
        except Exception:
            return exc.code, b""


# Le fetcher courant (remplaçable pour tests / curl / FlareSolverr).
_FETCH = _default_fetch


def set_fetcher(fn) -> None:
    global _FETCH
    _FETCH = fn


# ---------------------------------------------------------------------------
# Cache disque
# ---------------------------------------------------------------------------
def _cache_get(url: str) -> int | None | bool:
    """Renvoie la taille cachée, None (sondé sans succès) ou False (absent du cache)."""
    if not CACHE_ENABLED:
        return False
    f = CACHE_DIR / (hashlib.sha256(url.encode()).hexdigest()[:20] + ".json")
    if f.exists():
        try:
            return json.loads(f.read_text()).get("size")
        except Exception:
            return False
    return False


def _cache_set(url: str, size: int | None) -> None:
    if not CACHE_ENABLED:
        return
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        f = CACHE_DIR / (hashlib.sha256(url.encode()).hexdigest()[:20] + ".json")
        f.write_text(json.dumps({"url": url, "size": size}))
    except Exception:
        pass


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _sane(size) -> int | None:
    if isinstance(size, bool) or not isinstance(size, (int, float)):
        return None
    size = int(size)
    return size if 0 < size <= MAX_SANE_BYTES else None


# ---------------------------------------------------------------------------
# vikingfile.com — POST /api/check-files (hash) -> size  (sans clé)
# ---------------------------------------------------------------------------
def _size_vikingfile(url: str) -> int | None:
    m = re.search(r"vikingfile\.com/(?:f|d)/([A-Za-z0-9]+)", url)
    if not m:
        return None
    h = m.group(1)
    body = urllib.parse.urlencode({"hash": h}).encode()
    status, raw = _FETCH("https://vikingfile.com/api/check-files", method="POST",
                         data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    if status != 200:
        return None
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None
    rows = data if isinstance(data, list) else data.get("files") or data.get("data") or []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and row.get("size") is not None:
            return _sane(row.get("size"))
    return None


# ---------------------------------------------------------------------------
# mega.nz — POST g.api.mega.co.nz/cs cmd 'g' -> 's' (octets)
# ---------------------------------------------------------------------------
def _size_mega(url: str) -> int | None:
    # Formats : https://mega.nz/file/<ID>#<KEY>  ou ancien  /#!<ID>!<KEY>
    m = re.search(r"mega\.(?:nz|co\.nz)/file/([A-Za-z0-9_-]+)", url) \
        or re.search(r"mega\.(?:nz|co\.nz)/#!([A-Za-z0-9_-]+)", url)
    if not m:
        return None
    file_id = m.group(1)
    body = json.dumps([{"a": "g", "p": file_id}]).encode()
    status, raw = _FETCH("https://g.api.mega.co.nz/cs?id=0", method="POST",
                         data=body, headers={"Content-Type": "application/json"})
    if status != 200:
        return None
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None
    # Réponse normale : [{"s": <octets>, ...}] ; erreur : un entier négatif.
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return _sane(data[0].get("s"))
    return None


# ---------------------------------------------------------------------------
# gofile.io — token invité + wt -> GET /contents -> size (ou somme du dossier)
# ---------------------------------------------------------------------------
_GOFILE_WT_RE = re.compile(r"""wt['"]?\s*[:=]\s*['"]([\w-]{4,})['"]""")


def _size_gofile(url: str) -> int | None:
    m = re.search(r"gofile\.io/(?:d|w)/([A-Za-z0-9]+)", url)
    if not m:
        return None
    code = m.group(1)
    # 1) token invité
    status, raw = _FETCH("https://api.gofile.io/accounts", method="POST",
                         data=b"", headers={"Content-Type": "application/json"})
    if status != 200:
        return None
    try:
        token = json.loads(raw.decode("utf-8", "replace")).get("data", {}).get("token")
    except Exception:
        token = None
    if not token:
        return None
    # 2) wt depuis global.js
    status, raw = _FETCH("https://gofile.io/dist/js/global.js")
    wt = None
    if status == 200:
        mm = _GOFILE_WT_RE.search(raw.decode("utf-8", "replace"))
        wt = mm.group(1) if mm else None
    if not wt:
        return None
    # 3) contents
    status, raw = _FETCH(f"https://api.gofile.io/contents/{code}?wt={wt}",
                         headers={"Authorization": f"Bearer {token}"})
    if status != 200:
        return None
    try:
        data = json.loads(raw.decode("utf-8", "replace")).get("data", {})
    except Exception:
        return None
    if data.get("size") is not None:
        return _sane(data.get("size"))
    # dossier : somme des enfants fichiers
    children = data.get("children") or {}
    if isinstance(children, dict) and children:
        total = sum(int(c.get("size") or 0) for c in children.values() if isinstance(c, dict))
        return _sane(total)
    return None


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
RESOLVERS = [
    ("vikingfile", _size_vikingfile),
    ("mega", _size_mega),
    ("gofile", _size_gofile),
]

# Priorité de fiabilité quand un jeu a plusieurs miroirs.
_HOST_PRIORITY = ["vikingfile.com", "mega.nz", "mega.co.nz", "gofile.io"]


def probe_size(url: str) -> int | None:
    """Renvoie la taille (octets) du fichier derrière `url`, ou None."""
    if not url:
        return None
    cached = _cache_get(url)
    if cached is not False:
        return cached  # int ou None déjà connu
    size = None
    for _, resolver in RESOLVERS:
        try:
            size = resolver(url)
        except Exception:
            size = None
        if size:
            break
    _cache_set(url, size)
    return size


def probe_package_size(pkg: dict) -> int | None:
    """Sonde la taille via les downloadLinks d'un package (miroirs fiables d'abord)."""
    links = pkg.get("downloadLinks") or []
    urls = [l.get("url") for l in links if isinstance(l, dict) and l.get("url")]

    def rank(u: str) -> int:
        h = _host(u)
        return _HOST_PRIORITY.index(h) if h in _HOST_PRIORITY else len(_HOST_PRIORITY)

    for u in sorted(urls, key=rank):
        size = probe_size(u)
        if size:
            return size
    return None


# ---------------------------------------------------------------------------
# Batch : remplir les sizeBytes manquants d'un catalogue
# ---------------------------------------------------------------------------
def fill_missing_sizes(catalog: dict, *, max_probe: int = 0, delay: float = 0.3) -> dict:
    stats = {"total": 0, "already": 0, "probed": 0, "filled": 0}
    pkgs = catalog.get("packages", [])
    stats["total"] = len(pkgs)
    for pkg in pkgs:
        if pkg.get("sizeBytes"):
            stats["already"] += 1
            continue
        if max_probe and stats["probed"] >= max_probe:
            continue
        stats["probed"] += 1
        size = probe_package_size(pkg)
        if size:
            pkg["sizeBytes"] = int(size)
            stats["filled"] += 1
        if delay:
            time.sleep(delay)
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", nargs="?", help="URL d'un fichier à sonder")
    ap.add_argument("--catalog", type=Path, help="Catalogue : remplir les sizeBytes manquants")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--max", type=int, default=0, help="Nb max de jeux à sonder (0 = tous)")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args(argv)

    global CACHE_ENABLED
    if args.no_cache:
        CACHE_ENABLED = False

    if args.catalog:
        catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
        stats = fill_missing_sizes(catalog, max_probe=args.max)
        out = args.out or args.catalog
        out.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Tailles : {stats['total']} jeux | {stats['already']} déjà connues | "
              f"{stats['probed']} sondés | {stats['filled']} complétés")
        return 0

    if args.url:
        size = probe_size(args.url)
        print(f"{args.url} -> {size} octets" if size else f"{args.url} -> taille inconnue")
        return 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
