# pdf_bookmark

为没有书签的 PDF 自动识别标题并生成多级书签。

- **文字版页面**：PyMuPDF 直接解析文字层（文字、字号、粗体），速度快、零误差；
- **扫描页面**：渲染成图片（默认 150 DPI）后用 ONNX Runtime 运行 PP-DocLayout-S 版面分析 + PP-OCRv5 文字检测 / 识别；
- **只识别标题区域**：正文只做文字检测（用于估计正文字号），只对标题、页眉、目录区域及版面框外的行做文字识别；
  书前部分（前 8% 页，10～40 页）识别全部文字，用于判断目录页和纠正标题错字；
- **自动选择设备**：有支持 DirectML 的显卡（NVIDIA / AMD / Intel 独显或核显）时用 GPU，否则用 CPU，无需安装 CUDA；
- **多线程分块**：主线程解析 / 渲染页面，按块（默认 8 页）交给线程池识别，最后按页码顺序合并；
- **跳过空白页、纯图片页**；同一份 PDF 中文字页和扫描页可以混排；
- **规则引擎**：综合编号样式、字号、粗体、版面类别、位置等特征判定标题并推断层级。

## 直接使用 exe

`dist\pdf_bookmark.exe`（单文件，约 150MB，已内置模型，无需联网）：

- 双击运行，按提示输入或拖入 PDF 路径；
- 或把一个或多个 PDF 直接拖到 exe 图标上；
- 或命令行：`pdf_bookmark.exe a.pdf b.pdf`、`pdf_bookmark.exe a.pdf --out a_bm.pdf`。

输出文件默认为 `<原文件名>_bookmarked.pdf`。在 exe 旁放一个 `rules.ini`（格式见 `config/rules.ini`）即可调整识别规则。

参考速度（405 页扫描书，RTX 4060 Ti / i7-14790F）：GPU 约 0.13 秒/页（整本约 52 秒），纯 CPU 约 0.5 秒/页（约 3 分 16 秒）。

## 源码运行

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m pdf_bookmark input.pdf
.venv\Scripts\python -m unittest tests.test_rules
```

## 模型与打包

```powershell
.\build_exe.ps1     # 安装依赖、准备模型、生成 dist\pdf_bookmark.exe
```

`models/` 中的 ONNX 模型由 PaddleX 官方模型（`%USERPROFILE%\.paddlex\official_models`）转换而来：
Windows 版 `paddle2onnx` 依赖 Paddle 未导出的内部符号而无法加载，因此在 WSL 中转换：

```powershell
wsl bash -c "tr -d '\r' < tools/convert_onnx.sh > /tmp/c.sh && bash /tmp/c.sh /mnt/c/Users/<用户名>/.paddlex/official_models onnx_models"
.venv\Scripts\python tools\prepare_models.py onnx_models   # 复制到 models\ 并导出识别字典
```

版面模型的 NMS 在 DirectML 上输出错误，因此版面模型固定在 CPU 上运行（输入仅 480×480，开销很小）。

## 常用参数

| 参数 | 说明 |
| --- | --- |
| `--mode` | `auto`（默认，逐页判断）/ `text` 只用文字层 / `ocr` 全部 OCR / `hybrid` 文字层 + 版面分析辅助 |
| `--device` | `auto`（默认）/ `gpu` / `cpu` |
| `--full_ocr` | 识别页面全部文字（更慢，一般不需要） |
| `--pages 1-20,25` | 只处理部分页面 |
| `--dpi` | OCR 渲染 DPI，默认 150 |
| `--min_text_chars` | 文字层字符数低于该值的页面视为扫描页，默认 30 |
| `--max_level` | 书签最大层级（默认取 `rules.ini` 的 `max_level=3`）；如 `--max_level 4` 可把小节的下一级也收进来 |
| `--rules` | 规则配置文件 |
| `--dump_toc` / `--toc_in` | 导出识别结果供人工校对 / 按校对后的目录写入 |
| `--force` | 覆盖已有书签 |
| `--dry_run` / `--verbose` | 只看结果不写文件 / 显示每页处理方式和标题得分 |
| `--lang zh` / `--lang en` | 界面语言（默认跟随系统；也可用环境变量 `PDF_BOOKMARK_LANG`） |

## 规则引擎

默认只识别带阿拉伯数字或中文数字编号的标题（`第1章`、`一、`、`（一）`、`1.1`、`(1)` 等）以及附录
（`附录`、`附录A`、`附录一`、`Appendix B`，与章同级；附录小节 `A.1`、`A.1.1` 与 `1.1`、`1.1.1` 同级），
“总结”“阅读建议”“前言”这类无编号标题不会生成书签；如需识别，在 `rules.ini` 中设置 `numbered_only = false`。

1. **过滤**：目录页（“标题 页码”“标题……页码”“标题页码”、页码单独成行、`• 1.1 xxx` 列表式目录）、
   章首页的本章小目录、末尾带页码且正文有同名标题的目录项、页眉页脚区域、章节页眉（每页顶部的“第1章 引言”）、
   多页重复文本、纯页码、图表标题、正文字号的长行、以句号/逗号结尾的句子、低置信度 OCR 结果。
2. **打分**（权重可在 `config/rules.ini` 调整，阈值 0.5）：版面判为标题 +0.45；`第X章/第X节/附录A` 等 +0.5；
   `一、（一）1. 1.1 1.1.1 (1)` +0.35；字号 ≥ 正文 1.35 倍 +0.35、≥ 1.12 倍 +0.2；粗体 +0.15；短行 +0.1。
3. **OCR 纠错**：同一编号出现多次时保留正文中的标题；用目录里同编号的标题纠正错字；补编号后的空格、
   去掉标题旁装饰图标识别出的杂字；按前后编号纠正 `2.1.11`→`2.1.1`。
4. **层级**：编号样式的内在次序为 篇 > 章 / 附录（同级） > 节 > 罗马数字 > 一、 > （一） > `1.` 一级编号 >
   `1.1`（附录小节 `A.1` 等同此级） > `1.1.1` > `1.1.1.1` > `(1)`；只对本书实际出现的样式依次分配层级
   （没有篇的书自动以章为第一级）；无编号的特殊标题（前言、参考文献等）和文档大标题为第一级；
   成品默认最多保留 3 级，可用 `--max_level` 调整；`1.`、`2.` 后面紧跟 `1.1` 时视为章号，否则作为小节内的列表标题；层级不跳级。
5. **缺失补全**（扫描书章首页排版复杂、装饰字读不出时，按顺序兜底）：
   - **上下文反推**：某章的小节（如 `8.1`）已识别而章标题缺失时，到小节上方（同页或上一页）找无编号的大字行，
     补成“第8章 …”；附录同理（如靠 `A.1` 补“附录A …”）；装饰性的大号章号 / 字母被 OCR 读成空行、单字或
     符号时不依赖其读出结果，章号由小节编号推得；只有篇号的行（如“第二部分72”，尾数是印刷页码）会与下方大字篇名合并；
   - **目录反查**：正文与反推都缺失的标题，若书中有目录，则先用已认出的条目校准“印刷页号 → PDF 页”的偏移，
     只拿印刷页号圈定搜索范围，再到正文找文字相合的大字行；书签的落点和标题文字以正文行为准
     （只向目录借编号），正文找不到对应行则不加；没有目录时自动跳过。

## 目录结构

```
pdf_bookmark/
  cli.py          命令行、交互模式、多线程分块流程
  pdf_io.py       PyMuPDF：文字层解析、渲染、书签读写
  ocr_engine.py   ONNX Runtime：版面分析、文字检测 / 识别，DirectML / CPU 自动选择
  rule_engine.py  标题识别规则引擎
  resources.py    内置模型与配置定位（兼容 exe）
config/rules.ini  规则配置
tools/            模型转换（convert_onnx.sh）与整理（prepare_models.py）
tests/            规则引擎单元测试
run_pdf_bookmark.py / pdf_bookmark.spec / build_exe.ps1   打包
```
