# Servidor de render (um PC faz o trabalho, você acessa de qualquer lugar)

O dubflow já é um app cliente-servidor. Em vez de rodar uma cópia em cada máquina,
rode **uma instância só no PC de render** (potente, ligado 24/7) e use os outros
aparelhos (este PC, celular) apenas como **navegador**: você cola o link aqui, o
render processa, e você acompanha/publica daqui.

```
   PC de render (servidor único)              Você (navegador)
   painel :8030 + worker + data/   ◄── rede ──►  cola link, vê, publica
```

## 1. Ligar o servidor no PC de render

No `.env` do PC de render:

```ini
HOST=0.0.0.0            # aceita conexões da rede (o padrão 127.0.0.1 é só local)
DUBFLOW_PASSWORD=<senha forte>   # obrigatório — o painel controla suas contas
# COOKIE_SECURE liga sozinho quando HOST não é local; deixe vazio.
```

Suba tudo:

```powershell
.\run.ps1        # painel + worker (+ bot, se houver token do Telegram)
```

O `run.ps1` imprime o **IP da LAN** e a URL a abrir (ex.: `http://192.168.0.42:8030`).

## 2. Liberar o firewall (uma vez, no PC de render)

```powershell
New-NetFirewallRule -DisplayName "dubflow" -Direction Inbound -Action Allow `
  -Protocol TCP -LocalPort 8030 -Profile Private
```

> Use só o perfil **Private** (rede de casa). Nunca exponha a porta direto na
> internet sem um túnel (próximo passo).

## 3. Acessar

- **Mesma rede (casa):** abra `http://<ip-do-render>:8030` neste PC ou no celular.
- **De fora de casa:** instale **Tailscale** nos dois PCs e no celular (mesma conta).
  Ele cria uma rede privada com IP fixo e **TLS**, sem abrir porta no roteador.
  Aí você acessa `http://<ip-tailscale-do-render>:8030` de qualquer lugar.
  (Alternativa: Cloudflare Tunnel, que dá um domínio https público.)

## 4. Levar seus canais e credenciais para o render

O banco e os cofres de credenciais ficam **fora do git** (gitignored), então
`git pull` **não** os carrega. Para mover os canais + tokens de uma máquina para
o render, use o migrador:

**No PC de origem** (onde os canais estão cadastrados):
```powershell
.venv\Scripts\python.exe -m scripts.channels_transfer export
# gera data/channels_transfer.json  (CONTÉM SEGREDOS)
```

Copie esse arquivo para o PC de render (USB ou pela LAN). **No PC de render:**
```powershell
.venv\Scripts\python.exe -m scripts.channels_transfer import
# recria os canais e reescreve os cofres sob os ids novos
```

Depois **apague** o `channels_transfer.json` nas duas pontas — ele tem client
secrets e tokens.

> Os **refresh tokens** do YouTube são gerados por OAuth no navegador. Rode o
> `scripts.youtube_auth --channel <id>` **na máquina que vai abrir o navegador**;
> se o render for headless, gere-os onde puder logar e leve-os no export/import.

## 5. Segurança (o que já está coberto)

- Login com senha + **limite de tentativas por IP** (anti força-bruta).
- **CSRF** em todas as ações de escrita.
- Cookie de sessão **secure** liga sozinho fora do localhost.
- `/media` é servido por **URL assinada (HMAC)**, não enumerável.

O que **você** garante: senha forte, firewall só em rede privada, e — para acesso
de fora — um túnel com TLS (Tailscale/Cloudflare), nunca a porta crua na internet.

## Quando migrar para "de verdade" distribuído

Vários workers de render, fila (Redis) e storage (S3/MinIO) só valem quando a
operação sair de casa para uma VPS/nuvem. Para 1 PC de render em casa, a
instância única acima entrega o mesmo resultado sem essa complexidade.
