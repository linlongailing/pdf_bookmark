"""标题识别规则引擎：过滤 → 打分 → 层级推断 → 去重。"""

import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher

from .models import Heading, TextLine

DEFAULT_SPECIALS = [
    "前言", "序", "序言", "自序", "引言", "绪论", "导论", "摘要", "中文摘要", "英文摘要",
    "目录", "目次", "参考文献", "致谢", "鸣谢", "后记", "结论", "结语", "总结", "索引",
    "附录", "术语表", "abstract", "contents", "preface", "introduction", "conclusion",
    "conclusions", "references", "bibliography", "index", "acknowledgements",
    "acknowledgments", "appendix",
]

TITLE_TYPES = {"title", "doc_title", "paragraph_title"}
NEGATIVE_TYPES = {"figure_caption", "table_caption", "figure_title", "table_title", "chart_title",
                  "header", "footer", "reference", "equation", "formula", "footnote", "number",
                  "content", "aside_text", "formula_number"}


@dataclass
class CustomPattern:
    regex: str
    priority: int = 50
    strong: bool = False


@dataclass
class RuleConfig:
    # 只识别带阿拉伯数字 / 中文数字编号的标题（第X章、一、、（一）、1.1 等）；
    # 关闭后才会使用特殊标题（前言、总结…）和纯字号规则
    numbered_only: bool = True
    min_score: float = 0.5
    max_level: int = 3
    header_zone: float = 0.06
    footer_zone: float = 0.06
    min_chars: int = 2
    max_chars: int = 60
    short_chars: int = 30
    repeat_page_ratio: float = 0.3
    running_zone: float = 0.15  # 页眉/页脚候选区域（占页高比例），在其中重复 3 页以上的文本视为页眉页脚
    min_ocr_score: float = 0.6
    size_ratio_small: float = 1.12
    size_ratio_large: float = 1.35
    w_layout_title: float = 0.45
    w_number: float = 0.35
    w_number_strong: float = 0.5
    w_font_small: float = 0.2
    w_font_large: float = 0.35
    w_bold: float = 0.15
    w_short: float = 0.1
    special_titles: list = field(default_factory=lambda: list(DEFAULT_SPECIALS))
    patterns: list = field(default_factory=list)

    @classmethod
    def load(cls, path: str) -> "RuleConfig":
        """读取与 C++ 版共用的 key = value 规则文件。"""
        cfg = cls()
        with open(path, encoding="utf-8-sig") as f:
            for lineno, raw in enumerate(f, 1):
                line = raw.strip()
                if not line or line[0] in "#;" or "=" not in line:
                    continue
                key, val = (s.strip() for s in line.split("=", 1))
                # 行尾注释
                if key != "pattern" and "#" in val:
                    val = val.split("#", 1)[0].strip()
                try:
                    if key == "special_reset":
                        if val.lower() in ("1", "true", "yes", "on"):
                            cfg.special_titles = []
                    elif key == "special":
                        cfg.special_titles.append(val)
                    elif key == "pattern":
                        prio, strong, regex = (s.strip() for s in val.split("|", 2))
                        cfg.patterns.append(CustomPattern(regex, int(prio), strong.lower() in ("1", "true", "yes", "on")))
                    elif hasattr(cfg, key) and key not in ("special_titles", "patterns"):
                        cur = getattr(cfg, key)
                        if isinstance(cur, bool):
                            setattr(cfg, key, val.lower() in ("1", "true", "yes", "on"))
                        elif isinstance(cur, int):
                            setattr(cfg, key, int(float(val)))
                        else:
                            setattr(cfg, key, float(val))
                    else:
                        from .i18n import t
                        print(t("rule_unknown", line=lineno, key=key), file=sys.stderr)
                except ValueError as e:
                    raise ValueError(f"{path} 第 {lineno} 行解析失败: {e}") from e
        return cfg


_CN = r"[一二三四五六七八九十百千零〇两0-9]+"

# (正则, 样式, 优先级, 强规则)；优先级越小层级越高
BUILTIN_RULES = [
    (re.compile(rf"^第\s*{_CN}\s*(篇|部分|编)"), "part", 0, True),
    (re.compile(r"^part\s+([0-9]+|[ivxlc]+)\b", re.I), "part", 0, True),
    (re.compile(rf"^第\s*{_CN}\s*章"), "chapter", 10, True),
    (re.compile(r"^chapter\s+([0-9]+|[ivxlc]+)\b", re.I), "chapter", 10, True),
    (re.compile(rf"^第\s*{_CN}\s*节"), "section", 20, True),
    (re.compile(r"^[A-Z]\.\d+\.\d+\.\d+\.?\s*[^\d.\s%]"), "alpha4", 80, False),
    (re.compile(r"^[A-Z]\.\d+\.\d+\.?\s*[^\d.\s%]"), "alpha3", 70, False),
    (re.compile(r"^[A-Z]\.\d+\.?\s*[^\d.\s%]"), "alpha2", 60, False),
    (re.compile(r"^[IVX]+\.\s*[^\d\s.]"), "roman", 25, False),
    (re.compile(r"^[一二三四五六七八九十]+\s*[、.]\s*\S"), "cn_num", 30, False),
    (re.compile(r"^\([一二三四五六七八九十]+\)\s*\S"), "cn_paren", 40, False),
    # 编号后可隔空格接以数字开头的文字（“15.3 64位微型机”“8.3 8259A应用举例”），纯数字行不算
    (re.compile(r"^\d+(\.\d+){3}\.?(\s+\d*[^\d\s.,%]|\s*[^\d.\s])"), "decimal4", 80, False),
    (re.compile(r"^\d+(\.\d+){2}\.?(\s+\d*[^\d\s.,%]|\s*[^\d.\s])"), "decimal3", 70, False),
    (re.compile(r"^\d+\.\d+\.?(\s+\d*[^\d\s.,%]|\s*[^\d.\s%])"), "decimal2", 60, False),
    # 章名以数字+字母开头：“6 3D向量类”“10 3D中的方位”
    (re.compile(r"^\d{1,2}\s+\d[A-Za-z]\S*"), "decimal1", 55, False),
    (re.compile(r"^\d{1,2}\s*[.、]\s*[^\d.\s]"), "decimal1", 55, False),
    (re.compile(r"^\d{1,2}\s+[^\d.\s,，。%年月日]"), "decimal1", 55, False),
    (re.compile(r"^\(\d{1,2}\)\s*\S"), "arabic_paren", 90, False),
]

# 附录：“附录”“附录A”“附录一”“附录 2”“Appendix B”，与章同级
# 字母后允许一个 OCR 杂字符（附录Ai、附录A|），见 _fix_appendix_ocr
_APPENDIX = re.compile(
    r"^(附\s*录\s*([A-Z](?![a-z])|[一二三四五六七八九十]+|\d+)?|(?i:appendix)(\s+([A-Z]|\d+)\b)?)")
_LEADER = re.compile(r"(\.{3,}|…+|·{3,}|-{4,}|_{4,})\s*\d+\s*$")
_LEADER_STRIP = re.compile(r"\s*(\.{3,}|…+|·{3,}|-{4,}|_{4,}|\s[.·…])\s*\d*\s*$")
# 目录项末尾的页码：引导点/空格分隔，或直接接在中文、右括号后，如 “2.10 类型转换 059”“双重指针210”
_TOC_PAGE_NUM = re.compile(
    r"(?:(\.{2,}|…+|·{2,}|-{3,}|_{3,}|\s+[.·…])\s*|\s+|(?<=[\u4e00-\u9fff)]))(\d{1,4})$")
_PURE_NUMBER = re.compile(r"^\d{1,4}$")
# 网页转 PDF 常见的列表式目录：“• 1 词法缺陷”“o 1.1 = 不是 ==”
_TOC_BULLET = re.compile(r"^[•·●○◦▪■□►\-*]\s*|^o\s+")
_HAS_CJK = re.compile(r"[\u4e00-\u9fff]")
_TOC_TITLES = {"目录", "目次", "contents", "tableofcontents"}
_CAPTION = re.compile(r"^(图|表|figure|fig\.|table)\s*[0-9一二三四五六七八九十]", re.I)
# 插图步骤说明（“第3步 将对象绕z轴旋转”），不是章/节标题
_FIGURE_STEP = re.compile(r"^第\s*\d+\s*步\b")
# 章末习题/复习题标题（本身可不入书签，其后的 2.14、2.15… 编号题一律丢掉）
_EXERCISE_HEAD = re.compile(
    r"^(第\s*\d+\s*章\s*)?(习题|练习题|复习题|思考题|复习思考题|练习与思考|problems|exercises)\b", re.I)
# 练习题正文：问句或“简述/试述/优缺点…”等出题套话（与真·小节标题区分）
_EXERCISE_END = re.compile(r"[？?吗呢]\s*$")
_EXERCISE_ASK = re.compile(
    r"(是什么|什么是|有哪些|哪些是|如何理解|怎样理解|为什么|为何|是否|简述|试述|试说明|请说明|"
    r"优缺点|分别说明|有何区别|谈谈你|分析原因|"
    r"^列出|^说明|^考虑|^假设|^设有|^试求|^试证|^计算下列|^写出)")
_PAGE_NUMBER = re.compile(r"^(page\s*)?[\s\d\-–—/第页共.]+$|^[ivxlc]+$", re.I)
_FULLWIDTH = {c: c - 0xFEE0 for c in range(0xFF01, 0xFF5F)}
_DECIMAL_STYLES = frozenset({"decimal1", "decimal2", "decimal3", "decimal4",
                             "alpha2", "alpha3", "alpha4"})


def _looks_like_exercise(w: str, style: str) -> bool:
    """编号形式的练习题（“2.14 …是什么?”），不是章节标题。"""
    if style not in _DECIMAL_STYLES:
        return False
    body = _title_text(normalize(w))
    if not body:
        return False
    if _EXERCISE_END.search(body):
        return True
    m = _EXERCISE_ASK.search(body)
    if not m:
        return False
    # “列出/说明…”起笔的题干可以很短；其余套话需足够长以免误伤真小节
    if m.start() == 0 and re.match(r"列出|说明|考虑|假设|设有|试求|试证|计算下列|写出", m.group(0) or body):
        return True
    return len(body) >= 18


def _fix_appendix_ocr(w: str) -> str:
    """OCR 常把附录号旁的装饰认成多余字母，或字母与标题粘连：
    “附录Ai 标题”→“附录A 标题”，“附录A计算机…”→“附录A 计算机…”。"""
    w = re.sub(r"^(附\s*录\s*)([A-Za-z])[il1|I．.·•](?=[\s\u4e00-\u9fff])",
               lambda m: f"{m.group(1)}{m.group(2).upper()} ", w, count=1)
    return re.sub(r"^(附\s*录\s*)([A-Za-z])(?=[\u4e00-\u9fff])",
                  lambda m: f"{m.group(1)}{m.group(2).upper()} ", w, count=1)


def normalize(text: str) -> str:
    """全角转半角并压缩空白；逐字符映射，长度与 collapse(text) 相同。"""
    return _fix_appendix_ocr(collapse(text).translate(_FULLWIDTH))


def collapse(text: str) -> str:
    return " ".join(text.split())


def _compact(s: str, drop_digits: bool = False) -> str:
    s = s.replace(" ", "").lower()
    return re.sub(r"[0-9]", "", s) if drop_digits else s


_CN_DIGITS = {c: i for i, c in enumerate("零一二三四五六七八九")} | {"〇": 0, "两": 2}


def _cn_to_int(s: str) -> int | None:
    if s.isdigit():
        return int(s)
    total, cur = 0, 0
    for ch in s:
        if ch in _CN_DIGITS:
            cur = _CN_DIGITS[ch]
        elif ch in "十百千":
            total += (cur or 1) * {"十": 10, "百": 100, "千": 1000}[ch]
            cur = 0
        else:
            return None
    return total + cur


def _number_key(title: str, style: str) -> str | None:
    """全书唯一的编号（多级小节号、第X章/篇）；“第X节”“1.”“(1)”等会在各章重复，不参与去重。"""
    w = normalize(title)
    if style in ("decimal2", "decimal3", "decimal4"):
        m = re.match(r"\d+(?:\.\d+)+", w)
        return m.group(0) if m else None
    if style.startswith("alpha"):
        m = re.match(r"[A-Z](?:\.\d+)+", w)
        return m.group(0) if m else None
    if style in ("chapter", "part"):
        m = re.match(rf"第\s*({_CN})\s*(章|篇|部分|编)", w)
        if m:
            n = _cn_to_int(m.group(1))
            return f"{m.group(2)}{n}" if n is not None else None
    if style == "appendix":
        m = _APPENDIX.match(w)
        return "附录" + (m.group(2) or m.group(4) or "").upper()
    return None


def _title_text(w: str) -> str:
    """去掉编号后的标题文字，用于在目录中按文字查找。"""
    return _compact(re.sub(rf"^(\d+(?:\.\d+)*|第\s*{_CN}\s*(?:章|篇|部分|编))[.、\s]*", "", w))


def _is_subsequence(short: str, long: str) -> bool:
    it = iter(long)
    return all(c in it for c in short)


_BARE_NUMBER = re.compile(r"^\d+(?:[.．]\d+)+[.．]?$")
# 目录项末尾括号里的页码：“8086的寻址方式和指令系统 （54）”
_PAREN_PAGE = re.compile(r"(\s+[(（]\s*\d{1,4}|\s*[(（]\s*\d{2,4})\s*[)）]\s*$")


_IO = re.compile(r"[1lI|]?\s*/\s*[0O](?=[\u4e00-\u9fff\s]|$)")
_CIRCLED = {chr(0x2460 + i): str(i + 1) for i in range(20)}  # ①～⑳
# 章号中间可能夹着 OCR 杂字符：“第 ;( 16 章”
_CHAPTER_OCR = re.compile(r"^第\s*([^章\u4e00-\u9fff]{0,8}?)\s*章\s*")
_APPENDIX_BARE = re.compile(r"^附\s*录\s*[A-Z]?\s*$")
# 标题开头的编号部分（目录反查时只向目录借编号，文字用正文大字行的）
_NUM_PREFIX = re.compile(
    rf"^(?:第\s*{_CN}\s*(?:章|篇|部分|编)|附\s*录\s*(?:[A-Z]|[一二三四五六七八九十]+|\d+)?"
    rf"|appendix\s+(?:[A-Z]|\d+)|part\s+\S+|\d+(?:\.\d+)+|[A-Z](?:\.\d+)+)", re.I)
# 只有篇号的行，行尾还常混入印刷页码：“第二部分72”，下面的大字行才是篇名
_PART_BARE = re.compile(rf"^第\s*({_CN})\s*(篇|部分|编)\s*(?:\d{{1,4}})?\s*$")
_SECTION_NUM = re.compile(r"^(\d+)\s*\.\s*\d")
_FIRST_SECTION = re.compile(r"^(\d+)\s*\.\s*1(?!\d)")


def _fix_chapter_lines(lines: list[TextLine]) -> list[TextLine]:
    """章首页的大号章号常是装饰字体（如带圆圈的“第⑪章”），OCR 会读错或丢掉数字，章名也常单独占一行：
    - 章号缺失或与随后的小节编号矛盾（“第1章”下面紧跟“11.1”）时，按小节编号改正；
    - 只有章号的行与紧挨在下面的大字章名合并。"""
    if not any(l.from_ocr and (_CHAPTER_OCR.match(l.text) or _PART_BARE.match(l.text.strip())) for l in lines):
        return lines
    order = sorted(range(len(lines)), key=lambda i: (lines[i].page, lines[i].y0))
    lines = list(lines)
    merged = set()
    heights = sorted(l.y1 - l.y0 for l in lines if l.text and l.from_ocr)
    med = heights[len(heights) // 2] if heights else 0.0

    def big(l):
        # 章首页的章号、章名字号很大；页眉里的“第3章 对象的共享”是小字，不参与推断
        return med > 0 and l.y1 - l.y0 >= 1.8 * med

    def merge_below(pos, l, rest):
        """只有章号（或“附录A”）的行，与紧挨在下面的大字标题合并。
        OCR 常产生一堆空文本框，最多看下面 3 个有文字的行，空框不占名额。"""
        h = l.y1 - l.y0
        seen = 0
        for j in order[pos + 1:]:
            o = lines[j]
            if o.page != l.page:
                break
            if not o.text.strip():
                continue
            seen += 1
            if o.y0 - l.y1 > 2 * h or seen > 3:
                break
            if o.y1 - o.y0 >= 0.6 * h and not _SECTION_NUM.match(o.text) \
                    and len(o.text.strip()) <= 30:
                merged.add(j)
                return o.text.strip(), replace(l, y1=max(l.y1, o.y1), x0=min(l.x0, o.x0), x1=max(l.x1, o.x1),
                                               layout_type=l.layout_type or o.layout_type)
        return rest, l

    for pos, i in enumerate(order):
        l = lines[i]
        if not l.from_ocr:
            continue
        if _APPENDIX_BARE.match(l.text):
            rest, l = merge_below(pos, l, "")
            if rest:
                lines[i] = replace(l, text=f"{l.text.strip()} {rest}")
            continue
        pm = _PART_BARE.match(l.text.strip())
        if pm:
            # 篇号行无篇名（行尾数字是印刷页码，一并去掉）：与下面的大字篇名合并
            rest, l = merge_below(pos, l, "")
            head = f"第{pm.group(1)}{pm.group(2)}"
            lines[i] = replace(l, text=f"{head} {rest}".strip())
            continue
        m = _CHAPTER_OCR.match(l.text)
        if not m or not big(l):
            continue
        num = "".join(_CIRCLED.get(c, c) for c in m.group(1))
        num = "".join(re.findall(r"\d", num))
        rest = l.text[m.end():].strip()
        # 本章第一个小节编号（3 页以内）
        nxt = None
        for j in order[pos + 1:]:
            o = lines[j]
            if o.page > l.page + 3 or (o is not l and _CHAPTER_OCR.match(o.text)):
                break
            s = _SECTION_NUM.match(o.text)
            if s:
                nxt = s.group(1)
                break
        if nxt and num != nxt:
            num = nxt
        if not num:
            continue
        if not rest:
            rest, l = merge_below(pos, l, rest)
        lines[i] = replace(l, text=f"第{num}章 {rest}".strip())

    # 章号整行没检测出来（装饰字体）：有“N.1”却没有第N章时，把同页“N.1”上方的大字标题补成“第N章 标题”
    found = {int(m.group(1)) for m in (re.match(r"^第(\d+)章", l.text) for l in lines if big(l)) if m}
    if len(found) >= 3:
        for pos, i in enumerate(order):
            l = lines[i]
            s = _FIRST_SECTION.match(l.text) if l.from_ocr else None
            if not s or int(s.group(1)) in found:
                continue
            n = int(s.group(1))
            above = [j for j in order[:pos] if lines[j].page == l.page and j not in merged
                     and lines[j].y1 <= l.y0 and lines[j].text.strip()]
            cands = [j for j in above if lines[j].y1 - lines[j].y0 >= 2 * med and len(lines[j].text.strip()) <= 20
                     and not re.match(r"^(第|附\s*录|\d)", lines[j].text.strip())]
            if med and cands:
                j = cands[-1]
                lines[j] = replace(lines[j], text=f"第{n}章 {lines[j].text.strip()}")
                found.add(n)
    return [l for k, l in enumerate(lines) if k not in merged]


def _fix_ocr_dots(lines: list[TextLine]) -> list[TextLine]:
    """OCR 常把编号里的点识别成句号（“1。1 计算机中数的表示方法”“3。十六进制数”），
    把“I/O”识别成“1/0”“/0”（“6.11/0接口”实为“6.1 I/O接口”）。"""
    out = []
    for l in lines:
        if l.from_ocr:
            text = l.text
            # 双语章号同行（“第9章 Chapter 9多GPU”“Chapter 10 第10章”）：只保留中文号
            text = re.sub(r"^第\s*(\d+)\s*章\s*[Cc]hapter\s*\d+\s*", r"第\1章 ", text)
            text = re.sub(r"^[Cc]hapter\s*(\d+)\s*第\s*\d+\s*章\s*", r"第\1章 ", text)
            # 英文章号被 OCR 读花并紧贴在中文号后（“第12章 Chyter12归约算法”）：剥掉该碎片
            text = re.sub(r"^第\s*(\d+)\s*章\s*[A-Za-z][a-z]{2,}\d{1,2}\s*", r"第\1章 ", text)
            m = re.match(r"\d+(?:[.。]\d+)*[.。]?", text)
            if m and "。" in m.group(0):
                text = m.group(0).replace("。", ".") + text[m.end():]
            text = _IO.sub(" I/O", text).replace("  I/O", " I/O")
            if text != l.text:
                l = replace(l, text=text.strip())
        out.append(l)
    return out


def _join_split_numbers(lines: list[TextLine]) -> list[TextLine]:
    """OCR 有时把“4.2”与后面的标题文字拆成两行，按同一行位置拼回去。"""
    if not any(l.from_ocr and _BARE_NUMBER.match(l.text.strip()) for l in lines):
        return lines
    out, used = [], set()
    for i, l in enumerate(lines):
        if i in used:
            continue
        if l.from_ocr and _BARE_NUMBER.match(l.text.strip()):
            h = max(l.y1 - l.y0, 1.0)
            cy = (l.y0 + l.y1) / 2
            # 允许编号框与标题框略微重叠（残缺字号时检测框常交叠）
            cands = [j for j, m in enumerate(lines) if j != i and j not in used and m.page == l.page
                     and abs((m.y0 + m.y1) / 2 - cy) < 0.5 * h
                     and l.x0 < m.x0 and m.x0 - l.x1 < 3 * h
                     and not _BARE_NUMBER.match(m.text.strip())]
            if cands:
                j = min(cands, key=lambda j: lines[j].x0)
                m = lines[j]
                used.add(j)
                l = replace(l, text=f"{l.text.strip()} {m.text.strip()}", x1=max(l.x1, m.x1),
                            y0=min(l.y0, m.y0), y1=max(l.y1, m.y1),
                            layout_type=l.layout_type or m.layout_type,
                            ocr_score=min(l.ocr_score, m.ocr_score))
        out.append(l)
    return out


_DISPLAY_NUMBER = re.compile(r"^(\d{1,2}|[A-Z])$")


_EN_CHAPTER_BARE = re.compile(r"^chapter\s+(?:\d+|[ivxlcdm]+)\.?\s*$", re.I)
_EN_CHAPTER_NUM = re.compile(r"^chapter\s+(?:\d+|[ivxlcdm]+)\b", re.I)


def _join_english_chapter_titles(lines: list[TextLine]) -> list[TextLine]:
    """英文书章名常折两行：大字 “Chapter 3” + 下一行大字 “Rasterization” → “Chapter 3 Rasterization”。"""
    body = {ocr: _body_size(lines, ocr) for ocr in (False, True)}
    order = sorted(range(len(lines)), key=lambda i: (lines[i].page, lines[i].y0))
    out, used = list(lines), set()
    for pos, i in enumerate(order):
        l = lines[i]
        if i in used:
            continue
        w = l.text.strip()
        b = body[l.from_ocr]
        if not b or l.font_size < 1.5 * b or not _EN_CHAPTER_BARE.match(w):
            continue
        h = max(l.y1 - l.y0, 1.0)
        for j in order[pos + 1:pos + 6]:
            m = lines[j]
            if m.page != l.page or m.y0 - l.y1 > 3.5 * h:
                break
            t = m.text.strip()
            mb = body[m.from_ocr]
            if (not t or not mb or m.font_size < 1.3 * mb or len(t) > 80
                    or _SECTION_NUM.match(t) or _EN_CHAPTER_NUM.match(t) or t[0].isdigit()):
                continue
            used.add(j)
            out[i] = replace(l, text=f"{w.rstrip(' .')} {t}", x1=max(l.x1, m.x1), y1=max(l.y1, m.y1),
                             font_size=max(l.font_size, m.font_size), bold=l.bold or m.bold,
                             layout_type=l.layout_type or m.layout_type)
            break
    return [l for k, l in enumerate(out) if k not in used]


def _join_title_continuations(lines: list[TextLine]) -> list[TextLine]:
    """标题折行续写：上行大字标题未写完，下行是短尾巴（“Parameterizatio”+“n”，“Revisite”+“d+”）。"""
    body = {ocr: _body_size(lines, ocr) for ocr in (False, True)}
    order = sorted(range(len(lines)), key=lambda i: (lines[i].page, lines[i].y0))
    out, used = list(lines), set()
    for pos, i in enumerate(order):
        l = lines[i]
        if i in used:
            continue
        b = body[l.from_ocr]
        t0 = l.text.strip()
        if not b or not t0:
            continue
        numbered = bool(re.match(r"^(\d+(?:\.\d+)+|chapter\s+\d+)\b", t0, re.I))
        # 带编号的小节标题字号可仅略大于正文
        if l.font_size < (1.05 * b if numbered else 1.2 * b):
            continue
        if not (numbered or l.font_size >= 1.35 * b):
            continue
        h = max(l.y1 - l.y0, 1.0)
        for j in order[pos + 1:pos + 3]:
            m = lines[j]
            if m.page != l.page or m.y0 - l.y1 > 0.9 * h or j in used:
                break
            t1 = m.text.strip()
            if not t1 or abs(m.font_size - l.font_size) > 0.6 * max(l.font_size, 1.0):
                break
            # 短尾巴或小写续写；不像新编号标题
            if _SECTION_NUM.match(t1) or _EN_CHAPTER_NUM.match(t1) or re.match(r"^\d+(?:\.\d+)+", t1):
                break
            short_tail = len(t1) <= 6 and re.fullmatch(r"[A-Za-z]{1,4}\+?", t1)
            lower_cont = t1[0].islower() and len(t1) <= 24
            if not (short_tail or lower_cont):
                break
            if short_tail:
                t1 = t1.rstrip("+")  # OCR 把 ∗ 等装饰读成 “d+”
            glue = "" if (t0[-1].isalnum() and (t1[:1].islower() or short_tail)) else " "
            used.add(j)
            t0 = f"{t0}{glue}{t1}"
            out[i] = replace(l, text=t0, x1=max(l.x1, m.x1), y1=max(l.y1, m.y1),
                             layout_type=l.layout_type or m.layout_type)
            l = out[i]
    return [l for k, l in enumerate(out) if k not in used]


# “N.1”是本章第一个小节；有的书小节号带尾点（“1.1.”），但“N.12”“N.1.2”不是
_FIRST_SEC2 = re.compile(r"^(\d{1,2})\.1(?!\d)(?:\.(?!\d)|\s|$)")


def _promote_missing_chapters(lines: list[TextLine]) -> list[TextLine]:
    """章号行没识别出来时（装饰数字被 OCR 读成空、或章名单独成行），按同页第一个“N.1”上面的大字标题补章。

    只补“已有章号体系里缺失”的章号；章的写法跟随本书已识别章标题的主流形式
    （“第N章 …”或“N …”），避免混写导致层级错位。
    """
    if not lines:
        return lines
    body = {ocr: _body_size(lines, ocr) for ocr in (False, True)}
    heights = sorted(l.y1 - l.y0 for l in lines if l.text.strip())
    med_h = heights[len(heights) // 2] if heights else 0.0

    def big(l) -> bool:
        b = body[l.from_ocr]
        return bool(b and l.font_size >= 1.5 * b) or (med_h > 0 and l.y1 - l.y0 >= 1.8 * med_h)

    # 目录页不能拿条目触发补章（会占掉章号，真章首页就不再补）。
    # 目录按章分页时每页不同章号不多，判据用：≥2 个不同章的“N.1”，或 ≥3 条带点线/尾页码的编号条目
    sec_chapters: dict[int, set[int]] = defaultdict(set)
    toc_entries: Counter = Counter()
    for l in lines:
        w = normalize(l.text)
        m = _FIRST_SEC2.match(w)
        if m:
            sec_chapters[l.page].add(int(m.group(1)))
        if re.match(r"^\d{1,2}\.\d", w) and re.search(r"(?:\.{2,}|…|\s)\s*\d{1,4}$|\.{2,}$", w):
            toc_entries[l.page] += 1
    toc_like = {p for p, ns in sec_chapters.items() if len(ns) >= 2} | {p for p, c in toc_entries.items() if c >= 3}
    # 页眉形态（去掉尾页码后在 ≥3 页重复）不能冒充已有章标题，如“第15章图形数学339”
    key_pages: dict[str, set[int]] = defaultdict(set)
    for l in lines:
        key_pages[_compact(normalize(l.text), True)].add(l.page)

    def upper(l) -> bool:
        return l.page_height <= 0 or (l.y0 + l.y1) / 2 <= 0.5 * l.page_height

    existing, cn_n, en_n, num_n = set(), 0, 0, 0
    for l in lines:
        if not big(l) or not upper(l):
            continue
        w = normalize(l.text)
        if len(key_pages[_compact(w, True)]) >= 3:
            continue
        # 正文句子（“第8章将介绍…，…”）和公式行（“2 =(-3)(5)…”）字号也会偏大，不能算已有章标题
        if len(w) > 40 or any(ch in w for ch in "。；;，,"):
            continue
        m = re.match(rf"第\s*({_CN})\s*章", w)
        if m and _cn_to_int(m.group(1)) is not None:
            existing.add(_cn_to_int(m.group(1)))
            cn_n += 1
            if os.environ.get("PDF_BOOKMARK_DEBUG_PROMOTE"):
                print(f"[promote] claim cn {_cn_to_int(m.group(1))} p{l.page + 1} {l.text.strip()!r}", file=sys.stderr)
            continue
        m = re.match(r"^chapter\s+(\d+)", w, re.I)
        if m:
            existing.add(int(m.group(1)))
            en_n += 1
            if os.environ.get("PDF_BOOKMARK_DEBUG_PROMOTE"):
                print(f"[promote] claim en {int(m.group(1))} p{l.page + 1} {l.text.strip()!r}", file=sys.stderr)
            continue
        # 章名可由数字开头（“6 3D向量类”），但编号后须是文字/数字；
        # 公式/矩阵行（“2 =(-3)(5)…”、“1 0 −xe 0 1 …”、“1 u u2 u3 . . . un”）不算：标题须有像样的词
        m = re.match(r"^(\d{1,2})\s+[\u4e00-\u9fffA-Za-z0-9]", w)
        if m and not _SECTION_NUM.match(w) and re.search(r"[A-Za-z]{3,}|[\u4e00-\u9fff]{2,}", w) \
                and not re.search(r"[=<>−]", w):
            existing.add(int(m.group(1)))
            num_n += 1
            if os.environ.get("PDF_BOOKMARK_DEBUG_PROMOTE"):
                print(f"[promote] claim num {int(m.group(1))} p{l.page + 1} {l.text.strip()!r}", file=sys.stderr)

    def chapter_text(n: int, title: str) -> str:
        if num_n >= cn_n and num_n >= en_n:
            return f"{n} {title}"
        return f"Chapter {n} {title}" if en_n > cn_n else f"第{n}章 {title}"

    order = sorted(range(len(lines)), key=lambda i: (lines[i].page, lines[i].y0))
    out = list(lines)

    def cand_ok(o) -> bool:
        t = o.text.strip()
        # 单字大行多是装饰字（如章首页美术字的首字）；纯符号碎行（“{}$”）也不是章名
        return bool(t and 2 <= len(t) <= 35 and big(o) and re.search(r"[\u4e00-\u9fffA-Za-z]", t)
                    and not _SECTION_NUM.match(t)
                    and not re.match(rf"^(第|附\s*录|\d|chapter\b)", t, re.I)
                    and t[-1] not in "。；;，,：:"
                    and not re.fullmatch(r"[A-Za-z]{1,4}", t))  # PDG 之类水印、装饰字母本身

    def find_cand(pos, page, y0_limit, x_ref):
        """在指定页找无编号大字行作章/附录名：先要求左对齐（排除右侧装饰/图注），
        找不到再放宽到页面上半部的居中大标题（有的书章名居中排版）。
        y0_limit 为触发行顶（同页时用），跨页找整页时传 None。"""
        for strict in (True, False):
            for j in order[:pos]:
                o = lines[j]
                if o.page != page or (y0_limit is not None and o.y1 > y0_limit):
                    continue
                if strict:
                    if o.x0 > x_ref + 2.0 * max(o.y1 - o.y0, 1.0):
                        continue
                else:
                    b = body[o.from_ocr]
                    if not b or o.font_size < 1.7 * b or not upper(o):
                        continue
                if cand_ok(o):
                    return j
        return None

    def full_title(cand):
        """候选行作标题时带上折行：紧挨着的同字号大字行是同一标题的下半行
        （居中排版的长章名常断成两行，第一行还常以逗号结尾）。"""
        o = lines[cand]
        title = o.text.strip()
        for j in order[order.index(cand) + 1:order.index(cand) + 3]:
            m = lines[j]
            if m.page != o.page:
                break
            t = m.text.strip()
            if not t:
                continue
            if (abs(m.font_size - o.font_size) <= 0.3 * max(o.font_size, 1.0)
                    and m.y0 - o.y1 < 1.0 * max(o.y1 - o.y0, 1.0)
                    and not _SECTION_NUM.match(t)
                    and not re.match(rf"^(第|附\s*录|\d|chapter\b)", t, re.I)
                    and len(title) + len(t) <= 60):
                title = f"{title} {t}"
            break
        return title

    dbg = os.environ.get("PDF_BOOKMARK_DEBUG_PROMOTE")
    for pos, i in enumerate(order):
        l = lines[i]
        w = normalize(l.text)
        m = _FIRST_SEC2.match(w)
        if not m:
            continue
        n = int(m.group(1))
        if n in existing or l.page in toc_like:
            if dbg:
                print(f"[promote] skip n={n} p{l.page + 1} existing={n in existing} toc_like={l.page in toc_like} {l.text.strip()!r}", file=sys.stderr)
            continue
        # 目录条目（“8.1 变换物体 … 93”“2.1 ID数学..”）不能触发补章，否则会占掉章号、真章首页不再补
        if re.search(r"(?:\.{2,}|…+|\s)\s*\d{1,4}$", w) or re.search(r"(?:\.{2,}|…+)$", w):
            if dbg:
                print(f"[promote] dot-guard n={n} p{l.page + 1} {l.text.strip()!r}", file=sys.stderr)
            continue

        # 同页该小节上方最靠前的大字无编号行即章名（其间多为本章简介）
        cand = find_cand(pos, l.page, l.y0, l.x0)
        # 章名在上一页（章首页只有章名+本章简介，N.1 标题在下一页）：上一页不得有别章小节
        if cand is None and l.page > 0:
            prev = [o for o in lines if o.page == l.page - 1]
            foreign = any(
                (mm := re.match(r"^(\d{1,2})\.\d", normalize(o.text))) and int(mm.group(1)) != n
                for o in prev)
            if not foreign:
                cand = find_cand(pos, l.page - 1, None, l.x0)
        if cand is not None:
            if dbg:
                print(f"[promote] rename n={n} p{lines[cand].page + 1} trig_p{l.page + 1} {lines[cand].text.strip()!r}", file=sys.stderr)
            out[cand] = replace(out[cand], text=chapter_text(n, full_title(cand)))
            existing.add(n)
            continue
        if dbg:
            print(f"[promote] no-cand n={n} p{l.page + 1} {l.text.strip()!r}", file=sys.stderr)
        # 章名只出现在页眉里（章分隔页的标题没被 OCR 到）：用页眉里出现最多的章名，
        # 在第一个小节的位置合成章标题
        if len(existing) < 2:
            continue
        titles = Counter()
        for o in lines:
            w = normalize(o.text)
            mm = (re.match(rf"^第\s*{n}\s*章[\s·•．.、:：-]*(\S.*?)\s*$", w)
                  or re.match(rf"^chapter\s+{n}\b[\s:.-]*(\S.*?)\s*$", w, re.I))
            if not mm:
                continue
            rest = re.sub(r"\d{1,4}$", "", mm.group(1)).strip()
            if 2 <= len(rest) <= 30 and not any(ch in rest for ch in "。；;，,"):
                titles[rest] += 1
        if not titles:
            continue
        title, cnt = titles.most_common(1)[0]
        if cnt < 2 and not big(l):
            continue
        h = max(l.y1 - l.y0, 1.0)
        b = body[l.from_ocr]
        out.append(replace(l, text=chapter_text(n, title), y0=max(0.0, l.y0 - h), y1=l.y0,
                           font_size=max(l.font_size * 1.6, (b or l.font_size) * 1.8),
                           layout_type=l.layout_type or "paragraph_title"))
        existing.add(n)

    # 附录同理：装饰字母（如卷轴图里的 “A”）没读成标题时，靠第一个 “A.1” 上方的大字标题反推
    existing_app = set()
    for l in lines:
        if not big(l):
            continue
        m = re.match(r"^(?:附\s*录|appendix)\s*([A-Za-z])\b", normalize(l.text), re.I)
        if m:
            existing_app.add(m.group(1).upper())
    app_secs: Counter = Counter()
    for l in lines:
        m = re.match(r"^([A-Za-z])\.\d+(?![\d.])", normalize(l.text))
        if m:
            app_secs[m.group(1).upper()] += 1
    seen_app = set()
    for pos, i in enumerate(order):
        l = lines[i]
        w = normalize(l.text)
        m = re.match(r"^([A-Za-z])\.1(?![\d.])", w)
        if not m:
            continue
        letter = m.group(1).upper()
        # 该字母至少要有两个小节才像附录，正文里偶然的 “A.1” 列表项不算
        if letter in existing_app or letter in seen_app or l.page in toc_like or app_secs[letter] < 2:
            continue
        if re.search(r"(?:\.{2,}|…+|\s)\s*\d{1,4}$", w) or re.search(r"(?:\.{2,}|…+)$", w):
            continue
        seen_app.add(letter)
        cand = find_cand(pos, l.page, l.y0, l.x0)
        # 附录名在上一页时跨页找；上一页可以有末章小节，只排除别字母的附录小节
        if cand is None and l.page > 0:
            prev = [o for o in lines if o.page == l.page - 1]
            foreign = any(
                (mm := re.match(r"^([A-Za-z])\.\d", normalize(o.text))) and mm.group(1).upper() != letter
                for o in prev)
            if not foreign:
                cand = find_cand(pos, l.page - 1, None, l.x0)
        if cand is not None:
            title = full_title(cand)
            text = f"附录{letter} {title}" if _HAS_CJK.search(title) or not en_n else f"Appendix {letter} {title}"
            if dbg:
                print(f"[promote] rename app {letter} p{lines[cand].page + 1} trig_p{l.page + 1} {title!r}", file=sys.stderr)
            out[cand] = replace(out[cand], text=text)
            existing_app.add(letter)
    return out


def _join_display_numbers(lines: list[TextLine]) -> list[TextLine]:
    """英文书（LaTeX 排版）章首页的章号是单独一行的特大数字或字母，章名在下一行：
    “1” + “Substructural Type Systems” → “1 Substructural Type Systems”，
    “A” + “Solutions to Selected Exercises” → “Appendix A Solutions to Selected Exercises”。"""
    body = {ocr: _body_size(lines, ocr) for ocr in (False, True)}
    order = sorted(range(len(lines)), key=lambda i: (lines[i].page, lines[i].y0))
    out, used = list(lines), set()
    for pos, i in enumerate(order):
        l = lines[i]
        b = body[l.from_ocr]
        if i in used or not b or l.font_size < 2.5 * b or not _DISPLAY_NUMBER.match(l.text.strip()):
            continue
        h = l.y1 - l.y0
        for j in order[pos + 1:pos + 4]:
            m = lines[j]
            if m.page != l.page or m.y0 - l.y1 > 2.5 * h:
                break
            t = m.text.strip()
            if m.font_size < 1.3 * b and len(t) > 40:
                break  # 中间隔着正文行，数字下面不是章名
            # 章名可由数字开头（“3D向量类”），但不能是小节号（“3.1 …”）
            if m.font_size >= 1.3 * b and t and not _SECTION_NUM.match(t) and len(t) <= 80:
                num = l.text.strip()
                # 字母章号只接像样的附录标题；插图坐标轴 “X”“Z” 等单字母不要合成 Appendix
                if not num.isdigit() and (len(re.sub(r"\s", "", t)) < 8 or re.fullmatch(r"[A-Za-z.$_{}\\]{1,6}", t)):
                    continue
                used.add(j)
                # 折行的长章名：同字号、紧挨着的下一两行
                last = m
                for k in order[order.index(j) + 1:order.index(j) + 3]:
                    n = lines[k]
                    if (n.page != m.page or abs(n.font_size - m.font_size) > 0.5
                            or n.y0 - last.y1 > 0.8 * (m.y1 - m.y0) or len(t) + len(n.text) > 120):
                        break
                    t = f"{t} {n.text.strip()}"
                    used.add(k)
                    last = n
                text = f"{num} {t}" if num.isdigit() else f"Appendix {num} {t}"
                out[i] = replace(l, text=text, x1=max(l.x1, m.x1), y1=last.y1, font_size=m.font_size,
                                 bold=m.bold, layout_type=l.layout_type or m.layout_type)
                break
    return [l for k, l in enumerate(out) if k not in used]


def _body_size(lines: list[TextLine], ocr: bool) -> float:
    hist = Counter()
    for l in lines:
        if l.from_ocr == ocr and l.font_size > 0:
            # 只做了检测未识别的 OCR 正文行没有文字，按宽高比估计字数
            n = len(l.text) or round((l.x1 - l.x0) / max(l.y1 - l.y0, 1e-3))
            hist[round(l.font_size * 2)] += n
    return hist.most_common(1)[0][0] / 2 if hist else 0.0


class RuleEngine:
    def __init__(self, config: RuleConfig | None = None):
        self.cfg = config or RuleConfig()
        self.rules = []
        for p in self.cfg.patterns:
            try:
                self.rules.append((re.compile(p.regex, re.I), f"custom{p.priority}", p.priority, p.strong))
            except re.error as e:
                from .i18n import t
                print(t("rule_bad_regex", regex=p.regex, err=e), file=sys.stderr)
        self.rules += BUILTIN_RULES
        self.specials = {_compact(normalize(s)) for s in self.cfg.special_titles}

    def classify(self, text: str) -> tuple[str, int, bool]:
        """返回 (样式, 优先级, 是否强规则)，无编号时样式为空串。"""
        w = normalize(text)
        key = _compact(w)
        m = _APPENDIX.match(w)
        # 带编号的附录，或单独成行的“附录”；“附录中列出了……”这类正文不算
        if m and (m.group(2) or m.group(4) or len(key) <= 12):
            return "appendix", 10, True
        if key in self.specials:
            return "special", 10, True
        for regex, style, prio, strong in self.rules:
            if regex.match(w):
                return style, prio, strong
        return "", 0, False

    def toc_entry_base(self, w: str) -> str | None:
        """w 形如目录项（标题 + 页码，或列表符号 + 编号标题）时返回标题部分，否则返回 None。"""
        b = _TOC_BULLET.match(w)
        if b and self.classify(w[b.end():])[0]:
            return w[b.end():]
        p = _PAREN_PAGE.search(w)
        if p and p.start() >= 2:
            head = _LEADER_STRIP.sub("", w[: p.start()]).rstrip()
            if head and (_HAS_CJK.search(head) or self.classify(head)[0]):
                return head
        m = _TOC_PAGE_NUM.search(w)
        if not m or m.start() < 2:
            return None
        head = w[: m.start()].rstrip()
        if not head or head[-1] in "=+-*/<>,(&|[{:;%#":
            return None
        # 代码行（如 “return 0”）也以数字结尾：要求有引导点、含中文或以编号开头
        if m.group(1) or _HAS_CJK.search(head) or self.classify(head)[0]:
            return head
        return None

    def detect(self, lines: list[TextLine], page_count: int) -> list[Heading]:
        c = self.cfg
        lines = _promote_missing_chapters(_join_title_continuations(
            _join_english_chapter_titles(
                _join_display_numbers(_fix_chapter_lines(_join_split_numbers(_fix_ocr_dots(lines)))))))
        body_text, body_ocr = _body_size(lines, False), _body_size(lines, True)
        norm = [normalize(l.text) for l in lines]

        toc_pages = self._find_toc_pages(lines, norm)

        # 篇首页常只印篇名（“Precise Type Analyses”），罗马数字篇号只出现在目录（“I Precise Type Analyses 1”）
        parts = {}
        for l, w in zip(lines, norm):
            m = re.match(r"^((?:part\s+)?([IVX]{1,4}))\s+(\D.*?)\s+\d{1,4}$", w, re.I) if l.page in toc_pages else None
            if m:
                parts[_compact(m.group(3))] = m.group(2).upper()
        if parts:
            for i, (l, w) in enumerate(zip(lines, norm)):
                body = body_ocr if l.from_ocr else body_text
                num = parts.get(_compact(w))
                if num and l.page not in toc_pages and body and l.font_size >= 1.8 * body:
                    lines[i] = replace(l, text=f"Part {num} {collapse(l.text)}")
                    norm[i] = normalize(lines[i].text)

        # 有的 PDF 全文字体都标记为粗体，此时粗体不能区分标题
        body_lines = [l for l in lines if not l.from_ocr and body_text and abs(l.font_size - body_text) < 0.5]
        bold_useful = not body_lines or sum(l.bold for l in body_lines) * 2 < len(body_lines)

        # 页眉页脚：去掉数字后在多页重复出现的文本
        repeats = defaultdict(set)
        for l, w in zip(lines, norm):
            repeats[_compact(w, True)].add(l.page)

        def repeated(w):
            pages = repeats[_compact(w, True)]
            return len(pages) >= 3 and len(pages) >= c.repeat_page_ratio * max(1, page_count)

        # 章节页眉（如每页顶部的“第1章 引言”）：只在本章若干页重复，比例不高，但总出现在页边
        def edge(l):
            if l.page_height <= 0:
                return False
            r = (l.y0 + l.y1) / 2 / l.page_height
            return r < c.running_zone or r > 1 - c.running_zone

        def edge_key(w):
            # 页眉常带页码：“第3章 对象的共享 29”“30 Java并发编程实战”
            return _compact(re.sub(r"^\d{1,4}\s*|\s*\d{1,4}$", "", w))

        def edge_dist(l):
            return min(l.y0, l.page_height - l.y1)

        edge_pages = defaultdict(set)
        edge_nearest = {}  # (页, 文字) -> 离页边最近的距离
        edge_font = {}  # 文字 -> 页边同名行的最小字号
        edge_fonts = defaultdict(list)  # 文字 -> 页边同名行字号（取中位数抗 OCR 行高抖动）
        body_repeats = defaultdict(set)  # 不在页边的重复
        for l, w in zip(lines, norm):
            if edge(l):
                k = edge_key(w)
                edge_pages[k].add(l.page)
                edge_nearest[(l.page, k)] = min(edge_nearest.get((l.page, k), 1e9), edge_dist(l))
                edge_font[k] = min(edge_font.get(k, 1e9), l.font_size)
                if l.font_size > 0:
                    edge_fonts[k].append(l.font_size)
            else:
                body_repeats[_compact(w, True)].add(l.page)

        def running_header(l, w):
            # 页眉印的是本节标题时（“7.5 Contextual Equivalence 261”），页顶的同名标题不是页眉：
            # 它在同页页眉的下面，或字号明显比页眉大。
            # OCR 行高逐页抖动，字号不用最小值卡（某一页读大一号就整页漏过），用中位数的宽容上限。
            k = edge_key(w)
            if not (edge(l) and len(edge_pages[k]) >= 3
                    and edge_dist(l) <= edge_nearest.get((l.page, k), 1e9) + 0.5):
                return False
            fonts = sorted(edge_fonts.get(k, []))
            med = fonts[len(fonts) // 2] if fonts else l.font_size
            # 行尾带印刷页码的同名页边行必然是页眉（章标题行尾不会跟页码），字号再大也算页眉
            return bool(re.search(r"\d{1,4}$", w)) or l.font_size <= max(med * 1.35, med + 2.0)

        def repeated_here(l, w):
            # 只在页边重复（页眉页脚）时，不连累页面正中的同名标题
            # 页边那行比同名页眉字号大时是真标题（页眉印着本节标题）
            at_edge = edge(l) and l.font_size <= edge_font.get(edge_key(w), 0) + 0.5
            return repeated(w) and (at_edge or len(body_repeats[_compact(w, True)]) >= 3)

        # 目录页和章首页小目录里的条目：不作为标题，但其文字可修正正文标题的 OCR 错误
        mini_toc = self._find_mini_toc(lines, norm)
        toc_titles, toc_by_text, toc_printed = {}, {}, {}
        for i, (l, w) in enumerate(zip(lines, norm)):
            if l.page not in toc_pages and i not in mini_toc:
                continue
            base = _LEADER_STRIP.sub("", _PAREN_PAGE.sub("", self.toc_entry_base(w) or w)).rstrip()
            key = _number_key(base, self.classify(base)[0])
            if key and len(base) <= c.max_chars:
                start = w.find(base)
                toc_titles.setdefault(key, collapse(l.text)[start:start + len(base)])
                toc_by_text.setdefault(_title_text(base), key)
                # 目录行尾是印刷页号，和 PDF 页差一个偏移；只记下来供反查圈搜索范围，绝不作落点
                if l.page in toc_pages and key not in toc_printed:
                    pm = re.search(r"(\d{1,4})\s*[)）]?\s*$", w)
                    if pm:
                        toc_printed[key] = int(pm.group(1))

        acc = []  # (Heading, numbered, priority)
        in_exercises = False  # 已进入“习题/练习题”段，直到下一章/篇/附录
        cur_chapter = None  # 最近一个章级标题的章号（习题段的归属章）
        ex_chapter = None  # 习题段触发时所在章号

        def chap_of(w, style):
            if style in _DECIMAL_STYLES:
                m = re.match(r"(\d+)(?=[.\s])", w)  # “3.1 …”与“3 Raster Images”两种形态
                return int(m.group(1)) if m else None
            m = re.search(rf"第\s*({_CN})\s*章", w) or re.search(r"chapter\s+(\d+)", w, re.I)
            return _cn_to_int(m.group(1)) if m else None

        for i, (l, w0) in enumerate(zip(lines, norm)):
            w = _LEADER_STRIP.sub("", w0).rstrip()
            n = len(w)
            if n < c.min_chars or n > c.max_chars * 2 or _PAGE_NUMBER.match(w) or i in mini_toc:
                continue
            # 图注、表注、插图里的“第N步”绝不进书签（插图内还有小数字标注，靠版面 image 跳过）
            if _CAPTION.match(w) or _FIGURE_STEP.match(w) or l.layout_type in (
                    "figure_title", "table_title", "figure_caption", "table_caption", "chart_title"):
                continue
            if _EXERCISE_HEAD.match(w) or _EXERCISE_HEAD.match(_title_text(w)):
                in_exercises = True
                ex_chapter = chap_of(w, self.classify(w)[0])
                if ex_chapter is None:
                    ex_chapter = cur_chapter
                continue
            # 页眉字号小；章首页大字号的章标题字号远超页眉中位数，running_header 本身已放行，
            # 不再按正文字号豁免（OCR 正文估计偏低时页眉会被误豁免）
            if running_header(l, w0):
                continue
            style, prio, strong = self.classify(w)
            if style in ("chapter", "part", "appendix"):
                in_exercises = False
            ch = chap_of(w, style)
            if ch is not None and style in ("chapter", "part", "appendix", "decimal1"):
                cur_chapter = ch
            # 习题段只在本章内有效：数字式章标题（“3 Raster Images”）不是 chapter 样式，
            # 也要能结束习题段，否则一处 Exercises 会吞掉后续所有章的小节
            if in_exercises and ch is not None and ex_chapter is not None and ch != ex_chapter:
                in_exercises = False
            if in_exercises and style in _DECIMAL_STYLES:
                continue
            if c.numbered_only and style in ("", "special", "roman"):
                continue
            if l.page in toc_pages and (c.numbered_only or style != "special"):
                continue
            if _looks_like_exercise(w, style):
                continue
            key = _number_key(w, style)
            layout_title = l.layout_type in TITLE_TYPES
            # OCR 置信度低的行，只有版面判为标题且编号或标题文字能在目录中找到时才保留
            low_ocr = l.from_ocr and l.ocr_score < c.min_ocr_score
            if low_ocr and layout_title and key and key not in toc_titles:
                t = _title_text(w)
                key = next((k for s, k in toc_by_text.items() if s and t.startswith(s)), key)
            if low_ocr and not (layout_title and key in toc_titles):
                continue
            # OCR 的“1.”“(1)”等弱编号：版面未判为标题时多是正文里的列表项
            if l.from_ocr and style in ("decimal1", "arabic_paren") and (not layout_title or n > c.short_chars):
                continue
            # 含逗号、句号的长行多是正文句子（如“第6章重点介绍……，……”）；
            # 但版面标题或大字行不是（如英文长章名“Building Operations, Assistance to …”）
            if n > c.short_chars and ("," in w or "。" in w):
                body = body_ocr if l.from_ocr else body_text
                if not layout_title and not (body and l.font_size >= c.size_ratio_large * body):
                    continue
            # 一行里有两个章号的是篇首页导读/目录粘连行（“第13章 扫描算法第14章N-体问题”），不是标题
            if len(re.findall(r"第\s*[\d一二三四五六七八九十]+\s*章|chapter\s+\d+", w, re.I)) >= 2:
                continue

            score = 0.0
            if layout_title:
                score += c.w_layout_title
            if style:
                score += c.w_number_strong if strong else c.w_number
            body = body_ocr if l.from_ocr else body_text
            ratio = l.font_size / body if body > 0 and l.font_size > 0 else 1.0
            # 正文里单独成行的 “Chapter 4.”（正文字号、无章名）不是真章标题
            if (style == "chapter" and _EN_CHAPTER_BARE.match(w)
                    and not layout_title and ratio < c.size_ratio_small
                    and not (bold_useful and l.bold and ratio >= 0.95)):
                continue
            if ratio >= c.size_ratio_large:
                score += c.w_font_large
            elif ratio >= c.size_ratio_small:
                score += c.w_font_small
            if bold_useful and l.bold and not l.from_ocr and ratio >= 0.95:
                score += c.w_bold
            # 正文字号的长行即使以编号开头，也多半是折行的正文句子；
            # 但粗体的 12.2.2 / 3.4 等多级标题常只比正文大一点，不能丢
            if (not layout_title and ratio < c.size_ratio_small and n > c.short_chars
                    and not (l.bold and style in _DECIMAL_STYLES | {"chapter", "part", "appendix"})):
                continue
            # 标题常以问号、感叹号结尾；英文正文 “Chapter 4.” 的句号也视为句子结尾
            sentence_end = w[-1] in "。;," or (w[-1] == "." and _EN_CHAPTER_BARE.match(w))
            if n <= c.short_chars and not sentence_end:
                score += c.w_short

            # 惩罚项
            if sentence_end:
                score -= 0.5
            if w[-1] == ":":
                score -= 0.2
            if n > c.max_chars:
                score -= 1.0
            if w.count(",") >= 2:
                score -= 0.3
            if ratio < 0.9 and not layout_title:
                score -= 0.3
            if _CAPTION.search(w):
                score -= 0.6
            # 版面模型常把章首页的大号章标题判成 header，位于页面中部时不扣分
            if l.layout_type in NEGATIVE_TYPES and not (l.layout_type in ("header", "footer") and not edge(l)):
                score -= 0.6
            if l.page_height > 0 and (l.y1 < l.page_height * c.header_zone
                                      or l.y0 > l.page_height * (1 - c.footer_zone)):
                score -= 1.0
            if repeated_here(l, w0):
                score -= 1.0
            if score < c.min_score:
                continue

            # 归一化是逐字符映射，按长度截取原文即可保留全角标点
            title = _fix_appendix_ocr(collapse(l.text)[:n])
            # OCR 常把编号里的点识别成全角“．”
            title = re.sub(r"^[0-9０-９.．]+", lambda m: m.group(0).translate(_FULLWIDTH), title)
            # 目录里同编号的标题与识别结果相近时用目录的写法纠正 OCR 错字；目录条目折行被截断时不用
            clean = toc_titles.get(key)
            if clean:
                a, b = _title_text(normalize(title)), _title_text(normalize(clean))
                # 目录写法只是比标题少了几个字（目录页 OCR 漏字）时不用
                dropped = len(b) < len(a) and _is_subsequence(b, a)
                # 只多出数字/标点（目录页的页码残留、引导点粘成的 “.5.3”、标题后的冒号）时也不用
                dropped |= len(b) > len(a) and re.sub(r"[\d\W]", "", b) == re.sub(r"[\d\W]", "", a)
                # 标题去掉杂符号后是目录写法的一部分（标题漏字或识别出“√中”这类残缺文字）时用目录补全
                core = re.sub(r"[^\w]", "", a)
                partial = (bool(core) and len(core) < len(b) <= len(core) + max(2, len(core) // 2)
                           and _is_subsequence(core, b) and not dropped)
                if low_ocr or partial or (len(b) >= len(a) - 1 and not dropped
                                          and SequenceMatcher(None, a, b).ratio() >= 0.8):
                    title = clean
            if l.from_ocr:
                # 汉字与英文、数字之间的空格 OCR 时有时有，汉字之间的空格多是字距大（“简 介”），统一去掉
                title = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[A-Za-z0-9\u4e00-\u9fff])"
                               r"|(?<=[A-Za-z0-9])\s+(?=[\u4e00-\u9fff])", "", title)
            # 编号旁残片：先剥括号/装饰符，再剥 id、cid:)；字符类不含“.”以免吞掉 1.1.1 的点
            sep_sym = r"\s*[（(【\[「『〈《|丨·•●○◦▪■√✓※★☆◆◇]+\s*"
            sep = r"\s*[（(【\[「『〈《|丨·•●○◦▪■√✓※★☆◆◇]*\s*" if l.from_ocr else sep_sym
            mid_junk = (
                r"(?:"
                r"\s*[「『【\[〈《√✓※★☆◆◇•·|丨]+|"
                r"\s*[a-z]{1,4}[:;．.]*[)）\]】>]?\s+|"
                r"\s*[A-Za-z]{1,3}[:;．.]+[)）\]】>]+\s*"
                r")+"
            )
            if style in ("decimal2", "decimal3", "decimal4", "alpha2", "alpha3", "alpha4"):
                title = re.sub(r"^([A-Z\d]+(?:\.\d+)+)\.(?=\s|$)", r"\1", title)  # 小节号尾点：“1.1. …”
                title = re.sub(rf"^([A-Z\d]+(?:\.\d+)+){sep}(?:[A-Za-z]\s+(?=[\u4e00-\u9fff]))?", r"\1 ", title)
                title = re.sub(rf"^([A-Z\d]+(?:\.\d+)+){mid_junk}(?=\s*[\u4e00-\u9fffA-Z])", r"\1 ", title)
            elif style in ("chapter", "part"):
                title = re.sub(rf"^(第\s*{_CN}\s*(?:章|篇|部分|编)){sep}", r"\1 ", title)
            elif style == "appendix":
                title = re.sub(rf"^(附\s*录\s*(?:[A-Z](?![a-z])|[一二三四五六七八九十]+|\d+)?){sep}", r"\1 ", title)
            if l.from_ocr or style in _DECIMAL_STYLES | {"chapter", "part", "appendix"}:
                title = re.sub(r"(?:[.·…]{2,}|…)\s*\d+$", "", title)  # 带进来的目录引导线和页码
                title = _PAREN_PAGE.sub("", title)
                # 目录引导点与页码粘成的编号碎片（“…曲线 .5.3”），只剥紧跟汉字后的
                title = re.sub(r"^(.+?[\u4e00-\u9fff])\s*(?:\.\d+)+$", r"\1", title)
                title = re.sub(r"[\s.·…\-_—，,、：:；;]+$", "", title)
                title = re.sub(r"(?<=[\u4e00-\u9fff])\s*[OoＯ○]$", "", title)
                title = re.sub(r"(?<=[\u4e00-\u9fff])\s*[a-zA-Z]{1,2}[:;．.]*[)）\]】>]+\s*$", "", title)
                title = re.sub(r"(?<=[\u4e00-\u9fff])[a-zA-Z]{1,2}[:;．.]*[)）\]】>]+\s*$", "", title)
            if _looks_like_exercise(title, style):
                continue
            numbered = bool(style) and style != "special"
            acc.append((Heading(title, l.page, l.y0, 1, score, ratio, style), numbered, prio))

        acc.sort(key=lambda a: (a[0].page, a[0].y))

        # 去重：同页同位置的相同文本
        uniq = []
        for a in acc:
            h = a[0]
            if not any(u[0].page == h.page and abs(u[0].y - h.y) < 5 and u[0].title == h.title for u in uniq[-5:]):
                uniq.append(a)
        acc = uniq

        # 漏检的目录项：末尾带页码，且别的页上有同名标题，则它只是目录里的条目
        plain = defaultdict(set)
        for h, _, _ in acc:
            if self.toc_entry_base(normalize(h.title)) is None:
                plain[_compact(normalize(h.title))].add(h.page)
        kept = []
        for a in acc:
            base = self.toc_entry_base(normalize(a[0].title))
            if base is not None and plain.get(_compact(base), set()) - {a[0].page}:
                continue
            kept.append(a)
        acc = kept
        # “第一部分 小结”这类只是挂了编号的总结，不加书签
        acc = [a for a in acc if a[0].style not in ("chapter", "part")
               or not re.fullmatch(r"(本[章篇节])?(小结|总结)", _title_text(normalize(a[0].title)))]

        # 先去掉不属于所在章的编号，免得它们在下面的同编号去重中挤掉真正的小节标题
        acc = self._check_chapters(acc)

        # 同一编号（1.2、第3章）出现多次时，其余多为章首页小目录或目录残留：保留得分最高的，同分取靠后的
        best = {}
        for i, (h, _, _) in enumerate(acc):
            key = _number_key(h.title, h.style)
            if key and (key not in best or (h.score, h.page) >= (acc[best[key]][0].score, acc[best[key]][0].page)):
                best[key] = i
        acc = [a for i, a in enumerate(acc) if (k := _number_key(a[0].title, a[0].style)) is None or best[k] == i]

        acc = self._fix_eroded_dots(acc)
        acc = self._fix_numbers(acc)

        # 正文识别与上下文反推都缺失的标题，最后用目录清单反查（无目录则为空操作）
        extra = self._toc_lookup(acc, lines, norm, toc_titles, toc_printed, toc_pages, mini_toc,
                                 body_text, body_ocr)
        if extra:
            acc += extra
            acc.sort(key=lambda a: (a[0].page, a[0].y))

        # “1”“2”后面紧跟“1.1”“2.1”时是章号；否则是小节内的列表式标题，层级放到多级编号之后
        d1 = [i for i, a in enumerate(acc) if a[0].style == "decimal1"]
        if d1 and any(a[0].style in ("decimal2", "decimal3", "decimal4") for a in acc):
            votes = 0
            for i in d1:
                num = re.match(r"\d+", normalize(acc[i][0].title)).group(0)
                if i + 1 < len(acc) and _number_key(acc[i + 1][0].title, acc[i + 1][0].style) == f"{num}.1":
                    votes += 1
            if votes * 2 < len(d1):
                acc = [(h, num, 85 if h.style == "decimal1" else p) for h, num, p in acc]

        self._assign_levels(acc)
        out, stack = [], []
        for h, _, _ in acc:
            # 超过最大层级的（如 1.1.1 下面的“1.”“(1)”小列表）不加书签
            if h.level > c.max_level:
                continue
            # 按实际存在的上级定层级：上级（如漏识别的“第一部分”）缺失时整组上移，兄弟之间保持同级
            while stack and stack[-1] >= h.level:
                stack.pop()
            stack.append(h.level)
            h.level = len(stack)
            out.append(h)
        self._anchor_tops(out, lines, body_text, body_ocr)
        return out

    @staticmethod
    def _anchor_tops(headings: list[Heading], lines: list[TextLine], body_text: float, body_ocr: float) -> None:
        """书签定位到标题块顶部：仅章/篇/附录上方紧挨的大字装饰行（如“第⑩章”）上提。

        小节标题不做上提：OCR 常把正文行高估成大字号，上提会穿过整段正文，导致点击书签时
        标题落在视口中下部、上方空出一大截上节内容。
        """
        by_page = defaultdict(list)
        for l in lines:
            by_page[l.page].append(l)
        for h in headings:
            if h.style not in ("chapter", "part", "appendix"):
                continue
            top = h.y
            for l in sorted((l for l in by_page[h.page] if l.y1 <= h.y + 1), key=lambda l: -l.y1):
                body = body_ocr if l.from_ocr else body_text
                if body and not l.text.strip() and l.font_size < 0.6 * body:
                    continue  # 底纹噪点
                # 装饰行应很短；长文本是上节正文，到此为止
                if len((l.text or "").strip()) > 12:
                    break
                lh = l.y1 - l.y0
                if not body or l.font_size < 1.3 * body or top - l.y1 > 2.5 * lh:
                    break
                top = l.y0
            h.y = top

    @staticmethod
    def _fix_eroded_dots(acc) -> list:
        """消蚀标题中间的点常被吃掉：10.1.3 → 10.13。

        若前后条目都是同一父级下的三级编号（如 10.1.2 与 10.1.4），则把夹在中间的 10.13 改回 10.1.3；
        真正的二级节 10.13（夹在 10.12 与 10.14 之间）保持不变。
        """
        keys = [_number_key(h.title, h.style) for h, _, _ in acc]
        out = []
        for i, (h, numbered, prio) in enumerate(acc):
            k = keys[i]
            m = re.fullmatch(r"(\d+)\.(\d{2})", k or "")
            if m and h.style in ("decimal2", "decimal3", "decimal4"):
                a, bc = m.group(1), m.group(2)
                mid, last = bc[0], bc[1:]
                alt, prefix = f"{a}.{mid}.{last}", f"{a}.{mid}."
                prev = next((keys[j] for j in range(i - 1, -1, -1) if keys[j]), "")
                nxt = next((keys[j] for j in range(i + 1, len(keys)) if keys[j]), "")
                if prev.startswith(prefix) and nxt.startswith(prefix):
                    rest = re.sub(r"^\d+(?:\.\d+)+\.?\s*", "", normalize(h.title))
                    h = replace(h, title=f"{alt} {rest}".strip(), style="decimal3")
                    keys[i] = alt
            out.append((h, numbered, prio))
        return out

    @staticmethod
    def _fix_numbers(acc) -> list:
        """按前一个兄弟（或父标题）推出应有的编号，纠正 OCR 的编号错误：

        - 多出一位数字：2.1.1 识别成 2.1.11；
        - 编号与后面的数字粘连（丢了空格）：“2.1 8086 CPU”识别成“2.18086 CPU”，按应有编号拆开；
          末段 3 位以上又拆不出应有编号的（如“2. 8086 存储器”读成“2.8086”）不是小节号，丢弃。
        """
        decimal = ("decimal2", "decimal3", "decimal4")
        keys = []
        for h, _, _ in acc:
            k = _number_key(h.title, h.style)
            if h.style in decimal:
                keys.append(k)
            elif h.style == "chapter" and k and k.startswith("章"):
                keys.append(k[1:])  # 第2章 是 2.1 的父标题
            else:
                keys.append(None)
        drop = set()
        for i, k in enumerate(keys):
            if not k or acc[i][0].style not in decimal:
                continue
            prefix, last = k.rsplit(".", 1)
            if len(last) < 2:
                continue
            prev = None
            for j in range(i - 1, -1, -1):
                kj = keys[j]
                if j in drop or not kj:
                    continue
                if kj == prefix:
                    prev = 0
                elif kj.count(".") == k.count(".") and kj.rsplit(".", 1)[0] == prefix:
                    prev = int(kj.rsplit(".", 1)[1])
                else:
                    continue
                break
            expect = None if prev is None else str(prev + 1)
            h = acc[i][0]
            if len(last) >= 3:
                if expect is None:
                    expect = last[0]
                if not last.startswith(expect):
                    drop.add(i)
                    continue
                fixed = f"{prefix}.{expect}"
                h.title = re.sub(rf"^\s*{re.escape(k)}\s*", f"{fixed} {last[len(expect):]}", h.title, count=1)
                keys[i] = fixed
            elif (expect and last != expect and last[:-1] == expect
                  and last[-1] == expect[-1]):  # 末位重复：2.1.11→2.1.1；勿把真·2.10 收成 2.1
                fixed = f"{prefix}.{expect}"
                h.title = h.title.replace(k, fixed, 1)
                keys[i] = fixed
        return [a for i, a in enumerate(acc) if i not in drop]

    @staticmethod
    def _check_chapters(acc) -> list:
        """有“第X章”时，多级编号的第一段应等于当前章号：第2章里的“1.8086 CPU”（实为“1. 8086”列表项）、
        第14章里的公式“5.2 ×100”都不是小节标题。章标题漏检时，遇到下一章的“X.1”视为进入新章。"""
        if sum(h.style == "chapter" for h, _, _ in acc) < 2:
            return acc
        out, cur = [], None
        for a in acc:
            h = a[0]
            k = _number_key(h.title, h.style)
            if h.style == "chapter":
                cur = int(k[1:]) if k and k.startswith("章") else None
            elif h.style == "appendix":
                cur = None
            elif h.style in ("decimal2", "decimal3", "decimal4") and cur is not None and k:
                parts = k.split(".")
                first = int(parts[0])
                if first == cur + 1 and all(p == "1" for p in parts[1:]):
                    cur = first
                elif first != cur:
                    continue
            out.append(a)
        return out

    def _toc_lookup(self, acc, lines, norm, toc_titles, toc_printed, toc_pages, mini_toc,
                    body_text, body_ocr) -> list:
        """目录反查（兜底）：目录里有、正文没认出的编号标题，拿目录文字去正文找大字行。

        印刷页号与 PDF 页的偏移先用已认出的条目校准，只用来圈搜索范围；
        落点以在正文找到的那一行为准，找不到就不加（不按页号硬造书签）。
        无目录（toc_titles 为空）时直接返回空，不影响原流程。
        """
        have = {k for h, _, _ in acc if (k := _number_key(h.title, h.style))}
        missing = [(k, t) for k, t in toc_titles.items() if k not in have]
        if not missing:
            return []
        offs = Counter()
        for h, _, _ in acc:
            k = _number_key(h.title, h.style)
            if k and k in toc_printed:
                offs[h.page + 1 - toc_printed[k]] += 1
        offset = offs.most_common(1)[0][0] if offs and offs.most_common(1)[0][1] >= 2 else None
        used = [(h.page, h.y) for h, _, _ in acc]
        added = []
        for key, toc_title in missing:
            style, prio, _ = self.classify(toc_title)
            want = _title_text(normalize(toc_title))
            if not style or not want:
                continue
            center = toc_printed[key] + offset - 1 if offset is not None and key in toc_printed else None
            best_l, best_r = None, 0.0
            for idx, (l, w) in enumerate(zip(lines, norm)):
                if l.page in toc_pages or idx in mini_toc:
                    continue
                if center is not None and abs(l.page - center) > 4:
                    continue
                body = body_ocr if l.from_ocr else body_text
                if not body or l.font_size < 1.3 * body:
                    continue
                # 候选行若带别的编号，不能张冠李戴
                okey = _number_key(w, self.classify(w)[0])
                if okey and okey != key:
                    continue
                got = _title_text(w)
                if not got:
                    continue
                if got == want:
                    r = 1.0
                elif min(len(got), len(want)) >= 3:
                    r = SequenceMatcher(None, got, want).ratio()
                else:
                    continue
                if r >= 0.72 and r > best_r:
                    best_l, best_r = l, r
            if best_l is not None and not any(best_l.page == p and abs(best_l.y0 - y) < 5 for p, y in used):
                body = body_ocr if best_l.from_ocr else body_text
                # 标题文字优先用正文大字行（目录小字易丢字、读错），只从目录借编号前缀
                title = toc_title
                if _number_key(best_l.text, self.classify(best_l.text)[0]) == key:
                    title = collapse(best_l.text)
                else:
                    pm = _NUM_PREFIX.match(normalize(toc_title))
                    if pm:
                        title = f"{pm.group(0)} {collapse(best_l.text)}".strip()
                added.append((Heading(title, best_l.page, best_l.y0, 1, 1.0,
                                       best_l.font_size / body if body else 1.0, style), True, prio))
                used.append((best_l.page, best_l.y0))
        return added

    def _find_mini_toc(self, lines: list[TextLine], norm: list[str]) -> set[int]:
        """章首页小目录：同页连续 ≥3 行同类多级编号 / 章号，行距紧凑，中间没有正文。"""
        order = sorted(range(len(lines)), key=lambda i: (lines[i].page, lines[i].y0))
        out, run = set(), []

        def flush():
            styles = Counter(s for _, s in run if s)
            if len(run) >= 3 and styles and styles.most_common(1)[0][1] >= 2:
                out.update(i for i, _ in run)
            run.clear()

        for i in order:
            l, w = lines[i], norm[i]
            style = self.classify(w)[0]
            # OCR 识别错的条目（如“2.3 3小结”）匹配不上样式，但仍算小目录的一行
            # 章/篇/附录标题不算小目录条目：章名常紧贴小目录上方，连成一串会把章标题一起吞掉
            ok = (style in ("decimal2", "decimal3", "decimal4") or bool(re.match(r"\d+\.\d+", w))) \
                and len(w) <= self.cfg.max_chars
            if style not in ("decimal2", "decimal3", "decimal4"):
                style = ""
            if ok and run:
                p = lines[run[-1][0]]
                h = max(p.y1 - p.y0, l.y1 - l.y0, 1.0)
                ok_gap = p.page == l.page and l.y0 - p.y0 < 2.5 * h
                if not ok_gap:
                    flush()
            if ok:
                run.append((i, style))
            else:
                flush()
        flush()
        return out

    def _find_toc_pages(self, lines: list[TextLine], norm: list[str]) -> set[int]:
        """目录页：大量行以页码结尾（或页码单独成行），或页面上有“目录”标题且带页码行。"""
        per_page, entries, titled = Counter(), Counter(), set()
        numbers = Counter()
        for l, w in zip(lines, norm):
            per_page[l.page] += 1
            if _PURE_NUMBER.match(w):
                numbers[l.page] += 1
            elif self.toc_entry_base(w) is not None:
                entries[l.page] += 1
            if _compact(w) in _TOC_TITLES:
                titled.add(l.page)

        def strength(p):
            # 页码单独成行时，正常页面也有 1 个页脚页码
            return entries[p] + max(0, numbers[p] - 1)

        toc = set()
        for p in sorted(per_page):
            s, n = strength(p), per_page[p]
            if (s >= 3 and s * 10 >= n * 3) or (p in titled and s >= 2):
                toc.add(p)
            elif p - 1 in toc and s >= 2 and s * 10 >= n * 2:
                toc.add(p)  # 目录的续页
        return toc

    def _assign_levels(self, acc) -> None:
        c = self.cfg
        # 1) 无编号标题按相对字号聚类
        ratios = sorted((h.font_ratio for h, num, _ in acc if not num and h.style != "special"), reverse=True)
        heads = [r for i, r in enumerate(ratios) if i == 0 or ratios[i - 1] - r > 0.08]

        def cluster(r):
            idx = 0
            for k, head in enumerate(heads):
                if r <= head + 1e-4:
                    idx = k
            return idx

        for h, num, _ in acc:
            if not num and h.style != "special":
                h.style = f"font#{cluster(h.font_ratio)}"

        # 2) 统计各样式；附录与章同级，附录小节 A.1 / A.1.1 与 1.1 / 1.1.1 同级
        # 没有“第X章/Chapter”而以“1 Xxx”作章名时，附录与“1 Xxx”同级
        chapter_key = "chapter"
        if not any(h.style == "chapter" for h, _, _ in acc) and any(
                h.style == "decimal1" and p == 55 for h, _, p in acc):
            chapter_key = "decimal1"

        def skey(style):
            return chapter_key if style == "appendix" else style.replace("alpha", "decimal")

        styles = {}
        for h, num, prio in acc:
            if h.style == "special":
                continue
            s = styles.setdefault(skey(h.style), {"key": skey(h.style), "numbered": num, "priority": prio,
                                            "sum": 0.0, "count": 0, "first_page": h.page})
            s["sum"] += h.font_ratio
            s["count"] += 1
        for s in styles.values():
            s["avg"] = s["sum"] / s["count"]
        numbered = sorted((s for s in styles.values() if s["numbered"]), key=lambda s: s["priority"])
        fonts = sorted((s for s in styles.values() if not s["numbered"]), key=lambda s: -s["avg"])

        # 首页只出现一次的最大字号文本视为文档标题，与一级标题并列
        doc_title = None
        if fonts and fonts[0]["count"] == 1 and fonts[0]["first_page"] <= 1 and (
            not numbered or fonts[0]["avg"] > numbered[0]["avg"] + 0.05
        ):
            doc_title = fonts.pop(0)["key"]

        # 3) 字号簇插到第一个字号明显更小的编号样式之前
        order, start = list(numbered), 0
        for f in fonts:
            j = start
            while j < len(order) and not (order[j]["numbered"] and order[j]["avg"] < f["avg"] - 0.05):
                j += 1
            order.insert(j, f)
            start = j + 1
        level_of = {s["key"]: i + 1 for i, s in enumerate(order)}

        for h, _, _ in acc:
            h.level = 1 if h.style in ("special", doc_title) else level_of.get(skey(h.style), 1)
