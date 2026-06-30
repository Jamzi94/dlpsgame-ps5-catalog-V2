#!/usr/bin/env python3
"""
Fusionne plusieurs catalogues au format Pegasus DL en un seul.

Usage :
  python merge_catalogs.py sortie.json catalogue_a.json catalogue_b.json [...]
  python merge_catalogs.py sortie.json FRAÎCHE1.json [FRAÎCHE2…] EXISTANT.json

  Convention CI : sources fraîches d'abord, catalogue existant en dernier.

Garde-fou anti-corruption (item 6) :
  - Chaque source FRAÎCHE doit exposer 'packages' comme une LISTE (sinon rejet).
  - Seuil : si total(packages frais) < --min-fresh-ratio × taille(existant), la
    fusion est REFUSÉE (rc=3) et l'existant conservé intact. Bypass : --allow-shrink
    (tolère, avertit) ou --full (désactive le seuil).

Règles de fusion :
  - Clé d'unicité : titleId réel (ex. PPSA01668) si présent, sinon le titre
    normalisé. Les titleId-placeholder "GAME_xxxxx" ne servent PAS de clé
    (ils ne sont pas stables d'un run à l'autre).
  - Doublons : on fusionne les downloadLinks (dédoublonnés par URL) et on
    garde, champ par champ, la métadonnée la plus riche (description la plus
    longue, posterUrl/sizeBytes/version non vides en priorité).
  - L'ordre des fichiers donne la priorité de base : le premier catalogue
    fournit la version "canonique", les suivants complètent.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

LOG = logging.getLogger("merge_catalogs")

# Un vrai identifiant ressemble à PPSA01668 / CUSA12345 (4 lettres + chiffres).
REAL_TITLEID_RE = re.compile(r"^[A-Z]{4}\d{3,}$")


def merge_key(pkg: dict) -> str:
    """Clé d'unicité d'un package."""
    tid = (pkg.get("titleId") or "").strip().upper()
    if REAL_TITLEID_RE.match(tid):
        return f"id:{tid}"
    # sinon : titre normalisé (minuscules, sans ponctuation/espaces multiples)
    title = (pkg.get("title") or "").strip().lower()
    title = re.sub(r"[^a-z0-9]+", " ", title).strip()
    return f"title:{title}"


def merge_links(existing: list[dict], incoming: list[dict]) -> list[dict]:
    """Union des downloadLinks, dédoublonnée par URL (ordre préservé).

    Pour un URL présent des DEUX côtés, on COMPLÈTE les champs manquants (ex.
    'group' = section/format capté lors d'un scrape no_cache) au lieu de garder
    bêtement la 1re version : sinon un run incrémental (contenu en cache, sans
    'group') écrasait le 'group' acquis -> le format par lien régressait."""
    out: list[dict] = []
    pos: dict[str, int] = {}
    for link in list(existing) + list(incoming):
        if not isinstance(link, dict):
            continue
        u = (link.get("url") or "").strip()
        if not u:
            continue
        if u in pos:
            cur = out[pos[u]]
            for k, v in link.items():
                if v and not cur.get(k):
                    cur[k] = v
        else:
            pos[u] = len(out)
            out.append(dict(link))
    return out


def richer(a, b):
    """Retourne la valeur 'la plus riche' entre a (existant) et b (entrant)."""
    # chaînes : on garde la plus longue non vide
    if isinstance(a, str) or isinstance(b, str):
        a = a or ""
        b = b or ""
        return a if len(a) >= len(b) else b
    # nombres : on garde le plus grand non nul (taille, etc.)
    if isinstance(a, (int, float)) or isinstance(b, (int, float)):
        return a if (a or 0) >= (b or 0) else b
    return a if a else b


def union_list(a, b) -> list:
    """Union de deux valeurs liste-ou-scalaire, dédoublonnée, ordre préservé."""
    out: list = []
    for val in (a, b):
        if val is None:
            continue
        items = val if isinstance(val, list) else [val]
        for it in items:
            if it not in out:
                out.append(it)
    return out


# Champs dont on veut conserver la version la plus complète.
# (posterUrl est traité à part : on préfère une couverture enrichie RAWG.)
ENRICHABLE_FIELDS = ("version", "description", "sizeBytes", "downloadSource", "category")
# Champs liste qu'on unionne (provenance, formats) pour ne rien perdre.
UNION_FIELDS = ("source", "fileFormat")
# Champs d'enrichissement à préserver tels quels (cache RAWG/IGDB + métadonnées).
# Indispensable : sans ça, la fusion effacerait _enrichedAt et RAWG/IGDB seraient
# ré-interrogés à chaque run, dépassant le quota.
PRESERVE_FIELDS = (
    "_enrichedAt", "_rawgMatched", "metadata",
    "_igdbEnrichedAt", "_igdbMatched",
)


def _cover_rank(pkg: dict) -> int:
    """Qualité de la posterUrl d'un package (plus haut = meilleure jaquette).

    3 = jaquette IGDB (vraie cover portrait)
    2 = cover scrapée du site (og:image) ou autre source réelle
    1 = image RAWG (rawg.io : screenshot/hero paysage, PAS une jaquette)
    0 = pas de poster
    """
    url = (pkg.get("posterUrl") or "").lower()
    if not url:
        return 0
    if "images.igdb.com" in url or pkg.get("_igdbMatched") is True:
        return 3
    if "rawg.io" in url:
        return 1
    return 2


def merge_package(base: dict, extra: dict) -> dict:
    """Fusionne deux packages représentant le même jeu."""
    merged = dict(base)
    merged["downloadLinks"] = merge_links(
        base.get("downloadLinks", []), extra.get("downloadLinks", [])
    )
    for field in ENRICHABLE_FIELDS:
        if field in base or field in extra:
            merged[field] = richer(base.get(field), extra.get(field))
    for field in UNION_FIELDS:
        if field in base or field in extra:
            merged[field] = union_list(base.get(field), extra.get(field))

    # posterUrl : on privilégie une VRAIE jaquette. L'image RAWG (rawg.io) est
    # une image paysage (screenshot/hero), pas une cover -> rang le plus bas.
    # Ordre : jaquette IGDB > cover scrapée du site (og:image) > image RAWG.
    # C'est ce qui « ré-guérit » les anciennes posterUrl RAWG : au prochain scrape,
    # la cover fraîche du site (rang 2) l'emporte sur l'image RAWG existante (rang 1).
    rank_base, rank_extra = _cover_rank(base), _cover_rank(extra)
    if rank_extra > rank_base:
        merged["posterUrl"] = extra.get("posterUrl")
    elif rank_base > rank_extra:
        merged["posterUrl"] = base.get("posterUrl")
    elif "posterUrl" in base or "posterUrl" in extra:
        merged["posterUrl"] = base.get("posterUrl") or extra.get("posterUrl")

    # Préservation des champs d'enrichissement (présents côté existant, absents
    # côté frais) : on garde celui qui existe, en privilégiant la base.
    for field in PRESERVE_FIELDS:
        val = base.get(field)
        if val is None:
            val = extra.get(field)
        if val is not None:
            merged[field] = val

    # titleId : on préfère un identifiant réel à un placeholder GAME_xxxxx
    for cand in (base.get("titleId"), extra.get("titleId")):
        if cand and REAL_TITLEID_RE.match(cand.strip().upper()):
            merged["titleId"] = cand
            break

    # sizeBytes est OPTIONNEL côté Pegasus DL (seuls titleId/title/
    # downloadLinks[].url sont requis). On omet simplement le champ quand la
    # taille est inconnue : un null/0 n'apporte rien et salit le catalogue.
    if not merged.get("sizeBytes"):
        merged.pop("sizeBytes", None)
    return merged


def load_catalog(path: Path, *, fresh: bool = False) -> dict:
    """Charge un catalogue et valide sa structure.

    Garde-fou (item 6) : une source FRAÎCHE doit impérativement exposer une clé
    'packages' qui est une LISTE. Un scrape partiel ou corrompu (objet, null,
    clé absente) est rejeté avec un message clair plutôt que de produire une
    fusion silencieusement dégradée.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "packages" not in data:
        kind = "source fraîche" if fresh else "catalogue"
        raise ValueError(
            f"{path} : {kind} invalide — clé 'packages' absente "
            "(pas un catalogue Pegasus DL valide)."
        )
    if not isinstance(data["packages"], list):
        kind = "source fraîche" if fresh else "catalogue"
        got = type(data["packages"]).__name__
        raise ValueError(
            f"{path} : {kind} invalide — 'packages' doit être une LISTE "
            f"(reçu : {got}). Scrape partiel ou fichier corrompu, rejeté."
        )
    return data


# Code de sortie dédié quand le merge est REFUSÉ pour cause de seuil : permet au
# workflow CI de conditionner le « Commit & push » (rc==0 -> commit, rc==3 -> skip).
RC_OK = 0
RC_USAGE = 2
RC_REFUSED = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fusionne plusieurs catalogues Pegasus DL en un seul.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("output", help="Fichier de sortie (catalogue fusionné).")
    parser.add_argument(
        "inputs", nargs="+",
        help="Catalogues d'entrée : sources fraîches d'abord, catalogue existant "
             "ensuite (le dernier si plusieurs entrées, sauf --existing).",
    )
    parser.add_argument(
        "--existing", default=None,
        help="Chemin explicite du catalogue EXISTANT (référence du seuil). "
             "Par défaut : la dernière entrée quand il y en a plusieurs.",
    )
    parser.add_argument(
        "--min-fresh-ratio", type=float, default=0.5,
        help="Seuil anti-run-dégradé : refuse la fusion si le total des packages "
             "frais < ratio × taille du catalogue existant (défaut : 0.5).",
    )
    parser.add_argument(
        "--allow-shrink", action="store_true",
        help="Bypass du seuil : autorise un total frais inférieur (catalogue qui "
             "rétrécit légitimement). N'AVERTIT plus, fusionne quand même.",
    )
    parser.add_argument(
        "--full", action="store_true",
        help="Mode full explicite : désactive entièrement le seuil anti-dégradé "
             "(scrape complet attendu, pas d'incrémental).",
    )
    return parser


def main(argv: list[str]) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    )
    # Rétro-compat : ancien appel positionnel « out a.json b.json … » conservé,
    # mais on exige au moins une entrée. (anciennement : argv < 3 -> usage.)
    if len(argv) < 3:
        build_parser().print_help()
        return RC_USAGE
    args = build_parser().parse_args(argv[1:])

    out_path = Path(args.output)
    in_paths = [Path(p) for p in args.inputs]

    # Détermination de la source EXISTANTE (référence du seuil) vs les FRAÎCHES.
    # Convention CI : « out FRESH… EXISTING » -> l'existant est la dernière entrée.
    existing_path: Path | None = None
    if args.existing:
        existing_path = Path(args.existing)
        fresh_paths = [p for p in in_paths if p != existing_path]
        if existing_path not in in_paths:
            # --existing peut désigner un fichier hors liste positionnelle :
            # on l'ajoute en queue (fusionné en dernier, comme l'existant).
            fresh_paths = list(in_paths)
    elif len(in_paths) > 1:
        existing_path = in_paths[-1]
        fresh_paths = in_paths[:-1]
    else:
        # Premier run : une seule entrée, tout est « frais », pas de seuil.
        fresh_paths = list(in_paths)

    # 1) Validation stricte des sources FRAÎCHES (packages == LISTE, sinon rejet).
    # On capture les erreurs de structure pour émettre un message propre (sans
    # traceback) et un code de sortie contrôlé plutôt qu'un crash.
    fresh_catalogs: list[tuple[Path, dict]] = []
    fresh_total = 0
    try:
        for path in fresh_paths:
            cat = load_catalog(path, fresh=True)
            fresh_catalogs.append((path, cat))
            fresh_total += len(cat["packages"])

        existing_cat: dict | None = None
        if existing_path is not None:
            existing_cat = load_catalog(existing_path, fresh=False)
    except (ValueError, json.JSONDecodeError) as exc:
        LOG.error("MERGE REFUSÉ : %s", exc)
        return RC_USAGE

    # 2) Seuil anti-corruption : on compare le total frais à la taille de l'existant.
    if existing_cat is not None and existing_path is not None:
        existing_total = len(existing_cat["packages"])
        threshold = args.min_fresh_ratio * existing_total

        if args.full:
            LOG.info(
                "Mode --full : seuil anti-dégradé désactivé "
                "(frais=%d, existant=%d).", fresh_total, existing_total,
            )
        elif existing_total > 0 and fresh_total < threshold:
            if args.allow_shrink:
                LOG.warning(
                    "Run dégradé toléré (--allow-shrink) : %d packages frais "
                    "< %.0f%% × %d existants. Fusion poursuivie.",
                    fresh_total, args.min_fresh_ratio * 100, existing_total,
                )
            else:
                LOG.error(
                    "MERGE REFUSÉ : %d packages frais < %.0f%% × %d existants "
                    "(seuil=%.0f). Scrape probablement partiel (timeout Cloudflare ?). "
                    "Catalogue existant CONSERVÉ intact. "
                    "Utilisez --allow-shrink ou --full pour forcer.",
                    fresh_total, args.min_fresh_ratio * 100, existing_total,
                    threshold,
                )
                return RC_REFUSED

    by_key: dict[str, dict] = {}
    order: list[str] = []
    stats = []
    catalog_name = "Catalogue fusionné"

    # Ordre de fusion : sources fraîches d'abord (canonique/à jour), existant
    # ensuite (préserve jeux disparus, miroirs, enrichissement RAWG/IGDB).
    merge_inputs: list[tuple[Path, dict]] = list(fresh_catalogs)
    if existing_cat is not None and existing_path is not None:
        merge_inputs.append((existing_path, existing_cat))

    for idx, (path, cat) in enumerate(merge_inputs):
        if idx == 0 and cat.get("name"):
            catalog_name = cat["name"]
        added = updated = 0
        for pkg in cat["packages"]:
            key = merge_key(pkg)
            if key in by_key:
                by_key[key] = merge_package(by_key[key], pkg)
                updated += 1
            else:
                by_key[key] = pkg
                order.append(key)
                added += 1
        stats.append((path.name, len(cat["packages"]), added, updated))

    packages = [by_key[k] for k in order]
    # On n'émet jamais sizeBytes null/0 (inutile) ; le champ est optionnel pour
    # Pegasus DL et son absence n'empêche pas l'affichage du jeu.
    for pkg in packages:
        if not pkg.get("sizeBytes"):
            pkg.pop("sizeBytes", None)
    packages.sort(key=lambda p: (p.get("title") or "").lower())

    result = {
        "name": catalog_name,
        "version": 1,
        "packages": packages,
    }
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print("Fusion terminée :")
    for name, total, added, updated in stats:
        print(f"  - {name:32s} {total:5d} jeux  (+{added} nouveaux, {updated} fusionnés)")
    print(f"  => {out_path} : {len(packages)} jeux uniques")
    return RC_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv))
