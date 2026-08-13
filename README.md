# BaixarLegenda

Pequeno utilitário para Windows que baixa automaticamente a legenda PT-BR de um arquivo de vídeo usando a API REST do OpenSubtitles.com.

## Comportamento

Ao clicar em **Baixar legenda PT-BR** no menu de contexto de um vídeo:

1. calcula o moviehash do vídeo;
2. procura legendas PT-BR por moviehash;
3. se não encontrar, faz fallback para busca pelo nome do arquivo;
4. se ainda não existir `Video.srt`, baixa a primeira legenda da lista, ignorando o histórico;
5. se `Video.srt` já existir, ignora as legendas que já foram tentadas para aquele moviehash e baixa a próxima;
6. registra a legenda tentada em SQLite;
7. substitui o `.srt` somente depois de o download terminar corretamente.

Para reiniciar completamente a seleção, basta apagar o `.srt`. O histórico permanece, mas a ausência do `.srt` faz o programa deliberadamente ignorá-lo na próxima execução.

## Requisitos

[Python](https://www.python.org/downloads/windows/):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

ffmpeg: Use o comando `ffprobe -version` para verificar se o ffmpeg está instalado.

```powershell
winget install ffmpeg
```

O caminho da pasta bin precisa estar no seu PATH.

## 1. Configuração

Copie:

```powershell
Copy-Item .\config.example.json .\config.json
```

Edite `config.json` e coloque sua API key.

Durante o desenvolvimento, o programa aceita `config.json` na mesma pasta de `baixar_legenda.py`.
Na instalação final, também pode usar `%APPDATA%\BaixarLegenda\config.json`.

O arquivo final não deve ser versionado.

### Autenticação

O projeto funciona inicialmente apenas com a API key. Se sua aplicação/API exigir autenticação do usuário para downloads, preencha também `username` e `password`; o programa fará login e enviará o JWT nas requisições seguintes.

## 2. Testar sem criar EXE

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python .\baixar_legenda.py "C:\Videos\Filme.mkv"
```

## 3. Criar o EXE

No PowerShell:

```powershell
.\build.ps1
```

O resultado será:

```text
dist\BaixarLegenda.exe
```

Copie o arquivo config.json para a pasta dist.

## 4. Instalar no menu de contexto

Por exemplo:

```powershell
powershell -ExecutionPolicy Bypass -File .\install_context_menu.ps1 -ExecutablePath .\dist\BaixarLegenda.exe
```

Depois disso, no Explorer:

**Botão direito no vídeo → Baixar legenda PT-BR**

O registro é feito apenas em `HKEY_CURRENT_USER`, então não precisa de administrador.

Para remover:

```powershell
powershell -ExecutionPolicy Bypass -File .\uninstall_context_menu.ps1
```

## 5. Onde ficam os dados

O programa guarda a configuração e o histórico em:

```text
%APPDATA%\BaixarLegenda\
    config.json
    history.db
```

O banco possui uma chave composta por:

```text
movie_hash + file_id
```

Assim, uma mesma legenda pode ser baixada para vídeos diferentes sem conflito, e a mesma legenda não será escolhida novamente para o mesmo vídeo quando já houver um `.srt` presente.

## Observações

- O download solicitado à API consome a cota de downloads do OpenSubtitles.
- O programa não baixa várias candidatas para descobrir qual é melhor: ele baixa somente a próxima candidata selecionada. Isso preserva a cota.
- A API retorna as legendas com metadados de correspondência, confiança e downloads; o programa prioriza match de moviehash, fonte confiável e popularidade recente antes de escolher a candidata.
- O `.srt` é salvo em UTF-8, conforme o comportamento documentado pela API.

## Estrutura

```text
baixador-de-legenda/
├── baixar_legenda.py
├── config.example.json
├── requirements.txt
├── build.ps1
├── install_context_menu.ps1
├── uninstall_context_menu.ps1
├── README.md
└── tests/
```
