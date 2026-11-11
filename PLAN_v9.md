# PLAN v9: work orders for Claude Code

This plan comes from two outside reviews of pdf2llm v8.2 (commit `52e7714`), checked against the code. Only the points that hold up are kept. The points that were rejected are listed at the end, so they don't get reintroduced.

Work through the tasks **in order**. Phase 1 has to land before anything changes the output.

---

## 0. Ground rules (apply to every task)

1. **Project priority is unchanged:** exact numbers first, then fewer tokens, then speed. Missing data beats wrong data. If a task would trade accuracy for speed, stop and ask.
2. **Language:** code comments, log messages, README, `debug_note.md` and report fields stay **in French**, the same as the existing code. This plan is in English only for its reader.
3. **No silent output changes.** After Phase 1, every change to the Markdown output must show up as a golden-file diff, be reviewed, and be re-baselined on purpose in the same commit, with a short note in the commit message.
4. **Don't touch existing thresholds** (34 % empty cells, 50 % / 10 % missing values, 0.55 logo similarity, vector-reading tolerances) unless a task explicitly says to.
5. **Don't change `VLM_PROMPT`, `VLM_TABLE_PROMPT` or `PROMPT_VERSION`.** No task here needs it.
6. **One commit per task.** Run `python test_pdf2llm.py` before each commit. All tests must pass.
7. **Keep the docs current:**
   - add a row to the `debug_note.md` § 2 trial table, and a § 3 entry for each fix;
   - document every new option or report field in `README.md`;
   - when all tasks are done, refresh the `debug_note.md` header. It still says "note n° 5 … v7.1" although the content goes up to v8.2. Make it note n° 6, code v9.
8. **Already implemented, do not redo:**
   - the vector-reading failure reason is already logged (`vec.last_reason` → `d.note` → `_debug/*.pictures.json`);
   - page renders are cached per page (`VectorDocument.page`);
   - a VLM failure already falls back to keeping the image;
   - PDFs already converted are skipped, and the VLM cache survives restarts;
   - repeated *images* are already dropped (`ahash` + `--repeat-threshold`).

---

## Phase 1: Measure and build a safety net (do first)

### T1. Per-stage timing and peak RAM in `report.json`

**Why:** every "what's slow" question is guesswork today. `debug_note.md` § 7.7 says RAM has never been measured.

**What:**
- Add to `DocReport`:
  - `durees: dict[str, float]`: seconds per stage, with keys `triage`, `docling`, `vectoriel`, `granite`, `vlm`, `tableaux`, `serialisation`;
  - `ram_max_mo: float`: peak RSS of the process.
- Time with `time.perf_counter()`.
  - `vectoriel`, `granite` and `vlm` happen inside `decide_pictures`. Pass in an accumulator dict, or wrap the `granite` closure and `vec.read`.
  - `vlm` time also covers the `decide_tables` VLM re-reads. Count those under `tableaux`.
- Peak RSS: use `resource.getrusage(resource.RUSAGE_SELF).ru_maxrss`. **It returns bytes on macOS and kilobytes on Linux**, so normalise to MB.
- Add one log line per PDF, for example: `durées : docling 14 s, granite 210 s, vlm 31 s… ; RAM max 9,8 Go`.

**Acceptance:**
- The fields appear in `nom.report.json` and `_resume.json`.
- The stage times add up to roughly `seconds` (within 10 %).
- A unit test covers the macOS/Linux unit normalisation.

### T2. Golden-file regression tests

**Why:** today, "output unchanged" is checked by eye ("Fractures p. 10-25 identique"). This is the safety net for everything that follows.

**What:**
1. **Synthetic goldens (committed):**
   - Add `tests/golden_gen.py`. It reuses the `_pdf` / `_txt` / `_rect` / `_poly` helpers from `test_pdf2llm.py`; move them to a shared module if that's cleaner.
   - It generates 3 or 4 deterministic multi-page slide PDFs covering:
     - simple bars with group headings;
     - a donut with a legend;
     - stacked bars with a "Total" column;
     - a recall block;
     - a key figure with a callout;
     - one chart that must be **refused** (an unexplained number).
   - `*.pdf` is gitignored, so commit the generator, not the PDFs.
   - Commit the expected outputs as `tests/golden/<nom>.md` plus `tests/golden/<nom>.report.json`, reduced to stable fields: `kind`, `actions`, `granite_pages`, `vector_tables`, `warnings`. Leave out timings and sizes.
   - Run these with `--no-vlm`. They must not need Granite (every chart is vector-readable, or refused and kept as an image).
2. **Local goldens (not committed):**
   - Add the env var `PDF2LLM_GOLDEN_DIR`. It points to a folder of `x.pdf` plus `x.attendu.md` pairs, plus an optional `x.args` file holding the CLI flags, such as `--pages 10-25`.
   - Add a test that converts each PDF and diffs against the expected Markdown. Skip it when the env var is missing, like the existing real-PDF tests.
   - Add `tests/fixtures/golden/` to `.gitignore` if that's the suggested default location.
3. **Tool:** add `python tools/golden.py check|update [--only nom]`.
   - `check` prints a unified diff per file and exits with a non-zero code on any difference.
   - `update` rewrites the expected files and lists what changed.

**Acceptance:**
- `python test_pdf2llm.py` runs the synthetic goldens.
- The user can baseline their real PDFs: Ifop p. 1-15, Ipsos Fractures p. 10-25, Ipsos antisémitisme 68 p., and the Assemblée nationale text report.
- **Ask the user to run `tools/golden.py update` on their local set before starting Phase 2.**

---

## Phase 2: Accuracy (the core of v9)

### T3. Provenance tag in the Markdown for model-read data

**Why:** the downstream LLM only reads the `.md`. Today it can't tell a table read exactly from PDF geometry from a table a 4B model guessed. The information exists in `PicDecision.action` and `reason`, but it never reaches the Markdown.

**What:**
- Add a `lecture: str = ""` field to `ChartMeta` (`chart_format.py`), rendered as the **last** metadata line: `Lecture : …`.
- **Vector reads get no tag.** Exact is the default, and leaving the tag off saves tokens.
- Set it in `build_chart_blocks` (or right before it) from the decision:
  - `chart_table` (Granite) → `Lecture : modèle (Granite Vision), valeurs non vérifiées`
  - `vlm` with a table, or text containing digits → `Lecture : modèle vision, valeurs non vérifiées`
  - `text_layer` (pie fallback) → none. `pie_text_block` already says that the label/value link isn't established.
- For tables re-read by the VLM in `decide_tables`, add the same line above the table.
- Make the `(page, md)` tuple carry its source (`"regle"` or `"vlm"`). Then fix the open issue in `debug_note.md` § 5: the report warning must tell "corrigé sans modèle" apart from "relu par le modèle vision".
- Mermaid blocks and photo descriptions: no tag.

**Acceptance:**
- The synthetic goldens are unchanged (all vector).
- In the local goldens, the only diffs are the added `Lecture :` lines. Re-baseline them.
- A unit test covers each source.

### T4. Scanned PDFs: flag numbers that can't be checked

**Why:** for `kind == "scan"` there is no text layer, so `invented_values` checks nothing. In v8.2, a screenshot PDF produced a false table of past results and nothing flagged it (`debug_note.md` § 2, trial 13).

**What:**
- When `rep.kind == "scan"`:
  - every model-read numeric table gets `Lecture : modèle vision, PDF sans couche texte, chiffres invérifiables`, replacing the T3 wording;
  - prepend one line at the top of the `.md`: `> PDF sans couche texte (scan ou capture) : chiffres lus par un modèle, non vérifiés.`;
  - add a report warning.
- Add `--scan-charts {tag,image}`, default `tag`. With `image`, numeric model output is not written for scans. The image is kept as WebP and the chart becomes `*Graphique non lu (PDF sans couche texte)*`.
- Note: `kind` is computed for the requested page range. Use that value as it is.

**Acceptance:**
- A unit test with a fake one-image PDF profile.
- README section "PDF sans couche texte" updated.

### T5. Consistency checks on tables (flag first, never reject)

**Why:** the existing checks catch invented values but **not swapped ones** (`debug_note.md` § 4.3). Survey data has built-in arithmetic you can check. This is already in the roadmap (§ 7.4).

**What:** write a pure function `controles_coherence(rows, kind_hint) -> list[str]`. It returns readable problems, for example:
- **Totals:** a column or row whose header starts with `Total` (e.g. "Total Oui", "Total Proche") must equal the sum of a contiguous run of at least 2 sibling values. Tolerance ±1 point, for rounding. Flag if no run matches.
- **Sum to 100:** apply this **only** to:
  - pie/donut data, meaning the classifier label is `pie_chart`, or the vector result kind is "secteurs";
  - each row of a stacked bar.

  Tolerance ±2 points. **Never apply it to profile bars** ("Hommes 52 %, Femmes 34 %"): those don't sum to 100.

Run it on:
- (a) Granite and VLM tables. Add the problems to `d.note`, to `report.json` → `warnings` (`p. N : incohérence …`), and append them to the T3 tag, e.g. `Lecture : modèle vision, valeurs non vérifiées ; total incohérent`;
- (b) **vector results too, as a self-test of the reader.** These should always pass. A failure points to a reader bug: log it as a warning with the page number, and leave the output unchanged.

**Do not reject anything in v9.** Flag only. Rejection comes later, once the flags have been checked on the local corpus.

**Acceptance:**
- Unit tests:
  - Ifop p. 9 donut: 2 + 11 = 13 passes;
  - a stacked row summing to 97 passes;
  - a stacked row summing to 90 is flagged;
  - profile bars are not checked.
- Zero flags on the vector outputs of the local goldens. If there are any, report them to the user; don't "fix" them silently.

### T6. Granite × VLM cross-check on charts that can't be read by geometry

**Why:** for a non-vector chart, Granite and the VLM are two independent, fallible readings. Today an accepted Granite table is never compared with anything. Their agreement is a cheap check against swapped values. **Don't** cross-check vector reads: they are exact by construction, and running Granite on them would undo the v7 speed-up (167 s → 22 s).

**What:**
- In `decide_pictures`, when a chart's Granite table passes `chart_rows_problem` and a VLM is available, also queue a VLM `"table"` call for that picture, with the existing hint and prompt. Add it to the existing `ThreadPoolExecutor` batch so it runs in parallel with the other calls.
- Compare **label → value**, not the multiset of values (a multiset can't detect a swap):
  - normalise labels with `_norm`, then match rows by label;
  - compare the numbers cell by cell;
  - there is agreement when ≥ 90 % of the matched cells are equal and ≥ 80 % of the Granite rows are matched.
- **On agreement:** keep Granite. The T3 tag becomes `Lecture : 2 modèles concordants, valeurs non vérifiées`.
- **On disagreement**, follow "missing beats wrong":
  - drop both tables;
  - write the values from the text layer without attaching them to labels (reuse the `uncovered_texts` / `pie_text_block` approach): `*Valeurs lues sans rattachement établi (Granite et modèle vision en désaccord) : …*`;
  - keep the WebP;
  - add a report warning.
- Add `--no-cross-check` to turn it off. It is **on by default** when a VLM is configured. Its cost appears in T1's `vlm` timing.

**Acceptance:**
- Unit tests with mocked Granite rows and VLM output: agreement, swapped values (must count as disagreement), partial label match.
- Report the time it adds on Ipsos p. 55-60 (Granite pages 56-57) to the user.

---

## Phase 3: Robustness and speed

### T7. Free the page bitmap in `VectorPage`

**Why:** `VectorPage.__init__` stores the 2× render as `int16` (`vector_charts.py:143`). That's about 12 MB per 16:9 slide, kept for every page until the PDF is done: around 1.5 GB on a 128-slide deck, on top of Docling, Granite and the VLM. Visibility is fully computed in `__init__` (`_ink` at line 230, `_shows_color` at line 306), and nothing reads `self.px` afterwards.

**What:**
- `del self.px` (and the PIL image) at the end of `__init__`.
- Check with grep that no other method uses `px`. If something does, compute what it needs in `__init__` instead.

**Acceptance:**
- Goldens identical.
- Report T1's `ram_max_mo` before and after on the 68-page Ipsos deck.

### T8. VLM client: sort errors and add a circuit breaker

**Why:** `VlmClient.ask` retries every error 3 times. A permanent error (e.g. a 400 "model doesn't accept images") wastes 12 s per image. If LM Studio dies mid-run, every remaining image goes through up to 3 timeouts of 180 s each.

**What:**
- **No retry** on HTTP 4xx, except 408 and 429. Log the response body (truncated to 300 characters) once.
- **Retry with backoff** on timeouts, connection errors, 5xx, 408 and 429. Keep the current 3 attempts.
- **Circuit breaker:** after 3 *consecutive* images fail on connection or timeout, disable the client for the rest of the run:
  - `ask` returns `None` straight away, so those images are kept as WebP, as today;
  - log one error;
  - add one report warning per affected PDF: `modèle vision désactivé après N échecs consécutifs`.
- It must be thread-safe: the counter and flag go under the existing `_lock`.

**Acceptance:** unit tests with mocked `requests.post`:
- a 400 → 1 call;
- a timeout → 3 calls;
- 3 images with connection errors → the 4th image triggers no call.

### T9. Remove repeated footer and header text

**Why:** `debug_note.md` § 5: the Ipsos footer appears on 123 of 128 pages and `denoise_markdown` doesn't remove it. This is text, not an image, so the existing image hash doesn't help.

**What:**
- In `process_pdf`, after all chunks are joined, before `denoise_markdown`, find lines to drop:
  - normalise each line (lowercase, digits and extra spaces removed);
  - drop it when it appears at least `max(5, 30 % of pages)` times.
- **Never consider these lines:**
  - table rows (`|`), headings (`#`), code fences, image links;
  - chart metadata lines: `Titre :`, `Question :`, `Base :`, `Légende :`, `Source :`, `Annotations :`, `Lecture :`. These repeat legitimately, e.g. "Base : ensemble des Français".
- Record the removed lines in `report.json` → `lignes_repetees_retirees: {texte: nombre}` so it can be audited. Add a `--no-denoise` bypass (reuse the existing flag).

**Acceptance:**
- A unit test with a synthetic 10-page Markdown.
- On the local goldens, only footer or header lines disappear. Ask the user to review the diff before re-baselining.

### T10. Exit codes

**What:**
- `0`: all PDFs converted with no warnings.
- `1`: at least one PDF failed (unchanged).
- `2`: all PDFs converted, but at least one report has warnings: irregular table, T4 scan, T5 inconsistency, T6 disagreement, T8 breaker.
- Document this in the README.
- Check that `wizard.py` and `pdf2llm.command` don't treat a non-zero exit as a crash. If they do, make them show "terminé avec avertissements" for code 2.

### T11. Spike: Granite fallback without converting the pages twice

**Gate:** only start this if T1 shows `granite` above 25 % of total time on the 68-page Ipsos deck. Otherwise write down the numbers in `debug_note.md` and skip it.

**Why:** `granite_fallback` currently re-runs the **whole** Docling pipeline on fallback pages (layout, TableFormer ACCURATE, OCR, classification) just to get `tabular_chart`. The chart crops are already in memory (`images[pic.self_ref]`).

**What (investigate first, then propose before implementing):**
1. Read Docling 2.149's chart-extraction stage in the installed package (`.venv/lib/python3.12/site-packages/docling…`). Find out whether the enrichment model can run on existing `PictureItem`s of the main `DoclingDocument`, given `(doc, element)` pairs, without converting again.
2. If yes, run it on the pending chart pictures of the main document. Remove `granite_fallback`'s reconversion. Keep the IoU-matching code only if it's still needed.
3. If not, at least for `--chart-api-url`: send the crop straight to the API, with Docling's Granite chart prompt and the same CSV parsing (the `tolerant_csv_to_dataframe` path).
4. **Stop and report to the user** with the findings and expected gain before writing the change.

**Acceptance:**
- Same tables on Ipsos p. 56-57 (golden diff empty).
- Lower `granite` time in T1.

---

## Phase 4: Upstream and housekeeping

### T12. Draft the upstream fix for the Granite CSV patch

**What:**
- Check whether the latest Docling release still parses Granite's CSV strictly in `granite_vision._extract_csv_to_dataframe` (labels with commas break `pd.read_csv`).
- If it does, write `upstream/docling-granite-csv.md`: the problem, a minimal reproduction, and the proposed patch taken from `tolerant_csv_to_dataframe`.
- **Do not open an issue or PR.** The user decides.
- If it's already fixed upstream, note the version in `debug_note.md` § 7.6.

### T13. Documentation pass

**What:**
- README:
  - new options: `--scan-charts`, `--no-cross-check`;
  - new report fields: `durees`, `ram_max_mo`, `lignes_repetees_retirees`;
  - the `Lecture :` metadata line;
  - exit codes;
  - golden tests and `PDF2LLM_GOLDEN_DIR`.
- `debug_note.md`:
  - header → note n° 6, code v9;
  - § 2 trial rows;
  - § 3 fixes;
  - move the items done from § 5/§ 7.

### T14. (Optional) CI

Only after asking the user. Add a GitHub Actions workflow running `python test_pdf2llm.py` (unit tests plus synthetic goldens) on `ubuntu-latest`, Python 3.12.

First check that `requirements.lock` installs on Linux. It may pin macOS-only wheels such as `mlx`. If it doesn't, install from `requirements.txt` with the Docling pin. The tests must not download Granite or need a VLM.

---

## Rejected, do not implement

| Suggestion | Why it was rejected |
|---|---|
| Fast path that skips OCR / picture classification, or `--no-ocr` on native PDFs | Measured in `debug_note.md` § 4.11: OCR costs about 5 s per PDF, and without it Docling loses text that's in the PDF. Classification drives logo removal and fallback routing. |
| Smaller or more quantised VLM for speed | Qwen 7B already misattributes values (§ 4.2). Speed must not cost exactness. |
| Raise the default `--vlm-workers` | VLM calls are rare on vector decks. Look at the T1 numbers first; the user can already set the flag. |
| Two-pass low-res render in `vector_charts.py` | The render takes milliseconds, which is negligible next to Docling. |
| SQLite cache, warm-up call, base64 caching | Gains of milliseconds. |
| CLI flags for the quality thresholds | Shifts calibration onto the user. The fix is the golden corpus (T2). |
| `--profile-only` with no Docling | Impossible: vector reading needs Docling's picture boxes. |
| Cross-checking vector reads against Granite | Vector reads are exact. It would undo the v7 speed-up. (The useful version is T6.) |
| Perceptual hash for repeated images | Already implemented (`ahash`, `--repeat-threshold`). The real problem was text (T9). |
| Formula → LaTeX, DocTags/JSON output | Outside the scope (survey decks). The Docling JSON already exists through `--dump-debug`. |
| Package split, PyPI, English README, OmniDocBench benchmark, Linux/CUDA work | **Postponed, not rejected.** Only relevant if the user decides to publish the tool for others. Ask before starting. |