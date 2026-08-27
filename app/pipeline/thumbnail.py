"""Capa do corte: escolhe o melhor frame e estampa o gancho por cima.

Um frame cru do meio do trecho e a capa mais fraca possivel — e a capa e a unica
coisa que a pessoa ve antes de decidir clicar. Aqui o frame e escolhido (nitido,
com rosto grande e bem exposto, e nao no meio de um piscar) e recebe o tratamento
que canal grande usa: texto curto em caixa alta, fonte pesada, contorno grosso,
uma palavra em cor de destaque e escurecimento por tras do texto para o contraste
nunca depender da imagem.

Duas saidas: 1280x720 para o YouTube e 1080x1920 para Reels/TikTok/Shorts.
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

from app.config import settings

log = logging.getLogger(__name__)

# Fontes pesadas do Windows, em ordem de preferencia. Impact e a fonte classica de
# thumbnail; Arial Black e a mesma familia ja usada na legenda dos cortes.
FONT_CANDIDATES = (
    r"C:\Windows\Fonts\impact.ttf",
    r"C:\Windows\Fonts\ariblk.ttf",
    r"C:\Windows\Fonts\seguibl.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
)

# Paleta de destaque. Nenhuma cor e fixa: a escolhida e a que tiver mais contraste
# com o fundo REAL daquela capa. Amarelo some em parede clara, ciano some em ceu.
HIGHLIGHT_PALETTE = (
    (255, 216, 0),    # amarelo
    (255, 122, 0),    # laranja
    (0, 229, 255),    # ciano
    (124, 252, 0),    # verde-limao
    (255, 60, 90),    # vermelho-rosa
)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)

CANDIDATE_FRAMES = 14         # quantos frames disputam a capa
# Peso da frontalidade na nota. Alto de proposito: rosto olhando para a mesa
# arruina a capa mesmo nitido e grande.
FRONTAL_WEIGHT = 3.0
MIN_CONTRAST = 4.5            # razao WCAG minima entre texto e fundo
# Raio da busca em torno do instante que a IA apontou. 1,5 s da margem para achar
# um frame nitido sem sair do momento: mais que isso e voltar a sortear a cena.
SEARCH_RADIUS = 2.5

# Enquadramento da capa. O video fonte costuma trazer moldura, tarja e ate QR de
# patrocinio de outro canal nas bordas — INSET descarta essa faixa antes de tudo.
PANEL_BORDER = (255, 216, 0)   # moldura da capa e do painel do apresentador
BADGE_BG = (208, 20, 34)       # selo curto ("TENSAO RECORDE")

# Altura do bloco do apresentador na 9:16. 0.44 espelha a proporcao que funcionou
# no 16:9 (coluna de 42% da largura): duas regioes claras, separadas por um risco.
VERTICAL_PANEL_FRAC = 0.44

INSET = 0.055        # fracao de cada borda que nunca entra na capa
FACE_RATIO = 0.55    # altura do rosto como fracao da altura da capa
MAX_ZOOM = 2.2       # ampliacao maxima, para nao transformar pixel em borrao


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    """Luminancia relativa (WCAG): quanto de luz a cor emite aos olhos.

    Nao e a media dos canais — verde pesa muito mais que azul, e por isso que
    amarelo (alto verde) parece claro e azul puro parece escuro.
    """
    canais = []
    for valor in rgb:
        c = valor / 255.0
        canais.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = canais
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(c1: tuple[int, int, int], c2: tuple[int, int, int]) -> float:
    """Razao de contraste WCAG entre duas cores (1 = iguais, 21 = preto/branco)."""
    l1, l2 = relative_luminance(c1), relative_luminance(c2)
    claro, escuro = max(l1, l2), min(l1, l2)
    return (claro + 0.05) / (escuro + 0.05)


def color_distance(c1: tuple[int, int, int], c2: tuple[int, int, int]) -> float:
    """Distancia entre duas cores, ponderada como o olho enxerga.

    Complementa o contraste WCAG, que so mede luminancia: amarelo e branco tem
    luminancia parecida e ainda assim ninguem confunde os dois.
    """
    r = (c1[0] + c2[0]) / 2  # a percepcao de diferenca no vermelho muda com o nivel
    dr, dg, db = c1[0] - c2[0], c1[1] - c2[1], c1[2] - c2[2]
    return ((2 + r / 256) * dr * dr + 4 * dg * dg + (2 + (255 - r) / 256) * db * db) ** 0.5


def pick_colors(fundo: tuple[int, int, int]) -> tuple[tuple, tuple, tuple]:
    """Escolhe (texto, destaque, contorno) que sobrevivem NESTE fundo.

    O contorno e sempre o oposto do texto: e ele que segura a legibilidade quando
    o texto atravessa uma area de fundo irregular — metade sobre a parede escura,
    metade sobre a camisa clara.
    """
    texto = WHITE if contrast_ratio(WHITE, fundo) >= contrast_ratio(BLACK, fundo) else BLACK
    contorno = BLACK if texto == WHITE else WHITE

    # A paleta esta em ordem de preferencia: fica no amarelo (a cor que o olho
    # associa a thumbnail) e so troca quando ele nao sobrevive neste fundo. Pontuar
    # por contraste puro elegeria sempre a mesma cor e deixaria toda capa igual.
    for cor in HIGHLIGHT_PALETTE:
        # Duas exigencias diferentes, e cada uma pede sua medida:
        # - contra o FUNDO, contraste WCAG (luminancia) — e o que decide legibilidade;
        # - contra o TEXTO, distancia de cor — amarelo e branco tem luminancia
        #   parecida (WCAG diria 1.07) e mesmo assim se distinguem de longe, porque
        #   o que separa os dois e o matiz.
        if contrast_ratio(cor, fundo) >= MIN_CONTRAST and color_distance(cor, texto) >= 120:
            return texto, cor, contorno

    # Nenhuma passou: pega a de maior contraste com o fundo, e se nem essa serve,
    # desiste do realce em vez de estampar uma palavra ilegivel.
    melhor = max(HIGHLIGHT_PALETTE, key=lambda c: contrast_ratio(c, fundo))
    if contrast_ratio(melhor, fundo) < 2.0:
        return texto, texto, contorno
    return texto, melhor, contorno


def _font_path() -> str | None:
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


# Palavras que os filtros automaticos do YouTube tratam como sinal de conteudo
# inadequado para anunciante. O assunto continua o mesmo — o que muda e a grafia
# na CAPA, para o classificador de imagem nao derrubar o alcance por uma palavra.
# Nao e para esconder do espectador: o video entrega exatamente o que promete.
PALAVRAS_SENSIVEIS = (
    # morte e violencia
    "morte", "morteu", "morreu", "morto", "morta", "matou", "matar", "assassinato",
    "assassino", "suicidio", "suicídio", "estupro", "estuprou", "tiro", "tiros",
    "sangue", "cadaver", "cadáver", "massacre", "tortura",
    # sexual
    "sexo", "sexual", "porno", "pornô", "pornografia", "prostituta", "prostituicao",
    "prostituição", "transar", "transou", "nudes", "nude", "puta", "putaria",
    "orgia", "virgem", "trair", "traicao", "traição",
    # drogas
    "droga", "drogas", "cocaina", "cocaína", "maconha", "crack", "traficante",
    "trafico", "tráfico", "usuario de droga",
    # outros gatilhos de desmonetizacao
    "arma", "armas", "bandido", "presidio", "presídio", "cadeia", "preso",
)

# Trocas que preservam a leitura no tamanho de miniatura. So UMA letra por palavra
# e trocada: "M0RTE" continua legivel de relance, "M0RT3" ja custa meio segundo do
# leitor — e meio segundo e o tempo que a capa tem.
_MASCARA = {"o": "0", "a": "@", "e": "3", "i": "1", "u": "ü", "s": "$"}


def mask_word(palavra: str) -> str:
    """Mascara UMA vogal da palavra, preservando caixa e pontuacao ao redor."""
    for i, ch in enumerate(palavra):
        sub = _MASCARA.get(ch.lower())
        if sub:
            return palavra[:i] + sub + palavra[i + 1:]
    return palavra


def _limpa(palavra: str) -> str:
    """Sem acento, sem pontuacao, minusculo — para comparar com a lista."""
    base = "".join(c for c in unicodedata.normalize("NFKD", palavra)
                   if not unicodedata.combining(c))
    return re.sub(r"[^\w]", "", base).lower()


def mask_sensitive(texto: str) -> str:
    """Mascara as palavras sensiveis do texto da capa.

    Roda no RENDER, nao so no prompt: o modelo as vezes esquece a regra, e a capa
    e o unico lugar onde a palavra vira imagem — que e justamente o que o filtro
    automatico le. Aqui a garantia nao depende de o modelo lembrar.

    Preserva os asteriscos de destaque (`*PALAVRA*`), que sao sintaxe nossa.
    """
    if not (texto or "").strip():
        return texto

    saida = []
    for pedaco in texto.split():
        marcado = "*" in pedaco
        nu = pedaco.replace("*", "")
        if _limpa(nu) in _SENSIVEIS_NORMALIZADAS:
            nu = mask_word(nu)
            # Recompoe a marcacao de destaque exatamente como veio.
            pedaco = f"*{nu}*" if marcado else nu
        saida.append(pedaco)
    return " ".join(saida)


_SENSIVEIS_NORMALIZADAS = frozenset(
    "".join(c for c in unicodedata.normalize("NFKD", p) if not unicodedata.combining(c)).lower()
    for p in PALAVRAS_SENSIVEIS
)


def parse_highlight(texto: str) -> list[tuple[str, bool]]:
    """Quebra "ELE *MENTIU* NA CARA" em palavras, marcando as destacadas.

    O modelo marca com asteriscos a parte que deve sair colorida. Sem marcacao,
    destaca a palavra mais longa — sempre ter uma cor quebra a parede de branco.
    """
    palavras: list[tuple[str, bool]] = []
    for pedaco in texto.split():
        destaque = "*" in pedaco
        limpo = pedaco.replace("*", "").strip()
        if limpo:
            palavras.append((limpo, destaque))

    if palavras and not any(d for _, d in palavras):
        maior = max(range(len(palavras)), key=lambda i: len(palavras[i][0]))
        palavras[maior] = (palavras[maior][0], True)
    return palavras


def _face_rows(img: Any) -> list[Any]:
    """Linhas cruas do YuNet: caixa + 5 landmarks (olhos, nariz, cantos da boca).

    `clips._face_boxes` joga os landmarks fora, e sao eles que dizem se a pessoa
    esta olhando para a camera. Sem detector com landmark, devolve vazio.
    """
    from app.pipeline import clips

    yunet = clips._load_yunet()
    if yunet is None:
        return []
    try:
        h, w = img.shape[:2]
        yunet.setInputSize((w, h))
        _, faces = yunet.detect(img)
        return list(faces) if faces is not None else []
    except Exception:  # noqa: BLE001 — sem landmark a nota cai no criterio antigo
        return []


def frontality(face: Any) -> float:
    """0 a 1: quanto a pessoa encara a camera. 1 = de frente, 0 = perfil/cabeca virada.

    Layout do YuNet: [x, y, w, h, olho_dir(x,y), olho_esq(x,y), nariz(x,y),
    boca_dir(x,y), boca_esq(x,y), score].

    De frente, o nariz fica a meio caminho entre os olhos e a linha dos olhos fica
    horizontal. De perfil ou com a cabeca baixa, o nariz desloca para um lado e a
    linha dos olhos inclina. E o que separa a capa em que a pessoa "fala com voce"
    da capa em que ela olha para a mesa — a diferenca que mais pesa numa thumbnail.
    """
    try:
        olho_d = (float(face[4]), float(face[5]))
        olho_e = (float(face[6]), float(face[7]))
        nariz = (float(face[8]), float(face[9]))
    except (IndexError, TypeError, ValueError):
        return 0.5  # sem landmark, nao penaliza nem premia

    dist_olhos = ((olho_e[0] - olho_d[0]) ** 2 + (olho_e[1] - olho_d[1]) ** 2) ** 0.5
    if dist_olhos < 1:
        return 0.5

    # Nariz centralizado entre os olhos.
    meio_x = (olho_d[0] + olho_e[0]) / 2
    desvio = abs(nariz[0] - meio_x) / dist_olhos      # 0 de frente, ~0.5+ de perfil
    nota_giro = max(0.0, 1.0 - desvio / 0.45)

    # Linha dos olhos horizontal (cabeca reta, nao tombada nem baixa).
    inclinacao = abs(olho_e[1] - olho_d[1]) / dist_olhos
    nota_tilt = max(0.0, 1.0 - inclinacao / 0.5)

    return max(0.0, min(1.0, 0.7 * nota_giro + 0.3 * nota_tilt))


def _score_frame(img: Any) -> float:
    """Nota de um candidato a capa: nitidez, rosto grande, de frente e bem exposto.

    Nao detecta olho fechado (5 landmarks nao dizem isso), mas frame borrado —
    que e onde o piscar e o movimento brusco costumam cair — perde no foco.
    """
    import cv2
    from app.pipeline import clips

    cinza = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = cinza.shape[:2]

    # Variancia do laplaciano: o indicador classico de foco.
    nitidez = float(cv2.Laplacian(cinza, cv2.CV_64F).var())
    nota = min(nitidez / 400.0, 2.5)

    # Exposicao: o alvo e descartar corte de cena (quadro preto) e flash estourado,
    # NAO cena escura. Estudio com luz neon roda em brilho medio ~34; um limiar de
    # 40 penalizava quase todo frame e fazia o unico frame claro vencer mesmo sendo
    # o pior enquadrado. Rampa suave em vez de degrau, e limites bem nas pontas.
    brilho = float(cinza.mean())
    if brilho < 18:
        nota -= 1.5 * (18 - brilho) / 18
    elif brilho > 235:
        nota -= 1.5 * (brilho - 235) / 20

    rows = _face_rows(img)
    if rows:
        maior = max(rows, key=lambda f: float(f[2]) * float(f[3]))
        area = float(maior[2]) * float(maior[3])
        nota += min((area / (w * h)) * 12.0, 2.0)
        # Peso alto de proposito: rosto grande olhando para a mesa vale menos que
        # rosto medio encarando a camera.
        nota += frontality(maior) * FRONTAL_WEIGHT
        return nota

    boxes = clips._face_boxes(img)
    if boxes:
        maior_area = max(b[2] * b[3] for b in boxes)
        nota += min((maior_area / (w * h)) * 12.0, 2.0)
    return nota


def window(start: float, duration: float, alvo: float | None) -> tuple[float, float]:
    """Faixa de tempo onde procurar o frame da capa.

    Com `alvo` (o instante que a IA apontou como clima do trecho), a busca fica
    numa janela curta em volta dele: o objetivo passa a ser "o melhor frame DAQUELE
    momento", nao "o frame mais nitido do trecho inteiro". Era essa a origem da
    capa que parecia sorteada — nitidez e um criterio tecnico e nao sabe qual
    instante significa alguma coisa.

    Sem alvo, volta ao comportamento antigo: o trecho inteiro, menos as pontas.
    """
    if alvo is None:
        return start + duration * 0.12, start + duration * 0.88

    alvo = max(start, min(start + duration, alvo))
    raio = min(SEARCH_RADIUS, duration / 2)
    ini = max(start + duration * 0.04, alvo - raio)
    fim = min(start + duration * 0.96, alvo + raio)
    if fim <= ini:  # corte curtissimo: nao ha janela, usa o alvo cru
        return alvo, alvo
    return ini, fim


def pick_frame(video_path: Path, start: float, duration: float,
               out_dir: Path, alvo: float | None = None) -> tuple[Path | None, float]:
    """Extrai candidatos na janela da capa e devolve o melhor frame."""
    try:
        import cv2
    except ImportError:
        return None, alvo if alvo is not None else start + duration / 2

    ini, fim = window(start, duration, alvo)
    passo = (fim - ini) / max(1, CANDIDATE_FRAMES - 1)

    melhor: tuple[float, Path, float] | None = None
    for i in range(CANDIDATE_FRAMES):
        t = ini + i * passo
        destino = out_dir / f"cand_{i:02d}.jpg"
        cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video_path),
               "-frames:v", "1", "-q:v", "2", str(destino)]
        try:
            if subprocess.run(cmd, capture_output=True).returncode != 0:
                continue
        except OSError:
            return None, alvo if alvo is not None else start + duration / 2
        img = cv2.imread(str(destino))
        if img is None:
            continue
        nota = _score_frame(img)
        if melhor is None or nota > melhor[0]:
            melhor = (nota, destino, t)

    if melhor is None:
        return None, alvo if alvo is not None else start + duration / 2
    return melhor[1], melhor[2]


def _fit_lines(draw: Any, palavras: list[tuple[str, bool]], largura_util: int,
               tamanho_inicial: int, font_file: str, max_linhas: int = 2):
    """Maior corpo de fonte que faz o texto caber em ate `max_linhas` linhas."""
    from PIL import ImageFont

    tamanho = tamanho_inicial
    while tamanho > 24:
        fonte = ImageFont.truetype(font_file, tamanho)
        espaco = draw.textlength(" ", font=fonte)
        linhas: list[list[tuple[str, bool]]] = [[]]
        largura_atual = 0.0
        estourou = False
        for palavra, destaque in palavras:
            largura = draw.textlength(palavra, font=fonte)
            if largura > largura_util:  # palavra sozinha ja nao cabe
                estourou = True
                break
            extra = largura if not linhas[-1] else espaco + largura
            if largura_atual + extra <= largura_util:
                linhas[-1].append((palavra, destaque))
                largura_atual += extra
            else:
                linhas.append([(palavra, destaque)])
                largura_atual = largura
        if not estourou and len(linhas) <= max_linhas:
            return fonte, linhas
        tamanho -= 6

    fonte = ImageFont.truetype(font_file, 24)
    return fonte, [palavras]


def compose(frame_path: Path, texto: str, output_path: Path,
            size: tuple[int, int] = (1280, 720)) -> Path | None:
    """Monta a capa: frame tratado + faixa escura + gancho estampado."""
    font_file = _font_path()
    if font_file is None:
        log.warning("nenhuma fonte pesada encontrada; capa fica sem texto")
        return None

    try:
        from PIL import Image, ImageDraw, ImageEnhance, ImageFilter
    except ImportError:
        log.warning("Pillow indisponivel; capa fica sem texto")
        return None

    largura, altura = size
    vertical = altura > largura

    img = Image.open(frame_path).convert("RGB")

    # Enquadra no apresentador e descarta a moldura/tarja do video fonte, para a
    # capa nao herdar a arte (nem o patrocinio) de outro canal.
    cx, cy, cw, ch = crop_box(img.width, img.height, _biggest_face(img), largura / altura)
    img = img.crop((cx, cy, cx + cw, cy + ch))

    # Preenche o quadro sem distorcer: escala pelo lado que falta e recorta o centro.
    escala = max(largura / img.width, altura / img.height)
    img = img.resize((max(1, round(img.width * escala)), max(1, round(img.height * escala))),
                     Image.LANCZOS)
    esquerda = (img.width - largura) // 2
    topo = (img.height - altura) // 2
    img = img.crop((esquerda, topo, esquerda + largura, topo + altura))

    # Tratamento de capa: mais contraste e cor, que e o que separa da timeline.
    img = ImageEnhance.Contrast(img).enhance(1.18)
    img = ImageEnhance.Color(img).enhance(1.25)
    img = ImageEnhance.Sharpness(img).enhance(1.4)

    palavras = parse_highlight(mask_sensitive(texto))
    if not palavras:
        img.save(output_path, quality=92)
        return output_path

    margem = int(largura * 0.06)
    largura_util = largura - 2 * margem
    draw = ImageDraw.Draw(img)
    corpo = int(altura * (0.085 if vertical else 0.17))
    fonte, linhas = _fit_lines(draw, palavras, largura_util, corpo, font_file,
                               max_linhas=3 if vertical else 2)

    alturas = [draw.textbbox((0, 0), "Ay", font=fonte)[3] for _ in linhas]
    entrelinha = int(fonte.size * 0.14)
    bloco = sum(alturas) + entrelinha * (len(linhas) - 1)

    y = _pick_band(img, bloco, vertical)

    # Escurece a faixa do texto o quanto ESTE fundo exigir: uma cena clara precisa
    # de mais veu que uma cena ja escura, e aplicar o mesmo veu nos dois casos
    # apaga a imagem a toa num caso e nao resolve no outro.
    folga = int(bloco * 0.55)
    topo_faixa, base_faixa = max(0, y - folga), min(altura, y + bloco + folga)
    fundo = _region_color(img, topo_faixa, base_faixa)

    veu = _veil_strength(fundo)
    if veu > 0:
        mascara = Image.new("L", (largura, altura), 0)
        ImageDraw.Draw(mascara).rectangle([0, topo_faixa, largura, base_faixa], fill=veu)
        mascara = mascara.filter(ImageFilter.GaussianBlur(radius=int(bloco * 0.35) or 1))
        img = Image.composite(Image.new("RGB", img.size, BLACK), img, mascara)
        draw = ImageDraw.Draw(img)
        fundo = _region_color(img, topo_faixa, base_faixa)  # remede depois do veu

    cor_texto, cor_destaque, cor_contorno = pick_colors(fundo)

    contorno = max(4, int(fonte.size * 0.13))
    for linha, alt in zip(linhas, alturas):
        largura_linha = sum(draw.textlength(p, font=fonte) for p, _ in linha)
        largura_linha += draw.textlength(" ", font=fonte) * (len(linha) - 1)
        x = (largura - largura_linha) / 2
        for palavra, destaque in linha:
            draw.text((x, y), palavra, font=fonte,
                      fill=cor_destaque if destaque else cor_texto,
                      stroke_width=contorno, stroke_fill=cor_contorno)
            x += draw.textlength(palavra + " ", font=fonte)
        y += alt + entrelinha

    img.save(output_path, quality=92)
    return output_path


def _fit_cover(img: Any, largura: int, altura: int) -> Any:
    """Preenche largura x altura sem distorcer (escala e recorta o centro)."""
    from PIL import Image

    escala = max(largura / img.width, altura / img.height)
    img = img.resize((max(1, round(img.width * escala)), max(1, round(img.height * escala))),
                     Image.LANCZOS)
    e, t = (img.width - largura) // 2, (img.height - altura) // 2
    return img.crop((e, t, e + largura, t + altura))


def compose_composite(frame_path: Path | None, art_path: Path | None, texto: str,
                      output_path: Path, badge: str = "",
                      size: tuple[int, int] = (1280, 720),
                      presenter: bool = True) -> Path | None:
    """Capa: imagem gerada por IA ao fundo, gancho estampado, apresentador opcional.

    A imagem vem do que se DIZ no trecho (a IA escreve o prompt lendo a
    transcricao), nao de um frame do video — o video e podcast e todo frame e a
    mesma pessoa na mesma cadeira.

    Serve as duas orientacoes:
      16:9  apresentador em coluna colada a direita, texto na coluna esquerda;
      9:16  apresentador em faixa no topo, texto no tercao de baixo — que e onde
            o dedo nao cobre e o app nao poe interface.

    `presenter=False` usa so a arte, em tela cheia. Ordem importa: o escurecimento
    do texto e aplicado ANTES do painel, senao corta o rosto com uma faixa preta.
    """
    font_file = _font_path()
    if font_file is None:
        return None
    try:
        from PIL import Image, ImageDraw, ImageEnhance
    except ImportError:
        return None

    largura, altura = size
    vertical = altura > largura
    borda = max(6, int(min(largura, altura) * 0.014))

    frame = None
    if presenter and frame_path is not None and Path(frame_path).exists():
        frame = Image.open(frame_path).convert("RGB")

    if art_path is not None and Path(art_path).exists():
        base = _fit_cover(Image.open(art_path).convert("RGB"), largura, altura)
    elif frame is not None:
        base = _fit_cover(frame, largura, altura)   # degradacao: sem arte, usa o video
        frame = None
    else:
        return None

    base = ImageEnhance.Contrast(base).enhance(1.14)
    base = ImageEnhance.Color(base).enhance(1.28)

    # Geometria do painel do apresentador e da coluna de texto.
    usa_painel = frame is not None
    if usa_painel and vertical:
        # Retrato nao comporta coluna lateral: a mesma ideia vira bloco no topo,
        # com o risco amarelo fazendo o papel do divisor vertical do 16:9.
        pw, ph = largura, int(altura * VERTICAL_PANEL_FRAC)
        col_texto = largura
    elif usa_painel:
        pw, ph = int(largura * 0.42), altura      # coluna na direita
        col_texto = largura - pw
    else:
        pw = ph = 0
        col_texto = largura

    palavras = parse_highlight(mask_sensitive(texto))
    draw = ImageDraw.Draw(base)
    margem = int(largura * (0.055 if vertical else 0.035))
    fonte = linhas = alturas = None
    entrelinha = bloco = 0

    if palavras:
        # Teto de altura do texto. Na 9:16 com apresentador, o texto vive SO no
        # bloco da arte — e ainda cede a faixa do selo, senao ele acaba impresso
        # em cima do rosto (que foi o que aconteceu na primeira versao).
        reserva_selo = int(altura * 0.075) if badge else 0
        rodape = int(altura * (0.09 if vertical else 0.055))
        if usa_painel and vertical:
            teto_bloco = altura - ph - reserva_selo - rodape - int(altura * 0.03)
        else:
            teto_bloco = altura - reserva_selo - rodape - int(altura * 0.06)

        corpo = int(altura * (0.098 if vertical else 0.20))
        for _ in range(8):   # encolhe ate caber; 8 passos chegam em qualquer caso
            fonte, linhas = _fit_lines(draw, palavras, col_texto - margem * 2, corpo,
                                       font_file, max_linhas=4)
            alturas = [draw.textbbox((0, 0), "Ay", font=fonte)[3] for _ in linhas]
            entrelinha = int(fonte.size * 0.06)
            bloco = sum(alturas) + entrelinha * (len(linhas) - 1)
            if bloco <= teto_bloco or corpo <= 28:
                break
            corpo = int(corpo * 0.88)

        # Degrade escuro subindo do rodape, restrito a coluna do texto.
        folga = int(altura * (0.16 if vertical else 0.26))
        topo_scrim = max(0, altura - bloco - folga)
        if usa_painel and vertical:
            # O degrade comeca abaixo do bloco do apresentador, nunca por cima dele.
            topo_scrim = max(topo_scrim, int(altura * VERTICAL_PANEL_FRAC))
        h_scrim = altura - topo_scrim
        mascara = Image.new("L", (col_texto, h_scrim), 0)
        md = ImageDraw.Draw(mascara)
        for i in range(h_scrim):
            md.line([(0, i), (col_texto, i)],
                    fill=int(235 * (i / max(1, h_scrim - 1)) ** 1.35))
        escuro = Image.new("RGB", (col_texto, h_scrim), BLACK)
        recorte = base.crop((0, topo_scrim, col_texto, altura))
        base.paste(Image.composite(escuro, recorte, mascara), (0, topo_scrim))

    if usa_painel:
        cx, cy, cw, ch = crop_box(frame.width, frame.height, _biggest_face(frame), pw / ph)
        painel = _fit_cover(frame.crop((cx, cy, cx + cw, cy + ch)), pw, ph)
        painel = ImageEnhance.Contrast(painel).enhance(1.12)
        painel = ImageEnhance.Color(painel).enhance(1.15)
        # O frame do video e mais macio que a arte gerada; sem isto o painel
        # parece desfocado ao lado dela.
        painel = ImageEnhance.Sharpness(painel).enhance(1.6)
        if vertical:
            base.paste(painel, (0, 0))
            ImageDraw.Draw(base).rectangle([0, ph, largura, ph + borda], fill=PANEL_BORDER)
        else:
            base.paste(painel, (largura - pw, 0))
            ImageDraw.Draw(base).rectangle(
                [largura - pw - borda, 0, largura - pw - 1, altura], fill=PANEL_BORDER
            )

    draw = ImageDraw.Draw(base)
    if palavras:
        fundo = _region_color(base, altura - bloco - borda, altura - borda)
        cor_texto, cor_destaque, cor_contorno = pick_colors(fundo)

        y = altura - bloco - rodape
        if badge:
            _draw_badge(base, draw, badge.upper()[:28], font_file, margem,
                        y - int(altura * 0.018), altura)

        contorno = max(5, int(fonte.size * 0.11))
        for linha, alt in zip(linhas, alturas):
            x = margem
            for palavra, destaque in linha:
                draw.text((x, y), palavra, font=fonte,
                          fill=cor_destaque if destaque else cor_texto,
                          stroke_width=contorno, stroke_fill=cor_contorno)
                x += draw.textlength(palavra + " ", font=fonte)
            y += alt + entrelinha

    _draw_border(base, borda)
    base.save(output_path, quality=93)
    return output_path


def _draw_border(img: Any, borda: int) -> None:
    """Moldura colorida: e o que descola a capa do fundo branco da timeline."""
    from PIL import ImageDraw

    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, img.width - 1, img.height - 1], outline=PANEL_BORDER, width=borda)


def _draw_badge(img: Any, draw: Any, texto: str, font_file: str, x: int, y: int,
                altura: int) -> int | None:
    """Selo curto acima do texto ("TENSAO RECORDE"). Devolve None se nao coube."""
    from PIL import ImageFont

    try:
        fonte = ImageFont.truetype(font_file, int(altura * 0.048))
    except OSError:
        return None
    caixa = draw.textbbox((0, 0), texto, font=fonte)
    lw, lh = caixa[2] - caixa[0], caixa[3] - caixa[1]
    pad = int(lh * 0.35)
    topo = y - lh - pad * 2
    if topo < 0:
        return None
    draw.rectangle([x, topo, x + lw + pad * 2, topo + lh + pad * 2], fill=BADGE_BG)
    draw.text((x + pad, topo + pad - caixa[1]), texto, font=fonte, fill=WHITE)
    return None


def crop_box(largura: int, altura: int, rosto: tuple[int, int, int, int] | None,
             proporcao: float) -> tuple[int, int, int, int]:
    """Recorte 16:9 que joga fora a moldura do video fonte e aproxima do rosto.

    O material de origem quase nunca e camera crua: vem com moldura, tarja, marca
    d'agua e ate QR de patrocinio de OUTRO canal. Usar o quadro inteiro herda tudo
    isso na sua capa. Entao o recorte comeca descartando INSET de cada borda — que
    e onde essa tralha mora — e, havendo rosto, centraliza nele.

    Enquadra com headroom: o rosto fica no terco superior, nao no meio, que e como
    capa de canal grande e composta. O zoom e limitado por MAX_ZOOM para nao
    ampliar poucos pixels ate virar borrao.
    """
    if rosto is None:
        # Sem rosto: recorte central ja sem as bordas.
        larg = int(largura * (1 - 2 * INSET))
        alt = int(larg / proporcao)
        if alt > altura * (1 - 2 * INSET):
            alt = int(altura * (1 - 2 * INSET))
            larg = int(alt * proporcao)
        x = (largura - larg) // 2
        y = max(int(altura * INSET), (altura - alt) // 2)
        return x, y, larg, alt

    fx, fy, fw, fh = rosto
    # Altura de recorte que deixa o rosto ocupando FACE_RATIO do quadro.
    alt = int(fh / FACE_RATIO)
    alt = max(alt, int(altura / MAX_ZOOM))          # nao amplia alem do limite
    alt = min(alt, int(altura * (1 - 2 * INSET)))   # nunca reencosta nas bordas
    larg = int(alt * proporcao)
    if larg > largura * (1 - 2 * INSET):
        larg = int(largura * (1 - 2 * INSET))
        alt = int(larg / proporcao)

    centro_x = fx + fw // 2
    # Headroom: o topo da cabeca fica a ~18% do topo do recorte.
    y = int(fy - alt * 0.18)
    x = centro_x - larg // 2

    # Empurra para dentro dos limites uteis (respeitando a borda descartada).
    lim_x0, lim_y0 = int(largura * INSET), int(altura * INSET)
    lim_x1, lim_y1 = largura - lim_x0, altura - lim_y0
    if larg >= lim_x1 - lim_x0:
        x, larg = lim_x0, lim_x1 - lim_x0
    else:
        x = max(lim_x0, min(lim_x1 - larg, x))
    if alt >= lim_y1 - lim_y0:
        y, alt = lim_y0, lim_y1 - lim_y0
    else:
        y = max(lim_y0, min(lim_y1 - alt, y))
    return x, y, larg, alt


def _biggest_face(img: Any) -> tuple[int, int, int, int] | None:
    try:
        import cv2
        import numpy as np
        from app.pipeline import clips
    except ImportError:
        return None
    try:
        matriz = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
        boxes = clips._face_boxes(matriz)
    except Exception:  # noqa: BLE001
        return None
    if not boxes:
        return None
    x, y, w, h = max(boxes, key=lambda b: b[2] * b[3])
    return int(x), int(y), int(w), int(h)


def _region_color(img: Any, topo: int, base: int) -> tuple[int, int, int]:
    """Cor media da faixa horizontal — o "fundo" que o texto vai enfrentar."""
    recorte = img.crop((0, max(0, topo), img.width, max(topo + 1, base)))
    pequeno = recorte.resize((1, 1))
    r, g, b = pequeno.getpixel((0, 0))[:3]
    return (r, g, b)


def _region_variance(img: Any, topo: int, base: int) -> float:
    """Quao irregular e a faixa. Fundo agitado engole texto mesmo com contraste."""
    from PIL import ImageStat

    recorte = img.crop((0, max(0, topo), img.width, max(topo + 1, base))).convert("L")
    return float(ImageStat.Stat(recorte.resize((32, 8))).stddev[0])


def _pick_band(img: Any, bloco: int, vertical: bool) -> int:
    """Escolhe a faixa onde o texto fica melhor: a mais uniforme e sem rosto.

    Sem isso o texto cai sempre no mesmo lugar e, quando calha de ser em cima do
    rosto ou de uma area cheia de detalhe, a capa fica ilegivel.
    """
    altura = img.height
    folga = int(bloco * 0.55)
    candidatos = [int(altura * 0.07), altura - bloco - int(altura * 0.08)]
    if vertical:  # no 9:16 sobra espaco no meio-baixo, entre o rosto e a borda
        candidatos.append(int(altura * 0.62))

    rostos = _face_bands(img)

    melhor, melhor_nota = candidatos[0], None
    for y in candidatos:
        topo, base = max(0, y - folga), min(altura, y + bloco + folga)
        nota = -_region_variance(img, topo, base)
        # Cobrir rosto e o pior resultado possivel numa capa.
        if any(not (base < ry0 or topo > ry1) for ry0, ry1 in rostos):
            nota -= 100
        if melhor_nota is None or nota > melhor_nota:
            melhor, melhor_nota = y, nota
    return melhor


def _face_bands(img: Any) -> list[tuple[int, int]]:
    """Faixas verticais ocupadas por rosto, para o texto nao passar por cima."""
    try:
        import cv2
        import numpy as np
        from app.pipeline import clips
    except ImportError:
        return []
    try:
        matriz = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
        return [(int(y), int(y + h)) for (_x, y, _w, h) in clips._face_boxes(matriz)]
    except Exception:  # noqa: BLE001 — sem deteccao a capa ainda sai
        return []


def _veil_strength(fundo: tuple[int, int, int]) -> int:
    """Quanto escurecer a faixa (0-255) para o texto ter contraste garantido.

    Cena clara precisa de veu forte; cena ja escura quase nao precisa — e poupar
    o veu preserva a imagem, que e o que atrai o olho na miniatura.
    """
    luz = relative_luminance(fundo)
    if luz < 0.06:
        return 0        # ja e quase preto: veu so apagaria a imagem
    if luz < 0.18:
        return 90
    if luz < 0.40:
        return 140
    return 185          # cena clara/estourada: sem veu forte nao ha texto legivel


def make(video_path: Path, clip: dict[str, Any], output_path: Path,
         vertical: bool = False, art_dir: Path | None = None) -> Path | None:
    """Capa completa de um corte. Nunca derruba o pipeline: falha vira None.

    `art_dir` guarda a imagem gerada por IA fora do diretorio temporario, para o
    cache sobreviver ao reprocessamento do episodio (a imagem e o item caro).
    """
    from app.pipeline import clips, imagegen

    start, end = float(clip["start"]), float(clip["end"])
    duration = end - start
    texto = (clip.get("thumb_text") or "").strip()

    # Instante que a IA apontou como clima do trecho. Sem ele (cortes antigos,
    # anteriores ao campo), a busca volta a varrer o trecho inteiro.
    try:
        alvo = float(clip["thumb_time"]) if clip.get("thumb_time") is not None else None
    except (TypeError, ValueError):
        alvo = None

    with tempfile.TemporaryDirectory(prefix="dubflow_thumb_") as td:
        tmp = Path(td)
        frame, instante = pick_frame(video_path, start, duration, tmp, alvo)

        if frame is None:  # sem opencv/ffmpeg: cai no frame do meio, sem escolha
            frame = tmp / "meio.jpg"
            cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{instante:.2f}",
                   "-i", str(video_path), "-frames:v", "1", "-q:v", "2", str(frame)]
            try:
                if subprocess.run(cmd, capture_output=True).returncode != 0:
                    return None
            except OSError:
                return None

        if vertical:
            # A capa vertical usa o mesmo enquadramento do corte: recorta a janela
            # 9:16 vigente naquele instante em vez do centro do quadro.
            mode, track = clips._resolve_reframe(video_path, start, duration)
            focus = clips._focus_at(track, instante - start)
            enquadrado = tmp / "vert.jpg"
            cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(frame),
                   "-filter_complex", clips._vertical_chain(mode, focus),
                   "-map", "[framed]", "-q:v", "2", str(enquadrado)]
            try:
                if subprocess.run(cmd, capture_output=True).returncode == 0:
                    frame = enquadrado
            except OSError:
                pass

        size = (1080, 1920) if vertical else (1280, 720)
        try:
            # A capa e sempre a imagem gerada pela IA a partir do que se diz no
            # trecho — nas duas orientacoes. O 9:16 pede arte em retrato: gerar
            # paisagem e recortar jogaria fora metade do enquadramento composto.
            arte = imagegen.generate(
                clip.get("thumb_image_prompt") or "",
                art_dir or tmp,
                size=(settings.thumb_image_size_vertical if vertical
                      else settings.thumb_image_size),
            )
            return compose_composite(frame, arte, texto, output_path,
                                     badge=(clip.get("thumb_badge") or "").strip(),
                                     size=size,
                                     presenter=settings.thumb_presenter)
        except Exception as exc:  # noqa: BLE001 — capa e um extra, nunca reprova o corte
            log.warning("composicao da capa falhou (%s)", exc)
            return None
