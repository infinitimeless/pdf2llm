#!/usr/bin/env python3
"""
pdf2llm.py — Convertit un dossier de PDF en Markdown léger, optimisé pour l'ingestion LLM.

Pipeline (tout local, open source) :
  1. Docling convertit chaque PDF (mise en page, tableaux, OCR si besoin).
  2. Docling classe chaque image (bar_chart, flow_chart, logo, photograph...).
  3. Graphiques de données  -> tableau de valeurs (modèle Granite Vision de Docling).
     Avant cela, si le graphique est VECTORIEL (formes + texte du PDF, cas des slides
     exportées de PowerPoint), il est lu exactement par la géométrie (vector_charts.py),
     sans modèle ; Granite et le modèle vision ne servent qu'en repli. Granite n'est
     exécuté que sur les pages dont un graphique n'a pas été lu (granite_fallback).
  0. Avant tout, un triage (pypdfium2) classe le PDF : texte, présentation ou scan.
  4. Logos, icônes, signatures, QR codes, images répétées, miniatures -> supprimés.
  5. Schémas et autres images -> envoyés à un modèle vision (LM Studio, Ollama, ou toute API
     compatible OpenAI) qui renvoie du Mermaid, un tableau ou une description texte,
     ou "DECORATIF" (alors l'image est supprimée).
  6. Les images conservées sont réduites (1024 px max) et recompressées en WebP, en lien local.

Sortie, pour chaque PDF :  <nom>.md, <nom>_assets/*.webp, <nom>.report.json
Reprise : un PDF dont le .md existe déjà est sauté (sauf --force). Les réponses du modèle
vision sont mises en cache (.vlm_cache.json), donc une relance ne refait pas les appels.

Exemples :
  python pdf2llm.py ./pdfs ./sortie --vlm-model qwen2.5-vl-7b-instruct
  python pdf2llm.py ./pdfs ./sortie --vlm-url http://localhost:11434/v1 --vlm-model qwen2.5vl:7b
  python pdf2llm.py ./pdfs ./sortie --no-vlm          # sans modèle vision
  python pdf2llm.py ./pdfs ./sortie --pages 1-15 --vlm-model ...   # test rapide
  python pdf2llm.py un.pdf ./sortie --pages 1-15 --vlm-model ... --dump-debug  # + sortie/_debug/
  python pdf2llm.py                                   # sans argument : assistant interactif
"""

from __future__ import annotations

import argparse
import base64
import csv
import difflib
import hashlib
import io
import json
import logging
import os
import re
import sys
import unicodedata
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import requests
from PIL import Image

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling_core.transforms.serializer.base import SerializationResult
from docling_core.transforms.serializer.common import create_ser_result
from docling_core.transforms.serializer.markdown import (
    MarkdownDocSerializer,
    MarkdownParams,
    MarkdownPictureSerializer,
    MarkdownTextSerializer,
)
from docling_core.types.doc import DoclingDocument, ImageRefMode, PictureItem

try:  # absent de certaines versions de docling-core : le repli tableau est alors désactivé
    from docling_core.transforms.serializer.markdown import MarkdownTableSerializer
except ImportError:  # pragma: no cover
    MarkdownTableSerializer = None  # type: ignore[assignment,misc]

try:  # lecture exacte des graphiques vectoriels (vector_charts.py, à côté de ce script)
    from vector_charts import VectorDocument
except ImportError:  # pragma: no cover
    VectorDocument = None  # type: ignore[assignment,misc]

from chart_format import ChartMeta, render as render_chart, split_meta, looks_like_question
from controles import controles_coherence, md_tables, short_tags

log = logging.getLogger("pdf2llm")

# --------------------------------------------------------------------------------------
# Triage par classe (étiquettes du classifieur DocumentFigureClassifier de Docling)
# --------------------------------------------------------------------------------------
CHART_LABELS = {
    "bar_chart", "line_chart", "pie_chart", "scatter_plot", "scatter_chart", "box_plot",
    "stacked_bar_chart", "other_chart", "heatmap",
}
DROP_LABELS = {
    "logo", "icon", "signature", "stamp", "bar_code", "qr_code", "page_thumbnail",
    "crossword_puzzle",
}
# Classes où l'on garde aussi l'image compressée (en plus du texte) avec --keep-images=visual
VISUAL_LABELS = {
    "photograph", "natural_image", "screenshot", "screenshot_from_computer",
    "screenshot_from_manual", "geographical_map", "topographical_map", "map",
    "engineering_drawing", "cad_drawing", "electrical_diagram", "remote_sensing",
    "chemistry_structure", "chemistry_molecular_structure", "chemistry_markush_structure",
}

PROMPT_VERSION = "v5"
# v2 : plus aucune demande de « tendance » ni d'interprétation (le modèle inventait des
# conclusions fausses) ; le cas « données chiffrées » passe avant le cas Mermaid.
VLM_PROMPT = """Tu reçois une image extraite d'un document PDF (classe estimée : {label}).
Ton texte remplacera l'image dans une version Markdown du document destinée à un LLM.
Tu TRANSCRIS ce qui est visible. Tu n'interprètes pas, tu ne conclus pas, tu ne commentes pas,
tu n'ajoutes aucune phrase de synthèse ni de tendance.
Choisis UNE seule forme de réponse :

1. Données chiffrées (graphique, anneau, barres, légende avec des valeurs, tableau) :
   un tableau Markdown, une ligne par libellé visible, avec la valeur exacte affichée
   (garde le signe %, ne recalcule rien). Si un titre est lisible, mets-le en première
   ligne en gras. Rien d'autre que le titre et le tableau.
   N'écris JAMAIS une valeur qui n'est pas imprimée dans l'image. S'il n'y a aucun chiffre
   (simple légende de libellés, couleurs), réponds uniquement DECORATIF.
2. Organigramme, processus, flux, architecture, hiérarchie, séquence, cycle ou carte mentale
   SANS valeurs chiffrées à restituer : Mermaid valide (flowchart TD/LR, sequenceDiagram,
   stateDiagram-v2, classDiagram, mindmap, timeline...). Reprends exactement les libellés
   visibles. Réponds uniquement par un bloc ```mermaid ... ```.
3. Autre image porteuse d'information (photo, capture d'écran, carte, plan...) :
   description factuelle et concise (2 à 6 phrases ou puces) ; transcris tout texte lisible.
4. Image purement décorative, sans information utile : réponds uniquement DECORATIF

Rédige dans la langue du texte visible dans l'image (français par défaut).
Aucun préambule, aucune phrase du type « Cette image montre »."""

VLM_TABLE_PROMPT = """Tu reçois une image extraite d'un document PDF (classe estimée : {label}).
Elle contient des données chiffrées. Transcris-les en un tableau Markdown : une ligne par
libellé visible, avec la valeur exacte affichée (garde le signe %, ne recalcule rien).
Si l'image montre plusieurs groupes (colonnes, séries, catégories), ajoute une colonne
« Groupe » ou utilise une colonne par série.
Si un titre est lisible, mets-le en première ligne en gras.
N'écris JAMAIS une valeur qui n'est pas imprimée dans l'image. S'il n'y a aucun chiffre
(simple légende de libellés, couleurs), réponds uniquement DECORATIF.
Réponds uniquement par le titre éventuel puis le tableau. Pas de Mermaid, pas de phrase de
commentaire, pas de tendance, pas d'interprétation."""

# Classes d'images dont le contenu est chiffré : on demande directement un tableau au VLM
TABLE_MODE_LABELS = CHART_LABELS | {"table"}

# Provenance des chiffres lus par un modèle (ligne « Lecture : » du bloc, v9). Une lecture
# exacte (géométrie du PDF) n'a pas de ligne : c'est le cas par défaut, sans token.
READERS = {
    "granite": "modèle (Granite Vision)",
    "vlm": "modèle vision",
    "2": "2 modèles concordants",
}


VECTOR_CHECK = "incohérence dans une lecture vectorielle (défaut du lecteur, à signaler)"
SCAN_BANNER = "> PDF sans couche texte (scan ou capture) : chiffres lus par un modèle, non vérifiés."
SCAN_UNREAD = "*Graphique non lu (PDF sans couche texte)*"


def lecture_text(reader: str, scan: bool = False) -> str:
    """« Lecture : » d'un graphique ou d'un tableau dont les chiffres viennent d'un modèle.
    PDF sans couche texte (scan) : aucun contrôle n'a pu vérifier ces chiffres."""
    if scan:
        return f"{READERS[reader]}, PDF sans couche texte, chiffres invérifiables"
    return f"{READERS[reader]}, valeurs non vérifiées"


def has_md_table(text: str) -> bool:
    return any(l.lstrip().startswith("|") for l in text.splitlines())


def vlm_reads_numbers(label: str, text: str) -> bool:
    """La sortie du modèle vision porte-t-elle des chiffres lus par lui ? Tableau, ou texte
    chiffré d'un graphique / tableau. Pas un Mermaid ni une description de photo."""
    if not text or text.startswith("```mermaid"):
        return False
    return has_md_table(text) or (label in TABLE_MODE_LABELS and bool(re.search(r"\d", text)))


def model_checks(text: str, label: str) -> list[str]:
    """Contrôles de cohérence (controles.py) des tableaux lus par un modèle. La somme à 100
    n'est appliquée qu'aux camemberts et aux barres empilées (classe du classifieur)."""
    hint = {"pie_chart": "secteurs", "stacked_bar_chart": "empilees"}.get(label, "")
    out: list[str] = []
    for rows in md_tables(text):
        out += controles_coherence(rows, hint)
    return list(dict.fromkeys(out))


def vector_checks(body: str, kind: str, series: list[str]) -> list[str]:
    """Auto-test d'une lecture vectorielle : exacte par construction, elle doit toujours
    passer les contrôles. Tableau des secteurs : premier tableau « Libellé | Valeur » ;
    barres empilées : tableaux dont les colonnes suivent les séries de la légende."""
    out: list[str] = []
    for i, rows in enumerate(md_tables(body)):
        head = rows[0]
        if series and head[1:1 + len(series)] == series:
            out += controles_coherence(rows, "empilees", len(series))
        elif i == 0 and "secteurs" in kind and head == ["Libellé", "Valeur"]:
            out += controles_coherence(rows, "secteurs")
        else:
            out += controles_coherence(rows, "")
    return list(dict.fromkeys(out))


# --------------------------------------------------------------------------------------
# Utilitaires image
# --------------------------------------------------------------------------------------
def downscale(img: Image.Image, max_side: int) -> Image.Image:
    img = img.convert("RGB") if img.mode not in ("RGB", "L") else img
    w, h = img.size
    if max(w, h) > max_side:
        r = max_side / max(w, h)
        img = img.resize((max(1, round(w * r)), max(1, round(h * r))), Image.LANCZOS)
    return img


def ahash(img: Image.Image) -> str:
    """Empreinte d'image répétée (logo d'en-tête, puce...) : taille + vignette couleur
    quantifiée. Tolère les petits écarts de rendu, pas les images réellement différentes."""
    w, h = img.size
    small = img.convert("RGB").resize((24, 24), Image.BILINEAR)
    q = bytes(v >> 4 for v in small.tobytes())
    return f"{w // 8}x{h // 8}:" + hashlib.md5(q).hexdigest()


def to_b64_jpeg(img: Image.Image, max_side: int) -> str:
    buf = io.BytesIO()
    downscale(img, max_side).save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode()


def save_webp(img: Image.Image, path: Path, max_side: int, quality: int) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    downscale(img, max_side).save(path, format="WEBP", quality=quality, method=6)
    return path.stat().st_size


# --------------------------------------------------------------------------------------
# Client du modèle vision (API compatible OpenAI : LM Studio, Ollama, vLLM, OpenAI...)
# --------------------------------------------------------------------------------------
class VlmClient:
    def __init__(self, url: str, model: str, api_key: str | None, timeout: float,
                 max_side: int, cache_path: Path, max_tokens: int):
        self.endpoint = url.rstrip("/")
        if not self.endpoint.endswith("/chat/completions"):
            self.endpoint += "/chat/completions"
        self.model = model
        self.headers = {"Content-Type": "application/json"}
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"
        self.timeout = timeout
        self.max_side = max_side
        self.max_tokens = max_tokens
        self.cache_path = cache_path
        self._lock = threading.Lock()
        try:
            self.cache: dict[str, str] = json.loads(cache_path.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self.cache = {}

    def save_cache(self) -> None:
        with self._lock:
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.cache, ensure_ascii=False), "utf-8")
            tmp.replace(self.cache_path)

    def ask(self, img: Image.Image, label: str, mode: str = "auto", hint: str = "") -> str | None:
        """mode = "auto" (consigne complète) ou "table" (données chiffrées : tableau seul)."""
        b64 = to_b64_jpeg(img, self.max_side)
        key = hashlib.sha1(
            f"{PROMPT_VERSION}|{self.model}|{mode}|{label}|{hint}|{b64}".encode()).hexdigest()
        if key in self.cache:
            return self.cache[key]
        prompt = VLM_TABLE_PROMPT if mode == "table" else VLM_PROMPT
        if hint and mode == "table":
            prompt += ("\n\nTextes déjà extraits de la page à cet endroit (fiables ; utilise-les "
                       "pour les libellés et les valeurs, n'en invente pas d'autres) :\n" + hint)
        payload = {
            "model": self.model,
            "temperature": 0.1,
            "max_tokens": self.max_tokens,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt.replace("{label}", label or "inconnue")},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }],
        }
        for attempt in range(3):
            try:
                r = requests.post(self.endpoint, json=payload, headers=self.headers,
                                  timeout=self.timeout)
                r.raise_for_status()
                text = r.json()["choices"][0]["message"]["content"] or ""
                text = clean_vlm_output(text)
                if mode == "table":
                    text = keep_table_only(text)
                with self._lock:
                    self.cache[key] = text
                return text
            except Exception as e:  # noqa: BLE001
                log.warning("Appel VLM échoué (essai %d/3) : %s", attempt + 1, e)
                time.sleep(2 * (attempt + 1))
        return None


def clean_vlm_output(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)  # modèles « raisonnants »
    text = text.strip()
    # Si un bloc mermaid est présent, on ne garde que lui
    m = re.search(r"```mermaid\s*\n(.*?)```", text, flags=re.S)
    if m:
        return "```mermaid\n" + m.group(1).strip() + "\n```"
    # Retire un éventuel emballage ```markdown ... ```
    m = re.fullmatch(r"```(?:markdown|md)?\s*\n(.*?)```", text, flags=re.S)
    if m:
        text = m.group(1).strip()
    return text


def is_decorative(text: str) -> bool:
    t = re.sub(r"[^A-ZÉa-zé]", "", text).upper()
    return t in {"DECORATIF", "DÉCORATIF", "DECORATIVE"} or (
        len(text) < 40 and "DECORATI" in t)


# --------------------------------------------------------------------------------------
# Contrôles qualité (tableaux Granite, sorties du VLM)
# --------------------------------------------------------------------------------------
NUM_RE = re.compile(r"^[-+−]?\s*\d[\d \u202f\u00a0]*([.,]\d+)?\s*(%|pts?|points?|€|k€|M€)?$")


def is_num(cell: str) -> bool:
    return bool(NUM_RE.match(cell.strip()))


def chart_rows_problem(rows: list[list[str]]) -> str | None:
    """Renvoie la raison pour laquelle un tableau issu de Granite est suspect, sinon None.
    Cas visé : libellés disparus, valeur manquante, bulles mêlées aux catégories (p. 9)."""
    if len(rows) < 2:
        return "tableau trop court"
    if max(len(r) for r in rows) < 2:
        return "une seule colonne"
    body = rows[1:]
    for r in body:
        if not r[0].strip():
            return "libellé vide"
        if not any(is_num(c) for c in r[1:]):
            return f"ligne sans valeur numérique ({r[0][:30]})"
    last_blank = sum(1 for r in body if not r[-1].strip())
    if last_blank / len(body) > 0.34:
        return "dernière colonne trop souvent vide"
    return None


def vlm_output_problem(text: str) -> str | None:
    """Rejette les Mermaid inutiles : diagramme de classes vide, ou nœuds qui sont des
    nombres / pourcentages (données chiffrées déguisées en schéma)."""
    m = re.match(r"```mermaid\s*\n(.*?)```", text, flags=re.S)
    if not m:
        return None
    body = m.group(1).strip()
    lines = body.splitlines()
    first = lines[0].strip() if lines else ""
    rest = "\n".join(lines[1:])
    if first.startswith("classDiagram"):
        has_members = re.search(r"\{\s*[^}\s]", rest) or re.search(r"--|\.\.|:\s*\S", rest)
        if not has_members:
            return "classDiagram vide"
    labels = re.findall(r"\[\"?([^\]\"]+)\"?\]|\(\(?([^()]+)\)?\)", body)
    labels = [a or b for a, b in labels]
    if len(labels) >= 2 and sum(is_num(x) for x in labels) / len(labels) >= 0.4:
        return "nœuds numériques (données chiffrées)"
    return None


def split_sparse(rows: list[list[str]]) -> list[list[list[str]]] | None:
    """Granite fusionne parfois deux graphiques voisins (p. 8 et 13 : proximité partisane +
    vote 2022) en un tableau [libellé | A | B] où chaque ligne n'a qu'UNE colonne remplie.
    On le scinde alors en un tableau par colonne, sans perdre de ligne."""
    head, body = rows[0], rows[1:]
    ncol = len(head)
    if ncol < 3 or not body:
        return None
    groups: list[list[list[str]]] = [[] for _ in range(ncol)]
    for r in body:
        r = r + [""] * (ncol - len(r))
        filled = [i for i in range(1, ncol) if r[i].strip()]
        if not r[0].strip() or len(filled) != 1:
            return None
        groups[filled[0]].append([r[0], r[filled[0]]])
    tables = [[[head[0], head[i].replace("_", " ")]] + g for i, g in enumerate(groups) if i and g]
    return tables if len(tables) >= 2 else None


def page_numbers(doc: DoclingDocument) -> dict[int, set[str]]:
    """Nombres présents dans la couche texte de chaque page (référence anti-invention)."""
    out: dict[int, set[str]] = {}
    for t in getattr(doc, "texts", []):
        if not t.prov:
            continue
        nums = out.setdefault(t.prov[0].page_no, set())
        for m in re.findall(r"\d+(?:[.,]\d+)?", t.text or ""):
            nums.add(m.replace(",", "."))
    return out


def invented_values(text: str, nums: set[str], max_missing: float = 0.5) -> str | None:
    """Si la page a une couche texte chiffrée, toute valeur d'un tableau du VLM qui n'y
    figure pas est suspecte (p. 9 : 0 %, 25 %, 50 %, 75 % inventés pour une légende)."""
    if len(nums) < 5:
        return None
    vals = []
    for l in text.splitlines():
        if l.strip().startswith("|"):
            for c in l.strip().strip("|").split("|"):
                if is_num(c):
                    m = re.search(r"\d+(?:[.,]\d+)?", c)
                    vals.append(m.group(0).replace(",", ".") if m else "")
    if len(vals) < 2:
        return None
    missing = [v for v in vals if v not in nums]
    if len(missing) / len(vals) >= max_missing:
        return "valeurs absentes de la page : " + ", ".join(missing[:6])
    return None


def text_items_near(doc: DoclingDocument, pic: PictureItem, margin: float = 0.0,
                    max_len: int = 40) -> list[tuple[str, float, float, float, float]]:
    """Textes courts de la couche texte du PDF dont le centre tombe dans la boîte de l'image
    (élargie de `margin` points), dans l'ordre de lecture : (texte, gauche, droite, haut, bas).
    Docling les avale dans l'image (p. 9 : 2 %, 11 %, 34 %, 53 % de l'anneau, légende, titre)."""
    try:
        pv = pic.prov[0]
        bb = pv.bbox
        l, r = min(bb.l, bb.r) - margin, max(bb.l, bb.r) + margin
        lo, hi = min(bb.b, bb.t) - margin, max(bb.b, bb.t) + margin
        items = []
        for t in doc.texts:
            if not t.prov or t.prov[0].page_no != pv.page_no or len(t.text or "") > max_len:
                continue
            tb = t.prov[0].bbox
            cx, cy = (tb.l + tb.r) / 2, (tb.b + tb.t) / 2
            if l <= cx <= r and lo <= cy <= hi:
                items.append((-cy, t.text.strip(), tb.l, tb.r, tb.t, tb.b))
        items.sort(key=lambda x: (x[0], x[2]))
        return [(x[1], x[2], x[3], x[4], x[5]) for x in items]
    except Exception:  # noqa: BLE001
        return []


def texts_near(doc: DoclingDocument, pic: PictureItem, margin: float = 0.0,
               max_len: int = 40) -> list[str]:
    return [x[0] for x in text_items_near(doc, pic, margin, max_len)]


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in s if not unicodedata.combining(c)).lower())


def uncovered_titles(inside: list[str], md: str) -> list[str]:
    """Textes sans chiffre de l'image (titre, sous-titre) absents de la sortie : Docling les
    avale avec l'image (p. 9 : « Qui se dit proche de ses idées ? Selon l'âge »)."""
    nm = _norm(md)
    return [t for t in inside if len(t) >= 4 and not re.search(r"\d", t) and _norm(t) not in nm]


def pie_text_block(doc: DoclingDocument, pic: PictureItem) -> str | None:
    """Camembert / anneau dont Granite n'a pas donné de tableau fiable : un VLM 7B associe
    mal valeurs et libellés quand le lien passe par la légende (p. 9 : 53 % attribué à
    « Très proche » au lieu de « Très éloigné »). On restitue donc fidèlement le texte de
    la couche PDF, SANS inventer l'association."""
    items = text_items_near(doc, pic, 80)
    merged: list[str] = []
    i = 0
    while i < len(items):
        t = items[i]
        if (i + 1 < len(items) and not re.search(r"\d", t[0]) and ":" in items[i + 1][0]
                and re.search(r"\d", items[i + 1][0]) and abs(t[1] - items[i + 1][1]) < 20
                and -2 <= t[4] - items[i + 1][3] < 12):
            merged.append(t[0] + " " + items[i + 1][0])     # « Ensemble des » + « Français : 6% »
            i += 2
            continue
        merged.append(t[0])
        i += 1
    vals: list[str] = []
    labelled: list[str] = []
    legend: list[str] = []
    for t in merged:
        if re.fullmatch(r"[\d\s%.,]+", t) and "%" in t:
            vals += [re.sub(r"\s+", "", v) for v in re.findall(r"\d+(?:[.,]\d+)?\s*%", t)]
        elif re.search(r"\d", t):
            labelled.append(t)
        else:
            legend.append(t)
    if not vals:
        return None
    vals.sort(key=lambda v: float(v.rstrip("%").replace(",", ".")))
    total = sum(float(v.rstrip("%").replace(",", ".")) for v in vals)
    lines = ["*Données lues dans le graphique (le rattachement valeur ↔ libellé n'est pas établi) :*"]
    if legend:
        lines.append("- Légende : " + " ; ".join(legend))
    lines.append("- Valeurs : " + " ; ".join(vals) + (" (total 100%)" if abs(total - 100) <= 1 else ""))
    lines += ["- " + t for t in labelled]
    return "\n".join(lines)


def uncovered_texts(inside: list[str], md: str) -> list[str]:
    """Textes de l'image contenant un nombre qui n'apparaît pas dans la sortie du VLM."""
    have = {m.replace(",", ".") for m in re.findall(r"\d+(?:[.,]\d+)?", md)}
    out = []
    for t in inside:
        nums = [m.replace(",", ".") for m in re.findall(r"\d+(?:[.,]\d+)?", t)]
        if nums and any(n not in have for n in nums):
            out.append(t)
    return out


def irregular_tables(doc: DoclingDocument) -> list:
    """Tableaux dont la grille semble décalée (p. 4 : taille d'échantillon en dernière
    colonne, deux nombres collés dans une cellule)."""
    res = []
    try:
        from docling_core.types.doc import TableItem
        for it, _ in doc.iterate_items():
            if not isinstance(it, TableItem):
                continue
            grid = [[(c.text or "").strip() for c in row] for row in it.data.grid]
            if len(grid) < 3:
                continue
            merged = any(re.fullmatch(r"\d{1,3}(?: \d{3})? \d+,\d+", c) for r in grid for c in r)
            last_full = sum(1 for r in grid[1:] if r[-1])
            ragged = 0 < last_full < len(grid) - 1 and last_full / (len(grid) - 1) > 0.5
            if merged or ragged:
                res.append(it)
    except Exception:  # noqa: BLE001
        pass
    return res


def fixed_table_md(grid: list[list[str]]) -> str | None:
    """Correctif déterministe du tableau « marges d'erreur » des études Ifop : Docling met la
    colonne « taille d'échantillon » en DERNIÈRE colonne (les en-têtes sont alors décalés) et
    fusionne « 3 000 0,8 » dans une cellule. On remet la colonne en tête, on retire la ligne
    de tirets et le préfixe répété dans chaque en-tête. Renvoie None si le motif n'est pas
    exactement celui-là (alors on ne touche à rien)."""
    rows = [r for r in grid if not all(c in ("", "-", "–", "—") for c in r)]
    if len(rows) < 3:
        return None
    # La grille brute de Docling a un en-tête sur plusieurs lignes (titre répété, « Taille
    # d'échantillon / Pourcentage observé », puis « 5 ou 95% »...) : on les fusionne par
    # colonne jusqu'à la première ligne chiffrée, comme le fait le sérialiseur Markdown.
    def numeric_row(r: list[str]) -> bool:
        n = sum(1 for c in r if is_num(c) or re.fullmatch(r"\d{1,3}(?: \d{3})? \d+,\d+", c.strip()))
        return n >= max(2, len(r) // 2)
    k = next((i for i, r in enumerate(rows) if numeric_row(r)), None)
    if not k:
        return None
    head = []
    for j in range(len(rows[0])):
        parts: list[str] = []
        for r in rows[:k]:
            c = r[j].strip() if j < len(r) else ""
            if c and (not parts or parts[-1] != c):
                parts.append(c)
        head.append(" - ".join(parts))
    body = rows[k:]
    if not re.search(r"taille|effectif|échantillon", head[0], re.I):
        return None
    fixed: list[list[str]] = []
    for r in body:
        m = re.fullmatch(r"(\d{1,3}(?: \d{3})?) (\d+,\d+)", r[0].strip())
        if m and not r[-1].strip():
            fixed.append([m.group(1), m.group(2)] + r[1:-1])
        elif re.fullmatch(r"\d{1,3}(?: \d{3})?", r[-1].strip()):
            fixed.append([r[-1].strip()] + r[:-1])
        else:
            return None
    sizes = [int(r[0].replace(" ", "")) for r in fixed]
    if len(set(len(r) for r in fixed)) != 1 or len(fixed[0]) != len(head) \
            or any(a >= b for a, b in zip(sizes, sizes[1:])):
        return None
    title = None
    pref = os.path.commonprefix(head)
    k = pref.rfind(" - ")
    if k > 10:
        title, head = pref[:k].strip(), [h[k + 3:] for h in head]
    md = "\n".join(["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
                   + ["| " + " | ".join(r) + " |" for r in fixed])
    return f"**{title}**\n\n{md}" if title else md


def vector_tables(doc: DoclingDocument, vec: "VectorDocument | None",
                  warnings: list[str] | None = None) -> dict[str, str]:
    """« Tableaux » de Docling qui sont en fait des graphiques vectoriels (barres empilées
    d'Ipsos lues comme une grille, colonnes décalées) : relus par la géométrie du PDF.
    Retenu seulement si TOUS les nombres de la zone sont rattachés à une barre ou à une
    colonne (sinon `read` renvoie None et le tableau de Docling est gardé)."""
    out: dict[str, str] = {}
    if vec is None:
        return out
    from docling_core.types.doc import TableItem
    for it, _ in doc.iterate_items():
        if not isinstance(it, TableItem) or not it.prov:
            continue
        try:
            res = vec.read(it.prov[0].page_no, *_bl_box(doc, it.prov[0]))
        except Exception as e:  # noqa: BLE001
            res = None
            log.debug("lecture vectorielle du tableau impossible : %s", e)
        if res is not None and res.kind:
            out[it.self_ref] = res.text
            for prob in vector_checks(res.body, res.kind, res.series):
                if warnings is not None:
                    warnings.append(f"p. {it.prov[0].page_no} : {VECTOR_CHECK} : {prob}")
            log.info("    p.%s : tableau Docling remplacé par le graphique vectoriel (%s)",
                     it.prov[0].page_no, res.kind)
    return out


def decide_tables(doc: DoclingDocument, args: argparse.Namespace, vlm: "VlmClient | None",
                  pnums: dict[int, set[str]],
                  skip: set[str] | None = None,
                  scan: bool = False) -> dict[str, tuple[int, str, str]]:
    """Tableaux à grille irrégulière : 1) correctif déterministe si le motif est reconnu ;
    2) sinon relecture de l'image par le VLM, retenue seulement si elle a au moins autant de
    lignes (à 2 près) et si ses nombres figurent dans la couche texte de la page ;
    3) sinon on garde la version de Docling. `skip` : déjà lus en vectoriel. `scan` : PDF
    sans couche texte (avec --scan-charts image, pas de relecture par le modèle).
    Renvoie {ref: (page, markdown, source)}, source = "regle" (sans modèle) ou "vlm"."""
    out: dict[str, tuple[int, str, str]] = {}
    for it in irregular_tables(doc):
        if it.self_ref in (skip or set()):
            continue
        try:
            page = it.prov[0].page_no
            grid = [[(c.text or "").strip() for c in row] for row in it.data.grid]
            md = fixed_table_md(grid)
            if md:
                out[it.self_ref] = (page, md, "regle")
                log.info("    p.%s : tableau décalé corrigé (colonne de tête remise en place)", page)
                continue
            if vlm is None or args.no_table_fallback:
                continue
            if scan and getattr(args, "scan_charts", "tag") == "image":
                continue
            img = it.get_image(doc)
            if img is None:
                continue
            text = vlm.ask(img, "table", "table")
            if not text or not any(l.strip().startswith("|") for l in text.splitlines()):
                continue
            nrows = sum(1 for l in text.splitlines() if l.strip().startswith("|")) - 1
            if nrows < it.data.num_rows - 2:
                log.info("    p.%s : tableau irrégulier, relecture VLM trop courte -> Docling", page)
                continue
            if invented_values(text, pnums.get(page, set()), 0.1):
                log.info("    p.%s : tableau irrégulier, relecture VLM douteuse -> Docling", page)
                continue
            out[it.self_ref] = (page, "Lecture : " + lecture_text("vlm", scan) + "\n\n" + text,
                                 "vlm")
            log.info("    p.%s : tableau irrégulier remplacé par la relecture du VLM", page)
        except Exception as e:  # noqa: BLE001
            log.warning("Correction de tableau impossible : %s", e)
    return out


def keep_table_only(text: str) -> str:
    """Mode « tableau » : ne garde qu'un titre en gras éventuel et le tableau Markdown,
    ce qui supprime structurellement toute phrase d'interprétation."""
    lines = text.splitlines()
    idx = [i for i, l in enumerate(lines) if l.strip().startswith("|")]
    if not idx:
        return text
    a, b = idx[0], idx[0]
    for i in idx[1:]:
        if i == b + 1:
            b = i
        else:
            break
    out = lines[a:b + 1]
    if a > 0 and lines[a - 1].strip().startswith("**"):
        out.insert(0, lines[a - 1].strip())
    elif a > 1 and not lines[a - 1].strip() and lines[a - 2].strip().startswith("**"):
        out.insert(0, lines[a - 2].strip())
    return "\n".join(out)


def denoise_markdown(md: str) -> tuple[str, int]:
    """Retire le bruit des slides : numéros de page isolés, numéros de section « 01 »,
    logo lu par l'OCR (ifop, Oifop, ifeapp...). Le logo est repéré tout seul : mot court
    répété sur au moins 3 lignes, puis variantes proches."""
    lines = md.split("\n")
    short = [l.strip().lower() for l in lines if re.fullmatch(r"[A-Za-zÀ-ÿ]{3,8}", l.strip())]
    counts: dict[str, int] = {}
    for t in short:
        counts[t] = counts.get(t, 0) + 1
    logo = None
    if counts:
        top, n = max(counts.items(), key=lambda kv: kv[1])
        if n >= 3 and top.islower():
            logo = top

    def is_logo(tok: str) -> bool:
        t = tok.lower()
        return bool(logo) and (t == logo or difflib.SequenceMatcher(None, t, logo).ratio() >= 0.55) \
            and len(t) <= len(logo) + 3

    out: list[str] = []
    removed = 0
    for l in lines:
        st = l.strip()
        # Titres repris de la couche texte (« *A ; B* ») : le logo lu par l'OCR (« Oifop »)
        # peut s'y glisser, car il est dans la boîte de l'image.
        m = re.fullmatch(r"\*([^*].*?)\*", st)
        if logo and m:
            parts = [x.strip() for x in m.group(1).split(" ; ")]
            kept = [x for x in parts if not all(is_logo(t) for t in x.split())]
            if len(kept) < len(parts):
                removed += 1
                if kept:
                    out.append("*" + " ; ".join(kept) + "*")
                continue
        if st.startswith(("#", "|", "```", "-", "*", "!")) or not st:
            out.append(l)
            continue
        if re.fullmatch(r"\d{1,3}", st):        # numéro de slide / de section
            removed += 1
            continue
        toks = st.split()
        if logo and all(is_logo(t) for t in toks):
            removed += 1
            continue
        if logo and len(toks) > 1 and is_logo(toks[0]) and not (toks[0][0].isupper() and toks[0][1:].islower()):
            out.append(" ".join(toks[1:]))
            removed += 1
            continue
        out.append(l)
    res = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", res), removed


def table_warnings(doc: DoclingDocument, fixed: set[str] | None = None) -> list[str]:
    """Tableaux irréguliers NON corrigés (les corrigés sont signalés à part)."""
    return [f"p. {it.prov[0].page_no if it.prov else '?'} : tableau à grille irrégulière, "
            "à vérifier sur le PDF" for it in irregular_tables(doc)
            if it.self_ref not in (fixed or set())]


# --------------------------------------------------------------------------------------
# Correctif Docling : CSV de Granite Vision avec libellés contenant une virgule (P1)
# --------------------------------------------------------------------------------------
def tolerant_csv_to_dataframe(decoded_text: str):
    """Remplace docling...granite_vision._extract_csv_to_dataframe.
    Granite n'entoure pas de guillemets les libellés contenant une virgule
    (« Oui, et vous voyez... »). On lit d'abord avec le module csv ; si une ligne a trop de
    champs, on la découpe par la droite : les valeurs sont à droite, le libellé à gauche."""
    import pandas as pd
    m = re.search(r"```csv\s*\n(.*?)\n```", decoded_text, re.DOTALL)
    if m:
        content = m.group(1).strip()
    else:
        content = re.sub(r"^```+(?:csv)?\s*", "", decoded_text.strip())
        content = re.sub(r"```+\s*$", "", content).strip()
    lines = [l for l in content.splitlines() if l.strip()]
    if not lines:
        raise ValueError("CSV vide")
    n = len(next(csv.reader([lines[0]])))
    rows: list[list[str]] = []
    for line in lines:
        try:
            cells = next(csv.reader([line]))
        except Exception:  # noqa: BLE001
            cells = [line]
        if len(cells) != n:
            cells = line.rsplit(",", n - 1) if n > 1 else [line]
        cells = [c.strip() for c in cells]
        cells += [""] * (n - len(cells))
        rows.append(cells)
    return pd.DataFrame(rows)


def install_granite_csv_patch() -> None:
    try:
        import docling.models.stages.chart_extraction.granite_vision as gv
    except Exception as e:  # noqa: BLE001
        log.warning("Correctif CSV Granite non installé (module introuvable : %s)", e)
        return
    if not hasattr(gv, "_extract_csv_to_dataframe"):
        log.warning("Correctif CSV Granite non installé : fonction absente de cette version "
                    "de Docling")
        return
    gv._extract_csv_to_dataframe = tolerant_csv_to_dataframe
    log.info("Correctif CSV Granite installé (libellés avec virgule)")


# --------------------------------------------------------------------------------------
# Mesures : durée par étape, pic de mémoire (T1, v9)
# --------------------------------------------------------------------------------------
STAGES = ("triage", "docling", "vectoriel", "granite", "vlm", "tableaux", "serialisation")


class Chrono:
    """Cumule des durées (secondes, horloge murale) par étape. Les appels parallèles au
    modèle vision sont chronométrés en bloc (durée du lot), pas additionnés par fil."""

    def __init__(self) -> None:
        self.durees: dict[str, float] = {k: 0.0 for k in STAGES}
        self._lock = threading.Lock()

    def add(self, stage: str, seconds: float) -> None:
        with self._lock:
            self.durees[stage] = self.durees.get(stage, 0.0) + seconds

    def __call__(self, stage: str) -> "_Span":
        return _Span(self, stage)


class _Span:
    def __init__(self, chrono: Chrono, stage: str):
        self.chrono, self.stage = chrono, stage

    def __enter__(self) -> "_Span":
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.chrono.add(self.stage, time.perf_counter() - self.t0)


def rss_to_mb(raw: float, system: str) -> float:
    """`ru_maxrss` est en OCTETS sous macOS et en KILO-octets sous Linux : -> Mo."""
    return raw / (1024 * 1024) if system == "Darwin" else raw / 1024


def peak_rss_mb() -> float:
    """Pic de mémoire résidente du processus depuis son lancement (Mo), 0 si inconnu."""
    try:
        import platform
        import resource
        return round(rss_to_mb(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                               platform.system()), 1)
    except Exception:  # noqa: BLE001  (Windows : pas de module resource)
        return 0.0


def fmt_durees(durees: dict[str, float], ram_mb: float) -> str:
    """« durées : docling 14 s, granite 210 s, vlm 31 s ; RAM max 9,8 Go »"""
    parts = [f"{k} {v:.0f} s" for k, v in durees.items() if v >= 0.5]
    ram = f"{ram_mb / 1024:.1f} Go".replace(".", ",") if ram_mb else "inconnue"
    return "durées : " + (", ".join(parts) or "–") + " ; RAM max " + ram


# --------------------------------------------------------------------------------------
# Décision par image
# --------------------------------------------------------------------------------------
@dataclass
class PicDecision:
    action: str = "drop"          # drop | vector | chart_table | text_layer | vlm | image
    label: str = ""
    confidence: float | None = None
    text: str = ""                # contenu Markdown qui remplace l'image
    image_rel: str | None = None  # lien relatif vers le WebP conservé
    reason: str = ""
    note: str = ""                # ex. « Granite rejeté : libellé vide »
    raw: str = ""                 # sortie brute du VLM avant contrôle (pour --dump-debug)
    page: int | None = None
    bbox: list[float] | None = None
    meta: ChartMeta | None = None  # métadonnées du graphique (titre, question, base...)
    body: str = ""                # tableaux seuls, sans les métadonnées
    meta_refs: list[str] = field(default_factory=list)  # textes du PDF repris dans `meta`
    lecture: str = ""             # provenance des chiffres lus par un modèle (« Lecture : »)
    coherence: list[str] = field(default_factory=list)  # contrôles arithmétiques en échec


@dataclass
class DocReport:
    pdf: str
    pages: int = 0
    seconds: float = 0.0
    pictures: int = 0
    actions: dict[str, int] = field(default_factory=dict)
    labels: dict[str, int] = field(default_factory=dict)
    webp_bytes: int = 0
    md_chars: int = 0
    md_tokens_est: int = 0
    noise_lines_removed: int = 0
    kind: str = ""                # triage : texte | présentation | scan
    granite_pages: list[int] = field(default_factory=list)   # pages reconverties par Granite
    vector_tables: int = 0        # « tableaux » Docling relus comme graphiques vectoriels
    durees: dict[str, float] = field(default_factory=dict)   # secondes par étape (STAGES)
    ram_max_mo: float = 0.0       # pic de mémoire résidente du processus (Mo)
    warnings: list[str] = field(default_factory=list)


def main_label(pic: PictureItem) -> tuple[str, float | None]:
    meta = getattr(pic, "meta", None)
    cls = getattr(meta, "classification", None) if meta else None
    if cls and cls.predictions:
        p = cls.get_main_prediction()
        return p.class_name, p.confidence
    return "", None


def area_fraction(pic: PictureItem, doc: DoclingDocument) -> float:
    try:
        prov = pic.prov[0]
        page = doc.pages[prov.page_no]
        bb = prov.bbox
        return abs((bb.r - bb.l) * (bb.t - bb.b)) / (page.size.width * page.size.height)
    except Exception:  # noqa: BLE001
        return 1.0


GENERIC_HEADERS = {"activity": "Libellé", "category": "Libellé", "percentage": "%", "subcategory": "Sous-catégorie"}


def chart_rows(tc: Any) -> list[list[str]] | None:
    """Lignes du tableau extrait par Granite Vision (`meta.tabular_chart` de Docling)."""
    if not tc or not tc.chart_data or not tc.chart_data.num_rows:
        return None
    rows = [[(c.text or "").strip().replace("|", "\\|") for c in row] for row in tc.chart_data.grid]
    rows = [r for r in rows if any(r)]
    return rows or None


def rows_to_md(rows: list[list[str]], title: str | None = None) -> str:
    head = [GENERIC_HEADERS.get(c.lower(), c) for c in rows[0]]   # en-têtes génériques -> courts
    md = "\n".join(["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
                   + ["| " + " | ".join(r) + " |" for r in rows[1:]])
    # Pas de résumé généré par le modèle (chart2summary) : il peut contenir des interprétations.
    return f"**{title}**\n\n{md}" if title else md


def bottom_left_bbox(doc: DoclingDocument, pic: PictureItem) -> tuple[float, float, float, float]:
    """Boîte de l'image en points PDF, origine en bas à gauche : (gauche, bas, droite, haut)."""
    pv = pic.prov[0]
    bb = pv.bbox
    if "TOP" in str(getattr(bb, "coord_origin", "")).upper():
        bb = bb.to_bottom_left_origin(page_height=doc.pages[pv.page_no].size.height)
    return min(bb.l, bb.r), min(bb.b, bb.t), max(bb.l, bb.r), max(bb.b, bb.t)


def _bl_box(doc: DoclingDocument, prov: Any) -> tuple[float, float, float, float]:
    """Boîte d'un élément en points PDF, origine en bas à gauche : (gauche, bas, droite, haut)."""
    bb = prov.bbox
    if "TOP" in str(getattr(bb, "coord_origin", "")).upper():
        bb = bb.to_bottom_left_origin(page_height=doc.pages[prov.page_no].size.height)
    return min(bb.l, bb.r), min(bb.b, bb.t), max(bb.l, bb.r), max(bb.b, bb.t)


def is_chart_data(d: PicDecision) -> bool:
    """Sortie chiffrée d'un graphique (et pas un Mermaid ni une description de photo)."""
    if not d.text or d.text.startswith("```mermaid"):
        return False
    if d.action in ("vector", "chart_table", "text_layer"):
        return True
    return d.action == "vlm" and any(l.lstrip().startswith("|") for l in d.text.splitlines())


def page_meta_items(doc: DoclingDocument) -> list[tuple[Any, dict[str, str]]]:
    """Textes du PDF qui sont entièrement une métadonnée de graphique : « Question : ... »,
    « Base : ... », « (base: ...) », « Source : ... », ou question non étiquetée suivie
    d'une base (Ipsos : la question et « (base: ...) » sont deux blocs)."""
    texts = [t for t in doc.texts if t.prov and (t.text or "").strip()
             and str(getattr(t, "label", "")).split(".")[-1].lower()
             not in ("page_header", "page_footer")]
    out = []
    for i, t in enumerate(texts):
        f = split_meta(t.text)
        if f:
            out.append((t, f))
            continue
        nxt = texts[i + 1] if i + 1 < len(texts) else None
        if (nxt is not None and nxt.prov[0].page_no == t.prov[0].page_no
                and looks_like_question(t.text)):
            fn = split_meta(nxt.text)
            if fn and set(fn) == {"base"}:
                out.append((t, {"question": " ".join(t.text.split())}))
    return out


def build_chart_blocks(doc: DoclingDocument, pics: list[PictureItem],
                       decisions: dict[str, PicDecision]) -> None:
    """Met chaque graphique au format commun (chart_format) : métadonnées puis tableaux.

    1. Titres lus dans l'image et avalés par Docling (déjà fait par vector_charts pour la
       lecture vectorielle) ;
    2. question, base et source de la page, prises dans la couche texte du PDF (jamais
       demandées à un modèle) : question et base vont au premier graphique situé dessous,
       la source au dernier graphique situé dessus, un texte DANS la boîte d'une image à
       cette image. Le texte repris est retiré du flux (`meta_refs`) pour ne pas doubler."""
    for pic in pics:
        d = decisions[pic.self_ref]
        if d.text and d.action in ("chart_table", "vlm") and not d.text.startswith("```mermaid"):
            ttl = uncovered_titles(texts_near(doc, pic, 0), d.text)
            if ttl:
                d.meta = d.meta or ChartMeta()
                d.meta.add_texts(ttl)
                d.note = (d.note + " ; " if d.note else "") + "titre repris de la couche texte"

    charts: dict[int, list[tuple[PictureItem, tuple]]] = {}
    for pic in pics:
        d = decisions[pic.self_ref]
        if is_chart_data(d) and pic.prov:
            charts.setdefault(pic.prov[0].page_no, []).append((pic, _bl_box(doc, pic.prov[0])))
    for t, f in page_meta_items(doc):
        page = t.prov[0].page_no
        if page not in charts:
            continue
        l, b, r, top = _bl_box(doc, t.prov[0])
        cx, cy = (l + r) / 2, (b + top) / 2
        target = next((p for p, (pl, pb, pr, pt) in charts[page]
                       if pl <= cx <= pr and pb <= cy <= pt), None)
        if target is None and "source" in f:
            below = [p for p, box in charts[page] if box[1] >= top - 5]
            target = below[-1] if below else None
        elif target is None:
            target = next((p for p, box in charts[page] if box[3] <= b + 5), None)
        if target is None:
            continue
        d = decisions[target.self_ref]
        d.meta = d.meta or ChartMeta()
        if f.get("base"):
            # Docling colle parfois des libellés de légende à la base (« A tous Assez positif
            # Assez négatif ») : on retire ceux qui sont déjà en-têtes du tableau
            f = dict(f, base=strip_labels(f["base"], table_heads(d.body or d.text)))
        d.meta.add_fields(f)
        # le texte n'est retiré du flux que si tout son contenu est dans le bloc
        if all(getattr(d.meta, k) == v for k, v in f.items()):
            d.meta_refs.append(t.self_ref)

    # Texte de la page fait seulement de libellés de légende déjà en en-tête d'un tableau
    # de la page (« Très négatif Ni positif, ni négatif ») : retiré du flux
    for page, items in charts.items():
        heads: set[str] = set()
        for p, _box in items:
            heads |= table_heads(decisions[p.self_ref].body or decisions[p.self_ref].text)
        if not heads:
            continue
        for t in doc.texts:
            # (seulement du texte libre : jamais une cellule de tableau ni le texte d'une image)
            parent = str(getattr(getattr(t, "parent", None), "cref", ""))
            if parent.startswith(("#/tables", "#/pictures")):
                continue
            if (t.prov and t.prov[0].page_no == page and (t.text or "").strip()
                    and not strip_labels(t.text, heads)):
                decisions[items[0][0].self_ref].meta_refs.append(t.self_ref)

    for pic in pics:
        d = decisions[pic.self_ref]
        if d.text and d.lecture:          # provenance : dernière ligne de métadonnées
            d.meta = d.meta or ChartMeta()
            d.meta.lecture = d.lecture
        if d.text and d.meta:
            d.body = d.body or d.text
            d.text = render_chart(d.meta, d.body)


def unread_scan_charts(decisions: dict[str, PicDecision]) -> int:
    """--scan-charts image : les chiffres lus par un modèle sur un PDF sans couche texte ne
    sont pas écrits ; le graphique devient une mention et son image est conservée."""
    n = 0
    for d in decisions.values():
        if d.lecture:
            d.action, d.text, d.body, d.meta, d.lecture = "image", SCAN_UNREAD, "", None, ""
            d.coherence = []
            d.reason = "PDF sans couche texte : chiffres du modèle non écrits"
            n += 1
    return n


def table_heads(md: str) -> set[str]:
    """Libellés des tableaux Markdown d'un bloc : en-têtes de colonnes (hors première) et
    libellés de lignes (première colonne ; ex. légende d'un anneau)."""
    out: set[str] = set()
    lines = md.splitlines()
    for i, l in enumerate(lines[:-1]):
        if l.startswith("| ") and lines[i + 1].startswith("|---"):
            out.update(c.strip() for c in l.strip().strip("|").split("|")[1:] if c.strip())
    for i, l in enumerate(lines):
        is_head = i + 1 < len(lines) and lines[i + 1].startswith("|---")
        if l.startswith("| ") and not is_head:
            first = l.strip().strip("|").split("|")[0].strip()
            if first and not re.fullmatch(r"[\d\s%.,–-]+", first):
                out.add(first)
    return out


def strip_labels(text: str, labels: set[str]) -> str:
    """Retire en fin de texte, autant de fois que possible, des libellés de `labels`."""
    t = " ".join(text.split())
    changed = True
    while t and changed:
        changed = False
        for lab in sorted(labels, key=len, reverse=True):
            if t == lab or t.endswith(" " + lab):
                t = t[: len(t) - len(lab)].rstrip()
                changed = True
                break
    return t


def decide_pictures(doc: DoclingDocument, args: argparse.Namespace, vlm: VlmClient | None,
                    assets_dir: Path, stem: str, img_offset: int,
                    vec: "VectorDocument | None" = None,
                    granite: Any = None, chrono: Chrono | None = None,
                    scan: bool = False) -> dict[str, PicDecision]:
    """Décide du sort de chaque image. `vec` : lecture vectorielle (prioritaire).
    `granite(pages) -> {page: [(boîte, tabular_chart)]}` : repli Granite Vision appelé
    une seule fois, pour les pages dont un graphique n'a pas été lu en vectoriel.
    `chrono` : cumul des durées des étapes vectoriel, granite et vlm. `scan` : PDF sans
    couche texte (chiffres invérifiables, voir --scan-charts)."""
    chrono = chrono or Chrono()
    pics: list[PictureItem] = [it for it, _ in doc.iterate_items() if isinstance(it, PictureItem)]
    decisions: dict[str, PicDecision] = {}
    images: dict[str, Image.Image] = {}

    # Comptage des images répétées dans ce document
    hashes: dict[str, str] = {}
    counts: dict[str, int] = {}
    for pic in pics:
        img = pic.get_image(doc)
        if img is None:
            continue
        images[pic.self_ref] = img
        h = ahash(img)
        hashes[pic.self_ref] = h
        counts[h] = counts.get(h, 0) + 1

    pnums = page_numbers(doc)
    to_vlm: list[PictureItem] = []
    rest: list[PictureItem] = []        # images ni supprimées ni lues en vectoriel
    # 1) Tri rapide et lecture vectorielle
    for pic in pics:
        label, conf = main_label(pic)
        d = PicDecision(label=label, confidence=conf)
        decisions[pic.self_ref] = d
        try:
            pv = pic.prov[0]
            d.page = pv.page_no
            d.bbox = [round(pv.bbox.l, 1), round(pv.bbox.t, 1), round(pv.bbox.r, 1),
                      round(pv.bbox.b, 1)]
        except Exception:  # noqa: BLE001
            pass
        img = images.get(pic.self_ref)
        if img is None:
            d.reason = "image indisponible"
            continue
        if label in DROP_LABELS and (conf is None or conf >= args.drop_min_conf):
            d.reason = f"classe {label}"
            continue
        if counts.get(hashes[pic.self_ref], 0) >= args.repeat_threshold:
            d.reason = "image répétée"
            continue
        if area_fraction(pic, doc) < args.min_area or min(img.size) < args.min_px:
            d.reason = "trop petite"
            continue
        # 1) Graphique vectoriel : lecture exacte par la géométrie du PDF, quelle que soit
        #    la classe (le classifieur prend parfois des barres pour un « flow_chart »).
        #    Rendu seulement si TOUS les nombres visibles de la zone sont expliqués.
        if vec is not None and d.page:
            try:
                with chrono("vectoriel"):
                    res = vec.read(d.page, *bottom_left_bbox(doc, pic))
            except Exception as e:  # noqa: BLE001
                res = None
                vec.last_reason = f"erreur : {e}"
            if res is not None:
                d.action, d.text = "vector", res.text
                d.meta, d.body = res.meta, res.body
                d.reason = f"graphique vectoriel lu dans le PDF ({res.kind})"
                d.coherence = vector_checks(res.body, res.kind, res.series)
                if d.coherence:          # sortie inchangée : seulement signalé
                    d.note = "auto-test vectoriel en échec : " + " ; ".join(d.coherence)
                continue
            if label in CHART_LABELS:
                d.note = f"lecture vectorielle impossible : {vec.last_reason}"
        rest.append(pic)

    # 2) Repli Granite Vision, seulement pour les pages dont un graphique n'a pas été lu
    #    en vectoriel (le convertisseur principal n'exécute plus Granite).
    tabular: dict[str, Any] = {}
    pending = [p for p in rest if decisions[p.self_ref].label in CHART_LABELS
               and decisions[p.self_ref].page]
    if pending and granite is not None:
        try:
            with chrono("granite"):
                found = granite({decisions[p.self_ref].page for p in pending})
        except Exception as e:  # noqa: BLE001
            log.warning("Repli Granite Vision impossible : %s", e)
            found = {}
        for p in pending:
            box = bottom_left_bbox(doc, p)
            cands = [(_iou(box, b), tc) for b, tc in found.get(decisions[p.self_ref].page, [])]
            if cands and max(cands, key=lambda x: x[0])[0] >= 0.5:
                tabular[p.self_ref] = max(cands, key=lambda x: x[0])[1]

    # 3) Chemin habituel pour ce qui reste : tableau Granite contrôlé, texte de la
    #    couche PDF pour les camemberts, légende sans valeur, modèle vision
    for pic in rest:
        d = decisions[pic.self_ref]
        label = d.label
        if label in CHART_LABELS:
            tc = tabular.get(pic.self_ref) or getattr(getattr(pic, "meta", None),
                                                      "tabular_chart", None)
            rows = chart_rows(tc)
            if rows:
                prob = chart_rows_problem(rows)
                if prob is None:
                    d.action, d.reason = "chart_table", "données extraites par Granite Vision"
                    d.lecture = lecture_text("granite", scan)
                    d.text = rows_to_md(rows)
                    if getattr(tc, "title", None):
                        d.meta = ChartMeta(title=[tc.title.strip()])
                    continue
                tabs = split_sparse(rows)
                if tabs and all(chart_rows_problem(t) is None for t in tabs):
                    d.action, d.reason = "chart_table", "données extraites par Granite Vision"
                    d.note = "tableau Granite fusionné, scindé en %d" % len(tabs)
                    d.lecture = lecture_text("granite", scan)
                    d.text = "\n\n".join(rows_to_md(t) for t in tabs)
                    continue
                d.note = (d.note + " ; " if d.note else "") + f"Granite rejeté : {prob}"
                d.raw = "GRANITE:\n" + rows_to_md(rows)
                log.info("    p.%s : tableau Granite rejeté (%s) -> VLM", d.page, prob)
        if label == "pie_chart":
            blk = pie_text_block(doc, pic)
            if blk:
                d.action, d.text = "text_layer", blk
                d.reason = "camembert : texte de la couche PDF"
                continue
        if label == "table":
            inside = texts_near(doc, pic, 0)
            if inside and not any(re.search(r"\d", t) for t in inside):
                d.reason = "légende sans valeur (libellés repris avec le graphique)"
                continue
        if vlm is not None:
            d.action = "vlm"
            to_vlm.append(pic)
        else:
            d.action, d.reason = "image", "pas de modèle vision"

    # Appels au modèle vision, en parallèle
    if to_vlm and vlm is not None:
        def work(p: PictureItem) -> tuple[str, str | None, str, str]:
            d0 = decisions[p.self_ref]
            mode = "table" if d0.label in TABLE_MODE_LABELS else "auto"
            img = images[p.self_ref]
            hint = ""
            if mode == "table":
                hint = " | ".join(texts_near(doc, p, 80))
            text = vlm.ask(img, d0.label, mode, hint)
            raw, note = text or "", ""
            if text and not is_decorative(text):
                prob = vlm_output_problem(text)
                if prob and mode == "auto":     # Mermaid inutile : on redemande en mode tableau
                    note = f"Mermaid rejeté ({prob}), relance en mode tableau"
                    text = vlm.ask(img, d0.label, "table")
                    raw += "\n--- RELANCE TABLEAU ---\n" + (text or "")
                    if text and vlm_output_problem(text):
                        text = None            # toujours inutilisable -> image conservée
                elif prob:
                    text = None
                    note = f"sortie rejetée ({prob})"
            if text and mode == "table" and not is_decorative(text):
                cells = [c for l in text.splitlines() if l.strip().startswith("|")
                         for c in l.strip().strip("|").split("|")]
                if cells and not any(is_num(c) for c in cells):
                    text = "DECORATIF"      # simple légende de libellés : sans valeur, on supprime
                    note = (note + " ; " if note else "") + "tableau sans valeur (légende)"
            if text and not is_decorative(text) and d0.page in pnums:
                inv = invented_values(text, pnums[d0.page])
                if inv:
                    note = (note + " ; " if note else "") + "VLM rejeté (" + inv + ")"
                    text = None
            if text and mode == "table" and not is_decorative(text):
                miss = uncovered_texts(texts_near(doc, p, 0), text)
                if miss:
                    text += ("\n\n*Valeurs lues dans l'image sans libellé établi : "
                             + " ; ".join(miss) + "*")
                    note = (note + " ; " if note else "") + "valeurs ajoutées depuis la couche texte"
            return p.self_ref, text, raw, note

        t_vlm = time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.vlm_workers) as ex:
            for i, (ref, text, raw, note) in enumerate(ex.map(work, to_vlm), 1):
                d = decisions[ref]
                d.raw = (d.raw + "\n\n" + raw).strip() if d.raw else raw
                if note:
                    d.note = (d.note + " ; " + note).strip(" ;")
                if text is None:
                    d.action, d.reason = "image", "échec VLM ou sortie rejetée"
                elif is_decorative(text):
                    d.action, d.reason = "drop", "jugée décorative par le VLM"
                else:
                    d.text = text
                    d.reason = ("mermaid" if text.startswith("```mermaid")
                                else "table VLM" if has_md_table(text) else "texte VLM")
                    if vlm_reads_numbers(d.label, text):
                        d.lecture = lecture_text("vlm", scan)
                if i % 10 == 0:
                    vlm.save_cache()
                    log.info("    VLM : %d/%d images", i, len(to_vlm))
        vlm.save_cache()
        chrono.add("vlm", time.perf_counter() - t_vlm)

    # Contrôles de cohérence des chiffres lus par un modèle : signalés, jamais rejetés (v9)
    for d in decisions.values():
        if d.lecture and d.text:
            d.coherence = model_checks(d.text, d.label)
            if d.coherence:
                d.note = (d.note + " ; " if d.note else "") + "incohérence : " + \
                    " ; ".join(d.coherence)
                d.lecture += " ; " + short_tags(d.coherence)

    # PDF sans couche texte avec --scan-charts image : rien de ce qu'un modèle a lu en
    # chiffres n'est écrit ; l'image est gardée en WebP
    if scan and getattr(args, "scan_charts", "tag") == "image":
        unread_scan_charts(decisions)

    # Format commun : métadonnées (titre, question, base, source...) puis tableaux
    build_chart_blocks(doc, pics, decisions)

    # Images conservées en WebP (écriture de la sortie)
    t_out = time.perf_counter()
    n = img_offset
    for pic in pics:
        d = decisions[pic.self_ref]
        keep = d.action == "image" or (
            d.action == "vlm" and (
                args.keep_images == "all"
                or (args.keep_images == "visual" and d.label in VISUAL_LABELS)))
        if keep and pic.self_ref in images:
            n += 1
            name = f"img_{n:04d}.webp"
            save_webp(images[pic.self_ref], assets_dir / name, args.max_side, args.webp_quality)
            d.image_rel = f"{stem}_assets/{name}"
    chrono.add("serialisation", time.perf_counter() - t_out)
    return decisions


# --------------------------------------------------------------------------------------
# Sérialisation Markdown : chaque image est remplacée par son substitut
# --------------------------------------------------------------------------------------
class SubstitutePictureSerializer(MarkdownPictureSerializer):
    decisions: dict[str, Any] = {}

    def serialize(self, *, item: PictureItem, doc_serializer, doc: DoclingDocument,
                  **kwargs: Any) -> SerializationResult:
        d: PicDecision | None = self.decisions.get(item.self_ref)
        cap = doc_serializer.serialize_captions(item=item, **kwargs).text.strip()
        if d is None or (d.action == "drop"):
            # On garde la légende si elle existe : elle porte parfois de l'information
            return create_ser_result(text=f"*{cap}*" if cap else "", span_source=item)
        parts: list[str] = []
        if cap:
            parts.append(f"*{cap}*")
        if d.text:
            parts.append(d.text)
        if d.image_rel:
            alt = (cap or d.label or "figure").replace("[", "(").replace("]", ")")[:80]
            parts.append(f"![{alt}]({d.image_rel})")
        return create_ser_result(text="\n\n".join(parts), span_source=item)


if MarkdownTableSerializer is not None:
    class SubstituteTableSerializer(MarkdownTableSerializer):
        replacements: dict[str, str] = {}

        def serialize(self, *, item, doc_serializer, doc, **kwargs):
            md = self.replacements.get(item.self_ref)
            if md is None:
                return super().serialize(item=item, doc_serializer=doc_serializer, doc=doc,
                                         **kwargs)
            cap = doc_serializer.serialize_captions(item=item, **kwargs).text.strip()
            return create_ser_result(text="\n\n".join(([f"*{cap}*"] if cap else []) + [md]),
                                     span_source=item)


class SkipTextSerializer(MarkdownTextSerializer):
    """Ne sérialise pas les textes repris dans le bloc de métadonnées d'un graphique."""
    skip: set[str] = set()

    def serialize(self, *, item, doc_serializer, doc, **kwargs):
        if item.self_ref in self.skip:
            return create_ser_result(text="", span_source=item)
        return super().serialize(item=item, doc_serializer=doc_serializer, doc=doc, **kwargs)


def doc_to_markdown(doc: DoclingDocument, decisions: dict[str, PicDecision],
                    tables: dict[str, tuple[int, str, str]] | None = None,
                    charts: dict[str, str] | None = None) -> str:
    pic_ser = SubstitutePictureSerializer()
    pic_ser.decisions = decisions
    txt_ser = SkipTextSerializer()
    txt_ser.skip = {ref for d in decisions.values() for ref in d.meta_refs}
    params = MarkdownParams(
        image_mode=ImageRefMode.PLACEHOLDER,
        blocked_meta_names={"classification", "description", "tabular_chart", "code",
                            "molecule"},
        include_picture_classification=False,
        compact_tables=True,       # pas d'espaces de remplissage : moins de tokens
        escape_underscores=False,
    )
    extra: dict[str, Any] = {}
    if (tables or charts) and MarkdownTableSerializer is not None:
        tab_ser = SubstituteTableSerializer()
        tab_ser.replacements = {ref: md for ref, (_pg, md, _src) in (tables or {}).items()}
        tab_ser.replacements.update(charts or {})
        extra["table_serializer"] = tab_ser
    ser = MarkdownDocSerializer(doc=doc, picture_serializer=pic_ser, text_serializer=txt_ser,
                                params=params, **extra)
    md = ser.serialize().text
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip() + "\n"


# --------------------------------------------------------------------------------------
# Conversion
# --------------------------------------------------------------------------------------
def build_converter(args: argparse.Namespace, ocr: bool = True,
                    charts: bool = False) -> DocumentConverter:
    """Convertisseur Docling. `ocr` : OCR sélectif de Docling (régions sans texte PDF,
    pages au texte cassé). `charts` : extraction des graphiques par Granite Vision,
    réservée au repli (voir ConverterPool)."""
    po = PdfPipelineOptions()
    po.do_ocr = ocr and not args.no_ocr
    po.do_table_structure = True
    try:
        from docling.datamodel.pipeline_options import TableFormerMode
        po.table_structure_options.mode = TableFormerMode.ACCURATE
        if args.no_cell_matching:
            po.table_structure_options.do_cell_matching = False
    except Exception as e:  # noqa: BLE001
        log.warning("Options TableFormer ignorées : %s", e)
    po.generate_picture_images = True
    if not args.no_vlm and not args.no_table_fallback:
        po.generate_table_images = True     # pour la relecture des tableaux irréguliers
    po.images_scale = args.images_scale
    po.do_picture_classification = True
    po.do_picture_description = False   # on gère nous-mêmes, avec un prompt routé
    po.do_chart_extraction = False      # Granite seulement dans le convertisseur de repli
    if charts and args.charts == "docling":
        po.do_chart_extraction = True
        if args.chart_api_url:
            from docling.datamodel.chart_extraction_options import (
                ChartExtractionVlmEngineOptions,
            )
            from docling.datamodel.vlm_engine_options import (
                ApiVlmEngineOptions,
                VlmEngineType,
            )
            po.enable_remote_services = True
            url = args.chart_api_url.rstrip("/")
            if not url.endswith("/chat/completions"):
                url += "/chat/completions"
            po.chart_extraction_options = ChartExtractionVlmEngineOptions.from_preset(
                "granite_vision_v4",
                engine_options=ApiVlmEngineOptions(
                    engine_type=VlmEngineType.API_OPENAI, url=url,
                    params={"model": args.chart_api_model}, timeout=180,
                ),
                use_natural_language_prompts=True,
            )
        elif args.mlx:
            from docling.datamodel.chart_extraction_options import (
                ChartExtractionVlmEngineOptions,
            )
            po.chart_extraction_options = ChartExtractionVlmEngineOptions.from_preset(
                "granite_vision_v4_mlx")
    return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=po)})


# --------------------------------------------------------------------------------------
# Triage : quel traitement pour quelles pages ? (pypdfium2, quelques ms par page)
# --------------------------------------------------------------------------------------
@dataclass
class PageProfile:
    """Profil d'une page pour le rapport. NB : on ne s'en sert PAS pour couper l'OCR.
    Mesuré sur un rapport de 364 pages de texte pur : l'OCR (sélectif) de Docling coûte
    environ 5 s par PDF (chargement du modèle, puis ~0,08 s par page), et sans lui
    Docling perd du texte pourtant présent dans la couche PDF (un intertitre, une puce)."""
    page: int
    chars: int          # caractères de la couche texte
    img_area: float     # part de la page couverte par des images bitmap
    paths: int          # formes vectorielles (barres, filets... ou lettres dessinées)
    landscape: bool


def profile_pages(pdf: Path, first: int, last: int) -> list[PageProfile]:
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfraw
    doc = pdfium.PdfDocument(str(pdf))
    out = []
    try:
        for n in range(first, last + 1):
            pg = doc[n - 1]
            w, h = pg.get_size()
            area, paths = 0.0, 0
            for o in pg.get_objects(max_depth=3):
                if o.type == pdfraw.FPDF_PAGEOBJ_IMAGE:
                    l, b, r, t = o.get_bounds()
                    area += max(0.0, (r - l) * (t - b)) / (w * h)
                elif o.type == pdfraw.FPDF_PAGEOBJ_PATH:
                    paths += 1
            out.append(PageProfile(n, pg.get_textpage().count_chars(), min(area, 1.0),
                                   paths, w > h))
    finally:
        doc.close()
    return out


def pdf_kind(profiles: list[PageProfile]) -> str:
    """Étiquette du PDF pour le rapport : texte, présentation ou scan."""
    n = len(profiles) or 1
    if sum(p.chars < 50 and p.img_area > 0.5 for p in profiles) / n >= 0.5:
        return "scan"
    if sum(p.landscape for p in profiles) / n >= 0.5:
        return "présentation"
    return "texte"


class ConverterPool:
    """Convertisseurs Docling créés à la demande et réutilisés d'un PDF à l'autre : le
    principal (sans Granite) et celui de repli avec Granite Vision. Granite n'est donc
    chargé que si un graphique résiste à la lecture vectorielle."""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self._pool: dict[bool, DocumentConverter] = {}

    def get(self, charts: bool = False) -> DocumentConverter:
        if charts not in self._pool:
            self._pool[charts] = build_converter(self.args, ocr=True, charts=charts)
        return self._pool[charts]


def _iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    l, bo, r, t = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, r - l) * max(0.0, t - bo)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def granite_fallback(pdf: Path, pool: ConverterPool, pages: set[int]) -> dict[int, list]:
    """Repli Granite Vision : reconvertit seulement les pages dont un graphique n'a pas
    été lu en vectoriel, avec l'extraction de graphiques activée. Renvoie, par page, les
    (boîte, tabular_chart) trouvés, à rapprocher des images du document principal."""
    out: dict[int, list] = {}
    runs: list[list[int]] = []
    for p in sorted(pages):
        if runs and p == runs[-1][-1] + 1:
            runs[-1].append(p)
        else:
            runs.append([p])
    conv = pool.get(charts=True)
    for run in runs:
        log.info("    Granite Vision (repli) : pages %d-%d", run[0], run[-1])
        doc = conv.convert(pdf, page_range=(run[0], run[-1])).document
        for it, _ in doc.iterate_items():
            if not isinstance(it, PictureItem) or not it.prov:
                continue
            tc = getattr(getattr(it, "meta", None), "tabular_chart", None)
            if tc is not None:
                out.setdefault(it.prov[0].page_no, []).append((bottom_left_bbox(doc, it), tc))
    return out


def count_pages(pdf: Path) -> int:
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(str(pdf))
    try:
        return len(doc)
    finally:
        doc.close()


def parse_pages(spec: str | None, total: int) -> tuple[int, int]:
    if not spec:
        return 1, total
    a, _, b = spec.partition("-")
    return max(1, int(a)), min(total, int(b) if b else int(a))


def dump_debug(doc: DoclingDocument, decisions: dict[str, PicDecision], debug_dir: Path,
               stem: str, start: int, end: int) -> None:
    """Instrumentation : DoclingDocument en JSON + une fiche par image (classe, page, boîte,
    décision, sortie brute du VLM). Permet de rejouer tri et sérialisation sans reconvertir."""
    # Chemin absolu : avec un chemin relatif, Docling recrée le chemin sous le dossier du JSON
    # (sortie/_debug/sortie/_debug/..._artifacts).
    debug_dir = debug_dir.resolve()
    debug_dir.mkdir(parents=True, exist_ok=True)
    try:
        doc.save_as_json(debug_dir / f"{stem}.p{start}-{end}.docling.json",
                         artifacts_dir=debug_dir / f"{stem}.p{start}-{end}_artifacts",
                         image_mode=ImageRefMode.REFERENCED)
    except Exception as e:  # noqa: BLE001
        log.warning("Export JSON Docling impossible : %s", e)
    (debug_dir / f"{stem}.p{start}-{end}.pictures.json").write_text(
        json.dumps([{"ref": ref, **asdict(d)} for ref, d in decisions.items()],
                   ensure_ascii=False, indent=1), "utf-8")


def process_pdf(pdf: Path, out_dir: Path, pool: ConverterPool,
                vlm: VlmClient | None, args: argparse.Namespace) -> DocReport:
    t0 = time.time()
    chrono = Chrono()
    stem = pdf.stem
    assets_dir = out_dir / f"{stem}_assets"
    rep = DocReport(pdf=pdf.name)
    total = count_pages(pdf)
    first, last = parse_pages(args.pages, total)
    rep.pages = last - first + 1
    chunk = args.chunk_pages if args.chunk_pages > 0 else rep.pages

    md_parts: list[str] = []
    img_count = 0
    all_decisions: list[PicDecision] = []
    debug_dir = out_dir / "_debug"
    try:
        with chrono("triage"):
            profiles = profile_pages(pdf, first, last)
            rep.kind = pdf_kind(profiles)
        log.info("  type : %s", rep.kind)
    except Exception as e:  # noqa: BLE001
        log.warning("Triage impossible : %s", e)
    # PDF sans couche texte : `kind` est calculé sur la plage de pages demandée
    scan = rep.kind == "scan"
    if scan:
        rep.warnings.append("PDF sans couche texte (scan ou capture) : chiffres lus par un "
                            "modèle, invérifiables")
    vec = None
    if VectorDocument is not None and not args.no_vector:
        try:
            with chrono("vectoriel"):
                vec = VectorDocument(pdf)
        except Exception as e:  # noqa: BLE001
            log.warning("Lecture vectorielle désactivée pour ce PDF : %s", e)

    def granite(pages: set[int]) -> dict[int, list]:
        if args.charts != "docling":
            return {}
        rep.granite_pages.extend(sorted(pages))
        return granite_fallback(pdf, pool, pages)

    converter = pool.get(charts=False)
    for start in range(first, last + 1, chunk):
        end = min(last, start + chunk - 1)
        log.info("  pages %d-%d / %d", start, end, total)
        with chrono("docling"):
            res = converter.convert(pdf, page_range=(start, end))
        doc = res.document
        decisions = decide_pictures(doc, args, vlm, assets_dir, stem, img_count, vec, granite,
                                    chrono, scan)
        img_count += sum(1 for d in decisions.values() if d.image_rel)
        all_decisions.extend(decisions.values())
        for d in decisions.values():
            for prob in d.coherence:
                rep.warnings.append(f"p. {d.page} : {VECTOR_CHECK} : {prob}" if d.action == "vector"
                                    else f"p. {d.page} : incohérence {prob}")
        with chrono("vectoriel"):
            vtabs = vector_tables(doc, vec, rep.warnings)
        with chrono("tableaux"):     # y compris les relectures de tableaux par le VLM
            tables = decide_tables(doc, args, vlm, page_numbers(doc), set(vtabs), scan)
        with chrono("serialisation"):
            md_parts.append(doc_to_markdown(doc, decisions, tables, vtabs))
        rep.vector_tables += len(vtabs)
        rep.warnings.extend(table_warnings(doc, set(tables) | set(vtabs)))
        rep.warnings.extend(
            f"p. {pg} : tableau irrégulier corrigé sans modèle" if src == "regle"
            else f"p. {pg} : tableau irrégulier relu par le modèle vision (valeurs non vérifiées)"
            for pg, _md, src in tables.values())
        if args.dump_debug:
            dump_debug(doc, decisions, debug_dir, stem, start, end)
        del res, doc
    if vec is not None:
        vec.close()

    t_ser = time.perf_counter()
    md = "\n".join(md_parts)
    if not args.no_denoise:
        md, rep.noise_lines_removed = denoise_markdown(md)
    if scan:
        md = SCAN_BANNER + "\n\n" + md
    (out_dir / f"{stem}.md").write_text(md, "utf-8")
    chrono.add("serialisation", time.perf_counter() - t_ser)

    rep.seconds = round(time.time() - t0, 1)
    rep.durees = {k: round(v, 1) for k, v in chrono.durees.items()}
    rep.ram_max_mo = peak_rss_mb()
    log.info("  %s", fmt_durees(rep.durees, rep.ram_max_mo))
    rep.pictures = len(all_decisions)
    for d in all_decisions:
        key = d.action if d.action != "drop" else f"drop ({d.reason})"
        if d.action == "vlm":
            key = f"vlm ({d.reason})"
        rep.actions[key] = rep.actions.get(key, 0) + 1
        lab = d.label or "non classée"
        rep.labels[lab] = rep.labels.get(lab, 0) + 1
    if assets_dir.exists():
        rep.webp_bytes = sum(p.stat().st_size for p in assets_dir.glob("*.webp"))
    rep.md_chars = len(md)
    rep.md_tokens_est = round(len(md) / 3.5)  # ordre de grandeur pour du français
    (out_dir / f"{stem}.report.json").write_text(
        json.dumps(asdict(rep), ensure_ascii=False, indent=2), "utf-8")
    return rep


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    # Sans argument, dans un vrai terminal : assistant interactif (wizard.py)
    if not argv and sys.stdin.isatty():
        from wizard import run_wizard
        try:
            argv = run_wizard()
        except (KeyboardInterrupt, EOFError):
            argv = None
        if not argv:
            print("Abandon : rien n'a été converti.")
            return 0
    ap = build_parser()
    args = ap.parse_args(argv)
    return run(ap, args)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", type=Path, help="Dossier de PDF (ou un seul PDF)")
    ap.add_argument("output", type=Path, help="Dossier de sortie")
    g = ap.add_argument_group("Modèle vision (schémas, photos, captures)")
    g.add_argument("--vlm-url", default="http://localhost:1234/v1",
                   help="API compatible OpenAI (défaut : LM Studio). Ollama : http://localhost:11434/v1")
    g.add_argument("--vlm-model", default=None,
                   help="Nom du modèle vision chargé (ex. qwen2.5-vl-7b-instruct). Obligatoire sauf --no-vlm")
    g.add_argument("--vlm-api-key", default=None, help="Clé API si service distant")
    g.add_argument("--vlm-workers", type=int, default=2, help="Appels VLM simultanés")
    g.add_argument("--vlm-timeout", type=float, default=180)
    g.add_argument("--vlm-max-tokens", type=int, default=1500)
    g.add_argument("--no-vlm", action="store_true",
                   help="Aucun modèle vision : schémas et photos gardés en WebP")
    c = ap.add_argument_group("Graphiques de données")
    c.add_argument("--charts", choices=["docling", "vlm"], default="docling",
                   help="docling = modèle Granite Vision dédié (défaut) ; vlm = votre modèle vision")
    c.add_argument("--chart-api-url", default=None,
                   help="Faire tourner Granite Vision via une API (ex. LM Studio) au lieu d'en local")
    c.add_argument("--chart-api-model", default="granite-vision-4.1-4b")
    c.add_argument("--mlx", action="store_true", help="Mac Apple Silicon : Granite Vision en MLX")
    c.add_argument("--scan-charts", choices=["tag", "image"], default="tag",
                   help="PDF sans couche texte (scan, capture) : tag = chiffres lus par un modèle "
                        "écrits avec la mention « invérifiables » (défaut) ; image = non écrits, "
                        "image conservée en WebP")
    c.add_argument("--no-vector", action="store_true",
                   help="Ne pas lire les graphiques vectoriels dans la géométrie du PDF "
                        "(tout passe alors par Granite Vision / le modèle vision)")
    i = ap.add_argument_group("Images")
    i.add_argument("--keep-images", choices=["none", "visual", "all"], default="visual",
                   help="Garder un WebP en plus du texte : none / visual (photos, captures, cartes, plans) / all")
    i.add_argument("--max-side", type=int, default=1024)
    i.add_argument("--webp-quality", type=int, default=50)
    i.add_argument("--min-area", type=float, default=0.01,
                   help="Ignorer les images < cette fraction de la page (défaut 1 %%)")
    i.add_argument("--min-px", type=int, default=48, help="Ignorer les images < N px de côté")
    i.add_argument("--repeat-threshold", type=int, default=3,
                   help="Image présente N fois ou plus dans un document = décorative")
    i.add_argument("--drop-min-conf", type=float, default=0.5,
                   help="Confiance minimale du classifieur pour supprimer un logo/icône...")
    i.add_argument("--images-scale", type=float, default=2.0, help="Résolution d'extraction (2 = 144 dpi)")
    p = ap.add_argument_group("Traitement")
    p.add_argument("--pages", default=None, help="Plage de pages, ex. 1-15 (pour tester)")
    p.add_argument("--chunk-pages", type=int, default=80,
                   help="Convertir par tranches de N pages (limite la RAM sur les gros PDF ; 0 = tout d'un coup)")
    p.add_argument("--no-ocr", action="store_true", help="Désactiver l'OCR (PDF natifs : plus rapide)")
    p.add_argument("--force", action="store_true", help="Refaire les PDF déjà convertis")
    p.add_argument("--dump-debug", action="store_true",
                   help="Écrit sortie/_debug/ : JSON Docling + fiche de chaque image (classe, page, "
                        "boîte, décision, sortie brute du VLM)")
    p.add_argument("--no-table-fallback", action="store_true",
                   help="Ne pas faire relire par le VLM les tableaux à grille irrégulière")
    p.add_argument("--no-denoise", action="store_true",
                   help="Ne pas retirer les numéros de slide et le logo lu par l'OCR")
    p.add_argument("--no-cell-matching", action="store_true",
                   help="TableFormer sans cell matching (à tester si les tableaux sont décalés)")
    p.add_argument("-v", "--verbose", action="store_true")
    return ap


def run(ap: argparse.ArgumentParser, args: argparse.Namespace) -> int:

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("docling", "httpx", "urllib3", "PIL", "transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if not args.no_vlm and not args.vlm_model:
        ap.error("--vlm-model est requis (ou utilisez --no-vlm)")
    if args.charts == "vlm" and args.no_vlm:
        ap.error("--charts vlm nécessite un modèle vision")

    # Garde-fou Mac : sans mlx-vlm, Docling bascule Granite Vision sur Transformers,
    # qui n'utilise pas MPS -> inférence 4B sur CPU, extrêmement lente (cf. DEBUG.md).
    import platform
    if (args.charts == "docling" and not args.chart_api_url
            and platform.system() == "Darwin" and platform.machine() == "arm64"):
        try:
            import mlx_vlm  # noqa: F401
        except ImportError as e:
            ap.error(
                "Mac Apple Silicon sans mlx-vlm importable (" + str(e) + ").\n"
                "Granite Vision tournerait sur CPU (très lent). Au choix :\n"
                "  - pip install \"docling[vlm]\"  (Python 3.12 conseillé)\n"
                "  - ou relancer avec --charts vlm (graphiques traités par LM Studio)")

    pdfs = [args.input] if args.input.is_file() else sorted(
        p for p in args.input.iterdir() if p.suffix.lower() == ".pdf")
    if not pdfs:
        log.error("Aucun PDF trouvé dans %s", args.input)
        return 1
    args.output.mkdir(parents=True, exist_ok=True)

    vlm = None if args.no_vlm else VlmClient(
        args.vlm_url, args.vlm_model, args.vlm_api_key, args.vlm_timeout,
        args.max_side, args.output / ".vlm_cache.json", args.vlm_max_tokens)
    if vlm is not None:
        try:
            requests.get(vlm.endpoint.replace("/chat/completions", "/models"), timeout=5,
                         headers=vlm.headers)
        except Exception:  # noqa: BLE001
            log.error("Serveur du modèle vision injoignable : %s (LM Studio : onglet "
                      "Developer > Start server)", args.vlm_url)
            return 1

    if args.charts == "docling":
        install_granite_csv_patch()
    pool = ConverterPool(args)      # Granite Vision chargé seulement si un repli le demande
    reports: list[dict[str, Any]] = []
    failures: list[str] = []
    for n, pdf in enumerate(pdfs, 1):
        if (args.output / f"{pdf.stem}.md").exists() and not args.force:
            log.info("[%d/%d] %s : déjà converti, ignoré", n, len(pdfs), pdf.name)
            continue
        log.info("[%d/%d] %s", n, len(pdfs), pdf.name)
        try:
            rep = process_pdf(pdf, args.output, pool, vlm, args)
            reports.append(asdict(rep))
            log.info("  OK en %.0f s : %d pages, %d images -> %s ; ~%d tokens",
                     rep.seconds, rep.pages, rep.pictures, rep.actions, rep.md_tokens_est)
        except Exception as e:  # noqa: BLE001
            log.exception("  ÉCHEC sur %s : %s", pdf.name, e)
            failures.append(pdf.name)

    summary_path = args.output / "_resume.json"
    prev = []
    if summary_path.exists():
        try:
            prev = [r for r in json.loads(summary_path.read_text("utf-8"))
                    if r["pdf"] not in {x["pdf"] for x in reports}]
        except Exception:  # noqa: BLE001
            prev = []
    summary_path.write_text(json.dumps(prev + reports, ensure_ascii=False, indent=2), "utf-8")
    if failures:
        log.warning("Échecs (%d) : %s", len(failures), ", ".join(failures))
    log.info("Terminé. Résumé : %s", summary_path)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())