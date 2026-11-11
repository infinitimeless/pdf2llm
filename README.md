# pdf2llm: your PDFs as lightweight Markdown for LLMs

Converts one PDF, or a whole folder of PDFs, into compact and faithful Markdown for a **text-only** (non-multimodal) LLM. Every visual element is replaced by readable data: a chart becomes a table of values, a diagram becomes Mermaid, a photo becomes a description.

Priority: **exact numbers** first, then a low token count, then speed. Missing data is better than wrong data.

Everything runs locally: [Docling](https://github.com/docling-project/docling) (layout, tables, OCR, picture classification, Granite Vision for charts), plus a vision model served by LM Studio or Ollama.

The code, log messages and reports are in French (the tool was built for French survey decks); this README is in English.

## What happens to each picture

| Picture type (classified by Docling) | What it becomes |
|---|---|
| **Vector chart** (slides exported from PowerPoint: bars, stacked bars, pies, donuts) | An **exact** table read from the PDF geometry, with no model (`vector_charts.py`): values, labels, legend, group headings, "Total" columns |
| Data chart that is not vector, or that the vector reader does not fully understand | Table of values (Granite Vision, built into Docling), checked, cross-checked by the vision model, and handed over to the vision model if suspect |
| Pie / donut misread by Granite | Values and legend taken from the PDF text layer, **without** inventing which value goes with which label |
| Diagram, org chart, flow, architecture | Mermaid block (vision model in LM Studio) |
| Photo, screenshot, map, plan | Text description, plus a compressed WebP (1024 px max, local link) |
| Logo, icon, signature, stamp, QR code, repeated image, thumbnail | Removed (the caption is kept) |
| Picture the vision model calls decorative | Removed |

### Vector reading of charts

In a presentation exported to PDF, a chart is not an image: bars and slices are coloured shapes, and every value is PDF text. `vector_charts.py` rebuilds the table from the geometry:

- **slices and stacked segments**: the value sits inside a shape of colour C; the legend swatch of colour C gives the label;
- **simple bars**: the value sits next to the bar (right of it or above it), the label on the same line; each group heading ("Sexe", "Âge"…) gets **its own table** and becomes the header of its first column;
- **columns without bars** ("Total Oui", "Ensemble des Français") are attached to their row, with their header; a **recall** column ("Rappel 2022", a year, "vague", "évolution") goes into a separate table;
- **legend** labels that appear in no table are kept in the "Légende" line;
- **invisible text** (covered by a shape, forgotten in the slide) is ignored, thanks to a render of the page;
- a chart that Docling takes for a **table** (Ipsos stacked bars read as a grid, shifted columns) is re-read the same way; Docling's table is replaced only if the reading succeeds (`vector_tables`);
- bare years above columns without bars ("2024 2020 2017" of a "Rappels" block) are headers: those columns form the recall table;
- a **key figure** in a very large font ("% Trop" / "30") becomes the annotation "% Trop : 30", and its "RAPPELS 2020 : 30% …" list becomes a small Year / value table;
- callout boxes (bullets, or lines starting with "19% chez …") give one annotation per entry; a pale callout background is not a bar.

Safety rule: the table is used only if **every** visible number in the area and every bar is explained. Otherwise the picture takes the usual path below, and the reason is recorded in `_debug/*.pictures.json`. Reading takes a few milliseconds per chart.

### Common chart format (`chart_format.py`)

Whatever the path (vector reading, Granite, text layer, vision model), a numeric chart becomes a block of metadata followed by one or more tables, **one homogeneous dataset per table**:

```
Titre : Qui connaît Alain Soral ? ; Selon le sexe et l'âge
Question : *Vous personnellement, connaissez-vous, ne serait-ce que de nom, Alain Soral ?*
Base : ensemble des Français
Légende : …
Source : …
Annotations : Total Oui : 43%
Lecture : modèle (Granite Vision), valeurs non vérifiées

| Sexe | Valeur |
|---|---|
| Hommes | 52% |
| Femmes | 34% |
```

Only the fields that were found are written. Metadata comes **from the PDF text layer, never from a model**: "Question :", "Base :", "(base: …)", "Source :", or an unlabelled question followed by a base. Question and base go to the first chart below them, the source to the last chart above it, and a text inside a picture's box to that picture; the text is then removed from the main flow so it does not appear twice. A missing value is written "–".

### Where the numbers come from: the `Lecture :` line

The downstream LLM only reads the `.md`, so the Markdown says where model-read numbers come from. `Lecture :` is always the **last** metadata line:

| Source | `Lecture :` line |
|---|---|
| Vector reading (exact) | none: exact is the default, and leaving the line out saves tokens |
| Granite Vision table | `Lecture : modèle (Granite Vision), valeurs non vérifiées` |
| Vision model table, or digits in the vision model's text for a chart | `Lecture : modèle vision, valeurs non vérifiées` |
| Granite and the vision model agree (cross-check) | `Lecture : 2 modèles concordants, valeurs non vérifiées` |
| Irregular table re-read by the vision model | same line, above the table |
| PDF without a text layer (scan) | `Lecture : modèle vision, PDF sans couche texte, chiffres invérifiables` |
| Pie values from the text layer, Mermaid, photo description | none |

When a consistency check fails (see below), its nature is appended: `Lecture : modèle vision, valeurs non vérifiées ; total incohérent`.

### PDFs without a text layer (scans)

A screenshot, a scan, or an image "exported to PDF" has no text layer: the report says `kind: scan`. No exact reading is possible, everything goes through a model, and **no check can verify its numbers** (in v8.2 a screenshot PDF produced a false table of past results that nothing flagged). So for a scan:

- the `.md` starts with `> PDF sans couche texte (scan ou capture) : chiffres lus par un modèle, non vérifiés.`;
- every model-read numeric table is tagged `… PDF sans couche texte, chiffres invérifiables`;
- the report gets a warning;
- with `--scan-charts image`, model-read numbers are **not written at all**: the chart becomes `*Graphique non lu (PDF sans couche texte)*` and its image is kept as WebP.

`kind` is computed on the requested page range. If you can, convert the original PDF instead.

### Triage and Granite Vision fallback

- **Triage**: before conversion, each PDF is profiled in a few milliseconds (text per page, images, shapes) and labelled `texte`, `présentation` or `scan` (report field `kind`).
- **Granite Vision is only a fallback**: the main conversion no longer runs it. Only pages where a chart could not be read as vector are converted again with Granite (report field `granite_pages`). A text PDF therefore never loads Granite, and the 15 test slides went from 167 s to 22 s.
- **OCR stays on everywhere**: Docling's OCR is selective (regions without PDF text, pages with broken or scanned text) and costs only about 5 s per PDF. Measured on a pure-text report, turning it off loses headings that are present in the PDF.

### Quality checks

Checks applied to model output (details in [debug_note.md](debug_note.md)):

- a Granite table is rejected if it has empty labels, rows without numbers, or a column that is too often empty; a "merged" table of two neighbouring charts is split in two;
- **cross-check (v9)**: when Granite's table is accepted and a vision model is configured, the vision model reads the same chart too. The two readings are compared **label → value** (comparing the set of values would miss a swap). They agree when at least 80 % of Granite's rows are found by label and at least 90 % of the compared cells are equal. If they disagree, both tables are dropped: the values from the text layer are written without labels (`*Valeurs lues sans rattachement établi (Granite et modèle vision en désaccord) : …*`), the image is kept, and the report gets a warning. Vector reads are never cross-checked: they are exact. The cross-check costs one extra vision-model call per Granite chart (about 20 s each with Qwen2.5-VL-7B on an Apple Silicon Mac); `--no-cross-check` turns it off;
- **consistency checks (v9, `controles.py`)**: a "Total …" column or row must equal the sum of a contiguous run of at least 2 sibling values (±1 point for rounding); pie values and each row of a stacked bar must sum to 100 (±2 points, or 0.5 point per summed value when there are many). Profile bars ("Hommes 52 %, Femmes 34 %") are never summed. Problems are **flagged, never rejected**: picture note, report warning, and the `Lecture :` line. Vector results go through the same checks as a self-test of the reader: a failure there means a reader bug, is reported as such, and leaves the output unchanged;
- vision model output is rejected if more than half of its numbers are absent from the page's text layer (invented values);
- Mermaid is rejected if it encodes numbers, and the request is retried in table mode;
- no interpretation or "trend" sentences: in table mode, only the title and the table are kept;
- titles and values that Docling "swallows" into the image are restored from the text layer;
- tables with a shifted grid are fixed without a model when the pattern is known (the Ifop "margins of error" table), otherwise re-read by the vision model; the report tells the two apart;
- slide noise removed: page numbers, logo read by OCR, and **repeated footer/header lines (v9)**. A line that appears on at least max(5, 30 % of pages) pages, once digits are ignored, is a footer: its copies are removed and the first occurrence is kept. Table rows, headings, code blocks, image links, italics, bullets and chart metadata lines are never touched, and a line whose varying part is anything other than a bare page number (for example "Ensemble des Français : 6%") is never removed. The removed lines are listed in the report (`lignes_repetees_retirees`).

### Vision model errors

The vision model client sorts errors. An HTTP 4xx error other than 408/429 (for example 400 "this model does not accept images") is permanent: it is logged once and not retried. Timeouts, connection errors, 5xx, 408 and 429 are retried (3 attempts, with backoff). **Circuit breaker**: after 3 consecutive images fail on connection or timeout (LM Studio stopped mid-run), the vision model is turned off for the rest of the run, the remaining images are kept as WebP, and each affected PDF gets the warning `modèle vision désactivé après N échecs consécutifs`.

## Installation (once)

```bash
python3.12 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt    # exact tested versions: requirements.lock
```

- **Python 3.12** is recommended: it is the safest version for torch and MLX (3.14 also worked).
- **Always activate the `.venv`** before running the script from the command line. Without it, `python` is the system Python, where Docling is not installed (`No module named 'docling'`). The `pdf2llm.command` launcher does not have this problem: it calls `.venv/bin/python` directly.
- The `[vlm]` extra installs `mlx-vlm` on Apple Silicon Macs. Without it, Granite Vision would run on the CPU, which is very slow; the script then refuses to start.
- On first run, Docling downloads its models from Hugging Face (layout, tables, picture classifier, Granite Vision, about 8 GB), cached in `~/.cache/huggingface`.

### Moving the project folder

Nothing in the code depends on where the folder is, and the git repository moves with it. Only the `.venv` contains absolute paths: after a move, `source .venv/bin/activate` and `pip` stop working, but `.venv/bin/python`, and therefore the `pdf2llm.command` launcher, keep working. To put everything back in order, recreate the environment (the launcher also offers to do it if it finds the environment broken):

```bash
rm -rf .venv && python3.12 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
```

Downloaded models stay in `~/.cache/huggingface` and are not downloaded again. Avoid a folder synced by iCloud: a `.venv` holds tens of thousands of files.

## Vision model (LM Studio)

1. In LM Studio, load a vision model, for example **Qwen2.5-VL-7B-Instruct** or **Qwen3-VL-8B**.
2. *Developer* tab → *Start server* (port 1234).
3. Note the exact model identifier: `curl http://localhost:1234/v1/models`.

With Ollama, add `--vlm-url http://localhost:11434/v1`.

## Usage

### The easy way: the wizard

Double-click **`pdf2llm.command`** in the Finder, or run `python pdf2llm.py` with no arguments. The wizard:

1. opens a Finder window to pick **a PDF or a folder of PDFs**;
2. suggests an output folder (Enter to accept, `f` to pick one in the Finder);
3. shows the **list of models loaded in LM Studio** and lets you pick one by number (with a clear message if LM Studio is not running, and the option to go on without a vision model);
4. asks a few questions with defaults: pages (for a trial run), re-converting PDFs already done, which images to keep, diagnostic files; advanced options (vector reading, OCR, Granite or vision model for charts, chunk size) are offered only on request;
5. shows a summary and the **equivalent command**, to copy and re-run without the wizard.

The last choices are saved in `~/.config/pdf2llm/derniers_choix.json` and offered next time.

To put it on the Desktop: right-click `pdf2llm.command` → *Make Alias*, then move the alias. On first launch, macOS may ask you to confirm opening it (right-click → *Open*). When the run ends, the window says whether it finished cleanly, with warnings, or with errors.

### Command line

```bash
# 1) Quick test on 15 pages: check the result before running everything
python pdf2llm.py ./pdfs/etude_exemple.pdf ./sortie --pages 1-15 --vlm-model qwen2.5-vl-7b-instruct

# 2) A whole folder
python pdf2llm.py ./pdfs ./sortie --vlm-model qwen2.5-vl-7b-instruct

# 3) Diagnostics: adds sortie/_debug/ (Docling JSON + a record for each picture)
python pdf2llm.py ./pdfs/etude_exemple.pdf ./sortie --pages 1-15 --vlm-model qwen2.5-vl-7b-instruct --force --dump-debug 2>&1 | tee run.log
```

The script can be stopped and restarted: PDFs already converted are skipped, and the vision model's answers are cached (`sortie/.vlm_cache.json`).

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Every PDF converted, no warnings |
| `1` | At least one PDF failed |
| `2` | Everything converted, but at least one report has warnings: irregular table, PDF without a text layer, inconsistency, Granite / vision model disagreement, vision model turned off by the circuit breaker |

### Output files

| File | Content |
|---|---|
| `name.md` | The Markdown for the LLM |
| `name_assets/*.webp` | Kept images (photos, maps…), depending on `--keep-images` |
| `name.report.json` | Report for the PDF (fields below) |
| `_resume.json` | Reports for all PDFs |
| `_debug/` | With `--dump-debug`: `*.docling.json` (full Docling document) and `*.pictures.json` (class, page, box, decision, raw model output, note for each picture) |

Fields of `name.report.json`:

| Field | Content |
|---|---|
| `kind` | `texte`, `présentation` or `scan` |
| `actions`, `labels` | Decision per picture (`vector`, `chart_table`, `vlm`…) and picture classes |
| `granite_pages` | Pages converted again with Granite Vision |
| `vector_tables` | Docling "tables" re-read as vector charts |
| `md_chars`, `md_tokens_est`, `webp_bytes` | Output size |
| `noise_lines_removed` | Slide numbers and OCR logo lines removed |
| `lignes_repetees_retirees` | Repeated footer/header lines removed: `{text of the first occurrence: copies removed}` |
| `durees` | Seconds per stage: `triage`, `docling`, `vectoriel`, `granite`, `vlm` (including cross-checks), `tableaux` (including vision model re-reads of tables), `serialisation` |
| `ram_max_mo` | Peak resident memory of the process so far, in MB |
| `seconds` | Total time for the PDF |
| `warnings` | Warnings: irregular table (to check, fixed without a model, or re-read by the vision model), scan, inconsistency, cross-check disagreement, circuit breaker, vector self-test failure |

The log also prints one line per PDF, for example `durées : docling 14 s, granite 210 s, vlm 31 s ; RAM max 9,8 Go`.

## Options

| Option | Effect |
|---|---|
| `--pages 1-15` | Converts only this page range |
| `--force` | Re-converts PDFs already done |
| `--dump-debug` | Writes `sortie/_debug/` to diagnose without re-converting |
| `--no-vector` | Turns off vector reading of charts (everything goes through Granite Vision / the vision model) |
| `--charts vlm` | Sends charts to the LM Studio model instead of Granite Vision (machine without a GPU) |
| `--chart-api-url http://localhost:1234/v1` | Runs Granite Vision in LM Studio (model `granite-vision-4.1-4b`, set with `--chart-api-model`) |
| `--mlx` | Forces the MLX preset of Granite Vision (not needed in practice: chosen automatically when `mlx-vlm` is installed) |
| `--no-cross-check` | No cross-check of Granite tables by the vision model (on by default when a vision model is configured) |
| `--scan-charts tag / image` | PDFs without a text layer: write model-read numbers with the "invérifiables" tag (default), or do not write them and keep the image |
| `--keep-images none / visual / all` | WebP kept: none / photos, screenshots, maps, plans (default) / all |
| `--no-vlm` | No vision model: charts as tables, everything else kept as WebP |
| `--no-ocr` | Turns OCR off. **Not recommended**: saves about 5 s per PDF, but Docling then loses text (headings, bullets), even in native PDFs |
| `--chunk-pages 40` | Converts in chunks (less RAM; default 80) |
| `--no-table-fallback` | Does not ask the vision model to re-read tables with an irregular grid |
| `--no-denoise` | Keeps slide numbers, the OCR logo and repeated footer/header lines |
| `--no-cell-matching` | TableFormer without *cell matching* (try it if tables are shifted) |
| `--vlm-workers`, `--vlm-timeout`, `--vlm-max-tokens` | Settings for vision model calls |
| `--min-area`, `--min-px`, `--repeat-threshold`, `--drop-min-conf` | Thresholds for dropping small images, repeated images and logos |

To change the instructions given to the vision model, edit `VLM_PROMPT` or `VLM_TABLE_PROMPT` at the top of the script and bump `PROMPT_VERSION` to invalidate the cache.

## Tests

```bash
python test_pdf2llm.py        # or: python -m pytest test_pdf2llm.py
```

The unit tests use fake data and need no files. The vector-reading tests generate their own small PDFs (bars with group headings and hidden text, pies with a legend, stacked bars with a "Total" column, refusal when a number stays unexplained).

### Golden tests (v9)

A golden test converts a whole PDF and compares the Markdown, plus the stable report fields (`kind`, `actions`, `granite_pages`, `vector_tables`, `warnings`), with an expected version that has been reviewed. Every change to the output must show up as a golden diff and be re-baselined on purpose.

- **Synthetic (committed)**: `tests/golden_gen.py` generates 3 small multi-page slide decks (bars with group headings, donut with a legend, stacked bars with a "Total" column, recall block, key figure, one chart that must be refused). They are converted without any model (`--no-vlm`, Granite off). The expected outputs are in `tests/golden/`. `python test_pdf2llm.py` runs them.
- **Local (not committed)**: your own PDFs, in the folder given by `PDF2LLM_GOLDEN_DIR` (default `tests/fixtures/golden/`, ignored by git): `x.pdf`, the expected `x.attendu.md` (and `x.attendu.report.json`), and an optional `x.args` file with the command-line options (for example `--pages 10-25 --vlm-model qwen2.5-vl-7b-instruct`; without it, `--no-vlm`). Outputs go to `<folder>/_sortie/`, which also keeps the vision model cache, so a re-run reads the same answers. The test suite runs them only when `PDF2LLM_GOLDEN_DIR` is set.

```bash
python tools/golden.py check            # unified diff per file, exit code 1 on any difference
python tools/golden.py update           # rewrite the expected files after reviewing the diff
python tools/golden.py check --only g3_refus --synthetiques
PDF2LLM_GOLDEN_DIR=~/pdf-golden python tools/golden.py check --locaux
```

### Other real-file tests

Two more tests use real files and are skipped when they are missing:

- `tests/fixtures/exemple.p1-15.docling.json` (or `PDF2LLM_FIXTURE_JSON`): a `--dump-debug` export of the development PDF;
- `PDF2LLM_TEST_PDF`: the development PDF itself (a 21-page Ifop survey), for vector reading.

These files are not committed. The expected values match that PDF.

## Code layout

| Part | Role |
|---|---|
| `wizard.py`, `pdf2llm.command` | Interactive wizard (Finder, list of LM Studio models, questions, saved choices; `answers_to_argv` turns answers into options) and double-click launcher |
| `chart_format.py` (separate module) | Common chart format: `ChartMeta` (including `lecture`), `split_meta` (question, base, source), `render`; in `pdf2llm.py`, `build_chart_blocks` attaches page metadata to the right chart |
| `vector_charts.py` (separate module) | Words, lines, shapes and legend of each page (pypdfium2); visibility from a render (freed once computed); `read_region` rebuilds the chart of an area or raises `Unreadable` |
| `controles.py` (separate module) | Arithmetic consistency checks of survey tables: `controles_coherence`, `md_tables` |
| Picture triage, prompts | Docling picture classes (chart, to drop, visual); vision model prompts |
| Image utilities, `VlmClient` | Downscaling, repeated-image fingerprint, WebP; OpenAI-compatible API client with cache, error sorting and circuit breaker |
| Quality checks | `chart_rows_problem`, `split_sparse`, `invented_values`, `vlm_output_problem`, `keep_table_only`, `denoise_markdown`, `repeated_lines` |
| PDF text layer | `text_items_near`, `pie_text_block`, `uncovered_titles`, `uncovered_texts`: text that Docling puts under pictures |
| Shifted tables | `irregular_tables`, `fixed_table_md`, `decide_tables` |
| Granite patch | `tolerant_csv_to_dataframe` replaces an internal Docling 2.149 function (labels with a comma); upstream draft in `upstream/docling-granite-csv.md` |
| Per-picture decision | `decide_pictures`: vector reading first, then the usual decision tree, Granite × vision model cross-check (`compare_readings`, `apply_cross_check`), parallel vision model calls |
| Serialisation | Docling Markdown serializers overridden to substitute pictures and tables |
| Triage, Granite fallback | `profile_pages`, `pdf_kind`, `ConverterPool` (converters built on demand), `granite_fallback` (re-converts only the pages with an unread chart) |
| Conversion, `main` | Converter options, chunking, timings (`Chrono`), reports, resume, exit codes; `build_parser` / `run` |
| `tools/golden.py`, `tests/` | Golden tests: runner, synthetic deck generator, shared PDF builders (`tests/pdfgen.py`), expected outputs |

## Known limits and next steps

- The check thresholds were calibrated on a single document (Ifop survey); the golden corpus is there to validate them on other PDFs.
- `fixed_table_md` only recognises the Ifop "margins of error" pattern.
- Vector reading covers bars (simple, stacked, horizontal or vertical) and pies. **Line charts** (series told apart by the colour of their value text) and large **tables drawn as free text** are not read yet: they go through Granite Vision, which renders them poorly (Ipsos report p. 57: only one of the series found).
- **Hidden text** is filtered only inside charts; outside charts, Docling still extracts it as normal text.
- Consistency checks only flag problems in v9; rejecting tables on that basis will come once the flags have been checked on a larger corpus.

Version history, problems met and checks: [debug_note.md](debug_note.md) (in French).
