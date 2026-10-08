"""Publicacao dos cortes de filme no Instagram (Reels), com fila e horarios.

Diferente de app/publishers/instagram.py, que manda a Meta BAIXAR o video de uma
URL publica, aqui o arquivo SOBE direto do PC (upload "resumable" da Graph API):
nao precisa de servidor exposto, tunel nem bucket.

    1. POST /{ig-user}/media  media_type=REELS upload_type=resumable  -> {id, uri}
    2. POST {uri}  (bytes do video, headers offset/file_size)
    3. GET  /{container}?fields=status_code  ate FINISHED
    4. POST /{ig-user}/media_publish  creation_id=...                 -> media id
    5. (opcional) POST /{media}/comments  primeiro comentario

Conexao: o usuario cola um token de usuario da Graph API; daqui saem as Paginas e
a conta do Instagram ligada a cada uma. Guardamos o token DA PAGINA: derivado de
um token de usuario de longa duracao, ele nao expira. Com app id + secret o token
curto e trocado pelo longo antes; os dois ficam guardados para a proxima conexao.

Tudo em filmes/instagram.json e filmes/fila_instagram.json (pasta no .gitignore).
Token e chave do app nunca vao para log nem para a resposta da API do painel.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import requests

from scripts import link_publico
from scripts import molde_filmes as mf

log = logging.getLogger("instagram_filmes")

GRAPH = "https://graph.facebook.com/v23.0"
TENTATIVAS = 3
DURACAO_MAX = 900.0                  # 15 min: teto do Reels por API (doc da Meta, 10/2026)
TAMANHO_MAX = 300 * 1024 * 1024      # 300 MB, idem
# acima disso o envio direto nao passa (a Meta desiste de processar em ~34s, medido
# 07/10/2026): vai por link temporario (scripts/link_publico.py)
LIMITE_DIRETO = 62.0
ESPERA_REPETIR = timedelta(minutes=10)
METRICAS = ("views", "reach", "likes", "comments", "shares", "saved", "total_interactions")
METRICAS_CADA = timedelta(hours=6)
METRICAS_DIAS = 30                   # numeros de post mais velho quase nao mudam
AVISO_TOKEN_DIAS = 3
NOTIFICAR_CADA = timedelta(hours=12)

_trava = threading.RLock()   # config e fila sao lidas/escritas pelo painel e pelo agendador


class ErroInstagram(Exception):
    """Erro da Graph API ja traduzido. `fatal` = nao adianta repetir (token, permissao).

    `processamento` = a Meta recebeu o arquivo mas nao conseguiu processar: vale uma
    segunda tentativa com uma versao mais leve do mesmo video.
    """

    def __init__(self, mensagem: str, fatal: bool = False, adiar: timedelta | None = None,
                 processamento: bool = False, codigo: int | None = None):
        super().__init__(mensagem)
        self.fatal = fatal
        self.adiar = adiar  # espera conhecida (limite diario): adia sem gastar tentativa
        self.processamento = processamento
        self.codigo = codigo


# ------------------------------------------------------------------ arquivos

def _arq_config() -> Path:
    return mf.RAIZ / "instagram.json"


def _arq_fila() -> Path:
    return mf.RAIZ / "fila_instagram.json"


CONFIG_PADRAO = {
    "contas": [],            # [{"ig_id", "username", "pagina", "token", "expira", "acesso_expira"}]
    "ativa": None,           # ig_id da conta padrao
    "app_id": "",            # guardados para a proxima conexao trocar o token sozinha
    "app_secret": "",
    "legenda_modelo": "{gancho} 🎬 {parte}\n\nSegue pra ver mais cenas!",
    "hashtags": "#filmes #cinema #series #netflix #filme",
    "legenda_ia": False,     # legenda de cada post escrita pela IA (cai no modelo se falhar)
    "comentario": "",        # primeiro comentario automatico; vazio = nenhum
    "distribuir": False,     # agendar repartindo os videos entre todas as contas
    "por_dia": 5,
    "inicio": "09:00",
    "intervalo_min": 180,
    "notificados": {},       # aviso -> quando o Windows foi avisado (nao repete toda hora)
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
        for k in ("legenda_modelo", "hashtags", "inicio", "ativa", "comentario"):
            if k in novo:
                atual[k] = str(novo[k]) if novo[k] is not None else None
        for k in ("legenda_ia", "distribuir"):
            if k in novo:
                atual[k] = bool(novo[k])
        for k, (lo, hi) in (("por_dia", (1, 50)), ("intervalo_min", (5, 1440))):
            if k in novo:
                try:
                    atual[k] = min(max(int(novo[k]), lo), hi)
                except (TypeError, ValueError):
                    pass
        atual["comentario"] = (atual["comentario"] or "")[:2200]
        try:
            datetime.strptime(atual["inicio"], "%H:%M")
        except ValueError:
            atual["inicio"] = CONFIG_PADRAO["inicio"]
        if atual["ativa"] not in {c["ig_id"] for c in atual["contas"]}:
            atual["ativa"] = atual["contas"][0]["ig_id"] if atual["contas"] else None
        _gravar(_arq_config(), atual)
        return atual


def config_publica() -> dict:
    """Config sem tokens nem chave do app, para mandar ao navegador."""
    c = config()
    publica = {k: v for k, v in c.items() if k not in ("app_secret", "notificados")}
    publica["app_salvo"] = bool(c["app_id"] and c["app_secret"])
    publica["contas"] = [{k: v for k, v in conta.items() if k != "token"}
                         for conta in c["contas"]]
    return publica


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
        # resposta do rupload, sem motivo. Medido em 07/10/2026 (teste_envio_ig): o
        # rupload processa o video DURANTE a requisicao e desiste por volta de 34s;
        # o tempo depende da DURACAO, nao do tamanho (77s com 5 MB tambem cai). 60s
        # passa. Arquivo mais leve nao resolve: so parte mais curta.
        return ErroInstagram(
            "A Meta nao processa video acima de ~60s no envio direto (a duracao conta, "
            "nao o tamanho). Sem o envio por link (ferramentas/cloudflared.exe), diminua "
            "'Duracao maxima de cada parte' no molde para 60s e renderize de novo.",
            fatal=True, processamento=True)
    erro = corpo.get("error") or {}
    codigo, msg = erro.get("code"), erro.get("message", "") or str(corpo)
    sub = erro.get("error_subcode")
    if codigo in (190, 102) or "session" in msg.lower():
        return ErroInstagram("Token expirado ou invalido: conecte o Instagram de novo.",
                             fatal=True, codigo=190)
    if codigo in (10, 200) or "permission" in msg.lower():
        return ErroInstagram("Token sem permissao de publicar (instagram_content_publish) "
                             "ou a conta nao e Profissional ligada a uma Pagina.",
                             fatal=True, codigo=10)
    if codigo == 9 or sub == 2207042 or "limit" in msg.lower():
        return ErroInstagram("Limite de publicacoes da conta atingido (24h); adiado 1h.",
                             adiar=timedelta(hours=1))
    if codigo in (4, 17, 32, 613):
        return ErroInstagram("Muitas chamadas a API agora; adiado 15 min.",
                             adiar=timedelta(minutes=15))
    return ErroInstagram(f"Instagram recusou: {msg}", codigo=codigo)


def _json(resp: requests.Response) -> dict:
    try:
        corpo = resp.json()
    except ValueError:
        corpo = {"error": {"message": f"HTTP {resp.status_code}: {resp.text[:200]}"}}
    if resp.status_code >= 400 or "error" in corpo:
        raise _explicar(corpo)
    return corpo


def conectar(token_usuario: str, app_id: str = "", app_secret: str = "") -> dict:
    """Descobre as contas do Instagram ligadas as Paginas do token e guarda todas.

    Sem app id/secret no pedido, usa os guardados da ultima conexao. Devolve a
    config publica com `aviso` quando o token que ficou ainda vai vencer logo.
    """
    token_usuario = (token_usuario or "").strip()
    if not token_usuario:
        raise ErroInstagram("cole o token", fatal=True)
    salvo = config()
    app_id = (app_id or "").strip() or salvo["app_id"]
    app_secret = (app_secret or "").strip() or salvo["app_secret"]
    if app_id and app_secret:
        # token curto (1-2h) -> longo (60 dias); token de Pagina derivado do longo nao expira
        token_usuario = _json(requests.get(f"{GRAPH}/oauth/access_token", params={
            "grant_type": "fb_exchange_token", "client_id": app_id,
            "client_secret": app_secret, "fb_exchange_token": token_usuario,
        }, timeout=30))["access_token"]

    paginas = _json(requests.get(f"{GRAPH}/me/accounts", params={
        "fields": "name,access_token,instagram_business_account{id,username}",
        "limit": 100, "access_token": token_usuario}, timeout=30)).get("data", [])
    contas = [{"ig_id": p["instagram_business_account"]["id"],
               "username": p["instagram_business_account"].get("username", ""),
               "pagina": p.get("name", ""), "token": p["access_token"],
               "conectado_em": datetime.now().isoformat(timespec="seconds"),
               **_validade(p["access_token"])}
              for p in paginas if p.get("instagram_business_account")]
    if not contas:
        raise ErroInstagram(_diagnostico(token_usuario, paginas), fatal=True)
    with _trava:
        c = config()
        por_id = {x["ig_id"]: x for x in c["contas"]}
        por_id.update({x["ig_id"]: x for x in contas})
        c["contas"] = list(por_id.values())
        c["ativa"] = c["ativa"] or contas[0]["ig_id"]
        if app_id and app_secret:  # so guarda o par que acabou de funcionar
            c["app_id"], c["app_secret"] = app_id, app_secret
        for x in contas:  # conta reconectada: avisos de token velho valem de novo
            c["notificados"] = {k: v for k, v in (c.get("notificados") or {}).items()
                                if x["ig_id"] not in k}
        _gravar(_arq_config(), c)
    log.info("instagram conectado: %s", ", ".join("@" + x["username"] for x in contas))
    publica = config_publica()
    vence = [x for x in contas if x["expira"] and x["expira"] - time.time() < 7 * 86400]
    if vence:
        publica["aviso"] = (
            "Conectado, mas o token vence em poucas horas. Para nao precisar reconectar, "
            "preencha o ID e a Chave secreta do app e conecte de novo." if not app_secret else
            "Conectado, mas a Meta devolveu um token que vence em menos de 7 dias.")
    return publica


def _validade(token: str) -> dict:
    """expira: epoch em que o token vence (0 = nao vence; None = nao deu para saber).
    acesso_expira: quando a Meta corta o acesso aos dados (reautorizar renova)."""
    try:
        dados = _json(requests.get(f"{GRAPH}/debug_token", params={
            "input_token": token, "access_token": token}, timeout=30)).get("data", {})
        return {"expira": int(dados.get("expires_at") or 0),
                "acesso_expira": int(dados.get("data_access_expires_at") or 0) or None}
    except (ErroInstagram, requests.RequestException, ValueError, TypeError):
        return {"expira": None, "acesso_expira": None}


PERMISSOES = ("instagram_basic", "instagram_content_publish", "pages_show_list",
              "pages_read_engagement", "business_management")
# nao impedem de publicar: sem elas so falta o recurso correspondente
PERMISSOES_EXTRAS = ("instagram_manage_insights", "instagram_manage_comments")


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


def esquecer_app() -> dict:
    with _trava:
        c = config()
        c["app_id"], c["app_secret"] = "", ""
        _gravar(_arq_config(), c)
    return config_publica()


def uso_do_limite(ig_id: str | None = None) -> dict:
    conta = _conta(ig_id)
    dados = _json(requests.get(f"{GRAPH}/{conta['ig_id']}/content_publishing_limit", params={
        "fields": "quota_usage,config", "access_token": conta["token"]}, timeout=30))
    item = (dados.get("data") or [{}])[0]
    return {"usado": item.get("quota_usage", 0),
            "limite": (item.get("config") or {}).get("quota_total", 50)}


def _subir(conta: dict, video: Path, legenda: str) -> str:
    """Cria o container e sobe o arquivo. Devolve o id do container."""
    token = conta["token"]
    criado = _json(requests.post(f"{GRAPH}/{conta['ig_id']}/media", data={
        "media_type": "REELS", "upload_type": "resumable", "caption": legenda[:2200],
        "share_to_feed": "true", "access_token": token}, timeout=60))
    container, uri = criado["id"], criado.get("uri")
    if not uri:
        raise ErroInstagram("a API nao devolveu o endereco de upload")
    tamanho = video.stat().st_size
    with open(video, "rb") as f:
        enviado = _json(requests.post(uri, data=f, timeout=900, headers={
            "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(tamanho),
            "Content-Type": "application/octet-stream"}))
    if not enviado.get("success", True):
        raise ErroInstagram(f"upload recusado: {enviado}")
    return container


def _esperar(container: str, token: str, espera_max: int) -> None:
    limite = time.time() + espera_max
    status = "IN_PROGRESS"
    while time.time() < limite:
        dados = _json(requests.get(f"{GRAPH}/{container}", params={
            "fields": "status_code,status", "access_token": token}, timeout=30))
        status = dados.get("status_code", "UNKNOWN")
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            motivo = f": {dados['status']}" if dados.get("status") else ""
            raise ErroInstagram(f"o Instagram nao conseguiu processar o video ({status}{motivo})")
        time.sleep(8)
    raise ErroInstagram(f"processamento demorou demais ({status})")


def _subir_por_link(conta: dict, video: Path, legenda: str, espera_max: int) -> str:
    """A Meta BAIXA o video de um link temporario e processa com calma (ate 15 min).
    O link so existe ate o container ficar pronto."""
    try:
        with link_publico.compartilhar(video) as url:
            criado = _json(requests.post(f"{GRAPH}/{conta['ig_id']}/media", data={
                "media_type": "REELS", "video_url": url, "caption": legenda[:2200],
                "share_to_feed": "true", "access_token": conta["token"]}, timeout=60))
            _esperar(criado["id"], conta["token"], espera_max)
            return criado["id"]
    except link_publico.ErroLink as exc:
        raise ErroInstagram(f"link temporario para a Meta baixar o video falhou: {exc}")


def publicar(video: Path, legenda: str, ig_id: str | None = None,
             espera_max: int = 1800, comentario: str | None = None) -> dict:
    """Sobe o arquivo e publica como Reels. Devolve {"media_id", "permalink"} e, se o
    primeiro comentario falhou, "comentario_erro" (o post em si saiu)."""
    conta = _conta(ig_id)
    token = conta["token"]
    duracao = mf.duracao(video)
    if duracao > DURACAO_MAX:
        raise ErroInstagram(f"o video tem {duracao / 60:.1f} min e o Reels aceita no maximo "
                            f"{DURACAO_MAX / 60:.0f} min: diminua 'Duracao maxima de cada "
                            "parte' e renderize de novo", fatal=True)
    if video.stat().st_size > TAMANHO_MAX:
        raise ErroInstagram("o arquivo passa de 300 MB (limite do Reels): renderize de novo, "
                            "o render atual ajusta a qualidade para caber", fatal=True)
    if duracao <= LIMITE_DIRETO or not link_publico.disponivel():
        try:
            container = _subir(conta, video, legenda)
            _esperar(container, token, espera_max)
        except ErroInstagram as exc:
            if not (exc.processamento and link_publico.disponivel()):
                raise
            log.warning("envio direto nao processou %s; indo por link", video.name)
            container = _subir_por_link(conta, video, legenda, espera_max)
    else:
        container = _subir_por_link(conta, video, legenda, espera_max)

    media_id = _json(requests.post(f"{GRAPH}/{conta['ig_id']}/media_publish", data={
        "creation_id": container, "access_token": token}, timeout=60))["id"]
    try:
        permalink = _json(requests.get(f"{GRAPH}/{media_id}", params={
            "fields": "permalink", "access_token": token}, timeout=30)).get("permalink")
    except (ErroInstagram, requests.RequestException):
        permalink = None
    log.info("publicado em @%s: %s", conta["username"], permalink or media_id)
    r = {"media_id": media_id, "permalink": permalink}
    if comentario and comentario.strip():
        try:
            _json(requests.post(f"{GRAPH}/{media_id}/comments", data={
                "message": comentario.strip()[:2200], "access_token": token}, timeout=30))
        except (ErroInstagram, requests.RequestException) as exc:
            # o post ja saiu: comentario que falha nao pode virar erro do post
            r["comentario_erro"] = (
                "sem a permissao instagram_manage_comments no token"
                if getattr(exc, "codigo", None) == 10 else str(exc))
            log.warning("primeiro comentario falhou (%s): %s", media_id, exc)
    return r


# ------------------------------------------------------------------ resultados

def metricas(media_id: str, token: str) -> dict:
    dados = _json(requests.get(f"{GRAPH}/{media_id}/insights", params={
        "metric": ",".join(METRICAS), "access_token": token}, timeout=30)).get("data", [])
    saida = {}
    for d in dados:
        valor = (d.get("values") or [{}])[0].get("value")
        if valor is None:
            valor = (d.get("total_value") or {}).get("value")
        saida[d.get("name")] = valor
    return saida


def atualizar_metricas(forcar: bool = False, agora: datetime | None = None,
                       maximo: int = 60) -> dict:
    """Busca os numeros dos posts publicados nos ultimos METRICAS_DIAS dias.

    Sem forcar, so os que nao foram atualizados nas ultimas METRICAS_CADA horas.
    """
    agora = agora or datetime.now()
    tokens = {c["ig_id"]: c["token"] for c in config()["contas"]}
    alvos = []
    for x in fila():
        if x["status"] != "publicado" or not x.get("media_id") or x["ig_id"] not in tokens:
            continue
        publicado = datetime.fromisoformat(x.get("publicado_em") or x["quando"])
        if agora - publicado > timedelta(days=METRICAS_DIAS):
            continue
        ultima = x.get("metricas_em")
        if not forcar and ultima and agora - datetime.fromisoformat(ultima) < METRICAS_CADA:
            continue
        alvos.append(x)
    feitos, erro = 0, None
    for x in alvos[:maximo]:
        try:
            m = metricas(x["media_id"], tokens[x["ig_id"]])
        except ErroInstagram as exc:
            erro = ("o token nao tem a permissao instagram_manage_insights: gere de novo "
                    "com ela marcada" if exc.codigo == 10 else str(exc))
            if exc.fatal:
                break  # token/permissao: os outros posts falhariam igual
            continue
        except requests.RequestException as exc:
            erro = f"sem conexao com a Meta: {exc}"
            break
        _atualizar(x["id"], metricas=m, metricas_em=agora.isoformat(timespec="minutes"))
        feitos += 1
    return {"atualizados": feitos, "erro": erro}


def resultados() -> dict:
    """Posts publicados com numeros, e a media de visualizacoes por gancho."""
    posts = []
    for x in fila():
        if x["status"] != "publicado":
            continue
        m = x.get("metricas") or {}
        posts.append({"id": x["id"], "video": x["video"], "username": x["username"],
                      "parte": x.get("parte", 1), "partes": x.get("partes", 1),
                      "gancho": x.get("gancho") or x["legenda"].split("\n")[0][:120],
                      "permalink": x.get("permalink"),
                      "publicado_em": x.get("publicado_em") or x["quando"],
                      "metricas": m, "metricas_em": x.get("metricas_em")})
    por_gancho: dict[str, list[int]] = {}
    for p in posts:
        if p["metricas"].get("views") is not None:
            por_gancho.setdefault(p["gancho"], []).append(int(p["metricas"]["views"]))
    ganchos = sorted(({"gancho": g, "posts": len(v), "media_views": round(sum(v) / len(v))}
                      for g, v in por_gancho.items()), key=lambda g: -g["media_views"])
    return {"posts": posts, "ganchos": ganchos}


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
    # a janela que comecou ontem ainda pode ter horarios livres hoje de madrugada
    saida, dia = [], agora.replace(hour=h, minute=m, second=0, microsecond=0) - timedelta(days=1)
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


def _ativos(atual: list[dict], ig_id: str) -> list[dict]:
    return [x for x in atual if x["ig_id"] == ig_id and x["status"] in ("pendente", "publicando")]


def agendar(itens: list[dict], ig_id: str | None = None,
            distribuir: bool = False) -> list[dict]:
    """itens: [{"video", "legenda", "parte"?, "partes"?, "gancho"?}] -> entram na fila,
    partes do mesmo video em horarios seguidos, na ordem e na MESMA conta.

    distribuir=True reparte os videos entre todas as contas conectadas, sempre
    mandando o proximo para a conta com menos posts na fila.
    """
    with _trava:
        cfg, atual = config(), fila()
        contas = cfg["contas"] if distribuir else [_conta(ig_id)]
        if not contas:
            raise ErroInstagram("nenhuma conta do Instagram conectada", fatal=True)
        carga = Counter({c["ig_id"]: len(_ativos(atual, c["ig_id"])) for c in contas})
        na_fila = {(x["video"], x.get("parte", 1), x["ig_id"]) for x in atual
                   if x["status"] in ("pendente", "publicando")}
        videos_na_fila = {x["video"] for x in atual if x["status"] in ("pendente", "publicando")}
        grupos: dict[str, list[dict]] = {}
        for i in itens:
            grupos.setdefault(i["video"], []).append(i)
        destino: dict[str, list[dict]] = {}
        for video, grupo in grupos.items():
            if distribuir:
                if video in videos_na_fila:
                    continue  # ja esta na fila de alguma conta
                conta = min(contas, key=lambda c: (carga[c["ig_id"]], contas.index(c)))
            else:
                conta = contas[0]
            novos = [i for i in grupo if (video, i.get("parte", 1), conta["ig_id"]) not in na_fila]
            destino.setdefault(conta["ig_id"], []).extend(novos)
            carga[conta["ig_id"]] += len(novos)
        nomes = {c["ig_id"]: c["username"] for c in contas}
        for conta_id, novos in destino.items():
            ocupados = [datetime.fromisoformat(x["quando"]) for x in _ativos(atual, conta_id)]
            for item, quando in zip(novos, horarios(len(novos), cfg, ocupados=ocupados)):
                atual.append({"id": f"{int(time.time() * 1000)}-{len(atual)}",
                              "video": item["video"], "legenda": item["legenda"],
                              "gancho": item.get("gancho"),
                              "parte": item.get("parte", 1), "partes": item.get("partes", 1),
                              "ig_id": conta_id, "username": nomes[conta_id],
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


def repetir(ids: list[str]) -> list[dict]:
    """Posts com erro voltam para a fila nos proximos horarios livres da conta.

    As partes seguintes do mesmo video (que ficaram paradas esperando a parte que
    falhou) vao junto, logo depois dela e na ordem.
    """
    with _trava:
        cfg, atual = config(), fila()
        alvos = [x for x in atual if x["id"] in ids and x["status"] == "erro"]
        mover: dict[str, dict] = {}
        for a in alvos:
            for x in atual:
                if x["video"] == a["video"] and x["ig_id"] == a["ig_id"] \
                        and x.get("parte", 1) >= a.get("parte", 1) \
                        and x["status"] in ("pendente", "erro"):
                    mover[x["id"]] = x
        for conta in {x["ig_id"] for x in mover.values()}:
            grupo = [x for x in mover.values() if x["ig_id"] == conta]
            primeiro = {}  # video sai na posicao do seu post mais antigo
            for x in grupo:
                primeiro[x["video"]] = min(primeiro.get(x["video"], x["quando"]), x["quando"])
            grupo.sort(key=lambda x: (primeiro[x["video"]], x["video"], x.get("parte", 1)))
            ocupados = [datetime.fromisoformat(x["quando"]) for x in _ativos(atual, conta)
                        if x["id"] not in mover]
            for item, quando in zip(grupo, horarios(len(grupo), cfg, ocupados=ocupados)):
                item.update(status="pendente", tentativas=0, erro=None,
                            quando=quando.isoformat(timespec="minutes"))
        _gravar(_arq_fila(), atual)
        return atual


def bloqueados(atual: list[dict] | None = None) -> set[str]:
    """Ids de pendentes parados porque uma parte anterior do video esta com erro."""
    atual = fila() if atual is None else atual
    com_erro = {(x["video"], x["ig_id"], x.get("parte", 1)) for x in atual if x["status"] == "erro"}
    return {x["id"] for x in atual if x["status"] == "pendente" and any(
        v == x["video"] and c == x["ig_id"] and p < x.get("parte", 1) for v, c, p in com_erro)}


def remover(ids: list[str]) -> list[dict]:
    """Tira da fila o que ainda nao foi publicado (publicados ficam como historico)."""
    with _trava:
        atual = [x for x in fila() if not (x["id"] in ids and x["status"] not in
                                           ("publicando", "publicado"))]
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
        comentario = config()["comentario"]

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
        r = publicar(video, item["legenda"], item["ig_id"], comentario=comentario)
        _atualizar(item["id"], status="publicado", permalink=r["permalink"],
                   media_id=r["media_id"], erro=None,
                   comentario_erro=r.get("comentario_erro"),
                   publicado_em=datetime.now().isoformat(timespec="minutes"))
    except (ErroInstagram, requests.RequestException, OSError,
            subprocess.CalledProcessError) as exc:
        fatal = getattr(exc, "fatal", False)
        adiar = getattr(exc, "adiar", None)
        if adiar and not fatal:  # espera conhecida: nao e falha do post
            _atualizar(item["id"], status="pendente", erro=str(exc),
                       quando=(agora + adiar).isoformat(timespec="minutes"))
            log.warning("publicacao adiada (%s): %s", item["video"], exc)
            return 1
        tentativas = item["tentativas"] + 1
        if fatal or tentativas >= TENTATIVAS:
            _atualizar(item["id"], status="erro", erro=str(exc), tentativas=tentativas,
                       erro_em=datetime.now().isoformat(timespec="seconds"))
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


# ------------------------------------------------------------------ avisos

def avisos(agora: datetime | None = None) -> list[dict]:
    """O que precisa de atencao, do mais grave ao menos: [{chave, nivel, texto}]."""
    agora = agora or datetime.now()
    agora_ts = agora.timestamp()
    cfg, atual = config(), fila()
    saida = []
    for c in cfg["contas"]:
        nome = "@" + c["username"]
        expira = c.get("expira")
        if expira and expira <= agora_ts:
            saida.append({"chave": f"vencido:{c['ig_id']}", "nivel": "erro",
                          "texto": f"O token de {nome} venceu: nada sai ate conectar de novo."})
        elif expira and expira - agora_ts < AVISO_TOKEN_DIAS * 86400:
            quando = datetime.fromtimestamp(expira).strftime("%d/%m %H:%M")
            saida.append({"chave": f"vence:{c['ig_id']}", "nivel": "alerta",
                          "texto": f"O token de {nome} vence em {quando}: conecte de novo "
                                   "com o ID e a Chave secreta do app (fica permanente)."})
        acesso = c.get("acesso_expira")
        if acesso and acesso - agora_ts < 7 * 86400:
            quando = datetime.fromtimestamp(acesso).strftime("%d/%m")
            saida.append({"chave": f"acesso:{c['ig_id']}", "nivel": "alerta",
                          "texto": f"A Meta pede nova autorizacao de {nome} ate {quando}: "
                                   "gere um token novo e conecte de novo."})
    # so erro de token DEPOIS da ultima conexao: o de antes ja foi resolvido reconectando
    conectada = {c["ig_id"]: c.get("conectado_em") for c in cfg["contas"]}
    invalidos = {x["ig_id"] for x in atual if x["status"] == "erro"
                 and (x.get("erro") or "").startswith("Token expirado")
                 and not (conectada.get(x["ig_id"]) and
                          (x.get("erro_em") or "") < conectada[x["ig_id"]])}
    for ig_id in invalidos - {c["ig_id"] for c in cfg["contas"]
                              if c.get("expira") and c["expira"] <= agora_ts}:
        nome = next((x["username"] for x in atual if x["ig_id"] == ig_id), ig_id)
        saida.append({"chave": f"invalido:{ig_id}", "nivel": "erro",
                      "texto": f"A Meta recusou o token de @{nome}: conecte de novo."})
    erros = sum(1 for x in atual if x["status"] == "erro")
    if erros:
        saida.append({"chave": f"erros:{erros}", "nivel": "alerta",
                      "texto": f"{erros} post(s) com erro na fila: veja em Publicar no "
                               "Instagram e use 'Tentar de novo'."})
    if not cfg["contas"] and any(x["status"] == "pendente" for x in atual):
        saida.append({"chave": "sem-conta", "nivel": "erro",
                      "texto": "Ha posts na fila mas nenhuma conta conectada."})
    return saida


def notificar_avisos(agora: datetime | None = None) -> int:
    """Manda para o Windows os avisos novos (cada um no maximo a cada 12h)."""
    agora = agora or datetime.now()
    novos = []
    with _trava:
        c = config()
        enviados = dict(c.get("notificados") or {})
        for a in avisos(agora):
            ultimo = enviados.get(a["chave"])
            if ultimo and agora - datetime.fromisoformat(ultimo) < NOTIFICAR_CADA:
                continue
            enviados[a["chave"]] = agora.isoformat(timespec="minutes")
            novos.append(a)
        if novos:
            c["notificados"] = enviados
            _gravar(_arq_config(), c)
    for a in novos:
        from scripts import notificar
        notificar.windows("Painel de Filmes", a["texto"])
    return len(novos)


# ------------------------------------------------------------------ agendador

def agendador(parar: threading.Event, intervalo: int = 30) -> None:
    recuperar_presos()
    ultimo_aviso = ultima_metrica = datetime.min
    while not parar.is_set():
        try:
            while processar_vencidos():
                if parar.is_set():
                    break
            agora = datetime.now()
            if agora - ultimo_aviso >= timedelta(minutes=10):
                ultimo_aviso = agora
                notificar_avisos(agora)
            if agora - ultima_metrica >= timedelta(hours=1):
                ultima_metrica = agora
                atualizar_metricas(agora=agora)
        except Exception:  # noqa: BLE001 — o agendador nao pode morrer
            log.exception("agendador do instagram")
        parar.wait(intervalo)
