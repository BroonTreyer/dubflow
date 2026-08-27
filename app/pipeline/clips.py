"""Etapa 5: selecao e renderizacao dos cortes verticais 9:16.

A selecao e feita por Claude sobre a transcricao traduzida com timestamps. O
modelo devolve trechos que se sustentam sozinhos — que e o criterio real de um
corte que funciona no feed, nao "o trecho onde alguem falou alto".
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from app.config import settings
from app.pipeline import llm, subtitles


class NoClipsSelected(RuntimeError):
    """Nenhuma janela devolveu corte.

    Existe para que o runner reprove o episodio em vez de arquiva-lo como
    concluido: em 25/08/2026 quatro episodios ficaram `done` com zero cortes
    porque a falha da selecao era apenas um warning.
    """


log = logging.getLogger(__name__)

ASSETS = Path(__file__).parent / "assets"

# Reframe 9:16 — parametros da "camera" que segue quem fala.
FOCUS_FPS = 2.0          # amostras por segundo (troca de falante dura ~1 s)
FOCUS_HYSTERESIS = 0.12  # so muda o enquadramento se o alvo andar mais que isso
MIN_SEGMENT = 1.2        # segundos minimos entre dois cortes de camera
MAX_SEGMENTS = 24        # teto de trocas por corte (a expressao do ffmpeg cresce)

# Quanto o inicio/fim pode andar para cair numa fronteira de FRASE em vez da
# fronteira de respiracao do Whisper. Generoso de proposito: alguns segundos a
# mais valem menos que abrir no meio de um raciocinio.
SNAP_WINDOW = 6.0
# Duracao alvo do corte para o desempate. Nao e limite (isso e clip_max_seconds):
# e o ponto em que um corte entrega o gancho e o clima sem esticar.
DURACAO_IDEAL = 40.0

# Detectores de rosto, carregados uma vez. YuNet (DNN) e melhor — pega rosto de
# lado e em angulo; o Haar frontal fica de reserva. Ambos tropecam em caminho com
# acento no Windows, entao sao carregados de forma que contorna isso (ver abaixo).
_CASCADE: Any = None
_CASCADE_LOADED = False
_YUNET: Any = None
_YUNET_LOADED = False

SELECTION_PROMPT = """\
Voce e editor de conteudo social e vive de fazer corte performar. Recebe um \
trecho de episodio com timestamps e escolhe os pedacos que funcionam como video \
vertical independente (Reels, TikTok, Shorts).

Seu unico criterio e retencao: a pessoa esta rolando o feed e precisa parar no \
seu corte e ficar ate o fim. Nao escolha o trecho "mais importante" do episodio \
— escolha o que prende.

{genre_block}
O QUE FAZ UM CORTE PERFORMAR

- **Os 3 primeiros segundos decidem tudo.** O corte tem que abrir no meio da \
tensao — uma acusacao, uma virada, uma frase que exige explicacao. Se abrir com \
rodeio, preparacao ou "entao, como eu estava dizendo", esta morto.
- **Uma emocao clara e forte**: raiva, vergonha alheia, revolta, desejo, choque, \
graca. Trecho morno nao performa, por mais bem escrito que seja.
- **Em conteudo explicativo, a moeda muda.** Aula, divulgacao cientifica e analise \
raramente tem briga — e forcar "polemica" onde nao ha produz corte falso. Ali o que \
prende e a REVELACAO CONTRAINTUITIVA: o fato que contradiz o senso comum, a conexao \
que ninguem faria, o numero que nao fecha com a intuicao. "A Amazonia depende da \
poeira do Saara" vale mais que qualquer discussao. Use os criterios do bloco do \
formato acima como prioridade quando eles existirem.
- **Se sustenta sozinho.** Quem nunca viu o episodio entende sem contexto externo.
- **Tem virada.** O melhor corte muda de direcao no meio: a resposta atravessada, \
a revelacao, a frase que cala o outro.
- **Termina em pico, nao em descida.** Corte na melhor frase — nunca na conversa \
esfriando depois dela.
- **Gera comentario.** A pessoa quer opinar, discordar ou marcar alguem.

O QUE NAO SERVE

- Apresentacoes, agradecimentos, "se inscreva no canal", leitura de patrocinio.
- Trechos que dependem de imagem que voce nao viu (referencia a grafico na tela).
- Conversa de transicao, logistica, gente combinando o que vai fazer.
- Trecho tecnicamente correto mas sem carga emocional. Na duvida, deixe de fora.

REGRAS DE PLATAFORMA — VALEM PARA `thumb_text`, `yt_title`, `caption` E `thumb_badge`

Estas nao sao sugestoes de estilo: violar derruba monetizacao e alcance. Uma
chamada que o trecho nao entrega e a definicao de clickbait para o YouTube.

- **A promessa tem que ser paga no proprio trecho.** Se a capa pergunta "quando
  vai acontecer?", a resposta precisa estar NO corte. Curiosidade que o video nao
  fecha e o que a plataforma pune, e o que faz a pessoa sair em 3 segundos.
- **Nao invente fato, numero, data nem declaracao.** So use dado que foi dito no
  trecho. Nada de "cientistas confirmam" se ninguem confirmou nada ali.
- **Nao anuncie o que nao aconteceu**: "morreu", "foi preso", "acabou" sobre quem
  nao morreu, nao foi preso, nao acabou. Isso e desinformacao, nao gancho.
- **Sem palavrao ou xingamento** em qualquer campo — isso entra em "conteudo
  inadequado para anunciantes".
- **Assunto sensivel se escreve mascarado NA CAPA.** Morte, sexo, drogas, crime e
  prisao sao temas legitimos de podcast, mas o filtro automatico le a palavra na
  imagem e derruba o alcance. No `thumb_text`, troque UMA letra: "M0RTE",
  "C@DEIA", "P0RNO", "DR0GAS". No `yt_title` e no `caption` escreva normal — ali
  a grafia mascarada atrapalha a busca e parece spam.
- **Nunca copie fala explicita crua para a capa.** Se o trecho tem linguagem
  pesada, a capa leva a TENSAO, nao a frase: "ELE CONTOU TUDO" em vez de
  reproduzir o palavrao.
- **Sem apelo a tragedia real** (mortes, acidentes, doenca de pessoa nomeada) como
  isca. Tratar o tema e legitimo; usar a dor como chamariz nao.
- **Sem promessa de saude, cura, ganho financeiro garantido ou previsao de
  catastrofe com data**. "Vai ter terremoto em setembro" e desinformacao; "o que
  os dados mostram sobre o risco" e o mesmo trecho, sem o problema.
- **Sem CAIXA ALTA gritada no `yt_title`** e sem fila de "!!!" ou "???".
- Tensao SIM, mentira NAO. A diferenca esta em prometer a pergunta certa em vez
  de uma resposta falsa: "ELE NEGOU TUDO" quando ele negou; nunca "CONFESSOU"
  quando ele negou.

O QUE FAZ UM GANCHO SER BOM (e nao so barulhento)

- **Especifico vence generico.** "PERDEU R$ 2 MILHOES EM 3 DIAS" prende; "VEJA O
  QUE ACONTECEU" nao diz nada e nao gera clique de quem interessa.
- **Contradicao prende mais que superlativo.** "QUANDO PARA DE TREMER, PREOCUPA"
  funciona porque inverte o senso comum. "O MAIOR TERREMOTO DE TODOS" e so volume.
- **Use a fala.** Quando o trecho tem uma frase que ja e o gancho, cite-a quase
  literalmente: soa humano e entrega exatamente o que promete.
- **Uma ideia por capa.** Duas informacoes competindo viram ruido no tamanho de
  miniatura.
- **Sem "voce nao vai acreditar", "chocante", "impressionante"** — sao marcadores
  de clickbait vazio, gastos e penalizados.
- **Nunca abra com pronome sem referente.** "ISSO E PRA 100 ANOS" nao diz nada para
  quem esta rolando o feed: ele nao sabe o que e "isso". Nomeie a coisa —
  "TERRAS RARAS: BRIGA DE 100 ANOS". Vale para isso, esse, aquilo, ele, ela, quando
  a pessoa/coisa nao foi nomeada na propria capa.
- **Constatacao nao e gancho.** "UM LUGAR CHOVE, OUTRO SECA" descreve; nao provoca.
  Ou vira pergunta que o corte responde, ou ganha a consequencia: "O MESMO EL NINO
  QUE ALAGA O SUL SECA O CENTRO".

REGRAS

- Escolha ate {count} trechos deste bloco, ou menos se o material nao render. \
Nao complete a cota com trecho fraco: e melhor devolver 4 fortes que 8 mornos.
- Cada trecho entre {min_s} e {max_s} segundos. **Mire em ~40s**: e onde um corte \
entrega gancho, desenvolvimento e clima sem esticar. So passe de 60s quando a \
revelacao REALMENTE precisa do contexto — e, nesse caso, a tensao tem que aparecer \
na primeira metade, nunca so no fim.
- Alinhe `start` e `end` ao inicio e ao fim de falas completas, usando os timestamps \
fornecidos. Nunca corte no meio de uma frase. O `start` tem que cair no comeco de \
uma frase NOVA: nao comece em "e...", "entao...", "mas...", "ai...", "porque...", \
"e...", nem em resposta solta como "absurdo, ne?". Se o trecho bom comeca no meio \
de um raciocinio, volte alguns segundos ate o inicio da frase que o abre.
- `score`: use a ESCALA INTEIRA. Numa leva de 5 trechos, espera-se algo como um 9, \
dois 7, um 6 e um 5. Se voce der 8 ou 9 para todos, o score vira inutil e os piores \
cortes nao tem como ser descartados. Seja duro: 9-10 e o corte que voce publicaria \
hoje sem pensar; 5-6 e o que so entra para completar cota.
- Trechos nao podem se sobrepor.
- `hook`: a frase de abertura do trecho, copiada da transcricao — serve para conferir \
o alinhamento.
- `title`: titulo curto em pt-BR para uso interno.
- `caption`: legenda pronta para publicar em pt-BR — uma linha de gancho, quebra de \
linha, contexto em uma frase, quebra de linha, 3 a 5 hashtags relevantes. Sem emoji \
em excesso, no maximo dois.
- `yt_title`: titulo otimizado para o YouTube, em pt-BR. Ate 90 caracteres, com o \
gancho ou o dado mais forte logo no comeco (as primeiras palavras decidem o clique). \
Chamativo mas honesto — nada de clickbait que o trecho nao entrega. Sem hashtags.
- `yt_description`: descricao para o YouTube, em pt-BR. Duas ou tres frases: o que o \
trecho mostra e por que vale assistir, seguidas de 3 a 6 hashtags relevantes em uma \
linha. Escreva para busca — use os termos que o publico procuraria.
- `thumb_text`: o texto ESTAMPADO na capa, em pt-BR e em caixa alta no efeito. \
Entre 2 e 16 palavras — escolha o tamanho pelo trecho, nao por regra fixa: um \
choque seco pede "E IMPOSSIVEL!"; uma revelacao com contexto pede a fala inteira, \
como "VOU ATE ORAR AGORA, O EL NINO CHEGOU NO BRASIL, SABE O QUE VAI ACONTECER?". \
Quando o trecho tem uma frase falada que ja e o gancho, PREFIRA cita-la quase \
literalmente — soa humano e e o que performa neste nicho. NAO repita o `yt_title`: \
o titulo informa, a capa provoca. Sem hashtag e sem emoji. Marque com asteriscos \
as palavras que saem coloridas: "VOU TE *EXPLICAR* DE UMA VEZ POR TODAS".
- `thumb_badge`: selo curto opcional, ate 3 palavras, tipo "TENSAO RECORDE", \
"ALERTA", "EXCLUSIVO". String vazia quando o trecho nao pede.
- `thumb_image_prompt`: prompt EM INGLES da imagem tematica que vai ao fundo da \
capa, gerada por IA. Descreva a CENA do assunto, dramatica e concreta — "San Andreas \
fault cracking through California desert, red warning glow, storm sky", "volcanic \
eruption at night over a city skyline", "flooded Brazilian street with cars \
submerged, dark clouds". Nunca peca pessoas, rosto, texto ou logotipo: o \
apresentador real e sobreposto depois e texto gerado sai errado. Se o trecho for \
abstrato demais para virar imagem, devolva string vazia.
- `thumb_time`: o segundo EXATO que vira a capa, na mesma escala de `start`/`end`. \
Nao chute o meio do corte: escolha o instante em que a emocao esta no rosto — a \
reacao ao ouvir, o espanto, o riso, o momento em que a frase pesada cai. Se o \
trecho e uma acusacao aos 3:12, a capa e a CARA de quem ouviu, nao a boca de quem \
falou. Precisa cair dentro de [start, end]; na duvida, prefira logo depois da frase \
mais forte, que e onde a reacao aparece.
- `score`: 0 a 10, o quanto voce aposta que ESTE corte performa. Use a escala \
inteira e seja duro: 9-10 e o corte que voce publicaria hoje, 7-8 e bom, 5-6 e \
mediano, abaixo de 5 nao deveria ter sido escolhido. Varios cortes com nota \
parecida tornam o score inutil — ele e usado para ranquear os trechos do \
episodio inteiro e cortar os piores.

Responda apenas com o JSON do schema.
"""

GENRE_PROMPT = """\
Voce recebe amostras da transcricao de um video e responde o que ele e, para \
orientar um editor de cortes. Seja concreto e curto.

- `genre`: o formato em poucas palavras (ex.: "novela turca dublada", \
"podcast de entrevista", "aula de matematica", "gameplay comentado").
- `audience`: quem assiste isso e o que essa pessoa procura.
- `viral_criteria`: 3 a 5 bullets do que faz um corte DESTE formato performar \
em Reels/TikTok. Especifico do formato, nao conselho generico: numa novela sao \
brigas, declaracoes, humilhacoes e revelacoes; num podcast sao opinioes \
polemicas e historias pessoais; numa aula e o macete que resolve rapido.
- `avoid`: o que neste formato parece bom e nao e.

Responda apenas com o JSON do schema.
"""

GENRE_SCHEMA = {
    "type": "object",
    "properties": {
        "genre": {"type": "string"},
        "audience": {"type": "string"},
        "viral_criteria": {"type": "array", "items": {"type": "string"}},
        "avoid": {"type": "string"},
    },
    "required": ["genre", "audience", "viral_criteria", "avoid"],
    "additionalProperties": False,
}

CLIP_SCHEMA = {
    "type": "object",
    "properties": {
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "title": {"type": "string"},
                    "hook": {"type": "string"},
                    "caption": {"type": "string"},
                    "yt_title": {"type": "string"},
                    "yt_description": {"type": "string"},
                    "thumb_text": {"type": "string"},
                    "thumb_badge": {"type": "string"},
                    "thumb_image_prompt": {"type": "string"},
                    "thumb_time": {"type": "number"},
                    "score": {"type": "number"},
                },
                "required": ["start", "end", "title", "hook", "caption",
                             "yt_title", "yt_description", "thumb_text",
                             "thumb_badge", "thumb_image_prompt", "thumb_time",
                             "score"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["clips"],
    "additionalProperties": False,
}


def target_count(duration_s: float) -> int:
    """Quantos cortes um episodio desta duracao deve render."""
    alvo = round((duration_s / 3600.0) * settings.clips_per_hour)
    # O piso vale para video curto (10 min nao pode voltar com 3 cortes so porque
    # a conta deu 3), mas nunca pode passar do teto.
    alvo = max(alvo, min(settings.clips_per_episode, settings.clips_max))
    return int(min(alvo, settings.clips_max))


def _detect_genre(segments: list[dict[str, Any]],
                  meta: dict[str, Any]) -> dict[str, Any] | None:
    """Le uma amostra do episodio e devolve o que faz um corte DELE performar.

    Sem isto o prompt de selecao fala de "numero surpreendente" e "leitura de
    patrocinio" para uma novela — criterios de podcast aplicados a ficcao.
    """
    textos = [(s.get("text") or "").strip() for s in segments if (s.get("text") or "").strip()]
    if not textos:
        return None

    # Amostra de tres pontos: abertura, meio e fim tem cara diferente no mesmo video.
    n = len(textos)
    amostra = textos[: min(60, n)] + textos[n // 2: n // 2 + 60] + textos[-60:]

    try:
        r = llm.call_json(
            llm.ROLE_SCAN,
            GENRE_PROMPT,
            (f"Titulo: {meta.get('title')}\nCanal: {meta.get('channel')}\n\n"
             "Amostras da transcricao:\n" + "\n".join(amostra)),
            GENRE_SCHEMA,
            max_tokens=1500,
        )
        if r.refusal or not r.text.strip():
            return None
        return r.json()
    except Exception as exc:  # o reconhecimento e um extra: sem ele a selecao ainda roda
        log.warning("deteccao de genero falhou (%s) — seguindo com criterios genericos", exc)
        return None


def _genre_block(genre: dict[str, Any] | None) -> str:
    if not genre:
        return ""
    criterios = "\n".join(f"- {c}" for c in genre.get("viral_criteria") or [])
    return (
        "ESTE VIDEO ESPECIFICAMENTE\n\n"
        f"Formato: {genre.get('genre')}\n"
        f"Publico: {genre.get('audience')}\n\n"
        f"O que faz um corte deste formato performar:\n{criterios}\n\n"
        f"Evite neste formato: {genre.get('avoid')}\n\n"
    )


def _windows(segments: list[dict[str, Any]], count: int) -> list[tuple[list[dict[str, Any]], int]]:
    """Fatia o episodio em janelas de analise, com a cota de cada uma."""
    if not segments:
        return []
    duracao = max(float(s["end"]) for s in segments)
    janela_s = max(300, settings.clip_window_minutes * 60)
    n_janelas = max(1, round(duracao / janela_s))
    if n_janelas == 1:
        return [(segments, count)]

    passo = duracao / n_janelas
    out: list[tuple[list[dict[str, Any]], int]] = []
    for i in range(n_janelas):
        ini, fim = i * passo, (i + 1) * passo
        bloco = [s for s in segments if ini <= float(s["start"]) < fim]
        if not bloco:
            continue
        # Pede com folga por janela: parte vira sobreposicao ou cai no corte final
        # por score, e uma janela fraca nao deve arrastar a cota do episodio.
        cota = max(2, round(count / n_janelas) + 2)
        out.append((bloco, cota))
    return out


def _window_task(bloco: list[dict[str, Any]], cota: int, meta: dict[str, Any],
                 genre_block: str) -> dict[str, Any] | None:
    """Monta a tarefa de uma janela — quem a executa (e em qual provedor) e a
    camada llm que decide."""
    transcript = [
        {"start": round(s["start"], 1), "end": round(s["end"], 1), "text": s.get("text") or ""}
        for s in bloco
        if (s.get("text") or "").strip()
    ]
    if not transcript:
        return None

    system = SELECTION_PROMPT.format(
        count=cota, genre_block=genre_block,
        min_s=settings.clip_min_seconds, max_s=settings.clip_max_seconds,
    )
    ini_min = int(transcript[0]["start"] // 60)
    fim_min = int(transcript[-1]["end"] // 60)
    user = (
        f"Episodio: {meta.get('title')}\nCanal: {meta.get('channel')}\n"
        f"Bloco analisado: minuto {ini_min} ao {fim_min} do episodio.\n\n"
        "Transcricao com timestamps (segundos, na escala do episodio inteiro):\n"
        "```json\n" + json.dumps(transcript, ensure_ascii=False) + "\n```"
    )
    return {"system": system, "user": user, "schema": CLIP_SCHEMA,
            "max_tokens": 16000, "effort": "high", "janela": (ini_min, fim_min)}


def select_clips(
    segments: list[dict[str, Any]],
    meta: dict[str, Any],
    count: int | None = None,
) -> list[dict[str, Any]]:
    """Escolhe os melhores trechos do episodio, janela por janela.

    As janelas sao independentes, entao a camada llm as reparte entre os
    provedores saudaveis e roda todas ao mesmo tempo: com duas chaves, um
    episodio de 4 janelas manda 2 para cada conta.

    Levanta NoClipsSelected se NENHUMA janela voltou. Isso e proposital: um
    episodio sem cortes e uma falha, e ja custou caro fingir que nao era.
    """
    if not llm.providers():
        raise RuntimeError(
            "Nenhum provedor de IA configurado. Preencha ANTHROPIC_API_KEY ou "
            "OPENAI_API_KEY no .env."
        )
    if not segments:
        return []

    duracao = max(float(s["end"]) for s in segments)
    count = count or target_count(duracao)

    genre = _detect_genre(segments, meta)
    if genre:
        log.info("cortes: formato reconhecido como '%s'", genre.get("genre"))
    genre_block = _genre_block(genre)

    janelas = _windows(segments, count)
    tarefas = [t for t in (_window_task(bloco, cota, meta, genre_block)
                           for bloco, cota in janelas) if t is not None]
    if not tarefas:
        return []

    vivos = [p.name for p in llm.healthy()] or [p.name for p in llm.providers()]
    log.info("cortes: alvo de %d em %.0f min, %d janela(s) entre %s",
             count, duracao / 60, len(tarefas), ", ".join(vivos))

    falhas: list[str] = []

    def _falhou(i: int, exc: BaseException) -> None:
        ini, fim = tarefas[i]["janela"]
        falhas.append(f"minuto {ini}-{fim}: {exc}")
        log.warning("janela %d-%d falhou em todos os provedores: %s", ini, fim, exc)

    resultados = llm.map_json(llm.ROLE_CLIP, tarefas, cache_system=True, on_error=_falhou)

    bruto: list[dict[str, Any]] = []
    por_provedor: dict[str, int] = {}
    for i, r in enumerate(resultados):
        if r is None:
            continue
        ini, fim = tarefas[i]["janela"]
        if r.refusal:
            log.warning("selecao recusada pelos classificadores (minuto %d-%d)", ini, fim)
            continue
        try:
            bruto.extend(r.json().get("clips", []))
        except (ValueError, AttributeError) as exc:
            falhas.append(f"minuto {ini}-{fim}: resposta ilegivel ({exc})")
            log.warning("janela %d-%d devolveu JSON invalido: %s", ini, fim, exc)
            continue
        por_provedor[r.provider] = por_provedor.get(r.provider, 0) + 1

    if por_provedor:
        log.info("cortes: janelas atendidas por %s",
                 ", ".join(f"{k}={v}" for k, v in sorted(por_provedor.items())))

    if not bruto:
        # Antes isto virava um episodio "done" com zero cortes. Agora e falha.
        raise NoClipsSelected(
            f"nenhuma das {len(tarefas)} janelas devolveu cortes"
            + (" — " + " | ".join(f[:160] for f in falhas[:4]) if falhas else "")
        )

    return _sanitize(bruto, segments, count)


# Muletas e conectivos: comecar por eles denuncia que o corte pegou a conversa no
# meio, mesmo quando a frase esta gramaticalmente inteira.
_ABERTURA_FRACA = re.compile(
    r"^(e|entao|então|ai|aí|mas|porque|por isso|ou seja|tipo|assim|ah|eh|é|entendeu|"
    r"ne|né|sim|nao|não|dai|daí|af|inclusive|alias|aliás|enfim|bom|olha|tambem|"
    r"também|so que|só que|dai que|daí que)\b",
    re.IGNORECASE,
)

# Fim de frase de verdade. Reticencias nao contam: quase sempre e fala cortada.
_FIM_DE_FRASE = re.compile(r"[.!?]['\"”’)]*\s*$")


def _abre_bem(texto: str) -> bool:
    """O trecho comeca como fala nova, e nao no meio do raciocinio?"""
    t = (texto or "").strip()
    if not t:
        return False
    if t[0].islower():        # frase cortada ao meio
        return False
    return not _ABERTURA_FRACA.match(t)


def speech_boundaries(segments: list[dict[str, Any]]) -> tuple[list[float], list[float]]:
    """(inicios_bons, fins_bons) — fronteiras de FRASE, nao de respiracao.

    O Whisper quebra segmento onde a pessoa respira, nao onde a frase acaba. Alinhar
    a esses limites e o que fazia 46% dos cortes abrirem com "e la na Italia tem um
    chamado" ou "absurdo, ne?" — e o proprio prompt diz que os 3 primeiros segundos
    decidem tudo.

    Inicio bom = segmento que vem depois de ponto final E nao comeca com muleta.
    Fim bom  = segmento cujo texto termina em . ! ?
    """
    inicios: list[float] = []
    fins: list[float] = []
    anterior_fechou = True   # o primeiro segmento sempre pode abrir

    for s in segments:
        texto = (s.get("text") or "").strip()
        if not texto:
            continue
        if anterior_fechou and _abre_bem(texto):
            inicios.append(float(s["start"]))
        if _FIM_DE_FRASE.search(texto):
            fins.append(float(s["end"]))
            anterior_fechou = True
        else:
            anterior_fechou = False

    return inicios, fins


def _sanitize(clips: list[dict[str, Any]], segments: list[dict[str, Any]],
              limit_count: int | None = None) -> list[dict[str, Any]]:
    """Encaixa cada corte nas fronteiras reais de fala e remove sobreposicao.

    Os cortes chegam de varias janelas, entao a disputa por sobreposicao e
    resolvida por score: percorrer na ordem do episodio faria o corte pior
    ganhar do melhor so por comecar antes.
    """
    if not segments:
        return []
    starts = sorted({float(s["start"]) for s in segments})
    ends = sorted({float(s["end"]) for s in segments})
    limit = max(ends)
    bons_inicios, bons_fins = speech_boundaries(segments)

    def nearest(values: list[float], target: float) -> float:
        return min(values, key=lambda v: abs(v - target))

    def encaixa(preferidos: list[float], fallback: list[float], alvo: float,
                para_tras: bool) -> float:
        """Fronteira de frase quando existe perto; senao, a de respiracao.

        A direcao importa e nao e simetrica. Se o inicio caiu no meio de uma frase,
        o certo e VOLTAR ate onde ela comeca — avancar cortaria o comeco dela fora e
        o problema continuaria. No fim vale o contrario: AVANCAR ate a frase fechar,
        porque parar antes deixa a fala pela metade.

        So quando nao ha candidato no lado preferido e que aceita o outro lado.
        """
        if preferidos:
            lado = [v for v in preferidos
                    if (v <= alvo if para_tras else v >= alvo)
                    and abs(v - alvo) <= SNAP_WINDOW]
            if lado:
                return max(lado) if para_tras else min(lado)
            candidato = nearest(preferidos, alvo)
            if abs(candidato - alvo) <= SNAP_WINDOW:
                return candidato
        return nearest(fallback, alvo)

    clips = sorted(clips, key=lambda c: float(c.get("score") or 0), reverse=True)

    cleaned: list[dict[str, Any]] = []
    for clip in clips:
        try:
            # inicio volta ate a frase abrir; fim avanca ate ela fechar
            start = encaixa(bons_inicios, starts, float(clip["start"]), para_tras=True)
            end = encaixa(bons_fins, ends, float(clip["end"]), para_tras=False)
        except (KeyError, TypeError, ValueError):
            continue

        start = max(0.0, start - 0.25)  # respiro antes da primeira palavra
        end = min(limit, end + 0.4)
        duration = end - start
        # A folga existe so para absorver o reencaixe nas fronteiras de frase.
        # 1.5x sobre 60s deixava passar corte de 90s, que e outro formato.
        if (duration < settings.clip_min_seconds * 0.6
                or duration > settings.clip_max_seconds * 1.2):
            continue
        if any(start < c["end"] and end > c["start"] for c in cleaned):
            continue

        cleaned.append(
            {
                "start": round(start, 2),
                "end": round(end, 2),
                "title": (clip.get("title") or "").strip()[:120],
                "hook": (clip.get("hook") or "").strip()[:300],
                "caption": (clip.get("caption") or "").strip()[:2000],
                "yt_title": (clip.get("yt_title") or "").strip()[:100],
                "yt_description": (clip.get("yt_description") or "").strip()[:4800],
                "thumb_text": (clip.get("thumb_text") or "").strip()[:180],
                "thumb_badge": (clip.get("thumb_badge") or "").strip()[:28],
                "thumb_image_prompt": (clip.get("thumb_image_prompt") or "").strip()[:600],
                "thumb_time": _thumb_time(clip.get("thumb_time"), start, end),
                "score": float(clip.get("score") or 0),
            }
        )

    if limit_count is not None and len(cleaned) > limit_count:
        # Nao da para confiar so no score: medido em 28 cortes, o modelo devolve
        # tudo entre 8 e 10 (media 8.7), entao ordenar por ele e quase sortear.
        # O desempate usa sinais que a gente MEDE no corte pronto.
        cleaned.sort(key=lambda c: _rank_key(c, segments), reverse=True)
        cleaned = cleaned[:limit_count]

    espalhamento = _score_spread(cleaned)
    if espalhamento is not None and espalhamento < 0.6:
        log.warning("score sem discriminacao (desvio %.2f em %d cortes) — "
                    "o desempate esta vindo da abertura e da duracao",
                    espalhamento, len(cleaned))

    cleaned.sort(key=lambda c: c["start"])
    return cleaned


def _score_spread(clips: list[dict[str, Any]]) -> float | None:
    """Desvio padrao dos scores. None com menos de 3 cortes (nao diz nada)."""
    notas = [float(c.get("score") or 0) for c in clips]
    if len(notas) < 3:
        return None
    media = sum(notas) / len(notas)
    return (sum((n - media) ** 2 for n in notas) / len(notas)) ** 0.5


def _rank_key(clip: dict[str, Any], segments: list[dict[str, Any]]) -> tuple:
    """Ordem de qualidade do corte, do melhor para o pior.

    Combina o score do modelo com duas coisas verificaveis no resultado:

    - **abre bem**: a primeira fala e inicio de frase, sem muleta. E o unico fator
      que a gente sabe que muda retencao nos 3 primeiros segundos.
    - **duracao no ponto**: perto de DURACAO_IDEAL. Corte de 75s com o clima no fim
      perde para um de 40s que entrega logo.
    """
    inicio = float(clip["start"])
    primeira = next(
        (s.get("text", "") for s in segments
         if float(s["end"]) > inicio and float(s["start"]) < float(clip["end"])
         and (s.get("text") or "").strip()),
        "",
    )
    abre = 1 if _abre_bem(primeira) else 0
    duracao = float(clip["end"]) - inicio
    # 0 a 1: 1 na duracao ideal, caindo conforme se afasta.
    encaixe = max(0.0, 1.0 - abs(duracao - DURACAO_IDEAL) / DURACAO_IDEAL)
    return (abre, round(float(clip.get("score") or 0) + encaixe, 2))


def _thumb_time(bruto: Any, start: float, end: float) -> float:
    """Instante da capa, sempre dentro do corte e longe das pontas.

    A IA as vezes devolve o tempo relativo ao inicio do trecho em vez da escala do
    episodio; um valor pequeno demais para ser absoluto e reinterpretado assim.
    Sem valor utilizavel, cai em 40% do trecho — depois da frase de abertura, que e
    onde a reacao costuma estar, e nao no meio cego.
    """
    padrao = start + (end - start) * 0.4
    try:
        t = float(bruto)
    except (TypeError, ValueError):
        return round(padrao, 2)

    if t < start and 0 <= t <= (end - start):
        t = start + t  # veio relativo ao inicio do corte
    # Nunca nas bordas: a primeira e a ultima fracao pegam a transicao de cena.
    margem = min(0.5, (end - start) * 0.08)
    t = max(start + margem, min(end - margem, t))
    return round(t, 2)


def _clip_segments(segments: list[dict[str, Any]], start: float, end: float) -> list[dict[str, Any]]:
    """Recorta os segmentos do intervalo e rebaseia os timestamps para zero."""
    out = []
    for seg in segments:
        if float(seg["end"]) <= start or float(seg["start"]) >= end:
            continue
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        # Rebaseia tambem os timestamps por palavra (recortando a janela do corte),
        # para a legenda do corte poder aparar no tempo real da fala.
        words = []
        for w in (seg.get("words") or []):
            ws, we = w.get("start"), w.get("end")
            if ws is None or we is None or float(we) <= start or float(ws) >= end:
                continue
            words.append({
                "start": max(0.0, float(ws) - start),
                "end": min(end - start, float(we) - start),
                "word": w.get("word", ""),
            })
        entry = {
            "start": max(0.0, float(seg["start"]) - start),
            "end": min(end - start, float(seg["end"]) - start),
            "text": text,
        }
        if words:
            entry["words"] = words
        out.append(entry)
    return out


def _load_cascade() -> Any:
    """Carrega o detector Haar de rosto, uma vez, tolerando ausencia do opencv.

    O XML e lido em Python (que abre caminhos Unicode) e passado ao OpenCV pela
    memoria: no Windows o cv2 nao abre arquivos em caminhos com acento, e a pasta
    do projeto ("Area de Trabalho") tem um. Sem isso, o detector nem carregaria.
    """
    global _CASCADE, _CASCADE_LOADED
    if _CASCADE_LOADED:
        return _CASCADE
    _CASCADE_LOADED = True
    try:
        import cv2
    except ImportError:
        log.warning("opencv indisponivel; o reframe cai para o recorte central")
        return None
    try:
        data = (ASSETS / "haarcascade_frontalface_default.xml").read_text(encoding="utf-8")
        fs = cv2.FileStorage(data, cv2.FILE_STORAGE_READ | cv2.FILE_STORAGE_MEMORY)
        cascade = cv2.CascadeClassifier()
        cascade.read(fs.getFirstTopLevelNode())
        _CASCADE = None if cascade.empty() else cascade
    except Exception as exc:  # noqa: BLE001 — deteccao e um luxo; nunca derruba o render
        log.warning("falha ao carregar detector de rosto (%s); reframe central", exc)
        _CASCADE = None
    return _CASCADE


def _load_yunet() -> Any:
    """Carrega o detector YuNet (DNN), uma vez. None se o modelo/opencv faltar.

    O cv2 abre o .onnx por caminho, e no Windows nao le caminho com acento; entao
    copiamos o modelo para uma pasta temporaria ASCII e carregamos de la.
    """
    global _YUNET, _YUNET_LOADED
    if _YUNET_LOADED:
        return _YUNET
    _YUNET_LOADED = True
    model = ASSETS / "face_detection_yunet_2023mar.onnx"
    if not model.exists():
        return None
    try:
        import cv2
        tmp = Path(tempfile.gettempdir()) / "dubflow_yunet_2023mar.onnx"
        if not tmp.exists() or tmp.stat().st_size != model.stat().st_size:
            shutil.copyfile(model, tmp)
        _YUNET = cv2.FaceDetectorYN_create(str(tmp), "", (320, 320), score_threshold=0.6)
    except Exception as exc:  # noqa: BLE001 — sem YuNet caimos no Haar
        log.warning("YuNet indisponivel (%s); usando Haar", exc)
        _YUNET = None
    return _YUNET


def _face_boxes(img: Any) -> list[tuple[float, float, float, float]]:
    """Devolve (x, y, w, h) em pixels de cada rosto no frame — YuNet, senao Haar.

    A caixa inteira importa, nao so o centro: sem a largura nao da para garantir
    que o rosto caiba dentro da janela 9:16 em vez de ser cortado ao meio.
    """
    import cv2

    h, w = img.shape[:2]
    yunet = _load_yunet()
    if yunet is not None:
        yunet.setInputSize((w, h))
        _, faces = yunet.detect(img)
        if faces is None:
            return []
        return [(float(f[0]), float(f[1]), float(f[2]), float(f[3])) for f in faces]

    cascade = _load_cascade()
    if cascade is None:
        return []
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    faces = cascade.detectMultiScale(
        gray, scaleFactor=1.1, minNeighbors=5,
        minSize=(int(h * 0.08), int(h * 0.08)),
    )
    return [(float(x), float(y), float(fw), float(fh)) for (x, y, fw, fh) in faces]


def _face_centers(img: Any) -> list[tuple[float, float]]:
    """(centro_x_px, area) de cada rosto — atalho sobre _face_boxes."""
    return [(x + w / 2, w * h) for (x, _y, w, h) in _face_boxes(img)]


def _window_ratio(frame_w: int, frame_h: int) -> float:
    """Fracao da largura do frame que a janela 9:16 cobre (1.0 = frame inteiro)."""
    if frame_w <= 0 or frame_h <= 0:
        return 1.0
    return min(1.0, (1080 / 1920) * (frame_h / frame_w))


def _focus_for_span(x0: float, x1: float, frame_w: int, frame_h: int,
                    margin: float = 0.02) -> float:
    """Posiciona a janela 9:16 de modo a conter o intervalo [x0, x1] inteiro.

    x0/x1 sao normalizados (0..1) e delimitam o que precisa aparecer — a caixa do
    rosto escolhido, ou a de um grupo de rostos. Quando o intervalo cabe na
    janela, o resultado e o enquadramento mais centrado que ainda nao corta
    ninguem; quando nao cabe, centraliza no meio do intervalo (o chamador ja
    deveria ter escolhido um subconjunto que coubesse).
    """
    r = _window_ratio(frame_w, frame_h)
    if r >= 1:  # fonte ja e 9:16 ou mais estreita: nao ha corte horizontal a fazer
        return 0.5

    centro = (x0 + x1) / 2
    ideal = (centro - r / 2) / (1 - r)

    # Faixa de posicoes que mantem [x0-margem, x1+margem] dentro da janela.
    lo = (x1 + margin - r) / (1 - r)
    hi = (x0 - margin) / (1 - r)
    if lo <= hi:
        ideal = max(lo, min(hi, ideal))
    return max(0.0, min(1.0, ideal))


def _pick_group(boxes: list[tuple[float, float, float, float]],
                pesos: list[float], frame_w: int, frame_h: int,
                margin: float = 0.02) -> tuple[float, float] | None:
    """Escolhe o que enquadrar: um rosto, ou os vizinhos que cabem junto com ele.

    Substitui a media ponderada de todos os rostos, que era o bug real — com duas
    pessoas afastadas a media caia no vazio entre elas e cortava as duas.
    """
    if not boxes:
        return None
    r = _window_ratio(frame_w, frame_h)
    util = max(0.0, r - 2 * margin)  # largura aproveitavel dentro da janela

    ordem = sorted(range(len(boxes)), key=lambda i: boxes[i][0])
    melhor: tuple[float, float, float] | None = None  # (peso, x0, x1)

    # Cada rosto e uma semente; o grupo cresce enquanto o conjunto couber na janela.
    for pos, i in enumerate(ordem):
        x0 = boxes[i][0] / frame_w
        x1 = (boxes[i][0] + boxes[i][2]) / frame_w
        peso = pesos[i]
        for j in ordem[pos + 1:]:
            nx0 = min(x0, boxes[j][0] / frame_w)
            nx1 = max(x1, (boxes[j][0] + boxes[j][2]) / frame_w)
            if (nx1 - nx0) > util:
                break  # o proximo rosto ja nao cabe junto: fecha o grupo aqui
            x0, x1, peso = nx0, nx1, peso + pesos[j]
        if melhor is None or peso > melhor[0]:
            melhor = (peso, x0, x1)

    return (melhor[1], melhor[2]) if melhor else None


def _focus_from_center(cx_norm: float, frame_w: int, frame_h: int) -> float:
    """Converte o centro horizontal do rosto (0..1) na posicao da janela 9:16.

    Devolve 0 (janela na esquerda), 0.5 (centro) ou 1 (direita). `r` e a fracao
    da largura — ja escalada para cobrir 1080x1920 — que a janela vertical ocupa;
    fora dessa faixa util o valor e travado para nao vazar do quadro.
    """
    if frame_w <= 0:
        return 0.5
    r = (1080 / 1920) * (frame_h / frame_w)
    if r >= 1:  # fonte ja e 9:16 ou mais estreita: nao ha corte horizontal a fazer
        return 0.5
    focus = (cx_norm - r / 2) / (1 - r)
    return max(0.0, min(1.0, focus))


def _detect_focus(video_path: Path, start: float,
                  duration: float) -> list[tuple[float, float]] | None:
    """Devolve a trilha [(segundo, foco 0..1)] que a janela 9:16 deve seguir.

    Uma so posicao para o corte inteiro nao da conta de cena com duas pessoas: a
    janela precisa acompanhar quem esta falando. A lista e sempre nao-vazia e
    comeca em t=0; um corte sem troca de enquadramento volta com um item so.

    Devolve None quando nao ha como decidir (sem opencv, sem ffmpeg, ou nenhum
    rosto encontrado) — o chamador entao usa o recorte central. Os frames saem
    para uma pasta temporaria ASCII porque o cv2.imread tambem tropeca em acento.
    """
    try:
        import cv2
    except ImportError:
        return None
    # Precisa de ao menos um detector (YuNet ou Haar); senao nao ha o que focar.
    if _load_yunet() is None and _load_cascade() is None:
        return None

    rate = FOCUS_FPS
    with tempfile.TemporaryDirectory(prefix="dubflow_focus_") as td:
        pattern = str(Path(td) / "f_%04d.jpg")
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{start:.2f}", "-t", f"{duration:.2f}", "-i", str(video_path),
            # Reduz para 480 de largura: deteccao de rosto nao precisa de resolucao
            # cheia e assim roda rapido mesmo em CPU.
            "-vf", f"fps={rate:.4f},scale=480:-2", "-q:v", "4", pattern,
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace")
        except OSError as exc:  # ffmpeg ausente do PATH, por exemplo
            log.warning("ffmpeg indisponivel para deteccao (%s); reframe central", exc)
            return None
        if res.returncode != 0:
            log.warning("extracao de frames para deteccao falhou; reframe central")
            return None

        amostras: list[tuple[float, float, float]] = []  # (t, x0, x1) do grupo escolhido
        frame_w = frame_h = 0
        anterior = None
        for i, frame in enumerate(sorted(Path(td).glob("f_*.jpg"))):
            img = cv2.imread(str(frame))
            if img is None:
                continue
            frame_h, frame_w = img.shape[:2]
            cinza = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            boxes = _face_boxes(img)
            if boxes:
                # Peso = tamanho do rosto, multiplicado pela boca em movimento. Numa
                # conversa os dois rostos tem area parecida; quem fala e o criterio
                # que decide, e e o que o espectador espera ver enquadrado.
                fala = _mouth_activity(cinza, anterior, boxes)
                pesos = [(b[2] * b[3]) * (1.0 + 2.0 * a) for b, a in zip(boxes, fala)]
                grupo = _pick_group(boxes, pesos, frame_w, frame_h)
                if grupo is not None:
                    amostras.append((i / rate, grupo[0], grupo[1]))
            anterior = cinza

    if not amostras or frame_w == 0:
        return None
    return _build_track(amostras, frame_w, frame_h, duration)


def _mouth_activity(cinza: Any, anterior: Any,
                    boxes: list[tuple[float, float, float, float]]) -> list[float]:
    """Quanto a boca de cada rosto se mexeu desde o frame anterior (0..1).

    Aproximacao barata de "quem esta falando": compara a faixa da boca (terco
    inferior do rosto) com o mesmo recorte do frame anterior. E relativa ao frame,
    entao um corte de camera — que mexe tudo de uma vez — nao elege ninguem.
    """
    if anterior is None or anterior.shape != cinza.shape:
        return [0.0] * len(boxes)

    import numpy as np

    brutos: list[float] = []
    for (x, y, w, h) in boxes:
        bx0, bx1 = int(x + w * 0.2), int(x + w * 0.8)
        by0, by1 = int(y + h * 0.6), int(y + h * 1.0)
        bx0, by0 = max(0, bx0), max(0, by0)
        bx1 = min(cinza.shape[1], bx1)
        by1 = min(cinza.shape[0], by1)
        if bx1 - bx0 < 4 or by1 - by0 < 4:
            brutos.append(0.0)
            continue
        atual = cinza[by0:by1, bx0:bx1].astype("float32")
        antes = anterior[by0:by1, bx0:bx1].astype("float32")
        brutos.append(float(np.abs(atual - antes).mean()))

    teto = max(brutos) if brutos else 0.0
    if teto < 2.0:  # ninguem se mexeu de verdade: nao inventa um falante
        return [0.0] * len(boxes)
    return [b / teto for b in brutos]


def _build_track(amostras: list[tuple[float, float, float]], frame_w: int, frame_h: int,
                 duration: float) -> list[tuple[float, float]]:
    """Transforma as amostras por frame numa trilha estavel de (tempo, foco).

    Duas travas contra a "camera nervosa": a mediana movel absorve deteccao que
    pisca, e a histerese so publica uma mudanca quando ela e grande e se sustenta
    por MIN_SEGMENT segundos. O resultado e um corte de camera, nao um tremor.
    """
    focos = [
        (t, _focus_for_span(x0, x1, frame_w, frame_h))
        for (t, x0, x1) in amostras
    ]

    # Mediana movel de 5 amostras (~2,5 s): remove deteccao isolada fora do lugar.
    suave: list[tuple[float, float]] = []
    for i, (t, _f) in enumerate(focos):
        janela = [f for _t, f in focos[max(0, i - 2): i + 3]]
        suave.append((t, sorted(janela)[len(janela) // 2]))

    trilha: list[tuple[float, float]] = []
    atual = suave[0][1]
    trilha.append((0.0, atual))
    candidato: tuple[float, float] | None = None
    for t, f in suave:
        if abs(f - atual) < FOCUS_HYSTERESIS:
            candidato = None
            continue
        if candidato is None or abs(f - candidato[1]) >= FOCUS_HYSTERESIS:
            candidato = (t, f)
            continue
        # O novo enquadramento se manteve tempo suficiente: vira corte de camera.
        # A distancia minima vale entre os tempos PUBLICADOS (candidato[0], nao t):
        # comparar com t deixaria passar dois cortes colados no inicio do trecho.
        if t - candidato[0] >= MIN_SEGMENT and candidato[0] - trilha[-1][0] >= MIN_SEGMENT:
            atual = candidato[1]
            trilha.append((candidato[0], atual))
            candidato = None

    if len(trilha) > MAX_SEGMENTS:
        trilha = trilha[:MAX_SEGMENTS]
    return trilha


Track = float | list[tuple[float, float]]


def _focus_expr(track: Track) -> str:
    """Expressao de foco para o ffmpeg: constante, ou variavel no tempo.

    Com mais de um segmento vira um if aninhado sobre `t`, que o crop avalia a
    cada frame — e assim a janela corta de uma pessoa para a outra no meio do
    trecho, em vez de ficar parada no meio das duas.
    """
    if isinstance(track, (int, float)):
        return f"{float(track):.4f}"
    if len(track) == 1:
        return f"{track[0][1]:.4f}"

    # Do fim para o inicio: if(lt(t,T1),F0, if(lt(t,T2),F1, ... Fn))
    expr = f"{track[-1][1]:.4f}"
    for (t_corte, _f), (_t_ant, f_ant) in zip(track[:0:-1], track[-2::-1]):
        expr = f"if(lt(t\\,{t_corte:.2f})\\,{f_ant:.4f}\\,{expr})"
    return expr


def _focus_at(track: Track, t: float) -> float:
    """O foco vigente em um instante — usado pela capa vertical (frame unico)."""
    if isinstance(track, (int, float)):
        return float(track)
    atual = track[0][1]
    for t_corte, f in track:
        if t_corte <= t:
            atual = f
        else:
            break
    return atual


def _vertical_chain(mode: str, focus: Track) -> str:
    """Cadeia que leva o quadro a 9:16, terminando no rotulo [framed].

    Fica separada da queima de legenda porque a thumbnail vertical precisa do
    mesmo enquadramento do corte — e uma capa com a cabeca cortada nao serve.
    """
    if mode == "pad":
        # Legado: o video inteiro encolhido no meio de um fundo borrado. Nao foca
        # na cena — a faixa util fica pequena entre duas barras desfocadas.
        return (
            "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,boxblur=22:2[bg];"
            "[0:v]scale=1080:-2[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2[framed]"
        )
    # face/center: recorta uma janela 9:16 que preenche a tela. O foco desliza a
    # janela na horizontal (0=esquerda, 0.5=centro, 1=direita); (in_w-out_w) e a
    # folga real, entao o corte nunca sai do quadro.
    return (
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
        f"crop=1080:1920:x='(in_w-out_w)*({_focus_expr(focus)})':y='(in_h-out_h)/2'[framed]"
    )


def _reframe_filter(mode: str, focus: Track, ass_path: Path) -> str:
    """Monta o filtro do ffmpeg que leva o video a 9:16 e queima a legenda."""
    sub = f"subtitles='{subtitles._escape_for_filter(ass_path)}'"
    return f"{_vertical_chain(mode, focus)};[framed]{sub}[v]"


# O reframe de um mesmo trecho e reusado pela thumbnail vertical: detectar rosto
# custa extracao de frames, e rodar duas vezes so para a capa nao se paga.
_REFRAME_CACHE: dict[tuple[str, float, float], tuple[str, Track]] = {}


def _resolve_reframe(video_path: Path, start: float, duration: float) -> tuple[str, Track]:
    """Decide o modo de enquadramento e a trilha de foco da janela 9:16."""
    mode = settings.clip_reframe
    if mode != "face":
        return mode, 0.5

    key = (str(video_path), round(start, 2), round(duration, 2))
    if key not in _REFRAME_CACHE:
        detected = _detect_focus(video_path, start, duration)
        # Sem rosto (ou sem detector), cai para o centro: preenche a tela do mesmo
        # jeito, so nao segue o rosto.
        _REFRAME_CACHE[key] = ("center", 0.5) if detected is None else ("face", detected)
    return _REFRAME_CACHE[key]


def render_clip(
    video_path: Path,
    segments: list[dict[str, Any]],
    clip: dict[str, Any],
    output_path: Path,
    work_dir: Path,
    card: bool = False,
) -> Path:
    """Corta o trecho, converte para 9:16 focando na cena e queima a legenda.

    O reframe (CLIP_REFRAME) recorta uma janela vertical que preenche a tela: em
    'face' a janela e posicionada sobre o rosto detectado; em 'center' fica no
    meio; 'pad' mantem o encaixe antigo com fundo borrado.

    Com `card`, sobrepoe o molde opcional (faixa do gancho + CTA) por cima do
    video, que continua em tela cheia. O molde e best-effort: se falhar, o corte
    sai sem ele.
    """
    start, end = float(clip["start"]), float(clip["end"])
    duration = end - start

    karaoke = settings.clip_karaoke
    ass_path = work_dir / f"clip_{output_path.stem}.ass"
    subtitles.write_ass(
        _clip_segments(segments, start, end),
        ass_path,
        width=1080,
        height=1920,
        style=subtitles.STYLE_CLIP_KARAOKE if karaoke else subtitles.STYLE_CLIP,
        max_chars=subtitles.CLIP_MAX_CHARS_PER_LINE,
        max_lines=subtitles.CLIP_MAX_LINES,
        karaoke=karaoke,
    )

    mode, focus = _resolve_reframe(video_path, start, duration)
    filter_complex = _reframe_filter(mode, focus, ass_path)

    # Molde opcional: PNG RGBA com a pilula do CTA no rodape, sobreposto ao video
    # 9:16 ja com a legenda. Best-effort: sem PNG, o corte sai normal.
    overlay_png = None
    if card:
        from app.pipeline import card as card_mod
        overlay_png = card_mod.render_overlay(
            settings.clip_cta_text, work_dir / f"card_{output_path.stem}.png"
        )

    inputs = ["-i", str(video_path)]
    out_label = "[v]"
    if overlay_png is not None:
        inputs += ["-i", str(overlay_png)]
        filter_complex += ";[v][1:v]overlay=0:0[vout]"
        out_label = "[vout]"

    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start:.2f}", "-t", f"{duration:.2f}", *inputs,
        "-filter_complex", filter_complex,
        "-map", out_label, "-map", "0:a?",
        "-c:v", "libx264", "-preset", "medium", "-crf", "21",
        "-r", "30", "-pix_fmt", "yuv420p",
        *_audio_args(),
        "-movflags", "+faststart",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"render do corte falhou: {result.stderr.strip()[-800:]}")
    return output_path


def _audio_args() -> list[str]:
    """Args de audio comuns aos renders. Normaliza o volume quando ativado.

    loudnorm mira -14 LUFS (o alvo de YouTube/streaming): sem isso, cada corte sai
    com um volume, e um feed de cortes fica com gente subindo e descendo o som.
    """
    args = ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11"] if settings.audio_loudnorm else []
    return [*args, "-c:a", "aac", "-b:a", "128k", "-ar", "44100"]


def render_clip_wide(
    video_path: Path,
    segments: list[dict[str, Any]],
    clip: dict[str, Any],
    output_path: Path,
    work_dir: Path,
) -> Path:
    """Renderiza a versao horizontal 16:9 do mesmo trecho, para o YouTube comum.

    Mantem o quadro original encaixado em 1920x1080 (sem recorte) e queima a
    legenda no estilo do episodio. Serve para publicar o corte como video normal,
    nao como Short.
    """
    start, end = float(clip["start"]), float(clip["end"])
    duration = end - start

    ass_path = work_dir / f"clip_{output_path.stem}_wide.ass"
    subtitles.write_ass(
        _clip_segments(segments, start, end),
        ass_path,
        width=1920,
        height=1080,
        style=subtitles.STYLE_EPISODE,
    )

    # decrease + pad: o quadro inteiro cabe em 16:9; fonte de origem ja landscape
    # preenche exato, uma fonte mais estreita ganha faixas laterais em vez de corte.
    filter_complex = (
        "[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,"
        f"subtitles='{subtitles._escape_for_filter(ass_path)}'[v]"
    )
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start:.2f}", "-t", f"{duration:.2f}", "-i", str(video_path),
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-r", "30", "-pix_fmt", "yuv420p",
        *_audio_args(),
        "-movflags", "+faststart",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"render do corte horizontal falhou: {result.stderr.strip()[-800:]}")
    return output_path


