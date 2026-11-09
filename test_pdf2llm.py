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
# Lecture vectorielle (vector_charts.py) sur des PDF factices générés ici
# --------------------------------------------------------------------------------------
def _pdf(ops: list[str], path: Path) -> Path:
    """PDF d'une page 960 x 540 dont le contenu est la liste d'opérateurs `ops`
    (Helvetica standard, encodage WinAnsi pour les accents)."""
    content = "\n".join(ops).encode("cp1252")
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 960 540] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"]
    out, offs = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offs)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    path.write_bytes(bytes(out))
    return path


def _txt(x, y, s, size=10, rgb=(0.25, 0.25, 0.25)):
    s = s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return f"BT /F1 {size} Tf {rgb[0]} {rgb[1]} {rgb[2]} rg {x} {y} Td ({s}) Tj ET"


def _rect(x, y, w, h, rgb):
    return f"{rgb[0]} {rgb[1]} {rgb[2]} rg {x} {y} {w} {h} re f"


def _poly(pts, rgb):
    path = " ".join(f"{x} {y} {'m' if i == 0 else 'l'}" for i, (x, y) in enumerate(pts))
    return f"{rgb[0]} {rgb[1]} {rgb[2]} rg {path} h f"


TEAL, CORAL, GREY = (0.18, 0.4, 0.4), (0.98, 0.55, 0.52), (0.6, 0.6, 0.6)


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
    assert "| Groupe | Libellé | Valeur |" in res.text
    assert "| Sexe | Hommes | 52% |" in res.text and "| Sexe | Femmes | 34% |" in res.text
    assert "| Âge | 18-24 ans | 36% |" in res.text
    assert "99" not in res.text


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
    assert "- Total Oui : 43%" in t


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
    assert prof and "| Statut | CHOMEURS | 39% |" in prof.text, doc.last_reason


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
