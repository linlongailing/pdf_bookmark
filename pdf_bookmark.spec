# PyInstaller 配置：打包为单个 exe（ONNX Runtime DirectML + 内置 ONNX 模型）
# 用法：.venv\Scripts\pyinstaller pdf_bookmark.spec --noconfirm

from PyInstaller.utils.hooks import collect_all

datas = [("models", "models"), ("config/rules.ini", "config")]
binaries = []
hiddenimports = []

for pkg in ("onnxruntime", "pymupdf"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h


def _keep(entry):
    path = entry[0].replace("\\", "/")
    # 头文件、测试、静态库、onnxruntime 的模型转换工具对运行无用
    return not ("/tests/" in path or "/onnxruntime/tools/" in path or "/onnxruntime/transformers/" in path
                or path.endswith((".h", ".hpp", ".lib", ".a")))


datas = [d for d in datas if _keep(d)]
binaries = [b for b in binaries if _keep(b)]

a = Analysis(
    ["run_pdf_bookmark.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "IPython", "jupyter", "pytest", "torch", "tensorflow",
              "paddle", "paddlex", "paddleocr", "sympy", "onnx", "pandas", "scipy"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="pdf_bookmark",
    icon="assets/logo.ico",
    console=True,
    upx=False,
    runtime_tmpdir=None,
)
