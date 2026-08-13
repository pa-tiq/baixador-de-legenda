from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
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


class SubtitleError(RuntimeError):
    pass


@dataclass
class Subtitle:
    file_id: int
    subtitle_id: str
    language: str
    release: str
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
    Identifica o vídeo priorizando o metadado 'Título'.

    Ordem:
        1. Metadado Title
        2. Nome do arquivo
    """

    metadata_title = get_video_title(video)

    if metadata_title:
        info = guessit(metadata_title)

        # Guardamos também o texto original utilizado.
        info["_source"] = "metadata"
        info["_query_title"] = metadata_title

        return info

    info = guessit(video.name)
    info["_source"] = "filename"
    info["_query_title"] = video.stem

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

    print(f"Identificação ({source}): {query_title}")
    print(f"Informações detectadas: {info}")

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

            results = parse_subtitles(response.json().get("data", []))

            if results:
                return rank_subtitles(results)

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

    results = parse_subtitles(response.json().get("data", []))

    if results:
        return rank_subtitles(results)

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

    results = parse_subtitles(response.json().get("data", []))

    return rank_subtitles(results)


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
        results.append(
            Subtitle(
                file_id=file_id,
                subtitle_id=str(attrs.get("subtitle_id") or item.get("id") or ""),
                language=language,
                release=str(attrs.get("release") or ""),
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

        subtitle = candidates[0]
        download_subtitle(session, subtitle, subtitle_path)
        mark_attempted(conn, movie_hash, subtitle)

        notify(
            APP_NAME,
            f"Legenda baixada com sucesso!\n\n{subtitle_path.name}\n\n"
            f"Release: {subtitle.release or '(não informado)'}",
        )
        return 0
    finally:
        conn.close()


def main() -> int:
    if len(sys.argv) != 2:
        notify(APP_NAME, "Uso: BaixarLegenda.exe <arquivo_de_video>", error=True)
        return 2
    try:
        return run(sys.argv[1])
    except requests.RequestException as exc:
        notify(
            APP_NAME, f"Erro de comunicação com o OpenSubtitles:\n\n{exc}", error=True
        )
        return 1
    except SubtitleError as exc:
        notify(APP_NAME, str(exc), error=True)
        return 1
    except Exception as exc:
        notify(APP_NAME, f"Erro inesperado:\n\n{type(exc).__name__}: {exc}", error=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
