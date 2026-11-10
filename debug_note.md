# pdf2llm.py : note de débogage n° 5 (9 novembre 2026)

Remplace la note n° 4. **État du code : v7.1** = assistant interactif (`wizard.py`, `pdf2llm.command`) + v6 (lecture vectorielle des graphiques, `vector_charts.py`) + triage des PDF + Granite Vision en simple repli. `PROMPT_VERSION = "v5"` : les consignes du modèle vision n'ont pas changé, le cache reste valable. Les vérifications du § 6 ont été faites sur la v7.

---

## 1. Contexte

Convertir une trentaine de PDF volumineux, surtout des présentations PowerPoint exportées (sondages, études), en Markdown compact et fidèle pour un LLM texte seul. Priorité : **exactitude des chiffres**, puis faible nombre de tokens. Une donnée absente est préférable à une donnée fausse.

Pipeline : Docling (mise en page, tableaux, OCR) → classifieur d'images Docling → Granite Vision 4.1 4B en MLX pour les graphiques → règles maison (logos, répétitions) → Qwen2.5-VL-7B dans LM Studio pour le reste → sérialiseur Markdown personnalisé.
Environnement : Mac Apple Silicon, Python 3.12 (essais 1 à 6 en 3.14), Docling 2.149.0 (`requirements.lock`), LM Studio.
Fichier de développement : sondage Ifop de 21 pages au format slides, pages 1 à 15 (appelé ici `etude_exemple.pdf`, non versionné). Second fichier d'essai prévu : rapport Ipsos de 128 slides (non versionné).

## 2. Historique des essais

| Essai | Code | Durée | Tokens (est.) | Résultat |
|---|---|---|---|---|
| 1 | origine | arrêté | – | Granite sur CPU (pas de `mlx-vlm`). Résolu : `docling[vlm]` + garde-fou dans `main()`. |
| 2 | origine | 254 s | 4 883 | Fonctionne mais 4 erreurs CSV, phrases inventées, Mermaid inutiles, bruit. |
| 3 | v2 | 218 s | 4 183 | P1, P2, P4, P7 corrigés. Restent : valeurs inventées p. 9, données perdues p. 8 et 13. |
| 4 | v3 | 188 s | 4 131 | Tableaux fusionnés scindés (p. 8, 13), valeurs inventées rejetées. Restent : anneau p. 9, tableau p. 4. |
| 5 | v4 | 206 s | 4 087 | Anneau p. 9 **faux** (associations inversées, voir § 4). Tableau p. 4 : relecture VLM jugée douteuse. |
| 6 | v5 | 237 s | 4 287 | Anneau p. 9 et titres avalés : OK. Tableau p. 4 **non corrigé** (en-tête sur 3 lignes, voir § 4.7). Logo « Oifop » repris deux fois comme titre. |
| 7 | v5.1 | 167 s | 4 209 | Tableau p. 4 corrigé, « Oifop » retiré. |
| 8 | v6 | 167 s | 4 998 | **18 graphiques sur 18 lus dans la géométrie du PDF**, aucun recours à Granite ni Qwen pour les remplacer. Anneaux rattachés, intertitres conservés. |
| 9 | v7 | **22 s** | 4 998 | Même Markdown, octet pour octet. Granite n'est plus exécuté pendant la conversion : aucun repli nécessaire sur ces pages. Voir § 6. |
| 10 | v7.1 | – | – | Assistant interactif (`wizard.py`) et lanceur `pdf2llm.command` ; conversion inchangée. |
| 11 | v8 | 227 s (Ipsos p. 10-25) | 4 710 | Format commun (`chart_format.py`) : métadonnées puis tableaux homogènes. Question / base reprises de la couche texte et retirées du flux (Ipsos : 8 blocs). Lecture vectorielle : un tableau par intertitre, rappels à part, légende non reprise conservée. Constat : sur Ipsos, aucun graphique lu en vectoriel (courbes non gérées) et tableaux Granite pauvres (`X / Y`, `Group 1`). |

## 3. Correctifs appliqués

| Réf. | Problème | Correctif | Vérifié sur le PDF |
|---|---|---|---|
| P1 | Granite ne met pas de guillemets autour des libellés avec virgule ; `pd.read_csv` échoue, le graphique tombe chez Qwen | `tolerant_csv_to_dataframe` remplace `granite_vision._extract_csv_to_dataframe` au démarrage (`install_granite_csv_patch`) | Oui |
| P2 | La consigne demandait une « tendance » : conclusions fausses | Consigne sans interprétation ; en mode tableau, `keep_table_only` ne garde que le titre et le tableau | Oui |
| P3 | Tableau Granite sans libellés accepté (p. 9) | `chart_rows_problem` : libellé vide, ligne sans nombre, dernière colonne vide à plus de 34 % | Oui |
| P3 bis | Granite fusionne deux graphiques voisins en un tableau « creux » (p. 8, 13) | `split_sparse` scinde en un tableau par colonne | Oui |
| P4 | Mermaid utilisé pour des chiffres, `classDiagram` vide | `vlm_output_problem` rejette, relance en mode tableau ; images de classe graphique ou `table` demandées directement en tableau | Oui |
| P7 | Numéros de slide, logo lu par l'OCR | `denoise_markdown` (logo détecté tout seul : mot court en minuscules répété 3 fois ou plus) ; en-têtes génériques « Category / Percentage » renommés | Oui (environ 20 lignes retirées) |
| Anti-invention | Qwen invente (légende p. 9 : 0, 25, 50, 75 %) | `invented_values` : rejet d'un tableau VLM dont 50 % des valeurs sont absentes de la couche texte de la page (10 % pour les tableaux relus) | Oui |
| Instrumentation | Rejouer sans reconvertir | `--dump-debug` : `sortie/_debug/*.docling.json` et `*.pictures.json` | Oui |
| P6 (v5, v5.1) | Tableau « marges d'erreur » décalé (p. 4) | `fixed_table_md` (correctif sans modèle, motif Ifop précis) ; sinon relecture VLM, sinon Docling. **v5.1** : fusion de l'en-tête multi-lignes de la grille brute avant reconnaissance | Oui (v5.1) |
| Anneau (v5) | Association valeur ↔ libellé fausse (p. 9) | `pie_text_block` : texte de la couche PDF, rattachement déclaré « non établi » | Oui |
| Titres (v5) | Titre avalé avec l'image (p. 9) | `uncovered_titles` reprend les textes sans chiffre absents de la sortie | Oui |
| Logo dans les titres (v5.1) | « *Oifop* » repris comme titre (logo dans la boîte de l'image) | `denoise_markdown` filtre aussi les lignes de titres en italique | Oui |
| Rapport (v5.1) | p. 4 signalée à la fois « à vérifier » et « corrigée » | `table_warnings` ignore les tableaux corrigés | Test unitaire seulement |
| Debug (v5.1) | Images du JSON écrites dans `sortie/_debug/sortie/_debug/…` | `dump_debug` passe un chemin absolu à Docling | Oui |
| Granite en repli (v7) | Granite Vision tournait sur tous les graphiques pendant la conversion Docling, même ceux lus en vectoriel (environ 2 min 30 pour 15 slides) | Extraction de graphiques coupée dans le convertisseur principal ; `granite_fallback` reconvertit seulement les pages dont un graphique n'a pas été lu, et ses tableaux sont rapprochés des images par recouvrement de boîtes (IoU ≥ 0,5). Convertisseurs créés à la demande (`ConverterPool`) : Granite n'est jamais chargé pour un PDF de texte | Oui : Ifop 167 s → 22 s ; Ipsos p. 55-60 : repli sur les seules p. 56-57 |
| Triage (v7) | Savoir quel genre de PDF on traite | `profile_pages` / `pdf_kind` (pypdfium2) : `texte`, `présentation` ou `scan`, inscrit dans le rapport (`kind`, `granite_pages`) | Oui (3 PDF) |
| Lecture vectorielle (v6) | Associations fausses (Qwen), intertitres et colonnes « Total » perdus (Granite), en-têtes en anglais | `vector_charts.py` : secteurs et segments empilés rattachés par la couleur de la légende ; barres simples par la position ; intertitres en colonne « Groupe » ; texte invisible écarté par le rendu. Utilisé seulement si tous les nombres visibles et toutes les barres sont expliqués, sinon chemin habituel (`--no-vector` pour le couper) | Oui (18/18, pages 1 à 15) |

## 4. Ce qu'on a appris (à ne pas redécouvrir)

1. **Docling avale le texte placé dans les images.** Les textes du PDF situés dans la boîte d'une image sont dans `doc.texts` mais n'apparaissent pas dans le Markdown (valeurs d'un anneau, titre, libellés de légende). Les boîtes sont en `BOTTOMLEFT` (haut > bas) ; `text_items_near` le gère.
2. **Qwen 7B est fiable pour lire des étiquettes voisines d'une barre, mauvais dès que le lien passe par une légende.** Constaté : une valeur attribuée au mauvais libellé, un libellé remplacé, des valeurs 0/25/50/75 % inventées pour une légende, un seul de deux graphiques recopié, l'en-tête omis, des mots de la consigne recopiés. Donner à Qwen les textes de la page comme indice **n'a pas suffi**.
3. **Le contrôle « les nombres existent sur la page » ne détecte pas une permutation.** Il arrête les valeurs inventées, pas les valeurs bien lues mais mal rattachées.
4. **Granite** : tableaux exacts sur les barres et camemberts simples ; en échec sur les anneaux et fusionne des graphiques voisins. Ses en-têtes sont génériques et en anglais (`Party | Proximity`), et il perd les intertitres de groupe (« Statut », « Revenu mensuel du foyer »).
5. **Ordre d'extraction** : le texte d'un anneau trié par position ne suit pas l'ordre de la légende. Ne jamais déduire l'association de l'ordre de lecture.
6. Le paquet PyPI `docling` est un méta-paquet ; le code est dans `docling-slim`.
7. **Un test doit recevoir la même entrée que le code en production.** Le test v5 de `fixed_table_md` utilisait l'en-tête déjà aplati du Markdown ; en production, la grille brute de Docling a un en-tête sur 3 lignes, et le correctif ne s'appliquait jamais. Le test v5.1 utilise la grille brute.
8. **Les slides exportées de PowerPoint sont vectorielles.** Dans le PDF de développement et dans le rapport Ipsos, la seule image bitmap par page est un logo ; barres et secteurs sont des formes vectorielles avec une couleur de remplissage, et toutes les valeurs sont dans la couche texte. Vérifié sur l'anneau p. 9 avec `pypdfium2` : chaque valeur est à l'intérieur du secteur de sa couleur, et chaque couleur correspond au carré de légende placé devant son libellé. L'association est donc exacte, sans modèle, sans estimer de centre ni d'angle (et cohérente avec « Total Proche 13 % = 2 + 11 »). Dans le rapport Ipsos, les courbes se distinguent par la **couleur du texte** des valeurs, identique à celle du titre de la série.
9. **Le texte « non affiché » (P5) est bien invisible.** Les blocs concernés ont un mode de rendu normal et une couleur visible, mais rien n'apparaît sur la page rendue : ils sont recouverts ou hors cadre. Seul un test sur le rendu (y a-t-il de l'encre dans la boîte du texte ?) permet de les repérer.
10. **Pièges de la lecture vectorielle, rencontrés et corrigés en v6** : (a) PowerPoint dessine toutes les barres d'une même couleur, ou tous les carrés de légende, dans **un seul chemin** : il faut le découper en sous-chemins (`MOVETO`) ; (b) les carrés de légende ont des **coins arrondis** (courbes) : les reconnaître par le taux de remplissage, pas comme rectangles ; (c) avec les boîtes **serrées** des caractères, « 11% » se coupe en « 1 » + « 1% » : utiliser les boîtes larges ; (d) regrouper les mots en lignes par **ligne de base**, pas par centre (accents, parenthèses) ; (e) une barre de 1 à 3 % fait 1,5 pt de large : rendu à 2 px par point et seuil de 0,8 pt ; (f) l'orientation (barres horizontales ou verticales) se décide **une fois par graphique**, sinon la valeur d'une barre trop fine est prise pour celle de la barre du dessous ; (g) barres divergentes : les colonnes se regroupent par chevauchement horizontal, pas par bord gauche ; (h) un texte gris foncé sur une barre foncée a peu de contraste : la visibilité se teste par la présence de pixels de la couleur du texte.
11. **L'OCR de Docling n'est pas un coût à couper.** Mesuré sur 60 pages de texte pur (rapport de l'Assemblée nationale) : 16 s avec OCR, 11 s sans ; l'écart est surtout le chargement du modèle (≈ 0,08 s par page ensuite). Et sans OCR, Docling **perd du texte présent dans la couche PDF** (un intertitre et une puce, p. 30-31). L'OCR de Docling 2.149 est sélectif : régions de mise en page sans texte PDF, lettres vectorisées, et passage en pleine page quand la couche texte est cassée (scans). Un triage qui couperait l'OCR sur les pages « propres » a été écrit puis abandonné : il aurait coupé l'OCR précisément sur ces pages.

## 5. Problèmes ouverts

- **Texte masqué (P5)** (p. 8, 10, 13) : écarté dans les graphiques (v6), mais les blocs de profil masqués situés hors des boîtes d'image sortent encore en lignes isolées dans le corps du texte. `VectorPage` sait déjà dire si un mot est visible : il reste à filtrer les textes de Docling avec.
- **Courbes** (Ipsos p. 57) : Docling découpe les trois courbes en morceaux (un tableau d'une ligne, des dates et titres isolés) et Granite n'en restitue qu'une série. C'est le prochain cas à lire en vectoriel : la couleur du texte des valeurs est celle du titre de la série.
- **Rapport** : un tableau irrégulier relu par le VLM est signalé « corrigé automatiquement », comme un tableau corrigé sans modèle (Ipsos p. 59) ; distinguer les deux.
- **Tokens** : + 19 % (4 209 → 4 998), surtout la colonne « Groupe » répétée à chaque ligne, plus les titres et totaux retrouvés. Variante possible : un petit tableau par groupe avec l'intertitre en gras.
- Grands tableaux en texte libre (Ipsos p. 7, 97) : pas encore lus en vectoriel.
- Les **pages 16 à 21** n'ont jamais été converties ni vérifiées.
- `fixed_table_md` ne couvre que le motif « taille / effectif / échantillon ».
- Les seuils (34 % de cases vides, 50 % / 10 % de valeurs absentes, 0,55 de similarité pour le logo) ont été calibrés sur un seul document.
- Rapport Ipsos (non encore converti) : pied de page répété sur 123 pages sur 128, que `denoise_markdown` ne retire pas ; mots coupés dans la couche texte (« E ST EN DÉCLIN », « 202 / 3 ») ; grands tableaux dessinés en texte libre (13 lignes × 27 colonnes).

## 6. Vérifications de l'essai 9 (v7)

1. Ifop p. 1-15 : 22 s au lieu de 167 s, Markdown identique à la v6, `kind` = présentation, `granite_pages` vide. **OK.**
2. Rapport de texte (p. 21-80) : Markdown identique à l'essai avec OCR, 12 s, Granite jamais chargé, `kind` = texte. **OK.**
3. Ipsos p. 55-60 : `kind` = présentation ; repli Granite sur les seules p. 56-57 (deux courbes et un camembert non lus en vectoriel), tableaux bien rapprochés malgré une plage qui ne commence pas à la page 1. **OK** pour le mécanisme ; la qualité des courbes reste faible (§ 5).
4. Tests : 20 sur 20.

## 6 bis. Vérifications de l'essai 8 (v6)

1. `report.json` : `vector` = 18 (toutes les images chiffrées), plus aucun « tableau Granite rejeté », plus d'appel à Qwen pour les graphiques ; seul avertissement : p. 4 « corrigé automatiquement » (le doublon de la v5.1 a disparu). **OK.**
2. Anneau p. 9 : Très proche 2 %, Plutôt proche 11 %, Plutôt éloigné 34 %, Très éloigné 53 %, plus « Total Proche : 13% » et « Ensemble des Français : 6% ». Anneau p. 7 : 23 / 20 / 57 % plus « Total Oui : 43% ». **OK** (vérifié sur le rendu).
3. Profils p. 8, 13, 15 : colonne « Groupe » (Sexe, Âge, Profession, Statut, Niveau d'éducation, Catégorie d'agglomération, Revenu mensuel du foyer, Proximité partisane, Vote à la présidentielle 2022) ; barre de 1 à 3 % lue. **OK.**
4. Barres empilées p. 12 et 14 : séries dans l'ordre de la légende, colonnes « Total Oui » et « Ensemble des Français » ; vérifié ligne par ligne sur le rendu de la p. 12. **OK.**
5. Tests : 16 sur 16, dont 4 sur des PDF factices générés par le test et 1 sur le vrai PDF (`PDF2LLM_TEST_PDF`).

## 6 bis. Vérifications de l'essai 7 (v5.1)

1. `run.log` : `Correctif CSV Granite installé` ; `p.4 : tableau décalé corrigé` ; aucune erreur CSV ; un seul « tableau Granite rejeté » (p. 9). **OK.**
2. p. 4 : titre « INTERVALLE DE CONFIANCE A 95% DE CHANCES » en gras, première colonne « Taille d'échantillon » (500 … 3 000), ligne 3 000 → 0,8 / 1,1 / 1,4 / 1,6 / 1,8 / 1,8, cohérente avec l'exemple de lecture de la slide. **OK.**
3. p. 9 : bloc « Données lues dans le graphique » avec 2 / 11 / 34 / 53 % (total 100 %), Total Proche 13 %, Ensemble 6 %, sans association ; légende vide absente ; titre repris au-dessus du tableau par âge. **OK.**
4. Titres en italique ajoutés au-dessus des tableaux (p. 7, 8, 12 à 15) : sans bruit après v5.1. **OK.**
5. `.report.json` : `noise_lines_removed` = 20. L'avertissement en double de la p. 4 est corrigé dans le code, pas encore observé sur une conversion complète.

## 7. Suite

1. **Lecture vectorielle des courbes** (Ipsos p. 57), puis des grands tableaux en texte libre (Ipsos p. 7, 97).
2. **Filtre du texte masqué** dans le corps du texte, avec `VectorPage` (§ 4.9).
3. **Repères de page** dans le Markdown (`## [p. 8] titre de la slide`) pour que le LLM puisse citer ses sources.
4. **Contrôle automatique des pertes** : pour chaque page, part des nombres de la couche texte visible retrouvés dans le Markdown, et nombres du Markdown absents de la page ; plus les contrôles propres aux sondages (sommes à 100 %, totaux égaux à la somme des parties). À inscrire dans `report.json`.
5. Essai sur le rapport Ipsos de 128 slides (tableaux denses, courbes, pied de page répété), puis sur un rapport textuel. Ajouter la lecture des **courbes** (couleur du texte des valeurs = couleur du titre de la série).
6. Le correctif P1 vise une fonction interne de Docling 2.149 : garder la version de `requirements.lock` (le script avertit et continue si la fonction disparaît).
7. Performance : environ 11 à 16 s par page de slides chargées en graphiques. RAM jamais mesurée avec Granite et Qwen chargés ensemble.

## 8. Messages sans gravité dans les logs

`RapidOCR returned empty result` ; avertissements `torch` (`Redirects are currently not supported in MacOs`, `padding='same'`) ; téléchargements de modèles au démarrage (cache `~/.cache/huggingface`).
