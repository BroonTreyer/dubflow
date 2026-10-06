"""Publicacao dos cortes de filme no Instagram (Reels), com fila e horarios.

Diferente de app/publishers/instagram.py, que manda a Meta BAIXAR o video de uma
URL publica, aqui o arquivo SOBE direto do PC (upload "resumable" da Graph API):
nao precisa de servidor exposto, tunel nem bucket.

    1. POST /{ig-user}/media  media_type=REELS upload_type=resumable  -> {id, uri}
    2. POST {uri}  (bytes do video, headers offset/file_size)
    3. GET  /{container}?fields=status_code  ate FINISHED
    4. POST /{ig-user}/media_publish  creation_id=...                 -> media id

Conexao: o usuario cola um token de usuario da Graph API; daqui saem as Paginas e
a conta do Instagram ligada a cada uma. Guardamos o token DA PAGINA: derivado de
um token de usuario de longa duracao, ele nao expira. Com app id + secret o token
curto e trocado pelo longo antes.

Tudo em filmes/instagram.json e filmes/fila_instagram.json (pasta no .gitignore).
Token nunca vai para log nem para a resposta da API do painel.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

from scripts import molde_filmes as mf

log = logging.getLogger("instagram_filmes")

GRAPH = "https://graph.facebook.com/v23.0"
TENTATIVAS = 3
DURACAO_MAX = 90.0   # segundos; o Reels por API recusa acima disso
ESPERA_REPETIR = timedelta(minutes=10)

_trava = threading.RLock()   # config e fila sao lidas/escritas pelo painel e pelo agendador


class ErroInstagram(Exception):
    """Erro da Graph API ja traduzido. `fatal` = nao adianta repetir (token, permissao)."""

    def __init__(self, mensagem: str, fatal: bool = False, adiar: timedelta | None = None):
        super().__init__(mensagem)
        self.fatal = fatal
        self.adiar = adiar  # espera conhecida (limite diario): adia sem gastar tentativa


# ------------------------------------------------------------------ arquivos

def _arq_config() -> Path:
    return mf.RAIZ / "instagram.json"


def _arq_fila() -> Path:
    return mf.RAIZ / "fila_instagram.json"


CONFIG_PADRAO = {
    "contas": [],            # [{"ig_id", "username", "pagina", "token"}]
    "ativa": None,           # ig_id da conta padrao
    "legenda_modelo": "{gancho} 🎬 {parte}\n\nSegue pra ver mais cenas!",
    "hashtags": "#filmes #cinema #series #netflix #filme",
    "por_dia": 5,
    "inicio": "09:00",
    "intervalo_min": 180,
}


def _ler(arq: Path, padrao):
    with _trava:
        if not arq.exists():
            return json.loads(json.dumps(padrao))
        try:
            return json.loads(arq.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.error("%s corrompido; usando o padrao", arq.name)
            return json.loads(json.dumps(padrao))


def _gravar(arq: Path, dados) -> None:
    with _trava:
        arq.parent.mkdir(parents=True, exist_ok=True)
        tmp = arq.with_suffix(".tmp")
        tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(arq)  # nunca deixa a fila pela metade se o PC desligar no meio


def config() -> dict:
    return {**CONFIG_PADRAO, **_ler(_arq_config(), CONFIG_PADRAO)}


def salvar_config(novo: dict) -> dict:
    """Grava o que e editavel no painel. Contas/tokens so mudam por conectar()."""
    with _trava:
        atual = config()
        for k in ("legenda_modelo", "hashtags", "inicio", "ativa"):
            if k in novo:
                atual[k] = str(novo[k]) if novo[k] is not None else None
        for k, (lo, hi) in (("por_dia", (1, 50)), ("intervalo_min", (5, 1440))):
            if k in novo:
                try:
                    atual[k] = min(max(int(novo[k]), lo), hi)
                except (TypeError, ValueError):
                    pass
        try:
            datetime.strptime(atual["inicio"], "%H:%M")
        except ValueError:
            atual["inicio"] = CONFIG_PADRAO["inicio"]
        if atual["ativa"] not in {c["ig_id"] for c in atual["contas"]}:
            atual["ativa"] = atual["contas"][0]["ig_id"] if atual["contas"] else None
        _gravar(_arq_config(), atual)
        return atual


def config_publica() -> dict:
    """Config sem tokens, para mandar ao navegador."""
    c = config()
    return {**c, "contas": [{k: v for k, v in conta.items() if k != "token"}
                            for conta in c["contas"]]}


def _conta(ig_id: str | None) -> dict:
    c = config()
    ig_id = ig_id or c["ativa"]
    for conta in c["contas"]:
        if conta["ig_id"] == ig_id:
            return conta
    raise ErroInstagram("nenhuma conta do Instagram conectada", fatal=True)


# ------------------------------------------------------------------ Graph API

def _explicar(corpo: dict) -> ErroInstagram:
    depuracao = corpo.get("debug_info") or {}
    if depuracao.get("type") == "ProcessingFailedError":
        # resposta do rupload, sem motivo. O que a provoca na pratica e video longo:
        # a Meta processa durante o envio e desiste antes de terminar.
        return ErroInstagram(
            "A Meta nao terminou de processar o video no envio direto (acontece com "
            "parte longa). Diminua 'Duracao maxima de cada parte' no molde (60s funciona), "
            "renderize de novo e agende de novo.", fatal=True)
    erro = corpo.get("error") or {}
    codigo, msg = erro.get("code"), erro.get("message", "") or str(corpo)
    sub = erro.get("error_subcode")
    if codigo in (190, 102) or "session" in msg.lower():
        return ErroInstagram("Token expirado ou invalido: conecte o Instagram de novo.", fatal=True)
    if codigo in (10, 200) or "permission" in msg.lower():
        return ErroInstagram("Token sem permissao de publicar (instagram_content_publish) "
                             "ou a conta nao e Profissional ligada a uma Pagina.", fatal=True)
    if codigo == 9 or sub == 2207042 or "limit" in msg.lower():
        return ErroInstagram("Limite de publicacoes da conta atingido (24h); adiado 1h.",
                             adiar=timedelta(hours=1))
    if codigo in (4, 17, 32, 613):
        return ErroInstagram("Muitas chamadas a API agora; adiado 15 min.",
                             adiar=timedelta(minutes=15))
    return ErroInstagram(f"Instagram recusou: {msg}")


def _json(resp: requests.Response) -> dict:
    try:
        corpo = resp.json()
    except ValueError:
        corpo = {"error": {"message": f"HTTP {resp.status_code}: {resp.text[:200]}"}}
    if resp.status_code >= 400 or "error" in corpo:
        raise _explicar(corpo)
    return corpo


def conectar(token_usuario: str, app_id: str = "", app_secret: str = "") -> dict:
    """Descobre as contas do Instagram ligadas as Paginas do token e guarda todas."""
    token_usuario = (token_usuario or "").strip()
    if not token_usuario:
        raise ErroInstagram("cole o token", fatal=True)
    if app_id and app_secret:
        # token curto (1-2h) -> longo (60 dias); token de Pagina derivado do longo nao expira
        token_usuario = _json(requests.get(f"{GRAPH}/oauth/access_token", params={
            "grant_type": "fb_exchange_token", "client_id": app_id.strip(),
            "client_secret": app_secret.strip(), "fb_exchange_token": token_usuario,
        }, timeout=30))["access_token"]

    paginas = _json(requests.get(f"{GRAPH}/me/accounts", params={
        "fields": "name,access_token,instagram_business_account{id,username}",
        "limit": 100, "access_token": token_usuario}, timeout=30)).get("data", [])
    contas = [{"ig_id": p["instagram_business_account"]["id"],
               "username": p["instagram_business_account"].get("username", ""),
               "pagina": p.get("name", ""), "token": p["access_token"]}
              for p in paginas if p.get("instagram_business_account")]
    if not contas:
        raise ErroInstagram(_diagnostico(token_usuario, paginas), fatal=True)
    with _trava:
        c = config()
        por_id = {x["ig_id"]: x for x in c["contas"]}
        por_id.update({x["ig_id"]: x for x in contas})
        c["contas"] = list(por_id.values())
        c["ativa"] = c["ativa"] or contas[0]["ig_id"]
        _gravar(_arq_config(), c)
    log.info("instagram conectado: %s", ", ".join("@" + x["username"] for x in contas))
    return config_publica()


PERMISSOES = ("instagram_basic", "instagram_content_publish", "pages_show_list",
              "pages_read_engagement", "business_management")


def _diagnostico(token_usuario: str, paginas: list[dict]) -> str:
    """Por que nenhuma conta apareceu: aponta a causa em vez de listar as quatro."""
    nomes = ", ".join(f'"{p.get("name", "?")}"' for p in paginas) or "nenhuma"
    try:
        concedidas = {x["permission"] for x in _json(requests.get(
            f"{GRAPH}/me/permissions", params={"access_token": token_usuario}, timeout=30)
        ).get("data", []) if x.get("status") == "granted"}
        faltando = [p for p in PERMISSOES if p not in concedidas]
    except ErroInstagram:
        faltando = None
    if not paginas:
        return ("O token nao ve nenhuma Pagina do Facebook. Gere de novo marcando a Pagina "
                "na janela de autorizacao (permissao pages_show_list).")
    if faltando:
        return (f"Paginas vistas: {nomes}. Faltam permissoes no token: {', '.join(faltando)}. "
                "Adicione no Graph API Explorer ANTES de gerar o token e gere de novo.")
    return (f"Paginas vistas: {nomes}, mas nenhuma devolveu conta do Instagram. Permissoes OK, "
            "entao e uma destas: (1) na janela de autorizacao a conta do Instagram nao foi "
            "marcada - gere o token de novo e marque a Pagina E o Instagram; (2) o Instagram "
            "nao e conta Profissional; (3) o Instagram esta ligado a outra Pagina.")


def desconectar(ig_id: str) -> dict:
    with _trava:
        c = config()
        c["contas"] = [x for x in c["contas"] if x["ig_id"] != ig_id]
        if c["ativa"] == ig_id:
            c["ativa"] = c["contas"][0]["ig_id"] if c["contas"] else None
        _gravar(_arq_config(), c)
    return config_publica()


def uso_do_limite(ig_id: str | None = None) -> dict:
    conta = _conta(ig_id)
    dados = _json(requests.get(f"{GRAPH}/{conta['ig_id']}/content_publishing_limit", params={
        "fields": "quota_usage,config", "access_token": conta["token"]}, timeout=30))
    item = (dados.get("data") or [{}])[0]
    return {"usado": item.get("quota_usage", 0),
            "limite": (item.get("config") or {}).get("quota_total", 50)}


def publicar(video: Path, legenda: str, ig_id: str | None = None,
             espera_max: int = 900) -> dict:
    """Sobe o arquivo e publica como Reels. Devolve {"media_id", "permalink"}."""
    conta = _conta(ig_id)
    token = conta["token"]
    # medido nesta conta em 05/10/2026: 90,0s sobe, 91s volta "ProcessingFailedError"
    # do rupload, sem dizer o motivo. Checa antes de gastar o upload.
    duracao = mf.duracao(video)
    if duracao > DURACAO_MAX:
        raise ErroInstagram(f"o video tem {duracao:.0f}s e o Instagram aceita no maximo "
                            f"{DURACAO_MAX:.0f}s por API: corte o fim (Fim no corte) e renderize "
                            "de novo", fatal=True)
    criado = _json(requests.post(f"{GRAPH}/{conta['ig_id']}/media", data={
        "media_type": "REELS", "upload_type": "resumable", "caption": legenda[:2200],
        "share_to_feed": "true", "access_token": token}, timeout=60))
    container, uri = criado["id"], criado.get("uri")
    if not uri:
        raise ErroInstagram("a API nao devolveu o endereco de upload")

    tamanho = video.stat().st_size
    with open(video, "rb") as f:
        enviado = _json(requests.post(uri, data=f, timeout=600, headers={
            "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(tamanho),
            "Content-Type": "application/octet-stream"}))
    if not enviado.get("success", True):
        raise ErroInstagram(f"upload recusado: {enviado}")

    limite = time.time() + espera_max
    status = "IN_PROGRESS"
    while time.time() < limite:
        status = _json(requests.get(f"{GRAPH}/{container}", params={
            "fields": "status_code,status", "access_token": token}, timeout=30)
        ).get("status_code", "UNKNOWN")
        if status == "FINISHED":
            break
        if status in ("ERROR", "EXPIRED"):
            raise ErroInstagram(f"o Instagram nao conseguiu processar o video ({status})")
        time.sleep(8)
    else:
        raise ErroInstagram(f"processamento demorou demais ({status})")

    media_id = _json(requests.post(f"{GRAPH}/{conta['ig_id']}/media_publish", data={
        "creation_id": container, "access_token": token}, timeout=60))["id"]
    try:
        permalink = _json(requests.get(f"{GRAPH}/{media_id}", params={
            "fields": "permalink", "access_token": token}, timeout=30)).get("permalink")
    except ErroInstagram:
        permalink = None
    log.info("publicado em @%s: %s", conta["username"], permalink or media_id)
    return {"media_id": media_id, "permalink": permalink}


# ------------------------------------------------------------------ fila

def fila() -> list[dict]:
    return _ler(_arq_fila(), [])


def montar_legenda(gancho: str, cfg: dict | None = None, parte: int = 1, total: int = 1) -> str:
    """Modelo com {gancho} e, opcional, {parte} ("Parte 2/3"). Sem {parte} no modelo
    e com mais de uma parte, o "Parte k/n" entra sozinho no fim do texto."""
    cfg = cfg or config()
    rotulo = f"Parte {parte}/{total}" if total > 1 else ""
    modelo = cfg["legenda_modelo"]
    texto = modelo.replace("{gancho}", gancho.strip()).replace("{parte}", rotulo)
    if rotulo and "{parte}" not in modelo:
        texto = f"{texto.rstrip()}\n\n{rotulo}"
    texto = "\n".join(l.rstrip() for l in texto.splitlines()).strip()
    return f"{texto}\n\n{cfg['hashtags']}".strip() if cfg["hashtags"].strip() else texto


def horarios(quantidade: int, cfg: dict, depois_de: datetime | None = None,
             ocupados: list[datetime] | None = None) -> list[datetime]:
    """Proximos horarios livres: `por_dia` a cada janela de 24h que comeca em `inicio`,
    um a cada `intervalo_min`, pulando os que ja passaram e os ja ocupados.

    A janela e de 24h e nao o dia do calendario: com inicio 22:35 e intervalo de 3h
    os horarios 01:35, 04:35... sao do mesmo "dia" de postagem. (Cortar na
    meia-noite deixava so 1 post por dia em quem posta a noite.)
    """
    agora = depois_de or datetime.now()
    h, m = map(int, cfg["inicio"].split(":"))
    ocupados_set = {o.replace(second=0, microsecond=0) for o in (ocupados or [])}
    saida, dia = [], agora.replace(hour=h, minute=m, second=0, microsecond=0)
    while len(saida) < quantidade:
        for i in range(cfg["por_dia"]):
            t = dia + timedelta(minutes=i * cfg["intervalo_min"])
            if t >= dia + timedelta(days=1):
                break  # intervalo grande demais: o resto invadiria a janela seguinte
            if t > agora and t not in ocupados_set:
                saida.append(t)
                if len(saida) == quantidade:
                    break
        dia += timedelta(days=1)
    return saida


def agendar(itens: list[dict], ig_id: str | None = None) -> list[dict]:
    """itens: [{"video", "legenda", "parte"?, "partes"?}] -> entram na fila, partes
    do mesmo video em horarios seguidos e na ordem."""
    with _trava:
        cfg, atual = config(), fila()
        conta = _conta(ig_id)
        ja = {(x["video"], x.get("parte", 1), x["ig_id"]) for x in atual
              if x["status"] in ("pendente", "publicando")}
        novos = [i for i in itens if (i["video"], i.get("parte", 1), conta["ig_id"]) not in ja]
        ocupados = [datetime.fromisoformat(x["quando"]) for x in atual
                    if x["ig_id"] == conta["ig_id"] and x["status"] in ("pendente", "publicando")]
        for item, quando in zip(novos, horarios(len(novos), cfg, ocupados=ocupados)):
            atual.append({"id": f"{int(time.time() * 1000)}-{len(atual)}",
                          "video": item["video"], "legenda": item["legenda"],
                          "parte": item.get("parte", 1), "partes": item.get("partes", 1),
                          "ig_id": conta["ig_id"], "username": conta["username"],
                          "quando": quando.isoformat(timespec="minutes"),
                          "status": "pendente", "tentativas": 0,
                          "erro": None, "permalink": None})
        _gravar(_arq_fila(), atual)
        return atual


def reorganizar(ig_id: str | None = None) -> list[dict]:
    """Redistribui os horarios dos pendentes pela config atual, mantendo a ordem.

    Para quando a config mudou (ou a fila foi montada com regra errada). Partes do
    mesmo video continuam em sequencia porque a ordem e preservada.
    """
    with _trava:
        cfg, atual = config(), fila()
        contas = {ig_id} if ig_id else {x["ig_id"] for x in atual}
        for conta in contas:
            pend = sorted((x for x in atual if x["ig_id"] == conta and x["status"] == "pendente"),
                          key=lambda x: (x["quando"], x["video"], x.get("parte", 1)))
            ocupados = [datetime.fromisoformat(x["quando"]) for x in atual
                        if x["ig_id"] == conta and x["status"] == "publicando"]
            for item, quando in zip(pend, horarios(len(pend), cfg, ocupados=ocupados)):
                item["quando"] = quando.isoformat(timespec="minutes")
        _gravar(_arq_fila(), atual)
        return atual


def remover(ids: list[str]) -> list[dict]:
    """Tira da fila o que ainda nao foi publicado (publicados ficam como historico)."""
    with _trava:
        atual = [x for x in fila() if not (x["id"] in ids and x["status"] != "publicando")]
        _gravar(_arq_fila(), atual)
        return atual


def remover_pendentes() -> list[dict]:
    """Esvazia a fila do que ainda nao saiu (pendentes e com erro); publicados ficam."""
    with _trava:
        atual = [x for x in fila() if x["status"] in ("publicado", "publicando")]
        _gravar(_arq_fila(), atual)
        return atual


def limpar_historico() -> list[dict]:
    with _trava:
        atual = [x for x in fila() if x["status"] not in ("publicado",)]
        _gravar(_arq_fila(), atual)
        return atual


def _atualizar(item_id: str, **campos) -> None:
    with _trava:
        atual = fila()
        for x in atual:
            if x["id"] == item_id:
                x.update(campos)
        _gravar(_arq_fila(), atual)


def processar_vencidos(agora: datetime | None = None) -> int:
    """Publica UM item vencido por chamada (o agendador chama em loop).

    Um por vez de proposito: maquina que ficou desligada nao despeja a fila
    atrasada inteira no mesmo minuto.
    """
    agora = agora or datetime.now()
    with _trava:
        atual = fila()

        def anterior_pendente(x):
            # parte 2 nunca sai antes da 1 (ex.: parte 1 adiada por limite diario)
            return any(y["video"] == x["video"] and y["ig_id"] == x["ig_id"]
                       and y.get("parte", 1) < x.get("parte", 1)
                       and y["status"] in ("pendente", "publicando", "erro") for y in atual)

        vencidos = sorted((x for x in atual if x["status"] == "pendente"
                           and datetime.fromisoformat(x["quando"]) <= agora
                           and not anterior_pendente(x)),
                          key=lambda x: x["quando"])
        if not vencidos:
            return 0
        item = vencidos[0]
        _atualizar(item["id"], status="publicando")

    saidas = mf.saidas_de(mf.ENTRADA / item["video"])
    k = item.get("parte", 1)
    video = saidas[k - 1] if len(saidas) >= k else None
    try:
        if video is not None and len(saidas) != item.get("partes", 1):
            raise ErroInstagram(
                f"o video foi renderizado de novo em {len(saidas)} parte(s) e este post era "
                f"a parte {k} de {item.get('partes', 1)}: tire da fila e agende de novo",
                fatal=True)
        if video is None or not video.exists():
            raise ErroInstagram("o video renderizado nao existe mais (apagado ou renderizado "
                                "de novo com outro numero de partes): agende de novo", fatal=True)
        r = publicar(video, item["legenda"], item["ig_id"])
        _atualizar(item["id"], status="publicado", permalink=r["permalink"], erro=None,
                   publicado_em=datetime.now().isoformat(timespec="minutes"))
    except (ErroInstagram, requests.RequestException) as exc:
        fatal = getattr(exc, "fatal", False)
        adiar = getattr(exc, "adiar", None)
        if adiar and not fatal:  # espera conhecida: nao e falha do post
            _atualizar(item["id"], status="pendente", erro=str(exc),
                       quando=(agora + adiar).isoformat(timespec="minutes"))
            log.warning("publicacao adiada (%s): %s", item["video"], exc)
            return 1
        tentativas = item["tentativas"] + 1
        if fatal or tentativas >= TENTATIVAS:
            _atualizar(item["id"], status="erro", erro=str(exc), tentativas=tentativas)
        else:  # adia em vez de perder o post
            _atualizar(item["id"], status="pendente", erro=str(exc), tentativas=tentativas,
                       quando=(agora + ESPERA_REPETIR).isoformat(timespec="minutes"))
        log.warning("publicacao falhou (%s, tentativa %d): %s", item["video"], tentativas, exc)
    return 1


def recuperar_presos() -> None:
    """'publicando' no boot = o painel caiu no meio da publicacao. Vira ERRO, nao
    volta pra fila: o Instagram pode ter publicado, e repetir duplicaria o post."""
    with _trava:
        atual = fila()
        for x in atual:
            if x["status"] == "publicando":
                x.update(status="erro", erro="painel caiu durante a publicacao: confira no "
                         "Instagram antes de tentar de novo")
        _gravar(_arq_fila(), atual)


def agendador(parar: threading.Event, intervalo: int = 30) -> None:
    recuperar_presos()
    while not parar.is_set():
        try:
            while processar_vencidos():
                if parar.is_set():
                    break
        except Exception:  # noqa: BLE001 — o agendador nao pode morrer
            log.exception("agendador do instagram")
        parar.wait(intervalo)
