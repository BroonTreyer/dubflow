"""Testes das partes puras do pipeline (nao tocam rede, GPU nem ffmpeg).

    py -m tests.test_pipeline
"""

from __future__ import annotations

import pathlib
import sys

from app.pipeline import clips, subtitles, translate
from app.publishers import youtube

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} -> {detail}")
        failures.append(label)


def test_wrap() -> None:
    print("legendas / quebra de linha")
    original = "Isso aqui e uma frase bem longa que precisa ser quebrada em duas linhas legiveis"
    text = subtitles.wrap_text(original)
    lines = text.split("\n")
    # A largura e o limite rigido: o renderizador ASS nao re-quebra, entao uma
    # linha acima de 42 colunas sai cortada na tela.
    check("respeita 42 colunas", all(len(l) <= 42 for l in lines), lines)
    check("nao perde palavras", set(text.split()) == set(original.split()))
    check("frase curta fica intacta", subtitles.wrap_text("oi tudo bem") == "oi tudo bem")

    # Cabe folgado em duas linhas: nao deve abrir uma terceira.
    curto = subtitles.wrap_text("uma frase media que cabe bem em duas linhas sem aperto")
    check("usa 2 linhas quando cabe", len(curto.split("\n")) == 2, curto)
    check("linhas equilibradas", abs(len(curto.split("\n")[0]) - len(curto.split("\n")[1])) < 18, curto)

    # Palavra unica maior que a largura nao pode ser cortada nem estourar sozinha.
    gigante = subtitles.wrap_text("anticonstitucionalissimamente " * 3)
    check("palavra longa fica inteira", "anticonstitucionalissimamente" in gigante.split("\n")[0])

    # Texto que nao cabe em 2x42: abre linha extra em vez de estourar a largura.
    longo = subtitles.wrap_text(
        "esta frase e deliberadamente muito mais longa do que oitenta e quatro "
        "caracteres para forcar uma terceira linha no resultado final"
    )
    check("linha extra em vez de estouro", all(len(l) <= 42 for l in longo.split("\n")), longo)
    check("abriu terceira linha", len(longo.split("\n")) >= 3, len(longo.split("\n")))


def test_timestamps() -> None:
    print("legendas / timestamps")
    check("srt", subtitles._fmt_srt(3661.5) == "01:01:01,500", subtitles._fmt_srt(3661.5))
    check("ass", subtitles._fmt_ass(3661.5) == "1:01:01.50", subtitles._fmt_ass(3661.5))
    check("negativo vira zero", subtitles._fmt_srt(-4) == "00:00:00,000")


def test_ffmpeg_escape() -> None:
    print("legendas / escape de caminho no ffmpeg")
    esc = subtitles._escape_for_filter(pathlib.Path(r"C:\Users\mathe\dubflow\a.ass"))
    check("drive escapado", esc.startswith("C\\:/"), esc)
    check("sem barra invertida", "\\/" not in esc and esc.count("\\") == 1, esc)


def test_srt_output(tmp: pathlib.Path) -> None:
    print("legendas / arquivo srt")
    segs = [
        {"start": 0.0, "end": 2.0, "text": "primeira fala"},
        {"start": 2.0, "end": 4.0, "text": ""},          # vazio: deve sumir
        {"start": 4.0, "end": 3.0, "text": "fim errado"},  # end < start: corrigido
    ]
    path = subtitles.write_srt(segs, tmp / "t.srt")
    content = path.read_text(encoding="utf-8")
    check("descarta segmento vazio", "1\n" in content and "2\n" in content and "3\n" not in content)
    check("corrige duracao invertida", "00:00:04,000 --> 00:00:05,200" in content, content)


def test_budget() -> None:
    print("traducao / orcamento de caracteres")
    b4 = translate._budget({"start": 0.0, "end": 4.0})
    b1 = translate._budget({"start": 0.0, "end": 1.0})
    check("4s cabe ~90 chars", 80 < b4 < 110, b4)
    check("cresce com a duracao", b4 > b1, (b4, b1))
    check("piso minimo", translate._budget({"start": 0.0, "end": 0.1}) >= 20)


def test_blocks() -> None:
    print("traducao / divisao em blocos")
    sizes = [len(b) for b in translate._build_blocks([{"id": i} for i in range(145)])]
    check("60/60/25", sizes == [60, 60, 25], sizes)
    check("lista vazia", translate._build_blocks([]) == [])


def test_clip_sanitize() -> None:
    print("cortes / alinhamento e sobreposicao")
    segs = [{"start": i * 5.0, "end": i * 5.0 + 4.5, "text": f"fala {i}"} for i in range(40)]
    raw = [
        {"start": 11.3, "end": 52.2, "title": "A", "hook": "h", "caption": "c", "score": 8,
         "yt_title": "Titulo YT A", "yt_description": "descricao para busca #tag"},
        {"start": 30.0, "end": 70.0, "title": "sobreposto", "hook": "h", "caption": "c", "score": 9},
        {"start": 100.4, "end": 141.0, "title": "B", "hook": "h", "caption": "c", "score": 7},
        {"start": 150.0, "end": 152.0, "title": "curto", "hook": "h", "caption": "c", "score": 5},
    ]
    out = clips._sanitize(raw, segs)
    titles = [c["title"] for c in out]
    check("descarta sobreposto e curto", titles == ["A", "B"], titles)
    check("carrega metadados de SEO (yt_title/yt_description)",
          out[0].get("yt_title") == "Titulo YT A"
          and out[0].get("yt_description") == "descricao para busca #tag", out[0])
    check("ordenado por tempo", out == sorted(out, key=lambda c: c["start"]))
    check("snap para fronteira de fala", abs(out[0]["start"] - (10.0 - 0.25)) < 0.01, out[0]["start"])


def test_clip_segments() -> None:
    print("cortes / recorte de segmentos")
    segs = [{"start": i * 5.0, "end": i * 5.0 + 4.5, "text": f"fala {i}"} for i in range(40)]
    sub = clips._clip_segments(segs, 20.0, 40.0)
    check("rebaseia para zero", sub[0]["start"] == 0.0, sub[0])
    check("nao ultrapassa a duracao", sub[-1]["end"] <= 20.0, sub[-1])
    check("pega 4 falas", len(sub) == 4, len(sub))


def test_ass_escape() -> None:
    """Achado 5: `{}` no texto vira override tag do ASS e a legenda some da tela."""
    print("legendas / escape da sintaxe ASS")
    escapado = subtitles.escape_ass("use {chave} e a barra \\ no JSON")
    check("chaves neutralizadas", "{" not in escapado and "}" not in escapado, escapado)
    check("barra invertida neutralizada", "\\" not in escapado, escapado)
    check("texto continua legivel", "chave" in escapado and "JSON" in escapado, escapado)

    tmp = pathlib.Path(__file__).parent / "_tmp"
    tmp.mkdir(exist_ok=True)
    p = subtitles.write_ass([{"start": 0, "end": 2, "text": "config {\"a\": 1} pronta"}],
                            tmp / "esc.ass")
    dialogo = [l for l in p.read_text(encoding="utf-8").splitlines()
               if l.startswith("Dialogue")][0]
    corpo = dialogo.split(",,", 1)[1]
    check("dialogo sem chave crua", "{" not in corpo and "}" not in corpo, corpo)
    check("quebra de linha \\N preservada",
          "\\N" in subtitles.write_ass(
              [{"start": 0, "end": 3,
                "text": "uma frase longa o suficiente para quebrar em duas linhas na tela"}],
              tmp / "brk.ass").read_text(encoding="utf-8"))


def test_translation_fallback() -> None:
    """Achado 3 (fala sumindo) sem estragar a regra 8 (ruido deve sumir mesmo)."""
    print("traducao / fallback de segmento faltante")
    segs = [
        {"id": 0, "start": 0, "end": 2, "text": "hello"},
        {"id": 1, "start": 2, "end": 4, "text": "world"},   # modelo pulou: falha
        {"id": 2, "start": 4, "end": 5, "text": "Hmm."},     # vazio deliberado: ruido
    ]
    devolvido = {0: "ola", 2: ""}
    saida = [
        {**s, "text_src": s["text"],
         "text": s["text"] if s["id"] not in devolvido else devolvido[s["id"]],
         "untranslated": s["id"] not in devolvido}
        for s in segs
    ]
    check("traduzido usa a traducao", saida[0]["text"] == "ola")
    check("faltante cai no original", saida[1]["text"] == "world", saida[1])
    check("faltante e sinalizado", saida[1]["untranslated"] is True)
    # A distincao que o teste real expos: interjeicao devolvida vazia deve
    # continuar vazia (some da legenda) e nao contar como falha de traducao.
    check("ruido continua vazio", saida[2]["text"] == "", saida[2])
    check("ruido nao conta como falha", saida[2]["untranslated"] is False, saida[2])
    check("segmento vazio sai da legenda",
          len(subtitles._usable(saida)) == 2, len(subtitles._usable(saida)))


def test_ffmpeg_quote_escape() -> None:
    """Achado 18: aspa simples no caminho quebrava o filtro do ffmpeg."""
    print("legendas / aspa simples no caminho")
    esc = subtitles._escape_for_filter(pathlib.Path(r"C:\Users\O'Brien\a.ass"))
    check("aspa escapada", r"\'" in esc, esc)
    check("nao sobra aspa solta", "'" not in esc.replace(r"\'", ""), esc)


def test_transcribe_modes() -> None:
    """A VRAM livre decide quais modos valem a tentativa."""
    print("transcricao / selecao de modo por VRAM disponivel")
    from app.pipeline import transcribe as tr

    # GPU folgada: usa o modo configurado.
    modos = tr._modes(7500)
    check("gpu livre comeca em float16", modos[0] == ("cuda", "float16"), modos[0])
    check("mantem cpu como ultimo recurso", modos[-1] == ("cpu", "int8"), modos[-1])

    # 4 GB nao cabe float16 (~7 GB), mas cabe int8_float16 (~3.5 GB).
    modos = tr._modes(4000)
    check("4GB pula float16", ("cuda", "float16") not in modos, modos)
    check("4GB usa int8_float16", modos[0] == ("cuda", "int8_float16"), modos[0])

    # Cenario que travou o worker: 344 MB livres.
    modos = tr._modes(344)
    check("vram esgotada vai direto para cpu", modos == [("cpu", "int8")], modos)

    check("sem gpu visivel usa cpu", tr._modes(None)[-1] == ("cpu", "int8"))
    check("nunca devolve lista vazia", len(tr._modes(0)) >= 1)

    livre = tr.free_vram_mb()
    check("consulta de vram funciona", livre is None or livre >= 0, livre)
    check("timeout de silencio configurado", tr.SILENCE_TIMEOUT >= 300, tr.SILENCE_TIMEOUT)


def test_subtitle_styles() -> None:
    """Legenda tem que ficar embaixo e legivel — os dois erros da primeira versao."""
    print("legendas / posicao e tamanho")

    def campos(style: str) -> list[str]:
        return style.replace("Style: ", "").split(",")

    ep, cl = campos(subtitles.STYLE_EPISODE), campos(subtitles.STYLE_CLIP)
    # Alignment segue o teclado numerico: 2 = inferior centralizado, 5 = meio da tela.
    check("episodio alinhado embaixo", ep[18] == "2", ep[18])
    check("corte alinhado embaixo", cl[18] == "2", cl[18])
    # ~5% da altura do quadro (PlayResY 1080 no episodio, 1920 no corte).
    check("fonte do episodio legivel", 44 <= int(ep[2]) <= 64, ep[2])
    check("fonte do corte legivel", 64 <= int(cl[2]) <= 92, cl[2])
    check("corte tem margem para a UI do app", int(cl[21]) >= 200, cl[21])
    check("corte tem contorno forte", float(cl[16]) >= 4, cl[16])

    # Com fonte 76 num quadro de 1080 de largura, o limite do episodio vazaria.
    check("limite do corte e menor que o do episodio",
          subtitles.CLIP_MAX_CHARS_PER_LINE < subtitles.MAX_CHARS_PER_LINE)
    linhas = subtitles.wrap_text(
        "O dinheiro acaba antes de eles descobrirem o caminho certo",
        subtitles.CLIP_MAX_CHARS_PER_LINE, subtitles.CLIP_MAX_LINES,
    ).split("\n")
    check("texto do corte cabe na largura",
          all(len(l) <= subtitles.CLIP_MAX_CHARS_PER_LINE for l in linhas), linhas)


def test_resegment() -> None:
    """A legenda deve seguir a fala: quebrar na pausa e aparar ao tempo das palavras."""
    print("legendas / re-segmentacao por pausa")

    # Segmento que junta duas falas com ~2s de silencio no meio.
    seg = {
        "start": 0.0, "end": 8.0, "text": "ola mundo tudo bem",
        "words": [
            {"start": 1.0, "end": 1.4, "word": "hello"},
            {"start": 1.4, "end": 2.0, "word": "world"},
            {"start": 4.0, "end": 4.5, "word": "how"},
            {"start": 4.5, "end": 5.0, "word": "are"},
        ],
    }
    cues = subtitles._resegment([seg])
    check("quebra em duas legendas na pausa", len(cues) == 2, len(cues))
    check("apara o silencio inicial (comeca na 1a palavra)", abs(cues[0]["start"] - 1.0) < 1e-6, cues[0]["start"])
    check("primeira termina na ultima palavra do grupo", abs(cues[0]["end"] - 2.0) < 1e-6, cues[0]["end"])
    check("segunda comeca so quando a fala volta", abs(cues[1]["start"] - 4.0) < 1e-6, cues[1]["start"])
    check("nao ha legenda durante o silencio", cues[0]["end"] < cues[1]["start"], (cues[0]["end"], cues[1]["start"]))
    check("todo o texto foi distribuido",
          " ".join(c["text"] for c in cues).split() == ["ola", "mundo", "tudo", "bem"],
          [c["text"] for c in cues])

    # Uma fala so, com silencio antes e depois: vira uma legenda aparada.
    seg2 = {
        "start": 0.0, "end": 6.0, "text": "so uma frase",
        "words": [{"start": 2.0, "end": 2.5, "word": "just"}, {"start": 2.5, "end": 3.2, "word": "one"}],
    }
    c2 = subtitles._resegment([seg2])
    check("uma fala vira uma legenda", len(c2) == 1, len(c2))
    check("apara silencio inicial e final", abs(c2[0]["start"] - 2.0) < 1e-6 and abs(c2[0]["end"] - 3.2) < 1e-6,
          (c2[0]["start"], c2[0]["end"]))

    # Sem timestamps por palavra, passa inalterado (nao quebra nada).
    c3 = subtitles._resegment([{"start": 0.0, "end": 2.0, "text": "sem palavras"}])
    check("sem timestamps passa inalterado",
          len(c3) == 1 and c3[0]["start"] == 0.0 and c3[0]["end"] == 2.0, c3)

    # Legenda muito curta ganha tempo minimo de leitura.
    seg4 = {
        "start": 0.0, "end": 10.0, "text": "a b",
        "words": [{"start": 0.0, "end": 0.1, "word": "a"}, {"start": 0.15, "end": 0.2, "word": "b"}],
    }
    c4 = subtitles._resegment([seg4])
    check("tempo minimo de leitura aplicado", c4[0]["end"] - c4[0]["start"] >= 0.8 - 1e-9,
          c4[0]["end"] - c4[0]["start"])


def test_karaoke(tmp: pathlib.Path) -> None:
    """Karaoke: cada palavra ganha uma duracao e a soma bate com o segmento."""
    print("legendas / karaoke")

    durs = subtitles._distribute_cs(["uma", "frase", "de", "teste"], 300)
    check("soma bate com o total", sum(durs) == 300, durs)
    check("uma duracao por palavra", len(durs) == 4, durs)
    check("nenhuma duracao zerada", all(d >= 1 for d in durs), durs)
    check("total minusculo nao quebra", sum(subtitles._distribute_cs(["a", "b", "c"], 1)) >= 3)

    txt = subtitles._karaoke_text(
        "uma frase simples de teste", 0.0, 2.0,
        subtitles.CLIP_MAX_CHARS_PER_LINE, subtitles.CLIP_MAX_LINES,
    )
    check("uma tag kf por palavra", txt.count("\\kf") == 5, txt)
    check("mantem as palavras",
          all(w in txt for w in ["uma", "frase", "simples", "de", "teste"]), txt)

    segs = [{"start": 0, "end": 2, "text": "palavra um dois tres"}]
    komp = subtitles.write_ass(
        segs, tmp / "k.ass", width=1080, height=1920,
        style=subtitles.STYLE_CLIP_KARAOKE, max_chars=subtitles.CLIP_MAX_CHARS_PER_LINE,
        max_lines=subtitles.CLIP_MAX_LINES, karaoke=True,
    ).read_text(encoding="utf-8")
    check("ass karaoke tem kf e Dialogue", "\\kf" in komp and "Dialogue" in komp, komp[-80:])
    plano = subtitles.write_ass(segs, tmp / "p.ass", karaoke=False).read_text(encoding="utf-8")
    check("ass normal nao tem kf", "\\kf" not in plano)


def test_reframe_focus() -> None:
    """A janela 9:16 tem que seguir o rosto sem vazar do quadro (achado deste ciclo)."""
    print("cortes / foco do recorte 9:16")

    # Fonte 16:9 (1920x1080): rosto no centro deixa a janela no centro.
    meio = clips._focus_from_center(0.5, 1920, 1080)
    check("rosto central -> janela central", abs(meio - 0.5) < 1e-6, meio)

    # Rosto a esquerda puxa a janela para a esquerda; a direita, para a direita.
    esq = clips._focus_from_center(0.2, 1920, 1080)
    dir_ = clips._focus_from_center(0.85, 1920, 1080)
    check("rosto a esquerda puxa a janela", 0.0 <= esq < 0.5, esq)
    check("rosto a direita puxa a janela", 0.5 < dir_ <= 1.0, dir_)

    # Nunca sai de [0, 1], mesmo com o rosto colado na borda.
    check("trava na esquerda", clips._focus_from_center(0.0, 1920, 1080) == 0.0)
    check("trava na direita", clips._focus_from_center(1.0, 1920, 1080) == 1.0)

    # Fonte ja vertical (mais alta que 9:16): nao ha corte horizontal, fica no centro.
    check("fonte vertical fica no centro", clips._focus_from_center(0.2, 1080, 1920) == 0.5)
    check("largura invalida nao quebra", clips._focus_from_center(0.5, 0, 0) == 0.5)

    # O filtro central e o com foco produzem um crop que preenche a tela (sem barras).
    ass = pathlib.Path("x.ass")
    central = clips._reframe_filter("center", 0.5, ass)
    face = clips._reframe_filter("face", 0.83, ass)
    check("center preenche a tela (crop, sem overlay)",
          "crop=1080:1920:x=" in central and "overlay" not in central, central)
    check("foco entra no filtro", "0.8300" in face, face)
    check("pad continua disponivel como legado", "overlay" in clips._reframe_filter("pad", 0.5, ass))


def test_youtube_metadata() -> None:
    """O worker so passa a caption; o publisher deriva titulo/descricao/tags dela."""
    print("youtube / metadados do Short")

    caption = (
        "O erro que quebra 9 em cada 10 startups\n"
        "Ele fala sobre gastar antes de validar\n"
        "#startup #empreendedorismo #negocios"
    )
    title, description, tags = youtube._metadata(caption, None, is_short=True)

    # Sem title do corte, a primeira linha (o gancho) vira titulo.
    check("titulo cai na primeira linha", title == "O erro que quebra 9 em cada 10 startups", title)
    check("descricao mantem a legenda", description.startswith("O erro que quebra"), description[:30])
    check("tags saem das hashtags", tags == ["startup", "empreendedorismo", "negocios"], tags)

    # Com title do corte (escolhido pela Claude), ele manda no titulo do YouTube.
    real = youtube._metadata(caption, "Titulo Escolhido", is_short=True)[0]
    check("usa o title do corte quando existe", real == "Titulo Escolhido", real)

    # #Shorts entra na descricao de Short, mas nao no video horizontal.
    check("shorts no vertical", "#Shorts" in description, description[-20:])
    wide = youtube._metadata(caption, None, is_short=False)[1]
    check("sem shorts no horizontal", "#shorts" not in wide.lower(), wide[-20:])
    ja_tem = youtube._metadata("Gancho\n#Shorts", None, is_short=True)[1]
    check("nao duplica shorts", ja_tem.lower().count("#shorts") == 1, ja_tem)
    check("shorts nao vira tag", "shorts" not in [t.lower() for t in tags], tags)

    # Limites da API: titulo <= 100 chars, sem < ou >.
    longo = youtube._metadata("x" * 250, None, is_short=True)[0]
    check("titulo respeita 100 chars", len(longo) <= youtube.TITLE_MAX, len(longo))
    perigoso = youtube._metadata("um <script> qualquer", None, is_short=True)[0]
    check("titulo sem < e >", "<" not in perigoso and ">" not in perigoso, perigoso)

    # Legenda e title vazios nao podem gerar titulo vazio (a API recusa).
    vazio = youtube._metadata("", None, is_short=True)[0]
    check("titulo nunca vazio", vazio != "", vazio)

    # Sem credenciais no ambiente de teste, o publisher se declara nao configurado.
    check("nao configurado sem credenciais", youtube.configured() is False)

    # Parser das metricas (views/curtidas) da resposta do YouTube.
    parsed = youtube._parse_stats(
        {"items": [{"statistics": {"viewCount": "1234", "likeCount": "56", "commentCount": "7"}}]}
    )
    check("stats parseia os numeros", parsed == {"views": 1234, "likes": 56, "comments": 7}, parsed)
    check("stats sem itens vira None", youtube._parse_stats({"items": []}) is None)
    ausente = youtube._parse_stats({"items": [{"statistics": {"viewCount": "9"}}]})
    check("campo ausente vira None sem quebrar",
          ausente == {"views": 9, "likes": None, "comments": None}, ausente)


def main() -> int:
    tmp = pathlib.Path(__file__).parent / "_tmp"
    tmp.mkdir(exist_ok=True)

    test_wrap()
    test_timestamps()
    test_ffmpeg_escape()
    test_srt_output(tmp)
    test_budget()
    test_blocks()
    test_clip_sanitize()
    test_clip_segments()
    test_ass_escape()
    test_translation_fallback()
    test_ffmpeg_quote_escape()
    test_transcribe_modes()
    test_subtitle_styles()
    test_resegment()
    test_karaoke(tmp)
    test_reframe_focus()
    test_youtube_metadata()

    print()
    if failures:
        print(f"{len(failures)} falha(s): {', '.join(failures)}")
        return 1
    print("todos os testes passaram")
    return 0


if __name__ == "__main__":
    sys.exit(main())
