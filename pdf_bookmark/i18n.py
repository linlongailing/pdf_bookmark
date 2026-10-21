"""界面文案（中/英）。CLI 与后续图形界面共用同一套 key。

设置优先级：`--lang` > 环境变量 `PDF_BOOKMARK_LANG` > 系统区域设置 > 中文。
"""

from __future__ import annotations

import locale
import os
from typing import Any

LANGS = ("zh", "en")

_MESSAGES: dict[str, dict[str, str]] = {
    "zh": {
        "prog_desc": "为无书签 PDF 识别标题并生成书签",
        "help_inputs": "输入 PDF，可多个",
        "help_out": "输出 PDF（仅单个输入时可用，默认 <输入名>_bookmarked.pdf）",
        "help_mode": "auto: 文字版页面解析文字层，扫描页面 OCR（默认）；text: 只用文字层；"
                     "ocr: 全部 OCR；hybrid: 文字层 + 版面分析辅助",
        "help_dpi": "OCR 渲染 DPI（默认 150）",
        "help_min_text": "文字层字符数低于该值的页面视为扫描页",
        "help_pages": "只处理部分页面，如 1-20,25（从 1 开始）",
        "help_rules": "规则配置文件（默认使用 exe 旁的 rules.ini 或内置配置）",
        "help_dump_toc": "导出识别结果（level<TAB>page<TAB>y<TAB>title）",
        "help_toc_in": "跳过识别，直接按目录文件写入书签",
        "help_max_level": "书签最大层级（默认取配置文件，3 表示保留到小节一级）",
        "help_force": "输入已有书签时仍然覆盖",
        "help_dry_run": "只输出识别结果，不写 PDF",
        "help_verbose": "输出每页处理信息与标题得分",
        "help_device": "auto：有支持 DirectML 的显卡用 GPU（默认）；cpu：只用 CPU",
        "help_workers": "OCR 线程数（默认按设备、CPU 核数、内存和页数自动选择，最多 20）",
        "help_chunk": "每个线程任务最多处理的页数（默认 8，小文档自动减小）",
        "help_full_ocr": "识别页面全部文字（默认只识别标题、页眉、目录等区域，正文只检测不识别）",
        "help_lang": "界面语言：zh 中文 / en English（默认跟随系统）",
        "banner": "PDF 书签生成工具：文字版直接解析，扫描版自动 OCR（有 GPU 用 GPU）",
        "banner_hint": "也可以把 PDF 文件直接拖到本程序图标上运行；命令行用法见 --help\n",
        "prompt_path": "请输入或拖入 PDF 文件路径（直接回车退出）: ",
        "press_enter": "\n按回车键退出 ...",
        "ocr_loading": "  检测到扫描页，正在加载 OCR 模型 ...",
        "ocr_ready": "  OCR 就绪（{dev}，{workers} 线程 × {intra} 核，每块 {chunk} 页，{sec:.1f}s）",
        "dev_gpu": "GPU（DirectML）",
        "dev_cpu": "CPU",
        "ocr_unavailable": "[OCR] 不可用: {err}",
        "progress": "\r  已处理 {done}/{total} 页，用时 {sec:.1f}s   ",
        "page_lines": "  第 {page} 页: {n} 行（{kind}）",
        "kind_ocr": "OCR",
        "kind_text_layout": "文字层+版面",
        "kind_text": "文字层",
        "summary": "\n  文字解析 {text} 页，OCR {ocr} 页",
        "summary_skipped": "，无法识别 {n} 页（OCR 不可用）",
        "open_fail": "打开 PDF 失败: {err}",
        "not_pdf": "输入文件不是 PDF",
        "input_info": "\n输入: {src}，共 {pages} 页",
        "has_toc": "该 PDF 已包含书签，跳过（如需覆盖请加 --force）",
        "found_headings": "识别到 {n} 个标题:",
        "toc_exported": "目录已导出: {path}",
        "no_headings": "未识别到标题，未生成输出文件",
        "out_same": "输出路径不能与输入相同",
        "written": "已写入书签: {out}（用时 {sec:.1f}s）",
        "multi_out": "多个输入时不能指定 --out",
        "rules_fail": "读取规则文件失败: {err}",
        "rule_unknown": "[规则] 第 {line} 行未知配置项: {key}",
        "rule_bad_regex": "[规则] 正则无效，已忽略: {regex} ({err})",
    },
    "en": {
        "prog_desc": "Detect headings in bookmarkless PDFs and write a multi-level TOC",
        "help_inputs": "Input PDF(s)",
        "help_out": "Output PDF (single input only; default <name>_bookmarked.pdf)",
        "help_mode": "auto: text layer on born-digital pages, OCR on scans (default); "
                     "text: text layer only; ocr: OCR all pages; hybrid: text + layout",
        "help_dpi": "Render DPI for OCR (default 150)",
        "help_min_text": "Pages with fewer text-layer characters are treated as scans",
        "help_pages": "Process only these pages, e.g. 1-20,25 (1-based)",
        "help_rules": "Rules file (default: rules.ini next to the exe, or the built-in one)",
        "help_dump_toc": "Export headings (level<TAB>page<TAB>y<TAB>title)",
        "help_toc_in": "Skip detection; write bookmarks from a TOC file",
        "help_max_level": "Maximum bookmark level (default: value from the rules file; 3 keeps down to subsections)",
        "help_force": "Overwrite bookmarks if the PDF already has a TOC",
        "help_dry_run": "Print headings only; do not write a PDF",
        "help_verbose": "Per-page details and heading scores",
        "help_device": "auto: GPU via DirectML when available (default); cpu: CPU only",
        "help_workers": "OCR threads (default: auto from device/CPU/RAM/pages, max 20)",
        "help_chunk": "Max pages per OCR task (default 8; smaller for short documents)",
        "help_full_ocr": "OCR all text on the page (default: titles/headers/TOC regions only)",
        "help_lang": "UI language: zh Chinese / en English (default: follow system locale)",
        "banner": "PDF bookmark tool: text layer for digital pages, OCR for scans (GPU when available)",
        "banner_hint": "You can also drop PDF files onto the program icon. See --help for CLI usage.\n",
        "prompt_path": "Enter or drop a PDF path (Enter to quit): ",
        "press_enter": "\nPress Enter to exit ...",
        "ocr_loading": "  Scan page detected; loading OCR models ...",
        "ocr_ready": "  OCR ready ({dev}, {workers} threads × {intra} cores, chunk {chunk} pages, {sec:.1f}s)",
        "dev_gpu": "GPU (DirectML)",
        "dev_cpu": "CPU",
        "ocr_unavailable": "[OCR] unavailable: {err}",
        "progress": "\r  Processed {done}/{total} pages in {sec:.1f}s   ",
        "page_lines": "  Page {page}: {n} lines ({kind})",
        "kind_ocr": "OCR",
        "kind_text_layout": "text+layout",
        "kind_text": "text layer",
        "summary": "\n  Text layer {text} pages, OCR {ocr} pages",
        "summary_skipped": ", skipped {n} (OCR unavailable)",
        "open_fail": "Failed to open PDF: {err}",
        "not_pdf": "Input is not a PDF",
        "input_info": "\nInput: {src}, {pages} pages",
        "has_toc": "PDF already has bookmarks; skipped (use --force to overwrite)",
        "found_headings": "Found {n} headings:",
        "toc_exported": "TOC exported: {path}",
        "no_headings": "No headings found; no output written",
        "out_same": "Output path must differ from the input",
        "written": "Bookmarks written: {out} ({sec:.1f}s)",
        "multi_out": "Cannot use --out with multiple inputs",
        "rules_fail": "Failed to load rules: {err}",
        "rule_unknown": "[rules] unknown key on line {line}: {key}",
        "rule_bad_regex": "[rules] invalid regex ignored: {regex} ({err})",
    },
}

_lang = "zh"


def detect_lang() -> str:
    env = (os.environ.get("PDF_BOOKMARK_LANG") or "").strip().lower()
    if env in LANGS:
        return env
    try:
        loc = (locale.getlocale()[0] or locale.getdefaultlocale()[0] or "").lower()
    except Exception:  # noqa: BLE001
        loc = ""
    if loc.startswith("zh"):
        return "zh"
    if loc:
        return "en"
    return "zh"


def set_lang(lang: str | None) -> str:
    global _lang
    if lang in LANGS:
        _lang = lang
    else:
        _lang = detect_lang()
    return _lang


def get_lang() -> str:
    return _lang


def t(key: str, **kwargs: Any) -> str:
    """按当前语言取文案；缺 key 时回退到中文，再回退到 key 本身。"""
    msg = _MESSAGES.get(_lang, {}).get(key) or _MESSAGES["zh"].get(key) or key
    return msg.format(**kwargs) if kwargs else msg
