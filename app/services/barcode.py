"""Code 128 barcodes as inline SVG, for the SI numbers on printed report sheets.

Code 128 set B covers every printable ASCII character, so any SI or serial number fits. Long runs of
digits switch to set C (two digits per symbol) to keep the code short. No outside library is needed.
"""
from __future__ import annotations

from markupsafe import Markup, escape

# bar and space widths for symbols 0..106 (106 is the stop symbol, with its extra closing bar)
_PATTERNS = (
    "212222 222122 222221 121223 121322 131222 122213 122312 132212 221213 221312 231212 112232 122132 122231 113222 "
    "123122 123221 223211 221132 221231 213212 223112 312131 311222 321122 321221 312212 322112 322211 212123 212321 "
    "232121 111323 131123 131321 112313 132113 132311 211313 231113 231311 112133 112331 132131 113123 113321 133121 "
    "313121 211331 231131 213113 213311 213131 311123 311321 331121 312113 312311 332111 314111 221411 431111 111224 "
    "111422 121124 121421 141122 141221 112214 112412 122114 122411 142112 142211 241211 221114 413111 241112 134111 "
    "111242 121142 121241 114212 124112 124211 411212 421112 421211 212141 214121 412121 111143 111341 131141 114113 "
    "114311 411113 411311 113141 114131 311141 411131 211412 211214 211232 2331112"
).split()
START_B, START_C, CODE_B, CODE_C, STOP = 104, 105, 100, 99, 106


def _symbols(text: str) -> list[int]:
    out: list[int] = []
    i, n, mode = 0, len(text), None
    while i < n:
        run = 0
        while i + run < n and text[i + run].isdigit():
            run += 1
        # four or more digits in a row go two to a symbol (set C); an odd one left over goes in set B
        if run >= 4:
            pairs = run - run % 2
            if mode != "C":
                out.append(START_C if mode is None else CODE_C)
                mode = "C"
            for k in range(i, i + pairs, 2):
                out.append(int(text[k:k + 2]))
            i += pairs
            continue
        if mode != "B":
            out.append(START_B if mode is None else CODE_B)
            mode = "B"
        ch = ord(text[i])
        if not 32 <= ch <= 126:
            raise ValueError("Code 128 set B takes printable ASCII only")
        out.append(ch - 32)
        i += 1
    check = out[0] + sum(k * s for k, s in enumerate(out[1:], 1))
    return out + [check % 103, STOP]


def modules(text: str) -> list[int]:
    """Widths, alternating bar and space, starting with a bar."""
    return [int(c) for s in _symbols(text) for c in _PATTERNS[s]]


def svg(text: str, module_mm: float = 0.254, height_mm: float = 7.0) -> Markup:
    """The barcode for ``text`` as an SVG element, sized in millimetres so printers keep the bars exact.
    Empty, or characters a barcode cannot carry, give an empty string (the SI is printed beside it anyway)."""
    text = (text or "").strip()
    if not text:
        return Markup("")
    try:
        widths = modules(text)
    except ValueError:
        return Markup("")
    quiet = 10
    x, bars = quiet, []
    for k, w in enumerate(widths):
        if k % 2 == 0:
            bars.append(f"M{x} 0h{w}v1h-{w}z")
        x += w
    total = x + quiet
    return Markup(
        f'<svg class="barcode" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total} 1" preserveAspectRatio="none" '
        f'width="{total * module_mm:.2f}mm" height="{height_mm}mm" role="img" aria-label="Barcode {escape(text)}">'
        f'<path d="{"".join(bars)}" fill="#000" shape-rendering="crispEdges"/></svg>'
    )
