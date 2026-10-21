"""ONNX Runtime 推理：PP-DocLayout 版面分析 + PP-OCRv5 文字检测 / 识别。

有 DirectML 可用的显卡（NVIDIA / AMD / Intel，含核显）时用 GPU，否则用 CPU。
默认只识别标题相关区域（标题、页眉、目录、版面框外的行），正文行只保留检测框用于估计正文字号。
"""

import math
import re
import threading

import cv2
import numpy as np

from . import resources
from .models import LayoutRegion, TextLine
from .rule_engine import TITLE_TYPES

LAYOUT_LABELS = [
    "paragraph_title", "image", "text", "number", "abstract", "content", "figure_title", "formula",
    "table", "table_title", "reference", "doc_title", "footnote", "header", "algorithm", "footer",
    "seal", "chart_title", "chart", "formula_number", "header_image", "footer_image", "aside_text",
]
_SKIP_TYPES = {"figure", "image", "table", "chart", "equation", "formula", "seal", "header_image", "footer_image"}
# 只识别这些区域里的文字（以及不在任何版面框内的行）
_REC_TYPES = TITLE_TYPES | {"header", "content"}


def detect_device(prefer: str = "auto") -> str:
    """返回 "dml"（DirectML GPU）或 "cpu"。"""
    if prefer == "cpu":
        return "cpu"
    try:
        import onnxruntime as ort
        if "DmlExecutionProvider" in ort.get_available_providers():
            return "dml"
    except Exception:  # noqa: BLE001
        pass
    return "cpu"


def assign_region(line, regions, min_cover: float = 0.6):
    """返回覆盖该行面积比例最大（且超过 min_cover）的区域下标。"""
    area = max(1e-3, (line.x1 - line.x0) * (line.y1 - line.y0))
    best, best_idx = min_cover, None
    for i, r in enumerate(regions):
        ix = max(0.0, min(line.x1, r.x1) - max(line.x0, r.x0))
        iy = max(0.0, min(line.y1, r.y1) - max(line.y0, r.y0))
        cover = ix * iy / area
        if cover > best:
            best, best_idx = cover, i
    return best_idx


def _merge_rows(items: list[TextLine]) -> list[TextLine]:
    """同一行被检测模型切成多段时按行合并。"""
    items = sorted(items, key=lambda t: t.y0 + t.y1)
    rows: list[list[TextLine]] = []
    for it in items:
        if rows:
            ref = rows[-1][0]
            if abs((it.y0 + it.y1) - (ref.y0 + ref.y1)) / 2 < 0.5 * min(it.y1 - it.y0, ref.y1 - ref.y0):
                rows[-1].append(it)
                continue
        rows.append([it])
    out = []
    for row in rows:
        row.sort(key=lambda t: t.x0)
        m = row[0]
        for t in row[1:]:
            # 远离行内文字的单个字符多是装饰图标（如章标题旁的“O”）
            if len(t.text) == 1 and m.text and t.x0 - m.x1 > 2 * (t.y1 - t.y0):
                continue
            m.text = f"{m.text} {t.text}".strip() if t.text else m.text
            m.x0, m.y0, m.x1, m.y1 = min(m.x0, t.x0), min(m.y0, t.y0), max(m.x1, t.x1), max(m.y1, t.y1)
            m.font_size = max(m.font_size, t.font_size)
            m.ocr_score = min(m.ocr_score, t.ocr_score)
        out.append(m)
    return out


def _join_text(a: str, b: str) -> str:
    return a + (" " if a and b and a[-1].isascii() and b[0].isascii() else "") + b


_CJK = re.compile(r"[\u4e00-\u9fff]")
_NUM_START = re.compile(r"^(\d|第|附\s*录|[（(]|[A-Z]\.\d|[一二三四五六七八九十]+[、.])")


def _merge_title_rows(rows: list[TextLine], page_lines: list[TextLine]) -> list[TextLine]:
    """折成两行的标题合并为一行：版面模型漏检的大号标题（“第3章 内容缓存工作 / 原理及实现”），
    或两行分属两个标题框（“3.3 WebCache的实现基础 / 基于HTTP协议的Web缓存技术”）。"""
    hs = sorted(l.y1 - l.y0 for l in page_lines)
    med = hs[len(hs) // 2] if hs else 0.0
    out: list[TextLine] = []
    for r in sorted(rows, key=lambda t: t.y0):
        # 两行标题之间可能夹着底纹噪点检测出的空文字小框，跳过它找上一行
        p = next((o for o in reversed(out) if o.text), None)
        if p is not None and med > 0 and r.text:
            ph, rh = p.y1 - p.y0, r.y1 - r.y0
            big = not p.layout_type and not r.layout_type and min(ph, rh) >= 1.6 * med
            titled = p.layout_type in TITLE_TYPES and r.layout_type in TITLE_TYPES and not _NUM_START.match(r.text)
            if (p.text and (big or titled) and max(ph, rh) <= 1.35 * min(ph, rh)
                    and 0 <= r.y0 - p.y1 < 0.6 * max(ph, rh) and min(p.x1, r.x1) > max(p.x0, r.x0)):
                p.text = _join_text(p.text, r.text)
                p.x0, p.x1, p.y1 = min(p.x0, r.x0), max(p.x1, r.x1), r.y1
                p.ocr_score = min(p.ocr_score, r.ocr_score)
                continue
        out.append(r)
    return out


def _group_by_region(lines: list[TextLine], regions: list[LayoutRegion]) -> list[TextLine]:
    """给 OCR 行打上所在版面区域的类别；标题区域内的多行合并为一条。"""
    buckets: dict[int, list[TextLine]] = {}
    free: list[TextLine] = []
    for l in lines:
        idx = assign_region(l, regions)
        if idx is None:
            free.append(l)
        else:
            l.layout_type = regions[idx].type
            buckets.setdefault(idx, []).append(l)

    # 版面框常常没框住行首编号（如 “2.1”），同一行且紧挨着的区域外文本并入该区域
    rest = []
    for l in free:
        h = l.y1 - l.y0
        target = None
        for idx, items in buckets.items():
            for t in items:
                same_row = abs((l.y0 + l.y1) - (t.y0 + t.y1)) / 2 < 0.5 * min(h, t.y1 - t.y0)
                gap = max(t.x0 - l.x1, l.x0 - t.x1)
                if same_row and gap < 1.5 * max(h, t.y1 - t.y0):
                    target = idx
                    break
            if target is not None:
                break
        if target is None:
            rest.append(l)
        else:
            l.layout_type = regions[target].type
            buckets[target].append(l)

    out = _merge_rows(rest)
    for idx, items in buckets.items():
        rtype = regions[idx].type
        if rtype in _SKIP_TYPES:
            continue
        rows = _merge_rows(items)
        if rtype in TITLE_TYPES and len(rows) <= 3:
            m = rows[0]
            for t in rows[1:]:
                m.text = _join_text(m.text, t.text)
                m.x0, m.x1, m.y1 = min(m.x0, t.x0), max(m.x1, t.x1), max(m.y1, t.y1)
                m.font_size = max(m.font_size, t.font_size)
                m.ocr_score = min(m.ocr_score, t.ocr_score)
            out.append(m)
        else:
            out.extend(rows)
    return _merge_title_rows(out, lines)


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def is_blank(img: np.ndarray, ink_ratio: float = 0.002) -> bool:
    """空白页：深色像素占比极低。"""
    small = cv2.cvtColor(img[::4, ::4], cv2.COLOR_BGR2GRAY)
    return float((small < 160).mean()) < ink_ratio


class OcrEngine:
    """analyze() 输入 BGR 图像，输出 (OCR 文本行, 版面区域)，坐标均换算为 pt。线程安全。"""

    def __init__(self, device: str = "auto", threads: int = 0, det_limit: int = 1600):
        import onnxruntime as ort

        ort.set_default_logger_severity(3)
        self._ort = ort
        self.device = detect_device(device)
        self.det_limit = det_limit
        self.threads = threads
        # CPU 会话每个线程一套，避免线程间争用；DirectML 会话全进程共用一套并加锁：
        # DirectML 不支持并发 Run，多个 DirectML 会话并发运行时会卡死
        self._local = threading.local()
        self._gpu: dict = {}
        self._lock = threading.Lock()
        self._sessions()  # 主线程先建一套，模型缺失或 DirectML 不可用时尽早发现
        with open(resources.model_file(f"{resources.REC_MODEL}.dict.txt"), encoding="utf-8") as f:
            self.chars = ["blank"] + [ln.rstrip("\r\n") for ln in f] + [" "]

    def _session(self, name: str, gpu: bool):
        ort = self._ort
        path = resources.model_file(f"{name}.onnx")
        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        if self.device == "dml" and gpu:
            so.enable_mem_pattern = False
            so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            try:
                return ort.InferenceSession(path, so, providers=["DmlExecutionProvider", "CPUExecutionProvider"])
            except Exception:  # noqa: BLE001
                self.device = "cpu"
                so = ort.SessionOptions()
        if self.threads:
            so.intra_op_num_threads = self.threads
            so.inter_op_num_threads = 1
        # 推理线程空闲时让出 CPU，不空转占满核心
        so.add_session_config_entry("session.intra_op.allow_spinning", "0")
        # 内存池按历史峰值只增不减，多线程时每个会话各占一份，内存会被吃光；关闭后用完即还
        so.enable_cpu_mem_arena = False
        return ort.InferenceSession(path, so, providers=["CPUExecutionProvider"])

    def _sessions(self) -> dict:
        s = getattr(self._local, "sessions", None)
        if s is None:
            # 版面模型的 NMS 在 DirectML 上输出错误的框；该模型输入仅 480×480，放在 CPU 上
            s = {"layout": self._session(resources.LAYOUT_MODEL, gpu=False)}
            if self.device == "dml":
                with self._lock:
                    if not self._gpu:
                        self._gpu = {"det": self._session(resources.DET_MODEL, gpu=True),
                                     "rec": self._session(resources.REC_MODEL, gpu=True)}
            if self.device != "dml":  # 包括 DirectML 创建失败回落到 CPU 的情况
                s["det"] = self._session(resources.DET_MODEL, gpu=False)
                s["rec"] = self._session(resources.REC_MODEL, gpu=False)
            self._local.sessions = s
        return s

    def _run(self, name: str, feeds):
        s = self._sessions()
        if name in s:
            return s[name].run(None, feeds)
        with self._lock:
            return self._gpu[name].run(None, feeds)

    # ---------------- 版面分析 ----------------
    def layout(self, img: np.ndarray, threshold: float = 0.5) -> list[tuple[str, list[float], float]]:
        h, w = img.shape[:2]
        x = cv2.resize(img, (480, 480), interpolation=cv2.INTER_CUBIC)[:, :, ::-1].astype(np.float32) / 255.0
        x = (x - (0.485, 0.456, 0.406)) / (0.229, 0.224, 0.225)
        x = x.transpose(2, 0, 1)[None].astype(np.float32)
        scale = np.array([[480.0 / h, 480.0 / w]], dtype=np.float32)
        boxes = self._run("layout", {"image": x, "scale_factor": scale})[0]
        cand = sorted(((LAYOUT_LABELS[int(cls)], [max(0, x0), max(0, y0), min(w, x1), min(h, y1)], float(score))
                       for cls, score, x0, y0, x1, y1 in boxes
                       if score >= threshold and 0 <= int(cls) < len(LAYOUT_LABELS)), key=lambda b: -b[2])
        # 跨类别 NMS：同一块区域同时被判为标题和正文时只保留得分高的
        out = []
        for b in cand:
            if all(_iou(b[1], k[1]) < 0.8 for k in out):
                out.append(b)
        return out

    # ---------------- 文字检测（DB） ----------------
    def detect_text(self, img: np.ndarray) -> list[tuple[float, float, float, float]]:
        h, w = img.shape[:2]
        r = min(1.0, self.det_limit / max(h, w))
        nh, nw = max(32, int(round(h * r / 32)) * 32), max(32, int(round(w * r / 32)) * 32)
        x = cv2.resize(img, (nw, nh)).astype(np.float32) / 255.0
        x = (x - (0.485, 0.456, 0.406)) / (0.229, 0.224, 0.225)
        x = x.transpose(2, 0, 1)[None].astype(np.float32)
        pred = self._run("det", {"x": x})[0][0, 0]
        mask = (pred > 0.3).astype(np.uint8)
        contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        sx, sy = w / nw, h / nh
        boxes = []
        for c in contours[:1000]:
            (cx, cy), (bw, bh), ang = cv2.minAreaRect(c)
            if min(bw, bh) < 3:
                continue
            bx, by, bw2, bh2 = cv2.boundingRect(c)
            roi = pred[by:by + bh2, bx:bx + bw2]
            cm = np.zeros_like(roi, dtype=np.uint8)
            cv2.fillPoly(cm, [c.reshape(-1, 2) - (bx, by)], 1)
            if cm.sum() == 0 or float(roi[cm > 0].mean()) < 0.6:
                continue
            # DB 的 unclip：按 面积×1.5/周长 向外扩
            d = bw * bh * 1.5 / (2 * (bw + bh))
            pts = cv2.boxPoints(((cx, cy), (bw + 2 * d, bh + 2 * d), ang))
            x0, y0 = pts.min(0)
            x1, y1 = pts.max(0)
            if min(x1 - x0, y1 - y0) < 5:
                continue
            boxes.append((max(0.0, x0 * sx), max(0.0, y0 * sy), min(w, x1 * sx), min(h, y1 * sy)))
        return boxes

    # ---------------- 文字识别（CTC） ----------------
    def recognize(self, img: np.ndarray, boxes, batch: int = 16) -> list[tuple[str, float]]:
        crops = []
        for x0, y0, x1, y1 in boxes:
            crop = img[int(y0):int(math.ceil(y1)), int(x0):int(math.ceil(x1))]
            crops.append(crop if crop.size else np.full((48, 48, 3), 255, np.uint8))
        order = sorted(range(len(crops)), key=lambda i: crops[i].shape[1] / max(1, crops[i].shape[0]))
        out: list[tuple[str, float]] = [("", 0.0)] * len(crops)
        for s in range(0, len(order), batch):
            idx = order[s:s + batch]
            ratios = [crops[i].shape[1] / max(1, crops[i].shape[0]) for i in idx]
            # 宽度按 64 对齐，减少 GPU 上的动态形状种类
            width = min(3200, int(math.ceil(max(320, 48 * max(ratios)) / 64)) * 64)
            x = np.zeros((len(idx), 3, 48, width), dtype=np.float32)
            for k, i in enumerate(idx):
                cw = min(width, max(1, int(math.ceil(48 * ratios[k]))))
                im = cv2.resize(crops[i], (cw, 48)).astype(np.float32) / 255.0
                x[k, :, :, :cw] = ((im - 0.5) / 0.5).transpose(2, 0, 1)
            prob = self._run("rec", {"x": x})[0]
            ids, conf = prob.argmax(2), prob.max(2)
            for k, i in enumerate(idx):
                chars, scores, prev = [], [], -1
                for t, c in enumerate(ids[k]):
                    if c != prev and c != 0:
                        chars.append(self.chars[c] if c < len(self.chars) else "")
                        scores.append(conf[k, t])
                    prev = c
                out[i] = ("".join(chars), float(np.mean(scores)) if scores else 0.0)
        return out

    def analyze(self, img: np.ndarray, page: int, scale: float, page_width: float, page_height: float,
                run_ocr: bool, full_text: bool = False) -> tuple[list[TextLine], list[LayoutRegion]]:
        """run_ocr=False 只做版面分析；full_text=False 时只识别标题相关区域。"""
        if is_blank(img):
            return [], []
        layout = self.layout(img)
        regions = [LayoutRegion(label, c[0] / scale, c[1] / scale, c[2] / scale, c[3] / scale, score)
                   for label, c, score in layout]
        if not run_ocr:
            return [], regions
        # 纯图片页：没有任何文字类区域
        if regions and all(r.type in _SKIP_TYPES for r in regions):
            return [], regions

        lines, todo, rest = [], [], []
        dets = self.detect_text(img)
        # 脏背景/网纹上的章标题横幅常被判成图片。矮区域（横幅）里若有明显大于正文的字，去掉图片标签以便识别；
        # 真正的插图通常更高，且内部标注字号接近正文，不会触发。
        hs = sorted((b[3] - b[1]) / scale for b in dets)
        med = hs[len(hs) // 2] if hs else 0.0
        page_h = page_height or (img.shape[0] / scale)

        def title_banner(r):
            if r.type not in ("image", "figure") or not med or (r.y1 - r.y0) > 0.35 * page_h:
                return False
            return any((b[3] - b[1]) / scale >= max(1.5 * med, 16)
                       and assign_region(TextLine("", page, *(v / scale for v in b)), [r]) is not None
                       for b in dets)

        regions = [r for r in regions if not title_banner(r)]
        for bx in dets:
            x0, y0, x1, y1 = (v / scale for v in bx)
            if x1 <= x0 or y1 <= y0:
                continue
            l = TextLine(text="", page=page, x0=x0, y0=y0, x1=x1, y1=y1, page_width=page_width,
                         page_height=page_height, font_size=(y1 - y0) / 1.2, from_ocr=True, ocr_score=1.0)
            idx = assign_region(l, regions)
            rtype = regions[idx].type if idx is not None else ""
            if rtype in _SKIP_TYPES:
                continue
            (todo if full_text or idx is None or rtype in _REC_TYPES else rest).append((l, bx, rtype))
            lines.append(l)
        # 标题行的后半截可能落在正文框里：与待识别行同一行且紧挨着的也识别
        for l, bx, rtype in rest:
            h = l.y1 - l.y0
            if any(abs((l.y0 + l.y1) - (t.y0 + t.y1)) / 2 < 0.5 * min(h, t.y1 - t.y0)
                   and max(t.x0 - l.x1, l.x0 - t.x1) < 2 * max(h, t.y1 - t.y0) for t, _, _ in todo):
                todo.append((l, bx, rtype))
        boxes = [b for _, b, _ in todo]
        results = self.recognize(img, boxes)
        # 识别模型在部分书上会成片漏掉“一”“里”“言”等字，把图缩小到 2/3 后基本不漏，但小字会变差：
        # 只对含汉字的标题候选行（标题版面或以编号开头的短行）在缩小图上再识别一次，字数更多且置信度不低时采用
        refine = [k for k, ((_, _, rtype), (text, _)) in enumerate(zip(todo, results))
                  if len(text) <= 40 and _CJK.search(text)
                  and (rtype in TITLE_TYPES or _NUM_START.match(text.strip()))]
        if refine:
            f = 2 / 3
            small = cv2.resize(img, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
            alt = self.recognize(small, [tuple(v * f for v in boxes[k]) for k in refine])
            for k, (text, score) in zip(refine, alt):
                old, old_score = results[k]
                if len(text.replace(" ", "")) > len(old.replace(" ", "")) and score >= old_score - 0.1:
                    results[k] = (text, score)
        for (l, _, _), (text, score) in zip(todo, results):
            text = text.strip()
            # 标题旁的装饰图标常被识别成 1~2 个低置信度的字（“厂”“国”“■”）
            if text and not (score < 0.5 and len(text) <= 2):
                l.text, l.ocr_score = text, score
        # 识别结果为空的行与未识别的正文行一样只用于估计字号
        return _group_by_region(lines, regions), regions
