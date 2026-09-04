"""Distribuicao automatica dos cortes para os canais certos.

Fluxo, depois que um episodio termina de cortar:

  1. classifica o episodio em UM segmento, escolhendo entre os nichos que voce
     REALMENTE tem canais (nao um taxonomico fixo). Sem confianca suficiente ou
     sem canal para o segmento -> nao faz nada (fica manual). Nunca chuta.
  2. roteia os cortes: cada corte vai para UM unico canal daquele segmento, em
     rodizio entre os canais (um corte nunca vai para duas contas).
  3. agenda em gotejamento: X cortes por dia por canal, continuando DEPOIS do que
     o canal ja tinha agendado — a fila estica por dias/meses ate acabar.

`plan_schedule` e o roteamento sao puros e deterministas (testaveis sem rede); so
a classificacao usa a API da Claude, e e injetavel para os testes.
"""

from __future__ import annotations

import json
import logging
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from app import db
from app.config import TARGET_LANGUAGES, settings
from app.pipeline import archive, llm

log = logging.getLogger(__name__)

# Sentinela para "nenhum segmento serve". String simples em vez de um schema com
# tipo nulavel: o structured output desta base so foi exercitado com tipos simples
# (ver GENRE_SCHEMA/CLIP_SCHEMA), entao um `type: [..., null]` seria risco de a API
# recusar e a classificacao falhar em silencio para sempre.
_NONE_TOKEN = "NENHUM"

CLASSIFY_PROMPT = (
    "Voce classifica um video em UM segmento de conteudo, escolhendo APENAS entre "
    "estes segmentos disponiveis:\n{niches}\n\n"
    "Responda em `segment` com o segmento que melhor descreve o video (copie "
    f"exatamente como esta escrito na lista) e em `confidence` um numero de 0 a 1. "
    f"Se nenhum servir bem, responda segment com a palavra exata {_NONE_TOKEN}. "
    "Nunca invente um segmento fora da lista."
)

_CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "segment": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["segment", "confidence"],
    "additionalProperties": False,
}


# Mercado do canal -> idioma da legenda que ele recebe. Um episodio so vai para
# canais cujo idioma bate com o idioma em que ele foi legendado (lang_dst).
# Derivado da fonte unica em config para nao divergir do que a web oferece.
MARKET_LANG = {spec["market"]: code for code, spec in TARGET_LANGUAGES.items()}


def _slug(value: str | None) -> str:
    """Normaliza para comparar segmento do episodio com nicho do canal.

    Remove acentos alem de baixar caixa/pontuacao: sem isso 'noticias' (como a IA
    costuma devolver) nao casaria com o nicho 'Noticias' escrito com acento.
    """
    base = archive.slugify(value or "")
    return unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode("ascii")


def _base_lang(code: str | None) -> str:
    return (code or "").strip().lower().replace("_", "-").split("-")[0]


def _channel_lang(channel: dict[str, Any]) -> str:
    """Idioma (base) do canal, derivado do mercado. Mercado desconhecido -> pt-BR."""
    market = (channel.get("market") or "").strip().upper()
    return _base_lang(MARKET_LANG.get(market, settings.target_lang))


# ------------------------------------------------------------------- classificacao


def _text_sample(episode: dict[str, Any], max_lines: int = 50) -> str:
    """Algumas linhas da transcricao (traduzida ou original) para dar contexto."""
    paths = episode.get("paths") or {}
    for key in ("translated", "transcript"):
        p = paths.get(key)
        if not p:
            continue
        try:
            data = json.loads(Path(p).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        segs = data.get("segments") if isinstance(data, dict) else data
        if not isinstance(segs, list):
            continue
        texts = [
            (s.get("text") or "").strip()
            for s in segs
            if isinstance(s, dict) and (s.get("text") or "").strip()
        ]
        if texts:
            return "\n".join(texts[:max_lines])
    return ""


def classify_segment(episode: dict[str, Any], niches: list[str]) -> str | None:
    """Escolhe um dos `niches` para o episodio, ou None se nao houver casamento
    confiavel. O retorno e sempre um item de `niches` (casado por slug) ou None."""
    if not llm.providers() or not niches:
        return None

    system = CLASSIFY_PROMPT.format(niches="\n".join(f"- {n}" for n in niches))
    user = (
        f"Titulo: {episode.get('title')}\n"
        f"Canal de origem: {episode.get('channel')}\n\n"
        f"Amostra da transcricao:\n{_text_sample(episode)}"
    )
    try:
        r = llm.call_json(llm.ROLE_SCAN, system, user, _CLASSIFY_SCHEMA, max_tokens=300)
        if r.refusal or not r.text.strip():
            return None
        data = r.json()
    except Exception as exc:  # noqa: BLE001 — classificacao e best-effort
        log.warning("classificacao de segmento falhou: %s", exc)
        return None

    seg = (data.get("segment") or "").strip()
    try:
        conf = float(data.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    if not seg or seg.upper() == _NONE_TOKEN or conf < settings.distribute_min_confidence:
        return None
    # So aceita se casar com um nicho que realmente existe (evita alucinar rotulo).
    for n in niches:
        if _slug(n) == _slug(seg):
            return n
    return None


# ------------------------------------------------------------------- roteamento


def assign_round_robin(clip_ids: list[int], channel_ids: list[int]) -> dict[int, list[int]]:
    """Cada corte para UM canal, em rodizio. Um corte nunca vai para dois canais."""
    out: dict[int, list[int]] = {cid: [] for cid in channel_ids}
    if not channel_ids:
        return out
    for i, clip_id in enumerate(clip_ids):
        out[channel_ids[i % len(channel_ids)]].append(clip_id)
    return out


# ------------------------------------------------------------------- agendamento (drip)


def _parse_iso(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _day_slot_times(per_day: int) -> list[tuple[int, int]]:
    """Horarios (hora, minuto) que dividem a janela de postagem em `per_day` slots."""
    start, end = settings.distribute_start_hour, settings.distribute_end_hour
    per_day = max(1, per_day)
    if per_day == 1:
        return [(min(start, 23), 0)]
    span = max(1, end - start)
    slots: list[tuple[int, int]] = []
    for k in range(per_day):
        h_float = start + span * k / (per_day - 1)
        h = int(h_float)
        m = int(round((h_float - h) * 60))
        if m >= 60:
            h, m = h + 1, m - 60
        slots.append((min(h, 23), min(m, 59)))
    return slots


def _iter_slots(anchor: datetime, per_day: int):
    """Gera horarios de postagem estritamente depois de `anchor`, `per_day` por dia."""
    times = _day_slot_times(per_day)
    day = datetime(anchor.year, anchor.month, anchor.day, tzinfo=timezone.utc)
    while True:
        for (h, m) in times:
            dt = day.replace(hour=h, minute=m, second=0, microsecond=0)
            if dt > anchor:
                yield dt
        day = day + timedelta(days=1)


def plan_schedule(assignments: dict[int, list[int]], horizons: dict[int, str | None],
                  per_day_map: dict[int, int], now: datetime) -> list[tuple[int, int, str]]:
    """Traduz as atribuicoes em (clip_id, channel_id, scheduled_at ISO).

    Cada canal goteja no seu proprio ritmo, comecando depois do que ja tinha
    agendado (horizons) e nunca no passado.
    """
    plan: list[tuple[int, int, str]] = []
    for channel_id, clip_ids in assignments.items():
        if not clip_ids:
            continue
        per_day = max(1, int(per_day_map.get(channel_id) or settings.distribute_per_day))
        anchor = now
        horizon = horizons.get(channel_id)
        if horizon:
            hd = _parse_iso(horizon)
            if hd:
                anchor = max(now, hd)
        slots = _iter_slots(anchor, per_day)
        for clip_id in clip_ids:
            dt = next(slots)
            plan.append((clip_id, channel_id, dt.isoformat(timespec="seconds")))
    return plan


# ------------------------------------------------------------------- orquestracao


def distribute_episode(episode_id: int,
                       classifier: Callable[[dict, list[str]], str | None] | None = None) -> dict:
    """Classifica, roteia e agenda os cortes novos deste episodio.

    Idempotente: so agenda cortes que ainda nao tem publicacao, entao rodar de
    novo nao duplica. Devolve um resumo com o que aconteceu.
    """
    episode = db.get_episode(episode_id)
    if episode is None:
        raise ValueError(f"episodio {episode_id} nao existe")

    channels = [c for c in db.list_channels(only_active=True) if (c.get("niche") or "").strip()]
    if not channels:
        return {"status": "sem_canais", "scheduled": 0}
    niches = sorted({c["niche"].strip() for c in channels})

    # Override manual do segmento vence a classificacao.
    segment = (episode.get("segment") or "").strip() or None
    if not segment and len(niches) == 1:
        # Nicho unico: nao ha o que decidir, e perguntar cria um jeito de falhar.
        # `classify_segment` devolve None quando o episodio nao casa com nenhum
        # rotulo — de proposito, para nao alucinar. Com uma opcao so, isso vira
        # uma recusa que ninguem ve: o episodio fica `done` com zero posts e a
        # distribuicao e best-effort, entao nem reprova. Foi o que prendeu os
        # episodios 11, 13, 14 e 15 (mais de 200 cortes) em 03-04/09/2026.
        segment = niches[0]
        db.update_episode(episode_id, segment=segment)
        log.info("[ep %s] nicho unico ('%s'): classificacao dispensada", episode_id, segment)
    if not segment:
        classify = classifier or classify_segment
        segment = classify(episode, niches)
        if segment:
            db.update_episode(episode_id, segment=segment)
    if not segment:
        return {"status": "nao_classificado", "scheduled": 0}

    seg_slug = _slug(segment)
    ep_lang = _base_lang(episode.get("lang_dst") or settings.target_lang)
    # Casa por segmento E por idioma: um episodio pt-BR nunca vai para um canal US.
    matching = sorted(
        [c for c in channels
         if _slug(c["niche"]) == seg_slug and _channel_lang(c) == ep_lang],
        key=lambda c: c["id"],
    )
    if not matching:
        return {"status": "sem_canal_para_segmento", "segment": segment,
                "lang": ep_lang, "scheduled": 0}

    clips = db.clips_ready_without_posts(episode_id)
    if not clips:
        return {"status": "sem_cortes_novos", "segment": segment, "scheduled": 0}

    channel_ids = [c["id"] for c in matching]
    assignments = assign_round_robin([c["id"] for c in clips], channel_ids)
    horizons = {cid: db.channel_scheduling_horizon(cid) for cid in channel_ids}
    per_day_map = {c["id"]: c.get("posts_per_day") or settings.distribute_per_day for c in matching}
    platform_of = {c["id"]: c["platform"] for c in matching}

    plan = plan_schedule(assignments, horizons, per_day_map, datetime.now(timezone.utc))
    for clip_id, channel_id, scheduled_at in plan:
        db.create_post(clip_id, platform_of[channel_id], scheduled_at, "vertical", channel_id)

    log.info(
        "[ep %s] distribuido: segmento '%s', %d canal(is), %d corte(s) agendado(s)",
        episode_id, segment, len(matching), len(plan),
    )
    return {"status": "ok", "segment": segment, "channels": len(matching),
            "scheduled": len(plan)}
