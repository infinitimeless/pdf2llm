"""
chart_format.py — Format de sortie commun à tous les graphiques (lecture vectorielle,
Granite Vision, couche texte, modèle vision).

Un graphique devient :

    Titre : <titre lu dans le graphique>
    Question : *<question posée>*
    Base : <population interrogée>
    Légende : <libellés de légende non repris dans les tableaux>
    Source : <source>
    Annotations : <valeurs mises en avant hors tableau>
    Lecture : <provenance des chiffres, seulement s'ils sont lus par un modèle>

    | tableau 1 : un seul jeu de données homogène |

    | tableau 2 : autre jeu (autre groupe, rappels historiques...) |

Seuls les champs trouvés sont écrits. Les métadonnées viennent de la couche texte du PDF
(jamais d'un modèle). « Lecture » est absente pour une lecture exacte (géométrie du PDF) :
elle signale au LLM des chiffres lus par un modèle, non vérifiés : `split_meta` reconnaît « Question : », « Base : », « (base: ...) »
et « Source : ».
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

_BASE = re.compile(r"(?:^|(?<=[\s(?…]))\(?\s*base\s*:\s*", re.I)
_QUESTION = re.compile(r"^\s*questions?\s*:\s*", re.I)
_SOURCE = re.compile(r"^\s*sources?\s*:\s*", re.I)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in s if not unicodedata.combining(c)).lower())


def looks_like_question(s: str) -> bool:
    """Question non étiquetée : phrase assez longue terminée par « ? » ou « … »."""
    s = s.strip()
    return len(s) >= 25 and (s.endswith("?") or s.endswith("…") or s.endswith("..."))


def split_meta(text: str) -> dict[str, str] | None:
    """Champs de métadonnées d'un texte, ou None si le texte n'est PAS entièrement une
    métadonnée (on ne retire alors rien du document).

    « Question : ... ? Base : ensemble des Français » -> {question, base}
    « Avec laquelle ... ? (base: Ensemble de l'échantillon) » -> {question, base}
    « (base: Ensemble de l'échantillon) » -> {base}
    « Source : Ifop, octobre 2026 » -> {source}"""
    t = " ".join(text.split())
    if not t:
        return None
    m = _SOURCE.match(t)
    if m:
        return {"source": t[m.end():].strip()} if t[m.end():].strip() else None
    out: dict[str, str] = {}
    head = t
    b = _BASE.search(t)
    if b:
        base = t[b.end():].strip()
        if b.group(0).lstrip().startswith("(") or t[:b.start()].rstrip().endswith("("):
            base = base.rstrip(")").strip()
        if not base:
            return None
        out["base"] = base
        head = t[:b.start()].rstrip(" (")
    q = _QUESTION.match(head)
    if q:
        if not head[q.end():].strip():
            return None
        out["question"] = head[q.end():].strip()
    elif head:
        if b and looks_like_question(head):
            out["question"] = head
        else:
            return None          # texte libre : ce n'est pas (seulement) une métadonnée
    return out or None


@dataclass
class ChartMeta:
    title: list[str] = field(default_factory=list)
    question: str = ""
    base: str = ""
    legend: list[str] = field(default_factory=list)
    source: str = ""
    notes: list[str] = field(default_factory=list)
    lecture: str = ""             # provenance des chiffres (vide = lecture exacte)

    def _known(self) -> set[str]:
        return {norm(x) for x in [*self.title, self.question, self.base, *self.legend,
                                  self.source, *self.notes] if x}

    def add_fields(self, f: dict[str, str]) -> None:
        for k in ("question", "base", "source"):
            if f.get(k) and not getattr(self, k):
                setattr(self, k, f[k])

    def add_texts(self, texts: list[str]) -> None:
        """Textes lus dans le graphique : métadonnées reconnues, sinon titre."""
        for t in texts:
            if norm(t) in self._known():
                continue
            f = split_meta(t)
            if f:
                self.add_fields(f)
            else:
                self.title.append(t)
        # question non étiquetée lue dans le graphique : seulement à côté d'une base
        if self.base and not self.question:
            q = next((t for t in self.title if looks_like_question(t)), None)
            if q:
                self.title.remove(q)
                self.question = q

    def lines(self) -> list[str]:
        out = []
        if self.title:
            out.append("Titre : " + " ; ".join(self.title))
        if self.question:
            out.append("Question : *" + self.question + "*")
        if self.base:
            out.append("Base : " + self.base)
        if self.legend:
            out.append("Légende : " + " ; ".join(self.legend))
        if self.source:
            out.append("Source : " + self.source)
        if self.notes:
            out.append("Annotations : " + " ; ".join(self.notes))
        if self.lecture:
            out.append("Lecture : " + self.lecture)
        return out


def md_table(head: list[str], rows: list[list[str]]) -> str:
    esc = [[c.replace("|", "\\|") for c in r] for r in [head] + rows]
    return "\n".join(["| " + " | ".join(esc[0]) + " |", "|" + "---|" * len(head)]
                     + ["| " + " | ".join(r) + " |" for r in esc[1:]])


def render(meta: ChartMeta | None, body: str) -> str:
    """Bloc de métadonnées (une ligne par champ) puis le corps (tableaux)."""
    head = "\n".join(meta.lines()) if meta else ""
    return "\n\n".join(p for p in (head, body.strip()) if p)
