#!/usr/bin/env python3
"""
Finalisation + validation d'un catalogue pour Pegasus DL.
==========================================================
Étape unique de fin de pipeline (idempotente), exécutée après fusion +
enrichissements. Elle :

  1. Assainit `sizeBytes` : retire toute valeur <= 0 ou aberrante (> 2 To,
     filet de sécurité contre le bug « to » résiduel). Un sizeBytes inconnu
     est OMIS (jamais null/0). NB : `sizeBytes` est un champ OPTIONNEL de
     Pegasus DL — son absence ne fait PAS ignorer le jeu (contrairement à une
     croyance répandue ; seuls titleId/title/downloadLinks[].url sont requis).
  2. Canonicalise `fileFormat` via le module formats (libellés stables).
  3. SURFACE le format dans des champs visibles : ajoute `formatLabel`
     (ex. « FPKG · Backport 4.xx ») et préfixe la description d'une ligne
     « Format: … » (visible dans la vue détail Pegasus).
  4. Valide les champs requis Pegasus et nettoie les downloadLinks invalides
     (URL vide ou non http). Rapporte un résumé ; en --strict, sort != 0 si
     des jeux n'ont aucun lien valide.

Usage :
  python pegasus_finalize.py dlpsgame-ps5.json
  python pegasus_finalize.py in.json --out out.json --strict
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from formats import display_label, normalize_formats

# Garde-fou : aucun jeu PS5 réel n'approche 900 Go (le SSD console fait 825 Go).
# Le bug historique « to » produit toujours des tailles >= 1 To ("1 to"=1 To,
# "2 to"=2 To…), donc 900 Go sépare proprement le réel des artefacts.
MAX_SANE_BYTES = 900 * 1024 ** 3
REAL_TITLEID_RE = re.compile(r"^[A-Z]{4}\d{3,}$")

# Marque unique exposée dans le JSON : on masque les vraies sources
# (dlpsgame/superpsx/exFAT) pour TOUS les jeux.
BRAND = "Phoenix DL"
# IMPORTANT : l'app affiche la SOURCE PAGE comme le HOSTNAME de cette URL. Un
# hostname ne peut pas contenir d'espace/majuscule, donc downloadSource DOIT
# être une URL valide -> l'app montrera « phoenixdl.com ». (Un texte brut comme
# « Phoenix DL » est interprété en URL relative et retombe sur l'IP de l'appareil.)
BRAND_SOURCE_URL = "https://phoenixdl.com"


# Conteneurs (pas un format de jeu) et libellés de section (axe orthogonal).
_CONTAINER_FMT = {"rar", "zip", "7z", "iso", "tar", "gz", "part"}
_SECTION_FMT = {"exfat", "backport", "dlc", "dump", "standard", "fix"}


def _base_format(file_format) -> str:
    """Type de PAQUET de base (PKG/FPKG/APR-EMU…), hors conteneurs et hors
    libellés de section (exFAT/Backport/DLC). Étiquette de la section « Standard »."""
    if not isinstance(file_format, list):
        return ""
    tags: list[str] = []
    for f in file_format:
        fl = str(f).lower()
        if fl in _CONTAINER_FMT or fl in _SECTION_FMT or fl.startswith("backport"):
            continue
        if str(f) not in tags:
            tags.append(str(f))
    return " · ".join(tags)


def _link_format(name: str, url: str, game_fmt: str, base_fmt: str, group: str) -> str:
    """Format SPÉCIFIQUE d'un lien. Priorité :
      1) la SECTION captée au scraping (group : exFAT/Backport/DLC/Dump) — fiable ;
      2) heuristique nom + URL (DLC, version backport, exfat/pkg/fpkg/apr-emu) ;
      3) section « Standard » -> format de paquet de base (PKG/FPKG/APR-EMU) ;
      4) repli sur le format du jeu (hôtes à hash sans info exploitable)."""
    g = (group or "").strip()
    blob = f"{name} {url}".lower()

    def _backport_with_version() -> str:
        m = re.search(r"\b([4-9])\.xx\b", blob) or re.search(r"[-_/]([4-9])\.\d{2}[-_/]", blob)
        return f"Backport {m.group(1)}.xx" if m else "Backport"

    # 1) Section identifiée au scraping (sauf « Standard », traité plus bas)
    if g and g.lower() != "standard":
        return _backport_with_version() if g == "Backport" else g

    # 2) Heuristique nom/URL
    if "dlc" in blob:
        return "DLC"
    fmts: list[str] = []
    if "exfat" in blob:
        fmts.append("exFAT")
    if "fpkg" in blob:
        fmts.append("FPKG")
    elif re.search(r"\bpkg\b", blob):
        fmts.append("PKG")
    if re.search(r"apr[\s_-]?emu", blob):
        fmts.append("APR-EMU")
    if re.search(r"\b([4-9])\.xx\b", blob) or re.search(r"[-_/]([4-9])\.\d{2}[-_/]", blob):
        fmts.append(_backport_with_version())
    elif "backport" in blob:
        fmts.append("Backport")
    detected = " · ".join(dict.fromkeys(fmts))
    if detected:
        return detected

    # 3) Section « Standard » -> format de paquet de base ; 4) sinon format du jeu
    if g.lower() == "standard" and base_fmt:
        return base_fmt
    return game_fmt


def _clean_links(pkg: dict) -> int:
    """Retire les downloadLinks à URL vide ou non http(s). Renvoie le nb gardé."""
    links = pkg.get("downloadLinks") or []
    kept = []
    for l in links:
        url = (l.get("url") or "").strip() if isinstance(l, dict) else ""
        if url.startswith(("http://", "https://")):
            kept.append(l)
    pkg["downloadLinks"] = kept
    return len(kept)


def finalize_package(pkg: dict, stats: dict) -> None:
    # 1) sizeBytes : borne de sécurité + omission si inconnu/aberrant
    sb = pkg.get("sizeBytes")
    if sb is not None:
        if not isinstance(sb, (int, float)) or isinstance(sb, bool) or sb <= 0 or sb > MAX_SANE_BYTES:
            pkg.pop("sizeBytes", None)
            stats["size_dropped"] += 1
        else:
            pkg["sizeBytes"] = int(sb)

    # 2) fileFormat canonique
    ff = pkg.get("fileFormat")
    if ff:
        norm = normalize_formats(ff)
        if norm:
            pkg["fileFormat"] = norm

    # 3) Surfaçage du format (idempotent)
    label = display_label(pkg.get("fileFormat"))
    desc = pkg.get("description") or ""
    desc_lines = [l for l in desc.split("\n") if not l.startswith("Format:")]
    desc_body = "\n".join(desc_lines).lstrip("\n")
    if label:
        pkg["formatLabel"] = label
        pkg["description"] = f"Format: {label}" + (f"\n{desc_body}" if desc_body else "")
    else:
        pkg.pop("formatLabel", None)
        pkg["description"] = desc_body

    # 3bis) CARTE du jeu : l'app affiche le champ `source`. On y met le/les
    # FORMAT(s) du jeu (exFAT/PKG/Backport/APR-EMU…) — pas la vraie provenance,
    # qui reste masquée. La SOURCE PAGE (downloadSource) garde la marque.
    ff = pkg.get("fileFormat")
    fmt = pkg.get("formatLabel") or (" · ".join(ff) if isinstance(ff, list) and ff else "")
    pkg["source"] = [fmt] if fmt else ["PS5"]
    pkg["downloadSource"] = BRAND_SOURCE_URL

    # 3ter) Format affiché À CÔTÉ de chaque hébergeur (repérage quand un jeu a
    # beaucoup de liens) : format (+ backport, déjà dans formatLabel) + version
    # si connue. Idempotent : retire un éventuel « [..] » terminal avant de
    # réappliquer (sinon la fusion ferait s'accumuler les suffixes).
    version = (pkg.get("version") or "").strip()
    vsuf = f" · v{version}" if version else ""
    base_fmt = _base_format(pkg.get("fileFormat"))
    for link in pkg.get("downloadLinks") or []:
        if not (isinstance(link, dict) and link.get("name")):
            continue
        base = re.sub(r"\s*\[[^\]]*\]\s*$", "", link["name"]).rstrip()
        link_fmt = _link_format(base, link.get("url", ""), fmt, base_fmt, link.get("group", ""))
        link["name"] = f"{base} [{link_fmt}{vsuf}]" if (link_fmt or version) else base

    # 4) Validation Pegasus
    if not (pkg.get("titleId") or "").strip():
        stats["missing_titleId"] += 1
    if not (pkg.get("title") or "").strip():
        stats["missing_title"] += 1
    n_links = _clean_links(pkg)
    if n_links == 0:
        stats["no_valid_links"] += 1
    tid = (pkg.get("titleId") or "").strip().upper()
    if tid and not REAL_TITLEID_RE.match(tid):
        stats["placeholder_titleId"] += 1


def finalize_catalog(catalog: dict) -> dict:
    stats = {
        "total": 0, "size_dropped": 0, "missing_titleId": 0, "missing_title": 0,
        "no_valid_links": 0, "placeholder_titleId": 0, "with_size": 0,
        "with_formatLabel": 0,
    }
    # Nom du catalogue rebrandé (sinon « SuperPSX PS5 » / « exFAT PS5 » fuite
    # la source, y compris dans l'en-tête de la liste de jeux générée ensuite).
    catalog["name"] = f"{BRAND} PS5"
    packages = catalog.get("packages", [])
    stats["total"] = len(packages)
    for pkg in packages:
        finalize_package(pkg, stats)
        if pkg.get("sizeBytes"):
            stats["with_size"] += 1
        if pkg.get("formatLabel"):
            stats["with_formatLabel"] += 1
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("catalog", type=Path)
    ap.add_argument("--out", type=Path, default=None, help="Sortie (défaut: sur place)")
    ap.add_argument("--strict", action="store_true",
                    help="Sort != 0 si des jeux n'ont aucun lien valide.")
    args = ap.parse_args(argv)

    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    if "packages" not in catalog:
        print("Fichier invalide : clé 'packages' absente.", file=sys.stderr)
        return 1

    stats = finalize_catalog(catalog)
    out = args.out or args.catalog
    out.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"Finalisation : {stats['total']} jeux | {stats['with_size']} avec taille | "
        f"{stats['with_formatLabel']} avec formatLabel | "
        f"{stats['size_dropped']} tailles aberrantes retirées | "
        f"{stats['no_valid_links']} sans lien valide | "
        f"{stats['placeholder_titleId']} titleId placeholder | "
        f"{stats['missing_title']} sans titre"
    )
    if args.strict and stats["no_valid_links"] > 0:
        print("::error::Des jeux n'ont aucun lien de téléchargement valide.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
