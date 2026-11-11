"""
controles.py — Contrôles de cohérence arithmétique des tableaux de sondage (v9, T5).

Les contrôles « les nombres existent sur la page » arrêtent les valeurs inventées, pas les
valeurs PERMUTÉES (debug_note § 4.3). Or un tableau de sondage porte une arithmétique
vérifiable :

- **totaux** : une colonne (ou une ligne) dont l'en-tête commence par « Total » (« Total
  Oui », « Total Proche ») vaut la somme d'une suite CONTIGUË d'au moins 2 valeurs sœurs,
  à ± 1 point près (arrondis) ;
- **somme à 100** : seulement pour un camembert / anneau (colonne des valeurs) et pour
  chaque ligne d'une barre empilée, à ± 2 points, ou plus si beaucoup de valeurs sont
  additionnées : chaque valeur arrondie à l'unité porte jusqu'à 0,5 point d'erreur, d'où
  une tolérance de max(2, 0,5 × nombre de valeurs) (6 segments : 97 reste possible). JAMAIS pour des barres de profil
  (« Hommes 52 %, Femmes 34 % ») : elles ne somment pas à 100.

v9 : on SIGNALE seulement, on ne rejette rien. Sur une lecture vectorielle (exacte par
construction), un signalement trahit un défaut du lecteur : c'est un auto-test.
"""
from __future__ import annotations

import re

TOTAL_TOL = 1.0
SUM_TOL = 2.0
_TOTAL = re.compile(r"^\s*\**\s*total\b", re.I)
_NUM = re.compile(r"^[-+−]?\d[\d   ]*(?:[.,]\d+)?\s*(?:%|pts?|points?)?$")
_DASH = {"–", "-", "—", ""}
# Colonne d'une vague antérieure (« Oct. 2021 », « Rappel 2025 », « vague 3 ») : jamais une
# partie d'un total (« Total » y désigne souvent le total des citations, réponses multiples)
_HISTORY = re.compile(r"rappel|\b(?:19|20)\d{2}\b|vague|évol|evol|écart|ecart|\bvs\b"
                      r"|précédent|precedent", re.I)


def num(cell: str) -> float | None:
    """« 52% » -> 52.0 ; « 1,8 » -> 1.8 ; non numérique (« – », « <1 », texte) -> None."""
    c = (cell or "").strip().replace("−", "-")
    if not _NUM.match(c):
        return None
    c = re.sub(r"[%a-z\s  ]", "", c, flags=re.I).replace(",", ".")
    try:
        return float(c)
    except ValueError:
        return None


def md_tables(md: str) -> list[list[list[str]]]:
    """Tableaux Markdown d'un texte : liste de lignes (en-tête compris, séparateur retiré)."""
    out: list[list[list[str]]] = []
    cur: list[list[str]] = []
    for line in md.splitlines() + [""]:
        st = line.strip()
        if st.startswith("|"):
            cells = [c.strip().replace("\\|", "|")
                     for c in re.split(r"(?<!\\)\|", st.strip("|"))]
            if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                cur.append(cells)
        elif cur:
            out.append(cur)
            cur = []
    return out


def _runs_match(vals: list[float | None], target: float, tol: float) -> bool:
    """Une suite contiguë d'au moins 2 valeurs (sans trou) somme-t-elle à `target` ?"""
    for i in range(len(vals)):
        s, n = 0.0, 0
        for v in vals[i:]:
            if v is None:
                break
            s, n = s + v, n + 1
            if n >= 2 and abs(s - target) <= tol:
                return True
    return False


def sum_tol(n: int) -> float:
    """Tolérance de la somme à 100 de n valeurs arrondies à l'unité."""
    return max(SUM_TOL, 0.5 * n)


def _fmt(v: float) -> str:
    return str(int(v)) if v == int(v) else str(v).replace(".", ",")


def controles_coherence(rows: list[list[str]], kind_hint: str = "",
                        n_series: int | None = None) -> list[str]:
    """Problèmes lisibles d'un tableau (en-tête compris), liste vide si tout est cohérent.

    kind_hint : "secteurs" (camembert, anneau), "empilees" (barres empilées) ou "" (autre :
    seuls les totaux sont contrôlés). n_series : nombre de colonnes de séries d'une barre
    empilée (lecture vectorielle) ; sinon, une ligne est cohérente si un préfixe d'au moins
    2 valeurs somme à 100 (les colonnes suivantes peuvent être « Ensemble », rappels...)."""
    if len(rows) < 2:
        return []
    head, body = rows[0], [r for r in rows[1:] if r]
    ncol = len(head)
    body = [r + [""] * (ncol - len(r)) for r in body]
    problems: list[str] = []
    total_cols = [j for j in range(1, ncol) if _TOTAL.match(head[j])]
    total_rows = {i for i, r in enumerate(body) if _TOTAL.match(r[0])}

    # 1) colonnes « Total ... » : somme d'une suite contiguë de valeurs sœurs de la ligne
    for j in total_cols:
        for r in body:
            tv = num(r[j])
            sib = [num(r[k]) for k in range(1, ncol)
                   if k not in total_cols and not _HISTORY.search(head[k])]
            if tv is None or sum(v is not None for v in sib) < 2:
                continue
            if not _runs_match(sib, tv, TOTAL_TOL):
                problems.append(f"total incohérent (« {r[0]} » : {head[j]} = {_fmt(tv)})")

    # 2) lignes « Total ... » : somme d'une suite contiguë des autres lignes, par colonne
    for i in sorted(total_rows):
        for j in range(1, ncol):
            tv = num(body[i][j])
            sib = [num(r[j]) for k, r in enumerate(body) if k not in total_rows]
            if tv is None or sum(v is not None for v in sib) < 2:
                continue
            if not _runs_match(sib, tv, TOTAL_TOL):
                problems.append(f"total incohérent ({body[i][0]} = {_fmt(tv)})")

    # 3) somme à 100
    plain = [r for k, r in enumerate(body) if k not in total_rows]
    if kind_hint == "secteurs" and ncol >= 2:
        vals = [num(r[1]) for r in plain]
        if len(vals) >= 2 and all(v is not None for v in vals):
            s = sum(vals)  # type: ignore[arg-type]
            if abs(s - 100) > sum_tol(len(vals)):
                problems.append(f"somme ≠ 100 (secteurs : {_fmt(round(s, 1))})")
    elif kind_hint == "empilees":
        for r in plain:
            if n_series:
                cells = r[1:1 + n_series]
                if any(num(c) is None and c.strip() not in _DASH for c in cells):
                    continue
                vals = [num(c) or 0.0 for c in cells]
                if sum(1 for c in cells if num(c) is not None) < 2:
                    continue
                s = sum(vals)
                if abs(s - 100) > sum_tol(len(vals)):
                    problems.append(f"somme ≠ 100 (« {r[0]} » : {_fmt(round(s, 1))})")
            else:
                vals = []
                for j in range(1, ncol):
                    if j in total_cols:
                        break
                    v = num(r[j])
                    if v is None:
                        break
                    vals.append(v)
                if len(vals) < 2:
                    continue
                sums = [sum(vals[:k]) for k in range(2, len(vals) + 1)]
                if not any(abs(s - 100) <= sum_tol(k) for k, s in enumerate(sums, 2)):
                    problems.append(f"somme ≠ 100 (« {r[0]} » : {_fmt(round(sums[-1], 1))})")
    return list(dict.fromkeys(problems))


def short_tags(problems: list[str]) -> str:
    """« total incohérent ; somme ≠ 100 » : nature des problèmes, pour la ligne Lecture."""
    return " ; ".join(dict.fromkeys(p.split(" (")[0] for p in problems))
