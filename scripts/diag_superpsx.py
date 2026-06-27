#!/usr/bin/env python3
"""Diagnostic SuperPSX — que voit réellement le runner GitHub ?

Single-shot (sans le backoff de 3,8 min de http_get) pour révéler vite le
status HTTP et un éventuel mur Cloudflare / blocage IP. Lancé par le workflow
.github/workflows/diag-superpsx.yml (job court → log entièrement lisible).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import scrape_superpsx as sp  # noqa: E402

BLOCK_MARKERS = (
    "just a moment", "cloudflare", "cf-browser-verification",
    "challenge-platform", "attention required", "access denied",
    "captcha", "ddos", "cf-ray", "enable javascript",
)


def show(url: str) -> int | None:
    """Single GET (pas de retry/backoff) : status, taille, indices de blocage."""
    print(f"\n===== GET {url} =====")
    try:
        resp = sp._fetch(url)  # un seul appel curl, sans backoff
    except Exception as exc:  # noqa: BLE001
        print(f"EXCEPTION: {type(exc).__name__}: {exc}")
        return None
    html = (resp.text or "")
    low = html.lower()
    flags = sorted({m for m in BLOCK_MARKERS if m in low})
    print(f"status_code : {resp.status_code}")
    print(f"final_url   : {getattr(resp, 'url', url)}")
    print(f"html_len    : {len(html)}")
    print(f"blocage?    : {flags or 'aucun indice évident'}")
    # Compte de liens d'articles PS5 visibles (ce que la découverte cherche)
    n_ps5 = low.count("/ps5/") + low.count("ps5-games")
    print(f"occurrences 'ps5' dans la page : {n_ps5}")
    print("---- premiers 600 caractères ----")
    print(html[:600].replace("\n", " "))
    return resp.status_code


def main() -> int:
    print(f"Backend HTTP : {sp._HTTP_BACKEND}")
    print(f"BASE_URL     : {sp.BASE_URL}")

    show(sp.BASE_URL + "/")
    cat_code = show(sp.BASE_URL + "/category/ps5/ps5-games/")

    # Découverte bornée : on coupe les retries pour ne pas subir 3,8 min de backoff.
    if hasattr(sp, "HTTP_RETRIES"):
        sp.HTTP_RETRIES = 1
    print("\n===== discover_game_urls(max_pages=1) =====")
    try:
        urls = sp.discover_game_urls(1)
        print(f"URLs découvertes : {len(urls)}")
        for u in urls[:5]:
            print("  -", u)
    except Exception as exc:  # noqa: BLE001
        print(f"EXCEPTION discovery: {type(exc).__name__}: {exc}")

    print("\n===== VERDICT =====")
    if cat_code in (403, 429, 503):
        print(f"-> Le site BLOQUE le runner (HTTP {cat_code}). "
              "curl simple insuffisant : il faudrait FlareSolverr (comme dlpsgame) "
              "ou une autre IP.")
    elif cat_code == 200:
        print("-> Le site répond 200 : si 0 URL découverte, c'est la STRUCTURE HTML "
              "qui a changé (sélecteurs de discover_game_urls à adapter).")
    else:
        print(f"-> Status inattendu ({cat_code}) : voir le dump ci-dessus.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
