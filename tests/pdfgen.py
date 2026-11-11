"""Petits PDF factices (slides 960 x 540) pour les tests : texte Helvetica, rectangles et
polygones remplis. Partagé par test_pdf2llm.py et tests/golden_gen.py.

Le contenu d'une page est une liste d'opérateurs PDF produits par `txt`, `rect`, `poly`.
Sortie déterministe : mêmes opérateurs -> mêmes octets."""
from __future__ import annotations

from pathlib import Path

TEAL, CORAL, GREY = (0.18, 0.4, 0.4), (0.98, 0.55, 0.52), (0.6, 0.6, 0.6)


def pdf_pages(pages: list[list[str]], path: Path) -> Path:
    """PDF de plusieurs pages 960 x 540 ; chaque page = une liste d'opérateurs
    (Helvetica standard, encodage WinAnsi pour les accents)."""
    n = len(pages)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n)).encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % n,
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"]
    for i, ops in enumerate(pages):
        content = "\n".join(ops).encode("cp1252")
        objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 960 540] "
                    b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % (5 + 2 * i))
        objs.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
    out, offs = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offs)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    path.write_bytes(bytes(out))
    return path


def pdf(ops: list[str], path: Path) -> Path:
    """PDF d'une page."""
    return pdf_pages([ops], path)


def txt(x, y, s, size=10, rgb=(0.25, 0.25, 0.25)):
    s = s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return f"BT /F1 {size} Tf {rgb[0]} {rgb[1]} {rgb[2]} rg {x} {y} Td ({s}) Tj ET"


def rect(x, y, w, h, rgb):
    return f"{rgb[0]} {rgb[1]} {rgb[2]} rg {x} {y} {w} {h} re f"


def poly(pts, rgb):
    path = " ".join(f"{x} {y} {'m' if i == 0 else 'l'}" for i, (x, y) in enumerate(pts))
    return f"{rgb[0]} {rgb[1]} {rgb[2]} rg {path} h f"


def frame(x, y, w, h, rgb=(0.7, 0.7, 0.7)):
    """Cadre non rempli (trait) : aide la mise en page de Docling à voir une figure."""
    return f"{rgb[0]} {rgb[1]} {rgb[2]} RG 0.8 w {x} {y} {w} {h} re S"
