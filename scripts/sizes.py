#!/usr/bin/env python3
"""
Analyse centralisée des tailles de fichiers (sizeBytes).
=========================================================
Corrige un bug confirmé : l'ancienne regex
    r"(\\d+(?:[.,]\\d+)?)\\s*([KMGT])[\\s]?(?:B|O|b|o)\\b"
acceptait une unité d'UNE lettre [KMGT] suivie de B **ou** O/o, si bien que le
mot anglais « to » (= T+o) était lu comme des Téraoctets. Résultat : des jeux
avec « Size: 2023 to » → 2.2 pétaoctets (5 entrées aberrantes en live).

Stratégie (vérifiée sur les 759 packages réels : 0 taille française légitime) :
  - Extraction depuis texte libre : on n'accepte QUE des unités anglaises
    complètes (KB/MB/GB/TB). « to » ne contient pas « TB » → jamais capté.
  - Ancrage prioritaire sur « SIZE: » pour ne pas prendre un numéro de firmware
    (« 5.50 to 7.xx ») ou une année pour une taille.
  - parse_size_bytes() (token déjà isolé, ex. cellule « Size » d'une table)
    accepte aussi un token français PROPRE (toute la chaîne == une taille),
    sans jamais matcher « to » au milieu d'une phrase.
"""
from __future__ import annotations

import re

_POW = {"K": 1, "M": 2, "G": 3, "T": 4}
_NUM = r"(\d{1,4}(?:[.,]\d{1,2})?)"

# Texte libre : unités anglaises complètes uniquement (anti-« to »).
_SIZE_ANCHORED = re.compile(r"(?i)\bSIZE\s*[:\-–]\s*" + _NUM + r"\s?([KMGT])B\b")
_SIZE_FREE = re.compile(r"(?i)" + _NUM + r"\s?([KMGT])B\b")

# Token isolé : anglais (KB/MB/GB/TB) OU token français propre (Ko/Mo/Go/To)
# borné par ^...$ pour rester sans ambiguïté.
_TOKEN_EN = re.compile(r"(?i)" + _NUM + r"\s?([KMGT])B\b")
_TOKEN_FR = re.compile(r"(?i)^\s*" + _NUM + r"\s?([KMGT])[O]\s*$")


def _to_bytes(value_str: str, scale: str) -> int:
    return int(float(value_str.replace(",", ".")) * (1024 ** _POW[scale.upper()]))


def parse_size_bytes(value) -> int | None:
    """Parse une taille DÉJÀ isolée (token : '54GB', '12 Go', '1.5 GB', int…)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value >= 0 else None
    s = str(value).replace("\xa0", " ")
    m = _TOKEN_EN.search(s) or _TOKEN_FR.match(s)
    return _to_bytes(m.group(1), m.group(2)) if m else None


def extract_size(text) -> tuple[int | None, str | None]:
    """Extrait (octets, libellé) depuis un texte libre. Ancré sur SIZE: d'abord.

    Renvoie (None, None) si aucune taille fiable. Le libellé est normalisé
    (« 54 GB ») pour l'affichage dans la description.
    """
    if not text:
        return None, None
    t = str(text).replace("\xa0", " ")
    m = _SIZE_ANCHORED.search(t) or _SIZE_FREE.search(t)
    if not m:
        return None, None
    return _to_bytes(m.group(1), m.group(2)), f"{m.group(1)} {m.group(2).upper()}B"


def extract_size_bytes(text) -> int | None:
    return extract_size(text)[0]


# ---------------------------------------------------------------------------
# Auto-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    GB, MB, TB = 1024**3, 1024**2, 1024**4
    ok = True

    def check(label, got, expected):
        global ok
        status = "OK " if got == expected else "FAIL"
        if got != expected:
            ok = False
        print(f"  [{status}] {label}: {got}  (attendu {expected})")

    # Extraction texte libre (doit IGNORER le bug 'to')
    check("'Size: 54GB'", extract_size_bytes("blah Size: 54GB blah"), 54 * GB)
    check("'117GB'", extract_size_bytes("Game 117GB total"), 117 * GB)
    check("'1.5 GB'", extract_size_bytes("1.5 GB"), int(1.5 * GB))
    check("'850 MB'", extract_size_bytes("850 MB"), 850 * MB)
    check("'15.9gb'", extract_size_bytes("size 15.9gb"), int(15.9 * GB))
    check("'SIZE : 54 GB'", extract_size_bytes("SIZE : 54 GB"), 54 * GB)
    # Le bug 'to' — TOUS doivent donner None
    check("'2023 to' (bug)", extract_size_bytes("BIOHAZARD RE 4 Size:2023 to"), None)
    check("'6.50 to' (bug)", extract_size_bytes("6.50 to"), None)
    check("'5 to' (bug)", extract_size_bytes("Devil May Cry 5 to"), None)
    check("'2023 to 2024'", extract_size_bytes("released 2023 to 2024"), None)
    check("'5.50 to 7.xx'", extract_size_bytes("FW 5.50 to 7.xx"), None)
    check("'SIZE: 2 to 4 players'", extract_size_bytes("SIZE: 2 to 4 players"), None)
    # Token isolé
    check("token '117 GB'", parse_size_bytes("117 GB"), 117 * GB)
    check("token '12 Go' (FR)", parse_size_bytes("12 Go"), 12 * GB)
    check("token '2 to 4 players'", parse_size_bytes("2 to 4 players"), None)
    check("token int", parse_size_bytes(57982058496), 57982058496)
    check("token None", parse_size_bytes(None), None)

    print("ALL OK" if ok else "SOME FAILURES")
    raise SystemExit(0 if ok else 1)
