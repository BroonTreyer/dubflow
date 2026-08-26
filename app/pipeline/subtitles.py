"""Etapa 4: geracao de SRT/ASS e queima da legenda no video.

Duas saidas diferentes:

- **SRT** — legenda separada, para o arquivo e para subir como faixa no YouTube.
- **ASS** — usada na queima. Permite estilo (contorno, sombra, posicao), que e o
  que faz a legenda continuar legivel sobre qualquer imagem.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable

MAX_CHARS_PER_LINE = 42
MAX_LINES = 2

# O corte vertical tem menos da metade da largura util do episodio e uma fonte
# muito maior, entao cabe bem menos texto por linha. Com o limite do episodio a
# legenda vazaria para fora do quadro. Em compensacao ha altura sobrando, por
# isso ele aceita tres linhas.
# 22 caracteres e o que cabe em 940px uteis (1080 menos as margens) com Arial
# Black 68. Linhas mais cheias significam menos linhas: a mesma frase que ocupava
# quatro linhas passa a ocupar tres, sobrando imagem visivel.
CLIP_MAX_CHARS_PER_LINE = 22
CLIP_MAX_LINES = 3

# Os tamanhos sao relativos ao PlayRes declarado no arquivo, nao a pixels da
# tela. Como o PlayResY do episodio e 1080 e o do corte e 1920, o mesmo numero
# significa tamanhos diferentes nos dois — e foi por isso que a primeira versao
# saiu ilegivel. A referencia usada aqui: a altura da fonte deve ficar por volta
# de 5% da altura do quadro (~54 em 1080, ~76 em 1920).
#
# Campos, em ordem: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,
# OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,
# Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
#
# Alignment usa a disposicao do teclado numerico: 2 = inferior centralizado,
# 5 = centro da tela. Precisa ser 2 nos dois estilos.

# Episodio completo (PlayRes 1920x1080): legivel sem tapar a imagem.
STYLE_EPISODE = (
    "Style: Default,Arial,52,&H00FFFFFF,&H000000FF,&H00000000,&H90000000,"
    "-1,0,0,0,100,100,0,0,1,3.2,1.6,2,60,60,58,1"
)

# Cortes verticais (PlayRes 1080x1920): fonte grande, contorno forte e margem
# inferior alta o bastante para a legenda nao ficar atras da interface do
# TikTok/Reels, que ocupa a faixa de baixo do quadro.
STYLE_CLIP = (
    "Style: Default,Arial Black,68,&H00FFFFFF,&H000000FF,&H00000000,&HC0000000,"
    "-1,0,0,0,100,100,0,0,1,5.0,2.5,2,70,70,260,1"
)

# Versao karaoke do corte: a palavra "ja falada" fica amarela (PrimaryColour) e a
# ainda nao falada fica branca (SecondaryColour) — o \kf preenche uma na outra no
# tempo da fala. So a cor muda em relacao ao STYLE_CLIP; tamanho e margem seguem iguais.
STYLE_CLIP_KARAOKE = (
    "Style: Default,Arial Black,68,&H0000FFFF,&H00FFFFFF,&H00000000,&HC0000000,"
    "-1,0,0,0,100,100,0,0,1,5.0,2.5,2,70,70,260,1"
)


def _fmt_srt(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _fmt_ass(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    cs = int(round(seconds * 100))
    h, cs = divmod(cs, 360_000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def _greedy_wrap(words: list[str], width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def wrap_text(text: str, max_chars: int = MAX_CHARS_PER_LINE, max_lines: int = MAX_LINES) -> str:
    """Quebra em linhas equilibradas de ate `max_chars`, sem cortar palavra.

    `max_lines` e um alvo, nao um teto rigido: quando o texto nao cabe em
    `max_lines * max_chars`, e melhor abrir uma linha extra do que estourar a
    largura — o renderizador ASS nao re-quebra, entao uma linha larga demais sai
    cortada na tela.

    O reequilibrio evita o resultado feio do greedy puro (uma linha cheia e
    outra com duas palavras): procura a menor largura que ainda produz o mesmo
    numero de linhas.
    """
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text

    words = text.split(" ")
    lines = _greedy_wrap(words, max_chars)

    target = min(len(lines), max_lines) if len(lines) > max_lines else len(lines)
    # A largura util nunca fica abaixo da maior palavra isolada.
    floor = max(len(max(words, key=len)), (len(text) + target - 1) // target)
    for width in range(floor, max_chars + 1):
        candidate = _greedy_wrap(words, width)
        if len(candidate) <= target:
            lines = candidate
            break

    return "\n".join(lines)


def _distribute_cs(words: list[str], total_cs: int) -> list[int]:
    """Reparte a duracao do segmento (centesimos de s) entre as palavras.

    Nao temos timestamp por palavra na traducao — o whisper marca as palavras no
    idioma de origem, que nao casam 1:1 com o pt-BR. Entao dividimos o tempo do
    segmento em proporcao ao tamanho de cada palavra, o que da um karaoke suave e
    fiel ao ritmo da fala. A soma bate exatamente com `total_cs`.
    """
    if not words:
        return []
    total_cs = max(len(words), total_cs)  # ao menos 1cs por palavra
    weights = [len(w) + 1 for w in words]
    tw = sum(weights)
    raw = [max(1, round(total_cs * w / tw)) for w in weights]
    diff = total_cs - sum(raw)
    i = 0
    guard = 10 * len(raw) + abs(diff) + 10
    while diff != 0 and i < guard:
        j = i % len(raw)
        if diff > 0:
            raw[j] += 1
            diff -= 1
        elif raw[j] > 1:
            raw[j] -= 1
            diff += 1
        i += 1
    return raw


def _karaoke_text(text: str, start: float, end: float, max_chars: int, max_lines: int) -> str:
    """Monta o campo Text do ASS com tags \\kf, mantendo a mesma quebra de linha."""
    wrapped = wrap_text(escape_ass(text), max_chars, max_lines)
    lines = wrapped.split("\n")
    words = [w for line in lines for w in line.split(" ") if w]
    if not words:
        return wrapped.replace("\n", "\\N")

    durations = _distribute_cs(words, int(round((end - start) * 100)))
    rendered: list[str] = []
    idx = 0
    for line in lines:
        parts = []
        for word in line.split(" "):
            if not word:
                continue
            parts.append(f"{{\\kf{durations[idx]}}}{word}")
            idx += 1
        rendered.append(" ".join(parts))
    return "\\N".join(rendered)


def escape_ass(text: str) -> str:
    """Neutraliza a sintaxe do ASS dentro do texto da legenda.

    No ASS, `{...}` delimita override tags e `\\` inicia escapes (`\\N`, `\\h`).
    Uma traducao que mencione JSON, LaTeX ou um trecho de codigo sumiria da tela
    sem erro nenhum. Os substitutos sao visualmente equivalentes.
    """
    return (
        text.replace("\\", "⧵")  # barra invertida "de exibicao"
        .replace("{", "｛")       # chaves de largura total
        .replace("}", "｝")
    )


def _usable(segments: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        start, end = float(seg["start"]), float(seg["end"])
        if end <= start:
            end = start + 1.2
        item: dict[str, Any] = {"start": start, "end": end, "text": text}
        # Preserva os timestamps por palavra (quando houver) para o _resegment.
        if seg.get("words"):
            item["words"] = seg["words"]
        out.append(item)
    return out


# Silencio (em segundos) que separa uma legenda da seguinte. O segmento do Whisper
# costuma juntar duas frases e esticar a janela sobre a pausa — a legenda entao
# aparece cedo e fica parada na tela ate a fala acontecer. Quebrar na pausa e
# aparar ao tempo real das palavras corrige os dois sintomas.
SPLIT_GAP = 0.6
MIN_CUE_SECONDS = 0.8  # tempo minimo de leitura, sem invadir a proxima legenda


def split_oversized(cues: list[dict[str, Any]], max_chars: int,
                    max_lines: int) -> list[dict[str, Any]]:
    """Divide NO TEMPO a legenda que nao cabe na tela, em vez de empilhar linhas.

    `_resegment` quebra onde ha pausa de silencio, mas fala corrida nao tem pausa:
    no ep 1 um segmento de 360 caracteres durou 22,9 s inteiros. Como `wrap_text`
    prefere abrir linha extra a estourar a largura, aquilo virou um bloco de 18
    linhas que cobria a tela toda.

    Aqui o criterio nao depende de pausa: se o texto passa do que cabe em
    `max_lines x max_chars`, ele e repartido em pedacos, cada um com sua fatia do
    tempo — proporcional ao tamanho, para a legenda continuar acompanhando a fala.
    A quebra respeita palavra e prefere cair depois de pontuacao.
    """
    # 85% do teorico: texto real nao empacota perfeito nas linhas (palavra que nao
    # cabe empurra a linha inteira), entao usar max_chars*max_lines cheio ainda
    # deixava passar um bloco de 4 linhas num teto de 3.
    capacidade = max(1, int(max_chars * max_lines * 0.85))
    saida: list[dict[str, Any]] = []

    for cue in cues:
        texto = " ".join((cue.get("text") or "").split())
        inicio, fim = float(cue["start"]), float(cue["end"])
        if len(texto) <= capacidade:
            saida.append({**cue, "text": texto})
            continue

        partes = _chunk_words(texto, capacidade)
        # Estimar por caractere nao basta: o empacotamento depende de ONDE as
        # palavras caem. Aqui a gente CONFERE o resultado no proprio wrap_text e
        # reparte de novo o pedaco que ainda estourar — o teto vira garantia.
        partes = _forcar_teto(partes, max_chars, max_lines)
        total = sum(len(p) for p in partes) or 1
        t = inicio
        for parte in partes:
            fatia = (fim - inicio) * len(parte) / total
            saida.append({**cue, "start": round(t, 3),
                          "end": round(min(t + fatia, fim), 3), "text": parte})
            t += fatia
    return saida


def _forcar_teto(partes: list[str], max_chars: int, max_lines: int) -> list[str]:
    """Reparte ao meio qualquer pedaco que ainda passe de `max_lines` na tela."""
    saida: list[str] = []
    fila = list(partes)
    guarda = 0
    while fila and guarda < 400:
        guarda += 1
        parte = fila.pop(0)
        palavras = parte.split()
        if len(palavras) < 2 or len(wrap_text(parte, max_chars, max_lines).split("\n")) <= max_lines:
            saida.append(parte)
            continue
        meio = len(palavras) // 2
        fila.insert(0, " ".join(palavras[meio:]))
        fila.insert(0, " ".join(palavras[:meio]))
    return saida + fila


def _chunk_words(texto: str, capacidade: int) -> list[str]:
    """Reparte o texto em pedacos de ate `capacidade`, cortando entre palavras.

    Fecha o pedaco assim que passar de 60% da capacidade E a palavra terminar em
    pontuacao: uma legenda que termina em virgula/ponto le muito melhor que uma
    cortada no meio da oracao.
    """
    palavras = texto.split()
    partes: list[str] = []
    atual: list[str] = []
    tamanho = 0

    for palavra in palavras:
        extra = len(palavra) + (1 if atual else 0)
        if atual and tamanho + extra > capacidade:
            partes.append(" ".join(atual))
            atual, tamanho = [palavra], len(palavra)
            continue
        atual.append(palavra)
        tamanho += extra
        if tamanho >= capacidade * 0.6 and palavra[-1:] in ",.;:!?":
            partes.append(" ".join(atual))
            atual, tamanho = [], 0

    if atual:
        partes.append(" ".join(atual))

    # Sobra minuscula no fim vira legenda-relampago ("periodo." por 0,3s). Junta
    # com a anterior desde que caiba — ler pela metade e melhor que piscar.
    if len(partes) > 1 and len(partes[-1]) < capacidade * 0.25:
        junto = f"{partes[-2]} {partes[-1]}"
        if len(junto) <= capacidade * 1.15:
            partes[-2:] = [junto]
    return partes or [texto]


def _resegment(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Re-corta as legendas usando os timestamps por palavra.

    - Quebra um segmento em legendas separadas onde ha pausa de silencio (> SPLIT_GAP).
    - Apara o inicio/fim ao tempo real da primeira/ultima palavra (tira o silencio).
    - Distribui o texto traduzido entre os pedacos, proporcional ao nº de palavras
      de cada grupo (a traducao nao casa 1:1 com as palavras de origem, mas as
      fronteiras de pausa sao reais, entao o texto cai no bloco de tempo certo).

    Sem timestamps por palavra, o segmento passa inalterado.
    """
    cues: list[dict[str, Any]] = []
    for seg in segments:
        text = " ".join((seg.get("text") or "").split())
        if not text:
            continue
        words = [w for w in (seg.get("words") or [])
                 if w.get("start") is not None and w.get("end") is not None]
        if len(words) < 2:
            cues.append({"start": float(seg["start"]), "end": float(seg["end"]), "text": text})
            continue

        # Agrupa palavras separando nas pausas de silencio.
        groups: list[list[dict[str, Any]]] = [[words[0]]]
        for w in words[1:]:
            if float(w["start"]) - float(groups[-1][-1]["end"]) > SPLIT_GAP:
                groups.append([w])
            else:
                groups[-1].append(w)

        if len(groups) == 1:
            # Uma fala so: mantem o texto inteiro, apenas aparado ao tempo das palavras.
            cues.append({"start": float(words[0]["start"]),
                         "end": float(words[-1]["end"]), "text": text})
            continue

        # Distribui as palavras traduzidas entre os grupos, deixando ao menos 1 por grupo.
        toks = text.split(" ")
        total = sum(len(g) for g in groups)
        i = 0
        for gi, group in enumerate(groups):
            restantes = len(groups) - 1 - gi
            if gi == len(groups) - 1:
                take = toks[i:]
            else:
                n = max(1, round(len(toks) * len(group) / total))
                n = min(n, len(toks) - i - restantes)  # garante 1 token por grupo futuro
                take = toks[i:i + n]
                i += n
            if take:
                cues.append({"start": float(group[0]["start"]),
                             "end": float(group[-1]["end"]), "text": " ".join(take)})

    # Tempo minimo de leitura, sem invadir a proxima legenda.
    for j, cue in enumerate(cues):
        if cue["end"] - cue["start"] < MIN_CUE_SECONDS:
            limite = (cues[j + 1]["start"] - 0.05) if j + 1 < len(cues) else cue["start"] + MIN_CUE_SECONDS
            cue["end"] = max(cue["end"], min(cue["start"] + MIN_CUE_SECONDS, limite))
    return cues


def write_srt(segments: list[dict[str, Any]], path: Path) -> Path:
    lines = []
    cues = split_oversized(_resegment(_usable(segments)),
                           MAX_CHARS_PER_LINE, MAX_LINES)
    for i, seg in enumerate(cues, start=1):
        lines.append(str(i))
        lines.append(f"{_fmt_srt(seg['start'])} --> {_fmt_srt(seg['end'])}")
        lines.append(wrap_text(seg["text"]))
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_ass(
    segments: list[dict[str, Any]],
    path: Path,
    width: int = 1920,
    height: int = 1080,
    style: str = STYLE_EPISODE,
    max_chars: int = MAX_CHARS_PER_LINE,
    max_lines: int = MAX_LINES,
    karaoke: bool = False,
) -> Path:
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
{style}

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
    events = []
    # O teto de tela vale para os dois estilos: sem ele, fala corrida sem pausa
    # vira um bloco que cobre o quadro (ep 1: 360 chars em 22,9s = 18 linhas).
    for seg in split_oversized(_resegment(_usable(segments)), max_chars, max_lines):
        if karaoke:
            text = _karaoke_text(seg["text"], seg["start"], seg["end"], max_chars, max_lines)
        else:
            # Escapa antes de quebrar: o \N da quebra de linha e sintaxe nossa, nao do texto.
            text = wrap_text(escape_ass(seg["text"]), max_chars, max_lines).replace("\n", "\\N")
        events.append(
            f"Dialogue: 0,{_fmt_ass(seg['start'])},{_fmt_ass(seg['end'])},Default,,0,0,0,,{text}"
        )
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return path


def _escape_for_filter(path: Path) -> str:
    """Escapa o caminho para o filtro `subtitles` do ffmpeg.

    No Windows o filtro precisa de barras normais e do `:` do drive escapado,
    senao o ffmpeg interpreta `C:` como separador de opcao do filtro. A aspa
    simples tambem escapa porque o valor inteiro vai entre aspas simples.
    """
    return (
        str(path.resolve())
        .replace("\\", "/")
        .replace(":", "\\:")
        .replace("'", r"\'")
    )


def probe_duration(video_path: Path) -> float | None:
    """Duracao do video em segundos, via ffprobe. None se nao der para saber."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except OSError:
        return None
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def burn(video_path: Path, ass_path: Path, output_path: Path, crf: int = 20,
         duration: float | None = None,
         on_progress: Callable[[float], None] | None = None) -> Path:
    """Queima a legenda ASS no video.

    Com `on_progress`, reporta o andamento real (0..1) durante a codificacao. Sem
    isso a etapa fica horas sem dar sinal — e uma barra parada e indistinguivel
    de um processo travado.
    """
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vf", f"subtitles='{_escape_for_filter(ass_path)}'",
        "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        str(output_path),
    ]

    if on_progress is None:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
        if result.returncode != 0:
            raise RuntimeError(f"queima de legenda falhou: {result.stderr.strip()[-800:]}")
        return output_path

    if duration is None:
        duration = probe_duration(video_path)

    # -progress escreve pares chave=valor no stdout, formato estavel (o stderr e
    # texto humano e muda entre versoes). O stderr vai para arquivo: le-lo em
    # paralelo travaria, e ele so importa se o ffmpeg falhar.
    cmd = cmd[:1] + ["-progress", "pipe:1", "-nostats"] + cmd[1:]
    _run_with_progress(cmd, duration, on_progress)
    return output_path


PROGRESS_STEP = 0.005  # so reporta a cada meio ponto percentual


def progress_fractions(linhas: Iterable[str], duration: float | None) -> Iterable[float]:
    """Le o fluxo do `-progress` do ffmpeg e emite o andamento (0..1).

    Sao pares chave=valor, uma por linha; interessa `out_time_us`, que vem como
    "N/A" ate o primeiro frame sair. Emite so quando o valor andou o suficiente:
    reportar cada linha encheria o banco de updates identicos.
    """
    if not duration or duration <= 0:
        return
    ultimo = 0.0
    for linha in linhas:
        if not linha.startswith("out_time_us="):
            continue
        try:
            segundos = int(linha.split("=", 1)[1]) / 1_000_000
        except ValueError:  # o "N/A" do inicio
            continue
        fracao = min(1.0, segundos / duration)
        if fracao - ultimo >= PROGRESS_STEP:
            ultimo = fracao
            yield fracao


def _run_with_progress(cmd: list[str], duration: float | None,
                       on_progress: Callable[[float], None]) -> None:
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err,
                                text=True, encoding="utf-8", errors="replace")
        assert proc.stdout is not None
        for fracao in progress_fractions(proc.stdout, duration):
            on_progress(fracao)
        proc.wait()
        if proc.returncode != 0:
            err.seek(0)
            raise RuntimeError(f"queima de legenda falhou: {err.read().strip()[-800:]}")
