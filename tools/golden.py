#!/usr/bin/env python3
"""tools/golden.py — tests de non-régression « golden » : la sortie Markdown (et les champs
stables du rapport) d'un PDF est comparée à une version attendue, relue et validée.

    python tools/golden.py check  [--only nom] [--synthetiques | --locaux]
    python tools/golden.py update [--only nom] [--synthetiques | --locaux]

Deux jeux :

1. **Synthétiques (versionnés)** : PDF générés par tests/golden_gen.py, convertis sans
   modèle (`--no-vlm`, Granite coupé). Attendus : tests/golden/<nom>.md et
   tests/golden/<nom>.report.json (champs stables seulement).
2. **Locaux (non versionnés)** : dossier `PDF2LLM_GOLDEN_DIR` (défaut :
   tests/fixtures/golden/) de paires `x.pdf` + `x.attendu.md` (+ `x.attendu.report.json`),
   avec un fichier facultatif `x.args` contenant les options de la ligne de commande
   (ex. `--pages 10-25 --vlm-model qwen2.5-vl-7b-instruct`). Sans `x.args` : `--no-vlm`.
   Les sorties vont dans `<dossier>/_sortie/`, qui garde le cache du modèle vision : une
   relance relit les mêmes réponses, la comparaison reste stable.

`check` affiche un diff unifié par fichier et sort avec le code 1 à la moindre différence.
`update` réécrit les fichiers attendus et liste ce qui a changé : à ne lancer qu'après
avoir relu le diff de `check`.
"""
from __future__ import annotations

import argparse
import difflib
import json
import logging
import os
import shlex
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

SYN_DIR = REPO / "tests" / "golden"
DEFAULT_LOCAL = REPO / "tests" / "fixtures" / "golden"
# Champs du rapport qui ne dépendent ni de la machine ni du temps (pas de durées ni de tailles)
STABLE = ("kind", "actions", "granite_pages", "vector_tables", "warnings")


@dataclass
class Case:
    name: str
    pdf: Path
    md: Path                       # Markdown attendu
    report: Path                   # champs stables attendus du rapport
    argv: list[str] = field(default_factory=list)
    synthetic: bool = False
    out_dir: Path | None = None


def local_dir() -> Path:
    return Path(os.environ.get("PDF2LLM_GOLDEN_DIR") or DEFAULT_LOCAL)


def synthetic_cases(work: Path) -> list[Case]:
    import golden_gen
    out = []
    for pdf in golden_gen.generate(work / "pdfs"):
        out.append(Case(pdf.stem, pdf, SYN_DIR / f"{pdf.stem}.md",
                        SYN_DIR / f"{pdf.stem}.report.json", ["--no-vlm"], True,
                        work / "sortie"))
    return out


def local_cases(folder: Path | None = None, only_baselined: bool = False) -> list[Case]:
    folder = folder or local_dir()
    if not folder.is_dir():
        return []
    out = []
    for pdf in sorted(folder.glob("*.pdf")):
        md = pdf.with_suffix(".attendu.md")
        if only_baselined and not md.exists():
            continue
        args_file = pdf.with_suffix(".args")
        argv = ["--no-vlm"]
        if args_file.exists():
            lines = [l for l in args_file.read_text("utf-8").splitlines()
                     if l.strip() and not l.lstrip().startswith("#")]
            argv = shlex.split(" ".join(lines))
        out.append(Case(pdf.stem, pdf, md, pdf.with_suffix(".attendu.report.json"), argv,
                        False, folder / "_sortie"))
    return out


_POOLS: dict[tuple, object] = {}


def convert(case: Case) -> tuple[str, dict]:
    """Convertit le PDF du cas comme la ligne de commande ; renvoie (Markdown, rapport)."""
    import pdf2llm as P
    out_dir = case.out_dir or Path(tempfile.mkdtemp(prefix="pdf2llm_golden_"))
    out_dir.mkdir(parents=True, exist_ok=True)
    args = P.build_parser().parse_args([str(case.pdf), str(out_dir), *case.argv, "--force"])
    if case.synthetic:
        args.charts = "aucun"        # interne : jamais de Granite pour les synthétiques
    vlm = None if args.no_vlm else P.VlmClient(
        args.vlm_url, args.vlm_model, args.vlm_api_key, args.vlm_timeout, args.max_side,
        out_dir / ".vlm_cache.json", args.vlm_max_tokens)
    if args.charts == "docling":
        P.install_granite_csv_patch()
    key = (args.no_vlm, args.no_table_fallback, args.no_ocr, args.no_cell_matching,
           args.images_scale, args.charts, args.chart_api_url, args.mlx)
    pool = _POOLS.setdefault(key, P.ConverterPool(args))
    rep = P.process_pdf(case.pdf, out_dir, pool, vlm, args)
    md = (out_dir / f"{case.pdf.stem}.md").read_text("utf-8")
    return md, stable_report(P.asdict(rep))


def stable_report(rep: dict) -> dict:
    return {k: rep.get(k) for k in STABLE}


def _dump(d: dict) -> str:
    return json.dumps(d, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def compare(case: Case, md: str, rep: dict) -> list[str]:
    """Diffs unifiés (liste vide si tout est identique)."""
    diffs = []
    for path, new in ((case.md, md), (case.report, _dump(rep))):
        old = path.read_text("utf-8") if path.exists() else ""
        if old != new:
            diffs.append("".join(difflib.unified_diff(
                old.splitlines(True), new.splitlines(True),
                fromfile=f"{path.name} (attendu)", tofile=f"{path.name} (obtenu)")))
    return diffs


def check_cases(cases: list[Case]) -> dict[str, list[str]]:
    """{nom: diffs} pour les cas qui diffèrent."""
    out = {}
    for case in cases:
        md, rep = convert(case)
        d = compare(case, md, rep)
        if d:
            out[case.name] = d
    return out


def update_cases(cases: list[Case]) -> list[str]:
    changed = []
    for case in cases:
        md, rep = convert(case)
        for path, new in ((case.md, md), (case.report, _dump(rep))):
            old = path.read_text("utf-8") if path.exists() else None
            if old != new:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(new, "utf-8")
                changed.append(f"{path} ({'créé' if old is None else 'modifié'})")
    return changed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["check", "update"])
    ap.add_argument("--only", default=None, help="Seulement le cas de ce nom (sans extension)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--synthetiques", action="store_true", help="Seulement les PDF synthétiques")
    g.add_argument("--locaux", action="store_true", help="Seulement le dossier local")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("docling", "httpx", "urllib3", "PIL", "transformers", "RapidOCR"):
        logging.getLogger(noisy).setLevel(logging.ERROR)

    work = Path(tempfile.mkdtemp(prefix="pdf2llm_golden_"))
    cases: list[Case] = []
    if not a.locaux:
        cases += synthetic_cases(work)
    if not a.synthetiques:
        cases += local_cases(only_baselined=a.action == "check")
    if a.only:
        cases = [c for c in cases if c.name == a.only]
    if not cases:
        print("Aucun cas à traiter.")
        return 1 if a.only else 0

    if a.action == "update":
        changed = update_cases(cases)
        print("\n".join(changed) if changed else "Rien n'a changé.")
        return 0
    bad = check_cases(cases)
    for name, diffs in bad.items():
        print(f"=== {name}")
        for d in diffs:
            print(d)
    print(f"{len(cases) - len(bad)}/{len(cases)} identiques"
          + (" ; différents : " + ", ".join(bad) if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
