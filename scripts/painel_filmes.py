"""Painel do molde de filmes: escolhe o visual, ve a previa, edita ganchos, renderiza.

A previa sai do MESMO codigo do render (molde_filmes.previa), entao o que aparece
no painel e o que vai pro video.

Feito para lotes (~100 videos): corte padrao + corte aplicado a uma selecao,
render em paralelo, e limpeza de arquivos e do cache em memoria.

Uso (na pasta dubflow):
    .venv\\Scripts\\python.exe -m scripts.painel_filmes
    -> http://127.0.0.1:8035
"""

from __future__ import annotations

import io
import logging
import secrets
import shutil
import subprocess
import sys
import threading
import webbrowser
import zipfile
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Query, UploadFile
from starlette.background import BackgroundTask
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from PIL import Image, ImageDraw, ImageFont

from scripts import instagram_filmes as ig
from scripts import legenda_filmes as lf
from scripts import legenda_ia_filmes as lia
from scripts import molde_filmes as mf

PORTA = 8035   # 8030 e do painel principal do dubflow (PORT no .env)
HTML = Path(__file__).with_name("painel_filmes.html")
MAX_QUADROS = 150         # quadros na RAM (reduzidos a 1280px: ~2,7 MB cada)
LADO_QUADRO = 1280
MAX_SIMULTANEOS = 6

log = logging.getLogger("painel_filmes")
app = FastAPI(title="Painel de filmes")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
_trava = threading.Lock()          # um lote por vez
_trava_status = threading.Lock()   # _status e escrito por varias threads
_trava_cache = threading.Lock()    # _quadros e lido/escrito por requisicoes paralelas
_status: dict = {"rodando": False, "parando": False, "feitos": 0, "total": 0,
                 "ativos": {}, "erros": [], "fila": set()}
_quadros: OrderedDict[str, Image.Image] = OrderedDict()
_duracoes: dict[str, float] = {}


def _video(nome: str) -> Path:
    """So aceita arquivo que esta de fato na pasta de entrada."""
    alvo = mf.ENTRADA / Path(nome).name
    if alvo.suffix.lower() not in mf.VIDEOS or not alvo.exists():
        raise HTTPException(404, f"video nao encontrado: {nome}")
    return alvo


def _selecao(nomes: list[str] | None) -> list[Path]:
    """Os videos pedidos; lista vazia ou None = todos."""
    if not nomes:
        return mf.listar_videos()
    return [_video(n) for n in nomes]


def _bloquear_se_na_fila(videos: list[Path]) -> None:
    na_fila = _status["fila"] if _status["rodando"] else set()
    presos = [v.name for v in videos if v.name in na_fila]
    if presos:
        raise HTTPException(409, f"{len(presos)} video(s) na fila do render; "
                                 "espere terminar ou clique em Parar")


def _chave(video: Path) -> str:
    return f"{video.name}:{video.stat().st_mtime}"


def _quadro(video: Path | None) -> Image.Image:
    if video is None:
        img = Image.new("RGB", (1280, 720), (60, 60, 70))
        d = ImageDraw.Draw(img)
        d.text((640, 360), "SEU VIDEO AQUI", fill=(200, 200, 210), anchor="mm",
               font=ImageFont.truetype(mf._fonte_arq(mf.PADRAO), 90))
        return img
    chave = _chave(video)
    with _trava_cache:
        if chave in _quadros:
            _quadros.move_to_end(chave)
            return _quadros[chave]
    # 1.5s pula tela preta de abertura; video mais curto que isso usa o 1o quadro
    instante = "1.5" if _duracao(video) > 2 else "0"
    png = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-ss", instante, "-i", str(video),
         "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
        check=True, capture_output=True).stdout
    if not png:
        raise HTTPException(422, f"nao consegui ler um quadro de {video.name}")
    img = Image.open(io.BytesIO(png)).convert("RGB")
    img.thumbnail((LADO_QUADRO, LADO_QUADRO))  # proporcao intacta: o corte e em %
    with _trava_cache:
        _quadros[chave] = img
        while len(_quadros) > MAX_QUADROS:
            _quadros.popitem(last=False)
    return img


def _duracao(video: Path) -> float:
    chave = _chave(video)
    if chave not in _duracoes:
        _duracoes[chave] = mf.duracao(video)
    return _duracoes[chave]


@app.get("/", response_class=HTMLResponse)
def pagina() -> str:
    return HTML.read_text(encoding="utf-8")


# ---------------------------------------------------------------- molde

@app.get("/api/molde")
def ler_molde() -> dict:
    return {"molde": mf.carregar_molde(), "fontes": list(mf.FONTES)}


@app.post("/api/molde")
def gravar_molde(molde: dict) -> dict:
    antes = mf.carregar_molde()
    novo = mf.salvar_molde(molde)
    if bool(antes["legendar_padrao"]) != bool(novo["legendar_padrao"]):
        if _status["rodando"]:
            mf.salvar_molde({**novo, "legendar_padrao": antes["legendar_padrao"]})
            raise HTTPException(409, "espere o render terminar para mudar a legenda padrao")
        for v in mf.listar_videos():
            if not lf.tem_escolha_propria(v):
                mf.apagar_saidas(v)
    return novo


@app.post("/api/previa")
def gerar_previa(dados: dict) -> Response:
    molde = {**mf.PADRAO, **dados.get("molde", {})}
    video = _video(dados["video"]) if dados.get("video") else None
    gancho = dados.get("gancho") or (molde["ganchos"] or mf.PADRAO["ganchos"])[0]
    if "corte" in dados:
        corte = dados["corte"]
    else:
        corte = mf.corte_de(video, molde) if video else molde["corte_padrao"]
    img = mf.previa(molde, gancho, _quadro(video), corte, _rotulo_previa(video, molde, corte))
    buf = io.BytesIO()
    img.resize((540, 960)).save(buf, "JPEG", quality=88)
    return Response(buf.getvalue(), media_type="image/jpeg")


def _n_partes(video: Path | None, molde: dict, corte: dict | None) -> int:
    if video is None:
        return 1
    return len(mf.partes(_duracao(video), corte or mf.corte_de(video, molde), molde["parte_max"]))


def _pronto(video: Path, molde: dict) -> bool:
    """Tem render E no numero de partes que o molde pede hoje.

    Render antigo de 155s inteiro (feito antes da divisao em partes, ou com outro
    parte_max) NAO esta pronto: precisa ser refeito, e nao pode ir pra fila.
    """
    saidas = mf.saidas_de(video)
    return bool(saidas) and len(saidas) == _n_partes(video, molde, None)


def _rotulo_previa(video: Path | None, molde: dict, corte: dict | None) -> str | None:
    """A previa mostra a PARTE 1 quando o video vai ser dividido."""
    return mf.rotulo_parte(molde, 1, _n_partes(video, molde, corte))


# ---------------------------------------------------------------- videos

@app.get("/api/videos")
def videos() -> list[dict]:
    molde = mf.carregar_molde()
    ativos = _status["ativos"]
    lista = []
    for i, v in enumerate(mf.listar_videos()):
        sidecar = v.with_suffix(".txt")
        proprio = sidecar.read_text(encoding="utf-8").strip() if sidecar.exists() else ""
        lista.append({
            "nome": v.name,
            "gancho": proprio,
            "gancho_auto": mf.gancho_de(v, i, molde),
            "pronto": _pronto(v, molde),
            "partes_prontas": len(mf.saidas_de(v)),
            "partes_previstas": _n_partes(v, molde, None),
            "renderizando": v.name in ativos,
            "corte": mf.corte_de(v, molde),
            "corte_proprio": mf.tem_corte_proprio(v),
            "legendar": lf.quer_legenda(v, molde),
            "legenda_propria": lf.tem_escolha_propria(v),
            "duracao": _duracao(v),
        })
    return lista


@app.get("/api/quadro/{nome}")
def quadro_original(nome: str, lado: int = 720) -> Response:
    """Quadro do video SEM molde, para marcar o corte em cima (lado = tamanho maximo)."""
    img = _quadro(_video(nome)).copy()
    buf = io.BytesIO()
    lado = max(120, min(lado, LADO_QUADRO))
    img.thumbnail((lado, lado))
    img.save(buf, "JPEG", quality=82)
    return Response(buf.getvalue(), media_type="image/jpeg",
                    headers={"Cache-Control": "max-age=300"})


@app.get("/api/miniatura/{nome}")
def miniatura(nome: str, topo: float = 0, base: float = 0, esq: float = 0,
              dir: float = 0) -> Response:  # noqa: A002 — nome do campo do corte
    """Resultado FINAL pequeno (270x480) com o molde salvo e o corte pedido.

    E o que a grade do corte em massa mostra em "ver resultado final".
    """
    video = _video(nome)
    molde = mf.carregar_molde()
    corte = {**mf.corte_de(video, molde), "topo": topo, "base": base, "esq": esq, "dir": dir}
    img = mf.previa(molde, mf.gancho_de(video, None, molde), _quadro(video), corte,
                    _rotulo_previa(video, molde, corte))
    buf = io.BytesIO()
    img.resize((270, 480)).save(buf, "JPEG", quality=80)
    return Response(buf.getvalue(), media_type="image/jpeg")


@app.post("/api/gancho")
def gravar_gancho(dados: dict) -> dict:
    video = _video(dados["video"])
    _bloquear_se_na_fila([video])
    sidecar = video.with_suffix(".txt")
    texto = (dados.get("gancho") or "").strip()
    if texto:
        sidecar.write_text(texto, encoding="utf-8")
    else:
        sidecar.unlink(missing_ok=True)
    mf.apagar_saidas(video)
    return {"ok": True}


@app.post("/api/upload")
async def upload(arquivos: list[UploadFile]) -> dict:
    mf.ENTRADA.mkdir(parents=True, exist_ok=True)
    salvos = []
    for arq in arquivos:
        nome = Path(arq.filename or "").name
        if Path(nome).suffix.lower() not in mf.VIDEOS:
            continue
        # grava com outro nome e so renomeia no fim: lista e render nunca pegam
        # arquivo pela metade
        destino = _nome_livre(mf.ENTRADA / nome)
        temporario = destino.with_name(destino.name + ".enviando")
        with open(temporario, "wb") as f:
            while bloco := await arq.read(1 << 20):
                f.write(bloco)
        temporario.replace(destino)
        mf.apagar_saidas(destino)  # render do arquivo antigo de mesmo nome
        salvos.append(destino.name)
    return {"salvos": salvos}


def _nome_livre(destino: Path) -> Path:
    """Mesmo nome = substitui (reenvio). Mesmo nome com OUTRA extensao ganha
    sufixo, porque render, gancho e corte sao nomeados sem a extensao."""
    outros = [v for v in mf.listar_videos() if v.stem == destino.stem and v.name != destino.name]
    n = 2
    while outros:
        destino = destino.with_name(f"{Path(destino.name).stem.rsplit(' (', 1)[0]} ({n}){destino.suffix}")
        outros = [v for v in mf.listar_videos() if v.stem == destino.stem]
        n += 1
    return destino


@app.get("/api/baixar-renders")
def baixar_renders(v: list[str] = Query(default=[]), checar: bool = False):
    """Os videos prontos (ja no molde) num .zip: os pedidos em `v`, ou todos.

    ZIP sem compressao: MP4 ja e comprimido, e assim o zip sai na hora em vez de
    gastar minutos de CPU para ganhar quase nada. O arquivo temporario some
    depois do download (ou no proximo boot, se o painel cair no meio).
    """
    videos = [_video(n) for n in v] if v else mf.listar_videos()
    arquivos = [s for x in videos for s in mf.saidas_de(x)]
    if not arquivos:
        raise HTTPException(404, "nenhum video renderizado para baixar")
    if checar:  # o navegador confere antes de baixar: erro vira mensagem, nao arquivo .json
        return {"arquivos": len(arquivos),
                "mb": round(sum(a.stat().st_size for a in arquivos) / 1e6)}
    tmp = mf.RAIZ / f"renders_{secrets.token_hex(4)}.baixando.zip"
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
            for a in arquivos:
                z.write(a, a.name)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    nome = f"filmes_renderizados_{datetime.now():%Y-%m-%d_%H%M}.zip"
    log.info("zip de %d render(s) de %d video(s) para baixar", len(arquivos), len(videos))
    return FileResponse(tmp, filename=nome, media_type="application/zip",
                        background=BackgroundTask(tmp.unlink, missing_ok=True))


def _area_de_trabalho() -> Path:
    """A Area de Trabalho de verdade (com OneDrive ela nao fica em ~/Desktop)."""
    try:
        import os
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
            pasta = Path(os.path.expandvars(winreg.QueryValueEx(k, "Desktop")[0]))
        if pasta.is_dir():
            return pasta
    except OSError:
        pass
    return Path.home() / "Desktop"


PASTA_TIKTOK = "tiktok"


def _pasta_tiktok() -> Path:
    return _area_de_trabalho() / PASTA_TIKTOK


@app.post("/api/enviar-pasta")
def enviar_pasta(corpo: dict | None = None) -> dict:
    """Copia para Area de Trabalho/tiktok so o que ainda nao esta la.

    "Ja esta la" = mesmo nome e mesmo tamanho; um render refeito (tamanho
    diferente) substitui o antigo. Copia para um temporario e renomeia, para
    uma copia interrompida nunca contar como pronta na proxima vez.
    """
    nomes = (corpo or {}).get("v") or []
    videos = [_video(n) for n in nomes] if nomes else mf.listar_videos()
    arquivos = [s for x in videos for s in mf.saidas_de(x)]
    if not arquivos:
        raise HTTPException(404, "nenhum video renderizado para enviar")
    pasta = _pasta_tiktok()
    pasta.mkdir(parents=True, exist_ok=True)
    copiados, ja_tinha, bytes_ = 0, 0, 0
    for a in arquivos:
        destino = pasta / a.name
        tamanho = a.stat().st_size
        if destino.is_file() and destino.stat().st_size == tamanho:
            ja_tinha += 1
            continue
        tmp = destino.with_name(destino.name + ".copiando")
        try:
            shutil.copyfile(a, tmp)
            tmp.replace(destino)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        copiados += 1
        bytes_ += tamanho
    log.info("pasta tiktok: %d copiado(s), %d ja estavam la", copiados, ja_tinha)
    return {"copiados": copiados, "ja_tinha": ja_tinha,
            "mb": round(bytes_ / 1e6), "pasta": str(pasta)}


@app.post("/api/abrir-pasta")
def abrir_pasta() -> dict:
    pasta = _pasta_tiktok()
    pasta.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(["explorer", str(pasta)])
    return {"ok": True}


@app.get("/saida/{nome}")
def baixar_saida(nome: str, parte: int = 1) -> FileResponse:
    saidas = mf.saidas_de(_video(nome))
    if not 1 <= parte <= len(saidas):
        raise HTTPException(404, "ainda nao renderizado")
    return FileResponse(saidas[parte - 1], media_type="video/mp4")


# ---------------------------------------------------------------- corte

@app.post("/api/corte")
def gravar_corte(dados: dict) -> dict:
    """Corte proprio para um ou varios videos de uma vez.

    {"videos": [...], "corte": {...}}  -> grava o mesmo corte em todos
    {"videos": [...], "corte": null}   -> eles voltam para o corte padrao
    """
    nomes = dados.get("videos") or ([dados["video"]] if dados.get("video") else [])
    if not nomes:
        raise HTTPException(400, "nenhum video selecionado")
    alvos = [_video(n) for n in nomes]
    _bloquear_se_na_fila(alvos)
    corte = None
    for v in alvos:
        corte = mf.salvar_corte(v, dados.get("corte"))
        mf.apagar_saidas(v)  # render antigo ficou desatualizado
    return {"corte": corte, "alterados": len(alvos)}


@app.post("/api/legenda")
def gravar_legenda(dados: dict) -> dict:
    """{"videos": [...], "legendar": true | false | null (volta pro padrao)}"""
    nomes = dados.get("videos") or []
    if not nomes:
        raise HTTPException(400, "nenhum video selecionado")
    alvos = [_video(n) for n in nomes]
    _bloquear_se_na_fila(alvos)
    escolha = dados.get("legendar")
    molde = mf.carregar_molde()
    mudaram = 0
    for v in alvos:
        antes = lf.quer_legenda(v, molde)
        lf.escolher(v, None if escolha is None else bool(escolha))
        if lf.quer_legenda(v, molde) != antes:
            mf.apagar_saidas(v)  # render antigo ficou desatualizado
            mudaram += 1
    return {"alterados": len(alvos), "renders_apagados": mudaram}


@app.post("/api/corte/padrao")
def gravar_corte_padrao(dados: dict) -> dict:
    """Muda o corte padrao; os renders de quem usa o padrao ficam desatualizados."""
    if _status["rodando"]:
        raise HTTPException(409, "espere o render terminar para mudar o corte padrao")
    molde = mf.carregar_molde()
    molde["corte_padrao"] = mf.normalizar_corte(dados.get("corte"))
    molde = mf.salvar_molde(molde)
    afetados = 0
    for v in mf.listar_videos():
        if not mf.tem_corte_proprio(v) and mf.apagar_saidas(v):
            afetados += 1
    return {"corte_padrao": molde["corte_padrao"], "renders_apagados": afetados}


# ---------------------------------------------------------------- render

def _render_um(i: int, v: Path, molde: dict) -> None:
    if _status["parando"]:
        with _trava_status:
            _status["fila"].discard(v.name)
        return
    _status["ativos"][v.name] = 0
    try:
        corte = mf.corte_de(v, molde)
        falas = None
        if lf.quer_legenda(v, molde):
            _status["ativos"][v.name] = "legendando"
            falas = lf.segmentos(v, corte["inicio"], corte["fim"])
            _status["ativos"][v.name] = 0
        mf.renderizar_video(
            v, molde, mf.gancho_de(v, i, molde), corte,
            progresso=lambda f: _status["ativos"].__setitem__(v.name, round(f * 100)),
            legenda=falas)
    except subprocess.CalledProcessError as exc:
        _status["erros"].append(f"{v.name}: {(exc.stderr or str(exc)).strip()[-300:]}")
        log.error("render falhou: %s: %s", v.name, exc.stderr)
    except Exception as exc:  # noqa: BLE001 — um video ruim nao derruba o lote
        _status["erros"].append(f"{v.name}: {exc}")
        log.exception("render falhou: %s", v.name)
    finally:
        with _trava_status:
            _status["ativos"].pop(v.name, None)
            _status["fila"].discard(v.name)
            _status["feitos"] += 1


def _rodar_lote(fila: list[tuple[int, Path]], simultaneos: int) -> None:
    molde = mf.carregar_molde()
    mf.SAIDA.mkdir(parents=True, exist_ok=True)
    try:
        with ThreadPoolExecutor(max_workers=simultaneos) as pool:
            for i, v in fila:
                pool.submit(_render_um, i, v, molde)
    finally:
        _status.update(rodando=False, parando=False, ativos={}, fila=set())
        _trava.release()


@app.post("/api/renderizar")
def renderizar(dados: dict | None = None) -> dict:
    """{"videos": [...] (vazio = todos), "refazer": bool, "simultaneos": n}"""
    dados = dados or {}
    if not _trava.acquire(blocking=False):
        raise HTTPException(409, "ja tem render rodando")
    try:
        escolhidos = {v.name for v in _selecao(dados.get("videos"))}
        refazer = bool(dados.get("refazer"))
        molde = mf.carregar_molde()
        fila = [(i, v) for i, v in enumerate(mf.listar_videos())
                if v.name in escolhidos and (refazer or not _pronto(v, molde))]
        simultaneos = max(1, min(MAX_SIMULTANEOS, int(dados.get("simultaneos") or 3)))
    except Exception:
        _trava.release()
        raise
    if not fila:
        _trava.release()
        return {"total": 0}
    _status.update(rodando=True, parando=False, feitos=0, total=len(fila),
                   ativos={}, erros=[], fila={v.name for _, v in fila})
    threading.Thread(target=_rodar_lote, args=(fila, simultaneos), daemon=True).start()
    return {"total": len(fila)}


@app.post("/api/parar")
def parar() -> dict:
    """Esvazia a fila: os que ja estao renderizando terminam, os outros nao comecam."""
    if _status["rodando"]:
        _status["parando"] = True
    return {"ok": True}


@app.get("/api/status")
def status() -> dict:
    with _trava_status:
        return {**_status, "ativos": dict(_status["ativos"]), "erros": list(_status["erros"]),
                "fila": sorted(_status["fila"])}


# ---------------------------------------------------------------- instagram

def _erro_ig(exc: Exception) -> HTTPException:
    return HTTPException(400, str(exc))


@app.get("/api/ig")
def ig_estado() -> dict:
    """Fila com `desatualizado` em cada pendente cujo render nao bate mais: o video foi
    (ou precisa ser) renderizado de novo em outro numero de partes."""
    molde = mf.carregar_molde()
    fila = ig.fila()
    estado: dict[str, tuple[bool, int]] = {}
    for x in fila:
        if x["status"] not in ("pendente", "erro"):
            continue
        if x["video"] not in estado:
            v = mf.ENTRADA / x["video"]
            estado[x["video"]] = (v.exists() and _pronto(v, molde), len(mf.saidas_de(v)))
        ok, n = estado[x["video"]]
        x["desatualizado"] = not ok or n != x.get("partes", 1)
    parados = ig.bloqueados(fila)
    for x in fila:
        x["bloqueado"] = x["id"] in parados
        x.pop("metricas", None)  # numeros vao pela tela de resultados
    return {"config": ig.config_publica(), "fila": fila, "avisos": ig.avisos()}


@app.get("/api/avisos")
def avisos() -> list[dict]:
    """Faixa de avisos da tela principal (token vencendo, posts com erro)."""
    return ig.avisos()


@app.post("/api/ig/esquecer-app")
def ig_esquecer_app() -> dict:
    return ig.esquecer_app()


@app.post("/api/ig/repetir")
def ig_repetir(dados: dict) -> dict:
    """Posts com erro (e as partes seguintes paradas) voltam para a fila."""
    ids = [str(i) for i in dados.get("ids") or []]
    if dados.get("todos"):
        ids = [x["id"] for x in ig.fila() if x["status"] == "erro"]
    if not ids:
        raise HTTPException(400, "nenhum post com erro selecionado")
    antes = {x["id"] for x in ig.fila() if x["status"] == "erro"}
    fila = ig.repetir(ids)
    return {"fila": fila, "repetidos": len(antes & set(ids))}


@app.post("/api/ig/legenda-ia")
def ig_legenda_ia(dados: dict) -> dict:
    """Reescreve com a IA a legenda de um post pendente ({"id"}) ou de todos
    os pendentes ({"todos": true}; partes do mesmo video numa chamada so)."""
    pendentes = [x for x in ig.fila() if x["status"] == "pendente"]
    if dados.get("todos"):
        alvos = pendentes
    else:
        alvos = [x for x in pendentes if x["id"] == str(dados.get("id", ""))]
    if not alvos:
        raise HTTPException(409, "so da para reescrever post pendente")
    molde = mf.carregar_molde()
    pedidos = []
    for x in alvos:
        video = mf.ENTRADA / x["video"]
        pedidos.append({"video": video, "parte": x.get("parte", 1), "partes": x.get("partes", 1),
                        "gancho": x.get("gancho") or mf.gancho_de(video, None, molde)})
    textos = lia.gerar(pedidos, ig.config())
    feitos = 0
    for x, t in zip(alvos, textos):
        if t:
            ig._atualizar(x["id"], legenda=t)
            feitos += 1
    if not feitos:
        raise HTTPException(502, "a IA nao respondeu agora; tente de novo em instantes")
    return {"fila": ig.fila(), "reescritas": feitos, "falharam": len(alvos) - feitos}


@app.get("/api/ig/resultados")
def ig_resultados() -> dict:
    return ig.resultados()


@app.post("/api/ig/metricas")
def ig_metricas() -> dict:
    """Busca agora os numeros de todos os posts recentes."""
    r = ig.atualizar_metricas(forcar=True)
    return {**r, **ig.resultados()}


@app.post("/api/ig/conectar")
def ig_conectar(dados: dict) -> dict:
    try:
        return ig.conectar(dados.get("token", ""), dados.get("app_id", ""),
                           dados.get("app_secret", ""))
    except (ig.ErroInstagram, KeyError) as exc:
        raise _erro_ig(exc)
    except Exception as exc:  # noqa: BLE001 — rede etc.: mensagem, nao 500
        raise HTTPException(502, f"nao consegui falar com a Meta: {exc}")


@app.post("/api/ig/desconectar")
def ig_desconectar(dados: dict) -> dict:
    return ig.desconectar(str(dados.get("ig_id", "")))


@app.post("/api/ig/config")
def ig_config(dados: dict) -> dict:
    ig.salvar_config(dados)
    return ig.config_publica()


@app.get("/api/ig/limite")
def ig_limite(ig_id: str | None = None) -> dict:
    try:
        return ig.uso_do_limite(ig_id)
    except ig.ErroInstagram as exc:
        raise _erro_ig(exc)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"nao consegui falar com a Meta: {exc}")


@app.post("/api/ig/agendar")
def ig_agendar(dados: dict) -> dict:
    """Agenda os videos PRONTOS pedidos, com a legenda montada pelo modelo."""
    molde, cfg = mf.carregar_molde(), ig.config()
    alvos = [_video(n) for n in dados.get("videos") or []]
    prontos = [v for v in alvos if _pronto(v, molde)]
    if not prontos:
        raise HTTPException(400, "nenhum dos selecionados esta renderizado no formato atual "
                                 "(videos longos precisam ser renderizados de novo em partes)")
    distribuir = bool(dados.get("distribuir", cfg["distribuir"]))
    ja = {x["video"] for x in ig.fila() if x["status"] in ("pendente", "publicando")
          and (distribuir or x["ig_id"] == (dados.get("ig_id") or cfg["ativa"]))}
    itens = []
    for v in prontos:
        n = len(mf.saidas_de(v))
        gancho = mf.gancho_de(v, None, molde)
        itens += [{"video": v.name, "parte": k, "partes": n, "gancho": gancho,
                   "legenda": ig.montar_legenda(gancho, cfg, k, n)} for k in range(1, n + 1)]
    sem_ia = 0
    if cfg["legenda_ia"]:
        # so os que vao de fato entrar na fila: nao gasta IA com video ja agendado
        novos = [i for i in itens if i["video"] not in ja]
        textos = lia.gerar([{"video": mf.ENTRADA / i["video"], "gancho": i["gancho"],
                             "parte": i["parte"], "partes": i["partes"]} for i in novos],
                           cfg)
        for i, t in zip(novos, textos):
            if t:
                i["legenda"] = t
            else:
                sem_ia += 1
    try:
        antes = len(ig.fila())
        fila = ig.agendar(itens, dados.get("ig_id"), distribuir=distribuir)
    except ig.ErroInstagram as exc:
        raise _erro_ig(exc)
    return {"agendados": len(fila) - antes, "ignorados_sem_render": len(alvos) - len(prontos),
            "legendas_sem_ia": sem_ia, "fila": fila}


@app.post("/api/ig/reorganizar")
def ig_reorganizar() -> dict:
    return {"fila": ig.reorganizar()}


@app.post("/api/ig/remover-pendentes")
def ig_remover_pendentes() -> dict:
    return {"fila": ig.remover_pendentes()}


@app.post("/api/ig/remover")
def ig_remover(dados: dict) -> dict:
    return {"fila": ig.remover([str(i) for i in dados.get("ids") or []])}


@app.post("/api/ig/limpar-historico")
def ig_limpar_historico() -> dict:
    return {"fila": ig.limpar_historico()}


@app.post("/api/ig/agora")
def ig_agora(dados: dict) -> dict:
    """Antecipa um item para agora (o agendador pega em ate 30s). Bom para testar."""
    item_id = str(dados.get("id", ""))
    if not any(x["id"] == item_id and x["status"] in ("pendente", "erro") for x in ig.fila()):
        raise HTTPException(404, "item nao esta pendente")
    ig._atualizar(item_id, status="pendente", tentativas=0, erro=None,
                  quando=datetime.now().isoformat(timespec="minutes"))
    return {"fila": ig.fila()}


@app.post("/api/ig/legenda")
def ig_editar_legenda(dados: dict) -> dict:
    item_id = str(dados.get("id", ""))
    if not any(x["id"] == item_id and x["status"] == "pendente" for x in ig.fila()):
        raise HTTPException(409, "so da para editar post pendente")
    ig._atualizar(item_id, legenda=str(dados.get("legenda", ""))[:2200])
    return {"fila": ig.fila()}


# ---------------------------------------------------------------- limpeza

@app.post("/api/limpar")
def limpar(dados: dict) -> dict:
    """Limpeza de arquivos e da memoria do painel.

    alvo = "cache"     -> esvazia os quadros e duracoes guardados na RAM
           "renders"   -> apaga os videos prontos (saida) dos selecionados
           "originais" -> apaga original + gancho + corte + render dos selecionados
    Arquivos so com `videos` explicito ou `todos: true`; `somente_prontos: true`
    restringe aos que ja tem render (limpar a fila depois de postar).
    """
    alvo = dados.get("alvo")
    if alvo == "cache":
        n = len(_quadros) + len(_duracoes)
        _quadros.clear()
        _duracoes.clear()
        return {"limpos": n}
    if _status["rodando"]:
        raise HTTPException(409, "espere o render terminar para apagar arquivos")
    if not dados.get("videos") and not dados.get("todos"):
        raise HTTPException(400, "nenhum video selecionado")  # nunca apaga tudo por engano
    alvos = _selecao(dados.get("videos"))
    if dados.get("somente_prontos"):
        alvos = [v for v in alvos if mf.pronto(v)]
    fila = ig.fila()
    if any(x["status"] == "publicando" and x["video"] in {v.name for v in alvos} for x in fila):
        raise HTTPException(409, "um destes videos esta sendo publicado agora; espere 1 minuto")
    agendados = {x["video"] for x in fila if x["status"] in ("pendente", "erro")}
    presos = [v.name for v in alvos if v.name in agendados]
    if presos and not dados.get("tirar_da_fila"):
        # o navegador pergunta e manda de novo com tirar_da_fila
        raise HTTPException(409, {"agendados": len(presos), "posts": sum(
            1 for x in fila if x["video"] in presos and x["status"] in ("pendente", "erro"))})
    if presos:
        ig.remover([x["id"] for x in fila if x["video"] in presos
                    and x["status"] in ("pendente", "erro")])
        log.warning("limpeza tirou da fila do Instagram os posts de %d video(s)", len(presos))
    # apagar e irreversivel: fica registrado o que, quanto e por qual pedido
    log.warning("limpeza %s (%s): %d video(s): %s", alvo,
                "todos" + (" prontos" if dados.get("somente_prontos") else "")
                if dados.get("todos") else "selecionados",
                len(alvos), ", ".join(v.name for v in alvos[:20]))
    if alvo == "renders":
        return {"limpos": sum(1 for v in alvos if mf.apagar_saidas(v))}
    if alvo == "originais":
        for v in alvos:
            mf.apagar_saidas(v)
            for arq in (v, v.with_suffix(".txt"), mf._sidecar_corte(v), *lf.arquivos_de(v)):
                arq.unlink(missing_ok=True)
        _quadros.clear()
        _duracoes.clear()
        return {"limpos": len(alvos)}
    raise HTTPException(400, "alvo invalido")


def _limpar_orfaos() -> None:
    """Sobras de render/upload interrompido (queda de energia, processo morto)."""
    for pasta, padrao in ((mf.SAIDA, "*.parcial.mp4"), (mf.ENTRADA, "*.enviando"),
                          (mf.RAIZ, "*.baixando.zip")):
        if pasta.exists():
            for arq in pasta.glob(padrao):
                try:
                    arq.unlink(missing_ok=True)
                    log.info("sobra removida: %s", arq.name)
                except OSError as exc:  # ainda aberto por um ffmpeg orfao
                    log.warning("sobra em uso, fica para a proxima: %s (%s)", arq.name, exc)


def _configurar_log() -> None:
    """Log em arquivo: rodando escondido (pythonw) nao ha console onde o erro apareca."""
    mf.RAIZ.mkdir(parents=True, exist_ok=True)
    arquivo = RotatingFileHandler(mf.RAIZ / "painel.log", maxBytes=2_000_000,
                                  backupCount=2, encoding="utf-8")
    arquivo.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    raiz = logging.getLogger()
    raiz.setLevel(logging.INFO)
    raiz.addHandler(arquivo)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def _sem_janelas() -> None:
    """No pythonw, cada ffmpeg/ffprobe/whisper filho ganharia um console proprio
    (pisca uma janela preta por processo). CREATE_NO_WINDOW vira o padrao de todo
    subprocess do painel, inclusive os de molde/legenda/instagram."""
    if sys.platform != "win32":
        return

    class _PopenSemJanela(subprocess.Popen):
        def __init__(self, *args, **kwargs):
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)

    subprocess.Popen = _PopenSemJanela


if __name__ == "__main__":
    _configurar_log()
    _sem_janelas()
    if "--sem-navegador" not in sys.argv:
        threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{PORTA}")).start()
    log.info("painel subindo na porta %s", PORTA)
    _limpar_orfaos()
    # publica a fila do Instagram enquanto o painel estiver aberto
    threading.Thread(target=ig.agendador, args=(threading.Event(),), daemon=True,
                     name="agendador-instagram").start()
    # log_config=None: o uvicorn usa o log acima em vez de escrever num console
    # que, no pythonw, nao existe
    uvicorn.run(app, host="127.0.0.1", port=PORTA, log_config=None)
