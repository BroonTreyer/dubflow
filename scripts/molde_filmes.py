"""Molde de cortes de filme: fundo, perfil no topo, gancho, video, CTA.

Pega cada video de uma pasta de entrada e devolve 1080x1920 pronto pro Reels:

    @achadinhos._isa              <- perfil
    Segue pra ver mais cenas      <- subtitulo (opcional)
    VOCE NAO SABE O QUE ELES      <- gancho
    ENCONTRARAM...
    [        video         ]      <- encaixado sem cortar, audio original
    ( SEGUE PRA VER O FINAL )     <- CTA

Tudo o que e escolha visual (textos, cores, fonte, tamanhos) mora em
filmes/molde.json, que o painel (scripts/painel_filmes.py) edita. Sem o arquivo,
vale o PADRAO abaixo.

O gancho de cada video vem, nesta ordem: --gancho, um .txt com o mesmo nome do
video ao lado dele, ou um dos ganchos do molde (rodizio).

Uso:
    .venv\\Scripts\\python.exe -m scripts.molde_filmes
    .venv\\Scripts\\python.exe -m scripts.molde_filmes --entrada X --saida Y
"""

from __future__ import annotations

import argparse
import json
import math
import re
import zlib
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

LARGURA, ALTURA = 1080, 1920
VIDEOS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}

RAIZ = Path("filmes")
ENTRADA, SAIDA = RAIZ / "entrada", RAIZ / "saida"
ARQ_MOLDE = RAIZ / "molde.json"

FONTES = {
    "Impact": r"C:\Windows\Fonts\impact.ttf",
    "Arial Black": r"C:\Windows\Fonts\ariblk.ttf",
    "Arial Negrito": r"C:\Windows\Fonts\arialbd.ttf",
    "Segoe Black": r"C:\Windows\Fonts\seguibl.ttf",
    "Bahnschrift": r"C:\Windows\Fonts\bahnschrift.ttf",
    "Georgia Negrito": r"C:\Windows\Fonts\georgiab.ttf",
}

PADRAO = {
    "perfil": "@achadinhos._isa",
    "subtitulo": "Segue pra ver mais cenas",
    "cta": "SEGUE PRA VER O FINAL",
    "fonte": "Impact",
    "cor_fundo": "#ffffff",
    "cor_texto": "#111111",
    "cor_subtitulo": "#6e6e6e",
    "cor_cta": "#e50914",
    "cor_texto_cta": "#ffffff",
    "caixa_alta": True,
    "mostrar_subtitulo": True,
    "mostrar_cta": True,
    "tamanho_perfil": 54,
    "tamanho_gancho_max": 86,
    "tamanho_cta": 44,
    "margem": 60,
    "cantos_video": 0,
    "legendar_padrao": False,   # legenda automatica em quem nao tem escolha propria
    # Video mais longo que isso vira Parte 1, Parte 2... O Reels por API aceita ate
    # 15 min, mas so por URL publica. No envio direto (rupload) a Meta processa
    # durante a requisicao e desiste por volta de 34s: medido em 07/10/2026 com o
    # render novo, 60s passa e 77s cai mesmo com 5 MB (scripts/teste_envio_ig.py).
    "parte_max": 60,
    "rotulo_parte": "PARTE {n}",
    "cta_ultima": "SEGUE PRA MAIS FILMES",
    # vale para todo video sem corte proprio (.corte.json)
    "corte_padrao": {"topo": 0.0, "base": 0.0, "esq": 0.0, "dir": 0.0,
                     "inicio": 0.0, "fim": 0.0},
    "ganchos": [
        "Você não sabe o que eles encontraram...",
        "Ninguém esperava o que aconteceu depois disso",
        "Esse filme vai te deixar sem dormir",
        "Repara no que acontece no final",
        "O plot twist mais insano que você vai ver hoje",
    ],
}


def carregar_molde() -> dict:
    if not ARQ_MOLDE.exists():
        return _validar({})
    try:
        return _validar(json.loads(ARQ_MOLDE.read_text(encoding="utf-8")))
    except json.JSONDecodeError:
        return _validar({})  # arquivo corrompido nao derruba o painel


LIMITES = {"tamanho_perfil": (20, 120), "tamanho_gancho_max": (40, 160),
           "tamanho_cta": (20, 90), "margem": (0, 300), "cantos_video": (0, 120),
           "parte_max": (10, 900)}


def _validar(molde: dict) -> dict:
    """Molde sempre renderizavel: campo invalido volta pro padrao em vez de virar 500."""
    limpo = {}
    for k, padrao in PADRAO.items():
        v = molde.get(k, padrao)
        if k.startswith("cor_"):
            v = v if isinstance(v, str) and re.fullmatch(r"#?[0-9a-fA-F]{6}", v) else padrao
        elif k in LIMITES:
            try:
                v = min(max(int(v), LIMITES[k][0]), LIMITES[k][1])
            except (TypeError, ValueError):
                v = padrao
        elif isinstance(padrao, bool):
            v = bool(v)
        elif isinstance(padrao, str):
            v = str(v)
        elif k == "ganchos":
            v = [str(g).strip() for g in v if str(g).strip()] if isinstance(v, list) else padrao
        elif k == "corte_padrao":
            v = normalizar_corte(v if isinstance(v, dict) else None)
        limpo[k] = v
    return limpo


def salvar_molde(molde: dict) -> dict:
    limpo = _validar(molde)
    RAIZ.mkdir(parents=True, exist_ok=True)
    ARQ_MOLDE.write_text(json.dumps(limpo, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    return limpo


def _cor(hexa: str) -> tuple[int, int, int]:
    hexa = hexa.lstrip("#")
    return tuple(int(hexa[i:i + 2], 16) for i in (0, 2, 4))


def _fonte_arq(molde: dict) -> str:
    arq = FONTES.get(molde["fonte"])
    if arq and Path(arq).exists():
        return arq
    return next(a for a in FONTES.values() if Path(a).exists())


def _picotar(draw, palavra: str, fonte, largura_max: int) -> list[str]:
    """Palavra mais larga que a linha (um link, um @ enorme) quebrada por letra."""
    pedacos, atual = [], ""
    for letra in palavra:
        if atual and draw.textlength(atual + letra, font=fonte) > largura_max:
            pedacos.append(atual)
            atual = ""
        atual += letra
    return pedacos + [atual] if atual else pedacos


def _quebrar(draw, texto: str, fonte, largura_max: int) -> list[str]:
    linhas, atual = [], ""
    palavras = []
    for palavra in texto.split():
        if draw.textlength(palavra, font=fonte) > largura_max:
            palavras += _picotar(draw, palavra, fonte, largura_max)
        else:
            palavras.append(palavra)
    for palavra in palavras:
        teste = f"{atual} {palavra}".strip()
        if draw.textlength(teste, font=fonte) <= largura_max:
            atual = teste
        else:
            if atual:
                linhas.append(atual)
            atual = palavra
    if atual:
        linhas.append(atual)
    return linhas


def _fonte_gancho(draw, texto: str, fonte_arq: str, largura_max: int, maximo: int):
    """Maior fonte (ate `maximo`) em que o gancho cabe em ate 3 linhas SEM vazar.

    Vazar acontece com uma palavra sozinha mais larga que a tela (um @ ou link):
    a quebra de linha nao resolve, so diminuir a fonte.
    """
    for tamanho in range(int(maximo), 23, -3):
        fonte = ImageFont.truetype(fonte_arq, tamanho)
        linhas = _quebrar(draw, texto, fonte, largura_max)
        if len(linhas) <= 3 and all(
                draw.textlength(l, font=fonte) <= largura_max for l in linhas):
            return fonte, linhas
    return fonte, linhas[:3]


def _fonte_que_cabe(draw, texto: str, fonte_arq: str, tamanho: int, largura_max: int):
    """A fonte de `tamanho`, reduzida ate a linha unica caber na largura."""
    fonte = ImageFont.truetype(fonte_arq, tamanho)
    while tamanho > 14 and draw.textlength(texto, font=fonte) > largura_max:
        tamanho -= 2
        fonte = ImageFont.truetype(fonte_arq, tamanho)
    return fonte


def dimensoes(video: Path) -> tuple[int, int]:
    """Largura x altura COMO O FFMPEG ENTREGA o quadro.

    Video de celular costuma vir gravado deitado com uma marca de rotacao; o
    ffmpeg gira sozinho ao decodificar, entao o corte tem que usar o tamanho ja
    girado - senao a area cortada sai no lugar errado (ou o crop falha).
    """
    saida = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height:stream_side_data=rotation:stream_tags=rotate",
         "-of", "json", str(video)],
        check=True, capture_output=True, text=True).stdout
    streams = json.loads(saida).get("streams") or []
    if not streams:
        raise ValueError(f"{video.name} nao tem faixa de video")
    s0 = streams[0]
    w, h = int(s0["width"]), int(s0["height"])
    rotacao = s0.get("tags", {}).get("rotate") or next(
        (d.get("rotation") for d in s0.get("side_data_list", []) if "rotation" in d), 0)
    if abs(int(float(rotacao or 0))) % 180 == 90:
        w, h = h, w
    return w, h


def duracao(video: Path) -> float:
    try:
        saida = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(video)],
            check=True, capture_output=True, text=True).stdout
        return round(float(saida.strip()), 2)
    except (subprocess.CalledProcessError, ValueError):
        return 0.0


def montar_fundo(molde: dict, gancho: str, video_wh: tuple[int, int],
                 parte: str | None = None) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """Desenha o fundo e devolve (imagem, (x, y, w, h) onde o video entra).

    Gancho + video + CTA formam um bloco centralizado na vertical, entao um
    video 16:9 nao fica boiando no meio da tela.
    """
    fonte_arq = _fonte_arq(molde)
    img = Image.new("RGB", (LARGURA, ALTURA), _cor(molde["cor_fundo"]))
    draw = ImageDraw.Draw(img)
    margem = int(molde["margem"])
    util = LARGURA - 2 * margem

    # topo fixo: perfil + subtitulo
    f_perfil = _fonte_que_cabe(draw, molde["perfil"], fonte_arq,
                               int(molde["tamanho_perfil"]), util)
    y = 110
    draw.text((LARGURA / 2, y), molde["perfil"], font=f_perfil,
              fill=_cor(molde["cor_texto"]), anchor="mt")
    y += int(f_perfil.size * 1.3)
    if molde["mostrar_subtitulo"] and molde["subtitulo"].strip():
        f_sub = _fonte_que_cabe(draw, molde["subtitulo"], fonte_arq,
                                int(molde["tamanho_perfil"] * 0.63), util)
        draw.text((LARGURA / 2, y), molde["subtitulo"], font=f_sub,
                  fill=_cor(molde["cor_subtitulo"]), anchor="mt")
        y += int(f_sub.size * 1.3)
    teto = y + 50

    texto = gancho.upper() if molde["caixa_alta"] else gancho
    f_gancho, linhas = _fonte_gancho(draw, texto, fonte_arq, util,
                                     molde["tamanho_gancho_max"])
    altura_linha = int(f_gancho.size * 1.15)
    h_gancho = altura_linha * len(linhas)

    # selo da parte ("PARTE 2"): pilula pequena logo acima do gancho
    f_parte = ImageFont.truetype(fonte_arq, max(26, int(f_gancho.size * 0.5))) if parte else None
    selo_h = int(f_parte.size * 1.7) if parte else 0
    h_gancho += selo_h + 22 if parte else 0

    respiro = 40
    mostrar_cta = molde["mostrar_cta"] and molde["cta"].strip()
    # o botao tem 2.7x a fonte de folga lateral; a fonte encolhe ate tudo caber
    f_cta = ImageFont.truetype(fonte_arq, int(molde["tamanho_cta"]))
    while f_cta.size > 14 and (draw.textlength(molde["cta"], font=f_cta)
                               + 2.7 * f_cta.size) > LARGURA - 40:
        f_cta = ImageFont.truetype(fonte_arq, f_cta.size - 2)
    cta_h = int(f_cta.size * 2.35) if mostrar_cta else 0

    # video: largura util, limitado pela altura que sobra
    vw, vh = video_wh
    livre = ALTURA - teto - 80 - h_gancho - cta_h - 2 * respiro
    escala = min(LARGURA / vw, livre / vh)
    w, h = int(vw * escala) // 2 * 2, int(vh * escala) // 2 * 2

    bloco = h_gancho + respiro + h + (respiro + cta_h if mostrar_cta else 0)
    y = teto + max(0, (ALTURA - 80 - teto - bloco) // 2)

    if parte:
        sw = draw.textlength(parte, font=f_parte) + 1.6 * f_parte.size
        sx = (LARGURA - sw) / 2
        draw.rounded_rectangle([sx, y, sx + sw, y + selo_h], radius=selo_h // 2,
                               fill=_cor(molde["cor_cta"]))
        draw.text((LARGURA / 2, y + selo_h / 2), parte, font=f_parte,
                  fill=_cor(molde["cor_texto_cta"]), anchor="mm")
        y += selo_h + 22
    for linha in linhas:
        draw.text((LARGURA / 2, y), linha, font=f_gancho,
                  fill=_cor(molde["cor_texto"]), anchor="mt")
        y += altura_linha
    y += respiro
    caixa = ((LARGURA - w) // 2, y, w, h)
    y += h + respiro

    if mostrar_cta:
        bw = draw.textlength(molde["cta"], font=f_cta) + 2.7 * f_cta.size
        bx = (LARGURA - bw) / 2
        draw.rounded_rectangle([bx, y, bx + bw, y + cta_h], radius=cta_h // 2,
                               fill=_cor(molde["cor_cta"]))
        draw.text((LARGURA / 2, y + cta_h / 2), molde["cta"], font=f_cta,
                  fill=_cor(molde["cor_texto_cta"]), anchor="mm")
    return img, caixa


def _mascara_cantos(w: int, h: int, raio: int) -> Image.Image:
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, w - 1, h - 1], radius=raio, fill=255)
    return m


CORTE_PADRAO = {"topo": 0.0, "base": 0.0, "esq": 0.0, "dir": 0.0,
                "inicio": 0.0, "fim": 0.0}


def _sidecar_corte(video: Path) -> Path:
    return video.with_suffix(".corte.json")


def tem_corte_proprio(video: Path) -> bool:
    return _sidecar_corte(video).exists()


def corte_de(video: Path, molde: dict | None = None) -> dict:
    """Corte do video: o proprio (.corte.json) ou, sem ele, o corte padrao do molde.

    Bordas em % do quadro, tempo em segundos (fim 0 = ate o fim).
    """
    arq = _sidecar_corte(video)
    if arq.exists():
        return normalizar_corte(json.loads(arq.read_text(encoding="utf-8")))
    return normalizar_corte((molde or carregar_molde()).get("corte_padrao"))


def salvar_corte(video: Path, corte: dict | None) -> dict | None:
    """Grava o corte proprio do video. None apaga e o video volta pro corte padrao.

    Um corte proprio zerado continua gravado: e o jeito de um video escapar de um
    corte padrao que nao serve pra ele.
    """
    arq = _sidecar_corte(video)
    if corte is None:
        arq.unlink(missing_ok=True)
        return None
    limpo = normalizar_corte(corte)
    arq.write_text(json.dumps(limpo), encoding="utf-8")
    return limpo


def _numero(v) -> float:
    try:
        n = float(v or 0)
    except (TypeError, ValueError):
        return 0.0
    return n if n == n and n not in (float("inf"), float("-inf")) else 0.0  # NaN/inf


def normalizar_corte(corte: dict | None) -> dict:
    corte = corte if isinstance(corte, dict) else {}
    limpo = {k: max(0.0, _numero(corte.get(k))) for k in CORTE_PADRAO}
    for a, b in (("topo", "base"), ("esq", "dir")):
        # sobra sempre pelo menos 10% do quadro em cada eixo
        excesso = limpo[a] + limpo[b] - 90
        if excesso > 0:
            limpo[b] -= excesso
    return limpo


def area_corte(wh: tuple[int, int], corte: dict) -> tuple[int, int, int, int]:
    """(x, y, w, h) em pixels do pedaco do quadro que fica, com lados pares."""
    vw, vh = wh
    x = int(vw * corte["esq"] / 100) // 2 * 2
    y = int(vh * corte["topo"] / 100) // 2 * 2
    w = int(vw * (100 - corte["esq"] - corte["dir"]) / 100) // 2 * 2
    h = int(vh * (100 - corte["topo"] - corte["base"]) / 100) // 2 * 2
    return x, y, max(w, 2), max(h, 2)


def previa(molde: dict, gancho: str, quadro: Image.Image,
           corte: dict | None = None, parte: str | None = None) -> Image.Image:
    """O frame final de um quadro do video, sem passar pelo ffmpeg."""
    cx, cy, cw, ch = area_corte(quadro.size, normalizar_corte(corte))
    quadro = quadro.crop((cx, cy, cx + cw, cy + ch))
    img, (x, y, w, h) = montar_fundo(molde, gancho, quadro.size, parte)
    quadro = quadro.convert("RGB").resize((w, h))
    raio = int(molde["cantos_video"])
    img.paste(quadro, (x, y), _mascara_cantos(w, h, raio) if raio else None)
    return img


# Especificacao do Reels por API (developers.facebook.com, ig-user/media, 10/2026):
# MP4 sem edit lists e com moov no inicio; H.264 progressivo, GOP fechado, 4:2:0;
# 23-60 fps; ate 25 Mbps; AAC ate 48 kHz e 128 kbps; ate 15 min e 300 MB.
# O render antigo saia com edit list, AAC 160k e quadro-chave a cada 8s, e a Meta
# levava tanto para processar que o envio direto desistia em trechos de 77s.
TETO_MB = 280          # folga sob os 300 MB da Meta
TETO_KBPS = 8000


def kbps_maximo(segundos: float) -> int:
    """Teto de bitrate do video para o arquivo caber em TETO_MB nessa duracao."""
    if segundos <= 0:
        return TETO_KBPS
    cabe = TETO_MB * 8 * 1000 / segundos - 128   # menos o audio
    return max(600, min(TETO_KBPS, int(cabe * 0.92)))   # 8% para o conteiner


def codificacao_reels(segundos: float) -> list[str]:
    teto = kbps_maximo(segundos)
    return [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-r", "30",
        "-profile:v", "high", "-level:v", "4.1", "-pix_fmt", "yuv420p",
        "-maxrate", f"{teto}k", "-bufsize", f"{teto * 2}k",
        "-g", "60", "-keyint_min", "30",            # quadro-chave a cada 2s
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
        *SEM_METADADOS,
        "-use_editlist", "0", "-movflags", "+faststart",
    ]


# Nada de metadados no arquivo final: sem titulo/comentario/data herdados do
# original, sem "encoder=Lavf/Lavc" e sem o SEI do x264 (texto "x264 - core ...
# opcoes" gravado dentro do proprio video). Ficam so as caixas que o MP4 exige
# (handler generico VideoHandler/SoundHandler, idioma "und").
SEM_METADADOS = [
    "-map_metadata", "-1", "-map_chapters", "-1",
    "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
    "-metadata:s:v", "encoder=", "-metadata:s:a", "encoder=",
    "-bsf:v", "filter_units=remove_types=6",
]


def limpar_metadados(arquivo: Path) -> None:
    """Tira os metadados de um MP4 ja pronto, sem recodificar (copia os fluxos).
    Escreve num temporario e so troca no fim: se cair no meio, o original fica."""
    tmp = arquivo.with_name(arquivo.stem + ".limpando.parcial.mp4")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(arquivo), "-map", "0",
             "-c", "copy", *SEM_METADADOS, "-use_editlist", "0", "-movflags", "+faststart",
             str(tmp)],
            check=True, capture_output=True)
        tmp.replace(arquivo)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def renderizar(video: Path, saida: Path, molde: dict, gancho: str,
               corte: dict | None = None,
               progresso: Callable[[float], None] | None = None,
               legenda: list[dict] | None = None, parte: str | None = None) -> None:
    """Renderiza em um .parcial e so renomeia no fim: arquivo pela metade nunca
    aparece como pronto. `progresso` recebe de 0 a 1 enquanto o ffmpeg anda.

    `legenda`: falas (scripts.legenda_filmes.segmentos) queimadas no rodape da
    area do video. None ou vazia = sem legenda.
    """
    corte = normalizar_corte(corte)
    dur = duracao(video)
    if dur and corte["inicio"] >= dur:
        raise ValueError(f"inicio do corte ({corte['inicio']}s) passa do fim do video ({dur}s)")
    total = (min(corte["fim"], dur or corte["fim"]) if corte["fim"] > corte["inicio"]
             else dur) - corte["inicio"]
    cx, cy, cw, ch = area_corte(dimensoes(video), corte)
    tempo = []
    if corte["inicio"]:
        tempo += ["-ss", f"{corte['inicio']:.2f}"]
    if corte["fim"] > corte["inicio"]:
        tempo += ["-t", f"{corte['fim'] - corte['inicio']:.2f}"]
    with tempfile.TemporaryDirectory() as tmp:
        fundo_png = Path(tmp) / "fundo.png"
        fundo, (x, y, w, h) = montar_fundo(molde, gancho, (cw, ch), parte)
        fundo.save(fundo_png)
        entradas = ["-loop", "1", "-i", str(fundo_png), *tempo, "-i", str(video)]
        raio = int(molde["cantos_video"])
        if raio:
            # moldura: cantos na cor do fundo, miolo transparente, por cima do video
            moldura = Path(tmp) / "moldura.png"
            canto = Image.new("RGBA", (w, h), (*_cor(molde["cor_fundo"]), 255))
            canto.putalpha(_mascara_cantos(w, h, raio).point(lambda a: 255 - a))
            canto.save(moldura)
            entradas += ["-loop", "1", "-i", str(moldura)]
            filtro = (f"[1:v]crop={cw}:{ch}:{cx}:{cy},scale={w}:{h},setsar=1[v0];"
                      f"[v0][2:v]overlay=0:0:shortest=1[v];")
        else:
            filtro = f"[1:v]crop={cw}:{ch}:{cx}:{cy},scale={w}:{h},setsar=1[v];"
        queimar = ""
        if legenda:
            from scripts import legenda_filmes
            ass = legenda_filmes.escrever_ass(legenda, Path(tmp) / "legenda.ass",
                                              (x, y, w, h), (LARGURA, ALTURA))
            queimar = legenda_filmes.filtro_subtitles(ass) + ","
        filtro += f"[0:v][v]overlay={x}:{y}:shortest=1,{queimar}format=yuv420p[out]"
        parcial = saida.with_name(saida.stem + ".parcial.mp4")
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
            *entradas,
            "-filter_complex", filtro,
            "-map", "[out]", "-map", "1:a:0?",
            *codificacao_reels(total),
            str(parcial),
        ]
        with tempfile.TemporaryFile() as erros:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=erros, text=True)
            for linha in proc.stdout:
                if progresso and total > 0 and linha.startswith("out_time_us="):
                    try:
                        progresso(min(1.0, int(linha.split("=")[1]) / 1e6 / total))
                    except ValueError:
                        pass  # out_time_us=N/A no primeiro bloco
            if proc.wait() != 0:
                parcial.unlink(missing_ok=True)
                erros.seek(0)
                raise subprocess.CalledProcessError(
                    proc.returncode, cmd, stderr=erros.read().decode(errors="replace"))
        parcial.replace(saida)


def partes(dur_total: float, corte: dict, parte_max: float) -> list[tuple[float, float]]:
    """Trechos (inicio, fim) de ate `parte_max` segundos, em tamanhos IGUAIS.

    Iguais e nao "89 + 89 + sobra": 150s viram 2 x 75s, nunca 89s + 61s. Um trecho
    so devolve o corte como veio (fim 0 = ate o fim do video).
    """
    corte = normalizar_corte(corte)
    ini = corte["inicio"]
    fim = corte["fim"] if corte["fim"] > ini else dur_total
    if dur_total:
        fim = min(fim, dur_total)
    total = fim - ini
    if total <= parte_max or total <= 0:
        return [(ini, corte["fim"])]
    n = math.ceil(total / parte_max)
    passo = total / n
    return [(round(ini + k * passo, 3), round(ini + (k + 1) * passo, 3)) for k in range(n)]


def rotulo_parte(molde: dict, k: int, n: int) -> str | None:
    if n <= 1 or not molde["rotulo_parte"].strip():
        return None
    return molde["rotulo_parte"].replace("{n}", str(k)).replace("{total}", str(n))


def _fatiar(falas: list[dict], desloc: float, dur: float) -> list[dict]:
    """Falas de um trecho com o tempo zerado no comeco dele."""
    saida = []
    for f in falas:
        ini, fim = f["start"] - desloc, f["end"] - desloc
        if fim <= 0 or ini >= dur:
            continue
        g = {**f, "start": max(0.0, ini), "end": min(dur, fim)}
        if f.get("words"):
            g["words"] = [{**w, "start": w["start"] - desloc, "end": w["end"] - desloc}
                          for w in f["words"]
                          if w["end"] - desloc > 0 and w["start"] - desloc < dur]
        saida.append(g)
    return saida


def saida_parte(video: Path, k: int, n: int) -> Path:
    """Video de uma parte so continua com o nome antigo (<stem>_molde.mp4)."""
    return saida_de(video) if n <= 1 else SAIDA / f"{video.stem}_molde_p{k}.mp4"


def saidas_de(video: Path) -> list[Path]:
    """Renders prontos do video, em ordem de parte. Vazio = pendente."""
    unica = saida_de(video)
    if unica.exists():
        return [unica]
    prefixo = f"{video.stem}_molde_p"
    achados = []
    if SAIDA.exists():
        for f in SAIDA.iterdir():  # iterdir e nao glob: nome de video pode ter [ ] * ?
            if f.name.startswith(prefixo) and f.name.endswith(".mp4") \
                    and not f.name.endswith(".parcial.mp4"):
                meio = f.name[len(prefixo):-4]
                if meio.isdigit():
                    achados.append((int(meio), f))
    return [f for _, f in sorted(achados)]


def pronto(video: Path) -> bool:
    return bool(saidas_de(video))


def apagar_saidas(video: Path) -> int:
    alvos = saidas_de(video)
    for f in alvos:
        f.unlink(missing_ok=True)
    return len(alvos)


def renderizar_video(video: Path, molde: dict, gancho: str, corte: dict | None = None,
                     progresso: Callable[[float], None] | None = None,
                     legenda: list[dict] | None = None) -> list[Path]:
    """Renderiza o video inteiro: uma saida, ou uma por parte se passar de parte_max.

    Falhou uma parte = apaga as que ja sairam: video pela metade nao fica "pronto".
    """
    corte = normalizar_corte(corte)
    trechos = partes(duracao(video), corte, molde["parte_max"])
    n = len(trechos)
    apagar_saidas(video)  # sobra de um render com outro numero de partes
    feitos: list[Path] = []
    try:
        for k, (a, b) in enumerate(trechos, start=1):
            m = molde
            if n > 1 and k == n and molde["cta_ultima"].strip():
                m = {**molde, "cta": molde["cta_ultima"]}
            falas = legenda
            if legenda and n > 1:
                falas = _fatiar(legenda, a - corte["inicio"], b - a)
            destino = saida_parte(video, k, n)
            renderizar(video, destino, m, gancho, {**corte, "inicio": a, "fim": b},
                       progresso=(lambda f, k=k: progresso((k - 1 + f) / n)) if progresso else None,
                       legenda=falas, parte=rotulo_parte(molde, k, n))
            feitos.append(destino)
    except BaseException:
        for f in feitos:
            f.unlink(missing_ok=True)
        raise
    return feitos


def listar_videos(entrada: Path | None = None) -> list[Path]:
    entrada = entrada or ENTRADA
    entrada.mkdir(parents=True, exist_ok=True)
    return sorted(f for f in entrada.iterdir() if f.suffix.lower() in VIDEOS
                  and not f.name.endswith(".parcial.mp4"))


def gancho_de(video: Path, indice: int | None, molde: dict) -> str:
    """Gancho proprio (.txt) ou um automatico escolhido pelo NOME do arquivo.

    Pelo nome, e nao pela posicao na pasta: assim o gancho de um video nao muda
    quando outros entram ou saem da fila. `indice` ficou so por compatibilidade.
    """
    sidecar = video.with_suffix(".txt")
    if sidecar.exists():
        texto = sidecar.read_text(encoding="utf-8").strip()
        if texto:
            return texto
    ganchos = molde["ganchos"] or PADRAO["ganchos"]
    return ganchos[zlib.crc32(video.name.encode("utf-8")) % len(ganchos)]


def saida_de(video: Path, saida: Path | None = None) -> Path:
    return (saida or SAIDA) / f"{video.stem}_molde.mp4"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--entrada", type=Path, default=None)
    p.add_argument("--saida", type=Path, default=None)
    p.add_argument("--gancho", help="mesmo gancho para todos os videos")
    args = p.parse_args()

    molde = carregar_molde()
    args.entrada = args.entrada or ENTRADA
    args.saida = args.saida or SAIDA
    if args.saida != SAIDA:  # saidas_de/partes usam a pasta do modulo
        globals()["SAIDA"] = args.saida
    args.saida.mkdir(parents=True, exist_ok=True)
    videos = listar_videos(args.entrada)
    if not videos:
        print(f"nenhum video em {args.entrada}")
        return

    for i, video in enumerate(videos):
        if pronto(video):
            print(f"pulando (ja feito): {video.name}")
            continue
        gancho = args.gancho or gancho_de(video, i, molde)
        print(f"[{i + 1}/{len(videos)}] {video.name} -> {gancho}")
        try:
            renderizar_video(video, molde, gancho, corte_de(video, molde))
        except subprocess.CalledProcessError as exc:
            print(f"  falhou: {exc}")


if __name__ == "__main__":
    main()
