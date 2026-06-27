#!/usr/bin/env python3
"""
Enrichit un catalogue Pegasus avec les VRAIES jaquettes PS5 via IGDB.
====================================================================
RAWG ne fournit aucune jaquette (son `background_image` est une image paysage
— screenshot/hero — d'où le problème historique « images lambda »). IGDB, lui,
expose une cover PORTRAIT (la jaquette du jeu). Ce script récupère cette cover
et la pose dans `posterUrl`.

Auth (Twitch, gratuit) :
  - Crée une app sur https://dev.twitch.tv/console/apps
  - Exporte TWITCH_CLIENT_ID et TWITCH_CLIENT_SECRET
  - Sans ces variables, le script ne fait RIEN (no-op propre), exactement comme
    enrich_rawg.py sans RAWG_API_KEY. Le pipeline reste fonctionnel.

Cache : chaque package porte `_igdbEnrichedAt` (ISO) + `_igdbMatched` (bool).
Un jeu déjà couvert n'est ré-interrogé qu'après `--ttl-days` (matché) ou
réessayé après un délai plus court (non matché), comme RAWG.

Usage :
  TWITCH_CLIENT_ID=xxx TWITCH_CLIENT_SECRET=yyy python enrich_igdb.py catalogue.json
  ... python enrich_igdb.py in.json --out out.json --ttl-days 30 --max-calls 500
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import random
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

# IGDB impose une limite STRICTE de 4 requêtes/seconde par client.
IGDB_RATE_PER_SEC = 4.0


# ---------------------------------------------------------------------------
# Token-bucket partagé (thread-safe) — borne le débit global à `rate` req/s
# quel que soit le nombre de threads (capacité = rate : pas de rafale > 1s).
# ---------------------------------------------------------------------------
class TokenBucket:
    def __init__(self, rate: float, capacity: float | None = None) -> None:
        self.rate = float(rate)
        self.capacity = float(capacity if capacity is not None else rate)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Bloque jusqu'à ce qu'un jeton soit disponible, puis le consomme."""
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity,
                                   self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self.rate
            time.sleep(wait)


TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
IGDB_GAMES_URL = "https://api.igdb.com/v4/games"
IGDB_IMAGE_TMPL = "https://images.igdb.com/igdb/image/upload/{size}/{image_id}.jpg"
IGDB_COVER_SIZE = "t_cover_big"  # jaquette portrait (264x374) ; _2x pour Retina
IGDB_PS5_PLATFORM_ID = 167       # PlayStation 5 sur IGDB (vérifié)
USER_AGENT = "dlpsgame-pegasus-igdb/1.0"

# TTL dynamiques (jours)
_TTL_MATCHED = 60      # cover trouvée : stable, rafraîchir rarement
_TTL_UNMATCHED = 21    # pas trouvée : réessayer (IGDB s'enrichit avec le temps)


# ---------------------------------------------------------------------------
# Correspondance de titres
# ---------------------------------------------------------------------------
def _normalize_title(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _best_match(title: str, results: list[dict]) -> dict | None:
    nt = _normalize_title(title)
    if not nt:
        return None
    best: dict | None = None
    best_score = -1
    for r in results:
        nn = _normalize_title(r.get("name") or "")
        if not nn:
            continue
        if nn == nt:
            return r
        if nt in nn or nn in nt:
            score = 80
        else:
            ta, tb = set(nt.split()), set(nn.split())
            score = int(100 * len(ta & tb) / len(ta | tb)) if (ta and tb) else 0
        if score > best_score:
            best_score, best = score, r
    return best if best_score >= 60 else None


# ---------------------------------------------------------------------------
# Auth Twitch (client_credentials)
# ---------------------------------------------------------------------------
def get_twitch_token(client_id: str, client_secret: str, *, timeout: int = 20) -> str:
    params = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
    })
    req = urllib.request.Request(
        f"{TWITCH_TOKEN_URL}?{params}", method="POST",
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"Twitch token absent de la réponse: {data}")
    return token


# ---------------------------------------------------------------------------
# Requête IGDB
# ---------------------------------------------------------------------------
def fetch_igdb_cover(title: str, client_id: str, token: str, *, timeout: int = 20) -> str | None:
    """Retourne l'URL de jaquette IGDB (PS5) pour `title`, ou None."""
    # Apicalypse : on demande le nom + l'image_id de la cover, filtré PS5.
    safe = title.replace('"', " ").strip()
    body = (
        f'search "{safe}"; '
        f'fields name,cover.image_id,platforms; '
        f'where platforms = ({IGDB_PS5_PLATFORM_ID}); '
        f'limit 5;'
    ).encode("utf-8")
    req = urllib.request.Request(
        IGDB_GAMES_URL, data=body, method="POST",
        headers={
            "Client-ID": client_id,
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        results = json.loads(resp.read().decode("utf-8"))
    if not isinstance(results, list) or not results:
        return None
    match = _best_match(title, results)
    if not match:
        return None
    cover = match.get("cover") or {}
    image_id = cover.get("image_id") if isinstance(cover, dict) else None
    if not image_id:
        return None
    return IGDB_IMAGE_TMPL.format(size=IGDB_COVER_SIZE, image_id=image_id)


# ---------------------------------------------------------------------------
# Cache / TTL
# ---------------------------------------------------------------------------
def _get_ttl(pkg: dict, default_ttl: int) -> int:
    if not pkg.get("_igdbEnrichedAt"):
        return 0
    if pkg.get("_igdbMatched") is True:
        return _TTL_MATCHED
    if pkg.get("_igdbMatched") is False:
        return _TTL_UNMATCHED
    return default_ttl


def is_fresh(pkg: dict, ttl_days: int) -> bool:
    ts = pkg.get("_igdbEnrichedAt")
    if not ts or ttl_days <= 0:
        return False
    try:
        when = dt.datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - when) < dt.timedelta(days=ttl_days)


def _priority(pkg: dict) -> int:
    # Prioriser : jamais enrichi IGDB d'abord, puis ceux dont la cover est encore
    # une image RAWG (à remplacer en priorité), puis le reste.
    if not pkg.get("_igdbEnrichedAt"):
        return 0
    if "rawg.io" in (pkg.get("posterUrl") or "").lower():
        return 1
    if pkg.get("_igdbMatched") is False:
        return 3
    return 2


def enrich_catalog(catalog: dict, client_id: str, token: str, *,
                   ttl_days: int, max_calls: int, delay: float,
                   concurrency: int = 3) -> dict:
    packages = sorted(catalog.get("packages", []), key=_priority)
    stats = {"total": len(packages), "fresh": 0, "calls": 0,
             "matched": 0, "unmatched": 0, "errors": 0, "capped": 0}

    # Présélection mono-thread + plafond --max-calls AVANT parallélisation
    # (budget d'appels strict et ordre déterministe).
    todo: list[dict] = []
    for pkg in packages:
        title = (pkg.get("title") or "").strip()
        if not title:
            continue
        if is_fresh(pkg, _get_ttl(pkg, ttl_days)):
            stats["fresh"] += 1
            continue
        if max_calls and len(todo) >= max_calls:
            stats["capped"] += 1
            continue
        todo.append(pkg)

    if not todo:
        return stats

    # Token-bucket partagé : IGDB est limité STRICTEMENT à 4 req/s.
    bucket = TokenBucket(IGDB_RATE_PER_SEC)
    workers = max(1, concurrency)

    def _do(pkg: dict) -> tuple[dict, str | None, Exception | None]:
        title = (pkg.get("title") or "").strip()
        bucket.acquire()
        # Jitter léger pour désynchroniser les threads.
        time.sleep(random.uniform(0.1, 0.3))
        try:
            return pkg, fetch_igdb_cover(title, client_id, token), None
        except Exception as exc:  # noqa: BLE001
            return pkg, None, exc

    if workers <= 1:
        results_iter = (_do(p) for p in todo)
    else:
        pool = cf.ThreadPoolExecutor(max_workers=workers)
        results_iter = pool.map(_do, todo)

    # Consommation mono-thread des résultats (mutation pkg + stats sûre).
    for pkg, cover, exc in results_iter:
        title = (pkg.get("title") or "").strip()
        if exc is not None:
            stats["errors"] += 1
            print(f"  [warn] {title}: {exc}", file=sys.stderr)
            continue
        stats["calls"] += 1
        pkg["_igdbEnrichedAt"] = dt.datetime.now(dt.timezone.utc).isoformat()
        if cover:
            pkg["posterUrl"] = cover  # vraie jaquette portrait
            pkg["_igdbMatched"] = True
            stats["matched"] += 1
        else:
            pkg["_igdbMatched"] = False
            stats["unmatched"] += 1

    if workers > 1:
        pool.shutdown(wait=True)
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("catalog", type=Path, help="Catalogue Pegasus à enrichir")
    ap.add_argument("--out", type=Path, default=None, help="Sortie (défaut: sur place)")
    ap.add_argument("--ttl-days", type=int, default=30, help="Âge max avant ré-essai (défaut 30)")
    ap.add_argument("--max-calls", type=int, default=500,
                    help="Plafond d'appels IGDB par run (défaut 500 ; 0 = illimité)")
    ap.add_argument("--delay", type=float, default=0.28,
                    help="Délai entre appels (défaut 0.28s ; IGDB limite à 4 req/s)")
    ap.add_argument("--concurrency", type=int, default=3,
                    help="Nb de threads parallèles (défaut 3 ; débit global borné "
                         "à 4 req/s par token-bucket — limite IGDB stricte)")
    args = ap.parse_args(argv)

    client_id = os.environ.get("TWITCH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("TWITCH_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        print("TWITCH_CLIENT_ID/SECRET absents — enrichissement IGDB ignoré "
              "(le catalogue reste inchangé).")
        return 0

    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    if "packages" not in catalog:
        print("Fichier invalide : clé 'packages' absente.", file=sys.stderr)
        return 1

    try:
        token = get_twitch_token(client_id, client_secret)
    except Exception as exc:  # noqa: BLE001
        print(f"Auth Twitch/IGDB échouée: {exc}", file=sys.stderr)
        return 0  # non bloquant : on n'altère pas le catalogue

    stats = enrich_catalog(catalog, client_id, token,
                           ttl_days=args.ttl_days, max_calls=args.max_calls,
                           delay=args.delay, concurrency=args.concurrency)

    out = args.out or args.catalog
    out.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"IGDB terminé : {stats['total']} jeux | {stats['fresh']} à jour (cache) | "
        f"{stats['calls']} appels ({stats['matched']} jaquettes trouvées, "
        f"{stats['unmatched']} non trouvées) | {stats['errors']} erreurs"
        + (f" | {stats['capped']} reportés (plafond)" if stats['capped'] else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
