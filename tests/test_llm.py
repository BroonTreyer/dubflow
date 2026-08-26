"""Testes da distribuicao entre provedores de IA.

O que precisa ser verdade para o pipeline nunca mais parar por causa de um teto:

  - teto estourado tira o provedor de circulacao NA HORA (nao fica repetindo);
  - a proxima chamada vai para o outro provedor sozinha;
  - tarefas independentes se dividem entre os dois e rodam juntas;
  - quando todo mundo cai, o erro SOBE — nunca vira resultado vazio silencioso.

Nada aqui toca a rede: os provedores sao dubles.

    py -m tests.test_llm
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ.setdefault("DUBFLOW_PASSWORD", "senha-de-teste")

from app.config import settings  # noqa: E402

_tmp = Path(tempfile.mkdtemp(prefix="dubflow_llm_"))
settings.data_dir = _tmp

from app.pipeline import llm  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} -> {detail}")
        failures.append(label)


# --------------------------------------------------------------------- dubles


class FakeStatusError(Exception):
    """Imita um erro de SDK que carrega status_code."""

    def __init__(self, msg: str, status_code: int) -> None:
        super().__init__(msg)
        self.status_code = status_code


class FakeProvider(llm.Provider):
    """Provedor de mentira: responde, falha ou demora, conforme configurado."""

    def __init__(self, name: str, *, erro: Exception | None = None,
                 texto: str = '{"ok": true}', delay: float = 0.0) -> None:
        super().__init__(name=name, models={r: f"{name}-modelo" for r in
                                            (llm.ROLE_CLIP, llm.ROLE_SCAN,
                                             llm.ROLE_TRANSLATE)})
        self.erro = erro
        self.texto = texto
        self.delay = delay
        self.chamadas = 0
        self.lock = threading.Lock()

    def call(self, role, system, user, schema, max_tokens, effort, cache_system):
        with self.lock:
            self.chamadas += 1
        if self.delay:
            time.sleep(self.delay)
        if self.erro is not None:
            raise self.erro
        return llm.LLMResult(text=self.texto, usage={"input": 1, "output": 1},
                             provider=self.name, model=self.model_for(role))


def usar(*provedores: llm.Provider) -> None:
    llm.reset(list(provedores))


# ---------------------------------------------------------------------- testes


TETO_ANTHROPIC = (
    "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
    "'message': 'You have reached your specified API usage limits. You will regain "
    "access on 2099-09-01 at 00:00 UTC.'}}"
)
QUOTA_OPENAI = (
    "Error code: 429 - {'error': {'message': 'You exceeded your current quota, "
    "please check your plan and billing details.', 'code': 'insufficient_quota'}}"
)


def test_classificacao() -> None:
    """Classificar o erro e o que separa 'tentar de novo' de 'trocar de chave'."""
    print("classificacao de erro")

    cat, cooldown = llm.classify(FakeStatusError(TETO_ANTHROPIC, 400))
    check("teto da Anthropic e bloqueio duro", cat == llm.HARD, cat)
    # A data informada pela API deixa de valer quando alguem aumenta o limite da
    # conta. Sem teto, o sistema ignorou a Anthropic por 6 dias depois de liberada.
    check("cooldown nunca passa do teto", cooldown <= llm.HARD_COOLDOWN_MAX, cooldown)
    check("mas ainda castiga por um tempo util", cooldown >= 60 * 30, cooldown)
    check("o teto e curto o bastante para redescobrir no mesmo dia",
          llm.HARD_COOLDOWN_MAX <= 6 * 3600, llm.HARD_COOLDOWN_MAX)

    cat_oa, _ = llm.classify(FakeStatusError(QUOTA_OPENAI, 429))
    check("quota da OpenAI e bloqueio duro", cat_oa == llm.HARD, cat_oa)
    check("um 429 comum continua transitorio",
          llm.classify(FakeStatusError("Rate limit reached, slow down", 429))[0]
          == llm.TRANSIENT)
    check("5xx e transitorio",
          llm.classify(FakeStatusError("bad gateway", 502))[0] == llm.TRANSIENT)
    check("400 de esquema e fatal (o outro falharia igual)",
          llm.classify(FakeStatusError("invalid schema: additionalProperties", 400))[0]
          == llm.FATAL)

    # Sem a data, cai no padrao configuravel (tambem limitado pelo teto).
    _, padrao = llm.classify(FakeStatusError("credit balance is too low", 400))
    check("sem data usa o cooldown padrao",
          abs(padrao - min(settings.llm_block_cooldown_minutes * 60,
                           llm.HARD_COOLDOWN_MAX)) < 1, padrao)

    # Destravar na mao depois de aumentar o limite da conta.
    a = FakeProvider("anthropic", erro=FakeStatusError(TETO_ANTHROPIC, 400))
    b = FakeProvider("openai")
    usar(a, b)
    llm.call_json(llm.ROLE_CLIP, "s", "u", {})
    check("provedor fica marcado como fora",
          any(not p["available"] for p in llm.status()))
    llm.clear_health()
    check("clear_health devolve todo mundo para a fila",
          all(p["available"] for p in llm.status()), llm.status())


def test_failover() -> None:
    """Uma chave morre, a outra assume — que era o pedido central."""
    print("failover entre provedores")

    a = FakeProvider("anthropic", erro=FakeStatusError(TETO_ANTHROPIC, 400))
    b = FakeProvider("openai", texto='{"quem": "openai"}')
    usar(a, b)

    r = llm.call_json(llm.ROLE_CLIP, "s", "u", {})
    check("resposta veio do segundo provedor", r.provider == "openai", r.provider)
    check("o bloqueado nao foi repetido", a.chamadas == 1, a.chamadas)

    # A partir daqui o provedor caido nem e tentado: economiza tempo e ruido.
    antes = a.chamadas
    llm.call_json(llm.ROLE_CLIP, "s", "u", {})
    check("provedor caido sai da fila nas chamadas seguintes", a.chamadas == antes,
          a.chamadas)
    check("saude registrada", any(not p["available"] for p in llm.status()
                                  if p["name"] == "anthropic"), llm.status())

    # Erro transitorio: repete no MESMO provedor antes de desistir dele.
    settings.llm_max_retries = 2
    c = FakeProvider("anthropic", erro=FakeStatusError("connection reset", 503))
    d = FakeProvider("openai")
    usar(c, d)
    llm.call_json(llm.ROLE_CLIP, "s", "u", {})
    check("transitorio tenta de novo no mesmo provedor", c.chamadas == 2, c.chamadas)
    check("e so entao cai para o outro", d.chamadas == 1, d.chamadas)
    settings.llm_max_retries = 3


def test_fatal_nao_faz_failover() -> None:
    """Erro nosso nao pode queimar a chave boa."""
    print("erro fatal")

    a = FakeProvider("anthropic", erro=FakeStatusError("invalid schema", 400))
    b = FakeProvider("openai")
    usar(a, b)
    try:
        llm.call_json(llm.ROLE_CLIP, "s", "u", {})
        check("400 de esquema propaga", False, "nao levantou")
    except FakeStatusError:
        check("400 de esquema propaga", True)
    check("o segundo provedor nem foi chamado", b.chamadas == 0, b.chamadas)


def test_todos_caidos() -> None:
    """O silencio e que custou caro: com todo mundo fora, tem que estourar."""
    print("todos os provedores fora")

    a = FakeProvider("anthropic", erro=FakeStatusError(TETO_ANTHROPIC, 400))
    b = FakeProvider("openai", erro=FakeStatusError(QUOTA_OPENAI, 429))
    usar(a, b)
    try:
        llm.call_json(llm.ROLE_CLIP, "s", "u", {})
        check("levanta AllProvidersDown", False, "nao levantou")
    except llm.AllProvidersDown as exc:
        check("levanta AllProvidersDown", True)
        check("a mensagem diz os dois motivos",
              "anthropic" in str(exc) and "openai" in str(exc), str(exc)[:120])


def test_divisao_paralela() -> None:
    """As duas chaves ao mesmo tempo: metade das janelas em cada uma."""
    print("divisao das tarefas")

    a = FakeProvider("anthropic", delay=0.25)
    b = FakeProvider("openai", delay=0.25)
    usar(a, b)

    tarefas = [{"system": "s", "user": f"u{i}", "schema": {}} for i in range(4)]
    inicio = time.time()
    res = llm.map_json(llm.ROLE_CLIP, tarefas)
    decorrido = time.time() - inicio

    check("todas as tarefas voltaram", all(r is not None for r in res), res)
    check("dividiu 2 para cada chave", a.chamadas == 2 and b.chamadas == 2,
          (a.chamadas, b.chamadas))
    # 4 tarefas de 0,25s: em serie levaria 1s. Em paralelo, bem menos.
    check("rodaram ao mesmo tempo", decorrido < 0.7, round(decorrido, 2))

    # Com um provedor fora, o outro absorve tudo em vez de a tarefa falhar.
    c = FakeProvider("anthropic", erro=FakeStatusError(TETO_ANTHROPIC, 400))
    d = FakeProvider("openai")
    usar(c, d)
    res2 = llm.map_json(llm.ROLE_CLIP, [{"system": "s", "user": "u", "schema": {}}
                                        for _ in range(3)])
    check("chave morta nao perde tarefa", all(r is not None for r in res2), res2)
    check("todas caem na chave viva", d.chamadas == 3, d.chamadas)


def test_falha_parcial_nao_derruba() -> None:
    """Uma janela que falha nao pode levar as outras junto."""
    print("falha parcial")

    class Instavel(FakeProvider):
        def call(self, role, system, user, schema, max_tokens, effort, cache_system):
            if user == "u1":
                raise FakeStatusError("schema ruim", 400)  # fatal: nao faz failover
            return super().call(role, system, user, schema, max_tokens, effort,
                                cache_system)

    usar(Instavel("anthropic"))
    vistos: list[int] = []
    tarefas = [{"system": "s", "user": f"u{i}", "schema": {}} for i in range(3)]
    res = llm.map_json(llm.ROLE_CLIP, tarefas, on_error=lambda i, e: vistos.append(i))

    check("a tarefa ruim vira None", res[1] is None, res)
    check("as outras duas sobrevivem", res[0] is not None and res[2] is not None, res)
    check("o chamador e avisado de qual falhou", vistos == [1], vistos)


def test_ordem_de_preferencia() -> None:
    """Quem vem primeiro no .env e o preferido — e da para inverter."""
    print("ordem de preferencia")

    a = FakeProvider("anthropic")
    b = FakeProvider("openai")
    usar(a, b)
    llm.call_json(llm.ROLE_CLIP, "s", "u", {})
    check("usa o primeiro da lista", a.chamadas == 1 and b.chamadas == 0,
          (a.chamadas, b.chamadas))

    usar(b, a)
    b.chamadas = a.chamadas = 0
    llm.call_json(llm.ROLE_CLIP, "s", "u", {})
    check("inverter a lista inverte a preferencia", b.chamadas == 1 and a.chamadas == 0,
          (a.chamadas, b.chamadas))

    # `prefer` (usado pelo rodizio) escolhe sem tirar o failover do caminho.
    usar(a, b)
    a.chamadas = b.chamadas = 0
    r = llm.call_json(llm.ROLE_CLIP, "s", "u", {}, prefer="openai")
    check("prefer manda na tarefa", r.provider == "openai", r.provider)


def test_sem_provedor() -> None:
    print("nenhum provedor configurado")
    usar()
    try:
        llm.call_json(llm.ROLE_CLIP, "s", "u", {})
        check("erro claro quando nao ha chave", False, "nao levantou")
    except llm.AllProvidersDown as exc:
        check("erro claro quando nao ha chave", "ANTHROPIC_API_KEY" in str(exc), str(exc))


def main() -> int:
    test_classificacao()
    test_failover()
    test_fatal_nao_faz_failover()
    test_todos_caidos()
    test_divisao_paralela()
    test_falha_parcial_nao_derruba()
    test_ordem_de_preferencia()
    test_sem_provedor()

    print()
    if failures:
        print(f"{len(failures)} falha(s): {', '.join(failures)}")
        return 1
    print("distribuicao entre provedores OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
