#!/usr/bin/env python3
"""
Enrichit un catalogue Pegasus avec les métadonnées RAWG (couverture, note,
Metacritic, genres, date de sortie).

Cache intégré : chaque package porte un champ `_enrichedAt` (timestamp ISO).
Un jeu n'est ré-interrogé que si cet enrichissement a plus de `--ttl-days`
jours (3 par défaut). Les jeux récemment enrichis sont sautés sans appel API,
ce qui garde la consommation bien sous le quota gratuit RAWG (20 000/mois).

Usage :
  RAWG_API_KEY=xxxx python enrich_rawg.py catalogue.json
  RAWG_API_KEY=xxxx python enrich_rawg.py in.json --out out.json --ttl-days 3 --max-calls 900

Si RAWG_API_KEY est absent, le script ne fait rien (sortie propre) : le
pipeline reste fonctionnel même sans clé configurée.
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

RAWG_SEARCH = "https://api.rawg.io/api/games"

# Budget RAWG : ~5 req/s côté API (gratuit). On reste poliment en dessous.
RAWG_RATE_PER_SEC = 5.0


# ---------------------------------------------------------------------------
# Token-bucket partagé (thread-safe) — borne le débit global quel que soit le
# nombre de threads. Chaque appel consomme 1 jeton ; les jetons se régénèrent
# à `rate` par seconde (capacité = rate, pas de rafale supérieure à 1s).
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
USER_AGENT = "dlpsgame-pegasus-enricher/1.0"

# ID de plateforme RAWG pour "PlayStation 5" (vérifié). On filtre la recherche
# dessus pour éviter de matcher la version PS4/PC/Switch d'un jeu homonyme.
RAWG_PS5_PLATFORM_ID = 187


# ---------------------------------------------------------------------------
# Correspondance de titres (évite les faux matches)
# ---------------------------------------------------------------------------
def _normalize_title(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _best_match(title: str, results: list[dict]) -> dict | None:
    """Choisit le meilleur résultat RAWG pour `title`, ou None si trop éloigné.

    Exact (normalisé) > inclusion > recouvrement de tokens (Jaccard >= 0.6).
    Évite d'attribuer une fiche RAWG sans rapport au jeu scrapé.
    """
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
# Appel API isolé (facile à mocker pour les tests)
# ---------------------------------------------------------------------------
def fetch_rawg(title: str, api_key: str, *, timeout: int = 20) -> dict | None:
    """Interroge RAWG (filtré PS5) et retourne le meilleur match, ou None.

    Filtre `platforms=187` (PS5) + vérification du nom pour fiabiliser le match.
    RAWG ne fournit aucune jaquette : on l'utilise pour les métadonnées
    (note, genres, date, metacritic), PAS pour la cover.
    """
    params = urllib.parse.urlencode({
        "key": api_key,
        "search": title,
        "search_precise": "true",
        "platforms": str(RAWG_PS5_PLATFORM_ID),
        "page_size": 5,
    })
    req = urllib.request.Request(f"{RAWG_SEARCH}?{params}", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    results = data.get("results") or []
    return _best_match(title, results)


# ---------------------------------------------------------------------------
# Logique d'enrichissement
# ---------------------------------------------------------------------------

# TTL dynamiques (configurables via CLI)
_TTL_MATCHED = 30     # Jeux matchés RAWG : rafraîchir tous les 30 jours
_TTL_UNMATCHED = 14  # Jeux non trouvés : réessayer tous les 14 jours


def get_ttl_for_package(pkg: dict, default_ttl: int = 3) -> int:
    """TTL dynamique basé sur le statut d'enrichissement du package.

    - Jamais enrichi : 0 (à enrichir immédiatement)
    - Matché RAWG avec succès : ttl_days_matched (défaut 30)
    - Non trouvé dans RAWG : ttl_days_unmatched (défaut 14)
    """
    ts = pkg.get("_enrichedAt")
    if not ts:
        return 0  # Jamais enrichi → prioritaire
    if pkg.get("_rawgMatched") is True:
        return _TTL_MATCHED  # Matché → long TTL
    if pkg.get("_rawgMatched") is False:
        return _TTL_UNMATCHED  # Non matché → TTL moyen
    return default_ttl  # Inconnu → TTL par défaut


def is_fresh(pkg: dict, ttl_days: int) -> bool:
    """Le package a-t-il été enrichi il y a moins de ttl_days jours ?

    Si ttl_days <= 0, le package n'est jamais considéré comme frais.
    Utilise get_ttl_for_package() pour un TTL dynamique."""
    ts = pkg.get("_enrichedAt")
    if not ts:
        return False
    try:
        when = dt.datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    age = dt.datetime.now(dt.timezone.utc) - when
    return age < dt.timedelta(days=ttl_days)


def apply_rawg(pkg: dict, result: dict | None) -> None:
    """Applique les métadonnées RAWG au package (sur place).

    IMPORTANT : RAWG ne fournit AUCUNE jaquette/cover. Le champ
    `background_image` est une image PAYSAGE (screenshot ou hero art), pas une
    cover portrait. L'utiliser comme posterUrl (comportement historique) est un
    bug : on ne touche donc PLUS à posterUrl ici. La vraie jaquette vient de la
    cover scrapée du site (og:image) ou d'IGDB (voir enrich_igdb.py).
    On conserve background_image dans metadata.rawgBackground (utilisable comme
    fond/écran, jamais comme cover).
    """
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    pkg["_enrichedAt"] = now_iso
    if not result:
        pkg["_rawgMatched"] = False
        return
    pkg["_rawgMatched"] = True
    pkg["metadata"] = {
        "rawgSlug": result.get("slug"),
        "rawgName": result.get("name"),
        "rating": result.get("rating"),
        "metacritic": result.get("metacritic"),
        "released": result.get("released"),
        "genres": [g.get("name") for g in (result.get("genres") or []) if g.get("name")],
        "rawgBackground": result.get("background_image"),
    }


def _prioritize_packages(packages: list[dict]) -> list[dict]:
    """Trie les packages pour prioriser les jeux jamais enrichis."""
    def priority(pkg):
        if not pkg.get("_enrichedAt"):
            return 0  # Jamais enrichi → priorité max
        if pkg.get("_rawgMatched") is False:
            return 2  # Non matché → basse priorité
        return 1  # Matché → priorité moyenne
    return sorted(packages, key=priority)


def enrich_catalog(catalog: dict, api_key: str, *, ttl_days: int,
                   max_calls: int, delay: float, concurrency: int = 4) -> dict:
    packages = catalog.get("packages", [])
    # Prioriser : jeux jamais enrichis en premier
    packages = _prioritize_packages(packages)
    stats = {"total": len(packages), "fresh": 0, "enriched": 0,
             "matched": 0, "unmatched": 0, "errors": 0, "calls": 0, "capped": 0}

    # Présélection (mono-thread, déterministe) : on ne garde que les jeux à
    # enrichir et on applique le plafond --max-calls AVANT de paralléliser, pour
    # un budget d'appels strict et reproductible.
    todo: list[dict] = []
    for pkg in packages:
        title = (pkg.get("title") or "").strip()
        if not title:
            continue
        # TTL dynamique : chaque jeu a son propre TTL
        effective_ttl = get_ttl_for_package(pkg, default_ttl=ttl_days)
        if is_fresh(pkg, effective_ttl):
            stats["fresh"] += 1
            continue
        if max_calls and len(todo) >= max_calls:
            stats["capped"] += 1
            continue  # plafond atteint : on laissera ce jeu au prochain run
        todo.append(pkg)

    if not todo:
        return stats

    # Token-bucket partagé : borne le débit global ~5 req/s quel que soit le
    # nombre de threads. La sérialisation JSON finale reste mono-thread.
    bucket = TokenBucket(RAWG_RATE_PER_SEC)
    workers = max(1, concurrency)

    def _do(pkg: dict) -> tuple[dict, dict | None, Exception | None]:
        title = (pkg.get("title") or "").strip()
        bucket.acquire()
        # Jitter léger pour désynchroniser les threads et lisser les rafales.
        time.sleep(random.uniform(0.1, 0.3))
        try:
            return pkg, fetch_rawg(title, api_key), None
        except Exception as exc:  # noqa: BLE001
            return pkg, None, exc

    if workers <= 1:
        results_iter = (_do(p) for p in todo)
    else:
        pool = cf.ThreadPoolExecutor(max_workers=workers)
        results_iter = pool.map(_do, todo)

    # Consommation mono-thread des résultats (mutation pkg + stats sûre).
    for pkg, result, exc in results_iter:
        title = (pkg.get("title") or "").strip()
        stats["calls"] += 1
        if exc is not None:
            stats["errors"] += 1
            print(f"  [warn] {title}: {exc}", file=sys.stderr)
            # on n'écrit pas _enrichedAt => sera retenté au prochain run
            continue
        apply_rawg(pkg, result)
        stats["enriched"] += 1
        if pkg.get("_rawgMatched"):
            stats["matched"] += 1
        else:
            stats["unmatched"] += 1

    if workers > 1:
        pool.shutdown(wait=True)

    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("catalog", type=Path, help="Catalogue Pegasus à enrichir")
    ap.add_argument("--out", type=Path, default=None, help="Fichier de sortie (défaut: sur place)")
    ap.add_argument("--ttl-days", type=int, default=3, help="Âge max avant ré-enrichissement (défaut 3)")
    ap.add_argument("--max-calls", type=int, default=900,
                    help="Plafond d'appels API par run (sécurité quota, défaut 900 ; 0 = illimité)")
    ap.add_argument("--delay", type=float, default=0.2, help="Délai entre appels en secondes (défaut 0.2)")
    ap.add_argument("--concurrency", type=int, default=4,
                    help="Nb de threads d'enrichissement parallèles (défaut 4 ; "
                         "le débit global reste borné ~5 req/s par token-bucket)")
    args = ap.parse_args(argv)

    api_key = os.environ.get("RAWG_API_KEY", "").strip()
    if not api_key:
        print("RAWG_API_KEY absente — enrichissement ignoré (le catalogue reste inchangé).")
        return 0

    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    if "packages" not in catalog:
        print("Fichier invalide : clé 'packages' absente.", file=sys.stderr)
        return 1

    stats = enrich_catalog(catalog, api_key, ttl_days=args.ttl_days,
                           max_calls=args.max_calls, delay=args.delay,
                           concurrency=args.concurrency)

    out = args.out or args.catalog
    out.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"Enrichissement terminé : {stats['total']} jeux | "
        f"{stats['fresh']} déjà à jour (cache) | {stats['enriched']} enrichis "
        f"({stats['matched']} trouvés, {stats['unmatched']} non trouvés) | "
        f"{stats['errors']} erreurs | {stats['calls']} appels API"
        + (f" | {stats['capped']} reportés (plafond)" if stats['capped'] else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
