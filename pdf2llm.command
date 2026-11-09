#!/bin/zsh
# pdf2llm : lanceur à double-cliquer (macOS). Ouvre un Terminal et lance l'assistant.
#
# - Fonctionne où que soit rangé le dossier du projet : il se repère par son propre
#   emplacement (y compris via un alias ou un lien symbolique posé sur le Bureau).
# - Appelle directement .venv/bin/python : contrairement à `source .venv/bin/activate`
#   et à `pip`, il continue de fonctionner après un déplacement du dossier.
# - Si l'environnement est absent ou cassé, propose de le (re)créer.

cd "${0:A:h}" || exit 1
PY=".venv/bin/python"

if ! "$PY" -c "import docling, pypdfium2, rich" >/dev/null 2>&1; then
  echo "L'environnement Python du projet est absent ou incomplet."
  BASE=$(command -v python3.12 || command -v python3)
  if [[ -z "$BASE" ]]; then
    echo "Aucun Python trouvé. Installez-le (brew install python@3.12) puis relancez."
    read "?Entrée pour fermer… "; exit 1
  fi
  read "REP?Le (re)créer maintenant avec $BASE ? Cela prend quelques minutes. [o/N] "
  if [[ "$REP" != [oOyY]* ]]; then exit 1; fi
  rm -rf .venv && "$BASE" -m venv .venv \
    && .venv/bin/python -m pip install --upgrade pip \
    && .venv/bin/python -m pip install -r requirements.txt \
    || { echo "Échec de l'installation."; read "?Entrée pour fermer… "; exit 1; }
fi

"$PY" pdf2llm.py
echo
read "?Terminé. Entrée pour fermer cette fenêtre… "
