# pdf2llm : vos PDF en Markdown léger pour LLM

Convertit un PDF, ou un dossier entier de PDF, en Markdown compact et fidèle, destiné à un LLM **texte seul** (non multimodal). Chaque élément visuel est remplacé par une donnée lisible : un graphique devient un tableau de valeurs, un schéma devient du Mermaid, une photo devient une description.

Priorité : **exactitude des chiffres**, puis faible nombre de tokens. Une donnée absente vaut mieux qu'une donnée fausse.

Tout tourne en local : [Docling](https://github.com/docling-project/docling) (mise en page, tableaux, OCR, classement des images, Granite Vision pour les graphiques), plus un modèle vision servi par LM Studio ou Ollama.

## Ce que devient chaque image

| Type d'image (classé par Docling) | Ce qu'elle devient |
|---|---|
| **Graphique vectoriel** (slides exportées de PowerPoint : barres, barres empilées, secteurs, anneaux) | Tableau **exact** lu dans la géométrie du PDF, sans modèle (`vector_charts.py`) : valeurs, libellés, légende, intertitres de groupe, colonnes « Total » |
| Graphique de données non vectoriel, ou non compris en entier par la lecture vectorielle | Tableau de valeurs (Granite Vision, intégré à Docling), contrôlé puis repris par le modèle vision si suspect |
| Camembert / anneau mal lu par Granite | Valeurs et légende tirées de la couche texte du PDF, **sans** rattachement inventé |
| Schéma, organigramme, flux, architecture | Bloc Mermaid (modèle vision de LM Studio) |
| Photo, capture d'écran, carte, plan | Description texte, plus un WebP compressé (1024 px max, lien local) |
| Logo, icône, signature, tampon, QR code, image répétée, miniature | Supprimée (la légende est conservée) |
| Image que le modèle vision juge décorative | Supprimée |

### Lecture vectorielle des graphiques

Dans une présentation exportée en PDF, un graphique n'est pas une image : les barres et les secteurs sont des formes de couleur, et chaque valeur est du texte du PDF. `vector_charts.py` reconstruit le tableau par la géométrie :

- **secteurs et segments empilés** : la valeur est dans une forme de couleur C ; le carré de légende de couleur C donne le libellé ;
- **barres simples** : la valeur est à côté de la barre (à droite ou au-dessus), le libellé sur la même ligne ; chaque intertitre (« Sexe », « Âge »…) donne **son propre tableau**, dont il devient l'en-tête de première colonne ;
- **colonnes sans barre** (« Total Oui », « Ensemble des Français ») : rattachées à la ligne, avec leur en-tête ; une colonne de **rappel** (« Rappel 2022 », année, « vague », « évolution ») part dans un tableau à part ;
- libellés de **légende** qui ne figurent dans aucun tableau : repris dans la ligne « Légende » ;
- le **texte invisible** (recouvert par une forme, oublié dans la slide) est écarté grâce au rendu de la page ;
- un graphique que Docling prend pour un **tableau** (barres empilées d'Ipsos lues comme une grille, colonnes décalées) est relu de la même façon ; le tableau de Docling n'est remplacé que si la lecture réussit (`vector_tables`) ;
- des années seules au-dessus de colonnes sans barre (« 2024 2020 2017 » d'un bloc « Rappels ») sont des en-têtes : ces colonnes forment le tableau des rappels.
- un **chiffre-clé** en très grand corps (« % Trop » / « 30 ») devient l'annotation « % Trop : 30 », et sa liste « RAPPELS 2020 : 30% … » un petit tableau Année / valeur ;
- les encadrés (à puces, ou lignes commençant par « 19% chez … ») donnent une annotation par entrée ; un fond d'encadré pâle n'est pas une barre.

**PDF sans couche texte** (capture d'écran, scan, « Exporter en PDF » d'une image : le rapport indique `kind: scan`) : aucune lecture exacte n'est possible, tout passe par le modèle vision et **aucun contrôle ne peut vérifier ses chiffres**. Convertir de préférence le PDF d'origine.

Règle de sûreté : le tableau n'est utilisé que si **tous** les nombres visibles de la zone et toutes les barres sont expliqués. Sinon l'image suit le chemin habituel ci-dessous, et la raison est notée dans `_debug/*.pictures.json`. La lecture prend quelques millisecondes par graphique.

### Format commun des graphiques (`chart_format.py`)

Quel que soit le chemin (lecture vectorielle, Granite, couche texte, modèle vision), un graphique chiffré devient un bloc de métadonnées puis un ou plusieurs tableaux, **un jeu de données homogène par tableau** :

```
Titre : Qui connaît Alain Soral ? ; Selon le sexe et l'âge
Question : *Vous personnellement, connaissez-vous, ne serait-ce que de nom, Alain Soral ?*
Base : ensemble des Français
Légende : …
Source : …
Annotations : Total Oui : 43%

| Sexe | Valeur |
|---|---|
| Hommes | 52% |
| Femmes | 34% |
```

Seuls les champs trouvés sont écrits. Les métadonnées viennent **de la couche texte du PDF, jamais d'un modèle** : « Question : », « Base : », « (base: …) », « Source : », ou une question non étiquetée suivie d'une base. Question et base vont au premier graphique situé dessous, la source au dernier graphique situé dessus, un texte placé dans la boîte d'une image à cette image ; le texte repris est retiré du flux pour ne pas apparaître deux fois. Une valeur absente s'écrit « – ».

### Triage et repli Granite Vision

- **Triage** : avant la conversion, chaque PDF est profilé en quelques millisecondes (texte par page, images, formes) et classé `texte`, `présentation` ou `scan` (champ `kind` du rapport).
- **Granite Vision n'est qu'un repli** : la conversion principale ne l'exécute plus. Seules les pages dont un graphique n'a pas été lu en vectoriel sont reconverties avec Granite (champ `granite_pages` du rapport). Un PDF de texte ne charge donc jamais Granite, et les 15 pages de slides de test passent de 167 s à 22 s.
- **L'OCR reste activé partout** : celui de Docling est sélectif (régions sans texte PDF, pages au texte cassé ou scannées) et ne coûte qu'environ 5 s par PDF. Mesuré sur un rapport de texte pur, le couper ferait perdre des intertitres pourtant présents dans le PDF.

### Contrôles qualité

Contrôles appliqués aux sorties des modèles (détail dans [debug_note.md](debug_note.md)) :

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
- **Activez toujours le `.venv`** avant de lancer le script en ligne de commande. Sans activation, `python` désigne le Python du système, où Docling n'est pas installé (`No module named 'docling'`). Le lanceur `pdf2llm.command` n'a pas ce problème : il appelle directement `.venv/bin/python`.

### Déplacer le dossier du projet

Rien dans le code ne dépend de l'emplacement du dossier, et le dépôt git suit le dossier. Seul le `.venv` contient des chemins absolus : après un déplacement, `source .venv/bin/activate` et `pip` ne fonctionnent plus, mais `.venv/bin/python` et donc le lanceur `pdf2llm.command` continuent de fonctionner. Pour tout remettre en ordre, recréez l'environnement (le lanceur le propose aussi s'il le trouve cassé) :

```bash
rm -rf .venv && python3.12 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
```

Les modèles téléchargés restent dans `~/.cache/huggingface` et ne sont pas retéléchargés. Évitez un dossier synchronisé par iCloud : un `.venv` compte des dizaines de milliers de fichiers.
- L'extra `[vlm]` installe `mlx-vlm` sur Mac Apple Silicon. Sans lui, Granite Vision tournerait sur le processeur, ce qui est très lent ; le script refuse alors de démarrer.
- Au premier lancement, Docling télécharge ses modèles depuis Hugging Face (mise en page, tableaux, classifieur d'images, Granite Vision, environ 8 Go), mis en cache dans `~/.cache/huggingface`.

## Modèle vision (LM Studio)

1. Dans LM Studio, chargez un modèle vision, par exemple **Qwen2.5-VL-7B-Instruct** ou **Qwen3-VL-8B**.
2. Onglet *Developer* → *Start server* (port 1234).
3. Notez l'identifiant exact du modèle : `curl http://localhost:1234/v1/models`.

Avec Ollama, ajoutez `--vlm-url http://localhost:11434/v1`.

## Utilisation

### Le plus simple : l'assistant

Double-cliquez sur **`pdf2llm.command`** dans le Finder, ou lancez `python pdf2llm.py` sans argument. L'assistant :

1. ouvre une fenêtre Finder pour choisir **un PDF ou un dossier de PDF** ;
2. propose un dossier de sortie (Entrée pour accepter, `f` pour le choisir dans le Finder) ;
3. affiche la **liste des modèles chargés dans LM Studio** et vous fait choisir par numéro (message clair si LM Studio n'est pas lancé, possibilité de continuer sans modèle vision) ;
4. pose quelques questions avec des valeurs par défaut : pages (pour un essai), reconversion des PDF déjà faits, images à garder, fichiers de diagnostic ; les options avancées (lecture vectorielle, OCR, Granite ou modèle vision pour les graphiques, taille des tranches) ne sont proposées que sur demande ;
5. affiche un résumé et la **commande équivalente**, à recopier pour relancer sans l'assistant.

Les derniers choix sont mémorisés dans `~/.config/pdf2llm/derniers_choix.json` et proposés la fois suivante.

Pour l'avoir sur le Bureau : clic droit sur `pdf2llm.command` → *Créer un alias*, puis déplacez l'alias. Au premier lancement, macOS peut demander de confirmer l'ouverture (clic droit → *Ouvrir*).

### En ligne de commande

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
| `nom.report.json` | Bilan du PDF : type (`texte`, `présentation`, `scan`), décision par type d'image (`vector`, `chart_table`, `vlm`…), pages reconverties par Granite, tokens estimés, lignes de bruit retirées, avertissements |
| `_resume.json` | Bilan de tous les PDF |
| `_debug/` | Avec `--dump-debug` : `*.docling.json` (document Docling complet) et `*.pictures.json` (classe, page, boîte, décision, sortie brute du modèle, note de chaque image) |

## Options

| Option | Effet |
|---|---|
| `--pages 1-15` | Ne convertit que cette plage de pages |
| `--force` | Reconvertit les PDF déjà faits |
| `--dump-debug` | Écrit `sortie/_debug/` pour diagnostiquer sans reconvertir |
| `--no-vector` | Désactive la lecture vectorielle des graphiques (tout passe par Granite Vision / le modèle vision) |
| `--charts vlm` | Envoie les graphiques au modèle de LM Studio au lieu de Granite Vision (machine sans GPU) |
| `--chart-api-url http://localhost:1234/v1` | Fait tourner Granite Vision dans LM Studio (modèle `granite-vision-4.1-4b`, réglable par `--chart-api-model`) |
| `--mlx` | Force le preset MLX de Granite Vision (inutile en pratique : choisi automatiquement si `mlx-vlm` est installé) |
| `--keep-images none / visual / all` | WebP conservés : aucun / photos, captures, cartes, plans (défaut) / tous |
| `--no-vlm` | Sans modèle vision : graphiques en tableaux, le reste gardé en WebP |
| `--no-ocr` | Coupe l'OCR. **Déconseillé** : gain d'environ 5 s par PDF, mais Docling perd alors du texte (intertitres, puces), même dans des PDF natifs |
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

Les tests unitaires utilisent des données factices et ne demandent aucun fichier ; ceux de la lecture vectorielle génèrent eux-mêmes de petits PDF (barres avec intertitres et texte masqué, secteurs avec légende, barres empilées avec colonne « Total », refus si un nombre reste inexpliqué).

Trois tests utilisent de vrais fichiers et sont sautés s'ils sont absents :

- `tests/fixtures/exemple.p1-15.docling.json` (ou `PDF2LLM_FIXTURE_JSON`) : un export `--dump-debug` du PDF de développement ;
- `PDF2LLM_TEST_PDF` : le PDF de développement lui-même (sondage Ifop de 21 pages), pour la lecture vectorielle.

Ces fichiers ne sont pas versionnés. Les valeurs attendues correspondent à ce PDF.

## Organisation du code (`pdf2llm.py`)

| Section | Rôle |
|---|---|
| `wizard.py`, `pdf2llm.command` | Assistant interactif (Finder, liste des modèles LM Studio, questions, mémoire des choix ; `answers_to_argv` traduit les réponses en options) et lanceur à double-cliquer |
| `chart_format.py` (module séparé) | Format commun des graphiques : `ChartMeta`, `split_meta` (question, base, source), `render` ; dans `pdf2llm.py`, `build_chart_blocks` rattache les métadonnées de la page au bon graphique |
| `vector_charts.py` (module séparé) | Mots, lignes, formes et légende de chaque page (pypdfium2) ; visibilité par le rendu ; `read_region` reconstruit le graphique d'une zone ou lève `Unreadable` |
| Triage par classe, consignes | Classes d'images de Docling (graphique, à supprimer, visuelle) ; consignes du modèle vision |
| Utilitaires image, `VlmClient` | Réduction, empreinte des images répétées, WebP ; client API compatible OpenAI avec cache |
| Contrôles qualité | `chart_rows_problem`, `split_sparse`, `invented_values`, `vlm_output_problem`, `keep_table_only`, `denoise_markdown` |
| Couche texte du PDF | `text_items_near`, `pie_text_block`, `uncovered_titles`, `uncovered_texts` : texte que Docling place sous les images |
| Tableaux décalés | `irregular_tables`, `fixed_table_md`, `decide_tables` |
| Correctif Granite | `tolerant_csv_to_dataframe` remplace une fonction interne de Docling 2.149 (libellés avec virgule) |
| Décision par image | `decide_pictures` : lecture vectorielle d'abord, puis arbre de décision habituel, appels parallèles au modèle vision |
| Sérialisation | Sérialiseurs Markdown de Docling surchargés pour substituer images et tableaux |
| Triage, repli Granite | `profile_pages`, `pdf_kind`, `ConverterPool` (convertisseurs créés à la demande), `granite_fallback` (reconversion des seules pages à graphique non lu) |
| Conversion, `main` | Options du convertisseur, découpage en tranches, rapports, reprise |

## Limites connues et pistes

- Les seuils des contrôles ont été calibrés sur un seul document (sondage Ifop) ; à valider sur d'autres PDF.
- `fixed_table_md` ne reconnaît que le motif Ifop « marges d'erreur ».
- La lecture vectorielle couvre les barres (simples, empilées, horizontales ou verticales) et les secteurs. Les **courbes** (séries distinguées par la couleur du texte des valeurs) et les grands **tableaux dessinés en texte libre** ne sont pas encore lus : ils passent par Granite Vision, qui les restitue mal (rapport Ipsos p. 57 : une seule des séries retrouvée).
- Le **texte masqué** n'est écarté que dans les graphiques ; hors graphique, Docling l'extrait encore comme du texte normal.

Historique des versions, problèmes rencontrés et vérifications : [debug_note.md](debug_note.md).
