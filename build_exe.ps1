# 一键打包：准备虚拟环境与 ONNX 模型，生成 dist\pdf_bookmark.exe
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path .venv\Scripts\python.exe)) {
    python -m venv .venv
}
.venv\Scripts\python.exe -m pip install -r requirements.txt pyinstaller pyyaml

# 内置模型：没有时从 PaddleX 官方模型转换（Windows 版 paddle2onnx 无法加载，借助 WSL 转换）
$need = "PP-DocLayout-S.onnx", "PP-OCRv5_mobile_det.onnx", "PP-OCRv5_mobile_rec.onnx", "PP-OCRv5_mobile_rec.dict.txt"
if ($need | Where-Object { -not (Test-Path "models\$_") }) {
    if (-not (Test-Path onnx_models\PP-OCRv5_mobile_rec.onnx)) {
        $user = $env:USERNAME
        wsl bash -c "tr -d '\r' < tools/convert_onnx.sh > /tmp/convert_onnx.sh && bash /tmp/convert_onnx.sh /mnt/c/Users/$user/.paddlex/official_models onnx_models"
    }
    .venv\Scripts\python.exe tools\prepare_models.py onnx_models
}

.venv\Scripts\pyinstaller.exe pdf_bookmark.spec --noconfirm
Write-Host "完成: $PSScriptRoot\dist\pdf_bookmark.exe"
