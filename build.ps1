$ErrorActionPreference = 'Stop'

python -m pip install -r requirements.txt
python -m pip install pyinstaller

pyinstaller `
  --onefile `
  --noconsole `
  --name BaixarLegenda `
  baixar_legenda.py

Write-Host ''
Write-Host 'Build concluído:'
Write-Host (Resolve-Path '.\dist\BaixarLegenda.exe')
