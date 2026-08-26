"""Camada de provedores de IA — o pipeline nunca mais para por causa de um teto.

O problema que este modulo existe para resolver: em 25/08/2026 a conta da
Anthropic bateu o teto de gasto e TODAS as chamadas passaram a voltar 400. Como
cada janela de selecao falhava em silencio, quatro episodios foram marcados como
`done` com zero cortes. Um provedor so e um ponto unico de falha.

Aqui cada papel (traduzir, selecionar cortes, classificar) e atendido por uma
lista de provedores. Se o preferido cai, o proximo assume; quando ha varias
tarefas independentes (as janelas de um episodio), elas sao repartidas entre os
provedores saudaveis e rodam ao mesmo tempo.

A distincao que faz tudo funcionar e classificar o erro:

  hard      teto estourado, sem credito, chave invalida. O provedor esta fora —
            marca como caido e vai para o proximo NA HORA. Repetir e desperdicio.
  transient 429, 5xx, rede. Tenta de novo no mesmo provedor com backoff; se
            insistir, cai para o proximo.
  fatal     400 de esquema malformado. O outro provedor falharia igual, entao
            propaga o erro em vez de queimar a chave boa.

O estado de saude e gravado em disco: reiniciar o worker nao pode fazer ele
tentar de novo um provedor que a gente ja sabe que esta bloqueado ate dia 1o.
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import anthropic

from app.config import settings

log = logging.getLogger(__name__)

# Papeis: cada um mapeia para um modelo diferente em cada provedor.
ROLE_TRANSLATE = "translate"
ROLE_CLIP = "clip"
ROLE_SCAN = "scan"

HARD, TRANSIENT, FATAL = "hard", "transient", "fatal"

# Teto do castigo de um provedor bloqueado. Existe porque a data que a API informa
# ("regain access on ...") deixa de valer assim que alguem aumenta o limite da
# conta — sem teto, o sistema fica cego para uma chave que ja voltou.
HARD_COOLDOWN_MAX = 2 * 60 * 60   # 2 horas


class AllProvidersDown(RuntimeError):
    """Nenhum provedor disponivel. E erro de verdade: o episodio deve falhar.

    Nunca engula isto para seguir em frente — foi exatamente o silencio que fez
    quatro episodios terminarem vazios parecendo bem-sucedidos.
    """


@dataclass
class LLMResult:
    """Resposta normalizada: o chamador nao sabe de quem veio."""

    text: str
    usage: dict[str, int]
    provider: str
    model: str
    refusal: bool = False
    truncated: bool = False           # bateu no teto de tokens

    def json(self) -> dict[str, Any]:
        return json.loads(self.text)


# --------------------------------------------------------------- saude dos provedores


def _health_path() -> Path:
    base = getattr(settings, "data_dir", None)
    return (Path(base) if base else Path("data")) / "llm_health.json"


@dataclass
class _State:
    down_until: float = 0.0       # epoch; 0 = saudavel
    reason: str = ""


class _Health:
    """Quem esta de pe, quem esta caido e ate quando. Compartilhado entre threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: dict[str, _State] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(_health_path().read_text(encoding="utf-8"))
            for nome, d in raw.items():
                self._states[nome] = _State(
                    down_until=float(d.get("down_until") or 0),
                    reason=str(d.get("reason") or ""),
                )
        except Exception:  # noqa: BLE001 — arquivo ausente/corrompido comeca limpo
            self._states = {}

    def _save(self) -> None:
        try:
            caminho = _health_path()
            caminho.parent.mkdir(parents=True, exist_ok=True)
            caminho.write_text(
                json.dumps({n: {"down_until": s.down_until, "reason": s.reason}
                            for n, s in self._states.items()}, indent=1),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001 — persistir e conveniencia, nao requisito
            log.debug("nao consegui gravar a saude dos provedores: %s", exc)

    def _st(self, nome: str) -> _State:
        return self._states.setdefault(nome, _State())

    def available(self, nome: str) -> bool:
        with self._lock:
            return self._st(nome).down_until <= time.time()

    def mark_down(self, nome: str, seconds: float, reason: str) -> None:
        with self._lock:
            st = self._st(nome)
            st.down_until = time.time() + max(seconds, 1)
            st.reason = reason[:300]
            self._save()
        log.error("provedor '%s' FORA por %.0f min: %s", nome, seconds / 60, reason[:160])

    def mark_ok(self, nome: str) -> None:
        with self._lock:
            st = self._st(nome)
            voltou = st.down_until > 0
            st.down_until, st.reason = 0.0, ""
            if voltou:
                self._save()
        if voltou:
            log.info("provedor '%s' voltou", nome)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            agora = time.time()
            return {
                nome: {
                    "available": st.down_until <= agora,
                    "down_for_s": max(0, int(st.down_until - agora)),
                    "reason": st.reason,
                }
                for nome, st in self._states.items()
            }

    def clear(self, persist: bool = False) -> None:
        """Zera todos os castigos (usado por clear_health e pelos testes).

        `persist` grava o estado limpo: sem isso o arquivo antigo e relido no
        proximo start e o provedor volta a ser considerado bloqueado.
        """
        with self._lock:
            self._states = {}
            if persist:
                self._save()


_health = _Health()


# ------------------------------------------------------------- classificacao de erro


def _openai_mod():
    try:
        import openai
        return openai
    except ImportError:
        return None


# "You will regain access on 2026-09-01 at 00:00 UTC." — a Anthropic diz a hora
# exata da liberacao; usar isso evita ficar batendo na porta por uma semana.
_REGAIN = re.compile(r"regain access on (\d{4}-\d{2}-\d{2}) at (\d{2}:\d{2}) UTC")

# Marcadores de "a conta acabou", que aparecem num 400/429 comum e por isso nao
# dao para distinguir pelo status HTTP.
_HARD_MARKERS = (
    "usage limit",
    "credit balance",
    "insufficient_quota",
    "insufficient quota",
    "exceeded your current quota",
    "spending limit",
)


def _hard_cooldown(msg: str) -> float:
    """Quanto tempo deixar o provedor de lado, em segundos.

    A data que a API informa ("regain access on ...") e um palpite bom, nao uma
    verdade: ela para de valer no instante em que alguem aumenta o limite da conta.
    Confiar nela cegamente fez o sistema ignorar a Anthropic por 6 dias depois de
    ela ja estar liberada.

    Por isso o teto: mesmo num bloqueio longo, o provedor volta a ser sondado de
    tempos em tempos. Se continuar bloqueado, custa UMA chamada recusada e ele e
    marcado de novo — barato perto de ficar cego para uma chave que voltou.
    """
    m = _REGAIN.search(msg)
    if m:
        try:
            volta = datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}:00+00:00")
            faltam = (volta - datetime.now(timezone.utc)).total_seconds()
            # +1 min de folga para nao acordar exatamente no segundo da virada.
            return max(min(faltam + 60, HARD_COOLDOWN_MAX), 60)
        except ValueError:
            pass
    return min(settings.llm_block_cooldown_minutes * 60, HARD_COOLDOWN_MAX)


def _isinstance_any(exc: BaseException, nomes: tuple[str, ...]) -> bool:
    for mod in (anthropic, _openai_mod()):
        if mod is None:
            continue
        classes = tuple(
            k for k in (getattr(mod, n, None) for n in nomes)
            if isinstance(k, type) and issubclass(k, BaseException)
        )
        if classes and isinstance(exc, classes):
            return True
    return False


def classify(exc: BaseException) -> tuple[str, float]:
    """(categoria, cooldown_em_segundos). Publica porque os testes cobrem isto."""
    msg = str(exc).lower()

    # A conta acabou. Vem como 400 na Anthropic e 429 na OpenAI, entao o status
    # HTTP nao serve para decidir — o texto serve.
    if any(marker in msg for marker in _HARD_MARKERS):
        return HARD, _hard_cooldown(str(exc))

    # Chave invalida ou sem permissao: nao conserta sozinha.
    if _isinstance_any(exc, ("AuthenticationError", "PermissionDeniedError")):
        return HARD, _hard_cooldown(str(exc))

    if _isinstance_any(exc, ("RateLimitError", "APIConnectionError",
                             "APITimeoutError", "InternalServerError")):
        return TRANSIENT, 0.0

    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        if status >= 500 or status in (408, 409, 429):
            return TRANSIENT, 0.0
        if status == 400:
            # Esquema malformado: trocar de provedor so repete o erro.
            return FATAL, 0.0

    return TRANSIENT, 0.0


# ------------------------------------------------------------------------ provedores


@dataclass
class Provider:
    name: str
    models: dict[str, str]
    _client: Any = field(default=None, repr=False)

    def model_for(self, role: str) -> str:
        return self.models[role]

    def call(self, role: str, system: str, user: str, schema: dict[str, Any],
             max_tokens: int, effort: str | None, cache_system: bool) -> LLMResult:
        raise NotImplementedError


class AnthropicProvider(Provider):
    def _cli(self):
        if self._client is None:
            self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        return self._client

    def call(self, role, system, user, schema, max_tokens, effort, cache_system):
        bloco: dict[str, Any] = {"type": "text", "text": system}
        if cache_system:
            bloco["cache_control"] = {"type": "ephemeral"}

        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
        if effort:
            output_config["effort"] = effort

        r = self._cli().messages.create(
            model=self.model_for(role),
            max_tokens=max_tokens,
            system=[bloco],
            output_config=output_config,
            messages=[{"role": "user", "content": user}],
        )
        u = r.usage
        return LLMResult(
            text=next((b.text for b in r.content if b.type == "text"), ""),
            usage={
                "input": getattr(u, "input_tokens", 0) or 0,
                "output": getattr(u, "output_tokens", 0) or 0,
                "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
                "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
            },
            provider=self.name,
            model=self.model_for(role),
            refusal=r.stop_reason == "refusal",
            truncated=r.stop_reason == "max_tokens",
        )


# A Anthropic aceita ate "max"; a OpenAI para em "xhigh".
_EFFORT_OPENAI = {"low": "low", "medium": "medium", "high": "high",
                  "xhigh": "xhigh", "max": "xhigh"}


class OpenAIProvider(Provider):
    def _cli(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(api_key=settings.openai_api_key)
        return self._client

    def call(self, role, system, user, schema, max_tokens, effort, cache_system):
        # cache_system nao existe aqui: a OpenAI cacheia prefixo sozinha.
        kwargs: dict[str, Any] = {}
        if effort:
            kwargs["reasoning_effort"] = _EFFORT_OPENAI.get(effort, "medium")

        r = self._cli().chat.completions.create(
            model=self.model_for(role),
            max_completion_tokens=max_tokens,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "resposta", "schema": schema, "strict": True},
            },
            **kwargs,
        )
        escolha = r.choices[0]
        u = r.usage
        cached = 0
        detalhes = getattr(u, "prompt_tokens_details", None)
        if detalhes is not None:
            cached = getattr(detalhes, "cached_tokens", 0) or 0
        return LLMResult(
            text=escolha.message.content or "",
            usage={
                "input": max((getattr(u, "prompt_tokens", 0) or 0) - cached, 0),
                "output": getattr(u, "completion_tokens", 0) or 0,
                "cache_read": cached,
                "cache_write": 0,
            },
            provider=self.name,
            model=self.model_for(role),
            refusal=getattr(escolha.message, "refusal", None) is not None,
            truncated=escolha.finish_reason == "length",
        )


def _build_providers() -> list[Provider]:
    """Provedores na ordem de preferencia do .env, so os que tem chave."""
    out: list[Provider] = []
    for nome in settings.llm_providers:
        if nome == "anthropic" and settings.anthropic_api_key:
            out.append(AnthropicProvider(name="anthropic", models={
                ROLE_TRANSLATE: settings.translate_model,
                ROLE_CLIP: settings.clip_model,
                ROLE_SCAN: settings.clip_scan_model,
            }))
        elif nome == "openai" and settings.openai_api_key:
            out.append(OpenAIProvider(name="openai", models={
                ROLE_TRANSLATE: settings.openai_translate_model,
                ROLE_CLIP: settings.openai_clip_model,
                ROLE_SCAN: settings.openai_scan_model,
            }))
    return out


_providers_lock = threading.Lock()
_providers_cache: list[Provider] | None = None


def providers() -> list[Provider]:
    global _providers_cache
    with _providers_lock:
        if _providers_cache is None:
            _providers_cache = _build_providers()
        return _providers_cache


def clear_health() -> None:
    """Zera o castigo de todos os provedores. Use depois de aumentar o limite de
    uma conta: sem isso o sistema espera o cooldown vencer para redescobrir."""
    _health.clear(persist=True)
    log.info("saude dos provedores zerada — todos voltam a ser tentados")


def reset(providers_override: list[Provider] | None = None) -> None:
    """Releitura das chaves/modelos e da saude. Usado pelos testes."""
    global _providers_cache
    with _providers_lock:
        _providers_cache = providers_override
    _health.clear()


def healthy() -> list[Provider]:
    return [p for p in providers() if _health.available(p.name)]


def status() -> list[dict[str, Any]]:
    """Para o painel mostrar quem esta de pe."""
    snap = _health.snapshot()
    return [
        {"name": p.name, "models": dict(p.models),
         **snap.get(p.name, {"available": True, "down_for_s": 0, "reason": ""})}
        for p in providers()
    ]


# ---------------------------------------------------------------------- as chamadas


def _order(prefer: str | None) -> list[Provider]:
    """Saudaveis primeiro, na preferencia do .env; caidos ficam no fim como ultimo
    recurso — melhor tentar um provedor que talvez tenha voltado do que desistir."""
    todos = providers()
    if not todos:
        raise AllProvidersDown(
            "Nenhum provedor de IA configurado. Preencha ANTHROPIC_API_KEY ou "
            "OPENAI_API_KEY no .env."
        )
    vivos = [p for p in todos if _health.available(p.name)]
    caidos = [p for p in todos if not _health.available(p.name)]
    if prefer:
        vivos.sort(key=lambda p: p.name != prefer)
    return vivos + caidos


def call_json(role: str, system: str, user: str, schema: dict[str, Any], *,
              max_tokens: int = 16000, effort: str | None = None,
              cache_system: bool = False, prefer: str | None = None) -> LLMResult:
    """Uma chamada, com failover entre provedores.

    Levanta AllProvidersDown se todo mundo falhou — o chamador DEVE tratar isso
    como falha do episodio, nunca como "seguiu sem resultado".
    """
    erros: list[str] = []

    for provedor in _order(prefer):
        for tentativa in range(settings.llm_max_retries):
            try:
                r = provedor.call(role, system, user, schema, max_tokens, effort,
                                  cache_system)
                _health.mark_ok(provedor.name)
                return r
            except Exception as exc:  # noqa: BLE001 — classificamos abaixo
                categoria, cooldown = classify(exc)
                if categoria == FATAL:
                    # Erro nosso (esquema/parametro): trocar de provedor nao ajuda.
                    raise
                if categoria == HARD:
                    _health.mark_down(provedor.name, cooldown, str(exc))
                    erros.append(f"{provedor.name}: {exc}")
                    break  # proximo provedor, sem repetir aqui
                # transitorio: espera e tenta de novo no mesmo provedor
                if tentativa == settings.llm_max_retries - 1:
                    erros.append(f"{provedor.name}: {exc}")
                    log.warning("provedor '%s' falhou %d vezes (%s) — passando adiante",
                                provedor.name, settings.llm_max_retries, str(exc)[:120])
                    break
                espera = min(2 ** tentativa * 2 + random.uniform(0, 1), 30)
                log.info("provedor '%s' instavel (%s); nova tentativa em %.0fs",
                         provedor.name, str(exc)[:90], espera)
                time.sleep(espera)

    raise AllProvidersDown(
        "todos os provedores de IA falharam — " + " | ".join(e[:180] for e in erros)
    )


def map_json(role: str, tarefas: list[dict[str, Any]], *, max_tokens: int = 16000,
             effort: str | None = None, cache_system: bool = False,
             on_error: Callable[[int, BaseException], None] | None = None,
             ) -> list[LLMResult | None]:
    """Varias tarefas independentes, repartidas ENTRE os provedores e em paralelo.

    Cada tarefa e um dict com "system", "user" e "schema". As tarefas sao
    distribuidas em rodizio pelos provedores saudaveis, entao um episodio com 4
    janelas e duas chaves manda 2 para cada uma — metade do tempo de parede e
    metade do consumo em cada conta. Se a chamada de uma tarefa falhar no
    provedor sorteado, o failover de call_json cobre com os outros.

    Devolve uma lista do mesmo tamanho de `tarefas`, com None onde falhou.
    """
    if not tarefas:
        return []

    vivos = healthy() or providers()
    if not vivos:
        raise AllProvidersDown("Nenhum provedor de IA configurado.")

    # Rodizio: a tarefa i comeca no provedor i % n. `prefer` so muda a ordem —
    # o failover continua valendo, entao ninguem fica preso a uma chave morta.
    preferidos = [vivos[i % len(vivos)].name for i in range(len(tarefas))]

    resultados: list[LLMResult | None] = [None] * len(tarefas)
    trabalhadores = max(1, min(settings.llm_max_parallel, len(tarefas)))

    def _uma(i: int) -> None:
        t = tarefas[i]
        resultados[i] = call_json(
            role, t["system"], t["user"], t["schema"],
            max_tokens=t.get("max_tokens", max_tokens),
            effort=t.get("effort", effort),
            cache_system=cache_system,
            prefer=preferidos[i],
        )

    with ThreadPoolExecutor(max_workers=trabalhadores) as pool:
        futuros = {pool.submit(_uma, i): i for i in range(len(tarefas))}
        for fut in futuros:
            i = futuros[fut]
            try:
                fut.result()
            except Exception as exc:  # noqa: BLE001 — uma tarefa nao derruba as outras
                if on_error is not None:
                    on_error(i, exc)
                else:
                    log.warning("tarefa %d falhou em todos os provedores: %s", i, exc)

    return resultados
