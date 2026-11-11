# Draft: Granite Vision chart CSV fails on labels that contain a comma

**Status: draft only.** Nothing has been opened upstream. Checked on 2026-11-11 against
docling 2.149.0 (installed), 2.150.0 (latest PyPI release) and the `main` branch: all three
still parse the model output with a plain `pd.read_csv`.

## Problem

`docling/models/stages/chart_extraction/granite_vision.py::_extract_csv_to_dataframe`
parses the `<chart2csv>` output of Granite Vision with:

```python
return pd.read_csv(StringIO(csv_content), header=None)
```

Granite Vision does not quote labels that contain a comma. Survey charts often have such
labels ("Yes, and you know exactly who", "No, not really"). Pandas then sees one field too
many on that line, raises `ParserError`, and the chart gets no `tabular_chart` at all: the
whole chart is lost because of one label.

## Minimal reproduction

```python
import pandas as pd, io
csv = "Label,Value\nYes, and you know exactly who,23%\nNo,57%"
pd.read_csv(io.StringIO(csv), header=None)
# pandas.errors.ParserError: Error tokenizing data. C error: Expected 2 fields in line 2, saw 3
```

Observed in practice on French survey slides (Ifop), several charts per deck.

## Proposed patch

Read with the `csv` module first (so properly quoted fields still work). When a line has
more fields than the header, split it **from the right**: values are on the right, the
free-text label is on the left, so the extra commas belong to the label.

```python
import csv

def _extract_csv_to_dataframe(decoded_text: str) -> pd.DataFrame:
    """Parse CSV from the V4 model output (raw generated tokens, no wrapper).

    Granite Vision does not quote labels containing a comma: when a line has more
    fields than the header, the extra commas belong to the label (leftmost field)."""
    csv_match = re.search(r"```csv\s*\n(.*?)\n```", decoded_text, re.DOTALL)
    if csv_match:
        content = csv_match.group(1).strip()
    else:
        content = re.sub(r"^```+(?:csv)?\s*", "", decoded_text.strip())
        content = re.sub(r"```+\s*$", "", content).strip()
    lines = [l for l in content.splitlines() if l.strip()]
    if not lines:
        raise ValueError("empty CSV")
    n = len(next(csv.reader([lines[0]])))
    rows = []
    for line in lines:
        try:
            cells = next(csv.reader([line]))
        except Exception:
            cells = [line]
        if len(cells) != n:
            cells = line.rsplit(",", n - 1) if n > 1 else [line]
        cells = [c.strip() for c in cells]
        cells += [""] * (n - len(cells))
        rows.append(cells)
    return pd.DataFrame(rows)
```

Notes for the PR:

- The result is a DataFrame of strings. `_dataframe_to_tabledata` already handles
  string cells; a follow-up could convert numeric columns with `pd.to_numeric(errors="ignore")`
  if typed values are wanted.
- A line with *fewer* fields than the header is padded with empty cells instead of failing.
- Suggested test: the reproduction above, plus a quoted label (`"Yes, sure",23%`) to show
  that the standard quoting still works.

In pdf2llm, the same function is `tolerant_csv_to_dataframe`, installed at start-up by
`install_granite_csv_patch` (fix P1 in `debug_note.md`).
