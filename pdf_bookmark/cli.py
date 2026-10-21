"""无书签 PDF 的标题识别与书签生成。

  文字版页面：PyMuPDF 解析文字层（字号、粗体）
  扫描页面：ONNX Runtime 运行 PP-DocLayout 版面分析 + PP-OCRv5 识别，有 DirectML 显卡用 GPU，否则用 CPU；
           多线程分块处理，按页码顺序合并
  规则引擎：编号样式 + 字号 + 版面类别 + 过滤规则 → 标题及层级
  输出：PyMuPDF set_toc 写入书签
"""

import argparse
import os
import sys
import time
import warnings
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import pymupdf

from . import pdf_io, resources
from .i18n import set_lang, t
from .rule_engine import RuleConfig, RuleEngine


def _apply_lang(argv: list[str] | None = None) -> None:
    """先解析 --lang（不消费其它参数），供帮助文案与运行时消息使用。"""
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--lang", choices=["zh", "en"], default=None)
    known, _ = pre.parse_known_args(argv)
    set_lang(known.lang)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pdf_bookmark", description=t("prog_desc"))
    p.add_argument("inputs", nargs="*", help=t("help_inputs"))
    p.add_argument("--out", help=t("help_out"))
    p.add_argument("--mode", choices=["auto", "text", "ocr", "hybrid"], default="auto", help=t("help_mode"))
    p.add_argument("--dpi", type=float, default=150, help=t("help_dpi"))
    p.add_argument("--min_text_chars", type=int, default=30, help=t("help_min_text"))
    p.add_argument("--pages", help=t("help_pages"))
    p.add_argument("--rules", help=t("help_rules"))
    p.add_argument("--dump_toc", help=t("help_dump_toc"))
    p.add_argument("--toc_in", help=t("help_toc_in"))
    p.add_argument("--max_level", type=int, default=None, help=t("help_max_level"))
    p.add_argument("--force", action="store_true", help=t("help_force"))
    p.add_argument("--dry_run", action="store_true", help=t("help_dry_run"))
    p.add_argument("--verbose", action="store_true", help=t("help_verbose"))
    p.add_argument("--lang", choices=["zh", "en"], default=None, help=t("help_lang"))
    g = p.add_argument_group("OCR")
    g.add_argument("--device", default="auto", choices=["auto", "gpu", "cpu"], help=t("help_device"))
    g.add_argument("--workers", type=int, default=0, help=t("help_workers"))
    g.add_argument("--chunk", type=int, default=8, help=t("help_chunk"))
    g.add_argument("--full_ocr", action="store_true", help=t("help_full_ocr"))
    return p


def parse_pages(spec: str | None, count: int) -> list[int]:
    if not spec:
        return list(range(count))
    pages = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            pages.update(range(int(a) - 1, min(int(b), count)))
        elif part:
            pages.add(int(part) - 1)
    return sorted(p for p in pages if 0 <= p < count)


MAX_WORKERS = 20


def _available_memory_mb() -> float:
    try:
        if os.name == "nt":
            import ctypes

            class MemStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong)] + [
                    (n, ctypes.c_ulonglong) for n in ("total", "avail", "tpage", "apage", "tvirt", "avirt", "ext")]

            st = MemStatus()
            st.dwLength = ctypes.sizeof(MemStatus)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return st.avail / 2**20
        return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 2**20
    except Exception:  # noqa: BLE001
        return 4096.0


def plan_workers(device: str, pages: int, requested: int = 0, chunk: int = 8) -> tuple[int, int, int]:
    """按设备、CPU 核数、可用内存和待 OCR 页数决定 (线程数, 每线程推理核数, 每块页数)。

    - 给用户留出 1/4 逻辑核（至少 2 个），避免电脑卡顿；
    - CPU：每线程 4 个推理核（核少时 2 / 1 个）。实测总推理核数超过物理核后不再变快，多开只会更卡；
    - GPU：DirectML 推理串行，3~4 个线程即可让 GPU 满载，再多只占 CPU；
    - 内存：每线程约 550MB，外加在途的渲染图片，最多使用可用内存的一半；
    - 文档：每个线程至少分到约 4 页，小文档少开线程；上限 20。
    """
    logical = os.cpu_count() or 4
    budget = max(1, logical - max(2, logical // 4))
    if device == "dml":
        intra = 2
        workers = min(4, max(1, budget // intra))
    else:
        intra = 4 if budget >= 16 else 2 if budget >= 4 else 1
        workers = max(1, budget // intra)
    workers = min(workers, max(1, -(-pages // 4)))
    chunk = max(1, min(chunk, -(-pages // (workers * 3))))
    per_worker_mb = 550 + 2 * chunk * 8
    workers = min(workers, max(1, int(_available_memory_mb() * 0.5 // per_worker_mb)))
    if requested > 0:
        workers = requested
    workers = max(1, min(MAX_WORKERS, workers))
    if device != "dml":
        intra = max(1, budget // workers)
    return workers, intra, chunk


def _tune_process() -> None:
    """OCR 期间优先级调为“低于正常”，前台程序优先，避免卡顿；同时关闭 Windows 的节能限流（EcoQoS）。

    大小核 CPU 上，被限流的后台进程会被调度到能效核，推理慢 3~4 倍；
    关闭限流后低优先级不影响速度（实测 64 页：限流 115~135s，不限流 29s）。
    """
    try:
        if os.name == "nt":
            import ctypes
            k32 = ctypes.windll.kernel32
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            proc = ctypes.c_void_p(k32.GetCurrentProcess())

            class Throttle(ctypes.Structure):
                _fields_ = [("Version", ctypes.c_ulong), ("ControlMask", ctypes.c_ulong),
                            ("StateMask", ctypes.c_ulong)]

            # ProcessPowerThrottling(4)：控制执行速度，状态 0 = 不限流
            st = Throttle(1, 1, 0)
            k32.SetProcessInformation(proc, 4, ctypes.byref(st), ctypes.sizeof(st))
            k32.SetPriorityClass(proc, 0x4000)  # BELOW_NORMAL_PRIORITY_CLASS
        else:
            os.nice(5)
    except Exception:  # noqa: BLE001
        pass


class OcrProvider:
    """首次遇到扫描页时才加载模型，纯文字版 PDF 不需要加载。"""

    def __init__(self, args):
        self.args = args
        self.engine = None
        self.error = None
        self.workers = 1
        self.chunk = args.chunk

    def get(self, pages_left: int = 1):
        """pages_left：剩余待处理页数（估计需要 OCR 的页数），用于决定线程数。"""
        if self.engine is None and self.error is None:
            print(t("ocr_loading"), flush=True)
            t0 = time.time()
            try:
                from .ocr_engine import OcrEngine, detect_device
                a = self.args
                device = detect_device(a.device)
                self.workers, intra, self.chunk = plan_workers(device, pages_left, a.workers, a.chunk)
                _tune_process()
                self.engine = OcrEngine(device, intra)
                dev = t("dev_gpu") if self.engine.device == "dml" else t("dev_cpu")
                print(t("ocr_ready", dev=dev, workers=self.workers, intra=intra,
                        chunk=self.chunk, sec=time.time() - t0), flush=True)
            except Exception as e:  # noqa: BLE001
                self.error = str(e)
                print(t("ocr_unavailable", err=e), file=sys.stderr)
        return self.engine


def _ocr_chunk(engine, chunk, scale, run_ocr, full_front):
    """线程任务：处理一块页面，返回 [(页码, 行, 版面区域)]。"""
    out = []
    for pno, img, w, h in chunk:
        lines, regions = engine.analyze(img, pno, scale, w, h, run_ocr, pno < full_front)
        out.append((pno, lines, regions))
    return out


def detect(doc: pymupdf.Document, args, rules: RuleEngine, ocr: OcrProvider) -> list:
    from .ocr_engine import assign_region

    scale = args.dpi / 72.0
    pages = parse_pages(args.pages, doc.page_count)
    # 书前部分（前言、目录）识别全部文字，用于目录页判断与标题纠错
    full_front = doc.page_count if args.full_ocr else min(40, max(10, doc.page_count * 8 // 100))
    by_page: dict[int, list] = {}
    text_pages = ocr_pages = skipped = done = 0
    t0 = time.time()
    pool = None
    pending = set()
    chunks = {True: [], False: []}  # run_ocr -> 待提交的页面

    shown = [-1]

    def progress():
        if not args.verbose and shown[0] != done:
            shown[0] = done
            print(t("progress", done=done, total=len(pages), sec=time.time() - t0), end="", flush=True)

    def collect(futs):
        nonlocal done, ocr_pages
        for f in futs:
            run_ocr = f.run_ocr
            for pno, lines, regions in f.result():
                done += 1
                if run_ocr:
                    by_page[pno] = lines
                    ocr_pages += 1
                else:
                    for l in by_page[pno]:
                        idx = assign_region(l, regions)
                        if idx is not None:
                            l.layout_type = regions[idx].type
                if args.verbose:
                    kind = t("kind_ocr") if run_ocr else t("kind_text_layout")
                    print(t("page_lines", page=pno + 1, n=len(by_page[pno]), kind=kind))
        progress()

    def submit(run_ocr, force=False):
        nonlocal pool
        chunk = chunks[run_ocr]
        if not chunk or (len(chunk) < ocr.chunk and not force):
            return
        if pool is None:
            pool = ThreadPoolExecutor(max_workers=ocr.workers)
        # 限制在途任务数，避免渲染好的图片堆积占用内存
        while len(pending) >= ocr.workers * 2:
            fin, _ = wait(pending, return_when=FIRST_COMPLETED)
            pending.difference_update(fin)
            collect(fin)
        f = pool.submit(_ocr_chunk, ocr.engine, list(chunk), scale, run_ocr, full_front)
        f.run_ocr = run_ocr
        pending.add(f)
        chunk.clear()

    # PyMuPDF 不支持多线程，解析与渲染在主线程顺序进行，OCR 交给线程池
    for i, pno in enumerate(pages):
        page = doc[pno]
        lines = pdf_io.extract_lines(page) if args.mode != "ocr" else []
        scanned = sum(len(l.text) for l in lines) < args.min_text_chars
        need_ocr = args.mode == "ocr" or (args.mode != "text" and scanned)
        need_layout = args.mode == "hybrid" and not need_ocr
        by_page[pno] = lines
        if (need_ocr or need_layout) and ocr.get(len(pages) - i):
            img = pdf_io.render_bgr(page, args.dpi)
            chunks[need_ocr].append((pno, img, page.rect.width, page.rect.height))
            submit(need_ocr)
            continue
        if need_ocr and not lines:
            skipped += 1
        text_pages += 1
        done += 1
        if args.verbose:
            print(t("page_lines", page=pno + 1, n=len(lines), kind=t("kind_text")))
        elif done % 5 == 0:
            progress()

    submit(True, force=True)
    submit(False, force=True)
    if pending:
        collect(pending)
    if pool:
        pool.shutdown()
    progress()

    print(t("summary", text=text_pages, ocr=ocr_pages)
          + (t("summary_skipped", n=skipped) if skipped else ""))
    # 按页码顺序合并各线程结果
    all_lines = [l for pno in sorted(by_page) for l in by_page[pno]]
    return rules.detect(all_lines, doc.page_count)


def process(src: str, out: str | None, args, rules: RuleEngine, ocr: OcrProvider) -> int:
    out = out or os.path.splitext(src)[0] + "_bookmarked.pdf"
    try:
        doc = pymupdf.open(src)
    except Exception as e:  # noqa: BLE001
        print(t("open_fail", err=e), file=sys.stderr)
        return 1
    if not doc.is_pdf:
        print(t("not_pdf"), file=sys.stderr)
        return 1
    print(t("input_info", src=src, pages=doc.page_count))
    if doc.get_toc() and not args.force:
        print(t("has_toc"), file=sys.stderr)
        return 2

    t0 = time.time()
    headings = pdf_io.load_toc_file(args.toc_in) if args.toc_in else detect(doc, args, rules, ocr)

    print(t("found_headings", n=len(headings)))
    for h in headings:
        extra = f"  [{h.style} score={h.score:.2f}]" if args.verbose and h.style else ""
        print(f"  {'  ' * (h.level - 1)}{h.title}  ... p{h.page + 1}{extra}")

    if args.dump_toc:
        pdf_io.save_toc_file(args.dump_toc, headings)
        print(t("toc_exported", path=args.dump_toc))
    if args.dry_run:
        return 0
    if not headings:
        print(t("no_headings"), file=sys.stderr)
        return 3
    if os.path.abspath(out) == os.path.abspath(src):
        print(t("out_same"), file=sys.stderr)
        return 1

    pdf_io.write_toc(doc, headings)
    doc.save(out, garbage=3, deflate=True)
    print(t("written", out=out, sec=time.time() - t0))
    return 0


def main(argv=None) -> int:
    _apply_lang(argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.inputs:
        parser.print_help()
        return 1
    if args.out and len(args.inputs) > 1:
        print(t("multi_out"), file=sys.stderr)
        return 1

    rules_path = args.rules or resources.default_rules()
    try:
        cfg = RuleConfig.load(rules_path) if rules_path else RuleConfig()
    except (OSError, ValueError) as e:
        print(t("rules_fail", err=e), file=sys.stderr)
        return 1
    if args.max_level is not None:
        cfg.max_level = max(1, args.max_level)
    rules, ocr = RuleEngine(cfg), OcrProvider(args)

    code = 0
    for src in args.inputs:
        code = max(code, process(src, args.out, args, rules, ocr))
    return code


def _owns_console() -> bool:
    """双击 / 拖放启动时控制台只属于本进程，结束前需要暂停，否则窗口一闪而过。"""
    if os.name != "nt":
        return False
    try:
        import ctypes
        return ctypes.windll.kernel32.GetConsoleProcessList((ctypes.c_uint * 4)(), 4) <= 1 + (
            1 if resources.FROZEN else 0)
    except Exception:  # noqa: BLE001
        return False


def _input(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""


def entry() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    warnings.filterwarnings("ignore")
    pause = _owns_console()
    argv = sys.argv[1:]
    _apply_lang(argv)
    try:
        if "-h" not in argv and "--help" not in argv and not build_parser().parse_args(argv).inputs:
            print(t("banner"))
            print(t("banner_hint"))
            while True:
                path = _input(t("prompt_path")).strip().strip('"')
                if not path:
                    return 0
                main(argv + [path])
                print()
        return main(argv)
    except KeyboardInterrupt:
        return 130
    finally:
        if pause:
            _input(t("press_enter"))
