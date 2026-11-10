"""
vector_charts.py — Lecture exacte des graphiques VECTORIELS d'un PDF, sans modèle vision.

Dans un PDF exporté de PowerPoint (sondages, études), un graphique n'est pas une image :
les barres et les secteurs sont des formes remplies d'une couleur, et chaque libellé ou
valeur est du texte de la couche PDF. On reconstruit donc le tableau par la géométrie :

  - secteur / segment empilé : la valeur est DANS une forme de couleur C ; le carré de
    légende de couleur C donne le libellé (couleur ↔ légende ↔ valeur : exact) ;
  - barre simple : la valeur est juste à droite (barres horizontales) ou juste au-dessus
    (barres verticales) d'un rectangle ; le libellé est sur la même ligne, à gauche
    (ou dessous) ; les intertitres de groupe (« Sexe », « Âge »...) sont au-dessus ;
  - colonnes de valeurs sans barre (« Total Oui », « Ensemble des Français ») : rattachées
    à la ligne de la barre, avec l'en-tête aligné au-dessus.

Principe de sûreté : un résultat n'est rendu que si TOUS les nombres visibles de la zone et
toutes les barres sont expliqués ; sinon `read()` renvoie None et pdf2llm.py garde son
chemin habituel (Granite Vision, puis modèle vision).

Le texte invisible (recouvert par une forme, hors cadre) est écarté : on vérifie sur le
rendu de la page qu'il y a bien de l'encre dans la boîte de chaque mot.

Dépend seulement de pypdfium2 (déjà installé avec Docling), de Pillow et de numpy.
Coordonnées : points PDF, origine en bas à gauche (comme les boîtes de Docling).
"""

from __future__ import annotations

import ctypes
import re
from dataclasses import dataclass, field
from statistics import median

import numpy as np
import pypdfium2 as pdfium
import pypdfium2.raw as raw

from chart_format import ChartMeta, md_table, render, split_meta

NUM_WORD = re.compile(r"^[-+−]?\d+(?:[.,]\d+)?%?$")
YEAR = re.compile(r"^(?:19|20)\d{2}$")
PCT_LEAD = re.compile(r"^\d+(?:[.,]\d+)?\s*%\s+[^\W\d]")   # « 19% chez ceux... »
BULLET = re.compile(r"^\s*[\uf000-\uf8ff•▪▸►‣]\s*")      # puce (souvent un glyphe privé)
WHITE = 235            # composante au-dessus de laquelle une couleur est « blanche »
MARGIN_BOTTOM = 25     # bande du bas de page ignorée (numéro de slide, pied de page)
MARGIN_TOP = 12


# --------------------------------------------------------------------------------------
# Primitives de la page
# --------------------------------------------------------------------------------------
@dataclass
class Box:
    l: float
    b: float
    r: float
    t: float

    @property
    def cx(self) -> float:
        return (self.l + self.r) / 2

    @property
    def cy(self) -> float:
        return (self.b + self.t) / 2

    @property
    def w(self) -> float:
        return self.r - self.l

    @property
    def h(self) -> float:
        return self.t - self.b


@dataclass
class Word(Box):
    text: str = ""
    color: tuple = (0, 0, 0)
    size: float = 8.0
    visible: bool = True

    @property
    def is_num(self) -> bool:
        return bool(NUM_WORD.match(self.text))


@dataclass
class Line(Box):
    words: list = field(default_factory=list)
    size: float = 8.0
    visible: bool = True

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def is_num(self) -> bool:
        return all(w.is_num for w in self.words)


@dataclass
class Shape(Box):
    color: tuple = (0, 0, 0)
    rect: bool = False                 # rectangle à angles droits (barre)
    fill_ratio: float = 0.0            # aire du polygone / aire de la boîte (carré arrondi ≈ 0,9)
    poly: list = field(default_factory=list)
    visible: bool = True

    @property
    def area(self) -> float:
        return self.w * self.h

    def contains(self, x: float, y: float) -> bool:
        if not (self.l <= x <= self.r and self.b <= y <= self.t):
            return False
        if self.rect:
            return True
        inside, pts = False, self.poly
        for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
            if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1 + 1e-9) + x1:
                inside = not inside
        return inside


def _near(c1: tuple, c2: tuple, tol: int = 8) -> bool:
    return all(abs(a - b) <= tol for a, b in zip(c1, c2))


def _is_white(c: tuple) -> bool:
    return all(v >= WHITE for v in c)


class VectorPage:
    """Mots, lignes de texte, formes remplies et légende d'une page."""

    def __init__(self, page: pdfium.PdfPage):
        self.W, self.H = page.get_size()
        # Rendu à 2 px par point : une barre de 1 % (1,5 pt de large) reste mesurable
        self.scale = 2
        img = page.render(scale=self.scale).to_pil().convert("RGB")
        self.px = np.asarray(img).astype(np.int16)
        self.words = self._words(page)
        self.lines = self._lines(self.words)
        self.shapes = self._shapes(page)
        self.legend, self.swatches = self._legend()

    # -- rendu : le texte et les formes sont-ils réellement visibles ? -----------------
    def _crop(self, b: Box) -> np.ndarray:
        h, w = self.px.shape[:2]
        k = self.scale
        x0, x1 = max(0, int(b.l * k)), min(w, int(np.ceil(b.r * k)))
        y0, y1 = max(0, int((self.H - b.t) * k)), min(h, int(np.ceil((self.H - b.b) * k)))
        if x1 <= x0 or y1 <= y0:                 # boîte plus fine qu'un pixel : pixel central
            cx, cy = int(b.cx * k), int((self.H - b.cy) * k)
            x0, x1, y0, y1 = cx, cx + 1, cy, cy + 1
        return self.px[y0:y1, x0:x1]

    def _ink(self, w: "Word") -> bool:
        """Le mot est visible si son rendu montre des pixels de SA couleur, avec un minimum
        de contraste. Recouvert par une forme (même blanche sur du blanc), il ne l'est pas."""
        c = self._crop(w)
        if c.size == 0:
            return False
        lum = c.mean(axis=2)
        if float(lum.max() - lum.min()) <= 15:
            return False
        close = np.abs(c - np.array(w.color)).max(axis=2) <= 50
        return int(close.sum()) >= 2

    def _shows_color(self, s: Shape) -> bool:
        # Part des pixels de la forme qui ont sa couleur (et non la médiane du centre : la
        # valeur écrite en blanc dans un segment étroit en couvre le milieu, ex. « 4 »).
        m = min(0.5, s.w / 4, s.h / 4)
        c = self._crop(Box(s.l + m, s.b + m, s.r - m, s.t - m))
        if c.size == 0:
            return False
        px = c.reshape(-1, 3)
        return bool((np.abs(px - np.array(s.color)).max(axis=1) <= 40).mean() >= 0.3)

    # -- texte ---------------------------------------------------------------------------
    def _words(self, page: pdfium.PdfPage) -> list[Word]:
        tp = page.get_textpage()
        words: list[Word] = []
        cur: list[tuple] = []          # (char, l, b, r, t, color, size)

        def flush() -> None:
            if not cur:
                return
            txt = "".join(c[0] for c in cur).strip().replace("\ufffe", "-")
            txt = re.sub(r"%{2,}", "%", txt)            # glyphe « % » doublé (« 40%% »)
            if txt:
                w = Word(min(c[1] for c in cur), min(c[2] for c in cur),
                         max(c[3] for c in cur), max(c[4] for c in cur),
                         text=txt, color=cur[0][5], size=max(c[6] for c in cur))
                if (txt == "%" and words and words[-1].is_num
                        and -0.2 * w.size <= w.l - words[-1].r < w.size):
                    p = words[-1]                       # « 43 % » -> « 43% »
                    p.text, p.r = p.text + "%", w.r
                else:
                    words.append(w)
            cur.clear()

        r_, g_, b_, a_ = (ctypes.c_uint() for _ in range(4))
        for i in range(tp.count_chars()):
            ch = tp.get_text_range(i, 1)
            if not ch or ch.isspace():
                flush()
                continue
            # Boîte « large » (avance du caractère) : avec la boîte serrée, l'écart entre
            # deux « 1 » étroits ferait couper « 11% » en « 1 » + « 1% ».
            l, b, r, t = tp.get_charbox(i, loose=True)
            l, r = min(l, r), max(l, r)                 # boîte parfois inversée (r < l)
            b, t = min(b, t), max(b, t)
            if r - l <= 0 and t - b <= 0:
                continue
            raw.FPDFText_GetFillColor(tp, i, ctypes.byref(r_), ctypes.byref(g_),
                                      ctypes.byref(b_), ctypes.byref(a_))
            size = raw.FPDFText_GetFontSize(tp, i) or (t - b)
            color = (r_.value, g_.value, b_.value)
            if cur:
                p = cur[-1]
                if (abs(b - p[2]) > 0.5 * size or l - p[3] > 0.25 * size
                        or l < p[1] - 0.5 * size):
                    flush()
            cur.append((ch, l, b, r, t, color, size))
        flush()
        for w in words:
            w.visible = self._ink(w)
        return words

    @staticmethod
    def _lines(words: list[Word]) -> list[Line]:
        """Mots de même ligne de base, puis découpe gauche -> droite aux grands blancs.
        (Regrouper par centre vertical échoue : accents et parenthèses le déplacent.)"""
        bands: list[list[Word]] = []
        for w in sorted(words, key=lambda w: -w.b):
            if bands and abs(bands[-1][0].b - w.b) <= 0.3 * max(w.size, bands[-1][0].size):
                bands[-1].append(w)
            else:
                bands.append([w])
        lines: list[Line] = []
        for band in bands:
            ln = None
            for w in sorted(band, key=lambda w: w.l):
                # (pas de fusion entre tailles très différentes : le grand « 30 » d'un
                # chiffre-clé et « catholicisme » de l'encadré voisin, même ligne de base)
                if (ln is not None and w.l - ln.r <= 0.6 * min(w.size, ln.size)
                        and max(w.size, ln.size) <= 1.5 * min(w.size, ln.size)):
                    ln.words.append(w)
                    ln.r, ln.t, ln.b = max(ln.r, w.r), max(ln.t, w.t), min(ln.b, w.b)
                    ln.size = max(ln.size, w.size)
                else:
                    ln = Line(w.l, w.b, w.r, w.t, words=[w], size=w.size)
                    lines.append(ln)
        for ln in lines:
            ln.visible = all(w.visible for w in ln.words)
            ln.size = max(w.size for w in ln.words)
        return lines

    # -- formes --------------------------------------------------------------------------
    def _shapes(self, page: pdfium.PdfPage) -> list[Shape]:
        out: list[Shape] = []
        fill, stroke = ctypes.c_int(), ctypes.c_int()
        rgba = [ctypes.c_uint() for _ in range(4)]
        for o in page.get_objects(max_depth=5):
            if o.type != raw.FPDF_PAGEOBJ_PATH:
                continue
            raw.FPDFPath_GetDrawMode(o, ctypes.byref(fill), ctypes.byref(stroke))
            if not fill.value:
                continue
            raw.FPDFPageObj_GetFillColor(o, *[ctypes.byref(x) for x in rgba])
            if rgba[3].value < 128:
                continue
            color = tuple(x.value for x in rgba[:3])
            m = o.get_matrix()
            # PowerPoint dessine souvent TOUTES les barres d'une couleur (ou tous les carrés
            # de légende) dans un seul chemin : on le découpe en sous-chemins (MOVETO).
            subpaths: list[tuple[list, bool]] = []
            for i in range(raw.FPDFPath_CountSegments(o)):
                seg = raw.FPDFPath_GetPathSegment(o, i)
                kind = raw.FPDFPathSegment_GetType(seg)
                x, y = ctypes.c_float(), ctypes.c_float()
                raw.FPDFPathSegment_GetPoint(seg, ctypes.byref(x), ctypes.byref(y))
                pt = (m.a * x.value + m.c * y.value + m.e, m.b * x.value + m.d * y.value + m.f)
                if kind == raw.FPDF_SEGMENT_MOVETO or not subpaths:
                    subpaths.append(([pt], False))
                else:
                    pts, curved = subpaths[-1]
                    pts.append(pt)
                    if kind == raw.FPDF_SEGMENT_BEZIERTO:
                        subpaths[-1] = (pts, True)
            for pts, curved in subpaths:
                if len(pts) < 3:
                    continue
                xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                l, b, r, t = min(xs), min(ys), max(xs), max(ys)
                box_area = (r - l) * (t - b)
                if box_area <= 0:
                    continue
                area = abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2)
                               in zip(pts, pts[1:] + pts[:1]))) / 2
                rect = not curved and area / box_area > 0.95
                s = Shape(l, b, r, t, color=color, rect=rect, fill_ratio=area / box_area, poly=pts)
                s.visible = not _is_white(color) and (not rect or self._shows_color(s))
                out.append(s)
        return out

    def _legend(self) -> tuple[dict[tuple, str], set[int]]:
        """Carrés de légende : petit rectangle de couleur C suivi d'un libellé à droite."""
        found: dict[tuple, list[tuple[Line, Shape]]] = {}
        for s in self.shapes:
            # carré ou carré arrondi (les angles arrondis sont des courbes)
            if not (s.visible and s.fill_ratio > 0.75 and 4 <= s.w <= 22 and 4 <= s.h <= 22
                    and max(s.w, s.h) <= 4 * min(s.w, s.h)):
                continue
            cands = [ln for ln in self.lines if ln.visible and not ln.is_num
                     and 0 <= ln.l - s.r <= 40 and s.b - 3 <= ln.cy <= s.t + 3]
            if cands:
                ln = min(cands, key=lambda ln: ln.l - s.r)
                found.setdefault(s.color, []).append((ln, s))
        legend, swatches = {}, set()
        self.legend_lines: set[int] = set()
        primary = {id(ln) for items in found.values() for ln, _ in items}
        for color, items in found.items():
            if len({ln.text for ln, _ in items}) == 1:      # une couleur = un seul libellé
                ln = items[0][0]
                # libellé sur deux lignes (« Ni une bonne chose, ni une » / « mauvaise
                # chose ») : ligne juste dessous, sous la première, sans chiffre
                parts, last = [ln], ln
                for o in sorted(self.lines, key=lambda x: -x.cy):
                    if (id(o) not in primary and o.visible and not re.search(r"\d", o.text)
                            and 0 < last.cy - o.cy <= 1.9 * o.size
                            and o.l >= ln.l - 5 and o.r <= ln.r + 5):
                        parts.append(o)
                        last = o
                legend[color] = " ".join(x.text for x in parts)
                self.legend_lines.update(id(x) for x in parts)
                swatches.update(id(s) for _, s in items)
        return legend, swatches

    def legend_label(self, color: tuple) -> str | None:
        for c, lab in self.legend.items():
            if _near(c, color):
                return lab
        return None

    def legend_order(self) -> list[str]:
        sw = [s for s in self.shapes if id(s) in self.swatches]
        out: list[str] = []
        for s in sorted(sw, key=lambda s: (-round(s.t), s.l)):
            lab = self.legend_label(s.color)
            if lab and lab not in out:
                out.append(lab)
        return out


# --------------------------------------------------------------------------------------
# Reconstruction d'un graphique dans une zone
# --------------------------------------------------------------------------------------
class Unreadable(Exception):
    """Le graphique n'est pas compris en entier : on rend la main au chemin habituel."""


@dataclass
class ChartResult:
    text: str          # rendu complet : métadonnées + tableaux (chart_format.render)
    kind: str          # "secteurs", "barres", "barres empilées", ...
    values: int        # nombre de valeurs lues
    meta: ChartMeta = field(default_factory=ChartMeta)
    body: str = ""     # tableaux seuls

# En-tête d'une colonne sans barre qui est un rappel d'une vague antérieure : ces valeurs
# vont dans un tableau à part (« un jeu de données homogène par tableau »).
HISTORY_HEAD = re.compile(r"rappel|\b(?:19|20)\d{2}\b|vague|évol|evol|écart|ecart|\bvs\b"
                          r"|précédent|precedent", re.I)


def _row_of(bars: list[Shape]) -> list[list[Shape]]:
    """Regroupe les rectangles en lignes : même hauteur et contigus horizontalement
    (segments d'une barre empilée), sinon une barre = une ligne."""
    rows: list[list[Shape]] = []
    for s in sorted(bars, key=lambda s: (-round(s.cy), s.l)):
        for row in rows:
            last = row[-1]
            if abs(s.cy - last.cy) <= 2 and abs(s.h - last.h) <= 2 and -2 <= s.l - last.r <= 3:
                row.append(s)
                break
        else:
            rows.append([s])
    return rows


def _columns(rows: list[dict]) -> None:
    """Numérote les colonnes de graphiques (rw["col"]) : deux lignes dont les barres se
    chevauchent horizontalement sont dans la même colonne (le bord gauche ne suffit pas :
    les barres divergentes ou empilées ne commencent pas toutes au même x)."""
    spans: list[list[float]] = []
    for rw in sorted(rows, key=lambda x: x["l"]):
        for i, (a, b) in enumerate(spans):
            if rw["l"] <= b and rw["r"] >= a:
                spans[i] = [min(a, rw["l"]), max(b, rw["r"])]
                rw["col"] = i
                break
        else:
            spans.append([rw["l"], rw["r"]])
            rw["col"] = len(spans) - 1


_md_table = md_table


def read_region(pg: VectorPage, l: float, b: float, r: float, t: float,
                pad: float = 4) -> ChartResult | None:
    zone = Box(l - pad, b - pad, r + pad, t + pad)

    def inside(x: float, y: float) -> bool:
        return (zone.l <= x <= zone.r and zone.b <= y <= zone.t
                and MARGIN_BOTTOM < y < pg.H - MARGIN_TOP)

    lines = [ln for ln in pg.lines if ln.visible and inside(ln.cx, ln.cy)]
    values = [w for ln in lines if ln.is_num for w in ln.words]
    if not values:
        raise Unreadable("aucun nombre visible dans la zone")
    labels = [ln for ln in lines if not ln.is_num]
    legend_texts = set(pg.legend.values())
    labels = [ln for ln in labels if ln.text not in legend_texts
              and id(ln) not in getattr(pg, "legend_lines", ())]
    # « Base : ... », « Source : ... » ne sont jamais des libellés de ligne
    meta_lines = [ln for ln in labels if split_meta(ln.text)]
    labels = [ln for ln in labels if not split_meta(ln.text)]
    zone_area = zone.w * zone.h
    shapes = [s for s in pg.shapes if s.visible and inside(s.cx, s.cy) and id(s) not in pg.swatches]
    # Barres : rectangles pleins ; une barre de 1 % peut ne faire qu'un point de large
    # (pointes de flèche et pointillés : max < 4 pt, ignorés)
    bars = [s for s in shapes if s.rect and min(s.w, s.h) >= 0.8 and max(s.w, s.h) >= 4
            and s.area < 0.25 * zone_area and min(s.color) < 215]   # fond d'encadré pâle : non

    owner: dict[int, Shape] = {}            # id(valeur) -> forme
    series: dict[int, str] = {}             # id(valeur) -> libellé de légende
    held: dict[int, list[Word]] = {}        # id(forme) -> valeurs

    # 1) valeur DANS une forme dont la couleur est en légende (secteur, segment empilé)
    for v in values:
        # (un grand secteur peut déborder de la zone : seul le fond de page est exclu)
        cont = [s for s in shapes if s.contains(v.cx, v.cy) and s.area < 0.6 * pg.W * pg.H]
        cont.sort(key=lambda s: s.area)
        if cont and pg.legend_label(cont[0].color):
            s = cont[0]
            owner[id(v)], series[id(v)] = s, pg.legend_label(s.color)
            held.setdefault(id(s), []).append(v)
    if any(len(vs) > 1 for vs in held.values()):
        raise Unreadable("deux valeurs dans une même forme")

    # 2) valeur à côté d'une barre : à droite (horizontal) ou au-dessus (vertical).
    #    L'orientation est choisie UNE fois pour la zone (majorité), sinon la valeur d'une
    #    barre trop fine passerait pour la valeur « au-dessus » de la barre suivante.
    def h_cands(v):
        return [(v.l - s.r, s) for s in bars if id(s) not in held
                and s.b - 1 <= v.cy <= s.t + 1 and -1 <= v.l - s.r <= 25]

    def v_cands(v):
        return [(v.b - s.t, s) for s in bars if id(s) not in held
                and s.l - 1 <= v.cx <= s.r + 1 and -1 <= v.b - s.t <= 20]

    free = [v for v in values if id(v) not in owner]
    n_h = sum(1 for v in free if h_cands(v))
    n_v = sum(1 for v in free if v_cands(v))
    cands = h_cands if n_h >= n_v else v_cands
    for v in free:
        c = cands(v)
        if c:
            s = min(c, key=lambda x: x[0])[1]
            owner[id(v)] = s
            held.setdefault(id(s), []).append(v)
    if any(len(vs) > 1 for vs in held.values()):
        raise Unreadable("deux valeurs pour une même barre")
    # Toute barre doit porter une valeur, sinon on ne comprend pas le graphique
    orphan = [s for s in bars if id(s) not in held]
    if orphan:
        s = orphan[0]
        raise Unreadable(f"{len(orphan)} barre(s) sans valeur, ex. {s.color} "
                         f"[{s.l:.0f},{s.b:.0f},{s.r:.0f},{s.t:.0f}]")

    used: set[int] = set()                   # id(ligne de texte) consommée
    blocks: list[str] = []
    kinds: list[str] = []

    # 3) secteurs (camembert, anneau) : tableau dans l'ordre de la légende
    pie = [v for v in values if id(v) in owner and not owner[id(v)].rect]
    if pie:
        by_label = {series[id(v)]: v.text for v in pie}
        if len(by_label) != len(pie):
            raise Unreadable("deux secteurs de même couleur")
        order = [lab for lab in pg.legend_order() if lab in by_label]
        blocks.append(_md_table(["Libellé", "Valeur"], [[lab, by_label[lab]] for lab in order]))
        kinds.append("secteurs")

    # 4) barres (simples ou empilées) regroupées en lignes
    rect_vals = [v for v in values if id(v) in owner and owner[id(v)].rect]
    row_bars = _row_of([s for s in bars if id(s) in held])
    vertical = bool(rect_vals) and cands is v_cands and any(id(v) not in series
                                                            for v in rect_vals)
    rows = []
    for rb in row_bars:
        rows.append({"bars": rb, "l": min(s.l for s in rb), "r": max(s.r for s in rb),
                     "cy": rb[0].cy, "label": [], "group": "", "extra": {}})

    if rows:
        pitch = median([abs(a["cy"] - b_["cy"]) for a, b_ in zip(rows, rows[1:])
                        if abs(a["cy"] - b_["cy"]) > 2] or [30])
        # libellés de ligne
        for ln in labels:
            if vertical:
                cands = [rw for rw in rows if rw["l"] - 6 <= ln.cx <= rw["r"] + 6
                         and 0 <= min(s.b for s in rw["bars"]) - ln.t <= 30]
                key = (lambda rw: min(s.b for s in rw["bars"]) - ln.t)
            else:
                cands = [rw for rw in rows if ln.r <= rw["l"] + 2
                         and abs(ln.cy - rw["cy"]) <= max(0.55 * pitch, 6)]
                key = (lambda rw: (abs(ln.cy - rw["cy"]), rw["l"] - ln.r))
            if cands:
                min(cands, key=key)["label"].append(ln)
                used.add(id(ln))
        if any(not rw["label"] for rw in rows):
            rw = next(rw for rw in rows if not rw["label"])
            raise Unreadable(f"ligne sans libellé (y={rw['cy']:.0f})")
        # intertitres de groupe (« Sexe », « Âge »...) : au-dessus des lignes d'une colonne
        if not vertical:
            # Un intertitre est un texte court non utilisé, placé dans la colonne (au-dessus
            # des libellés) à moins de 1,6 pas de ligne au-dessus de la première ligne qui le
            # suit ; il vaut pour cette ligne et les suivantes de la colonne.
            # (pas une suite de phrase : « pensez-vous qu'elle est vraie ou fausse ? »)
            heads = [ln for ln in labels if id(ln) not in used and len(ln.text) <= 45
                     and not ln.text[:1].islower() and not ln.text.rstrip().endswith(("?", "…"))]
            for h in heads:
                below = [rw for rw in rows
                         if min(x.l for x in rw["label"]) - 30 <= h.cx <= rw["r"] + 10
                         and 0 < h.cy - rw["cy"] <= 1.6 * pitch]
                if below:
                    rw = min(below, key=lambda rw: h.cy - rw["cy"])
                    if not rw["group"]:
                        rw["group"] = h.text
                        used.add(id(h))
            # propage l'intertitre aux lignes suivantes de la même colonne
            _columns(rows)
            cols: dict[int, list] = {}
            for rw in rows:
                cols.setdefault(rw["col"], []).append(rw)
            for col_rows in cols.values():
                g = ""
                for rw in sorted(col_rows, key=lambda x: -x["cy"]):
                    g = rw["group"] or g
                    rw["group"] = g

    # 4 bis) années isolées au-dessus des lignes (« 2024 2020 2017 » d'un bloc de rappels) :
    #    ce sont des en-têtes de colonnes, pas des valeurs
    if rows and not vertical:
        top = max(rw["cy"] for rw in rows)
        years = [v for v in values if id(v) not in owner and YEAR.match(v.text) and v.cy > top]
        if years:
            labels += [Line(w.l, w.b, w.r, w.t, words=[w], size=w.size) for w in years]
            gone = {id(w) for w in years}
            values = [v for v in values if id(v) not in gone]

    # 5) colonnes de valeurs sans barre, sur la ligne d'une barre (« Total Oui »...)
    extra_cols: list[dict] = []
    for v in values:
        if id(v) in owner or vertical:
            continue
        rw = next((rw for rw in rows if abs(v.cy - rw["cy"]) <= 4 and v.l > rw["r"]), None)
        if rw is None:
            continue
        col = next((c for c in extra_cols if abs(c["cx"] - v.cx) <= 15), None)
        if col is None:
            col = {"cx": v.cx, "l": v.l, "r": v.r, "head": ""}
            extra_cols.append(col)
        col["l"], col["r"] = min(col["l"], v.l), max(col["r"], v.r)
        rw["extra"][id(col)] = v.text
        owner[id(v)] = rw["bars"][0]
    if extra_cols:
        top = max(rw["cy"] for rw in rows)
        for col in extra_cols:
            above = [ln for ln in labels if id(ln) not in used and ln.cy > top
                     and ln.l <= col["r"] + 12 and ln.r >= col["l"] - 12]
            if not above:
                continue
            first = min(above, key=lambda ln: ln.cy)
            stack = [first]
            if YEAR.match(first.text):        # en-tête « 2024 » : rien d'autre au-dessus
                above = []
            for ln in sorted(above, key=lambda ln: ln.cy):
                if ln is not first and 0 < ln.cy - stack[-1].cy <= 1.8 * ln.size:
                    stack.append(ln)
            col["head"] = " ".join(ln.text for ln in reversed(stack))
            used.update(id(ln) for ln in stack)

    # 5 bis) chiffre-clé : nombre isolé en très grand corps (« % Trop » / « 30 ») ; son
    #    intitulé est le texte juste au-dessus. Il devient une annotation « % Trop : 30 ».
    headline: list[tuple[str, Word]] = []
    owned = [v.size for v in values if id(v) in owner]
    if owned:
        big = [v for v in values if id(v) not in owner and v.size >= 1.8 * median(owned)]
        for v in big:
            parts, last = [], v
            for ln in sorted(labels, key=lambda x: x.cy):
                if (id(ln) not in used and not re.search(r"\d", ln.text)
                        and ln.r >= v.l - 80 and ln.l <= v.r + 80
                        and -2 <= ln.b - last.t <= 0.9 * ln.size + (6 if last is v else 0)):
                    parts.append(ln)
                    last = ln
            if parts:
                rows_: list[list[Line]] = []
                for ln in parts:           # même ligne (« % » et « Trop ») : gauche -> droite
                    if rows_ and abs(rows_[-1][0].cy - ln.cy) < 0.5 * ln.size:
                        rows_[-1].append(ln)
                    else:
                        rows_.append([ln])
                lab = " ".join(" ".join(x.text for x in sorted(r_, key=lambda x: x.l))
                               for r_ in reversed(rows_))
                headline.append((lab, v))
                used.update(id(ln) for ln in parts)
        gone = {id(v) for _, v in headline}
        values = [v for v in values if id(v) not in gone]

    # 6) annotations chiffrées (« Total Oui : 43% ») : lignes non utilisées portant un
    #    pourcentage ou « : nombre », précédées éventuellement d'un début de libellé
    #    juste au-dessus. (Une question qui cite une année reste un titre.)
    notes: list[str] = []
    # encadrés à puces (« ▸ 64% des moins de 35 ans contre » / « 53% de ceux qui... ») :
    # une puce = une annotation, avec ses lignes de suite (en retrait, juste dessous)
    by_y = sorted(labels, key=lambda x: -x.cy)
    def starts_note(t: str) -> bool:      # puce, ou « 19% chez ... » (encadré sans puce)
        return bool(BULLET.match(t) and re.search(r"\w", t)) or bool(PCT_LEAD.match(t))
    for ln in by_y:
        if id(ln) in used or not starts_note(ln.text):
            continue
        indent = 30 if BULLET.match(ln.text) else 3
        parts, last = [BULLET.sub("", ln.text)], ln
        used.add(id(ln))
        for o in by_y:
            # (sous une puce, seule une nouvelle puce ouvre une nouvelle annotation :
            #  « 53% de ceux qui... » est la suite de « ▸ 64% des moins de 35 ans contre »)
            if (id(o) not in used
                    and not (BULLET.match(o.text) if BULLET.match(ln.text) else starts_note(o.text))
                    and ln.l - 3 <= o.l <= ln.l + indent and (indent > 3 or o.l > ln.l - 3)
                    and (BULLET.match(ln.text) is None or o.l > ln.l)
                    and 0 < last.cy - o.cy <= 1.9 * o.size):
                parts.append(o.text)
                used.add(id(o))
                last = o
        notes.append(" ".join(parts))
    for ln in by_y:
        # (une longue phrase qui cite « 21% » est une question, pas une annotation)
        if (id(ln) in used or len(ln.text) > 80
                or not re.search(r"\d\s*%|:\s*\d", ln.text)):
            continue
        prev = [o for o in labels if id(o) not in used and o is not ln
                and not re.search(r"\d", o.text) and 0 < o.cy - ln.cy <= 1.8 * ln.size
                and o.l <= ln.r and o.r >= ln.l]
        txt = ln.text
        if prev:
            p = min(prev, key=lambda o: o.cy - ln.cy)
            txt = p.text + " " + txt
            used.add(id(p))
        notes.append(txt)
        used.add(id(ln))

    # Couverture : chaque nombre visible doit être expliqué
    lost = [v.text for v in values if id(v) not in owner]
    if lost:
        raise Unreadable("valeurs non rattachées : " + ", ".join(lost[:8]))

    if rows:
        stacked = sorted({series[id(v)] for v in rect_vals if id(v) in series},
                         key=lambda s: pg.legend_order().index(s) if s in pg.legend_order() else 99)
        heads = [c["head"] or f"Colonne {i + 1}" for i, c in enumerate(extra_cols)]
        # Colonnes de rappel (vague antérieure) : tableau à part
        hist = [i for i, h in enumerate(heads) if c_is_history(h)]
        main = [i for i in range(len(extra_cols)) if i not in hist]
        _columns(rows)
        top_row = max(rw["cy"] for rw in rows)

        def col_key(rw):
            # vertical : de gauche à droite ; horizontal : colonne par colonne, de haut en bas
            return (0, rw["l"]) if vertical else (rw["col"], -rw["cy"])
        # Un tableau par intertitre de groupe (« Sexe », « Âge »...) : l'intertitre devient
        # l'en-tête de la première colonne. Lignes sans intertitre : « Libellé ».
        segments: list[tuple[str, list[list[str]]]] = []
        hist_rows: list[list[str]] = []
        for rw in sorted(rows, key=col_key):
            lab = " ".join(ln.text for ln in sorted(rw["label"], key=lambda x: (-x.cy, x.l)))
            vals = [v for v in values if id(v) in owner and owner[id(v)] in rw["bars"]
                    and id(owner[id(v)]) in held and v in held[id(owner[id(v)])]]
            cells = [lab]
            if stacked:
                by = {series.get(id(v)): v.text for v in vals}
                cells += [by.get(s) or "–" for s in stacked]
            else:
                cells += [vals[0].text if vals else "–"]
            cells += [rw["extra"].get(id(extra_cols[i])) or "–" for i in main]
            if not segments or segments[-1][0] != rw["group"]:
                segments.append((rw["group"], []))
            segments[-1][1].append(cells)
            hv = [rw["extra"].get(id(extra_cols[i])) or "–" for i in hist]
            if hist and any(x != "–" for x in hv):
                hist_rows.append(([rw["group"]] if any(r["group"] for r in rows) else [])
                                 + [lab] + hv)
        tail = (stacked if stacked else ["Valeur"]) + [heads[i] for i in main]
        for group, seg_rows in segments:
            blocks.append(_md_table([group or "Libellé"] + tail, seg_rows))
        if hist_rows:
            gh = ["Groupe"] if any(r["group"] for r in rows) else []
            # intitulés posés au-dessus des colonnes de rappel (« Rappels », « % Vraie »)
            hl = min(extra_cols[i]["l"] for i in hist) - 12
            hr = max(extra_cols[i]["r"] for i in hist) + 12
            cap = [ln for ln in sorted(labels, key=lambda x: -x.cy) if id(ln) not in used
                   and ln.cy > top_row and hl <= ln.cx <= hr]
            hh = [heads[i] for i in hist]
            # Années répétées (« 2020 2017 | 2020 2017 ») : plusieurs mesures côte à côte ;
            # l'intitulé posé au-dessus de chaque groupe (« % Correspond à la réalité »)
            # préfixe ses années. Un texte à cheval sur deux groupes reste en légende.
            if len(set(hh)) < len(hh):
                groups: list[list[int]] = [[]]
                for k, i in enumerate(hist):
                    if heads[i] in [heads[j] for j in groups[-1]]:
                        groups.append([])
                    groups[-1].append(i)
                k = 0
                for g in groups:
                    gl = min(extra_cols[i]["l"] for i in g) - 8
                    gr = max(extra_cols[i]["r"] for i in g) + 8
                    sub = [ln for ln in cap if gl <= ln.l and ln.r <= gr]
                    name = " ".join(ln.text for ln in sub)
                    for i in g:
                        hh[k] = f"{name} {heads[i]}".strip()
                        k += 1
                    cap = [ln for ln in cap if ln not in sub]
                    used.update(id(ln) for ln in sub)
            used.update(id(ln) for ln in cap)
            table = _md_table(gh + ["Libellé"] + hh, hist_rows)
            blocks.append(("*" + " — ".join(ln.text for ln in cap) + "*\n\n" if cap else "")
                          + table)
        kinds.append("barres empilées" if stacked else
                     "barres verticales" if vertical else "barres")

    # Légende : libellés dont le carré est dans la zone mais qui ne figurent dans aucun
    # tableau (ex. « 2023 » pour une série de barres simples)
    shown = " ".join(blocks)
    legend = []
    for sw in pg.shapes:
        if id(sw) in pg.swatches and inside(sw.cx, sw.cy):
            lab = pg.legend_label(sw.color)
            if lab and lab not in legend and f"| {lab} |" not in shown:
                legend.append(lab)

    titles: list[str] = []
    prev = None
    for ln in sorted([*labels, *meta_lines], key=lambda x: (-x.cy, x.l)):
        if id(ln) in used or not re.search(r"\w", ln.text):
            continue
        # suite d'une phrase coupée en deux lignes (commence par une minuscule)
        if (prev is not None and titles
                and (ln.text[:1].islower()
                     or (len(titles[-1]) >= 50 and not titles[-1].rstrip().endswith(
                         (".", "?", "!", ":", ";", "…"))))
                and 0 < prev.cy - ln.cy <= 2 * ln.size and not split_meta(titles[-1])):
            titles[-1] += " " + ln.text
        else:
            titles.append(ln.text)
        prev = ln
    # « RAPPELS 2020 : 30% » « 2017 : 37% »... d'un chiffre-clé unique : petit tableau
    if len(headline) == 1:
        lab, v = headline[0]
        hist = [(m.group(1), m.group(2)) for n in notes
                for m in [re.fullmatch(r"(?:RAPPELS?\s+)?((?:19|20)\d\d)\s*:\s*(\d+(?:[.,]\d+)?\s*%?)",
                                       n.strip(), re.I)] if m]
        if len(hist) >= 2:
            notes = [n for n in notes if not re.fullmatch(
                r"(?:RAPPELS?\s+)?(?:19|20)\d\d\s*:\s*\d+(?:[.,]\d+)?\s*%?", n.strip(), re.I)]
            blocks.append("*Rappels*\n\n" + _md_table(["Année", lab], [[y, x] for y, x in hist]))
    notes = [f"{lab} : {v.text}" for lab, v in headline] + notes
    meta = ChartMeta(legend=legend, notes=notes)
    meta.add_texts(titles)
    body = "\n\n".join(blocks)
    return ChartResult(text=render(meta, body), kind=" + ".join(kinds), values=len(values),
                       meta=meta, body=body)


def c_is_history(head: str) -> bool:
    return bool(HISTORY_HEAD.search(head))


class VectorDocument:
    """Accès paresseux aux pages d'un PDF ; une page n'est analysée qu'une fois."""

    def __init__(self, pdf_path):
        self.pdf = pdfium.PdfDocument(str(pdf_path))
        self._pages: dict[int, VectorPage] = {}
        self.last_reason = ""            # pourquoi la dernière lecture a échoué

    def page(self, page_no: int) -> VectorPage:
        if page_no not in self._pages:
            self._pages[page_no] = VectorPage(self.pdf[page_no - 1])
        return self._pages[page_no]

    def read(self, page_no: int, l: float, b: float, r: float, t: float) -> ChartResult | None:
        """Tableau exact du graphique de la zone, ou None (raison dans `last_reason`)."""
        self.last_reason = ""
        try:
            return read_region(self.page(page_no), l, b, r, t)
        except Unreadable as e:
            self.last_reason = str(e)
            return None

    def close(self) -> None:
        self._pages.clear()
        self.pdf.close()
