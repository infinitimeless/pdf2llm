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


def test_v9_t1_unites_de_la_ram_et_chrono():
    # ru_maxrss : octets sous macOS, kilo-octets sous Linux
    assert p.rss_to_mb(512 * 1024 * 1024, "Darwin") == 512
    assert p.rss_to_mb(512 * 1024, "Linux") == 512
    assert p.peak_rss_mb() > 10                  # ce processus occupe plus de 10 Mo
    c = p.Chrono()
    with c("docling"):
        pass
    c.add("vlm", 2.0)
    c.add("vlm", 1.5)
    assert set(p.STAGES) == set(c.durees) and c.durees["vlm"] == 3.5
    assert p.fmt_durees({"docling": 14.2, "vlm": 31, "granite": 0.1}, 10035) == \
        "durées : docling 14 s, vlm 31 s ; RAM max 9,8 Go"
    assert "durees" in p.asdict(p.DocReport(pdf="x")) and "ram_max_mo" in p.asdict(p.DocReport(pdf="x"))


# --------------------------------------------------------------------------------------
# Lecture vectorielle (vector_charts.py) sur des PDF factices générés ici
# --------------------------------------------------------------------------------------
sys.path.insert(0, str(HERE / "tests"))
from pdfgen import CORAL, GREY, TEAL, pdf as _pdf, poly as _poly, rect as _rect, txt as _txt  # noqa: E402,F401


def _vec():
    import tempfile
    import vector_charts as vc
    return vc, Path(tempfile.mkdtemp(prefix="pdf2llm_test_"))


def test_vector_barres_avec_intertitres_et_texte_masque():
    vc, tmp = _vec()
    ops = [_txt(80, 420, "Sexe", rgb=(0.8, 0.1, 0.2)),
           _txt(60, 400, "Hommes"), _rect(120, 399, 104, 9, TEAL), _txt(228, 400, "52%"),
           _txt(60, 380, "Femmes"), _rect(120, 379, 68, 9, TEAL), _txt(192, 380, "34%"),
           _txt(80, 360, "Âge", rgb=(0.8, 0.1, 0.2)),
           _txt(50, 340, "18-24 ans"), _rect(120, 339, 72, 9, TEAL), _txt(196, 340, "36%"),
           # texte recouvert par une forme blanche : invisible, doit être ignoré
           _txt(300, 380, "99%"), _rect(295, 370, 40, 25, (1, 1, 1))]
    doc = vc.VectorDocument(_pdf(ops, tmp / "barres.pdf"))
    res = doc.read(1, 40, 320, 400, 440)
    assert res is not None, doc.last_reason
    # un tableau par intertitre : l'intertitre devient l'en-tête de la première colonne
    assert "| Sexe | Valeur |\n|---|---|\n| Hommes | 52% |\n| Femmes | 34% |" in res.text, res.text
    assert "| Âge | Valeur |\n|---|---|\n| 18-24 ans | 36% |" in res.text, res.text
    assert "Groupe" not in res.text
    assert "99" not in res.text
    assert not hasattr(doc.page(1), "px")          # rendu libéré après l'analyse (v9, T7)


def test_vector_secteurs_rattaches_par_la_legende():
    vc, tmp = _vec()
    c3 = (0.37, 0.7, 0.71)
    ops = [_poly([(600, 300), (720, 300), (600, 420)], TEAL), _txt(620, 330, "23%", rgb=(1, 1, 1)),
           _poly([(600, 300), (600, 420), (480, 300)], c3), _txt(560, 330, "20%", rgb=(1, 1, 1)),
           _poly([(480, 300), (720, 300), (600, 180)], CORAL), _txt(590, 260, "57%", rgb=(1, 1, 1)),
           # légende dans un ordre différent de la lecture : c'est elle qui fait foi
           _rect(780, 400, 10, 10, CORAL), _txt(795, 401, "Non"),
           _rect(780, 385, 10, 10, TEAL), _txt(795, 386, "Oui, souvent"),
           _rect(780, 370, 10, 10, c3), _txt(795, 371, "Oui, parfois"),
           _txt(660, 410, "Total Oui : 43%")]
    doc = vc.VectorDocument(_pdf(ops, tmp / "secteurs.pdf"))
    res = doc.read(1, 470, 170, 760, 440)
    assert res is not None, doc.last_reason
    t = res.text
    assert "| Non | 57% |\n| Oui, souvent | 23% |\n| Oui, parfois | 20% |" in t, t
    assert "Annotations : Total Oui : 43%" in t
    assert t.index("Annotations") < t.index("| Non |"), t      # métadonnées avant le tableau


def test_vector_barres_empilees_et_colonne_total():
    vc, tmp = _vec()
    ops = [_rect(40, 100, 10, 10, TEAL), _txt(55, 101, "Souvent"),
           _rect(40, 85, 10, 10, CORAL), _txt(55, 86, "Jamais"),
           _txt(320, 240, "Total Oui"),
           _txt(60, 200, "Regarder"), _rect(120, 195, 30, 20, TEAL), _txt(125, 201, "7%"),
           _rect(150, 195, 150, 20, CORAL), _txt(210, 201, "81%"), _txt(325, 201, "19%"),
           _txt(60, 160, "Lire"), _rect(120, 155, 20, 20, TEAL), _txt(122, 161, "5%"),
           _rect(140, 155, 160, 20, CORAL), _txt(210, 161, "88%"), _txt(325, 161, "12%")]
    doc = vc.VectorDocument(_pdf(ops, tmp / "empilees.pdf"))
    res = doc.read(1, 40, 140, 380, 260)
    assert res is not None, doc.last_reason
    assert "| Libellé | Souvent | Jamais | Total Oui |" in res.text, res.text
    assert "| Regarder | 7% | 81% | 19% |" in res.text and "| Lire | 5% | 88% | 12% |" in res.text


def test_vector_rappels_dans_un_tableau_a_part_et_legende():
    vc, tmp = _vec()
    ops = [_rect(40, 100, 10, 10, TEAL), _txt(55, 101, "Septembre 2026"),
           _txt(320, 240, "Rappel 2025"),
           _txt(60, 200, "Regarder"), _rect(120, 195, 100, 20, TEAL), _txt(225, 201, "40%"),
           _txt(325, 201, "35%"),
           _txt(60, 160, "Lire"), _rect(120, 155, 50, 20, TEAL), _txt(175, 161, "20%")]
    doc = vc.VectorDocument(_pdf(ops, tmp / "rappels.pdf"))
    res = doc.read(1, 40, 90, 380, 260)
    assert res is not None, doc.last_reason
    t = res.text
    assert "| Libellé | Valeur |\n|---|---|\n| Regarder | 40% |\n| Lire | 20% |" in t, t
    # rappel : tableau à part, seulement les lignes qui en ont un
    assert "| Libellé | Rappel 2025 |\n|---|---|\n| Regarder | 35% |" in t, t
    assert "| Lire | – |" not in t
    assert "Légende : Septembre 2026" in t, t


def test_vector_slide_ipsos_legende_haute_segment_etroit_et_rappels():
    """Traits de la slide Ipsos « préjugés (1/3) » : repères de légende hauts et étroits,
    libellé à 22 pt du repère, segment de 12 pt dont la valeur blanche couvre le centre,
    question sur deux lignes + « Base: A tous », bloc « % Vraie » avec années en en-tête."""
    vc, tmp = _vec()
    red, orange, green, white = (0.75, 0, 0), (1, 0.45, 0.06), (0.18, 0.7, 0.45), (1, 1, 1)
    ops = [_txt(60, 470, "Voici des opinions. Pour chacune d'entre elles,"),
           _txt(60, 458, "pensez-vous qu'elle est vraie ou fausse ?"),
           _txt(60, 446, "Base: A tous", size=8),
           _txt(712, 440, "% Vraie"), _txt(690, 420, "2024"), _txt(750, 420, "2020"),
           _txt(60, 401, "Opinion A"),
           _rect(300, 395, 12, 18, red), _txt(303, 401, "4", rgb=white),
           _rect(312, 395, 138, 18, orange), _txt(370, 401, "40", rgb=white),
           _rect(450, 395, 150, 18, green), _txt(515, 401, "56", rgb=white),
           _txt(695, 401, "44"), _txt(755, 401, "39"),
           _txt(60, 371, "Opinion B"),
           _rect(300, 365, 60, 18, red), _txt(325, 371, "20", rgb=white),
           _rect(360, 365, 140, 18, orange), _txt(420, 371, "30", rgb=white),
           _rect(500, 365, 100, 18, green), _txt(545, 371, "50", rgb=white),
           _txt(695, 371, "50"), _txt(757, 371, "-"),
           _rect(100, 60, 5, 19, red), _txt(127, 64, "Vraie"),
           _rect(250, 60, 5, 19, orange), _txt(277, 64, "Plutot vraie"),
           _rect(400, 60, 5, 19, green), _txt(427, 64, "Fausse")]
    doc = vc.VectorDocument(_pdf(ops, tmp / "ipsos.pdf"))
    res = doc.read(1, 40, 50, 800, 480)
    assert res is not None, doc.last_reason
    t = res.text
    assert t.startswith("Question : *Voici des opinions. Pour chacune d'entre elles, pensez-vous "
                        "qu'elle est vraie ou fausse ?*\nBase : A tous\n\n"), t
    assert ("| Libellé | Vraie | Plutot vraie | Fausse |\n|---|---|---|---|\n"
            "| Opinion A | 4 | 40 | 56 |\n| Opinion B | 20 | 30 | 50 |") in t, t
    assert ("*% Vraie*\n\n| Libellé | 2024 | 2020 |\n|---|---|---|\n"
            "| Opinion A | 44 | 39 |\n| Opinion B | 50 | – |") in t, t


def test_libelles_de_legende_colles_a_la_base():
    import pdf2llm as m
    body = ("| Libellé | Très positif | Assez positif | Assez négatif |\n|---|---|---|---|\n"
            "| France | 17 | 40 | 17 |")
    heads = m.table_heads(body)
    assert {"Très positif", "Assez positif", "Assez négatif", "France"} == heads
    # Docling colle les libellés de légende à « Base: A tous » (Ipsos p. 38)
    assert m.strip_labels("A tous Assez positif Assez négatif", heads) == "A tous"
    assert m.strip_labels("Très positif Assez négatif", heads) == ""      # texte retiré
    assert m.strip_labels("Les Français positifs", heads) == "Les Français positifs"


def test_vector_legende_a_32_points_du_repere():
    """Ipsos p. 38 : « Très négatif » est à 32 pt de son repère."""
    vc, tmp = _vec()
    ops = [_txt(60, 401, "France"), _rect(300, 395, 100, 18, TEAL), _txt(340, 401, "57"),
           _rect(400, 395, 50, 18, CORAL), _txt(420, 401, "43"),
           _rect(100, 60, 5, 19, TEAL), _txt(137, 64, "Positif"),
           _rect(250, 60, 5, 19, CORAL), _txt(287, 64, "Negatif")]
    doc = vc.VectorDocument(_pdf(ops, tmp / "ecart.pdf"))
    res = doc.read(1, 40, 50, 800, 430)
    assert res is not None, doc.last_reason
    assert "| Libellé | Positif | Negatif |" in res.text and "| France | 57 | 43 |" in res.text


def test_vector_chiffre_cle_rappels_et_encadre():
    """Ipsos p. 35 : « % Trop » / grand « 30 » / « RAPPELS 2020 : 30% »..., encadré pâle
    « 47% chez ceux ... » sur deux lignes, sur la même ligne de base que le « 30 »."""
    vc, tmp = _vec()
    red, green, pale = (0.75, 0, 0), (0.18, 0.7, 0.45), (0.9, 0.92, 0.94)
    ops = [_rect(100, 200, 120, 30, red), _txt(150, 210, "30", rgb=(1, 1, 1)),
           _rect(220, 200, 280, 30, green), _txt(350, 210, "70", rgb=(1, 1, 1)),
           _txt(60, 210, "Opinion"),
           _txt(560, 400, "% Trop", size=24), _txt(560, 330, "30", size=66),
           _txt(565, 300, "RAPPELS", size=10), _txt(565, 286, "2020 : 30%", size=10),
           _txt(565, 272, "2017 : 37%", size=10),
           _rect(700, 300, 200, 100, pale),
           _txt(710, 370, "47% chez ceux d'une", size=12), _txt(710, 355, "autre religion", size=12),
           _txt(710, 330, "36% chez les CSP-", size=12),
           _rect(100, 60, 5, 19, red), _txt(115, 64, "Trop"),
           _rect(250, 60, 5, 19, green), _txt(265, 64, "Comme il faut")]
    doc = vc.VectorDocument(_pdf(ops, tmp / "cle.pdf"))
    res = doc.read(1, 40, 50, 920, 450)
    assert res is not None, doc.last_reason
    t = res.text
    assert "Annotations : % Trop : 30 ; 47% chez ceux d'une autre religion ; 36% chez les CSP-" in t, t
    assert "| Opinion | 30 | 70 |" in t, t
    assert "*Rappels*\n\n| Année | % Trop |\n|---|---|\n| 2020 | 30% |\n| 2017 | 37% |" in t, t


def test_metadonnees_reconnues():
    from chart_format import ChartMeta, render, split_meta
    assert split_meta("Question : Connaissez-vous X ? Base : ensemble des Français") == {
        "question": "Connaissez-vous X ?", "base": "ensemble des Français"}
    assert split_meta("Avec laquelle des deux affirmations êtes-vous d'accord ? "
                      "(base: Ensemble de l'échantillon)") == {
        "question": "Avec laquelle des deux affirmations êtes-vous d'accord ?",
        "base": "Ensemble de l'échantillon"}
    assert split_meta("Base : personnes connaissant X (43% des Français)") == {
        "base": "personnes connaissant X (43% des Français)"}
    assert split_meta("Source : Ifop, octobre 2026") == {"source": "Ifop, octobre 2026"}
    # texte libre : rien n'est retiré du document
    assert split_meta("Qui connaît Alain Soral ?") is None
    assert split_meta("La database : 12 serveurs") is None
    m = ChartMeta()
    m.add_texts(["Qui connaît X ?", "Selon l'âge", "(base: Ensemble)"])
    assert render(m, "| a | b |") == ("Titre : Qui connaît X ? ; Selon l'âge\nBase : Ensemble"
                                      "\n\n| a | b |")


def test_vector_refuse_si_un_nombre_reste_inexplique():
    vc, tmp = _vec()
    ops = [_txt(60, 400, "Hommes"), _rect(120, 399, 104, 9, TEAL), _txt(228, 400, "52%"),
           _txt(300, 300, "77%")]                    # nombre isolé : pas de barre
    doc = vc.VectorDocument(_pdf(ops, tmp / "refus.pdf"))
    assert doc.read(1, 40, 280, 400, 440) is None
    assert "77%" in doc.last_reason


# --------------------------------------------------------------------------------------
# Triage et repli Granite
# --------------------------------------------------------------------------------------
def test_triage_type_de_pdf():
    P = p.PageProfile
    slides = [P(i, 900, 0.02, 40, True) for i in range(1, 11)]
    text = [P(i, 3000, 0.0, 5, False) for i in range(1, 11)]
    scan = [P(i, 0, 0.98, 0, False) for i in range(1, 11)]
    assert p.pdf_kind(slides) == "présentation"
    assert p.pdf_kind(text) == "texte"
    assert p.pdf_kind(scan) == "scan"


def test_triage_profil_depuis_un_pdf():
    _, tmp = _vec()
    path = _pdf([_txt(60, 400, "Bonjour " * 40), _rect(100, 100, 50, 50, TEAL)], tmp / "t.pdf")
    (pr,) = p.profile_pages(path, 1, 1)
    assert pr.page == 1 and pr.chars > 200 and pr.paths == 1 and pr.img_area == 0 and pr.landscape


def test_granite_hors_du_convertisseur_principal():
    """Le convertisseur principal n'exécute plus Granite ; seul celui de repli le fait."""
    from docling.datamodel.base_models import InputFormat
    args = NS(no_ocr=False, no_cell_matching=False, no_vlm=True, no_table_fallback=False,
              images_scale=2.0, charts="docling", chart_api_url=None, mlx=False)
    opts = lambda c: c.format_to_options[InputFormat.PDF].pipeline_options  # noqa: E731
    assert opts(p.build_converter(args)).do_chart_extraction is False
    assert opts(p.build_converter(args, charts=True)).do_chart_extraction is True
    assert opts(p.build_converter(args)).do_ocr is True
    pool = p.ConverterPool(args)
    assert pool.get() is pool.get() and pool.get(charts=True) is not pool.get()


def test_granite_rapprochement_des_boites():
    assert p._iou((0, 0, 10, 10), (0, 0, 10, 10)) == 1
    assert p._iou((0, 0, 10, 10), (20, 20, 30, 30)) == 0
    assert 0.6 < p._iou((0, 0, 10, 10), (1, 1, 10, 11)) < 0.95


# --------------------------------------------------------------------------------------
# Assistant interactif (wizard.py) : la logique, sans interaction
# --------------------------------------------------------------------------------------
def test_assistant_reponses_vers_arguments():
    import wizard as w
    base = {"input": "/d/a b.pdf", "output": "/d/sortie", "vlm_model": "qwen2.5-vl-7b-instruct",
            "vlm_url": w.DEFAULT_VLM_URL, "keep_images": "visual"}
    assert w.answers_to_argv(base) == ["/d/a b.pdf", "/d/sortie",
                                       "--vlm-model", "qwen2.5-vl-7b-instruct"]
    full = dict(base, pages="1-15", keep_images="none", force=True, dump_debug=True,
                no_vector=True, no_ocr=True, charts="vlm", chunk_pages=40,
                vlm_url="http://localhost:11434/v1")
    argv = w.answers_to_argv(full)
    assert argv[argv.index("--vlm-url") + 1] == "http://localhost:11434/v1"
    for flag in ["--force", "--dump-debug", "--no-vector", "--no-ocr"]:
        assert flag in argv
    assert argv[argv.index("--pages") + 1] == "1-15" and ["--charts", "vlm"] == argv[
        argv.index("--charts"):argv.index("--charts") + 2]
    assert ["--chunk-pages", "40"] == argv[argv.index("--chunk-pages"):][:2]
    # sans modèle vision : --no-vlm, et jamais --charts vlm
    nov = w.answers_to_argv(dict(base, vlm_model=None, charts="vlm"))
    assert "--no-vlm" in nov and "--charts" not in nov and "--vlm-model" not in nov
    # la commande affichée se recolle telle quelle (espaces protégés)
    assert w.command_line(w.answers_to_argv(base)).startswith("python pdf2llm.py '/d/a b.pdf'")


def test_assistant_memoire_et_utilitaires():
    import wizard as w
    _, tmp = _vec()
    prefs = tmp / "sous" / "choix.json"
    assert w.load_prefs(prefs) == {}
    w.save_prefs({"vlm_model": "x"}, prefs)
    assert w.load_prefs(prefs) == {"vlm_model": "x"}
    prefs.write_text("{cassé", "utf-8")
    assert w.load_prefs(prefs) == {}                     # fichier abîmé : ignoré
    assert w.vision_first(["llama-3-8b", "qwen2.5-vl-7b"])[0] == "qwen2.5-vl-7b"
    assert w.valid_pages("1-15") and w.valid_pages("7") and not w.valid_pages("1-")
    assert w.list_vlm_models("http://127.0.0.1:9/v1", timeout=0.5) is None
    pdf = _pdf([_txt(60, 400, "x")], tmp / "un.pdf")
    assert w.pdfs_in(tmp) == [pdf] and w.page_count(pdf) == 1


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


def test_fixture_metadonnees_de_page_dans_le_bloc_du_graphique():
    if not FIXTURE.exists():
        return "sauté"
    from docling_core.types.doc import DoclingDocument, PictureItem
    import pdf2llm as m
    doc = DoclingDocument.load_from_json(FIXTURE)
    pics = [it for it, _ in doc.iterate_items() if isinstance(it, PictureItem)]
    decisions = {p.self_ref: m.PicDecision() for p in pics}
    tab = "| Libellé | Valeur |\n|---|---|\n| X | 1% |"
    for i in (6, 8, 14):                  # p. 7 (deux graphiques), p. 9 (anneau)
        d = decisions[doc.pictures[i].self_ref]
        d.action, d.text, d.body = "vector", tab, tab
    m.build_chart_blocks(doc, pics, decisions)
    p7a, p7b = decisions[doc.pictures[6].self_ref], decisions[doc.pictures[8].self_ref]
    # « Question : ... ? Base : ... » (un seul bloc) -> premier graphique sous le texte
    assert p7a.text.startswith("Question : *Vous personnellement, connaissez-vous, ne serait-ce "
                               "que de nom, Alain Soral ?*\nBase : ensemble des Français\n\n| "), \
        p7a.text
    assert p7b.text == tab                # pas de doublon sur le second graphique
    # p. 9 : question et base en deux blocs
    p9 = decisions[doc.pictures[14].self_ref].text
    assert "Question : *Au regard de ce que vous en savez" in p9, p9
    assert "Base : personnes connaissant Alain Soral (43% des Français)" in p9, p9
    md = m.doc_to_markdown(doc, decisions)
    assert md.count("Vous personnellement, connaissez-vous") == 2   # p. 7 (bloc) + p. 8 (texte)
    # p. 9 : dans le bloc seulement (celle de la p. 12 est dans une image, avalée par Docling)
    assert md.count("personnes connaissant Alain Soral (43% des Français)") == 1
    assert "Base : ensemble des Français" in md and md.count("ensemble des Français") == 1


def test_fixture_lecture_vectorielle_du_vrai_pdf():
    """Le PDF de développement lui-même (variable PDF2LLM_TEST_PDF) + la fixture JSON."""
    pdf = os.environ.get("PDF2LLM_TEST_PDF")
    if not pdf or not FIXTURE.exists():
        return "sauté"
    import vector_charts as vc
    dj = json.loads(FIXTURE.read_text("utf-8"))
    doc = vc.VectorDocument(pdf)

    def read(i):
        pr = dj["pictures"][i]["prov"][0]
        b = pr["bbox"]
        return doc.read(pr["page_no"], b["l"], b["b"], b["r"], b["t"])
    ring = read(14)                       # anneau p. 9 : rattachement exact par la couleur
    assert ring and "| Très proche | 2% |\n| Plutôt proche | 11% |\n| Plutôt éloigné | 34% |" \
        "\n| Très éloigné | 53% |" in ring.text, doc.last_reason
    stacked = read(21)                    # barres empilées p. 12 + colonnes « Total Oui »
    assert stacked and "| 7% | 4% | 8% | 81% | 19% | 8% |" in stacked.text, doc.last_reason
    prof = read(10)                       # profil p. 8 : intertitres conservés
    assert prof and "| Statut | Valeur |" in prof.text and "| CHOMEURS | 39% |" in prof.text, \
        doc.last_reason


# --------------------------------------------------------------------------------------
# v9 Phase 2 : provenance, scans, cohérence, double lecture
# --------------------------------------------------------------------------------------
def _fake_pic(ref, page=1):
    return NS(self_ref=ref, prov=[NS(page_no=page, bbox=NS(l=0, t=100, r=100, b=0))])


def test_v9_t3_ligne_lecture_selon_la_source():
    tab = "| Libellé | % |\n|---|---|\n| Oui | 23% |"
    doc = NS(texts=[])
    pics = [_fake_pic(f"#/pictures/{i}") for i in range(5)]
    ds = {
        pics[0].self_ref: p.PicDecision(action="vector", text=tab, body=tab, page=1),
        pics[1].self_ref: p.PicDecision(action="chart_table", text=tab, page=1,
                                        lecture=p.lecture_text("granite")),
        pics[2].self_ref: p.PicDecision(action="vlm", text=tab, page=1, label="bar_chart",
                                        lecture=p.lecture_text("vlm")),
        pics[3].self_ref: p.PicDecision(action="text_layer", text="*Données lues…*", page=1),
        pics[4].self_ref: p.PicDecision(action="vlm", text="```mermaid\nflowchart TD\n```",
                                        page=1, label="flow_chart"),
    }
    p.build_chart_blocks(doc, pics, ds)
    out = [ds[x.self_ref].text for x in pics]
    assert "Lecture" not in out[0] and out[0] == tab              # exact : pas de ligne
    assert out[1] == "Lecture : modèle (Granite Vision), valeurs non vérifiées\n\n" + tab
    assert out[2] == "Lecture : modèle vision, valeurs non vérifiées\n\n" + tab
    assert "Lecture" not in out[3] and "Lecture" not in out[4]
    # la ligne vient en DERNIER parmi les métadonnées
    from chart_format import ChartMeta
    m = ChartMeta(title=["T"], source="Ifop", lecture="modèle vision, valeurs non vérifiées")
    assert m.lines()[-1].startswith("Lecture : ")
    # règle du modèle vision : tableau ou texte chiffré d'un graphique, pas une photo
    assert p.vlm_reads_numbers("photograph", tab)
    assert p.vlm_reads_numbers("bar_chart", "Valeurs : 12 % et 30 %")
    assert not p.vlm_reads_numbers("photograph", "Trois personnes devant 2 maisons.")
    assert not p.vlm_reads_numbers("bar_chart", "```mermaid\nflowchart TD\n A-->B\n```")


def test_v9_t3_tableaux_irreguliers_regle_ou_vlm():
    from PIL import Image
    grid = [["Libellé", "A", "B"], ["x", "1", "2"], ["y", "3", ""], ["z", "4 000 5,1", ""]]
    it = NS(self_ref="#/tables/0", prov=[NS(page_no=3)],
            data=NS(grid=[[NS(text=c) for c in r] for r in grid], num_rows=4),
            get_image=lambda doc: Image.new("RGB", (40, 40)))
    vlm = NS(ask=lambda img, label, mode, hint="": "| Libellé | A | B |\n|---|---|---|\n"
             "| x | 1 | 2 |\n| y | 3 | 9 |\n| z | 4 | 5 |")
    args = NS(no_table_fallback=False)
    orig = p.irregular_tables
    p.irregular_tables = lambda doc: [it]
    try:
        out = p.decide_tables(None, args, vlm, {3: {"1", "2", "3", "4", "5", "9"}})
        page, md, src = out["#/tables/0"]
        assert (page, src) == (3, "vlm") and md.startswith(
            "Lecture : modèle vision, valeurs non vérifiées\n\n| Libellé |"), md
        it.data.grid = [[NS(text=c) for c in r] for r in RAW_MARGIN_GRID]
        _pg, md, src = p.decide_tables(None, args, vlm, {})["#/tables/0"]
        assert src == "regle" and "Lecture" not in md
    finally:
        p.irregular_tables = orig


def test_v9_t4_pdf_sans_couche_texte():
    from PIL import Image, ImageDraw
    _, tmp = _vec()
    img = Image.new("RGB", (1200, 800), "white")
    ImageDraw.Draw(img).rectangle((100, 100, 500, 300), fill=(30, 90, 90))
    path = tmp / "capture.pdf"
    img.save(path, "PDF", resolution=100)            # une seule image, aucun texte
    prof = p.profile_pages(path, 1, 1)
    assert prof[0].chars == 0 and prof[0].img_area > 0.9
    assert p.pdf_kind(prof) == "scan"
    assert p.lecture_text("vlm", scan=True) == \
        "modèle vision, PDF sans couche texte, chiffres invérifiables"
    assert p.lecture_text("granite") == "modèle (Granite Vision), valeurs non vérifiées"
    # --scan-charts image : chiffres du modèle non écrits, image conservée
    tab = "| Libellé | % |\n|---|---|\n| Oui | 23% |"
    ds = {"a": p.PicDecision(action="vlm", text=tab, lecture=p.lecture_text("vlm", True)),
          "b": p.PicDecision(action="vlm", text="Une photo.", label="photograph")}
    assert p.unread_scan_charts(ds) == 1
    assert ds["a"].action == "image" and ds["a"].text == p.SCAN_UNREAD and not ds["a"].lecture
    assert ds["b"].text == "Une photo."
    assert p.SCAN_BANNER.startswith("> PDF sans couche texte")
    args = p.build_parser().parse_args(["a.pdf", "out"])
    assert args.scan_charts == "tag"


def test_v9_t5_controles_de_coherence():
    from controles import controles_coherence as cc, md_tables, num
    assert num("52%") == 52 and num("1,8") == 1.8 and num("–") is None and num("<1") is None
    # Ifop p. 9 : anneau 2 / 11 / 34 / 53 et « Total Proche » = 2 + 11
    ring = [["Libellé", "Valeur"], ["Très proche", "2%"], ["Plutôt proche", "11%"],
            ["Plutôt éloigné", "34%"], ["Très éloigné", "53%"], ["Total Proche", "13%"]]
    assert cc(ring, "secteurs") == []
    assert cc([r if r[0] != "Total Proche" else ["Total Proche", "15%"] for r in ring],
              "secteurs") == ["total incohérent (Total Proche = 15)"]
    # barres empilées : 97 sur 6 segments passe (arrondis), 90 est signalé
    head = ["Libellé", "A", "B", "C", "D", "E", "F"]
    assert cc([head, ["x", "10", "20", "30", "17", "10", "10"]], "empilees") == []
    assert cc([["Libellé", "A", "B", "C"], ["y", "30", "30", "30"]], "empilees") == \
        ["somme ≠ 100 (« y » : 90)"]
    # colonne « Total Oui » = somme d'une suite contiguë ; « Ensemble » à droite ignorée
    ifop = [["Libellé", "Très", "Assez", "Peu", "Pas", "Total Oui", "Ensemble"],
            ["Regarder", "7%", "4%", "8%", "81%", "19%", "8%"]]
    assert cc(ifop, "empilees") == [] and cc(ifop, "empilees", 4) == []
    # « Peu » et « Pas » permutés : plus aucune suite contiguë ne fait 19
    swapped = [ifop[0][:6], ["Regarder", "7%", "4%", "81%", "8%", "19%"]]
    assert cc(swapped, "empilees", 4) == ["total incohérent (« Regarder » : Total Oui = 19)"]
    # barres de profil : jamais de somme à 100
    assert cc([["Sexe", "Valeur"], ["Hommes", "52%"], ["Femmes", "34%"]], "") == []
    # intégration : auto-test vectoriel et étiquette « Lecture »
    body = "| Libellé | Valeur |\n|---|---|\n| Non | 57% |\n| Oui | 23% |"
    assert p.vector_checks(body, "secteurs", []) == ["somme ≠ 100 (secteurs : 80)"]
    assert p.vector_checks(body, "barres", []) == []
    assert md_tables(body)[0][1] == ["Non", "57%"]
    assert p.model_checks(body, "pie_chart") and not p.model_checks(body, "bar_chart")


def test_v9_t6_double_lecture_granite_vlm():
    g = [["Libellé", "Valeur"], ["Oui, souvent", "23%"], ["Oui, parfois", "20%"], ["Non", "57%"]]
    same = ("**Titre**\n\n| Réponse | % |\n|---|---|\n| Non | 57% |\n| Oui, souvent | 23% |\n"
            "| Oui, parfois | 20% |")
    ok, detail = p.compare_readings(g, [["h"]] + p.md_tables(same)[0][1:])
    assert ok, detail
    # valeurs permutées entre deux libellés : désaccord (le multiensemble serait identique)
    swap = [["h"], ["Oui, souvent", "20%"], ["Oui, parfois", "23%"], ["Non", "57%"]]
    ok, detail = p.compare_readings(g, swap)
    assert not ok and "1/3 valeurs égales" in detail, detail
    # colonne « Groupe » ajoutée par le VLM : le libellé est retrouvé quand même
    grp = [["h"], ["Fréquence", "Oui, souvent", "23%"], ["Fréquence", "Oui, parfois", "20%"],
           ["Fréquence", "Non", "57%"]]
    assert p.compare_readings(g, grp)[0]
    # libellés partiellement retrouvés (2 sur 3 < 80 %) : désaccord
    part = [["h"], ["Oui souvent", "23%"], ["Jamais", "20%"], ["Non", "57%"]]
    ok, detail = p.compare_readings(g, part)
    assert not ok and detail.startswith("2/3 lignes"), detail

    # application : concordance -> « 2 modèles concordants » ; désaccord -> image + valeurs
    # de la couche texte sans rattachement + avertissement
    doc = NS(texts=[NS(text="57%", prov=[NS(page_no=1, bbox=NS(l=10, t=50, r=20, b=40))]),
                    NS(text="Titre", prov=[NS(page_no=1, bbox=NS(l=10, t=90, r=40, b=80))])])
    pic = _fake_pic("#/pictures/0")
    d = p.PicDecision(action="chart_table", text=p.rows_to_md(g), page=1,
                      lecture=p.lecture_text("granite"))
    p.apply_cross_check(doc, pic, d, g, same)
    assert d.action == "chart_table" and d.lecture == "2 modèles concordants, valeurs non vérifiées"
    d = p.PicDecision(action="chart_table", text=p.rows_to_md(g), page=1,
                      lecture=p.lecture_text("granite"))
    p.apply_cross_check(doc, pic, d, g, "| L | % |\n|---|---|\n| Oui, souvent | 20% |\n"
                        "| Oui, parfois | 23% |\n| Non | 57% |")
    assert d.action == "image" and not d.lecture and d.warning.startswith(p.CROSS_DISAGREE)
    assert d.text == ("*Valeurs lues sans rattachement établi (Granite et modèle vision en "
                      "désaccord) : 57%*"), d.text
    # VLM muet : on garde Granite tel quel
    d = p.PicDecision(action="chart_table", text="x", lecture=p.lecture_text("granite"))
    p.apply_cross_check(doc, pic, d, g, None)
    assert d.action == "chart_table" and "impossible" in d.note
    assert p.build_parser().parse_args(["a", "b", "--no-cross-check"]).no_cross_check


# --------------------------------------------------------------------------------------
# Golden : sortie complète comparée à une version validée (tools/golden.py)
# --------------------------------------------------------------------------------------
def _golden():
    sys.path.insert(0, str(HERE / "tools"))
    import golden
    return golden


def _golden_report(bad: dict) -> str:
    return "\n".join(f"=== {k}\n" + "\n".join(v) for k, v in bad.items())


def test_v9_t2_goldens_synthetiques():
    """PDF générés (tests/golden_gen.py), convertis sans modèle : Markdown et rapport
    identiques à tests/golden/. Après un changement voulu : python tools/golden.py update."""
    import tempfile
    g = _golden()
    bad = g.check_cases(g.synthetic_cases(Path(tempfile.mkdtemp(prefix="pdf2llm_golden_"))))
    assert not bad, "sortie modifiée (python tools/golden.py check) :\n" + _golden_report(bad)


def test_v9_t2_goldens_locaux():
    """Vrais PDF de l'utilisateur (PDF2LLM_GOLDEN_DIR : x.pdf + x.attendu.md + x.args)."""
    if not os.environ.get("PDF2LLM_GOLDEN_DIR"):
        return "sauté"
    g = _golden()
    cases = g.local_cases(only_baselined=True)
    if not cases:
        return "sauté"
    bad = g.check_cases(cases)
    assert not bad, "sortie modifiée :\n" + _golden_report(bad)


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
