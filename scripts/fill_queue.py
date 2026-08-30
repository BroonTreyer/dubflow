"""Abastece a fila sozinho, ate cada canal ter N dias de publicacao agendada.

O alvo NAO e "baixar X videos por dia" — e manter um HORIZONTE. Cada canal tem um
calendario; enquanto o dele estiver abaixo do alvo, o sistema puxa episodio novo
das fontes autorizadas. Quando alcanca, para de puxar sozinho. Assim a fila nunca
seca nem estoura disco a toa.

    previa (nao enfileira nada):
        .venv\\Scripts\\python.exe -m scripts.fill_queue

    enfileira de verdade:
        .venv\\Scripts\\python.exe -m scripts.fill_queue --apply

    alvo e teto por rodada:
        .venv\\Scripts\\python.exe -m scripts.fill_queue --apply --days 365 --max 5

So puxa de app/sources.py (canais com politica de cortes PUBLICADA), e respeita a
janela de espera de cada fonte. Episodio novo demais e pulado, nao baixado.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from app import db, sources
from app.config import settings
from app.pipeline import ingest


def _listar(source: sources.Source, limite: int) -> list[dict]:
    """Lista os videos do canal SEM baixar (modo flat: rapido e barato)."""
    ingest.ensure_js_runtime()
    cmd = [sys.executable, "-m", "yt_dlp", "--flat-playlist", "--dump-json",
           *ingest.yt_args(), "--playlist-end", str(limite), source.url]
    out = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    itens = []
    for linha in out.stdout.splitlines():
        try:
            itens.append(json.loads(linha))
        except json.JSONDecodeError:
            continue
    return itens


def _idade_horas(video_id: str) -> float | None:
    """Idade do video. O modo flat nao traz data, entao consulta so o que passou
    nos filtros baratos — a janela de espera das fontes depende disto."""
    ingest.ensure_js_runtime()
    out = subprocess.run(
        [sys.executable, "-m", "yt_dlp", "--no-playlist", "--dump-single-json",
         *ingest.yt_args(), "--skip-download",
         f"https://www.youtube.com/watch?v={video_id}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if out.returncode != 0:
        return None
    try:
        info = json.loads(out.stdout)
    except json.JSONDecodeError:
        return None
    ts = info.get("timestamp")
    if ts:
        return (datetime.now(timezone.utc).timestamp() - float(ts)) / 3600
    bruto = info.get("upload_date")  # YYYYMMDD, sem hora
    if not bruto:
        return None
    try:
        d = datetime.strptime(bruto, "%Y%m%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - d).total_seconds() / 3600


def horizonte_dias() -> dict[int, int]:
    """Quantos dias de publicacao ja estao agendados em cada canal ativo."""
    agora = datetime.now(timezone.utc)
    out: dict[int, int] = {}
    for ch in db.list_channels(only_active=True):
        limite = db.channel_scheduling_horizon(ch["id"])
        if not limite:
            out[ch["id"]] = 0
            continue
        try:
            fim = datetime.fromisoformat(str(limite))
        except (TypeError, ValueError):
            out[ch["id"]] = 0
            continue
        if fim.tzinfo is None:
            fim = fim.replace(tzinfo=timezone.utc)
        out[ch["id"]] = max(0, (fim - agora).days)
    return out


def episodios_faltando(alvo_dias: int) -> tuple[int, dict[int, int]]:
    """Quantos episodios ainda faltam para todo canal chegar ao alvo.

    Usa a media real de cortes por episodio deste acervo — chutar 10 faria o
    sistema puxar de menos ou de mais.
    """
    horizontes = horizonte_dias()
    canais = {c["id"]: c for c in db.list_channels(only_active=True)}

    with db.connect() as conn:
        n_ep = conn.execute("SELECT COUNT(DISTINCT episode_id) FROM clips").fetchone()[0]
        n_cl = conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0]
    por_episodio = (n_cl / n_ep) if n_ep else 7.0

    cortes_faltando = 0
    for cid, dias in horizontes.items():
        falta_dias = max(0, alvo_dias - dias)
        ppd = canais[cid].get("posts_per_day") or settings.distribute_per_day
        cortes_faltando += falta_dias * ppd
    return int(round(cortes_faltando / max(por_episodio, 1))), horizontes


def rodar(alvo_dias: int, maximo: int, aplicar: bool) -> int:
    faltam, horizontes = episodios_faltando(alvo_dias)
    print(f"alvo: {alvo_dias} dias de fila por canal")
    for cid, dias in sorted(horizontes.items()):
        marca = "ok" if dias >= alvo_dias else f"faltam {alvo_dias - dias}d"
        print(f"  canal {cid}: {dias} dia(s) agendado(s)  [{marca}]")
    print(f"\nepisodios estimados para fechar o alvo: {faltam}")

    if faltam <= 0:
        print("Nada a fazer: todos os canais ja alcancaram o alvo.")
        return 0

    cota = min(maximo, faltam)
    print(f"puxando ate {cota} nesta rodada (teto --max {maximo})\n")

    ja_temos = {e["source_url"] for e in db.list_episodes()}
    enfileirados = 0

    for src in sources.enabled_sources():
        if enfileirados >= cota:
            break
        print(f"== {src.name}  (espera {src.min_age_hours}h — {src.policy_url})")
        for item in _listar(src, limite=40):
            if enfileirados >= cota:
                break
            vid = item.get("id")
            titulo = (item.get("title") or "")[:46]
            minutos = int(item.get("duration") or 0) // 60
            views = item.get("view_count") or 0
            url = f"https://www.youtube.com/watch?v={vid}"

            if url in ja_temos:
                continue
            if not (src.min_minutes <= minutos <= src.max_minutes):
                continue
            if views < src.min_views:
                continue

            idade = _idade_horas(vid)
            if idade is None:
                print(f"   ? {titulo} — sem data, pulado por seguranca")
                continue
            if idade < src.min_age_hours:
                print(f"   . {titulo} — {idade:.0f}h de idade, aguarda {src.min_age_hours}h")
                continue

            if aplicar:
                db.create_episode(url, src.license)
                print(f"   + {titulo}  ({minutos}min, {views:,}v) ENFILEIRADO")
            else:
                print(f"   ~ {titulo}  ({minutos}min, {views:,}v) [previa]")
            ja_temos.add(url)
            enfileirados += 1

    print(f"\n{enfileirados} episodio(s) {'enfileirados' if aplicar else 'na previa'}.")
    if not aplicar and enfileirados:
        print("Rode com --apply para enfileirar de verdade.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--days", type=int, default=settings.queue_target_days,
                   help="dias de fila desejados por canal")
    p.add_argument("--max", type=int, default=settings.queue_max_per_run,
                   help="teto de episodios por rodada (protege disco e GPU)")
    p.add_argument("--apply", action="store_true", help="enfileira (sem isto e previa)")
    args = p.parse_args()
    db.init_db()
    return rodar(args.days, args.max, args.apply)


if __name__ == "__main__":
    sys.exit(main())
