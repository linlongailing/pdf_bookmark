"""定位内置模型与规则文件，兼容源码运行与 PyInstaller 打包后的 exe。"""

import sys
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)

# 打包时 PyInstaller 把数据解压到 _MEIPASS；源码运行时为项目根目录
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
# 用户可修改的文件（rules.ini）放在 exe 旁边
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else BUNDLE_DIR

LAYOUT_MODEL = "PP-DocLayout-S"
DET_MODEL = "PP-OCRv5_mobile_det"
REC_MODEL = "PP-OCRv5_mobile_rec"


def model_file(name: str) -> str:
    """exe 旁的 models 目录优先（便于替换模型），其次为内置模型。"""
    for base in (APP_DIR / "models", BUNDLE_DIR / "models"):
        p = base / name
        if p.is_file():
            return str(p)
    raise FileNotFoundError(f"找不到模型文件 models/{name}，请先运行 tools/prepare_models.py")


def default_rules() -> str | None:
    for p in (APP_DIR / "rules.ini", BUNDLE_DIR / "config" / "rules.ini"):
        if p.is_file():
            return str(p)
    return None
