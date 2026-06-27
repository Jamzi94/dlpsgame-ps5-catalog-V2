#!/usr/bin/env python3
"""Diag faisabilité option 3 (SuperPSX) — via FlareSolverr.

1) Confirme que FlareSolverr franchit le Cloudflare de superpsx.com (HTTP 200 + vrai HTML).
2) Sonde keepshield.org/safe/<id> : est-ce résolvable vers les vrais miroirs
   (vikingfile/akirabox/gofile/mega...) ou bloqué par captcha/timer/JS-locker ?

Lancé par .github/workflows/diag-keepshield.yml (FlareSolverr en service). Log court → lisible.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

FS_URL = os.environ.get("FLARESOLVERR_URL", "http://localhost:8191/v1")

MIRROR_HOSTS = (
    "vikingfile", "akirabox", "gofile", "mega.nz", "mega.co",
    "1fichier", "pixeldrain", "buzzheavier", "datanodes", "onefile",
    "fileaxa", "rapidgator", "1cloudfile", "mediafire",
)
LOCKER_MARKERS = (
    "captcha", "recaptcha", "hcaptcha", "turnstile", "verify you",
    "are human", "countdown", "timer", "please wait", "get link",
    "continue to", "unlock", "i am not a robot", "just a moment",
)


def fs_get(url: str, max_timeout: int = 60000) -> dict:
    body = json.dumps({"cmd": "request.get", "url": url, "maxTimeout": max_timeout}).encode()
    req = urllib.request.Request(
        FS_URL, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=max_timeout / 1000 + 40) as resp:
        return json.load(resp)


def probe(url: str) -> None:
    print(f"\n===== FlareSolverr GET {url} =====")
    try:
        data = fs_get(url)
    except Exception as exc:  # noqa: BLE001
        print(f"EXCEPTION: {type(exc).__name__}: {exc}")
        return
    sol = data.get("solution", {}) or {}
    html = (sol.get("response") or "")
    low = html.lower()
    print(f"flaresolverr status : {data.get('status')} ({data.get('message','')[:60]})")
    print(f"http status         : {sol.get('status')}")
    print(f"final url           : {sol.get('url')}")
    print(f"html length         : {len(html)}")
    cf = "just a moment" in low or "challenge-platform" in low
    print(f"cloudflare bloque ? : {cf}")
    mirrors = sorted({h for h in MIRROR_HOSTS if h in low})
    print(f"miroirs visibles    : {mirrors or 'AUCUN'}")
    lockers = sorted({m for m in LOCKER_MARKERS if m in low})
    print(f"indices locker      : {lockers or 'aucun'}")
    # liens externes notables
    import re
    ext = re.findall(r'https?://([a-z0-9.-]+)/', low)
    from collections import Counter
    top = Counter(d for d in ext if "superpsx" not in d and "keepshield" not in d
                  and "cloudflare" not in d and "google" not in d
                  and "facebook" not in d and "twitter" not in d
                  and "youtube" not in d and "discord" not in d).most_common(8)
    print(f"domaines externes top: {top}")
    print("---- snippet (400c) ----")
    print(html[:400].replace("\n", " "))


def main() -> int:
    print(f"FS_URL = {FS_URL}")
    # 1) SuperPSX via FlareSolverr (bypass Cloudflare ?)
    probe("https://www.superpsx.com/26528-2626/")
    # 2) keepshield locker — résolvable ?
    probe("https://www.keepshield.org/safe/1b4c7f6a")

    print("\n===== VERDICT (à lire dans les sorties ci-dessus) =====")
    print("- Si superpsx 200 + miroirs/HTML réel => FlareSolverr OK pour SuperPSX.")
    print("- Si keepshield montre les miroirs => option 3 faisable (résolution directe).")
    print("- Si keepshield montre captcha/timer/turnstile => locker NON résoluble auto.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
