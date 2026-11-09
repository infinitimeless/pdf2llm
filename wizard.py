"""
wizard.py — Assistant interactif de pdf2llm.

Lancé par `python pdf2llm.py` SANS argument (ou par double-clic sur pdf2llm.command) :
  1. choix du PDF ou du dossier dans une fenêtre Finder (macOS), sinon chemin tapé ;
  2. dossier de sortie ;
  3. modèle vision choisi dans la liste des modèles chargés dans LM Studio ;
  4. quelques questions avec valeurs par défaut, options avancées sur demande ;
  5. résumé + commande équivalente, puis lancement.

Les derniers choix sont mémorisés dans ~/.config/pdf2llm/derniers_choix.json (hors du
dossier du projet : déplacer le projet ne les perd pas). Un chemin mémorisé qui n'existe
plus est simplement ignoré.

La fonction centrale `answers_to_argv` est pure (testée sans interaction) : elle traduit
les réponses en arguments de la ligne de commande habituelle.
"""

from __future__ import annotations

import json
import os
import platform
import shlex
import subprocess
from pathlib import Path
from typing import Any

PREFS = Path(os.environ.get("PDF2LLM_PREFS",
                            Path.home() / ".config" / "pdf2llm" / "derniers_choix.json"))
DEFAULT_VLM_URL = "http://localhost:1234/v1"


# --------------------------------------------------------------------------------------
# Mémoire des derniers choix
# --------------------------------------------------------------------------------------
def load_prefs(path: Path = PREFS) -> dict[str, Any]:
    try:
        return json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_prefs(prefs: dict[str, Any], path: Path = PREFS) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(prefs, ensure_ascii=False, indent=1), "utf-8")
    except OSError:
        pass                                    # confort seulement : jamais bloquant


# --------------------------------------------------------------------------------------
# Fenêtre Finder (macOS) ; ailleurs, ou si PDF2LLM_NO_FINDER=1, on tape le chemin
# --------------------------------------------------------------------------------------
def finder_available() -> bool:
    return platform.system() == "Darwin" and not os.environ.get("PDF2LLM_NO_FINDER")


def _as_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def finder_choose(kind: str, prompt: str, start: str | None = None) -> str | None:
    """kind = "pdf" (un fichier PDF) ou "folder". Renvoie le chemin POSIX, ou None si
    l'utilisateur annule. La fenêtre est rattachée à l'application au premier plan
    (le Terminal) pour s'ouvrir devant elle."""
    what = ('choose file of type {"com.adobe.pdf"}' if kind == "pdf" else "choose folder")
    loc = ""
    if start and Path(start).exists():
        d = start if Path(start).is_dir() else str(Path(start).parent)
        loc = f" default location (POSIX file {_as_str(d)})"
    script = (f"tell application (path to frontmost application as text)\n"
              f"  set p to {what} with prompt {_as_str(prompt)}{loc}\n"
              f"end tell\n"
              f"POSIX path of p")
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True,
                           timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:                       # -128 : annulé par l'utilisateur
        return None
    out = r.stdout.strip()
    return out.rstrip("/") if kind == "folder" and len(out) > 1 else out or None


# --------------------------------------------------------------------------------------
# LM Studio : modèles réellement chargés
# --------------------------------------------------------------------------------------
def list_vlm_models(url: str, timeout: float = 4) -> list[str] | None:
    """Identifiants des modèles servis (hors modèles d'embedding), ou None si le serveur
    ne répond pas."""
    import requests
    try:
        r = requests.get(url.rstrip("/").removesuffix("/chat/completions") + "/models",
                         timeout=timeout)
        r.raise_for_status()
        ids = [m.get("id", "") for m in r.json().get("data", [])]
    except Exception:  # noqa: BLE001
        return None
    return [i for i in ids if i and "embed" not in i.lower()]


def vision_first(models: list[str]) -> list[str]:
    """Met en tête les modèles dont le nom évoque la vision (vl, vision, llava...)."""
    hint = ("vl", "vision", "llava", "pixtral", "gemma-3", "minicpm-v")
    return sorted(models, key=lambda m: (not any(h in m.lower() for h in hint), m))


# --------------------------------------------------------------------------------------
# Réponses -> arguments de la ligne de commande (fonction pure)
# --------------------------------------------------------------------------------------
def answers_to_argv(a: dict[str, Any]) -> list[str]:
    argv = [a["input"], a["output"]]
    if a.get("vlm_model"):
        argv += ["--vlm-model", a["vlm_model"]]
        if a.get("vlm_url") and a["vlm_url"] != DEFAULT_VLM_URL:
            argv += ["--vlm-url", a["vlm_url"]]
    else:
        argv.append("--no-vlm")
    if a.get("pages"):
        argv += ["--pages", a["pages"]]
    if a.get("keep_images", "visual") != "visual":
        argv += ["--keep-images", a["keep_images"]]
    for key, flag in [("force", "--force"), ("dump_debug", "--dump-debug"),
                      ("no_vector", "--no-vector"), ("no_ocr", "--no-ocr"),
                      ("no_denoise", "--no-denoise")]:
        if a.get(key):
            argv.append(flag)
    if a.get("charts") == "vlm" and a.get("vlm_model"):
        argv += ["--charts", "vlm"]
    if a.get("chunk_pages") and int(a["chunk_pages"]) != 80:
        argv += ["--chunk-pages", str(int(a["chunk_pages"]))]
    return argv


def command_line(argv: list[str]) -> str:
    return "python pdf2llm.py " + shlex.join(argv)


def pdfs_in(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(p for p in path.iterdir() if p.suffix.lower() == ".pdf")


def page_count(pdf: Path) -> int | None:
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(str(pdf))
        try:
            return len(doc)
        finally:
            doc.close()
    except Exception:  # noqa: BLE001
        return None


def valid_pages(spec: str) -> bool:
    import re
    return bool(re.fullmatch(r"\d+(-\d+)?", spec.strip()))


# --------------------------------------------------------------------------------------
# L'assistant
# --------------------------------------------------------------------------------------
def run_wizard() -> list[str] | None:
    """Pose les questions et renvoie les arguments, ou None si l'utilisateur abandonne."""
    from rich.console import Console
    from rich.prompt import Confirm as _Confirm, IntPrompt, InvalidResponse, Prompt
    from rich.table import Table

    class Confirm(_Confirm):
        """Oui / non en français : [o/n] (y et yes restent acceptés)."""
        choices = ["o", "n"]
        validate_error_message = "[prompt.invalid]Répondez o (oui) ou n (non)"

        def process_response(self, value: str) -> bool:
            v = value.strip().lower()
            if v in ("o", "oui", "y", "yes"):
                return True
            if v in ("n", "non", "no"):
                return False
            raise InvalidResponse(self.validate_error_message)

        def render_default(self, default):  # type: ignore[override]
            from rich.text import Text
            return Text(f"({'o' if default else 'n'})", "prompt.default")

    con = Console()
    prefs = load_prefs()
    a: dict[str, Any] = {}
    con.print("\n[bold]pdf2llm[/bold] : PDF → Markdown léger pour LLM "
              "[dim](Ctrl+C pour quitter)[/dim]\n")

    # 1) Entrée
    kind = Prompt.ask("Convertir [bold]1[/] un PDF ou [bold]2[/] un dossier de PDF",
                      choices=["1", "2"], default=prefs.get("kind", "1"))
    want = "pdf" if kind == "1" else "folder"
    # Chemin mémorisé proposé seulement s'il existe encore et s'il est du bon type
    last_in = prefs.get("input")
    if not last_in or not Path(last_in).exists() or Path(last_in).is_file() != (want == "pdf"):
        last_in = None
    src: str | None = None
    while not src:
        if finder_available():
            con.print("[dim]Une fenêtre Finder s'ouvre…[/dim]")
            src = finder_choose(want, "PDF à convertir" if want == "pdf"
                                else "Dossier contenant les PDF", last_in)
            if src is None:
                if not Confirm.ask("Aucune sélection. Taper le chemin à la place ?",
                                   default=True):
                    return None
        if not src:
            src = Prompt.ask("Chemin du PDF" if want == "pdf" else "Chemin du dossier",
                             default=last_in or None)
            src = os.path.expanduser((src or "").strip().strip("'\""))
        p = Path(src) if src else None
        if not p or not p.exists() or (want == "pdf" and p.suffix.lower() != ".pdf"):
            con.print(f"[red]Introuvable ou pas un PDF : {src}[/red]")
            src = None
            continue
        if want == "folder" and not pdfs_in(p):
            con.print(f"[red]Aucun PDF dans {src}[/red]")
            src = None
    a["input"] = str(Path(src).resolve())
    files = pdfs_in(Path(a["input"]))
    con.print(f"  → {len(files)} PDF : " + ", ".join(f.name for f in files[:4])
              + (" …" if len(files) > 4 else ""))

    # 2) Sortie
    base = Path(a["input"]).parent if want == "pdf" else Path(a["input"])
    default_out = prefs.get("output") if prefs.get("output") and \
        Path(prefs["output"]).exists() else str(base / "sortie_md")
    choice = Prompt.ask(f"Dossier de sortie : Entrée = [cyan]{default_out}[/], "
                        "[bold]f[/] = choisir dans le Finder, ou tapez un chemin",
                        default="", show_default=False)
    if choice.strip().lower() == "f" and finder_available():
        a["output"] = finder_choose("folder", "Dossier de sortie", default_out) or default_out
    elif choice.strip() and choice.strip().lower() != "f":
        a["output"] = os.path.expanduser(choice.strip().strip("'\""))
    else:
        a["output"] = default_out
    a["output"] = str(Path(a["output"]).resolve())

    # 3) Modèle vision (LM Studio)
    a["vlm_url"] = prefs.get("vlm_url", DEFAULT_VLM_URL)
    while True:
        models = list_vlm_models(a["vlm_url"])
        if models is not None:
            break
        con.print(f"[yellow]LM Studio ne répond pas sur {a['vlm_url']}[/yellow] "
                  "(LM Studio > Developer > Start server).")
        r = Prompt.ask("[bold]r[/] réessayer, [bold]s[/] continuer sans modèle vision, "
                       "[bold]u[/] autre adresse, [bold]q[/] quitter",
                       choices=["r", "s", "u", "q"], default="r")
        if r == "q":
            return None
        if r == "s":
            models = []
            break
        if r == "u":
            a["vlm_url"] = Prompt.ask("Adresse de l'API", default=a["vlm_url"])
    a["vlm_model"] = None
    if models:
        models = vision_first(models)
        t = Table(show_header=False, box=None, padding=(0, 1))
        for i, m in enumerate(models, 1):
            t.add_row(f"[bold]{i}[/]", m)
        t.add_row("[bold]0[/]", "[dim]aucun (images non décrites, graphiques quand même lus)[/dim]")
        con.print("Modèle vision (pour les schémas, photos et graphiques non lus) :")
        con.print(t)
        prev = prefs.get("vlm_model")
        dflt = models.index(prev) + 1 if prev in models else 1
        n = IntPrompt.ask("Numéro", default=dflt,
                          choices=[str(i) for i in range(len(models) + 1)], show_choices=False)
        a["vlm_model"] = models[n - 1] if n else None

    # 4) Questions courantes
    if want == "pdf":
        total = page_count(files[0])
        while True:
            pages = Prompt.ask(f"Pages à convertir ({total or '?'} au total ; "
                               "ex. 1-15 pour un essai ; Entrée = toutes)",
                               default="", show_default=False).strip()
            if not pages or valid_pages(pages):
                break
            con.print("[red]Format attendu : 5 ou 1-15[/red]")
        a["pages"] = pages or None
    done = [f for f in files if (Path(a["output"]) / f"{f.stem}.md").exists()]
    a["force"] = bool(done) and Confirm.ask(
        f"{len(done)} PDF déjà converti(s) dans ce dossier. Les reconvertir ?", default=False)
    keep = Prompt.ask("Images à garder en WebP à côté du texte : [bold]n[/]one, "
                      "[bold]v[/]isuelles (photos, cartes, captures), [bold]a[/]ll",
                      choices=["n", "v", "a"], default={"none": "n", "visual": "v", "all": "a"}
                      .get(prefs.get("keep_images", "visual"), "v"))
    a["keep_images"] = {"n": "none", "v": "visual", "a": "all"}[keep]
    a["dump_debug"] = Confirm.ask("Écrire les fichiers de diagnostic (_debug/) ?",
                                  default=bool(prefs.get("dump_debug", False)))

    # 5) Options avancées (rarement utiles)
    if Confirm.ask("Options avancées ?", default=False):
        a["no_vector"] = not Confirm.ask("Lire les graphiques vectoriels dans le PDF "
                                         "(exact, recommandé) ?", default=True)
        if a["vlm_model"]:
            a["charts"] = "vlm" if Confirm.ask(
                "Graphiques non lus : modèle vision au lieu de Granite Vision "
                "(machine sans GPU) ?", default=False) else "docling"
        con.print("[yellow]Couper l'OCR fait gagner ~5 s par PDF mais Docling perd alors "
                  "du texte (intertitres, puces), même dans des PDF natifs.[/yellow]")
        a["no_ocr"] = Confirm.ask("Couper l'OCR ?", default=False)
        a["no_denoise"] = not Confirm.ask("Retirer numéros de slide et logo lu par l'OCR ?",
                                          default=True)
        a["chunk_pages"] = IntPrompt.ask("Pages par tranche (moins = moins de RAM)", default=80)

    # 6) Résumé et lancement
    argv = answers_to_argv(a)
    t = Table(show_header=False, box=None, padding=(0, 1))
    t.add_row("Entrée", a["input"])
    t.add_row("Sortie", a["output"])
    t.add_row("Modèle vision", a["vlm_model"] or "aucun")
    t.add_row("Pages", a.get("pages") or "toutes")
    con.print("\n[bold]Résumé[/bold]")
    con.print(t)
    con.print("[dim]Commande équivalente (pour relancer sans l'assistant) :[/dim]")
    con.print(command_line(argv), highlight=False, soft_wrap=True)
    save_prefs({"kind": kind, "input": a["input"], "output": a["output"],
                "vlm_url": a["vlm_url"], "vlm_model": a["vlm_model"],
                "keep_images": a["keep_images"], "dump_debug": a["dump_debug"]})
    if not Confirm.ask("\nLancer la conversion ?", default=True):
        return None
    return argv
