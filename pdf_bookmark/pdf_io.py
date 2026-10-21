"""PyMuPDF：文字层提取、页面渲染、书签读写。"""

import numpy as np
import pymupdf

from .models import Heading, TextLine

_BOLD_HINTS = ("bold", "black", "heavy", "semibold", "demi", "hei", "-bd", ",bd")


def _is_bold(span) -> bool:
    if span["flags"] & pymupdf.TEXT_FONT_BOLD:
        return True
    name = span["font"].lower()
    return any(h in name for h in _BOLD_HINTS) or "黑" in span["font"]


def _is_cjk(ch: str) -> bool:
    c = ord(ch)
    return 0x3000 <= c <= 0x303F or 0x3400 <= c <= 0x4DBF or 0x4E00 <= c <= 0x9FFF or 0xFF00 <= c <= 0xFFEF


def _join(a: str, b: str) -> str:
    if a and b and not _is_cjk(a[-1]) and not _is_cjk(b[0]) and a[-1] != " " and b[0] != " ":
        return a + " " + b
    return a + b


class _Raw:
    __slots__ = ("text", "bbox", "size_sum", "glyphs", "bold_glyphs")

    def __init__(self, text, bbox, size_sum, glyphs, bold_glyphs):
        self.text, self.bbox = text, list(bbox)
        self.size_sum, self.glyphs, self.bold_glyphs = size_sum, glyphs, bold_glyphs

    @property
    def size(self):
        return self.size_sum / self.glyphs if self.glyphs else 0.0

    @property
    def bold(self):
        return self.glyphs > 0 and self.bold_glyphs * 10 >= self.glyphs * 6

    def merge(self, o: "_Raw"):
        self.text = _join(self.text, o.text)
        self.bbox = [min(self.bbox[0], o.bbox[0]), min(self.bbox[1], o.bbox[1]),
                     max(self.bbox[2], o.bbox[2]), max(self.bbox[3], o.bbox[3])]
        self.size_sum += o.size_sum
        self.glyphs += o.glyphs
        self.bold_glyphs += o.bold_glyphs


def extract_lines(page: pymupdf.Page) -> list[TextLine]:
    """提取带字号/粗体信息的文本行。"""
    pw, ph = page.rect.width, page.rect.height
    out: list[TextLine] = []
    # 不保留连字，“Eﬀect”“Deﬁnitions”展开为普通字母
    data = page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT & ~pymupdf.TEXT_PRESERVE_LIGATURES)
    for block in data["blocks"]:
        if block.get("type") != 0:
            continue
        raws: list[_Raw] = []
        for line in block["lines"]:
            text, size_sum, glyphs, bold_glyphs = "", 0.0, 0, 0
            for s in line["spans"]:
                text += s["text"]
                n = sum(1 for ch in s["text"] if not ch.isspace())
                size_sum += s["size"] * n
                glyphs += n
                if _is_bold(s):
                    bold_glyphs += n
            if glyphs == 0:
                continue
            raw = _Raw(text, line["bbox"], size_sum, glyphs, bold_glyphs)
            # 同一基线被拆开的行合并为视觉行
            if raws:
                last = raws[-1]
                h = max(1.0, min(last.bbox[3] - last.bbox[1], raw.bbox[3] - raw.bbox[1]))
                if abs(last.bbox[1] - raw.bbox[1]) < h * 0.5 and abs(last.bbox[3] - raw.bbox[3]) < h * 0.5:
                    last.merge(raw)
                    continue
            raws.append(raw)
        if not raws:
            continue

        # 不超过 3 行且样式一致的块（常见于折行标题）整体作为一个候选
        if 1 < len(raws) <= 3 and all(
            abs(r.size - raws[0].size) < 0.6 and r.bold == raws[0].bold for r in raws[1:]
        ):
            for r in raws[1:]:
                raws[0].merge(r)
            raws = raws[:1]

        for r in raws:
            text = " ".join(r.text.split())
            if not text:
                continue
            out.append(TextLine(text=text, page=page.number, x0=r.bbox[0], y0=r.bbox[1],
                                x1=r.bbox[2], y1=r.bbox[3], page_width=pw, page_height=ph,
                                font_size=r.size, bold=r.bold))
    return out


def render_bgr(page: pymupdf.Page, dpi: float) -> np.ndarray:
    """渲染为 OpenCV/Paddle 使用的 BGR 数组。"""
    pix = page.get_pixmap(dpi=int(dpi), colorspace=pymupdf.csRGB, alpha=False)
    arr = np.frombuffer(pix.samples_mv, dtype=np.uint8).reshape(pix.height, pix.stride)
    rgb = arr[:, : pix.width * 3].reshape(pix.height, pix.width, 3)
    return np.ascontiguousarray(rgb[:, :, ::-1])


def _normalize_origin(doc: pymupdf.Document, pno: int) -> bool:
    """把页面的旋转（/Rotate）和页面框偏移（如扫描件的 MediaBox [0 497 514 1224]）并入内容，
    输出为不旋转、原点在 (0,0) 的页面；显示效果不变。

    阅读器对旋转页、带偏移页面的书签坐标理解不一，归一后所有阅读器跳转一致。
    带注释的页面不处理（注释会错位），返回 False。
    """
    page = doc[pno]
    if page.first_annot or page.first_widget:
        return False
    rot = page.rotation % 360
    mb = page.mediabox
    # PyMuPDF 的 cropbox 是按 MediaBox 上边翻转过的 y，换回 PDF 坐标，并与 MediaBox 取交集
    bx0, bx1 = max(page.cropbox.x0, mb.x0), min(page.cropbox.x1, mb.x1)
    by0, by1 = max(mb.y1 - page.cropbox.y1, mb.y0), min(mb.y1 - page.cropbox.y0, mb.y1)
    w0, h0 = bx1 - bx0, by1 - by0
    if not rot and abs(bx0) < 0.01 and abs(by0) < 0.01 and page.mediabox == page.cropbox:
        return True

    def to_new(x, y):
        # 原 PDF 坐标 -> 显示坐标（左上原点、顺时针旋转后）-> 新 PDF 坐标（左下原点）
        ux, uy = x - bx0, by1 - y
        dx, dy, height = {0: (ux, uy, h0), 90: (h0 - uy, ux, w0),
                          180: (w0 - ux, h0 - uy, h0), 270: (uy, w0 - ux, w0)}[rot]
        return dx, height - dy

    ex, ey = to_new(0, 0)
    a, b = (v - o for v, o in zip(to_new(1, 0), (ex, ey)))
    c, d = (v - o for v, o in zip(to_new(0, 1), (ex, ey)))
    contents = page.get_contents()
    if contents:
        head, tail = doc.get_new_xref(), doc.get_new_xref()
        for x, data in ((head, f"q {a:g} {b:g} {c:g} {d:g} {ex:g} {ey:g} cm\n".encode()), (tail, b"\nQ\n")):
            doc.update_object(x, "<<>>")
            doc.update_stream(x, data, compress=False)
        refs = " ".join(f"{x} 0 R" for x in (head, *contents, tail))
        doc.xref_set_key(page.xref, "Contents", f"[{refs}]")
    w, h = (h0, w0) if rot in (90, 270) else (w0, h0)
    box = f"[0 0 {w:g} {h:g}]"
    for key in ("MediaBox", "CropBox"):
        doc.xref_set_key(page.xref, key, box)
    for key in ("BleedBox", "TrimBox", "ArtBox"):
        doc.xref_set_key(page.xref, key, "null")
    doc.xref_set_key(page.xref, "Rotate", "0")
    return True


def write_toc(doc: pymupdf.Document, headings: list[Heading]) -> None:
    """写入书签（覆盖原有书签），保证层级不跳级。"""
    plain = [_normalize_origin(doc, pno) for pno in range(len(doc))]
    toc, prev = [], 0
    for h in headings:
        lv = max(1, min(h.level, prev + 1))
        prev = lv
        page = doc[h.page]
        # 略上留白，让标题贴近视口顶部；过大（旧逻辑还经 _anchor_tops 上提）会露出上节正文
        y = max(0.0, h.y - 4)
        if plain[h.page]:
            to = pymupdf.Point(0, y)  # set_toc 按“页高 - y”换算成 PDF 坐标
        else:
            # 未归一的页面（带注释）：按页面变换矩阵补偿页面框偏移
            pdf = (pymupdf.Point(0, y) * page.derotation_matrix) * ~page.transformation_matrix
            to = pymupdf.Point(pdf.x, page.rect.height - pdf.y)
        dest = {"kind": pymupdf.LINK_GOTO, "page": h.page, "to": to, "zoom": 0}
        toc.append([lv, h.title, h.page + 1, dest])
    doc.set_toc(toc, collapse=1)


def save_toc_file(path: str, headings: list[Heading]) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("# level\tpage\ty\ttitle\n")
        for h in headings:
            f.write(f"{h.level}\t{h.page + 1}\t{int(h.y)}\t{h.title}\n")


def load_toc_file(path: str) -> list[Heading]:
    out = []
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if not line or line.startswith("#"):
                continue
            cols = line.split("\t")
            if len(cols) < 3:
                continue
            if len(cols) >= 4:
                out.append(Heading(title=cols[3], page=int(cols[1]) - 1, y=float(cols[2]), level=int(cols[0])))
            else:
                out.append(Heading(title=cols[2], page=int(cols[1]) - 1, level=int(cols[0])))
    return out
