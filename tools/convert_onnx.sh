#!/usr/bin/env bash
# 在 Linux / WSL 中把 PaddleX 官方模型转换为 ONNX（Windows 版 paddle2onnx 无法加载）。
# 用法: bash tools/convert_onnx.sh <official_models 目录> <输出目录>
set -euo pipefail
SRC=${1:-/mnt/c/Users/$USER/.paddlex/official_models}
OUT=${2:-onnx_models}
MODELS="PP-DocLayout-S PP-OCRv5_mobile_det PP-OCRv5_mobile_rec"

export PATH="$HOME/.local/bin:$PATH"
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
if [ ! -x "$HOME/p2o/bin/python" ]; then
    uv venv -q -p 3.12 "$HOME/p2o"
fi
uv pip install -q -p "$HOME/p2o/bin/python" paddlepaddle paddle2onnx packaging onnxruntime onnx_graphsurgeon

mkdir -p "$OUT"
for m in $MODELS; do
    "$HOME/p2o/bin/paddle2onnx" --model_dir "$SRC/$m" --model_filename inference.json \
        --params_filename inference.pdiparams --save_file "$OUT/$m.onnx" --opset_version 14
    cp "$SRC/$m/inference.yml" "$OUT/$m.yml"
done
ls -l "$OUT"
