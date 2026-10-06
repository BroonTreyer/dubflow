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
import subprocess
import sys
import threading
import webbrowser
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, UploadFile
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from PIL import Image, ImageDraw, ImageFont

from scripts import instagram_filmes as ig
from scripts import legenda_filmes as lf
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
                mf.saida_de(v).unlink(missing_ok=True)
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
    img = mf.previa(molde, gancho, _quadro(video), corte)
    buf = io.BytesIO()
    img.resize((540, 960)).save(buf, "JPEG", quality=88)
    return Response(buf.getvalue(), media_type="image/jpeg")


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
            "pronto": mf.saida_de(v).exists(),
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
    img = mf.previa(molde, mf.gancho_de(video, None, molde), _quadro(video), corte)
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
    mf.saida_de(video).unlink(missing_ok=True)
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
        mf.saida_de(destino).unlink(missing_ok=True)  # render do arquivo antigo de mesmo nome
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


@app.get("/saida/{nome}")
def baixar_saida(nome: str) -> FileResponse:
    alvo = mf.saida_de(_video(nome))
    if not alvo.exists():
        raise HTTPException(404, "ainda nao renderizado")
    return FileResponse(alvo, media_type="video/mp4")


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
        mf.saida_de(v).unlink(missing_ok=True)  # render antigo ficou desatualizado
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
            mf.saida_de(v).unlink(missing_ok=True)  # render antigo ficou desatualizado
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
        if not mf.tem_corte_proprio(v) and mf.saida_de(v).exists():
            mf.saida_de(v).unlink()
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
        mf.renderizar(v, mf.saida_de(v), molde, mf.gancho_de(v, i, molde), corte,
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
        fila = [(i, v) for i, v in enumerate(mf.listar_videos())
                if v.name in escolhidos and (refazer or not mf.saida_de(v).exists())]
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
    return {"config": ig.config_publica(), "fila": ig.fila()}


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
    prontos = [v for v in alvos if mf.saida_de(v).exists()]
    if not prontos:
        raise HTTPException(400, "nenhum dos selecionados esta renderizado")
    itens = [{"video": v.name,
              "legenda": ig.montar_legenda(mf.gancho_de(v, None, molde), cfg)} for v in prontos]
    try:
        antes = len(ig.fila())
        fila = ig.agendar(itens, dados.get("ig_id"))
    except ig.ErroInstagram as exc:
        raise _erro_ig(exc)
    return {"agendados": len(fila) - antes, "ignorados_sem_render": len(alvos) - len(prontos),
            "fila": fila}


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
        alvos = [v for v in alvos if mf.saida_de(v).exists()]
    agendados = {x["video"] for x in ig.fila() if x["status"] in ("pendente", "publicando")}
    presos = [v.name for v in alvos if v.name in agendados]
    if presos:
        raise HTTPException(409, f"{len(presos)} video(s) tem post agendado no Instagram; "
                                 "tire da fila de publicacao antes de apagar")
    # apagar e irreversivel: fica registrado o que, quanto e por qual pedido
    log.warning("limpeza %s (%s): %d video(s): %s", alvo,
                "todos" + (" prontos" if dados.get("somente_prontos") else "")
                if dados.get("todos") else "selecionados",
                len(alvos), ", ".join(v.name for v in alvos[:20]))
    if alvo == "renders":
        n = 0
        for v in alvos:
            if mf.saida_de(v).exists():
                mf.saida_de(v).unlink()
                n += 1
        return {"limpos": n}
    if alvo == "originais":
        for v in alvos:
            for arq in (v, v.with_suffix(".txt"), mf._sidecar_corte(v), mf.saida_de(v),
                        *lf.arquivos_de(v)):
                arq.unlink(missing_ok=True)
        _quadros.clear()
        _duracoes.clear()
        return {"limpos": len(alvos)}
    raise HTTPException(400, "alvo invalido")


def _limpar_orfaos() -> None:
    """Sobras de render/upload interrompido (queda de energia, processo morto)."""
    for pasta, padrao in ((mf.SAIDA, "*.parcial.mp4"), (mf.ENTRADA, "*.enviando")):
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


if __name__ == "__main__":
    _configurar_log()
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
