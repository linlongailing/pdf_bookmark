"""把 convert_onnx.sh 生成的 ONNX 模型整理到 models/：复制 .onnx，并从识别模型配置中导出字符字典。

用法: python tools/prepare_models.py [onnx_models 目录]
"""

import shutil
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pdf_bookmark import resources  # noqa: E402

src = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "onnx_models")
dst = ROOT / "models"
dst.mkdir(exist_ok=True)
for name in (resources.LAYOUT_MODEL, resources.DET_MODEL, resources.REC_MODEL):
    shutil.copy2(src / f"{name}.onnx", dst / f"{name}.onnx")
    print("复制", name)

cfg = yaml.safe_load((src / f"{resources.REC_MODEL}.yml").read_text(encoding="utf-8"))
chars = cfg["PostProcess"]["character_dict"]
(dst / f"{resources.REC_MODEL}.dict.txt").write_text("\n".join(chars) + "\n", encoding="utf-8")
print("字典", len(chars), "字")
