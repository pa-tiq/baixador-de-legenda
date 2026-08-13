$extensions = @('.mkv','.mp4','.avi','.mov','.m4v','.wmv','.webm','.flv','.3gp')

foreach ($ext in $extensions) {
    $path = "HKCU:\Software\Classes\SystemFileAssociations\$ext\shell\BaixarLegenda"
    if (Test-Path $path) {
        Remove-Item -Path $path -Recurse -Force
    }
}

Write-Host 'Menu de contexto removido.'
