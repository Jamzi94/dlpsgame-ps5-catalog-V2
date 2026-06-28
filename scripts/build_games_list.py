#!/usr/bin/env python3
"""Génère une liste lisible de TOUS les jeux du catalogue, avec leur format.

Entrée : le catalogue mergé (``dlpsgame-ps5.json``).
Sorties :
  - ``games-list.md``  : index Markdown trié (titre, format, version, sources)
                         + une répartition par format en tête.
  - ``games-list.csv`` : même contenu, exploitable (Excel / Google Sheets).

Le catalogue est cumulatif : le merge est non destructif et la mémoire
incrémentale empêche de re-scraper l'existant. Régénérer cette liste à chaque
run l'enrichit donc automatiquement des nouveaux jeux, sans jamais perdre les
anciens. Le script est idempotent : à catalogue identique, sortie identique.

Usage :
    python3 scripts/build_games_list.py [catalogue.json]
        [--md games-list.md] [--csv games-list.csv]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def _as_list(value) -> list:
    """Normalise une valeur (None / str / list) en liste de chaînes propres."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            if item is None:
                continue
            text = str(item).strip()
            if text:
                out.append(text)
        return out
    return [str(value)]


def _format_of(pkg: dict) -> str:
    """Format principal d'un paquet (formatLabel, repli sur fileFormat)."""
    label = (pkg.get("formatLabel") or "").strip()
    if label:
        return label
    formats = _as_list(pkg.get("fileFormat"))
    return formats[0] if formats else "—"


def _row(pkg: dict) -> dict:
    """Extrait les champs affichables d'un paquet (robuste aux clés manquantes)."""
    title = (pkg.get("title") or pkg.get("name") or "").strip() or "(sans titre)"
    file_formats = _as_list(pkg.get("fileFormat"))
    sources = _as_list(pkg.get("source"))
    return {
        "title": title,
        "title_id": (pkg.get("titleId") or "").strip(),
        "format": _format_of(pkg),
        "file_formats": ", ".join(file_formats),
        "version": (pkg.get("version") or "").strip(),
        "sources": ", ".join(sources),
        "download_count": len(_as_list(pkg.get("downloadLinks") and
                                       [l.get("url") for l in pkg["downloadLinks"]
                                        if isinstance(l, dict)])),
        "download_source": (pkg.get("downloadSource") or "").strip(),
    }


def _md_escape(text: str) -> str:
    """Échappe le pipe pour ne pas casser les colonnes Markdown."""
    return text.replace("|", "\\|")


def build(catalog_path: Path, md_path: Path, csv_path: Path) -> int:
    data = json.loads(catalog_path.read_text(encoding="utf-8"))
    packages = data.get("packages", data if isinstance(data, list) else [])
    catalog_name = data.get("name", "Catalogue PS5") if isinstance(data, dict) else "Catalogue PS5"

    rows = [_row(p) for p in packages if isinstance(p, dict)]
    rows.sort(key=lambda r: (r["title"].lower(), r["version"]))

    by_format = Counter(r["format"] for r in rows)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # ── Markdown ────────────────────────────────────────────────────────────
    lines: list[str] = []
    lines.append(f"# Liste des jeux PS5 disponibles — {catalog_name}")
    lines.append("")
    lines.append(f"_**{len(rows)}** jeux · généré le {generated} · "
                 "régénéré et enrichi à chaque run_")
    lines.append("")
    lines.append("## Répartition par format")
    lines.append("")
    lines.append("| Format | Jeux |")
    lines.append("| --- | ---: |")
    for fmt, count in sorted(by_format.items(), key=lambda kv: (-kv[1], kv[0].lower())):
        lines.append(f"| {_md_escape(fmt)} | {count} |")
    lines.append("")
    lines.append("## Tous les jeux")
    lines.append("")
    lines.append("| # | Titre | Format | Version | Sources |")
    lines.append("| ---: | --- | --- | --- | --- |")
    for i, r in enumerate(rows, 1):
        lines.append(
            f"| {i} | {_md_escape(r['title'])} | {_md_escape(r['format'])} "
            f"| {_md_escape(r['version']) or '—'} | {_md_escape(r['sources']) or '—'} |"
        )
    lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")

    # ── CSV ─────────────────────────────────────────────────────────────────
    fieldnames = ["title", "title_id", "format", "file_formats",
                  "version", "sources", "download_count", "download_source"]
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"[build_games_list] {len(rows)} jeux → {md_path} + {csv_path}")
    print(f"[build_games_list] formats : " +
          ", ".join(f"{fmt}={n}" for fmt, n in
                    sorted(by_format.items(), key=lambda kv: -kv[1])[:8]))
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", nargs="?", default="dlpsgame-ps5.json",
                        help="catalogue JSON d'entrée (défaut: dlpsgame-ps5.json)")
    parser.add_argument("--md", default="games-list.md",
                        help="sortie Markdown (défaut: games-list.md)")
    parser.add_argument("--csv", default="games-list.csv",
                        help="sortie CSV (défaut: games-list.csv)")
    args = parser.parse_args(argv)

    catalog_path = Path(args.catalog)
    if not catalog_path.is_file():
        print(f"[build_games_list] introuvable: {catalog_path}", file=sys.stderr)
        return 1

    build(catalog_path, Path(args.md), Path(args.csv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
