"""Tests de non-régression de pdf2llm.py.

Lancement :   python test_pdf2llm.py        (ou : python -m pytest test_pdf2llm.py)

- Les tests « unitaires » tournent sur des données factices écrites ici : aucun fichier requis.
- Les tests « fixture » rejouent la détection de texte sur un vrai DoclingDocument exporté
  par `--dump-debug` (fichier `*.docling.json`). Ils sont sautés si le fichier est absent.
  Chemin par défaut : tests/fixtures/exemple.p1-15.docling.json (non versionné), ou
  variable d'environnement PDF2LLM_FIXTURE_JSON. Les indices d'images (14, 15, 17) et les
  valeurs attendues correspondent au PDF d'étude utilisé pendant le développement
  (sondage Ifop de 21 pages, slide 9 = anneau + barres par âge).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# Docling est lourd à importer (torch) ; s'il est absent, on le remplace par des simulacres :
# les fonctions testées ici n'en dépendent pas.
try:
    import docling  # noqa: F401
except ImportError:  # pragma: no cover
    for m in ["docling", "docling.datamodel", "docling.datamodel.base_models",
              "docling.datamodel.pipeline_options", "docling.document_converter",
              "docling_core", "docling_core.transforms", "docling_core.transforms.serializer",
              "docling_core.transforms.serializer.base",
              "docling_core.transforms.serializer.common",
              "docling_core.transforms.serializer.markdown", "docling_core.types",
              "docling_core.types.doc"]:
        sys.modules[m] = MagicMock()

import pdf2llm as p  # noqa: E402

FIXTURE = Path(os.environ.get("PDF2LLM_FIXTURE_JSON",
                              HERE / "tests" / "fixtures" / "exemple.p1-15.docling.json"))


# --------------------------------------------------------------------------------------
# Tests unitaires (données factices)
# --------------------------------------------------------------------------------------
def test_p1_csv_libelles_avec_virgule():
    """Granite n'entoure pas de guillemets les libellés contenant une virgule."""
    raw = ("```csv\nLibellé,Valeur\nOui, et vous voyez précisément de qui il s'agit,23%\n"
           "Oui, mais pas précisément,20%\nNon,57%\n```")
    df = p.tolerant_csv_to_dataframe(raw)
    assert df.shape == (4, 2), df
    assert df.iloc[1, 0] == "Oui, et vous voyez précisément de qui il s'agit"
    assert all(p.is_num(v) for v in df[1].iloc[1:])


def test_p3_tableau_granite_suspect():
    bad = [["Category", "Percentage"], ["Total A", "13"], ["Ensemble", "6"],
           ["", "53"], ["", "11"], ["", "2"]]
    assert p.chart_rows_problem(bad) == "libellé vide"
    good = [["Category", "Percentage"], ["Groupe 1", "51"], ["Groupe 2", "29"]]
    assert p.chart_rows_problem(good) is None
    assert p.rows_to_md(good, "T").startswith("**T**\n\n| Libellé | % |")


def test_p4_mermaid_inutile():
    m1 = "```mermaid\nclassDiagram\n    class Très proche {}\n    class Plutôt proche {}\n```"
    m2 = "```mermaid\nflowchart TD\n    A[18-24 ans] --> B[31%]\n    C[25-34 ans] --> D[25%]\n```"
    m3 = "```mermaid\nflowchart TD\n    A[Début] --> B[Valider]\n    B --> C[Fin]\n```"
    m4 = "```mermaid\nclassDiagram\n  Animal <|-- Chien\n  Animal : +int age\n```"
    assert p.vlm_output_problem(m1) and p.vlm_output_problem(m2)
    assert p.vlm_output_problem(m3) is None and p.vlm_output_problem(m4) is None


def test_p2_tableau_seul():
    t = ("**Notoriété**\n\n| Réponse | % |\n|---|---|\n| Oui | 23% |\n| Non | 57% |\n\n"
         "La majorité des répondants (57%) ne voient pas...")
    out = p.keep_table_only(t)
    assert out.startswith("**Notoriété**") and "majorité" not in out


def test_p7_bruit_des_slides():
    md = "## Titre\n\nifop\n\nTexte A\n\n12\n\nifop\n\nTexte B\n\nifop\n\nifap\n"
    out, n = p.denoise_markdown(md)
    assert "ifop" not in out and "ifap" not in out and "\n12\n" not in out, out
    assert "Texte A" in out and "Texte B" in out and n == 5


def test_v3_tableaux_fusionnes_et_valeurs_inventees():
    merged = [["Party", "Proximity", "Vote"],
              ["Groupe A", "54%", ""], ["Groupe B", "64%", ""], ["Groupe C", "40%", ""],
              ["Candidat 1", "", "58%"], ["Candidat 2", "", "45%"], ["Candidat 3", "", "30%"]]
    tabs = p.split_sparse(merged)
    assert tabs and len(tabs) == 2 and len(tabs[1]) == 4, tabs
    assert all(p.chart_rows_problem(t) is None for t in tabs)
    assert p.split_sparse([["A", "B", "C"], ["", "1", ""]]) is None
    nums = {"2", "11", "34", "53", "13", "6", "31", "25", "15", "9", "3"}
    legend = ("| Groupe | Valeur |\n|---|---|\n| Très proche | 0% |\n| Plutôt proche | 25% |\n"
              "| Plutôt éloigné | 50% |\n| Très éloigné | 75% |")
    assert p.invented_values(legend, nums)
    ok = "| Age | % |\n|---|---|\n| 18-24 ans | 31% |\n| 25-34 ans | 25% |\n| 65 ans et plus | 3% |"
    assert p.invented_values(ok, nums) is None


# Grille BRUTE renvoyée par Docling pour le tableau « marges d'erreur » (en-tête sur 3 lignes,
# taille d'échantillon passée en dernière colonne, « 3 000 0,8 » fusionnés). C'est ce que
# reçoit decide_tables ; le premier test v5 ne voyait que l'en-tête déjà aplati du Markdown.
_T = "INTERVALLE DE CONFIANCE A 95% DE CHANCES"
_PCT = ["5 ou 95%", "10 ou 90%", "20 ou 80%", "30 ou 70%", "40 ou 60%", "50%"]
RAW_MARGIN_GRID = [
    [_T] * 7,
    ["Taille d'échantillon"] + ["Pourcentage observé"] * 6,
    [""] + _PCT,
    ["1,9", "2,7", "3,6", "4,1", "4,4", "4,5", "500"],
    ["1,8", "2,4", "3,3", "3,7", "4,0", "4,1", "600"],
    ["1,0", "1,3", "1,8", "2,0", "2,2", "2,2", "2 000"],
    ["3 000 0,8", "1,1", "1,4", "1,6", "1,8", "1,8", ""],
]


def test_v5_tableau_decale_grille_brute():
    fx = p.fixed_table_md(RAW_MARGIN_GRID)
    assert fx, "motif non reconnu"
    lines = fx.splitlines()
    assert lines[0] == f"**{_T}**"
    assert lines[2].startswith("| Taille d'échantillon | Pourcentage observé - 5 ou 95% |")
    assert lines[4].startswith("| 500 | 1,9 | 2,7")
    assert "| 3 000 | 0,8 | 1,1 | 1,4 | 1,6 | 1,8 | 1,8 |" in fx
    # Même tableau avec l'en-tête déjà aplati (ancien format) : même résultat
    flat = [[f"{_T} - Taille d'échantillon"] + [f"{_T} - Pourcentage observé - {x}" for x in _PCT]]
    assert p.fixed_table_md(flat + [["-"] * 7] + RAW_MARGIN_GRID[3:]) == fx
    assert p.fixed_table_md([["Libellé", "%"], ["a", "1"], ["b", "2"]]) is None


def test_v5_1_logo_dans_les_titres_repris():
    md = "ifop\n\nifop\n\nifop\n\nOifop\n\n*Oifop*\n\n*Statut ; Oifop*\n\n*Données lues :*"
    out, _ = p.denoise_markdown(md)
    assert "Oifop" not in out and "*Statut*" in out and "*Données lues :*" in out, out


def test_v5_1_avertissement_tableau_corrige():
    it = NS(self_ref="#/tables/0", prov=[NS(page_no=4)])
    orig = p.irregular_tables
    p.irregular_tables = lambda doc: [it]
    try:
        assert len(p.table_warnings(None)) == 1
        assert p.table_warnings(None, {"#/tables/0"}) == []
    finally:
        p.irregular_tables = orig


# --------------------------------------------------------------------------------------
# Tests sur un vrai export Docling (sautés si la fixture est absente)
# --------------------------------------------------------------------------------------
def _fixture_doc():
    dj = json.loads(FIXTURE.read_text("utf-8"))

    def bb(b):
        return NS(l=b["l"], t=b["t"], r=b["r"], b=b["b"])

    doc = NS(texts=[NS(text=t["text"], prov=[NS(page_no=t["prov"][0]["page_no"],
                                                 bbox=bb(t["prov"][0]["bbox"]))])
                    for t in dj["texts"] if t.get("prov")])

    def pic(i):
        pr = dj["pictures"][i]["prov"][0]
        return NS(prov=[NS(page_no=pr["page_no"], bbox=bb(pr["bbox"]))])
    return doc, pic


def test_fixture_texte_avale_par_les_images():
    if not FIXTURE.exists():
        return "sauté"
    doc, pic = _fixture_doc()
    ring = p.texts_near(doc, pic(14), 80)
    assert {"2% 11%", "34%", "53%", "Très proche", "Très éloigné"} <= set(ring), ring
    assert "31%" not in ring                       # la barre par âge est hors cadre
    inside = p.texts_near(doc, pic(14), 0)
    out = "**Total Proche : 13%**\n| Groupe | Valeur |\n|---|---|\n| Total Proche | 13% |"
    assert "34%" in p.uncovered_texts(inside, out)
    assert not p.uncovered_texts(p.texts_near(doc, pic(15), 0), "")


def test_fixture_anneau_sans_association_inventee():
    if not FIXTURE.exists():
        return "sauté"
    doc, pic = _fixture_doc()
    blk = p.pie_text_block(doc, pic(14))
    assert "Légende : Très proche ; Plutôt proche ; Plutôt éloigné ; Très éloigné" in blk
    assert "2% ; 11% ; 34% ; 53% (total 100%)" in blk
    assert "Ensemble des Français : 6%" in blk and "Total Proche : 13%" in blk
    t17 = p.uncovered_titles(p.texts_near(doc, pic(17), 0),
                             "| 18-24 ans | 31% |\n| 25-34 ans | 25% |\n| 65 ans et plus | 3% |")
    assert any("Qui se dit proche" in x for x in t17) and not any("ans" in x for x in t17)


if __name__ == "__main__":
    failed = 0
    for name, fn in [(k, v) for k, v in globals().items() if k.startswith("test_")]:
        try:
            res = fn()
            print(f"{'SKIP' if res == 'sauté' else 'ok  '}  {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {name} : {type(e).__name__}: {e}")
    if not FIXTURE.exists():
        print(f"(fixture absente : {FIXTURE} ; voir l'en-tête du fichier)")
    sys.exit(1 if failed else 0)
