from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import tkinter as tk
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import ttk
from typing import Any

import requests
from guessit import guessit

APP_NAME = "BaixarLegenda"
APP_VERSION = "1.0.0"
API_BASE = "https://api.opensubtitles.com/api/v1"
VIDEO_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".avi",
    ".mov",
    ".m4v",
    ".wmv",
    ".webm",
    ".flv",
    ".3gp",
}
CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
CONFIG_FILE = CONFIG_DIR / "config.json"
# Durante o desenvolvimento, também aceitamos config.json ao lado do script.
# Isso facilita o primeiro teste sem exigir cópia manual para %APPDATA%.
LOCAL_CONFIG_FILE = Path(__file__).resolve().parent / "config.json"
# Quando empacotado com PyInstaller --onefile.
EXE_CONFIG_FILE = Path(sys.executable).resolve().parent / "config.json"

DB_FILE = CONFIG_DIR / "history.db"
LOG_DIR = CONFIG_DIR / "logs"
LOG_FILE = LOG_DIR / "baixarlegenda.log"

logger = logging.getLogger(APP_NAME)


def setup_logging() -> None:
    """
    Salva um log detalhado em %APPDATA%\\BaixarLegenda\\logs\\baixarlegenda.log,
    tanto na execução via menu de contexto quanto em testes diretos no
    terminal. Isso é o que permite investigar por que a busca funciona
    para alguns vídeos e falha para outros: fica registrado o que foi
    identificado no vídeo e a lista completa de candidatas retornadas
    pelo OpenSubtitles.

    Quando executado com um terminal anexado (teste manual via
    `python baixar_legenda.py ...`), o log também é espelhado no console.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # No EXE empacotado com console=False, sys.stdout é None: só
    # duplicamos para o console quando ele realmente existe (teste
    # direto no terminal com `python baixar_legenda.py ...`).
    if sys.stdout is not None:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setLevel(logging.INFO)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

    logger.info("=== Nova execução ===")
    logger.info("Log salvo em: %s", LOG_FILE)


class SubtitleError(RuntimeError):
    pass


@dataclass
class Subtitle:
    file_id: int
    subtitle_id: str
    language: str
    release: str
    title: str | None
    download_count: int
    new_download_count: int
    rating: float | None
    hearing_impaired: bool
    ai_translated: bool
    machine_translated: bool
    from_trusted: bool
    moviehash_match: bool
    file_name: str
    fps: float | None


def load_config() -> dict[str, Any]:
    """
    Procura o config.json nesta ordem:

    1. %APPDATA%\\BaixarLegenda\\config.json
    2. Ao lado do executável (PyInstaller)
    3. Ao lado do .py (desenvolvimento)
    """

    config_candidates = [
        CONFIG_FILE,
        EXE_CONFIG_FILE,
        LOCAL_CONFIG_FILE,
    ]

    config_file = next(
        (path for path in config_candidates if path.exists()),
        None,
    )

    if config_file is None:
        raise SubtitleError(
            "Configuração não encontrada.\n"
            f"Crie {CONFIG_FILE} a partir de config.example.json, "
            f"ou coloque config.json ao lado do executável."
        )

    try:
        return json.loads(config_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SubtitleError(f"Configuração inválida em {config_file}: {exc}") from exc


def get_video_title(video: Path) -> str | None:
    """
    Obtém o metadado 'Título' do arquivo de vídeo usando ffprobe.

    Exemplo:
        Welcome to the NHK - S01E01
    """
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_entries",
                "format_tags=title",
                str(video),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )

        data = json.loads(result.stdout)

        title = data.get("format", {}).get("tags", {}).get("title")

        if title:
            title = title.strip()

        return title or None

    except (
        FileNotFoundError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
    ):
        return None


def init_db() -> sqlite3.Connection:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS attempted_subtitles (
            video_hash TEXT NOT NULL,
            file_id INTEGER NOT NULL,
            subtitle_id TEXT,
            release TEXT,
            downloaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (video_hash, file_id)
        )
        """)
    conn.commit()
    return conn


def was_attempted(conn: sqlite3.Connection, video_hash: str, file_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM attempted_subtitles WHERE video_hash = ? AND file_id = ?",
        (video_hash, file_id),
    ).fetchone()
    return row is not None


def mark_attempted(
    conn: sqlite3.Connection,
    video_hash: str,
    subtitle: Subtitle,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO attempted_subtitles
            (video_hash, file_id, subtitle_id, release)
        VALUES (?, ?, ?, ?)
        """,
        (video_hash, subtitle.file_id, subtitle.subtitle_id, subtitle.release),
    )
    conn.commit()


def opensubtitles_hash(path: Path) -> str:
    """Calcula o moviehash oficial: tamanho + soma little-endian de 64 bits
    dos primeiros e últimos 64 KiB.
    """
    size = path.stat().st_size
    chunk = 64 * 1024
    if size < chunk * 2:
        raise SubtitleError("O vídeo é pequeno demais para calcular o moviehash.")

    value = size
    with path.open("rb") as f:
        for offset in (0, size - chunk):
            f.seek(offset)
            data = f.read(chunk)
            for (number,) in struct.iter_unpack("<Q", data):
                value = (value + number) & 0xFFFFFFFFFFFFFFFF

    return f"{value:016x}"


def build_session(config: dict[str, Any]) -> requests.Session:
    api_key = config.get("api_key", "").strip()
    if not api_key or api_key == "COLOQUE_SUA_API_KEY_AQUI":
        raise SubtitleError("Preencha api_key no arquivo de configuração.")

    app_name = config.get("app_name", APP_NAME)
    app_version = config.get("app_version", APP_VERSION)
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Api-Key": api_key,
            "User-Agent": f"{app_name} v{app_version}",
        }
    )

    username = config.get("username", "").strip()
    password = config.get("password", "")
    if username and password:
        response = session.post(
            f"{API_BASE}/login",
            json={"username": username, "password": password},
            timeout=30,
        )
        if response.status_code >= 400:
            raise SubtitleError(api_error(response, "Falha no login do OpenSubtitles"))
        token = response.json().get("token")
        if not token:
            raise SubtitleError(
                "O OpenSubtitles não retornou um token de autenticação."
            )
        session.headers["Authorization"] = f"Bearer {token}"

    return session


def api_error(response: requests.Response, prefix: str) -> str:
    try:
        payload = response.json()
        message = payload.get("message") or payload.get("errors") or payload
    except ValueError:
        message = response.text[:500]
    return f"{prefix} (HTTP {response.status_code}): {message}"


def identify_video(video: Path) -> dict[str, Any]:
    """
    Identifica o vídeo priorizando o nome do arquivo para não perder
    a marcação de temporada e episódio (SXXEXX), usando o metadado 'Título'
    como fallback ou complemento.
    """
    # 1. Analisa o nome do arquivo primeiro (ideal para extrair SXXEXX)
    info = guessit(video.name)
    info["_source"] = "filename"
    info["_query_title"] = video.stem

    # 2. Busca o metadado interno
    metadata_title = get_video_title(video)

    if metadata_title:
        info_metadata = guessit(metadata_title)

        # Se o nome do arquivo falhou em detectar que é série, mas o metadado tem essa info
        if info.get("type") != "episode" and info_metadata.get("type") == "episode":
            info_metadata["_source"] = "metadata"
            info_metadata["_query_title"] = metadata_title
            return info_metadata

        # Se o nome do arquivo detectou o episódio (S00E01) mas o título ficou confuso (ex: "OTGW")
        if info.get("type") == "episode" and info_metadata.get("title"):
            # Substitui pelo título limpo do metadado se disponível
            info["title"] = info_metadata["title"]
            info["_source"] = "filename_and_metadata"

        # Se não achou título pelo nome do arquivo
        elif not info.get("title"):
            info["title"] = info_metadata.get("title") or metadata_title
            info["_source"] = "metadata_fallback"

    return info


def search_subtitles(
    session: requests.Session,
    video: Path,
    language: str,
    max_results: int,
) -> list[Subtitle]:
    movie_hash = opensubtitles_hash(video)
    info = identify_video(video)

    source = info.get("_source", "filename")
    query_title = info.get("_query_title")

    logger.info("Vídeo: %s", video)
    logger.info("Moviehash: %s", movie_hash)
    logger.info("Identificação (%s): %s", source, query_title)
    logger.debug("Informações detectadas pelo GuessIt: %s", info)

    # ------------------------------------------------------------
    # 1. Se for episódio de série, NÃO usar o nome completo do
    #    arquivo como query.
    # ------------------------------------------------------------
    if info.get("type") == "episode":
        title = info.get("title")
        episode = info.get("episode")
        season = info.get("season")

        if title and episode is not None:
            params: dict[str, Any] = {
                "query": title,
                "episode_number": int(episode),
                "type": "episode",
                "languages": language,
                "order_by": "download_count",
                "order_direction": "desc",
                "page": 1,
                "per_page": min(max_results, 100),
            }

            # Só envia season_number quando o GuessIt conseguiu
            # identificá-lo.
            if season is not None:
                params["season_number"] = int(season)

            logger.info("Busca por episódio: params=%s", params)

            response = session.get(
                f"{API_BASE}/subtitles",
                params=params,
                timeout=30,
            )

            if response.status_code >= 400:
                raise SubtitleError(
                    api_error(
                        response,
                        "Falha na busca do episódio",
                    )
                )

            raw_data = response.json().get("data", [])
            logger.debug(
                "Resposta bruta (episódio, %d itens): %s", len(raw_data), raw_data
            )

            results = parse_subtitles(raw_data)
            ranked = rank_subtitles(results)
            log_candidates("episódio", ranked)

            if ranked:
                return ranked

    # ------------------------------------------------------------
    # 2. Para filmes, ou se a identificação da série falhar,
    #    tenta primeiro pelo moviehash.
    # ------------------------------------------------------------
    params = {
        "moviehash": movie_hash,
        "languages": language,
        "order_by": "download_count",
        "order_direction": "desc",
        "page": 1,
        "per_page": min(max_results, 100),
    }

    logger.info("Busca por moviehash: params=%s", params)

    response = session.get(
        f"{API_BASE}/subtitles",
        params=params,
        timeout=30,
    )

    if response.status_code >= 400:
        raise SubtitleError(
            api_error(
                response,
                "Falha na busca por moviehash",
            )
        )

    raw_data = response.json().get("data", [])
    logger.debug("Resposta bruta (moviehash, %d itens): %s", len(raw_data), raw_data)

    results = parse_subtitles(raw_data)
    ranked = rank_subtitles(results)
    log_candidates("moviehash", ranked)

    if ranked:
        return ranked

    # ------------------------------------------------------------
    # 3. Último fallback: busca pelo título identificado pelo
    #    GuessIt, e NÃO pelo nome inteiro do arquivo.
    # ------------------------------------------------------------
    query = info.get("title") or video.stem

    params = {
        "query": query,
        "languages": language,
        "order_by": "download_count",
        "order_direction": "desc",
        "page": 1,
        "per_page": min(max_results, 100),
    }

    logger.info("Busca por nome: params=%s", params)

    response = session.get(
        f"{API_BASE}/subtitles",
        params=params,
        timeout=30,
    )

    if response.status_code >= 400:
        raise SubtitleError(
            api_error(
                response,
                "Falha na busca por nome",
            )
        )

    raw_data = response.json().get("data", [])
    logger.debug("Resposta bruta (nome, %d itens): %s", len(raw_data), raw_data)

    results = parse_subtitles(raw_data)
    ranked = rank_subtitles(results)
    log_candidates("nome", ranked)

    return ranked


def parse_subtitles(items: list[dict[str, Any]]) -> list[Subtitle]:
    results: list[Subtitle] = []
    for item in items:
        attrs = item.get("attributes", {})
        files = attrs.get("files") or []
        if not files:
            continue
        file_info = files[0]
        language = (attrs.get("language") or "").lower()
        if language != "pt-br":
            continue
        try:
            file_id = int(file_info["file_id"])
        except (KeyError, TypeError, ValueError):
            continue

        # Extrair Título (Tenta várias chaves possíveis que a API retorna)
        feat = attrs.get("feature_details", {})
        title_str = (
            feat.get("title")
            or feat.get("movie_name")
            or feat.get("parent_title")
            or attrs.get("movie_name")
            or ""
        )

        results.append(
            Subtitle(
                file_id=file_id,
                subtitle_id=str(attrs.get("subtitle_id") or item.get("id") or ""),
                language=language,
                release=str(attrs.get("release") or ""),
                title=str(title_str) if title_str else None,  # <--- Enviando título
                download_count=int(attrs.get("download_count") or 0),
                new_download_count=int(attrs.get("new_download_count") or 0),
                rating=(
                    float(attrs["ratings"])
                    if attrs.get("ratings") is not None
                    else None
                ),
                hearing_impaired=bool(attrs.get("hearing_impaired")),
                ai_translated=bool(attrs.get("ai_translated")),
                machine_translated=bool(attrs.get("machine_translated")),
                from_trusted=bool(attrs.get("from_trusted")),
                moviehash_match=bool(attrs.get("moviehash_match")),
                file_name=str(file_info.get("file_name") or ""),
                fps=float(attrs["fps"]) if attrs.get("fps") is not None else None,
            )
        )
    return results


def rank_subtitles(results: list[Subtitle]) -> list[Subtitle]:
    """
    Prioriza correspondência exata com o vídeo e fontes confiáveis.
    Popularidade é usada apenas como critério secundário.
    """
    return sorted(
        results,
        key=lambda s: (
            s.moviehash_match,
            s.from_trusted,
            s.hearing_impaired is False,
            s.ai_translated is False,
            s.machine_translated is False,
            s.new_download_count,
            s.download_count,
            s.rating if s.rating is not None else -1,
        ),
        reverse=True,
    )


def log_candidates(stage: str, candidates: list[Subtitle]) -> None:
    """Registra, de forma legível, as candidatas encontradas em uma etapa
    da busca (já parseadas e ranqueadas). Complementa o JSON bruto que
    já foi salvo em nível DEBUG logo antes desta chamada."""
    if not candidates:
        logger.info("Etapa '%s': nenhuma candidata PT-BR encontrada.", stage)
        return
    logger.info(
        "Etapa '%s': %d candidata(s) PT-BR encontrada(s).", stage, len(candidates)
    )
    for i, s in enumerate(candidates, start=1):
        logger.info(
            "  [%d] file_id=%s release=%r moviehash_match=%s trusted=%s "
            "downloads=%s rating=%s hi=%s ai=%s mt=%s",
            i,
            s.file_id,
            s.release or s.file_name,
            s.moviehash_match,
            s.from_trusted,
            s.download_count,
            s.rating,
            s.hearing_impaired,
            s.ai_translated,
            s.machine_translated,
        )


def download_subtitle(
    session: requests.Session,
    subtitle: Subtitle,
    destination: Path,
) -> None:
    payload: dict[str, Any] = {
        "file_id": subtitle.file_id,
        "sub_format": "srt",
        "file_name": destination.name,
    }
    response = session.post(f"{API_BASE}/download", json=payload, timeout=30)
    if response.status_code >= 400:
        raise SubtitleError(
            api_error(response, "Falha ao solicitar download da legenda")
        )

    link = response.json().get("link")
    if not link:
        raise SubtitleError("A API não retornou o link temporário da legenda.")

    content = session.get(link, timeout=60)
    if content.status_code >= 400:
        raise SubtitleError(api_error(content, "Falha ao baixar o arquivo de legenda"))

    if not content.content:
        raise SubtitleError("A legenda retornada está vazia.")

    # Escreve primeiro em arquivo temporário e só então substitui o .srt.
    # Isso evita deixar uma legenda corrompida se a transferência falhar.
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb",
        delete=False,
        dir=destination.parent,
        prefix=".subtitle-",
        suffix=".tmp",
    ) as temp:
        temp.write(content.content)
        temp_path = Path(temp.name)

    try:
        os.replace(temp_path, destination)
    finally:
        temp_path.unlink(missing_ok=True)


def _format_flags(subtitle: Subtitle) -> str:
    flags = []
    if subtitle.moviehash_match:
        flags.append("match exato")
    if subtitle.from_trusted:
        flags.append("confiável")
    if subtitle.hearing_impaired:
        flags.append("HI")
    if subtitle.ai_translated:
        flags.append("trad. IA")
    if subtitle.machine_translated:
        flags.append("trad. máquina")
    return ", ".join(flags) if flags else "—"


def choose_subtitle_gui(
    video: Path,
    candidates: list[Subtitle],
) -> tuple[Subtitle | None, tk.Tk | None]:  # <--- Atualize o tipo de retorno
    result: dict[str, Subtitle | None] = {"subtitle": None}

    root = tk.Tk()
    root.title(f"{APP_NAME} — Escolher legenda")
    root.geometry("900x420")
    root.minsize(700, 320)

    header = ttk.Label(
        root,
        text=f"Vídeo: {video.name}\nEscolha a legenda PT-BR para baixar:",
        justify="left",
        padding=(10, 10),
    )
    header.pack(anchor="w")

    columns = ("release", "flags", "downloads", "rating")
    tree = ttk.Treeview(root, columns=columns, show="headings", selectmode="browse")
    tree.heading("release", text="Release / arquivo")
    tree.heading("flags", text="Indicadores")
    tree.heading("downloads", text="Downloads")
    tree.heading("rating", text="Avaliação")
    tree.column("release", width=430, anchor="w")
    tree.column("flags", width=220, anchor="w")
    tree.column("downloads", width=100, anchor="center")
    tree.column("rating", width=90, anchor="center")

    for i, subtitle in enumerate(candidates):
        # <--- Adicionando o título na exibição --->
        base_name = subtitle.release or subtitle.file_name or "(sem nome)"
        display_name = (
            f"{subtitle.title} | {base_name}" if subtitle.title else base_name
        )

        tree.insert(
            "",
            "end",
            iid=str(i),
            values=(
                display_name,
                _format_flags(subtitle),
                subtitle.download_count,
                f"{subtitle.rating:.1f}" if subtitle.rating is not None else "—",
            ),
        )

    tree.pack(fill="both", expand=True, padx=10, pady=(0, 10))
    if candidates:
        tree.selection_set("0")
        tree.focus("0")

    button_frame = ttk.Frame(root, padding=(10, 0, 10, 10))
    button_frame.pack(fill="x")

    def confirm(_event: object = None) -> None:
        selection = tree.selection()
        if not selection:
            return
        result["subtitle"] = candidates[int(selection[0])]
        root.quit()  # <--- Modificado: Interrompe o mainloop sem destruir a janela

    def cancel() -> None:
        result["subtitle"] = None
        root.destroy()  # <--- Botão cancelar continua destruindo e abortando

    ttk.Button(button_frame, text="Baixar selecionada", command=confirm).pack(
        side="right"
    )
    ttk.Button(button_frame, text="Cancelar", command=cancel).pack(
        side="right", padx=(0, 8)
    )

    tree.bind("<Double-1>", confirm)
    root.protocol("WM_DELETE_WINDOW", cancel)
    root.mainloop()

    # Se root foi destruída no cancelar, verificamos antes de retornar
    try:
        root.state()
        return result["subtitle"], root
    except tk.TclError:
        return result["subtitle"], None


def notify(title: str, message: str, error: bool = False) -> None:
    try:
        import ctypes

        flags = 0x10 if error else 0x40
        ctypes.windll.user32.MessageBoxW(0, message, title, flags)
    except Exception:
        print(f"{title}: {message}")


def run(video_arg: str) -> int:
    video = Path(video_arg).resolve()
    if not video.exists() or not video.is_file():
        raise SubtitleError(f"Arquivo não encontrado: {video}")
    if video.suffix.lower() not in VIDEO_EXTENSIONS:
        raise SubtitleError(f"Extensão não reconhecida como vídeo: {video.suffix}")

    config = load_config()
    language = config.get("language", "pt-br").lower()
    max_results = int(config.get("max_results", 100))
    subtitle_path = video.with_suffix(".srt")
    movie_hash = opensubtitles_hash(video)

    conn = init_db()
    try:
        session = build_session(config)
        subtitles = search_subtitles(session, video, language, max_results)
        if not subtitles:
            raise SubtitleError("Nenhuma legenda PT-BR encontrada para este vídeo.")

        # Regra solicitada:
        # - sem arquivo .srt com mesmo nome => usa a primeira opção, ignorando histórico;
        # - com .srt existente => pula todas as opções já tentadas para este moviehash.
        existing_subtitle = subtitle_path.exists()
        candidates = (
            [s for s in subtitles if not was_attempted(conn, movie_hash, s.file_id)]
            if existing_subtitle
            else subtitles
        )

        if not candidates:
            raise SubtitleError(
                "Todas as legendas encontradas já foram tentadas para este vídeo. "
                "Apague o .srt se quiser reiniciar a seleção do topo da lista."
            )

        logger.info(
            "Abrindo janela de seleção com %d candidata(s) (existing_subtitle=%s).",
            len(candidates),
            existing_subtitle,
        )
        # Substitua a chamada antiga a choose_subtitle_gui por esta:
        subtitle, root = choose_subtitle_gui(video, candidates)

        if subtitle is None:
            logger.info("Usuário cancelou a seleção de legenda.")
            return 3

        logger.info(
            "Legenda escolhida: file_id=%s release=%r",
            subtitle.file_id,
            subtitle.release or subtitle.file_name,
        )

        download_subtitle(session, subtitle, subtitle_path)
        mark_attempted(conn, movie_hash, subtitle)

        logger.info("Download concluído com sucesso: %s", subtitle_path)

        # <--- Lógica da nova interface de Sucesso com contagem --->
        if root is not None:
            # Apaga a tabela e botões da janela
            for widget in root.winfo_children():
                widget.destroy()

            msg_label = ttk.Label(
                root,
                text="Legenda baixada com sucesso!\nFechando a janela em 2...",
                font=("Segoe UI", 14),
                justify="center",
            )
            msg_label.pack(expand=True)
            root.update()  # Força a tela a renderizar

            # Faz a contagem descrescente
            for i in [1]:
                time.sleep(1)
                msg_label.config(
                    text=f"Legenda baixada com sucesso!\nFechando a janela em {i}..."
                )
                root.update()

            time.sleep(1)
            root.destroy()

        return 0
    finally:
        conn.close()


def main() -> int:
    setup_logging()

    if len(sys.argv) != 2:
        logger.error("Uso incorreto: argv=%s", sys.argv)
        notify(APP_NAME, "Uso: BaixarLegenda.exe <arquivo_de_video>", error=True)
        return 2
    try:
        return run(sys.argv[1])
    except requests.RequestException as exc:
        logger.exception("Erro de comunicação com o OpenSubtitles")
        notify(
            APP_NAME, f"Erro de comunicação com o OpenSubtitles:\n\n{exc}", error=True
        )
        return 1
    except SubtitleError as exc:
        logger.error("Erro esperado: %s", exc)
        notify(APP_NAME, str(exc), error=True)
        return 1
    except Exception as exc:
        logger.exception("Erro inesperado")
        notify(APP_NAME, f"Erro inesperado:\n\n{type(exc).__name__}: {exc}", error=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
