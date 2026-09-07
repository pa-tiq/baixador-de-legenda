# BaixarLegenda

Pequeno utilitário para Windows que baixa automaticamente a legenda PT-BR de um arquivo de vídeo usando a API REST do OpenSubtitles.com.

## Comportamento

Ao clicar em **Baixar legenda PT-BR** no menu de contexto de um vídeo:

1. calcula o moviehash do vídeo;
2. procura legendas PT-BR por moviehash;
3. se não encontrar, faz fallback para busca pelo nome do arquivo;
4. se ainda não existir `Video.srt`, mostra todas as legendas encontradas em uma janela;
5. se `Video.srt` já existir, mostra na janela apenas as legendas que ainda não foram tentadas para aquele moviehash;
6. o usuário escolhe na janela a legenda que faz mais sentido (release, downloads, avaliação, se é match exato de moviehash, se é confiável, HI, tradução por IA/máquina) e clica em "Baixar selecionada";
7. registra a legenda tentada em SQLite;
8. substitui o `.srt` somente depois de o download terminar corretamente.

Não abre mais uma janela de terminal: a interação é só pela janela de seleção (e, em caso de erro, uma caixa de mensagem do Windows).

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

Copie o arquivo config.json para a pasta dist. Ele precisa estar na mesma pasta que o arquivo `.exe`.

```powershell
Copy-Item .\config.json .\dist\config.json
```

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
    logs\
        baixarlegenda.log
```

`logs\baixarlegenda.log` registra, a cada execução, o vídeo processado, o moviehash, o que foi identificado (metadado/nome de arquivo, GuessIt), os parâmetros de cada busca e a lista completa de legendas retornadas pelo OpenSubtitles (bruta e já ranqueada) — útil para descobrir por que a busca funciona para um vídeo e não para outro. O arquivo gira automaticamente (até ~2 MB, mantendo 3 backups) para não crescer indefinidamente. Ao rodar `python .\baixar_legenda.py ...` direto no terminal, o mesmo log também aparece no console.

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
