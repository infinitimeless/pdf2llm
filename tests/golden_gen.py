"""Générateur des PDF « golden » synthétiques (tests de non-régression de bout en bout).

Les PDF ne sont pas versionnés (*.pdf est ignoré par git) : on versionne ce générateur et
les sorties attendues (tests/golden/<nom>.md, tests/golden/<nom>.report.json). Les
données sont fictives. Chaque deck est un PDF de slides 960 x 540, de plusieurs pages.

    python tests/golden_gen.py <dossier>      # écrit les PDF dans <dossier>

Couverture voulue :
- barres simples avec intertitres de groupe, question et base ;
- anneau (secteurs) rattaché par la légende, « Total Oui » ;
- barres empilées avec une colonne « Total » ;
- bloc de rappels (colonne d'une vague antérieure) ;
- chiffre-clé avec liste de rappels et encadré ;
- un graphique qui doit être REFUSÉ (un nombre inexpliqué) : gardé en image ;
- un pied de page répété sur toutes les slides du premier deck.
Aucun graphique ne demande Granite ni le modèle vision : soit lu en vectoriel, soit refusé."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pdfgen import CORAL, TEAL, frame, pdf_pages, poly, rect, txt  # noqa: E402

RED, GREEN, PALE, WHITE = (0.75, 0, 0), (0.18, 0.7, 0.45), (0.9, 0.92, 0.94), (1, 1, 1)
DARK = (0.1, 0.1, 0.3)
FOOT = "Etude factice pdf2llm - septembre 2026 - document de test"


def _title(s: str) -> str:
    return txt(60, 490, s, size=22, rgb=DARK)


def _foot(n: int) -> list[str]:
    return [txt(60, 20, FOOT, size=8), txt(900, 20, str(n), size=8)]


def deck_barres() -> list[list[str]]:
    """Barres simples par groupe, chiffre-clé, texte : 5 slides avec pied de page."""
    p1 = [_title("Barometre factice de la confiance"),
          txt(60, 400, "Enquete realisee en ligne du 1er au 5 septembre 2026 aupres de"),
          txt(60, 385, "1 000 personnes, echantillon representatif de la population."),
          *_foot(1)]
    p2 = [_title("Confiance dans les institutions selon le profil"),
          txt(60, 455, "Question : Faites-vous confiance aux institutions locales ?"),
          txt(60, 440, "Base : ensemble des personnes interrogees", size=8),
          txt(80, 410, "Sexe", rgb=(0.8, 0.1, 0.2)),
          txt(60, 390, "Hommes"), rect(140, 389, 104, 9, TEAL), txt(248, 390, "52%"),
          txt(60, 370, "Femmes"), rect(140, 369, 68, 9, TEAL), txt(212, 370, "34%"),
          txt(80, 340, "Age", rgb=(0.8, 0.1, 0.2)),
          txt(60, 320, "18-24 ans"), rect(140, 319, 72, 9, TEAL), txt(216, 320, "36%"),
          txt(60, 300, "25-34 ans"), rect(140, 299, 90, 9, TEAL), txt(234, 300, "45%"),
          txt(60, 280, "65 ans et plus"), rect(140, 279, 40, 9, TEAL), txt(184, 280, "20%"),
          *_foot(2)]
    p3 = [_title("Le chiffre cle"),
          rect(100, 200, 120, 30, RED), txt(150, 210, "30", rgb=WHITE),
          rect(220, 200, 280, 30, GREEN), txt(350, 210, "70", rgb=WHITE),
          txt(60, 210, "Opinion"),
          rect(100, 240, 150, 30, RED), txt(170, 250, "35", rgb=WHITE),
          rect(250, 240, 250, 30, GREEN), txt(370, 250, "65", rgb=WHITE),
          txt(60, 250, "Avis B"),
          txt(560, 400, "% Trop", size=24), txt(560, 330, "30", size=66),
          txt(565, 300, "RAPPELS", size=10), txt(565, 286, "2020 : 30%", size=10),
          txt(565, 272, "2017 : 37%", size=10),
          rect(700, 300, 200, 100, PALE),
          txt(710, 370, "47% chez ceux d'une", size=12), txt(710, 355, "autre religion", size=12),
          txt(710, 330, "36% chez les CSP-", size=12),
          rect(100, 60, 5, 19, RED), txt(115, 64, "Trop"),
          rect(250, 60, 5, 19, GREEN), txt(265, 64, "Comme il faut"),
          *_foot(3)]
    p4 = [_title("Methodologie"),
          txt(60, 400, "Les resultats sont arrondis a l'unite. Les totaux peuvent"),
          txt(60, 385, "differer de la somme des parties a cause des arrondis."),
          *_foot(4)]
    p5 = [_title("Contacts"),
          txt(60, 400, "Departement Opinion, institut factice."),
          *_foot(5)]
    return [p1, p2, p3, p4, p5]


def deck_anneau_empilees() -> list[list[str]]:
    """Anneau + barres empilées avec colonne « Total Oui » + rappels."""
    c3 = (0.37, 0.7, 0.71)
    p1 = [_title("Notoriete de la mesure"),
          txt(60, 455, "Question : Avez-vous entendu parler de la mesure ?"),
          poly([(600, 300), (720, 300), (600, 420)], TEAL), txt(620, 330, "23%", rgb=WHITE),
          poly([(600, 300), (600, 420), (480, 300)], c3), txt(560, 330, "20%", rgb=WHITE),
          poly([(480, 300), (720, 300), (600, 180)], CORAL), txt(590, 260, "57%", rgb=WHITE),
          rect(780, 400, 10, 10, CORAL), txt(795, 401, "Non"),
          rect(780, 385, 10, 10, TEAL), txt(795, 386, "Oui, souvent"),
          rect(780, 370, 10, 10, c3), txt(795, 371, "Oui, parfois"),
          txt(660, 410, "Total Oui : 43%")]
    # séries cohérentes : somme 100, « Total Oui » = Souvent + Parfois
    gold = (0.85, 0.65, 0.13)
    p2 = [_title("Pratiques culturelles"),
          rect(40, 115, 10, 10, TEAL), txt(55, 116, "Souvent"),
          rect(40, 100, 10, 10, gold), txt(55, 101, "Parfois"),
          rect(40, 85, 10, 10, CORAL), txt(55, 86, "Jamais"),
          txt(340, 240, "Total Oui"),
          txt(60, 200, "Regarder"), rect(120, 195, 30, 20, TEAL), txt(125, 201, "7%"),
          rect(150, 195, 40, 20, gold), txt(160, 201, "12%"),
          rect(190, 195, 130, 20, CORAL), txt(240, 201, "81%"), txt(345, 201, "19%"),
          txt(60, 160, "Lire"), rect(120, 155, 20, 20, TEAL), txt(122, 161, "5%"),
          rect(140, 155, 30, 20, gold), txt(145, 161, "7%"),
          rect(170, 155, 150, 20, CORAL), txt(240, 161, "88%"), txt(345, 161, "12%")]
    p3 = [_title("Evolution des usages"),
          rect(40, 100, 10, 10, TEAL), txt(55, 101, "Septembre 2026"),
          txt(320, 240, "Rappel 2025"),
          txt(60, 200, "Regarder"), rect(120, 195, 100, 20, TEAL), txt(225, 201, "40%"),
          txt(325, 201, "35%"),
          txt(60, 160, "Lire"), rect(120, 155, 50, 20, TEAL), txt(175, 161, "20%")]
    return [p1, p2, p3]


def deck_refus() -> list[list[str]]:
    """Un graphique avec un nombre isolé : la lecture vectorielle doit le refuser."""
    p1 = [_title("Graphique a refuser"),
          frame(40, 270, 360, 160),
          txt(60, 400, "Hommes"), rect(120, 399, 104, 9, TEAL), txt(228, 400, "52%"),
          txt(60, 380, "Femmes"), rect(120, 379, 68, 9, TEAL), txt(192, 380, "34%"),
          txt(300, 300, "77%"),
          txt(60, 200, "Un nombre sans barre rend le graphique inexplicable.")]
    p2 = [_title("Conclusion"), txt(60, 400, "Fin du document de test.")]
    return [p1, p2]


DECKS = {
    "g1_barres_chiffre_cle": deck_barres,
    "g2_anneau_empilees_rappels": deck_anneau_empilees,
    "g3_refus": deck_refus,
}


def generate(out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    return [pdf_pages(fn(), out_dir / f"{name}.pdf") for name, fn in DECKS.items()]


if __name__ == "__main__":
    for path in generate(Path(sys.argv[1] if len(sys.argv) > 1 else "golden_pdfs")):
        print(path)
