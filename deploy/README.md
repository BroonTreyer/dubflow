# Migrar dubflow + OfertaFlow para servidor Linux com GPU

Ordem importa. Cada passo assume o anterior.

## 0. A máquina

Precisa de GPU NVIDIA para a transcrição (`faster-whisper large-v3`, `float16`).
O uso de GPU é **esporádico** — rajadas de 6 a 8 episódios em dias alternados —,
então servidor dedicado mensal sai melhor que GPU por hora.

Mínimo confortável: 8 GB de VRAM, 4 vCPU, 16 GB de RAM, 200 GB de disco
(44 GB de dados hoje, e o acervo cresce).

## 1. Provisionar

```bash
sudo bash provisionar-servidor.sh
```

Se ele instalar o driver NVIDIA, **reinicie e rode de novo** — driver só vale
depois do boot. Confira com `nvidia-smi` antes de seguir.

## 2. Código

```bash
sudo -u dubflow    git clone <seu-remoto-dubflow> /opt/dubflow
sudo -u ofertaflow git clone <seu-remoto-wppgrups> /opt/ofertaflow

sudo -u dubflow bash -c 'cd /opt/dubflow && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt'
sudo -u ofertaflow bash -c 'cd /opt/ofertaflow && npm install'
```

## 3. Segredos

Os `.env` **não** estão no git, e é assim que tem que ser. Copie da sua máquina:

```bash
scp C:\Users\mathe\dubflow\.env   servidor:/tmp/df.env
scp C:\Users\mathe\wppgrups\.env  servidor:/tmp/of.env
```

No servidor, mova para o lugar e tranque a permissão:

```bash
sudo install -o dubflow    -m 600 /tmp/df.env /opt/dubflow/.env
sudo install -o ofertaflow -m 600 /tmp/of.env /opt/ofertaflow/.env
sudo shred -u /tmp/df.env /tmp/of.env
```

Ajuste no `.env` do dubflow: `HOST=127.0.0.1`. O painel não pode escutar na rede.

## 4. Dados

O dubflow tem 44 GB. **Não precisa mover tudo de uma vez.**

```bash
# Primeiro o que a operação precisa para voltar a publicar:
rsync -avP data/dubflow.db data/channels/ servidor:/opt/dubflow/data/
rsync -avP data/episodes/  servidor:/opt/dubflow/data/episodes/   # 27 GB

# O acervo pode ir depois, em segundo plano:
rsync -avP data/archive/   servidor:/opt/dubflow/data/archive/    # 17 GB
```

Conta de tempo: a 20 Mbps de upload, 44 GB levam ~5 horas. Comece pelo banco e
pelos episódios, deixe o acervo subindo durante a noite.

O OfertaFlow tem 23 MB — vai num `rsync` só.

## 5. YouTube: reautorizar

**Os tokens não viajam.** Além disso, hoje (12/09/2026) todos os 7 canais estão
com o token expirado ou revogado — a reautorização é necessária de qualquer jeito.

Precisa de navegador. Use o VNC (passo 7) e rode lá dentro:

```bash
sudo -u dubflow bash -c 'cd /opt/dubflow && PYTHONPATH=/opt/dubflow .venv/bin/python -m scripts.youtube_auth --channel <id>'
```

Cada canal é uma conta Google diferente. Confira o nome na tela de autorização
antes de aprovar: autorizar o canal 7 logado na conta do 6 grava o token errado
e o corte sai no canal errado.

## 6. Serviços

```bash
sudo cp /opt/dubflow/deploy/dubflow-*.service /etc/systemd/system/
sudo cp /opt/ofertaflow/deploy/ofertaflow*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dubflow-web ofertaflow
```

O **worker do dubflow por último**, e só depois de conferir a fila:

```bash
sudo -u dubflow /opt/dubflow/deploy/guarda-vencidas.sh
```

Se ele recusar, reespace antes de insistir:

```bash
sudo -u dubflow bash -c 'cd /opt/dubflow && .venv/bin/python -m scripts.reagendar_fila --apply'
sudo systemctl enable --now dubflow-worker
```

## 7. VNC: login do Mercado Livre

O gerador de link de afiliado e a colheita das lojas de marca só funcionam num
Chrome logado — o servidor sozinho leva anti-bot nessas páginas.

```bash
sudo -u ofertaflow vncpasswd              # define a senha uma vez
sudo -u ofertaflow vncserver :1 -localhost yes -geometry 1600x900
```

Do seu PC, abra o túnel e conecte o cliente VNC em `localhost:5901`:

```bash
ssh -L 5901:localhost:5901 servidor
```

Dentro da sessão: abra o painel de afiliados do Mercado Livre e faça login. O
perfil em `/opt/ofertaflow/chrome-profile` guarda a sessão. Depois:

```bash
sudo systemctl enable --now ofertaflow-chrome
```

`-localhost yes` não é detalhe: VNC exposto na internet é console gráfico aberto.

## 8. Acessar os painéis

Nenhum dos dois escuta fora do servidor. Do seu PC:

```bash
ssh -L 8030:localhost:8030 -L 3000:localhost:3000 servidor
```

E abra `http://localhost:8030` (dubflow) e `http://localhost:3000` (OfertaFlow).

## Depois: desligar o que sobrou na sua máquina

Só quando os dois estiverem publicando no servidor. Rodar os dois em paralelo
duplica publicação: dois workers na mesma conta do YouTube e dois bots no mesmo
grupo de WhatsApp.

No Windows, a tarefa agendada **OfertaFlow** sobe o bot no logon — desabilite,
senão ela ressuscita a instância antiga sem ninguém pedir.

---

# Operação híbrida no dia a dia

O PC processa (GPU, render) e agenda. O servidor só publica. Os dois nunca
escrevem no mesmo banco — o que viaja é um **pacote de mão única**.

## No PC, depois de processar os episódios

```powershell
# Ver o que sairia, sem gravar nada:
.venv\Scripts\python.exe -m scripts.exportar_pacote

# Gravar o pacote e tirar os posts da fila do PC:
.venv\Scripts\python.exe -m scripts.exportar_pacote --apply
```

O comando imprime as duas linhas prontas de `rsync` e de importação.

**O que o `--apply` faz de mais importante não é copiar arquivo.** Ele muda o
status dos posts exportados para `exported`, e isso os tira da fila do PC — o
`pending_posts()` filtra por `status = 'pending'`. Sem esse passo, PC e servidor
publicariam o mesmo corte no mesmo canal.

Nada é apagado: o corte continua no disco e o post continua no banco, só que fora
da fila. Para trazer de volta, `UPDATE posts SET status='pending' WHERE ...`.

Os posts cujo arquivo de vídeo não existe **ficam na fila do PC** e são listados
na saída — eles não somem dos dois lados.

## No servidor

```bash
.venv/bin/python -m scripts.importar_pacote pacotes/pacote_<id>            # simula
.venv/bin/python -m scripts.importar_pacote pacotes/pacote_<id> --apply
```

A importação recria as linhas de `episodes`/`clips`/`posts` que o worker daqui
já sabe ler. **Não existe publicador novo** — e por isso não existe um segundo
caminho de publicação para manter em dia.

É idempotente: cada item traz uma chave `origem` única, com índice único no banco.
Reenviar o mesmo pacote não duplica publicação; ele relata "já importados" e não
grava nada.

## Duas coisas que quebram em silêncio se você não souber

**O canal é casado por nome + plataforma, nunca por id.** Os ids dos dois bancos
são independentes. Antes do primeiro pacote, cadastre no servidor os canais com
**exatamente o mesmo nome** que têm no PC. Canal que não existir lá interrompe a
importação com a lista do que falta — de propósito, porque publicar no canal
errado é irreversível.

**O crédito da fonte viaja no pacote** (`source_url`, `channel`, `meta` do
episódio). É o que o publicador cola no fim da descrição e o que diferencia
"corte com crédito" de "reupload" aos olhos de quem denuncia.

## Tamanho

Medido no acervo atual: **18,2 MB por corte**. Um dia de publicação (15 posts)
dá ~270 MB. Sobe em minutos, e o servidor nunca precisa dos 17 GB de vídeos-fonte
nem dos 17 GB de acervo.
