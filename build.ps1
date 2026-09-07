$ErrorActionPreference = "Stop"

Remove-Item -Recurse -Force ".\build" -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force ".\dist" -ErrorAction SilentlyContinue
Remove-Item -Force ".\BaixarLegenda.spec" -ErrorAction SilentlyContinue

pyinstaller `
    --onefile `
    --noconsole `
    --name "BaixarLegenda" `
    --collect-all babelfish `
    --collect-all guessit `
    --copy-metadata babelfish `
    --copy-metadata guessit `
    ".\baixar_legenda.py"

Write-Host ""
Write-Host "Build concluido:"
Write-Host "  dist\BaixarLegenda.exe"