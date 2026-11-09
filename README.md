# pdf2llm : vos PDF en Markdown léger pour LLM

Convertit un PDF, ou un dossier entier de PDF, en Markdown compact et fidèle, destiné à un LLM **texte seul** (non multimodal). Chaque élément visuel est remplacé par une donnée lisible : un graphique devient un tableau de valeurs, un schéma devient du Mermaid, une photo devient une description.

Priorité : **exactitude des chiffres**, puis faible nombre de tokens. Une donnée absente vaut mieux qu'une donnée fausse.

Tout tourne en local : [Docling](https://github.com/docling-project/docling) (mise en page, tableaux, OCR, classement des images, Granite Vision pour les graphiques), plus un modèle vision servi par LM Studio ou Ollama.

## Ce que devient chaque image

| Type d'image (classé par Docling) | Ce qu'elle devient |
|---|---|
| Graphique de données (barres, courbes, secteurs, nuages…) | Tableau de valeurs (Granite Vision, intégré à Docling), contrôlé puis repris par le modèle vision si suspect |
| Camembert / anneau mal lu par Granite | Valeurs et légende tirées de la couche texte du PDF, **sans** rattachement inventé |
| Schéma, organigramme, flux, architecture | Bloc Mermaid (modèle vision de LM Studio) |
| Photo, capture d'écran, carte, plan | Description texte, plus un WebP compressé (1024 px max, lien local) |
| Logo, icône, signature, tampon, QR code, image répétée, miniature | Supprimée (la légende est conservée) |
| Image que le modèle vision juge décorative | Supprimée |

Contrôles qualité appliqués aux sorties des modèles (détail dans [debug_note.md](debug_note.md)) :

- tableau Granite rejeté s'il a des libellés vides, des lignes sans nombre ou une colonne trop souvent vide ; tableau « fusionné » de deux graphiques voisins scindé en deux ;
- sortie du modèle vision rejetée si plus de la moitié de ses nombres sont absents de la couche texte de la page (valeurs inventées) ;
- Mermaid rejeté s'il code des chiffres, relance en mode tableau ;
- aucune phrase d'interprétation ni de « tendance » : en mode tableau, seuls le titre et le tableau sont gardés ;
- titres et valeurs que Docling « avale » dans l'image repris depuis la couche texte ;
- tableaux à grille décalée corrigés sans modèle quand le motif est reconnu (tableau Ifop « marges d'erreur »), sinon relus par le modèle vision ;
- bruit des slides retiré : numéros de page, logo lu par l'OCR.

## Installation (une fois)

```bash
python3.12 -m venv .venv
source .venv/bin/activate          # Windows : .venv\Scripts\activate
pip install -r requirements.txt    # versions exactes testées : requirements.lock
```

- **Python 3.12** est conseillé : c'est la version la plus sûre pour torch et MLX (3.14 a aussi fonctionné).
- **Activez toujours le `.venv`** avant de lancer le script. Sans activation, `python` désigne le Python du système, où Docling n'est pas installé (`No module named 'docling'`). Un `.venv` déplacé ne fonctionne plus : recréez-le.
- L'extra `[vlm]` installe `mlx-vlm` sur Mac Apple Silicon. Sans lui, Granite Vision tournerait sur le processeur, ce qui est très lent ; le script refuse alors de démarrer.
- Au premier lancement, Docling télécharge ses modèles depuis Hugging Face (mise en page, tableaux, classifieur d'images, Granite Vision, environ 8 Go), mis en cache dans `~/.cache/huggingface`.

## Modèle vision (LM Studio)

1. Dans LM Studio, chargez un modèle vision, par exemple **Qwen2.5-VL-7B-Instruct** ou **Qwen3-VL-8B**.
2. Onglet *Developer* → *Start server* (port 1234).
3. Notez l'identifiant exact du modèle : `curl http://localhost:1234/v1/models`.

Avec Ollama, ajoutez `--vlm-url http://localhost:11434/v1`.

## Utilisation

```bash
# 1) Test rapide sur 15 pages : vérifiez le résultat avant de tout lancer
python pdf2llm.py ./pdfs/etude_exemple.pdf ./sortie --pages 1-15 --vlm-model qwen2.5-vl-7b-instruct

# 2) Tout un dossier
python pdf2llm.py ./pdfs ./sortie --vlm-model qwen2.5-vl-7b-instruct

# 3) Diagnostic : ajoute sortie/_debug/ (JSON Docling + fiche de chaque image)
python pdf2llm.py ./pdfs/etude_exemple.pdf ./sortie --pages 1-15 --vlm-model qwen2.5-vl-7b-instruct --force --dump-debug 2>&1 | tee run.log
```

Le script peut être interrompu et relancé : les PDF déjà convertis sont sautés et les réponses du modèle vision sont en cache (`sortie/.vlm_cache.json`).

### Fichiers produits

| Fichier | Contenu |
|---|---|
| `nom.md` | Le Markdown pour le LLM |
| `nom_assets/*.webp` | Images conservées (photos, cartes…), selon `--keep-images` |
| `nom.report.json` | Bilan du PDF : décision par type d'image, tokens estimés, lignes de bruit retirées, avertissements (tableaux à vérifier ou corrigés) |
| `_resume.json` | Bilan de tous les PDF |
| `_debug/` | Avec `--dump-debug` : `*.docling.json` (document Docling complet) et `*.pictures.json` (classe, page, boîte, décision, sortie brute du modèle, note de chaque image) |

## Options

| Option | Effet |
|---|---|
| `--pages 1-15` | Ne convertit que cette plage de pages |
| `--force` | Reconvertit les PDF déjà faits |
| `--dump-debug` | Écrit `sortie/_debug/` pour diagnostiquer sans reconvertir |
| `--charts vlm` | Envoie les graphiques au modèle de LM Studio au lieu de Granite Vision (machine sans GPU) |
| `--chart-api-url http://localhost:1234/v1` | Fait tourner Granite Vision dans LM Studio (modèle `granite-vision-4.1-4b`, réglable par `--chart-api-model`) |
| `--mlx` | Force le preset MLX de Granite Vision (inutile en pratique : choisi automatiquement si `mlx-vlm` est installé) |
| `--keep-images none / visual / all` | WebP conservés : aucun / photos, captures, cartes, plans (défaut) / tous |
| `--no-vlm` | Sans modèle vision : graphiques en tableaux, le reste gardé en WebP |
| `--no-ocr` | PDF natifs (non scannés) : plus rapide |
| `--chunk-pages 40` | Convertit par tranches (moins de RAM ; défaut 80) |
| `--no-table-fallback` | Ne fait pas relire par le modèle vision les tableaux à grille irrégulière |
| `--no-denoise` | Garde numéros de slide et logo lu par l'OCR |
| `--no-cell-matching` | TableFormer sans *cell matching* (à essayer si des tableaux sont décalés) |
| `--vlm-workers`, `--vlm-timeout`, `--vlm-max-tokens` | Réglages des appels au modèle vision |
| `--min-area`, `--min-px`, `--repeat-threshold`, `--drop-min-conf` | Seuils de suppression des petites images, images répétées et logos |

Pour changer la consigne donnée au modèle vision, modifiez `VLM_PROMPT` ou `VLM_TABLE_PROMPT` en tête de script et incrémentez `PROMPT_VERSION` pour invalider le cache.

## Tests

```bash
python test_pdf2llm.py        # ou : python -m pytest test_pdf2llm.py
```

Les tests unitaires utilisent des données factices et ne demandent aucun fichier. Deux tests rejouent la détection de texte sur un vrai export Docling : ils sont sautés si `tests/fixtures/exemple.p1-15.docling.json` est absent. Pour les activer, lancez une conversion avec `--dump-debug` et copiez le `*.docling.json` produit à cet endroit (ou indiquez son chemin dans `PDF2LLM_FIXTURE_JSON`). Les valeurs attendues correspondent au PDF de développement (sondage Ifop de 21 pages), non versionné.

## Organisation du code (`pdf2llm.py`)

| Section | Rôle |
|---|---|
| Triage par classe, consignes | Classes d'images de Docling (graphique, à supprimer, visuelle) ; consignes du modèle vision |
| Utilitaires image, `VlmClient` | Réduction, empreinte des images répétées, WebP ; client API compatible OpenAI avec cache |
| Contrôles qualité | `chart_rows_problem`, `split_sparse`, `invented_values`, `vlm_output_problem`, `keep_table_only`, `denoise_markdown` |
| Couche texte du PDF | `text_items_near`, `pie_text_block`, `uncovered_titles`, `uncovered_texts` : texte que Docling place sous les images |
| Tableaux décalés | `irregular_tables`, `fixed_table_md`, `decide_tables` |
| Correctif Granite | `tolerant_csv_to_dataframe` remplace une fonction interne de Docling 2.149 (libellés avec virgule) |
| Décision par image | `decide_pictures` : arbre de décision, appels parallèles au modèle vision |
| Sérialisation | Sérialiseurs Markdown de Docling surchargés pour substituer images et tableaux |
| Conversion, `main` | Options du convertisseur, découpage en tranches, rapports, reprise |

## Limites connues et pistes

- Les seuils des contrôles ont été calibrés sur un seul document (sondage Ifop) ; à valider sur d'autres PDF.
- `fixed_table_md` ne reconnaît que le motif Ifop « marges d'erreur ».
- Un PDF exporté de PowerPoint peut contenir du **texte masqué** (recouvert ou hors cadre) que Docling extrait comme du texte normal.
- Piste principale : dans les présentations exportées, les graphiques sont des **formes vectorielles** et leurs valeurs sont dans la couche texte. Les lire directement (couleur de la légende ↔ couleur du secteur ou de la barre ↔ valeur) donne un résultat exact, sans modèle vision, et bien plus vite. Voir [debug_note.md](debug_note.md).

Historique des versions, problèmes rencontrés et vérifications : [debug_note.md](debug_note.md).
