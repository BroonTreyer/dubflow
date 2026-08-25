"""Etapa 3: traducao dos segmentos com Claude.

Tres coisas fazem a diferenca entre isto e "jogar no tradutor automatico":

1. **Contexto de bloco** — cada lote recebe as ultimas falas ja traduzidas, para
   que pronomes, tratamento (voce/senhor) e termos nao mudem no meio do episodio.
2. **Restricao de duracao** — pt-BR estica ~20-25% sobre o ingles. O modelo
   recebe o orcamento de caracteres de cada segmento e precisa caber nele.
3. **Glossario por canal** — nomes, bordoes e termos tecnicos ficam estaveis.

O system prompt e identico entre lotes e vai com `cache_control`, entao do
segundo lote em diante ele custa ~10% do preco de entrada.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

import anthropic

from app.config import settings

log = logging.getLogger(__name__)

# Quantos segmentos por requisicao. Menor = mais chamadas; maior = mais risco de
# estourar max_tokens. 60 e um bom meio-termo para fala continua.
BLOCK_SIZE = 60
CONTEXT_TAIL = 4  # falas anteriores enviadas so como contexto

# Caracteres por segundo que uma legenda confortavel comporta em pt-BR.
CPS_BUDGET = 17

SYSTEM_PROMPT = """\
Voce e um tradutor profissional de legendas para portugues brasileiro, com \
experiencia em conteudo de YouTube longform, podcasts e documentarios.

Voce recebe blocos de segmentos transcritos automaticamente e devolve a traducao \
de cada um, mantendo o mesmo `id`.

REGRAS DE TRADUCAO

1. Traduza o sentido, nao as palavras. O resultado deve soar como alguem falando \
portugues brasileiro naturalmente, nao como texto traduzido. Expressoes idiomaticas \
viram expressoes equivalentes em pt-BR; se nao houver equivalente, transmita a intencao.

2. Respeite o orcamento de caracteres (`budget`) de cada segmento. Ele existe porque \
a legenda precisa ser lida no tempo em que a fala acontece. Se a traducao natural nao \
couber, corte redundancia (interjeicoes, repeticoes, muletas como "voce sabe", "tipo \
assim") ate caber. Nunca corte informacao que muda o sentido.

3. Mantenha o registro do falante. Conteudo informal continua informal, incluindo \
giria quando o original tem giria. Conteudo tecnico ou academico mantem precisao. \
Palavrao no original vira palavrao em pt-BR, com a mesma intensidade.

4. Trate a transcricao como imperfeita. O ASR erra nomes proprios, siglas e numeros. \
Quando o contexto deixar claro qual era a palavra, use a correta na traducao.

5. Termos tecnicos consagrados em ingles permanecem em ingles (deploy, marketing, \
software, commit). Nao traduza o que o publico brasileiro da area usa em ingles.

6. Numeros, unidades e moedas: converta o formato para o padrao brasileiro (virgula \
decimal, ponto de milhar). Nao converta o valor de moedas — "10 dollars" vira \
"10 dolares", nunca o equivalente em reais.

7. Nomes proprios, marcas e titulos de obras permanecem como no original.

8. Se um segmento for apenas ruido, musica ou interjeicao sem conteudo ("uh", "hmm"), \
devolva string vazia. Nao invente conteudo para preencher.

9. Nunca junte ou divida segmentos. Um segmento de entrada = um segmento de saida, \
com o mesmo `id`.

FORMATO

Responda apenas com o objeto JSON pedido pelo schema. Sem comentarios, sem \
explicacoes, sem texto fora do JSON.
"""

# Nome legivel de cada idioma de destino suportado (usado no prompt generico).
LANG_NAMES = {"pt-BR": "portugues brasileiro", "en": "ingles (dos EUA)", "es": "espanhol"}

# Prompt para idiomas que NAO sao pt-BR. O pt-BR continua usando SYSTEM_PROMPT
# acima, palavra por palavra, para preservar a qualidade ja validada (e o cache).
GENERIC_SYSTEM_PROMPT = """\
Voce e um tradutor profissional de legendas para {lang}, com experiencia em \
conteudo de YouTube longform, podcasts e documentarios.

Voce recebe blocos de segmentos transcritos automaticamente e devolve a traducao \
de cada um para {lang}, mantendo o mesmo `id`.

REGRAS DE TRADUCAO

1. Traduza o sentido, nao as palavras. O resultado deve soar como um falante \
nativo de {lang} falando naturalmente, nao como texto traduzido.

2. Respeite o orcamento de caracteres (`budget`) de cada segmento — a legenda \
precisa ser lida no tempo da fala. Se nao couber, corte redundancia (interjeicoes, \
muletas), nunca informacao que muda o sentido.

3. Mantenha o registro do falante (informal continua informal, tecnico mantem \
precisao, palavrao vira palavrao com a mesma intensidade).

4. Trate a transcricao como imperfeita: o ASR erra nomes, siglas e numeros. Use a \
palavra correta quando o contexto deixar claro.

5. Termos tecnicos consagrados em ingles no meio tecnico permanecem em ingles.

6. Numeros e unidades no formato padrao de {lang}. Nao converta o valor de moedas.

7. Nomes proprios, marcas e titulos de obras permanecem como no original.

8. Segmento que e so ruido/musica/interjeicao ("uh", "hmm") -> string vazia.

9. Nunca junte ou divida segmentos. Um segmento de entrada = um de saida, mesmo `id`.

FORMATO

Responda apenas com o objeto JSON pedido pelo schema. Sem texto fora do JSON.
"""


def _base_lang(code: str | None) -> str:
    return (code or "").strip().lower().replace("_", "-").split("-")[0]


def _target(meta: dict[str, Any] | None) -> str:
    """Idioma de destino do episodio (meta['lang_dst']), com fallback global."""
    return ((meta or {}).get("lang_dst") or settings.target_lang or "pt-BR")


def _system_prompt(target_lang: str) -> str:
    """pt-BR usa o prompt validado, verbatim; os demais, o generico parametrizado."""
    if _base_lang(target_lang) == "pt":
        return SYSTEM_PROMPT
    return GENERIC_SYSTEM_PROMPT.format(lang=LANG_NAMES.get(target_lang, target_lang))


RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "text": {"type": "string"},
                },
                "required": ["id", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["segments"],
    "additionalProperties": False,
}


def _client() -> anthropic.Anthropic:
    if not settings.anthropic_api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY nao configurada. Adicione a chave no arquivo .env."
        )
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def _budget(segment: dict[str, Any]) -> int:
    """Quantos caracteres cabem no tempo de fala do segmento."""
    duration = max(float(segment["end"]) - float(segment["start"]), 0.6)
    # 1.35x de folga: o modelo deve mirar no orcamento, nao ser estrangulado por ele.
    return max(int(duration * CPS_BUDGET * 1.35), 20)


def _build_blocks(segments: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    return [segments[i : i + BLOCK_SIZE] for i in range(0, len(segments), BLOCK_SIZE)]


def _user_content(
    block: list[dict[str, Any]],
    context_tail: list[str],
    meta: dict[str, Any],
    glossary: dict[str, str],
) -> str:
    payload = {
        "episodio": {
            "titulo": meta.get("title"),
            "canal": meta.get("channel"),
            "idioma_origem": meta.get("lang_src"),
            "idioma_destino": _target(meta),
        },
        "glossario": glossary or {},
        "contexto_anterior_ja_traduzido": context_tail,
        "segmentos": [
            {
                "id": seg["id"],
                "text": seg["text"],
                "budget": _budget(seg),
            }
            for seg in block
        ],
    }
    return (
        "Traduza os segmentos abaixo para "
        f"{_target(meta)}.\n\n"
        "```json\n" + json.dumps(payload, ensure_ascii=False, indent=1) + "\n```"
    )


def _call_claude(client: anthropic.Anthropic, user_text: str, target_lang: str = "pt-BR",
                 max_tokens: int = 16000):
    return client.messages.create(
        model=settings.translate_model,
        max_tokens=max_tokens,
        system=[
            {
                "type": "text",
                "text": _system_prompt(target_lang),
                "cache_control": {"type": "ephemeral"},
            }
        ],
        output_config={
            "effort": settings.translate_effort,
            "format": {"type": "json_schema", "schema": RESPONSE_SCHEMA},
        },
        messages=[{"role": "user", "content": user_text}],
    )


def _parse(response) -> dict[int, str]:
    text = next((b.text for b in response.content if b.type == "text"), "")
    if not text.strip():
        raise ValueError("resposta sem conteudo de texto")
    data = json.loads(text)
    return {int(item["id"]): item["text"].strip() for item in data.get("segments", [])}


def same_language(src: str | None, dst: str | None) -> bool:
    """Origem e destino sao o mesmo idioma? Compara so a base ('pt' == 'pt-BR').

    O Whisper devolve o codigo curto ("pt"), enquanto TARGET_LANG costuma trazer a
    variante regional ("pt-BR"). Sem normalizar, um video ja em portugues seria
    "traduzido" de pt para pt-BR.
    """
    if not src or not dst:
        return False
    base = lambda s: s.strip().lower().replace("_", "-").split("-")[0]  # noqa: E731
    return base(src) == base(dst)


def passthrough(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Usa a transcricao como texto final, sem passar pelo tradutor.

    Mantem o mesmo formato de translate_segments para o resto do pipeline nao
    saber a diferenca: legenda, cortes e karaoke seguem iguais.
    """
    return [
        {**seg, "text_src": seg["text"], "text": seg["text"], "untranslated": False}
        for seg in segments
    ]


def translate_segments(
    segments: list[dict[str, Any]],
    meta: dict[str, Any],
    glossary: dict[str, str] | None = None,
    on_progress: Callable[[float, str], None] | None = None,
) -> list[dict[str, Any]]:
    """Traduz todos os segmentos preservando ids e timestamps."""
    if not segments:
        return []

    # Video ja no idioma de destino: traduzir seria pagar a API para reescrever um
    # texto que ja esta certo — e o modelo, aplicando as regras de reformulacao e
    # o limite de caracteres, mudaria falas sem necessidade.
    if same_language(meta.get("lang_src"), _target(meta)):
        log.info("origem ja e %s — legenda sai da transcricao, sem traduzir",
                 _target(meta))
        if on_progress:
            on_progress(1.0, "sem traducao (mesmo idioma)")
        return passthrough(segments)

    client = _client()
    blocks = _build_blocks(segments)
    translated: dict[int, str] = {}
    context_tail: list[str] = []
    usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    missing = 0

    for block_idx, block in enumerate(blocks):
        user_text = _user_content(block, context_tail, meta, glossary or {})
        result = _translate_block_with_retry(client, user_text, block, meta, glossary or {})
        translated.update(result["texts"])
        for key in usage:
            usage[key] += result["usage"].get(key, 0)

        ordered = [translated.get(seg["id"], "") for seg in block]
        context_tail = [t for t in ordered if t][-CONTEXT_TAIL:]
        missing += sum(1 for seg in block if seg["id"] not in translated)

        if on_progress:
            on_progress((block_idx + 1) / len(blocks), f"bloco {block_idx + 1}/{len(blocks)}")

    log.info(
        "traducao: %d segmentos, entrada=%d saida=%d cache_read=%d cache_write=%d",
        len(segments), usage["input"], usage["output"], usage["cache_read"], usage["cache_write"],
    )
    if missing:
        # Nunca silenciosamente: um segmento sem traducao vira legenda faltando.
        log.warning(
            "%d de %d segmentos voltaram sem traducao — mantendo o texto original neles",
            missing, len(segments),
        )

    out = []
    for seg in segments:
        # Duas situacoes diferentes que nao podem ser tratadas igual:
        #
        # - id ausente = o modelo pulou o segmento. Cair no original e melhor do
        #   que legenda vazia, que seria uma fala sumindo sem aviso.
        # - id presente com texto vazio = decisao deliberada (regra 8 do prompt:
        #   ruido, "hmm", "uh"). Precisa continuar vazio para o segmento sumir da
        #   legenda; devolver o original poria "Hmm." em portugues na tela.
        faltou = seg["id"] not in translated
        out.append(
            {
                **seg,
                "text_src": seg["text"],
                "text": seg["text"] if faltou else translated[seg["id"]],
                # O runner soma isto e mostra no painel, para voce revisar antes de publicar.
                "untranslated": faltou,
            }
        )
    return out


def _translate_block_with_retry(
    client: anthropic.Anthropic,
    user_text: str,
    block: list[dict[str, Any]],
    meta: dict[str, Any],
    glossary: dict[str, str],
    attempts: int = 3,
) -> dict[str, Any]:
    last_error: Exception | None = None

    for attempt in range(attempts):
        try:
            response = _call_claude(client, user_text, _target(meta))

            if response.stop_reason == "refusal":
                # Conteudo que os classificadores recusaram: preserva o original
                # em vez de derrubar o episodio inteiro.
                log.warning("bloco recusado pelos classificadores; mantendo texto original")
                return {
                    "texts": {seg["id"]: seg["text"] for seg in block},
                    "usage": _usage(response),
                }

            if response.stop_reason == "max_tokens" and len(block) > 4:
                # Bloco grande demais: divide ao meio e traduz cada metade.
                mid = len(block) // 2
                merged: dict[int, str] = {}
                total = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
                for half in (block[:mid], block[mid:]):
                    sub = _translate_block_with_retry(
                        client, _user_content(half, [], meta, glossary), half, meta, glossary
                    )
                    merged.update(sub["texts"])
                    for key in total:
                        total[key] += sub["usage"].get(key, 0)
                return {"texts": merged, "usage": total}

            return {"texts": _parse(response), "usage": _usage(response)}

        except (anthropic.RateLimitError, anthropic.APIConnectionError) as exc:
            last_error = exc
            wait = 2 ** attempt * 5
            log.warning("erro transitorio na traducao (%s); retry em %ss", type(exc).__name__, wait)
            time.sleep(wait)
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            log.warning("resposta invalida na traducao: %s", exc)

    log.error("bloco falhou apos %d tentativas: %s", attempts, last_error)
    return {"texts": {seg["id"]: seg["text"] for seg in block}, "usage": {}}


def _usage(response) -> dict[str, int]:
    u = response.usage
    return {
        "input": getattr(u, "input_tokens", 0) or 0,
        "output": getattr(u, "output_tokens", 0) or 0,
        "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
    }


# --------------------------------------------------------------------------- batch


def translate_segments_batch(
    segments: list[dict[str, Any]],
    meta: dict[str, Any],
    glossary: dict[str, str] | None = None,
    poll_seconds: int = 30,
) -> list[dict[str, Any]]:
    """Mesma traducao via Batch API: metade do preco, sem garantia de latencia.

    Vale para reprocessar acervo ou rodar a fila da noite. Nao vale quando o
    episodio precisa sair em minutos — um batch pode levar ate 24h (a maioria
    termina em menos de 1h).

    Sem contexto entre blocos: todos sao enviados de uma vez. Em troca do preco,
    perde-se um pouco de consistencia de terminologia ao longo do episodio.
    """
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    if not segments:
        return []

    if same_language(meta.get("lang_src"), _target(meta)):
        log.info("origem ja e %s — legenda sai da transcricao, sem traduzir",
                 _target(meta))
        return passthrough(segments)

    client = _client()
    blocks = _build_blocks(segments)
    system_text = _system_prompt(_target(meta))

    requests = [
        Request(
            custom_id=f"block-{i}",
            params=MessageCreateParamsNonStreaming(
                model=settings.translate_model,
                max_tokens=16000,
                system=[
                    {
                        "type": "text",
                        "text": system_text,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                output_config={
                    "effort": settings.translate_effort,
                    "format": {"type": "json_schema", "schema": RESPONSE_SCHEMA},
                },
                messages=[
                    {"role": "user", "content": _user_content(block, [], meta, glossary or {})}
                ],
            ),
        )
        for i, block in enumerate(blocks)
    ]

    batch = client.messages.batches.create(requests=requests)
    log.info("batch %s criado com %d blocos", batch.id, len(requests))

    while True:
        batch = client.messages.batches.retrieve(batch.id)
        if batch.processing_status == "ended":
            break
        time.sleep(poll_seconds)

    translated: dict[int, str] = {}
    for result in client.messages.batches.results(batch.id):
        if result.result.type != "succeeded":
            log.warning("bloco %s falhou no batch: %s", result.custom_id, result.result.type)
            continue
        message = result.result.message
        if message.stop_reason == "refusal":
            continue
        try:
            translated.update(_parse(message))
        except (json.JSONDecodeError, ValueError) as exc:
            log.warning("bloco %s com resposta invalida: %s", result.custom_id, exc)

    faltando = sum(1 for seg in segments if seg["id"] not in translated)
    if faltando:
        log.warning(
            "%d de %d segmentos voltaram sem traducao no batch — mantendo o original",
            faltando, len(segments),
        )

    # Mesma distincao do caminho sincrono: id ausente cai no original, texto
    # vazio deliberado continua vazio.
    return [
        {
            **seg,
            "text_src": seg["text"],
            "text": seg["text"] if seg["id"] not in translated else translated[seg["id"]],
            "untranslated": seg["id"] not in translated,
        }
        for seg in segments
    ]
