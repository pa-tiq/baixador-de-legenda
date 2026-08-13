param(
    [Parameter(Mandatory=$true)]
    [string]$ExecutablePath
)

$exe = (Resolve-Path $ExecutablePath).Path
$extensions = @('.mkv','.mp4','.avi','.mov','.m4v','.wmv','.webm','.flv','.3gp')

foreach ($ext in $extensions) {
    $base = "HKCU:\Software\Classes\SystemFileAssociations\$ext\shell\BaixarLegenda"
    New-Item -Path "$base\command" -Force | Out-Null
    Set-ItemProperty -Path $base -Name '(Default)' -Value 'Baixar legenda PT-BR'
    Set-ItemProperty -Path $base -Name 'Icon' -Value $exe
    Set-ItemProperty -Path "$base\command" -Name '(Default)' -Value "`"$exe`" `"%1`""
}

Write-Host "Menu de contexto instalado para: $($extensions -join ', ')"
Write-Host "Comando: Baixar legenda PT-BR"
