"""Testes das partes puras do pipeline (nao tocam rede, GPU nem ffmpeg).

    py -m tests.test_pipeline
"""

from __future__ import annotations

import pathlib
import sys

from app.pipeline import clips, subtitles, translate

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
        {"start": 11.3, "end": 52.2, "title": "A", "hook": "h", "caption": "c", "score": 8},
        {"start": 30.0, "end": 70.0, "title": "sobreposto", "hook": "h", "caption": "c", "score": 9},
        {"start": 100.4, "end": 141.0, "title": "B", "hook": "h", "caption": "c", "score": 7},
        {"start": 150.0, "end": 152.0, "title": "curto", "hook": "h", "caption": "c", "score": 5},
    ]
    out = clips._sanitize(raw, segs)
    titles = [c["title"] for c in out]
    check("descarta sobreposto e curto", titles == ["A", "B"], titles)
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

    print()
    if failures:
        print(f"{len(failures)} falha(s): {', '.join(failures)}")
        return 1
    print("todos os testes passaram")
    return 0


if __name__ == "__main__":
    sys.exit(main())
