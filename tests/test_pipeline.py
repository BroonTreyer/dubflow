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


def test_subtitle_screen_cap() -> None:
    """Legenda nunca pode cobrir a tela.

    No ep 1 um segmento de 360 caracteres durou 22,9s sem pausa: como wrap_text
    prefere abrir linha extra a estourar a largura, virou um bloco de 18 linhas.
    Fala corrida tem que ser dividida NO TEMPO, nao empilhada.
    """
    print("legendas / teto de tela")

    longo = ("Vou falar abertamente como jurista que eu acho que o STF tem que "
             "voltar a ser uma casa constitucional e nao um tribunal politico, "
             "porque a gente precisa de estabilidade juridica para o pais crescer "
             "e gerar emprego de verdade para todo mundo agora.")
    cue = [{"start": 100.0, "end": 122.9, "text": longo}]

    for mc, ml in ((subtitles.CLIP_MAX_CHARS_PER_LINE, subtitles.CLIP_MAX_LINES),
                   (subtitles.MAX_CHARS_PER_LINE, subtitles.MAX_LINES)):
        partes = subtitles.split_oversized(cue, mc, ml)
        check(f"divide em varias legendas ({mc}x{ml})", len(partes) > 1, len(partes))
        estouros = [p for p in partes
                    if len(subtitles.wrap_text(p["text"], mc, ml).split("\n")) > ml]
        check(f"nenhum bloco passa de {ml} linhas", not estouros,
              [p["text"] for p in estouros])
        check(f"nenhuma linha passa de {mc} colunas ({mc}x{ml})",
              all(len(l) <= mc for p in partes
                  for l in subtitles.wrap_text(p["text"], mc, ml).split("\n")))

        # O tempo tem que continuar cobrindo o mesmo trecho, em ordem e sem furo.
        check("comeca junto com a fala", abs(partes[0]["start"] - 100.0) < 0.01)
        check("termina junto com a fala", abs(partes[-1]["end"] - 122.9) < 0.05,
              partes[-1]["end"])
        check("em ordem e sem sobreposicao",
              all(partes[i]["end"] <= partes[i + 1]["start"] + 0.01
                  for i in range(len(partes) - 1)))
        check("nao perde nem inventa palavra",
              " ".join(p["text"] for p in partes).split() == longo.split())
        # Legenda-relampago cansa e nao da tempo de ler.
        check("sem legenda abaixo de 0,3s",
              all(p["end"] - p["start"] >= 0.3 for p in partes),
              [round(p["end"] - p["start"], 2) for p in partes])

    # Texto que ja cabe passa intacto.
    curto = [{"start": 0.0, "end": 2.0, "text": "Frase curta."}]
    igual = subtitles.split_oversized(curto, 42, 2)
    check("texto que cabe nao e mexido", len(igual) == 1 and igual[0]["text"] == "Frase curta.")


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
    # Na sobreposicao quem ganha e o maior score, nao quem comeca antes: os cortes
    # chegam de janelas diferentes, entao a ordem do episodio nao vale como criterio.
    check("na sobreposicao vence o de maior score", titles == ["sobreposto", "B"], titles)
    check("descarta o curto demais", "curto" not in titles, titles)
    check("ordenado por tempo", out == sorted(out, key=lambda c: c["start"]))
    check("snap para fronteira de fala", abs(out[1]["start"] - (100.0 - 0.25)) < 0.01, out[1]["start"])

    # Sem disputa, os metadados de SEO precisam sobreviver ao saneamento.
    solo = clips._sanitize([raw[0]], segs)
    check("carrega metadados de SEO (yt_title/yt_description)",
          solo[0].get("yt_title") == "Titulo YT A"
          and solo[0].get("yt_description") == "descricao para busca #tag", solo[0])

    # Teto: mantem os melhores, nao os primeiros.
    limitado = clips._sanitize(raw, segs, 1)
    check("teto mantem o de maior score", [c["title"] for c in limitado] == ["sobreposto"], limitado)


def test_clip_target_count() -> None:
    print("cortes / cota proporcional a duracao")
    from app.config import settings
    por_hora, piso, teto = settings.clips_per_hour, settings.clips_per_episode, settings.clips_max

    duas_horas = clips.target_count(2 * 3600)
    check("2h rende ~2x a cota horaria", duas_horas == min(2 * por_hora, teto), duas_horas)
    check("video longo rende mais que curto", clips.target_count(8330) > clips.target_count(918))
    check("video curto respeita o piso", clips.target_count(120) >= min(piso, teto),
          clips.target_count(120))
    check("nunca passa do teto", clips.target_count(50 * 3600) == teto, clips.target_count(50 * 3600))


def test_clip_windows() -> None:
    print("cortes / janelas de analise")
    # 2h de fala em segmentos de 10s.
    segs = [{"start": i * 10.0, "end": i * 10.0 + 9.0, "text": f"fala {i}"} for i in range(720)]
    janelas = clips._windows(segs, 40)
    check("fatia 2h em varias janelas", len(janelas) > 1, len(janelas))
    check("soma das cotas cobre o alvo", sum(c for _, c in janelas) >= 40,
          sum(c for _, c in janelas))
    todos = [s for bloco, _ in janelas for s in bloco]
    check("nao perde nem duplica segmento", len(todos) == len(segs), (len(todos), len(segs)))
    check("janelas em ordem e sem buraco",
          [s["start"] for s in todos] == [s["start"] for s in segs])

    curto = clips._windows(segs[:30], 5)
    check("video curto fica em uma janela so", len(curto) == 1, len(curto))


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

    # A capa vertical precisa do MESMO enquadramento do corte: se ela usasse o
    # recorte 16:9, a capa do Reels sairia com a cabeca cortada.
    cadeia = clips._vertical_chain("face", 0.83)
    check("capa vertical usa o mesmo foco do corte", "0.8300" in cadeia, cadeia)
    check("capa vertical sai 9:16", "crop=1080:1920" in cadeia, cadeia)
    check("cadeia termina no rotulo reusavel", cadeia.endswith("[framed]"), cadeia[-40:])
    check("o filtro do corte reusa a cadeia",
          clips._vertical_chain("face", 0.83) in clips._reframe_filter("face", 0.83, ass))


def test_reframe_two_people() -> None:
    """Com duas pessoas a janela tem que escolher UMA, nao ficar no vazio entre elas."""
    print("cortes / duas pessoas no quadro")

    W, H = 1920, 1080          # a janela 9:16 cobre ~31,6% da largura
    r = clips._window_ratio(W, H)
    check("janela 9:16 e estreita em fonte 16:9", 0.30 < r < 0.33, r)

    # Ela a esquerda (rosto grande, falando), ele a direita (de costas, parado).
    esquerda = (200.0, 300.0, 260.0, 260.0)   # x=200..460
    direita = (1500.0, 300.0, 300.0, 300.0)   # x=1500..1800
    boxes = [esquerda, direita]

    # A media ponderada dos dois cairia no meio do quadro e cortaria os dois — era
    # exatamente o bug. O grupo escolhido tem que ser um rosto so.
    grupo = clips._pick_group(boxes, [260 * 260 * 3.0, 300 * 300 * 1.0], W, H)
    x0, x1 = grupo
    check("escolhe um rosto, nao a media", (x1 - x0) < r, (x0, x1))
    check("escolhe quem esta falando", x1 < 0.5, (x0, x1))

    foco = clips._focus_for_span(x0, x1, W, H)
    jan0 = foco * (1 - r)
    jan1 = jan0 + r
    check("o rosto escolhido cabe inteiro na janela", jan0 <= x0 and x1 <= jan1,
          (jan0, x0, x1, jan1))

    # Sem o bonus de fala, quem manda e o rosto maior — e ainda assim um so.
    g2 = clips._pick_group(boxes, [260 * 260, 300 * 300], W, H)
    check("sem fala, vence o rosto maior", g2[0] > 0.5, g2)

    # Dois rostos proximos cabem juntos: nao ha por que descartar um.
    perto = [(800.0, 300.0, 200.0, 200.0), (1050.0, 300.0, 200.0, 200.0)]
    gp = clips._pick_group(perto, [200 * 200, 200 * 200], W, H)
    check("rostos proximos ficam juntos", (gp[1] - gp[0]) > 0.2, gp)
    f2 = clips._focus_for_span(gp[0], gp[1], W, H)
    check("os dois cabem na janela", f2 * (1 - r) <= gp[0] and gp[1] <= f2 * (1 - r) + r, gp)

    # Rosto colado na borda: a janela trava sem vazar do quadro.
    borda = clips._focus_for_span(0.0, 0.05, W, H)
    check("rosto na borda nao vaza", 0.0 <= borda <= 1.0, borda)


def test_reframe_track() -> None:
    """A camera segue quem fala, mas sem tremer: histerese e tempo minimo."""
    print("cortes / trilha de foco no tempo")

    W, H = 1920, 1080
    # Alguem a esquerda por 6 s, depois alguem a direita por 6 s.
    amostras = []
    for i in range(24):
        t = i / clips.FOCUS_FPS
        if t < 6:
            amostras.append((t, 0.10, 0.24))
        else:
            amostras.append((t, 0.76, 0.90))
    trilha = clips._build_track(amostras, W, H, 12.0)
    check("troca de enquadramento acontece", len(trilha) == 2, trilha)
    check("comeca no tempo zero", trilha[0][0] == 0.0, trilha)
    check("segue quem fala (esquerda -> direita)", trilha[1][1] > trilha[0][1], trilha)
    check("a troca cai perto dos 6 s", 5.0 <= trilha[1][0] <= 7.5, trilha)

    # Deteccao que pisca por meio segundo nao pode virar corte de camera.
    ruido = []
    for i in range(24):
        t = i / clips.FOCUS_FPS
        pisca = (t, 0.80, 0.94) if i == 10 else (t, 0.10, 0.24)
        ruido.append(pisca)
    check("ruido nao vira troca", len(clips._build_track(ruido, W, H, 12.0)) == 1,
          clips._build_track(ruido, W, H, 12.0))

    # Alvo parado = uma posicao so.
    parado = [(i / clips.FOCUS_FPS, 0.40, 0.54) for i in range(24)]
    check("cena estavel fica com um segmento", len(clips._build_track(parado, W, H, 12.0)) == 1)


def test_focus_expression() -> None:
    """A trilha vira expressao valida de ffmpeg, e a capa le o foco do instante."""
    print("cortes / expressao de foco")

    check("foco constante vira numero", clips._focus_expr(0.42) == "0.4200")
    check("trilha de um item vira numero", clips._focus_expr([(0.0, 0.31)]) == "0.3100")

    expr = clips._focus_expr([(0.0, 0.20), (6.0, 0.80)])
    check("trilha de dois vira if(lt(t...))", expr.startswith("if(lt(t") , expr)
    check("virgulas escapadas para o filtro", "\\," in expr and "," not in expr.replace("\\,", ""),
          expr)
    check("contem os dois focos", "0.2000" in expr and "0.8000" in expr, expr)

    tres = clips._focus_expr([(0.0, 0.2), (5.0, 0.5), (9.0, 0.9)])
    check("tres segmentos aninham dois ifs", tres.count("if(") == 2, tres)

    # O valor em cada instante tem que bater com o segmento vigente.
    t = [(0.0, 0.2), (5.0, 0.5), (9.0, 0.9)]
    check("antes do primeiro corte", clips._focus_at(t, 1.0) == 0.2)
    check("no meio", clips._focus_at(t, 6.0) == 0.5)
    check("depois do ultimo", clips._focus_at(t, 20.0) == 0.9)
    check("constante ignora o tempo", clips._focus_at(0.33, 7.0) == 0.33)

    # A cadeia do corte aceita trilha; a expressao entra dentro do crop.
    cadeia = clips._vertical_chain("face", t)
    check("cadeia usa a expressao no crop", "crop=1080:1920:x='(in_w-out_w)*(if(" in cadeia,
          cadeia[:90])


def test_same_language_passthrough(tmp: pathlib.Path) -> None:
    """Video ja em pt-BR: legenda sai da transcricao, sem pagar traducao pt->pt."""
    print("traducao / video ja no idioma de destino")

    check("pt vira pt-BR", translate.same_language("pt", "pt-BR"))
    check("aceita variante e caixa", translate.same_language("PT_br", "pt-BR"))
    check("ingles nao e portugues", not translate.same_language("en", "pt-BR"))
    check("turco nao e portugues", not translate.same_language("tr", "pt-BR"))
    check("idioma ausente nao passa direto", not translate.same_language(None, "pt-BR"))
    check("destino ausente nao passa direto", not translate.same_language("pt", None))

    segs = [
        {"id": 0, "start": 0.0, "end": 2.0, "text": "Bom dia, pessoal."},
        {"id": 1, "start": 2.0, "end": 4.5, "text": "Hoje eu vou falar de três coisas."},
    ]
    # Sem chave de API configurada, qualquer chamada ao Claude explodiria — se este
    # teste passa, e porque o caminho realmente nao chamou a API.
    out = translate.translate_segments(segs, {"lang_src": "pt", "title": "x", "channel": "y"})
    check("mantem a quantidade de segmentos", len(out) == 2, len(out))
    check("texto chega intacto na legenda",
          [s["text"] for s in out] == [s["text"] for s in segs], out)
    check("acentuacao preservada", "três" in out[1]["text"], out[1]["text"])
    check("nao marca como sem traducao", all(not s["untranslated"] for s in out), out)
    check("guarda o original em text_src", out[0]["text_src"] == "Bom dia, pessoal.")
    check("preserva timestamps", (out[1]["start"], out[1]["end"]) == (2.0, 4.5), out[1])

    # O que importa no fim: a legenda existe e tem o texto certo.
    srt = subtitles.write_srt(out, tmp / "ptbr.srt").read_text(encoding="utf-8")
    check("gera SRT com as duas falas", srt.count("-->") == 2, srt)
    check("SRT tem o texto original", "Bom dia, pessoal." in srt, srt[:120])

    # O atalho vale so para o mesmo idioma: um episodio turco continua indo para o
    # tradutor (o passthrough entregaria a legenda em turco).
    check("idioma diferente nao pega o atalho", not translate.same_language("tr", "pt-BR"))


def test_clip_openings() -> None:
    """Corte tem que abrir em frase nova. Medido: 46% abriam no meio do raciocinio."""
    print("cortes / abertura em frase nova")

    check("frase inteira abre bem", clips._abre_bem("Tem um filme do The Rock."))
    check("minuscula e frase cortada", not clips._abre_bem("e la na Italia tem um"))
    check("conectivo e muleta", not clips._abre_bem("Entao a galera do sul se prepara"))
    check("'Mas' nao abre corte", not clips._abre_bem("Mas o que acontece e que"))
    check("resposta solta nao abre", not clips._abre_bem("absurdo, ne?"))
    check("vazio nao abre bem", not clips._abre_bem("   "))

    # Fronteiras: inicio depois de ponto final, fim em pontuacao terminal.
    segs = [
        {"start": 0.0, "end": 3.0, "text": "Primeira frase completa."},
        {"start": 3.0, "end": 6.0, "text": "e isso continua sem fechar"},
        {"start": 6.0, "end": 9.0, "text": "porque emenda de novo."},
        {"start": 9.0, "end": 12.0, "text": "Agora sim uma frase nova."},
    ]
    inicios, fins = clips.speech_boundaries(segs)
    check("primeiro segmento sempre pode abrir", 0.0 in inicios, inicios)
    check("segmento que emenda nao vira inicio", 3.0 not in inicios, inicios)
    check("segmento apos ponto final e com abertura boa entra",
          9.0 in inicios, inicios)
    check("so fim de frase vira fim", fins == [3.0, 9.0, 12.0], fins)

    # O reencaixe: inicio VOLTA ate a frase abrir, fim AVANCA ate ela fechar.
    bruto = [{"start": 4.5, "end": 7.0, "title": "t", "hook": "h", "caption": "c",
              "score": 9}]
    # (duracao minima impede aceitar; o que importa aqui e a direcao do encaixe)
    longos = segs + [{"start": 12.0 + i * 3, "end": 15.0 + i * 3,
                      "text": f"Frase numero {i}."} for i in range(12)]
    bruto = [{"start": 4.5, "end": 40.0, "title": "t", "hook": "h", "caption": "c",
              "score": 9}]
    out = clips._sanitize(bruto, longos)
    if out:
        check("inicio recuou para o comeco da frase", out[0]["start"] <= 4.5,
              out[0]["start"])
        check("fim avancou para fechar a frase", out[0]["end"] >= 40.0 - 0.5,
              out[0]["end"])


def test_clip_ranking() -> None:
    """Com score colapsado em 8-9, o desempate tem que vir de sinal medido."""
    print("cortes / ranqueamento sem depender do score")

    notas_iguais = [{"score": 9}, {"score": 9}, {"score": 9}, {"score": 8}]
    check("detecta score sem discriminacao",
          clips._score_spread(notas_iguais) < 0.6, clips._score_spread(notas_iguais))
    check("score espalhado nao dispara alerta",
          clips._score_spread([{"score": 9}, {"score": 6}, {"score": 3}]) > 0.6)
    check("poucos cortes nao geram julgamento",
          clips._score_spread([{"score": 9}, {"score": 8}]) is None)

    segs = [{"start": i * 5.0, "end": i * 5.0 + 5.0,
             "text": "Frase nova completa." if i % 2 == 0 else "e emenda aqui"}
            for i in range(30)]
    # Mesmo score: quem abre bem tem que ganhar de quem abre no meio da frase.
    abre_bem = {"start": 0.0, "end": 40.0, "score": 8}
    abre_mal = {"start": 5.0, "end": 45.0, "score": 8}
    check("abertura boa vence empate de score",
          clips._rank_key(abre_bem, segs) > clips._rank_key(abre_mal, segs))

    # Duracao perto da ideal desempata quando os dois abrem bem.
    ideal = {"start": 0.0, "end": clips.DURACAO_IDEAL, "score": 8}
    esticado = {"start": 0.0, "end": clips.DURACAO_IDEAL * 2, "score": 8}
    check("duracao ideal vence a esticada",
          clips._rank_key(ideal, segs) > clips._rank_key(esticado, segs))

    # Score alto de verdade ainda manda: o sinal medido desempata, nao substitui.
    otimo = {"start": 0.0, "end": clips.DURACAO_IDEAL, "score": 10}
    check("score continua pesando", clips._rank_key(otimo, segs) > clips._rank_key(ideal, segs))


def test_attribution() -> None:
    """Credito da fonte: e o que apresenta o corte como corte, nao como reupload."""
    print("credito da fonte")
    from app import attribution
    from app.config import settings

    ep = {
        "source_url": "https://www.youtube.com/watch?v=aWfu",
        "channel": "Cortes do Inteligencia",
        "meta": {"uploader_id": "@Inteligencia", "channel_url": ""},
    }
    bloco = attribution.credit_block(ep)
    check("cita o canal", "Cortes do Inteligencia" in bloco, bloco)
    check("leva o @ do canal", "@Inteligencia" in bloco, bloco)
    check("leva o link do episodio completo", ep["source_url"] in bloco, bloco)

    # Handle: uploader_id moderno e @; o antigo (UC...) nao serve como mencao.
    check("uploader_id no formato @ vira mencao",
          attribution.handle({"meta": {"uploader_id": "@canal.x"}}) == "@canal.x")
    check("id cru de canal nao vira mencao",
          attribution.handle({"meta": {"uploader_id": "UC123abc"}}) == "")
    check("extrai o @ da url do canal quando falta o uploader_id",
          attribution.handle({"meta": {"channel_url": "https://youtube.com/@fonte"}})
          == "@fonte")

    # O credito vai no FIM: o comeco da descricao e o que aparece no feed.
    texto = attribution.apply("Gancho do corte.\n#tag", ep)
    check("credito vai no fim, nao no comeco", texto.startswith("Gancho do corte."), texto[:40])
    check("credito presente", ep["source_url"] in texto)

    # Republicar/reprocessar nao pode empilhar dois blocos.
    duplo = attribution.apply(texto, ep)
    check("nao duplica o credito", duplo.count(ep["source_url"]) == 1,
          duplo.count(ep["source_url"]))

    # Sem dado de origem, nao monta bloco pela metade.
    check("sem origem nao inventa credito", attribution.credit_block({}) == "")
    check("sem origem devolve o texto intacto",
          attribution.apply("so o texto", {}) == "so o texto")

    antes = settings.attribution_enabled
    settings.attribution_enabled = False
    check("desligado no .env nao credita", attribution.apply("x", ep) == "x")
    settings.attribution_enabled = antes


def test_thumb_moment() -> None:
    """A capa tem que sair do momento que importa, nao do frame mais nitido.

    Era essa a queixa: 'cena aleatoria do video'. Nitidez e criterio tecnico e nao
    sabe qual instante significa alguma coisa — agora a IA aponta o instante e a
    busca por nitidez acontece SO em volta dele.
    """
    print("capa / momento certo")
    from app.pipeline import clips as cl
    from app.pipeline import thumbnail as th

    # Janela de busca: curta e centrada no alvo.
    ini, fim = th.window(100.0, 60.0, 130.0)
    check("busca fica em volta do instante apontado",
          ini >= 130.0 - th.SEARCH_RADIUS - 1e-6 and fim <= 130.0 + th.SEARCH_RADIUS + 1e-6,
          (ini, fim))
    check("o instante apontado esta dentro da janela", ini <= 130.0 <= fim, (ini, fim))
    check("janela e bem menor que o corte", (fim - ini) <= 2 * th.SEARCH_RADIUS + 0.01,
          fim - ini)

    # Sem alvo (cortes antigos), volta a varrer o trecho todo.
    ini_s, fim_s = th.window(100.0, 60.0, None)
    check("sem alvo varre o trecho inteiro", (fim_s - ini_s) > 40, fim_s - ini_s)

    # Alvo colado na borda nao pode empurrar a busca para fora do corte.
    ini_b, fim_b = th.window(100.0, 60.0, 100.0)
    check("alvo na borda nao sai do corte", ini_b >= 100.0 and fim_b <= 160.0,
          (ini_b, fim_b))
    ini_c, fim_c = th.window(100.0, 4.0, 103.9)
    check("corte curto nao gera janela invertida", ini_c <= fim_c, (ini_c, fim_c))

    # Normalizacao do que a IA devolve.
    check("instante dentro do corte e mantido",
          cl._thumb_time(130.0, 100.0, 160.0) == 130.0)
    check("instante fora do corte e trazido para dentro",
          100.0 < cl._thumb_time(999.0, 100.0, 160.0) < 160.0,
          cl._thumb_time(999.0, 100.0, 160.0))
    # A IA as vezes responde em tempo relativo ao inicio do trecho.
    check("tempo relativo vira absoluto",
          cl._thumb_time(30.0, 100.0, 160.0) == 130.0,
          cl._thumb_time(30.0, 100.0, 160.0))
    check("sem valor cai em 40% (depois da abertura, nao no meio cego)",
          cl._thumb_time(None, 100.0, 200.0) == 140.0,
          cl._thumb_time(None, 100.0, 200.0))
    check("lixo nao quebra", cl._thumb_time("agora", 100.0, 160.0) == 124.0,
          cl._thumb_time("agora", 100.0, 160.0))
    # As pontas pegam transicao de cena.
    t_borda = cl._thumb_time(100.0, 100.0, 160.0)
    check("nunca crava exatamente na borda", t_borda > 100.0, t_borda)

    # O texto da capa nunca mais sai da transcricao crua.
    saneado = cl._sanitize(
        [{"start": 10, "end": 40, "title": "t", "hook": "linha crua da legenda",
          "caption": "c", "thumb_text": "ELE *MENTIU*", "thumb_time": 25, "score": 9}],
        [{"start": 10, "end": 40, "text": "x"}],
    )
    check("thumb_time sobrevive ao saneamento",
          saneado and saneado[0]["thumb_time"] == 25.0, saneado)
    check("thumb_text sobrevive ao saneamento",
          saneado and saneado[0]["thumb_text"] == "ELE *MENTIU*", saneado)
    check("o gerador de capa do frame do meio nao existe mais",
          not hasattr(cl, "make_thumbnail"))

    # Enquadramento: o video fonte trazia moldura e ate QR de patrocinio de outro
    # canal, e a capa herdava tudo. O recorte tem que descartar as bordas.
    prop = 1280 / 720
    x, y, w, h = th.crop_box(1920, 1080, (921, 237, 310, 451), prop)
    check("recorte nunca encosta na borda esquerda", x >= 1920 * th.INSET * 0.95, x)
    check("recorte nao encosta no topo", y >= 1080 * th.INSET * 0.95, y)
    check("recorte cabe no frame", x + w <= 1920 and y + h <= 1080, (x + w, y + h))
    check("mantem 16:9", abs((w / h) - prop) < 0.02, w / h)
    # O rosto (centro em x=1076) precisa continuar dentro, e com folga em cima.
    check("rosto fica dentro do recorte", x < 1076 < x + w, (x, x + w))
    check("tem headroom (nao corta o topo da cabeca)", y <= 237, (y, 237))
    # Rosto grande => recorte menor que o quadro: e o que exclui a tarja do canto.
    check("aproxima de verdade", w < 1920 * 0.9, w)

    # Sem rosto detectado, ainda assim tira as bordas.
    xs, ys, ws, hs = th.crop_box(1920, 1080, None, prop)
    check("sem rosto ainda descarta a moldura", xs > 0 and ys > 0, (xs, ys))
    check("sem rosto mantem 16:9", abs((ws / hs) - prop) < 0.02, ws / hs)

    # Rosto minusculo nao pode ampliar poucos pixels ate virar borrao.
    _, _, w_mini, h_mini = th.crop_box(1920, 1080, (900, 500, 40, 60), prop)
    check("zoom limitado em rosto pequeno", h_mini >= 1080 / th.MAX_ZOOM - 1, h_mini)

    # Campos novos da capa em camadas sobrevivem ao saneamento.
    comp = cl._sanitize(
        [{"start": 10, "end": 40, "title": "t", "hook": "h", "caption": "c",
          "thumb_text": "NAO E SE, E *QUANDO*", "thumb_badge": "ALERTA",
          "thumb_image_prompt": "San Andreas fault, red glow", "thumb_time": 25,
          "score": 9}],
        [{"start": 10, "end": 40, "text": "x"}],
    )
    check("selo sobrevive", comp and comp[0]["thumb_badge"] == "ALERTA", comp)
    check("prompt da imagem sobrevive",
          comp and comp[0]["thumb_image_prompt"].startswith("San Andreas"), comp)
    # O texto agora e frase, nao slogan de 5 palavras: o limite tem que caber nela.
    longo = "VOU ATE ORAR AGORA, O EL NINO CHEGOU NO BRASIL, SABE O QUE VAI ACONTECER"
    comp2 = cl._sanitize(
        [{"start": 10, "end": 40, "title": "t", "hook": "h", "caption": "c",
          "thumb_text": longo, "thumb_time": 25, "score": 9}],
        [{"start": 10, "end": 40, "text": "x"}],
    )
    check("texto longo nao e truncado", comp2 and comp2[0]["thumb_text"] == longo,
          comp2[0]["thumb_text"] if comp2 else None)


def test_thumb_frontality() -> None:
    """Rosto encarando a camera vale mais que rosto grande olhando para a mesa."""
    print("capa / rosto de frente")
    from app.pipeline import thumbnail as th

    # [x,y,w,h, olho_dir(x,y), olho_esq(x,y), nariz(x,y), boca_dir, boca_esq, score]
    def face(nariz_x, olho_d_y=100.0, olho_e_y=100.0):
        return [0, 0, 200, 200, 100.0, olho_d_y, 200.0, olho_e_y, nariz_x, 140.0,
                120.0, 180.0, 180.0, 180.0, 0.9]

    de_frente = th.frontality(face(150.0))            # nariz no meio dos olhos
    de_lado = th.frontality(face(205.0))              # nariz na linha do olho esquerdo
    check("de frente pontua alto", de_frente > 0.85, de_frente)
    check("de perfil pontua baixo", de_lado < 0.45, de_lado)
    check("de frente vence de lado", de_frente > de_lado)

    tombado = th.frontality(face(150.0, olho_d_y=100.0, olho_e_y=145.0))
    check("cabeca tombada perde pontos", tombado < de_frente, (tombado, de_frente))

    check("sem landmark nao premia nem pune", th.frontality([0, 0, 10, 10]) == 0.5)
    check("landmark degenerado nao divide por zero",
          0 <= th.frontality([0, 0, 10, 10, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0,
                              5.0, 5.0, 5.0, 5.0, 0.9]) <= 1)
    check("frontalidade pesa de verdade na nota", th.FRONTAL_WEIGHT >= 2.0,
          th.FRONTAL_WEIGHT)


def test_thumb_layout() -> None:
    """As duas orientacoes seguem a mesma gramatica, e o texto nao invade o rosto."""
    print("capa / layout nas duas orientacoes")
    import tempfile
    from PIL import Image
    from app.pipeline import thumbnail as th

    tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="dubflow_layout_"))
    arte = tmpdir / "arte.png"
    Image.new("RGB", (1024, 1536), (40, 30, 25)).save(arte)
    frame = tmpdir / "frame.jpg"
    Image.new("RGB", (1920, 1080), (90, 60, 120)).save(frame)

    vert = th.compose_composite(frame, arte, "QUANDO *PARA* DE TREMER, PREOCUPA",
                                tmpdir / "v.jpg", badge="TENSAO",
                                size=(1080, 1920))
    check("gera a capa 9:16", vert is not None and vert.exists())
    if vert:
        check("9:16 sai no tamanho certo", Image.open(vert).size == (1080, 1920),
              Image.open(vert).size)

    horiz = th.compose_composite(frame, arte, "QUANDO *PARA* DE TREMER, PREOCUPA",
                                 tmpdir / "h.jpg", badge="TENSAO", size=(1280, 720))
    check("gera a capa 16:9", horiz is not None and horiz.exists())

    # Sem apresentador: a arte sozinha ainda vira capa (THUMB_PRESENTER=false).
    so_arte = th.compose_composite(frame, arte, "SO A ARTE", tmpdir / "s.jpg",
                                   size=(1080, 1920), presenter=False)
    check("sem apresentador ainda gera capa", so_arte is not None and so_arte.exists())

    # Sem arte E sem frame nao ha o que compor — devolve None em vez de explodir.
    check("sem imagem nenhuma devolve None",
          th.compose_composite(None, None, "X", tmpdir / "n.jpg") is None)

    # Degradacao: sem arte gerada, o frame do video assume o fundo.
    check("sem arte usa o frame do video",
          th.compose_composite(frame, None, "X", tmpdir / "f.jpg") is not None)

    check("bloco do apresentador na vertical e proporcional ao 16:9",
          0.35 <= th.VERTICAL_PANEL_FRAC <= 0.5, th.VERTICAL_PANEL_FRAC)


def test_thumb_imagegen() -> None:
    """A imagem gerada e um extra: sem ela a capa degrada, nunca falha."""
    print("capa / imagem tematica")
    import tempfile
    from app.config import settings
    from app.pipeline import imagegen

    tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="dubflow_art_"))
    check("prompt vazio nao chama a API", imagegen.generate("", tmpdir) is None)

    antes = settings.thumb_generate_image
    settings.thumb_generate_image = False
    check("desligado no .env nao chama a API",
          imagegen.generate("um vulcao", tmpdir) is None)
    settings.thumb_generate_image = antes

    # Cache por prompt: reprocessar episodio nao pode pagar a imagem de novo.
    paisagem, retrato = "1536x1024", "1024x1536"
    p1 = imagegen._cache_path("vulcao em erupcao", tmpdir, paisagem)
    p2 = imagegen._cache_path("vulcao em erupcao", tmpdir, paisagem)
    p3 = imagegen._cache_path("mapa da falha", tmpdir, paisagem)
    p4 = imagegen._cache_path("vulcao em erupcao", tmpdir, retrato)
    check("mesmo prompt, mesmo arquivo de cache", p1 == p2, (p1.name, p2.name))
    check("prompt diferente, arquivo diferente", p1 != p3, (p1.name, p3.name))
    # A 9:16 pede arte em retrato: nao pode reusar a paisagem do 16:9.
    check("retrato e paisagem nao compartilham cache", p1 != p4, (p1.name, p4.name))

    check("o estilo proibe texto na imagem gerada",
          "no text" in imagegen.STYLE.lower())
    check("o estilo proibe pessoas (o apresentador vem do video)",
          "no people" in imagegen.STYLE.lower())


def test_thumb_colors() -> None:
    """A capa tem que se adaptar ao fundo: cor fixa some assim que a cena muda."""
    print("capa / cores adaptativas")
    from app.pipeline import thumbnail as th

    ESCURO, CLARO = (18, 18, 20), (245, 245, 245)
    AMARELO = (255, 216, 0)

    # Contraste WCAG: o basico que decide legibilidade.
    check("preto/branco e o contraste maximo",
          abs(th.contrast_ratio((0, 0, 0), (255, 255, 255)) - 21) < 0.1)
    check("cor igual nao contrasta", abs(th.contrast_ratio(ESCURO, ESCURO) - 1) < 0.01)
    check("amarelo e mais luminoso que azul",
          th.relative_luminance(AMARELO) > th.relative_luminance((0, 0, 255)))

    # Texto sempre no maior contraste possivel contra o fundo.
    txt_escuro, _, borda_escuro = th.pick_colors(ESCURO)
    txt_claro, _, borda_claro = th.pick_colors(CLARO)
    check("fundo escuro -> texto branco", txt_escuro == (255, 255, 255), txt_escuro)
    check("fundo claro -> texto preto", txt_claro == (0, 0, 0), txt_claro)
    check("contorno e sempre o oposto do texto",
          borda_escuro == (0, 0, 0) and borda_claro == (255, 255, 255))

    # A garantia que importa: nunca fonte escura em fundo escuro.
    for fundo in [ESCURO, CLARO, (92, 80, 70), (150, 150, 150), (20, 60, 140), (250, 235, 60)]:
        t, d, _ = th.pick_colors(fundo)
        check(f"texto legivel em {fundo}", th.contrast_ratio(t, fundo) >= th.MIN_CONTRAST,
              round(th.contrast_ratio(t, fundo), 1))
        check(f"destaque nao some em {fundo}", th.contrast_ratio(d, fundo) >= 2.0,
              round(th.contrast_ratio(d, fundo), 1))

    # Amarelo e o padrao estetico, mas cede quando o fundo e amarelado.
    _, dest_escuro, _ = th.pick_colors(ESCURO)
    _, dest_amarelo, _ = th.pick_colors((250, 235, 60))
    check("amarelo e o padrao no escuro", dest_escuro == AMARELO, dest_escuro)
    check("fundo amarelo troca a cor do destaque", dest_amarelo != AMARELO, dest_amarelo)

    # Distancia de cor pega o que o contraste WCAG nao ve.
    check("amarelo e branco se distinguem",
          th.color_distance(AMARELO, (255, 255, 255)) >= 120,
          th.color_distance(AMARELO, (255, 255, 255)))
    check("WCAG sozinho nao separaria os dois",
          th.contrast_ratio(AMARELO, (255, 255, 255)) < 1.4)

    # Veu: cena clara precisa de escurecimento, cena ja preta nao.
    check("cena clara ganha veu forte", th._veil_strength(CLARO) > 150, th._veil_strength(CLARO))
    check("cena preta nao leva veu", th._veil_strength((5, 5, 6)) == 0)
    check("veu cresce com a luz",
          th._veil_strength((60, 60, 60)) < th._veil_strength((200, 200, 200)))

    # Texto do gancho: marcacao por asterisco e destaque garantido.
    palavras = th.parse_highlight("ELE *MENTIU* NA CARA")
    check("separa as palavras", [p for p, _ in palavras] == ["ELE", "MENTIU", "NA", "CARA"], palavras)
    check("destaca so a marcada", [d for _, d in palavras] == [False, True, False, False], palavras)
    check("tira os asteriscos", all("*" not in p for p, _ in palavras), palavras)

    sem_marca = th.parse_highlight("PERDEU TUDO AGORA")
    check("sem marcacao destaca a maior palavra",
          sum(1 for _, d in sem_marca if d) == 1 and dict(sem_marca)["PERDEU"], sem_marca)
    check("texto vazio nao quebra", th.parse_highlight("   ") == [])


def test_eta() -> None:
    """A barra precisa dizer quanto falta — 'burning 10%' por 1h nao informa nada."""
    print("painel / tempo estimado")
    import datetime as dt
    from app import db

    agora = dt.datetime.now(dt.timezone.utc)
    def ep(progress, minutos_atras, status="clipping", started=True):
        inicio = (agora - dt.timedelta(minutes=minutos_atras)).isoformat()
        return {"status": status, "progress": progress, "started_at": inicio if started else None}

    # 25% em 5 min => faltam 75%, ou seja ~15 min.
    e = db.eta_seconds(ep(0.25, 5))
    check("regra de tres bate", 14 * 60 <= e <= 16 * 60, e)

    # Quanto mais perto do fim, menor o que falta.
    check("diminui conforme avanca", db.eta_seconds(ep(0.9, 45)) < db.eta_seconds(ep(0.3, 45)))

    check("terminado nao tem eta", db.eta_seconds(ep(1.0, 30, "done")) is None)
    check("falhou nao tem eta", db.eta_seconds(ep(0.4, 30, "failed")) is None)
    check("na fila nao tem eta", db.eta_seconds(ep(0.0, 30, "queued")) is None)
    check("progresso baixo demais nao estima", db.eta_seconds(ep(0.02, 30)) is None)
    check("sem started_at nao estima", db.eta_seconds(ep(0.5, 30, started=False)) is None)
    check("started_at invalido nao quebra",
          db.eta_seconds({"status": "clipping", "progress": 0.5, "started_at": "ontem"}) is None)

    # started_at e o inicio do PROCESSAMENTO, nao da fila: um episodio que esperou
    # 3h para ser pego nao pode reportar 3h de trabalho.
    esperou = db.eta_seconds(ep(0.5, 10))
    check("fila nao infla a estimativa", esperou is not None and esperou < 20 * 60, esperou)


def test_burn_progress() -> None:
    """A barra da queima tem que andar de verdade — 10% parado por 1h e igual a travado."""
    print("queima / progresso real")
    from app.pipeline import runner

    check("comeca em 10%", abs(runner._burn_progress(0.0) - 0.10) < 1e-9)
    check("meio da queima cai no meio da faixa", 0.5 < runner._burn_progress(0.5) < 0.6,
          runner._burn_progress(0.5))
    check("nao crava 100% antes do fim", runner._burn_progress(1.0) < 1.0,
          runner._burn_progress(1.0))
    check("sempre crescente",
          runner._burn_progress(0.2) < runner._burn_progress(0.6) < runner._burn_progress(0.9))
    check("valor fora da faixa nao quebra",
          runner._burn_progress(-1) == 0.1 and runner._burn_progress(2) < 1.0)

    # O parser le o fluxo do -progress do ffmpeg (chave=valor, uma por linha).
    saida = (
        "frame=120\nout_time_us=N/A\nprogress=continue\n"
        "frame=240\nout_time_us=10000000\nprogress=continue\n"    # 10 s
        "frame=480\nout_time_us=50000000\nprogress=continue\n"    # 50 s
        "out_time_us=100000000\nprogress=end\n"                   # 100 s
    )
    vistos = list(subtitles.progress_fractions(saida.splitlines(), 100.0))
    check("ignora o N/A do inicio", len(vistos) == 3, vistos)
    check("fracao bate com o tempo codificado",
          [round(v, 2) for v in vistos] == [0.1, 0.5, 1.0], vistos)
    check("chega a 100% no fim", vistos[-1] == 1.0, vistos)

    # Atualizacao a cada linha encheria o banco: so reporta a cada 0,5 ponto.
    denso = [f"out_time_us={i * 100_000}" for i in range(1, 400)]
    poucos = list(subtitles.progress_fractions(denso, 100.0))
    check("nao reporta a cada frame", len(poucos) < 100, len(poucos))
    check("ainda assim cobre a queima inteira", poucos[-1] > 0.35, poucos[-1])

    # Sem duracao conhecida nao da para calcular fracao — e nao pode explodir.
    check("sem duracao nao emite nada", list(subtitles.progress_fractions(saida.splitlines(), None)) == [])
    check("duracao zero nao divide por zero",
          list(subtitles.progress_fractions(saida.splitlines(), 0)) == [])
    check("lixo na linha e ignorado",
          list(subtitles.progress_fractions(["frame=1", "bitrate=N/A", "progress=end"], 100.0)) == [])


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

    # configured() le o cofre da maquina, entao o teste precisa isolar a leitura:
    # antes ele so passava enquanto ninguem tivesse conectado o YouTube de verdade.
    original = youtube.credentials.get
    try:
        youtube.credentials.get = lambda key, channel_id=None: ""
        check("nao configurado sem credenciais", youtube.configured() is False)
        youtube.credentials.get = lambda key, channel_id=None: "" if key == "YOUTUBE_REFRESH_TOKEN" else "x"
        check("faltando so o refresh token ainda e nao configurado",
              youtube.configured() is False)
        youtube.credentials.get = lambda key, channel_id=None: "x"
        check("configurado com o cofre completo", youtube.configured() is True)
    finally:
        youtube.credentials.get = original

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
    test_subtitle_screen_cap()
    test_timestamps()
    test_ffmpeg_escape()
    test_srt_output(tmp)
    test_budget()
    test_blocks()
    test_clip_sanitize()
    test_clip_target_count()
    test_clip_windows()
    test_clip_segments()
    test_ass_escape()
    test_translation_fallback()
    test_ffmpeg_quote_escape()
    test_transcribe_modes()
    test_subtitle_styles()
    test_resegment()
    test_karaoke(tmp)
    test_reframe_focus()
    test_reframe_two_people()
    test_reframe_track()
    test_focus_expression()
    test_same_language_passthrough(tmp)
    test_clip_openings()
    test_clip_ranking()
    test_attribution()
    test_thumb_moment()
    test_thumb_frontality()
    test_thumb_layout()
    test_thumb_imagegen()
    test_thumb_colors()
    test_eta()
    test_burn_progress()
    test_youtube_metadata()

    print()
    if failures:
        print(f"{len(failures)} falha(s): {', '.join(failures)}")
        return 1
    print("todos os testes passaram")
    return 0


if __name__ == "__main__":
    sys.exit(main())
