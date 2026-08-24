"""Orquestrador: leva um episodio de link colado ate cortes prontos e arquivados.

Cada etapa grava o estado no banco antes de comecar, entao a UI mostra o
progresso real e um episodio que falha diz exatamente onde parou.
"""

from __future__ import annotations

import json
import logging
import traceback
from pathlib import Path
from typing import Any

from app import db
from app.config import settings
from app.pipeline import archive, clips, ingest, subtitles, transcribe, translate

log = logging.getLogger(__name__)


def _set(episode_id: int, status: str, progress: float, **extra: Any) -> None:
    db.update_episode(episode_id, status=status, progress=round(progress, 3), **extra)
    log.info("[ep %s] %s (%.0f%%)", episode_id, status, progress * 100)


def process_episode(episode_id: int) -> dict[str, Any]:
    episode = db.get_episode(episode_id)
    if episode is None:
        raise ValueError(f"episodio {episode_id} nao existe")

    work_dir = settings.episode_dir(episode_id)
    paths: dict[str, str] = dict(episode.get("paths") or {})

    try:
        # ---------------------------------------------------------- 1. ingest
        _set(episode_id, "downloading", 0.02)
        info = ingest.probe(episode["source_url"])
        db.update_episode(
            episode_id,
            video_id=info["video_id"],
            title=info["title"],
            channel=info["channel"],
            duration=info["duration"],
            meta=info,
        )

        video_path = ingest.download(episode["source_url"], work_dir)
        paths["source_video"] = str(video_path)
        _set(episode_id, "downloading", 0.15, paths=paths)

        audio_path = ingest.extract_audio(video_path)
        paths["audio"] = str(audio_path)

        # ------------------------------------------------------ 2. transcricao
        _set(episode_id, "transcribing", 0.18, paths=paths)
        result = transcribe.transcribe(audio_path)
        segments = result["segments"]
        if not segments:
            raise RuntimeError("transcricao vazia — o video tem fala audivel?")

        transcript_path = work_dir / "transcript.json"
        transcript_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        paths["transcript"] = str(transcript_path)
        db.update_episode(episode_id, lang_src=result["language"], paths=paths)
        _set(episode_id, "transcribing", 0.45)

        # -------------------------------------------------------- 3. traducao
        _set(episode_id, "translating", 0.46)
        meta = {
            "title": info["title"],
            "channel": info["channel"],
            "lang_src": result["language"],
        }
        glossary = _glossary_for(info["channel"])

        if settings.use_batch_api:
            translated = translate.translate_segments_batch(segments, meta, glossary)
        else:
            translated = translate.translate_segments(
                segments,
                meta,
                glossary,
                on_progress=lambda frac, label: _set(
                    episode_id, "translating", 0.46 + frac * 0.24
                ),
            )

        translated_path = work_dir / "translated.json"
        translated_path.write_text(
            json.dumps(translated, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        paths["translated"] = str(translated_path)

        # Segmentos que voltaram sem traducao ficam com o texto original. Registrar
        # a contagem deixa isso visivel no painel em vez de virar surpresa na tela.
        untranslated = sum(1 for seg in translated if seg.get("untranslated"))
        if untranslated:
            db.update_episode(
                episode_id,
                meta={**info, "untranslated_segments": untranslated,
                      "total_segments": len(translated)},
            )
            log.warning("[ep %s] %d segmentos sem traducao", episode_id, untranslated)

        # -------------------------------------------------------- 4. legendas
        _set(episode_id, "subtitling", 0.72, paths=paths)
        srt_path = subtitles.write_srt(translated, work_dir / "legenda_ptbr.srt")
        ass_path = subtitles.write_ass(translated, work_dir / "legenda_ptbr.ass")
        paths["srt"] = str(srt_path)
        paths["ass"] = str(ass_path)

        if settings.burn_full_episode:
            burned = subtitles.burn(video_path, ass_path, work_dir / "episodio_legendado.mp4")
            paths["episode_burned"] = str(burned)
        _set(episode_id, "subtitling", 0.78, paths=paths)

        # ---------------------------------------------------------- 5. cortes
        _set(episode_id, "clipping", 0.80)
        selected = clips.select_clips(translated, meta)
        clip_ids = db.replace_clips(episode_id, selected)

        clip_dir = work_dir / "clips"
        clip_dir.mkdir(exist_ok=True)
        for i, (clip_id, clip) in enumerate(zip(clip_ids, selected)):
            try:
                out = clips.render_clip(
                    video_path,
                    translated,
                    clip,
                    # O id do episodio entra no nome do arquivo: sem isso todos os
                    # episodios tem "corte_01.mp4" e a rota /media, que resolve por
                    # nome, serve o corte de outro episodio.
                    clip_dir / f"ep{episode_id:05d}_corte_{i + 1:02d}.mp4",
                    work_dir,
                )
                db.update_clip(clip_id, path=str(out), status="ready")
            except RuntimeError as exc:
                log.error("[ep %s] corte %d falhou: %s", episode_id, i + 1, exc)
                db.update_clip(clip_id, status="failed")
            _set(episode_id, "clipping", 0.80 + (i + 1) / max(len(selected), 1) * 0.12)

        # --------------------------------------------------------- 6. arquivo
        _set(episode_id, "archiving", 0.94)
        episode = db.get_episode(episode_id)
        archived = archive.archive_episode(
            episode,
            {
                "episodio": Path(paths.get("episode_burned") or paths["source_video"]),
                "legenda_ptbr": Path(paths["srt"]),
                "transcricao": Path(paths["translated"]),
            },
        )
        paths["archive_dir"] = str(archived)

        paths = _cleanup(work_dir, paths)
        _set(episode_id, "done", 1.0, paths=paths, error=None)
        return db.get_episode(episode_id)

    except Exception as exc:  # noqa: BLE001 — o worker precisa registrar qualquer falha
        detail = f"{type(exc).__name__}: {exc}"
        log.error("[ep %s] falhou: %s\n%s", episode_id, detail, traceback.format_exc())
        db.update_episode(episode_id, status="failed", error=detail, paths=paths)
        raise


def _load_artifacts(episode: dict[str, Any]) -> tuple[Path, list[dict[str, Any]]]:
    """Recupera video de origem e transcricao traduzida de um episodio concluido."""
    paths = episode.get("paths") or {}
    video = paths.get("source_video")
    translated = paths.get("translated")

    if not video or not Path(video).exists():
        raise RuntimeError(
            "video de origem indisponivel. Ele foi apagado apos o arquivamento "
            "(DELETE_SOURCE_AFTER_ARCHIVE); reprocesse o episodio para baixa-lo de novo."
        )
    if not translated or not Path(translated).exists():
        raise RuntimeError("transcricao traduzida indisponivel; reprocesse o episodio.")

    return Path(video), json.loads(Path(translated).read_text(encoding="utf-8"))


def burn_episode(episode_id: int) -> Path:
    """Queima a legenda no episodio inteiro, sob demanda.

    Fica fora do fluxo padrao porque é a etapa mais cara: re-encoda o video
    completo, o que leva dezenas de minutos numa palestra de 1h.
    """
    episode = db.get_episode(episode_id)
    if episode is None:
        raise ValueError(f"episodio {episode_id} nao existe")

    video, translated = _load_artifacts(episode)
    paths = dict(episode.get("paths") or {})
    work_dir = settings.episode_dir(episode_id)

    _set(episode_id, "burning", 0.1)
    ass_path = subtitles.write_ass(translated, work_dir / "legenda_ptbr.ass")
    saida = subtitles.burn(video, ass_path, work_dir / "episodio_legendado.mp4")

    paths["ass"] = str(ass_path)
    paths["episode_burned"] = str(saida)
    _set(episode_id, "done", 1.0, paths=paths, error=None)
    return saida


def rerender_clips(episode_id: int) -> int:
    """Refaz os cortes ja selecionados, sem repetir transcricao nem traducao.

    Serve para aplicar mudancas de estilo de legenda sem gastar API de novo — os
    trechos escolhidos continuam valendo, so o video e refeito.
    """
    episode = db.get_episode(episode_id)
    if episode is None:
        raise ValueError(f"episodio {episode_id} nao existe")

    video, translated = _load_artifacts(episode)
    existentes = db.list_clips(episode_id)
    if not existentes:
        raise RuntimeError("este episodio nao tem cortes para refazer")

    work_dir = settings.episode_dir(episode_id)
    clip_dir = work_dir / "clips"
    clip_dir.mkdir(exist_ok=True)

    _set(episode_id, "clipping", 0.8)
    refeitos = 0
    for i, clip in enumerate(existentes):
        destino = clip_dir / f"ep{episode_id:05d}_corte_{clip['idx'] + 1:02d}.mp4"
        try:
            clips.render_clip(video, translated, clip, destino, work_dir)
            db.update_clip(clip["id"], path=str(destino), status="ready")
            refeitos += 1
        except RuntimeError as exc:
            log.error("[ep %s] corte %d falhou no re-render: %s", episode_id, i + 1, exc)
            db.update_clip(clip["id"], status="failed")
        _set(episode_id, "clipping", 0.8 + (i + 1) / len(existentes) * 0.19)

    _set(episode_id, "done", 1.0, error=None)
    return refeitos


def run_action(episode_id: int, action: str) -> None:
    try:
        if action == "burn":
            burn_episode(episode_id)
        elif action == "rerender_clips":
            rerender_clips(episode_id)
        else:
            raise ValueError(f"acao desconhecida: {action}")
    except Exception as exc:  # noqa: BLE001 — a acao falha, o episodio continua valido
        log.error("[ep %s] acao '%s' falhou: %s", episode_id, action, exc)
        db.update_episode(episode_id, status="done", error=f"{action}: {exc}")


def _cleanup(work_dir: Path, paths: dict[str, str]) -> dict[str, str]:
    """Apaga os intermediarios grandes depois que o episodio foi arquivado.

    Um episodio de 1h deixa ~2-3 GB para tras (`audio.wav` de 110 MB e o MP4 de
    origem). Sem esta etapa, algumas dezenas de episodios enchem o disco e o
    pipeline passa a falhar no download, longe da causa real.

    Os cortes e as legendas ficam — sao o produto. A copia arquivada ja existe.
    """
    removable = ["audio"]
    if settings.delete_source_after_archive:
        removable.append("source_video")

    freed = 0
    for key in removable:
        target = paths.get(key)
        if not target:
            continue
        path = Path(target)
        try:
            if path.exists():
                freed += path.stat().st_size
                path.unlink()
            paths.pop(key, None)
        except OSError as exc:
            log.warning("nao consegui apagar %s: %s", path, exc)

    if freed:
        log.info("limpeza: %.1f MB liberados em %s", freed / 1024**2, work_dir.name)
    return paths


def _glossary_for(channel: str | None) -> dict[str, str]:
    """Glossario por canal, lido de data/glossaries/<canal>.json (opcional).

    E o mecanismo mais barato de manter nomes e jargao consistentes entre
    episodios do mesmo canal.
    """
    if not channel:
        return {}
    path = settings.data_dir / "glossaries" / f"{archive.slugify(channel)}.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.warning("glossario invalido para %s", channel)
        return {}
