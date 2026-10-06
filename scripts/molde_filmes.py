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
           "tamanho_cta": (20, 90), "margem": (0, 300), "cantos_video": (0, 120)}


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


def montar_fundo(molde: dict, gancho: str, video_wh: tuple[int, int]
                 ) -> tuple[Image.Image, tuple[int, int, int, int]]:
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
           corte: dict | None = None) -> Image.Image:
    """O frame final de um quadro do video, sem passar pelo ffmpeg."""
    cx, cy, cw, ch = area_corte(quadro.size, normalizar_corte(corte))
    quadro = quadro.crop((cx, cy, cx + cw, cy + ch))
    img, (x, y, w, h) = montar_fundo(molde, gancho, quadro.size)
    quadro = quadro.convert("RGB").resize((w, h))
    raio = int(molde["cantos_video"])
    img.paste(quadro, (x, y), _mascara_cantos(w, h, raio) if raio else None)
    return img


def renderizar(video: Path, saida: Path, molde: dict, gancho: str,
               corte: dict | None = None,
               progresso: Callable[[float], None] | None = None,
               legenda: list[dict] | None = None) -> None:
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
        fundo, (x, y, w, h) = montar_fundo(molde, gancho, (cw, ch))
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
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-r", "30",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
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
    args.saida.mkdir(parents=True, exist_ok=True)
    videos = listar_videos(args.entrada)
    if not videos:
        print(f"nenhum video em {args.entrada}")
        return

    for i, video in enumerate(videos):
        destino = saida_de(video, args.saida)
        if destino.exists():
            print(f"pulando (ja feito): {video.name}")
            continue
        gancho = args.gancho or gancho_de(video, i, molde)
        print(f"[{i + 1}/{len(videos)}] {video.name} -> {gancho}")
        try:
            renderizar(video, destino, molde, gancho, corte_de(video, molde))
        except subprocess.CalledProcessError as exc:
            print(f"  falhou: {exc}")


if __name__ == "__main__":
    main()
