import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pdf_bookmark import RuleConfig, RuleEngine, TextLine  # noqa: E402

BODY = "这是正文内容，用于模拟论文中的普通段落文字，长度较长并且以句号结尾。"
CONFIG = os.path.join(os.path.dirname(__file__), "..", "config", "rules.ini")


def line(page, y, text, size, bold=False, layout="", ocr=False):
    return TextLine(text=text, page=page, x0=72, y0=y, x1=500, y1=y + size * 1.2, page_width=595,
                    page_height=842, font_size=size, bold=bold, layout_type=layout, from_ocr=ocr)


class I18nTest(unittest.TestCase):
    def test_lang_switch(self):
        from pdf_bookmark.i18n import get_lang, set_lang, t
        set_lang("zh")
        self.assertIn("书签", t("prog_desc"))
        set_lang("en")
        self.assertIn("bookmark", t("prog_desc").lower())
        self.assertEqual(get_lang(), "en")
        set_lang("zh")


class RuleEngineTest(unittest.TestCase):
    def setUp(self):
        self.eng = RuleEngine()

    def test_numbering(self):
        cases = {
            "第一章 绪论": "chapter", "第 3 章 实验": "chapter", "第二节 方法": "section",
            "Chapter 2 Methods": "chapter", "一、总体要求": "cn_num", "（二）工作目标": "cn_paren",
            "1.1 研究背景": "decimal2", "1.1.1 国内现状": "decimal3", "2.3.4.1 细节": "decimal4",
            "1. 引言": "decimal1", "3 系统设计": "decimal1", "（1）数据来源": "arabic_paren",
            "参考文献": "special", "致 谢": "special", "附录A 公式推导": "appendix", "附录ACDN试验床实施指南": "appendix", "附录": "appendix", "Appendix B": "appendix", "A.1 推导": "alpha2",
            "2019 年度报告": "", "普通文字": "",
        }
        for text, style in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.eng.classify(text)[0], style)

    @staticmethod
    def thesis_lines():
        v = [line(0, 200, "基于深度学习的文档结构分析研究", 22, True)]
        v += [line(0, 300 + i * 16, BODY, 10.5) for i in range(3)]
        v += [line(1, 80, "目录", 16, True), line(1, 120, "第一章 绪论........1", 10.5),
              line(1, 140, "1.1 研究背景........2", 10.5), line(1, 160, "1.2 研究内容........3", 10.5),
              line(1, 180, "第二章 方法........5", 10.5)]
        for p in range(2, 8):
            v += [line(p, 20, "某某大学硕士学位论文", 9), line(p, 810, str(p - 1), 9)]
        def body(p, y, n):
            return [line(p, y + i * 16, BODY, 10.5) for i in range(n)]
        v += [line(2, 80, "第一章 绪论", 16, True)] + body(2, 120, 5)
        v += [line(2, 230, "1.1 研究背景", 14, True)] + body(2, 260, 5)
        v += [line(3, 80, "1.1.1 国内研究现状", 12, True)] + body(3, 110, 5)
        v += [line(3, 300, "1. 首先需要说明的是数据的来源。", 10.5), line(3, 330, "图1-1 系统框架", 10.5)]
        v += [line(4, 80, "1.2 研究内容", 14, True)] + body(4, 110, 8)
        v += [line(5, 80, "第二章 方法", 16, True)] + body(5, 110, 8)
        v += [line(6, 80, "2.1 模型结构", 14, True)] + body(6, 110, 8)
        v += [line(7, 80, "参考文献", 16, True)] + body(7, 110, 8)
        return v

    def test_thesis_numbered_only(self):
        got = [(h.title, h.level) for h in self.eng.detect(self.thesis_lines(), 8)]
        self.assertEqual(got, [
            ("第一章 绪论", 1), ("1.1 研究背景", 2), ("1.1.1 国内研究现状", 3), ("1.2 研究内容", 2),
            ("第二章 方法", 1), ("2.1 模型结构", 2),
        ])

    def test_thesis_with_unnumbered(self):
        eng = RuleEngine(RuleConfig(numbered_only=False))
        got = [(h.title, h.level) for h in eng.detect(self.thesis_lines(), 8)]
        self.assertEqual(got, [
            ("基于深度学习的文档结构分析研究", 1), ("目录", 1), ("第一章 绪论", 1), ("1.1 研究背景", 2),
            ("1.1.1 国内研究现状", 3), ("1.2 研究内容", 2), ("第二章 方法", 1), ("2.1 模型结构", 2),
            ("参考文献", 1),
        ])

    def test_book_with_plain_toc(self):
        """目录项为 “标题 页码” / “标题页码” / 页码单独成行；正文有 阅读建议、总结、代码行。"""
        v = []
        # 第 8~10 页：目录（无引导点）
        toc = [
            ["目录", "第1章预备知识", "8", "1.1工具介绍 012", "1.2 环境搭建 015", "阅读建议 020", "总结 021",
             "第2章基础语法", "22", "2.1 变量与类型 025", "2.10 代码陷阱类型转换 059", "总结 060"],
            ["第3章函数原理", "61", "3.1 函数调用 062", "阅读建议 064", "总结 070",
             "第4章C++特性", "71", "4.1 引用 075", "阅读建议 108", "总结 110"],
            ["第5章高级编程", "111", "5.4 CPU眼的volatile 164", "总结 170",
             "第6章面试挑战", "171", "6.2 数组和双重指针210", "总结 220"],
        ]
        for i, items in enumerate(toc):
            v += [line(7 + i, 80 + k * 20, t, 16 if t == "目录" else 10.5) for k, t in enumerate(items)]
        # 第 12 页起：正文
        code = ["int main() {", "    int a = 10;", "    return 0", "}"]
        chapters = [("第1章预备知识", ["1.1工具介绍", "1.2 环境搭建"]),
                    ("第2章基础语法", ["2.1 变量与类型", "2.10 代码陷阱类型转换"])]
        p = 11
        for chap, secs in chapters:
            v += [line(p, 80, chap, 20, True), line(p, 130, "阅读建议", 14, True)]
            v += [line(p, 160 + k * 16, BODY, 10.5) for k in range(5)]
            v += [line(p, 820, str(p + 1), 9)]
            p += 1
            for s in secs:
                v += [line(p, 80, s, 14, True)] + [line(p, 110 + k * 16, BODY, 10.5) for k in range(4)]
                v += [line(p, 200 + k * 14, c, 9.5) for k, c in enumerate(code)]
                v += [line(p, 820, str(p + 1), 9)]
                p += 1
            v += [line(p, 80, "总结", 14, True)] + [line(p, 110 + k * 16, BODY, 10.5) for k in range(4)]
            v += [line(p, 820, str(p + 1), 9)]
            p += 1

        got = [(h.title, h.page + 1, h.level) for h in self.eng.detect(v, p)]
        self.assertEqual(got, [
            ("第1章预备知识", 12, 1), ("1.1工具介绍", 13, 2), ("1.2 环境搭建", 14, 2),
            ("第2章基础语法", 16, 1), ("2.1 变量与类型", 17, 2), ("2.10 代码陷阱类型转换", 18, 2),
        ])

    def test_running_header_and_chapter_mini_toc(self):
        """章首页有本章小目录（无页码），每页顶部有“第1章 引言”页眉。"""
        v = [line(0, 200, "第1章 引言", 24, layout="doc_title", ocr=True)]
        v += [line(0, 500 + k * 20, t, 9, layout="text", ocr=True)
              for k, t in enumerate(["1.1 CDN的基本概念和产生背景", "1.2 CDN的基本工作过程", "1.3 3CDN的发展历史"])]
        secs = {2: "1.1 CDN的基本概念和产生背景", 4: "1.2 CDN的基本工作过程", 6: "1.3 CDN的发展历史"}
        for p in range(1, 8):
            v.append(line(p, 30, "第1章 引言", 10, ocr=True))
            if p in secs:
                v.append(line(p, 300, secs[p], 16, layout="paragraph_title", ocr=True))
            v += [line(p, 100 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(6)]
            v.append(line(p, 810, str(p + 1), 9, ocr=True))
        got = [(h.title, h.page + 1, h.level) for h in self.eng.detect(v, 8)]
        self.assertEqual(got, [("第1章 引言", 1, 1), ("1.1 CDN的基本概念和产生背景", 3, 2),
                               ("1.2 CDN的基本工作过程", 5, 2), ("1.3 CDN的发展历史", 7, 2)])

    def test_running_header_with_page_number_and_size_jitter(self):
        """页眉“第5章 向量运算 62”逐页字号抖动，某一页读大一号也不能当成章标题。"""
        v = [line(0, 200, "第5章 向量运算", 24, layout="doc_title", ocr=True)]
        v += [line(0, 300 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(5)]
        for p, fs in enumerate([10, 10, 17, 10], start=1):
            v.append(line(p, 30, f"第5章 向量运算 {60 + p}", fs, ocr=True))
            if p == 2:
                v.append(line(p, 300, "5.1 线性代数与几何", 16, layout="paragraph_title", ocr=True))
            v += [line(p, 100 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(5)]
        got = [(h.title, h.level) for h in self.eng.detect(v, 5)]
        self.assertEqual(got, [("第5章 向量运算", 1), ("5.1 线性代数与几何", 2)])

    def test_chapter_title_above_mini_toc_not_swallowed(self):
        """章名紧贴章首页小目录上方时，章名不能被当成小目录条目吞掉。"""
        v = [line(0, 130, "第3章 三维物体的表示和建模（2）", 17, layout="paragraph_title", ocr=True)]
        v += [line(0, 170 + k * 16, t, 9, layout="text", ocr=True)
              for k, t in enumerate(["3.1 Bézier曲线", "3.2 B样条表示", "3.3 有理曲线"])]
        v += [line(0, 240 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(3)]
        v.append(line(0, 330, "3.1 Bézier曲线", 11, layout="paragraph_title", ocr=True))
        v += [line(0, 360 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(3)]
        got = [(h.title, h.level) for h in self.eng.detect(v, 1)]
        self.assertEqual(got, [("第3章 三维物体的表示和建模（2）", 1), ("3.1 Bézier曲线", 2)])

    def test_display_chapter_3d_title_and_promoted_missing_chapter(self):
        """大数字章名可接“3D…”章名；章号行丢失时按第一个 N.1 上方的大字标题补章。"""
        v = [line(0, 100, "6", 60, ocr=True), line(0, 190, "3D向量类", 32, layout="paragraph_title", ocr=True)]
        v += [line(0, 300 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(4)]
        v.append(line(0, 420, "6.1 类接口", 14, layout="paragraph_title", ocr=True))
        v += [line(1, 100, "7 矩阵", 20, layout="paragraph_title", ocr=True)]
        v += [line(1, 160 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(4)]
        v.append(line(1, 300, "7.1 矩阵的定义", 14, layout="paragraph_title", ocr=True))
        v += [line(2, 200, "矩阵和线性变换", 32, layout="paragraph_title", ocr=True)]
        v += [line(2, 300 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(4)]
        v.append(line(2, 500, "8.1 变换物体与变换坐标系", 14, layout="paragraph_title", ocr=True))
        v += [line(2, 560 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(3)]
        got = [(h.title, h.level) for h in self.eng.detect(v, 3)]
        self.assertIn(("6 3D向量类", 1), got)
        self.assertIn(("8矩阵和线性变换", 1), got)  # OCR 清理会去掉数字与汉字间的空格，与“7矩阵”一致
        self.assertIn(("6.1 类接口", 2), got)
        self.assertIn(("8.1 变换物体与变换坐标系", 2), got)

    def test_missing_chapter_title_only_in_running_header(self):
        """章标题只出现在页眉里时，在第一个小节处合成章标题（页眉本身不进书签）；
        目录页上的同名条目不能抢先占掉章号。"""
        v = [line(0, 100, "目录", 16, True),
             line(0, 140, "3.1 变换物体 45", 10.5, ocr=True),
             line(0, 160, "3.2 旋转 48", 10.5, ocr=True)]
        v += [line(0, 200 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(3)]
        for p, chap in enumerate(["第1章 基础", "第2章 进阶"], start=1):
            v.append(line(p * 2, 200, chap, 24, layout="doc_title", ocr=True))
            v += [line(p * 2, 300 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(3)]
            v.append(line(p * 2 + 1, 300, f"{p}.1 小节", 14, layout="paragraph_title", ocr=True))
            v += [line(p * 2 + 1, 340 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(3)]
        # 第3章：每页顶部只有页眉，正文从 3.1 开始
        for p in (6, 7, 8):
            v.append(line(p, 30, "第3章 线性变换 45", 10, ocr=True))
            if p == 6:
                v.append(line(p, 300, "3.1 变换物体", 14, layout="paragraph_title", ocr=True))
            v += [line(p, 100 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(4)]
        got = [(h.title, h.level, h.page + 1) for h in self.eng.detect(v, 9)]
        self.assertIn(("第3章 线性变换", 1, 7), got)
        self.assertIn(("3.1 变换物体", 2, 7), got)
        self.assertTrue(all("45" not in title for title, _, _ in got))

    def test_missing_chapter_toc_trigger_prev_page_and_header_claim(self):
        """目录页的多章条目不能抢先触发补章；章名在上一页时跨页补章；
        带页码的页眉哪怕字号偏大也不能冒充已有章标题（否则该章永远补不上）。"""
        v = [line(0, 100, "目录", 16, True),
             line(0, 140, "2.1 ID数学..", 10.5, ocr=True),
             line(0, 160, "8.1 变换物体与变换坐标系 .5", 10.5, ocr=True),
             line(0, 180, "15.1 图形管道概述. .320", 10.5, ocr=True)]
        v += [line(0, 240 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(3)]
        # 第1章正常（数字式章标题）
        v.append(line(1, 120, "1 简介", 31, layout="paragraph_title", ocr=True))
        v.append(line(1, 300, "1.1 背景", 14, layout="paragraph_title", ocr=True))
        v += [line(1, 340 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(3)]
        # 第2章：章名在章首页，2.1 在下一页
        v.append(line(2, 200, "笛卡尔坐标系统", 30, layout="paragraph_title", ocr=True))
        v += [line(2, 300 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(4)]
        v.append(line(3, 300, "2.1 1D数学", 14, layout="paragraph_title", ocr=True))
        v += [line(3, 340 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(3)]
        # 大字公式行“2 =(-3)(5)…”不能冒充已有第2章（否则第2章永远补不上）
        v.append(line(3, 500, "2 =(-3)(5)-(4)(2)=-23", 30, layout="text", ocr=True))
        # 第15章：章名只在带页码的页眉里（字号 17，OCR 正文 10.5 时会被误判为大字）
        for p in (4, 5, 6):
            v.append(line(p, 30, f"第15章 图形数学 {339 + p}", 17, ocr=True))
            if p == 4:
                v.append(line(p, 300, "15.1 图形管道概述", 14, layout="paragraph_title", ocr=True))
            v += [line(p, 100 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(4)]
        got = [(h.title, h.level, h.page + 1) for h in self.eng.detect(v, 7)]
        # OCR 清理会去掉数字与汉字间的空格
        self.assertIn(("1简介", 1, 2), got)
        self.assertIn(("2笛卡尔坐标系统", 1, 3), got)
        self.assertIn(("2.1 1D数学", 2, 4), got)
        self.assertIn(("15图形数学", 1, 5), got)
        self.assertTrue(all(page != 1 for _, _, page in got))  # 目录页不产生书签
        self.assertTrue(all("339" not in t and "34" not in t for t, _, _ in got))

    def test_appendix_promoted_from_alpha_sections(self):
        """附录装饰字母（如卷轴图里的“A”）没读成标题：无编号大字附录名靠第一个 A.1 反推，
        不能让 A.1、A.2 挂到最后一章下面。"""
        v = [line(0, 200, "第1章 绪论", 24, layout="doc_title", ocr=True)]
        v += [line(0, 300 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(3)]
        v.append(line(1, 300, "1.1 背景", 14, layout="paragraph_title", ocr=True))
        v += [line(1, 340 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(3)]
        v.append(line(2, 120, "A", 60, ocr=True))  # 装饰字母单独成行，拼不成附录标题
        v.append(line(2, 300, "简单的数学概念", 30, layout="paragraph_title", ocr=True))
        v += [line(2, 380 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(3)]
        v.append(line(2, 520, "A.1 求和记法", 16, layout="paragraph_title", ocr=True))
        v.append(line(3, 300, "A.2 三角函数", 16, layout="paragraph_title", ocr=True))
        v += [line(3, 340 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(3)]
        got = [(h.title, h.level) for h in self.eng.detect(v, 4)]
        self.assertIn(("附录A 简单的数学概念", 1), got)
        self.assertIn(("A.1 求和记法", 2), got)
        self.assertIn(("A.2 三角函数", 2), got)

    def test_dirty_toc_entry_not_adopted(self):
        """目录条目被 OCR 读脏（引导点与页码粘成“.5.3”、标题后带冒号）时，
        目录纠错不能把脏文本搬到本来干净的正文标题上。"""
        v = [line(0, 100, "目录", 16, True),
             line(0, 140, "2.6 总结 : …48", 10.5, ocr=True),
             line(0, 160, "3.1 Bézier曲线 .5.3", 10.5, ocr=True),
             line(0, 180, "3.2 B样条表示 …59", 10.5, ocr=True),
             line(0, 200, "4.1 向量 …70", 10.5, ocr=True)]
        v += [line(0, 260 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(3)]
        t26 = line(1, 300, "2.6总结", 16, layout="paragraph_title", ocr=True)
        t26.ocr_score = 0.5  # 低置信行才会走目录纠错
        t31 = line(2, 300, "3.1Bézier曲线", 16, layout="paragraph_title", ocr=True)
        t31.ocr_score = 0.5
        v += [t26] + [line(1, 340 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(3)]
        v += [t31] + [line(2, 340 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(3)]
        got = [h.title.replace(" ", "") for h in self.eng.detect(v, 3)]
        self.assertIn("2.6总结", got)
        self.assertIn("3.1Bézier曲线", got)

    def test_toc_lookup_missing_chapter(self):
        """目录反查兜底：正文只有无编号大字章名、又无编号小节可反推时，按目录清单反查。
        印刷页号与 PDF 页有偏移，只能圈搜索范围；正文没有大字行的小节不按页号硬造。"""
        v = [line(0, 100, "目录", 16, True),
             line(0, 140, "第1章 绪论 …1", 10.5, ocr=True),
             line(0, 160, "1.1 背景 …1", 10.5, ocr=True),
             line(0, 180, "第2章 深层结构 …3", 10.5, ocr=True),
             line(0, 200, "2.1 细节 …4", 10.5, ocr=True)]
        v += [line(0, 260 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(3)]
        # 第1章在 PDF 第3页（印刷第1页，偏移 +2）
        v.append(line(2, 200, "第1章 绪论", 24, layout="doc_title", ocr=True))
        v.append(line(2, 300, "1.1 背景", 14, layout="paragraph_title", ocr=True))
        v += [line(2, 340 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(3)]
        # 第2章在 PDF 第5页：只有无编号大字章名，章内也没有数字编号小节
        v.append(line(4, 200, "深层结构", 30, layout="paragraph_title", ocr=True))
        v += [line(4, 300 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(4)]
        got = [(h.title, h.level, h.page + 1) for h in self.eng.detect(v, 5)]
        self.assertIn(("第1章 绪论", 1, 3), got)
        self.assertIn(("第2章 深层结构", 1, 5), got)  # 落点是正文大字行所在的 PDF 第5页，不是印刷页3
        self.assertTrue(all("细节" not in t for t, _, _ in got))  # 正文无此标题行，不硬造

    def test_toc_lookup_prefers_body_text_over_toc_typo(self):
        """目录反查命中时，标题文字用正文大字行的（目录小字把“手册”读成“册”也不能带过来），
        只向目录借编号；附录名与 A.1 不在同页、上一页也没有时，小节反推够不到，只能靠反查。"""
        v = [line(0, 100, "目录", 16, True),
             line(0, 140, "第1章 绪论 …1", 10.5, ocr=True),
             line(0, 160, "1.1 背景 …1", 10.5, ocr=True),
             line(0, 180, "附录A CUDA专家册库 …4", 10.5, ocr=True)]
        v += [line(0, 260 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(3)]
        v.append(line(2, 200, "第1章 绪论", 24, layout="doc_title", ocr=True))
        v.append(line(2, 300, "1.1 背景", 14, layout="paragraph_title", ocr=True))
        v += [line(2, 340 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(3)]
        v.append(line(4, 200, "CUDA专家手册库", 24, layout="paragraph_title", ocr=True))
        v += [line(4, 300 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(3)]
        v.append(line(6, 300, "A.1 计时操作", 14, layout="paragraph_title", ocr=True))
        v.append(line(6, 360, "A.2 错误处理", 14, layout="paragraph_title", ocr=True))
        v += [line(6, 400 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(3)]
        got = [(h.title.replace(" ", ""), h.level, h.page + 1) for h in self.eng.detect(v, 7)]
        self.assertIn(("附录ACUDA专家手册库", 1, 5), got)

    def test_part_bare_number_merges_title_below(self):
        """篇首页只有“第二部分72”（尾数是印刷页码），篇名是下方的大字行，应合并为完整篇标题。"""
        v = [line(0, 180, "第二部分72", 14, ocr=True),
             line(0, 195, "", 6, ocr=True), line(0, 200, "", 6, ocr=True),
             line(0, 205, "", 6, ocr=True), line(0, 210, "", 6, ocr=True),
             line(0, 220, "CUDA编程", 24, layout="paragraph_title", ocr=True)]
        v += [line(0, 300 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(4)]
        v.append(line(1, 300, "第5章 内存", 20, layout="paragraph_title", ocr=True))
        v += [line(1, 340 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(3)]
        v.append(line(2, 300, "5.1 主机内存", 14, layout="paragraph_title", ocr=True))
        v += [line(2, 340 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(3)]
        got = [(h.title.replace(" ", ""), h.level) for h in self.eng.detect(v, 3)]
        self.assertIn(("第二部分CUDA编程", 1), got)
        self.assertIn(("第5章内存", 2), got)

    def test_bilingual_chapter_line_and_single_char_decoration(self):
        """双语章号同行只留中文号；章首页的单字装饰大字不能被反推当作章名。"""
        v = [line(0, 150, "第9章 Chapter 9多GPU", 16, layout="paragraph_title", ocr=True)]
        v += [line(0, 200 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(3)]
        v.append(line(0, 330, "9.1 概述", 14, layout="paragraph_title", ocr=True))
        v.append(line(1, 80, "国", 56, ocr=True))  # 装饰单字
        v.append(line(1, 160, "Chytern 第11章", 11, ocr=True))
        v.append(line(1, 190, "流式负载", 18, layout="paragraph_title", ocr=True))
        v += [line(1, 240 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(3)]
        v.append(line(2, 300, "11.1 设备内存", 14, layout="paragraph_title", ocr=True))
        v += [line(2, 340 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(3)]
        got = [(h.title.replace(" ", ""), h.level) for h in self.eng.detect(v, 3)]
        self.assertIn(("第9章多GPU", 1), got)
        self.assertIn(("第11章流式负载", 1), got)
        self.assertTrue(all(not t.endswith("国") or "第11章" not in t for t, _ in got))

    def test_two_chapter_numbers_in_one_line_dropped(self):
        """一行里有两个章号的是篇首页导读粘连行（“第13章 …第14章 …”），不能当章标题。"""
        v = [line(0, 200, "第13章 扫描算法第14章N-体问题", 16, layout="paragraph_title", ocr=True)]
        v += [line(0, 300 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(3)]
        v.append(line(1, 200, "第13章 扫描算法", 20, layout="paragraph_title", ocr=True))
        v.append(line(1, 300, "13.1 冒泡扫描", 14, layout="paragraph_title", ocr=True))
        v.append(line(2, 200, "第14章 N-体问题", 20, layout="paragraph_title", ocr=True))
        v.append(line(2, 300, "14.1 引力计算", 14, layout="paragraph_title", ocr=True))
        v += [line(2, 340 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(3)]
        got = [(h.title.replace(" ", ""), h.level) for h in self.eng.detect(v, 3)]
        self.assertIn(("第13章扫描算法", 1), got)
        self.assertIn(("第14章N-体问题", 1), got)
        self.assertTrue(all("扫描算法第14章" not in t for t, _ in got))

    def test_exercises_do_not_swallow_next_chapter(self):
        """数字式章标题的英文书：章末 Exercises 段只在本章内有效，
        下一章（数字式章标题，不是 chapter 样式）的小节不能被当练习题吞掉。"""
        v = [line(0, 200, "2 Miscellaneous Math", 24, layout="paragraph_title"),
             line(0, 300, "2.1 Sets and Mappings", 14, layout="paragraph_title")]
        v += [line(0, 340 + k * 18, BODY, 10.5, layout="text") for k in range(3)]
        v.append(line(1, 200, "Exercises", 14, layout="paragraph_title"))
        v.append(line(1, 260, "2.14 What is a vector?", 12, layout="paragraph_title"))
        v += [line(1, 300 + k * 18, BODY, 10.5, layout="text") for k in range(3)]
        v.append(line(2, 200, "3 Raster Images", 24, layout="paragraph_title"))
        v.append(line(2, 300, "3.1 Raster Devices", 14, layout="paragraph_title"))
        v += [line(2, 340 + k * 18, BODY, 10.5, layout="text") for k in range(3)]
        got = [(h.title, h.level) for h in self.eng.detect(v, 3)]
        self.assertIn(("2 Miscellaneous Math", 1), got)
        self.assertIn(("3 Raster Images", 1), got)
        self.assertIn(("3.1 Raster Devices", 2), got)
        self.assertTrue(all("2.14" not in t for t, _ in got))

    def test_promote_chapter_with_trailing_dot_sections(self):
        """小节号带尾点（“1.1.”）的书：靠第一个小节反推无编号的大字章名，成品标题去掉尾点。"""
        v = [line(0, 200, "The BC-IFS Model", 24, layout="paragraph_title")]
        v += [line(0, 300 + k * 18, BODY, 10.5, layout="text") for k in range(3)]
        v.append(line(1, 300, "1.1. Self-similarity and IFS", 14, layout="paragraph_title"))
        v.append(line(1, 360, "1.2. Controlled Systems", 14, layout="paragraph_title"))
        v += [line(1, 400 + k * 18, BODY, 10.5, layout="text") for k in range(3)]
        got = [(h.title, h.level) for h in self.eng.detect(v, 2)]
        self.assertIn(("1 The BC-IFS Model", 1), got)
        self.assertIn(("1.1 Self-similarity and IFS", 2), got)

    def test_promote_centered_chapter_title(self):
        """章名居中排版（x 不在左边界）且折成两行（第一行以逗号结尾）时，
        靠第一个小节反推要认这个大字行并把两行拼全，不能被“长行像句子”误杀。"""
        t = line(0, 200, "Building Operations, Assistance to", 24, layout="paragraph_title")
        t.x0, t.x1 = 200, 560
        t2 = line(0, 234, "Design and Applications", 24, layout="paragraph_title")
        t2.x0, t2.x1 = 260, 560
        v = [t, t2]
        v += [line(0, 300 + k * 18, BODY, 10.5, layout="text") for k in range(3)]
        v.append(line(1, 300, "4.1. Topological consistency", 14, layout="paragraph_title"))
        v.append(line(1, 360, "4.2. Topological combination", 14, layout="paragraph_title"))
        v += [line(1, 400 + k * 18, BODY, 10.5, layout="text") for k in range(3)]
        got = [(h.title, h.level) for h in self.eng.detect(v, 2)]
        self.assertIn(("4 Building Operations, Assistance to Design and Applications", 1), got)

    def test_ocr_number_cleanup(self):
        """OCR 丢空格、编号后多出“（”、2.1.1 识别成 2.1.11。"""
        v = []
        for p, t in enumerate(["2.1CDN的系统架构", "2.1.11 功能架构", "2.1.2部署架构", "2.2（ CDN系统分类"]):
            v.append(line(p, 300, t, 16, layout="paragraph_title", ocr=True))
            v += [line(p, 400 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(6)]
        got = [h.title for h in self.eng.detect(v, 4)]
        self.assertEqual(got, ["2.1 CDN的系统架构", "2.1.1 功能架构", "2.1.2 部署架构", "2.2 CDN系统分类"])

    def test_appendix(self):
        """附录与章同级，附录小节 A.1 在其下；正文“附录中列出……”不算。"""
        titles = [("第1章 概述", 24), ("1.1 背景", 16), ("附录A CDN试验床实施指南", 24),
                  ("A.1 安装操作系统", 16), ("附录中列出了常用命令，供读者参考查阅，这里不再赘述", 10.5)]
        v = []
        for p, (t, size) in enumerate(titles):
            v.append(line(p, 300, t, size, layout="paragraph_title" if size > 12 else "text"))
            v += [line(p, 400 + k * 18, BODY, 10.5, layout="text") for k in range(6)]
        got = [(h.title, h.level) for h in self.eng.detect(v, len(titles))]
        self.assertEqual(got, [("第1章 概述", 1), ("1.1 背景", 2), ("附录A CDN试验床实施指南", 1), ("A.1 安装操作系统", 2)])

    def test_gov_doc_ocr(self):
        def body(p, y):
            return line(p, y, "各单位要高度重视，认真组织实施，确保各项工作落到实处。", 12, layout="text", ocr=True)
        v = [line(0, 100, "关于开展专项检查工作的通知", 20, layout="doc_title", ocr=True), body(0, 150), body(0, 170),
             line(0, 200, "一、总体要求", 12, layout="paragraph_title", ocr=True), body(0, 230), body(0, 250),
             line(0, 280, "（一）指导思想", 12, layout="text", ocr=True), body(0, 300),
             line(0, 330, "1.坚持问题导向", 12, layout="text", ocr=True), body(0, 350),
             line(1, 100, "二、工作安排", 12, layout="paragraph_title", ocr=True), body(1, 130), body(1, 150),
             line(1, 180, "（一）时间节点", 12, layout="paragraph_title", ocr=True), body(1, 210)]
        got = [(h.title, h.level) for h in self.eng.detect(v, 2)]
        self.assertEqual(got, [("一、总体要求", 1), ("二、工作安排", 1), ("（一）时间节点", 2)])

    def test_load_shared_config(self):
        cfg = RuleConfig.load(CONFIG)
        self.assertAlmostEqual(cfg.w_layout_title, 0.45)
        self.assertEqual(cfg.max_level, 3)

    def test_textbook_ocr_numbers(self):
        """编号与数字粘连、编号里的点识别成句号、不属于本章的编号、目录括号页码、第四级小列表。"""
        titles = [("第1章 微型计算机基础 （1）", 24), ("1。1 数的表示方法", 16), ("1.1.1 进位计数制", 14),
                  ("1.十进制数", 12), ("2.二进制数", 12), ("第2章 微处理器", 24), ("2.18086 CPU的内部结构", 16),
                  ("2.1.18086 CPU内部结构", 14), ("1.8086 CPU内部结构", 12), ("2.1.28086 CPU内部寄存器", 14),
                  ("2.8086 存储器的分体结构", 12), ("2.2 工作模式", 16), ("2.332 位微处理器", 16), ("5.2 ×100", 12)]
        v = []
        for p, (t, size) in enumerate(titles):
            v.append(line(p, 300, t, size, layout="paragraph_title", ocr=True))
            v += [line(p, 400 + k * 18, BODY, 10.5, layout="text", ocr=True) for k in range(6)]
        got = [(h.title, h.level) for h in self.eng.detect(v, len(titles))]
        self.assertEqual(got, [
            ("第1章 微型计算机基础", 1), ("1.1 数的表示方法", 2), ("1.1.1 进位计数制", 3),
            ("第2章 微处理器", 1), ("2.1 8086CPU的内部结构", 2), ("2.1.1 8086CPU内部结构", 3),
            ("2.1.2 8086CPU内部寄存器", 3), ("2.2 工作模式", 2), ("2.3 32位微处理器", 2)])

    def test_eroded_dot_10_13_to_10_1_3(self):
        text = "正文用于估计字号的普通段落，需要足够的长度。"
        v = [line(0, 80, "第10章 光照", 20)]
        v += [line(0, 120 + k * 14, text, 10, ocr=True) for k in range(4)]
        v += [
            line(1, 100, "10.1 光源", 14, layout="paragraph_title", ocr=True),
            line(1, 160, "10.1.1 点光源", 12, layout="paragraph_title", ocr=True),
            line(1, 220, "10.1.2 无穷远光源", 12, layout="paragraph_title", ocr=True),
            line(1, 280, "10.13 辐射强度衰减", 12, layout="paragraph_title", ocr=True),  # 消蚀丢点
            line(1, 340, "10.1.4 方向光源", 12, layout="paragraph_title", ocr=True),
        ]
        for k in range(4):
            v.append(line(1, 400 + k * 14, text, 10, ocr=True))
        got = [h.title for h in self.eng.detect(v, 2)]
        self.assertIn("10.1.3 辐射强度衰减", got)
        self.assertNotIn("10.13 辐射强度衰减", got)

    def test_join_split_number_overlap(self):
        from pdf_bookmark.rule_engine import _join_split_numbers
        num = TextLine(text="13.1", page=0, x0=100, y0=100, x1=180, y1=130,
                       page_width=595, page_height=842, font_size=14, from_ocr=True)
        # 标题框与编号框重叠（x0 < num.x1）
        title = TextLine(text="计算机动画的光栅方法", page=0, x0=170, y0=98, x1=400, y1=132,
                         page_width=595, page_height=842, font_size=14, from_ocr=True)
        got = _join_split_numbers([num, title])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].text, "13.1 计算机动画的光栅方法")

    def test_trailing_ocr_junk(self):
        text = "正文用于估计字号的普通段落，需要足够的长度。"
        v = [line(0, 80, "第15章 图形文件格式", 20)]
        v += [line(0, 200 + k * 14, text, 10, ocr=True) for k in range(6)]
        v.append(line(0, 400, "15.3 文件压缩技术b:)", 14, layout="paragraph_title", ocr=True))
        got = [h.title for h in self.eng.detect(v, 1)]
        self.assertIn("15.3 文件压缩技术", got)
        self.assertTrue(all("b:)" not in h for h in got))

    def test_reject_exercises_and_mid_ocr_junk(self):
        text = "正文用于估计字号的普通段落，需要足够的长度。"
        v = [line(0, 80, "第2章 图形系统", 20)]
        v += [line(0, 120 + k * 14, text, 10, ocr=True) for k in range(4)]
        v += [
            line(0, 220, "2.10 小结", 14, layout="paragraph_title", ocr=True),
            line(0, 280, "练习题", 14, layout="paragraph_title", ocr=True),
            line(0, 320, "2.1 列出下列显示技术的工作特性：光栅刷新系统。", 12,
                 layout="paragraph_title", ocr=True),
            line(0, 360, "2.14 使用变焦镜的三维监视器与立体感系统相比的优点和缺点是什么?", 12,
                 layout="paragraph_title", ocr=True),
            line(1, 80, "第3章 输出图元", 20),
        ]
        v += [line(1, 120 + k * 14, text, 10, ocr=True) for k in range(4)]
        v += [
            line(1, 220, "3.2 (√id OpenGL中指定二维世界坐标系统", 14, layout="paragraph_title", ocr=True),
            line(1, 280, "3.7 「 帧缓存值的装载", 14, layout="paragraph_title", ocr=True),
            line(1, 340, "3.10 (cid:) 椭圆生成算法", 14, layout="paragraph_title", ocr=True),
            line(1, 400, "3.11 其他曲线", 14, layout="paragraph_title", ocr=True),
        ]
        got = [h.title for h in self.eng.detect(v, 2)]
        self.assertIn("2.10 小结", got)
        self.assertTrue(all(not re.match(r"2\.(1|14)\b", h) and "练习" not in h for h in got))
        self.assertIn("3.2 OpenGL中指定二维世界坐标系统", got)
        self.assertIn("3.7 帧缓存值的装载", got)
        self.assertIn("3.10 椭圆生成算法", got)

    def test_chapter_bullet_ocr_junk(self):
        text = "正文用于估计字号的普通段落，需要足够的长度。"
        v = [line(0, 100, "第12章 •颜色模型和颜色应用", 22, layout="paragraph_title", ocr=True)]
        v += [line(0, 200 + k * 14, text, 10, ocr=True) for k in range(6)]
        v.append(line(0, 400, "12.1 概述", 14, layout="paragraph_title", ocr=True))
        got = [h.title for h in self.eng.detect(v, 1)]
        self.assertEqual(got[0], "第12章 颜色模型和颜色应用")

    def test_appendix_ocr_junk(self):
        text = "正文用于估计字号的普通段落，需要足够的长度。"
        v = [line(0, 80, "第15章 图形文件格式", 20)]
        v += [line(0, 200 + k * 14, text, 10, ocr=True) for k in range(6)]
        v += [
            line(1, 100, "附录Ai 计算机图形学的数学基础", 22, layout="paragraph_title", ocr=True),
            line(1, 200, "A.1 坐标参照系", 14, layout="paragraph_title", ocr=True),
            line(1, 280, "A.1.1 二维笛卡儿屏幕坐标系", 12, layout="paragraph_title", ocr=True),
        ]
        for k in range(6):
            v.append(line(1, 360 + k * 14, text, 10, ocr=True))
        got = [(h.title, h.level) for h in self.eng.detect(v, 2)]
        self.assertEqual(got[0], ("第15章 图形文件格式", 1))
        self.assertEqual(got[1][0], "附录A 计算机图形学的数学基础")
        self.assertEqual(got[1][1], 1)
        self.assertEqual(got[2], ("A.1 坐标参照系", 2))

    def test_reject_caption_and_figure_step(self):
        text = "正文用于估计字号的普通段落，需要足够的长度。"
        v = [line(0, 100, "第1章 综述", 22)]
        v += [line(0, 200 + k * 14, text, 10, ocr=True) for k in range(8)]
        v += [
            line(0, 400, "图1.1 示例说明", 11, layout="figure_title", ocr=True),
            line(0, 450, "第3步 将对象绕z轴旋转", 11, ocr=True),
            line(0, 500, "1.1 图和表", 14, layout="paragraph_title", ocr=True),
        ]
        got = [h.title for h in self.eng.detect(v, 1)]
        self.assertEqual(got, ["第1章 综述", "1.1 图和表"])

    def test_english_latex_book(self):
        text = "The bulk of this chapter will focus on applications of type systems to memory resources."
        v = [line(0, 100, "Contents", 15)] + [line(0, 150 + k * 12, t, 9) for k, t in enumerate(
            ["I Basics 1", "1 Intro 3", "1.1 Start 4", "1.2 More 6", "2 Next 9", "2.1 First 9"])]
        v.append(line(1, 174, "Basics", 21))
        v += [line(2, 94, "1", 45), line(2, 150, "Intro", 15)]
        v.append(line(3, 300, "1.1 Start", 10.5))
        for p in (4, 5, 6):
            v.append(line(p, 34, f"1.2 More {p + 2}", 8.2))
        v.append(line(4, 66, "1.2 More", 10.5))
        v += [line(7, 94, "2", 45), line(7, 150, "Next", 15), line(8, 300, "2.1 First", 10.5)]
        for p in range(1, 9):
            v += [line(p, 400 + k * 12, text, 9) for k in range(8)]
        got = [(h.title, h.level) for h in self.eng.detect(v, 9)]
        self.assertEqual(got, [("Part I Basics", 1), ("1 Intro", 2), ("1.1 Start", 3), ("1.2 More", 3),
                               ("2 Next", 2), ("2.1 First", 3)])

    def test_english_long_decimal3_near_body_size(self):
        """字号略大于正文的长三级标题（12.2.2 …）不能被当折行正文丢掉。"""
        text = "The impulse method resolves collisions by applying impulses at contact points."
        v = [line(0, 50, "Chapter 12 Physics-based Simulation", 24, True)]
        v += [line(0, 120 + k * 14, text, 10) for k in range(5)]
        v += [
            line(1, 80, "12.2 Impulse Method", 12, True),
            line(1, 140, "12.2.1 Impulse", 11, True),
            line(1, 200, "12.2.2 Impulse-based Collision Resolution", 11, True),
            line(1, 280, "12.3 Collision Detection", 12, True),
        ]
        v += [line(1, 340 + k * 14, text, 10) for k in range(5)]
        got = [h.title for h in self.eng.detect(v, 2)]
        self.assertIn("12.2.2 Impulse-based Collision Resolution", got)
        self.assertIn("12.2.1 Impulse", got)

    def test_english_chapter_wrap_and_body_false_positive(self):
        """Chapter 3 / Rasterization 折两行要合并；正文里单独一行的 Chapter 4. 不能进书签。"""
        text = "The vertices processed by the vertex processor are then assembled into primitives."
        v = [line(0, 50, "Chapter 3", 24, True)]
        v.append(line(0, 100, "Rasterization", 18, True))
        v += [line(0, 160 + k * 14, text, 10) for k in range(6)]
        v += [
            line(1, 200, "algorithm at the output merging stage. It will be presented in", 10),
            line(1, 220, "Chapter 4.", 10),
            line(1, 280, "3.4 Coordinate Systems - Revisite", 12, True),
            line(1, 295, "d+", 12, True),
        ]
        v += [line(1, 340 + k * 14, text, 10) for k in range(4)]
        v += [
            line(2, 80, "Chapter 4", 24, True),
            line(2, 120, "Fragment Processing", 18, True),
            line(2, 200, "4.1.2 Surface Parameterizatio", 11, True),
            line(2, 214, "n", 11, True),
        ]
        v += [line(2, 260 + k * 14, text, 10) for k in range(4)]
        got = [h.title for h in self.eng.detect(v, 3)]
        self.assertIn("Chapter 3 Rasterization", got)
        self.assertIn("Chapter 4 Fragment Processing", got)
        self.assertIn("3.4 Coordinate Systems - Revisited", got)
        self.assertIn("4.1.2 Surface Parameterization", got)
        self.assertNotIn("Chapter 4.", got)
        self.assertTrue(all(h not in ("d+", "n", "Chapter 4") for h in got))

    def test_bookmark_dest_with_offset_mediabox(self):
        import pymupdf
        from pdf_bookmark.models import Heading
        from pdf_bookmark.pdf_io import write_toc

        doc = pymupdf.open()
        page = doc.new_page(width=514, height=727)
        page.set_mediabox(pymupdf.Rect(0, 497, 514, 1224))
        write_toc(doc, [Heading(title="第1章 简介", page=0, y=106, level=1)])
        outlines = int(doc.xref_get_key(doc.pdf_catalog(), "Outlines")[1].split()[0])
        first = int(doc.xref_get_key(outlines, "First")[1].split()[0])
        self.assertIn("/XYZ 0 625 0", doc.xref_object(first, compressed=True))  # page_h 727 - (106-4)
        self.assertEqual(doc.xref_get_key(page.xref, "MediaBox")[1], "[0 0 514 727]")


if __name__ == "__main__":
    unittest.main()
