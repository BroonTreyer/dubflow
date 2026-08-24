# dubflow

Cola um link do YouTube → sai episódio legendado em pt-BR, cortes verticais 9:16
prontos para Reels/TikTok, e o episódio arquivado no acervo.

Transcrição roda local na GPU (custo zero). Só a tradução e a seleção de cortes
usam API paga.

## Como funciona

```
link → yt-dlp → faster-whisper (GPU) → tradução (Claude) → SRT/ASS
                                              ↓
                            seleção de cortes (Claude) → render 9:16 + legenda
                                              ↓
                     fila de publicação → Instagram / TikTok / Telegram
                                              ↓
                                     acervo (pasta local ou Drive)
```

## Instalação

Requer FFmpeg no PATH e uma GPU NVIDIA (testado em RTX 3060 Ti, 8 GB).

```powershell
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Preencha no `.env` — os dois são obrigatórios:

- `ANTHROPIC_API_KEY` — a chave da API.
- `DUBFLOW_PASSWORD` — a senha do painel. Sem ela nenhum login é aceito.

`SECRET_KEY` pode ficar vazia: é gerada e guardada em `data/.secret_key` na
primeira execução.

## Uso

```powershell
.\run.ps1            # sobe painel (porta 8030) + worker
```

Abra <http://127.0.0.1:8030>, cole o link, escolha a origem do conteúdo e clique
em Processar. O worker pega da fila; o painel mostra o progresso.

Para rodar separado: `.\run.ps1 -Only web` e `.\run.ps1 -Only worker`.

## Testes

```powershell
$env:PYTHONPATH = $PWD
.venv\Scripts\python.exe -m tests.test_pipeline   # lógica pura
.venv\Scripts\python.exe -m tests.test_render     # ffmpeg real, vídeo sintético
.venv\Scripts\python.exe -m tests.test_web        # painel, fila e regra de licença
```

Nenhum deles precisa de rede, GPU ou chave de API.

## Custo por episódio de 1 hora

| Etapa | Custo |
|---|---|
| Download + extração de áudio | 0 |
| Transcrição (large-v3 local) | 0 |
| Tradução (~13k tokens in / 13k out, Opus 5) | ~US$ 0,40 |
| Seleção de cortes | ~US$ 0,10 |
| Render dos cortes | 0 |

O system prompt da tradução vai com `cache_control`, então do segundo bloco em
diante ele custa ~10% do preço de entrada. `USE_BATCH_API=true` corta os custos
de API pela metade em troca de latência (bom para reprocessar acervo à noite).

Para reduzir mais: `TRANSLATE_MODEL=claude-sonnet-5` custa menos e continua
sólido em tradução. `TRANSLATE_EFFORT=low` reduz mais ainda.

## Segurança

O painel comanda suas contas sociais e o acervo inteiro, então nada nele é anônimo.

- **Sessão obrigatória** em toda rota, com cookie assinado (`httponly`, `samesite=lax`)
  e token CSRF em cada formulário.
- **Bind local por padrão** (`HOST=127.0.0.1`). Expor na rede é decisão explícita
  no `.env`, e o app avisa no log quando você faz isso.
- **`/media` é a única rota sem login**, porque a Meta busca o vídeo por URL e não
  faz login. Em vez de ficar aberta, cada arquivo tem uma assinatura HMAC no
  caminho: a URL funciona sem credencial, mas não é adivinhável nem enumerável.
- **Nomes de corte carregam o id do episódio** (`ep00042_corte_01.mp4`) e o caminho
  é resolvido de forma exata. Sem isso, todo episódio teria `corte_01.mp4` e a
  publicação sairia com o vídeo do episódio errado.
- **`.gitignore` cobre `.env` e `data/.secret_key`** desde o primeiro commit.
- Se você trocar `secure=False` por `True` no cookie (em `web/main.py`), o painel
  passa a exigir HTTPS — recomendado se for expor pela internet.

## Licença do conteúdo

Cada episódio carrega um campo de origem: `unknown`, `licensed`, `owned` ou
`public_domain`.

Legendar, cortar e publicar nas redes funciona com qualquer origem. **A entrega
do episódio completo no Telegram só acontece para `licensed`, `owned` ou
`public_domain`** — a checagem está em `publishers/telegram.py:deliver_episode`
e roda antes de qualquer coisa, inclusive antes de verificar credenciais.

Isso existe porque o pipeline é o mesmo nos dois casos, mas o risco não: um canal
ou catálogo construído sobre conteúdo de terceiros pode ser desligado por um
terceiro. O flag mantém o caminho legítimo como o padrão sem travar seu teste.

## Configuração relevante

| Variável | Padrão | Para quê |
|---|---|---|
| `TRANSLATE_MODEL` | `claude-opus-5` | Modelo da tradução |
| `TRANSLATE_EFFORT` | `medium` | Profundidade de raciocínio |
| `USE_BATCH_API` | `false` | Metade do preço, sem garantia de latência |
| `CLIPS_PER_EPISODE` | `5` | Quantos cortes por episódio |
| `CLIP_REFRAME` | `face` | Enquadramento 9:16: `face` / `center` / `pad` (legado) |
| `CLIP_KARAOKE` | `true` | Legenda karaokê (palavra destacada no tempo da fala) |
| `CLIP_RENDER_WIDE` | `true` | Renderiza também a versão 16:9 (YouTube horizontal) |
| `CLIP_THUMBNAIL` | `true` | Gera thumbnail 16:9 de cada corte |
| `AUDIO_LOUDNORM` | `true` | Normaliza o volume dos cortes (-14 LUFS) |
| `BURN_FULL_EPISODE` | `false` | Queimar legenda no episódio inteiro (lento) |
| `ARCHIVE_DIR` | `data/archive` | Aponte para o Drive sincronizado |
| `WHISPER_COMPUTE` | `float16` | Use `int8_float16` se faltar VRAM |

### Glossário por canal

`data/glossaries/<canal-slug>.json` com pares `{"termo original": "tradução"}`
mantém nomes e jargão consistentes entre episódios do mesmo canal. Opcional.

## Publicação

- **YouTube** — upload direto via Data API v3 (OAuth2; rode `scripts/youtube_auth.py`
  uma vez para gerar o refresh token). Vídeo vertical vira **Short**; escolhendo a
  orientação horizontal no painel, o corte 16:9 sobe como **vídeo comum**. Privacidade
  `private` por padrão (`YOUTUBE_PRIVACY`).
- **Instagram** — a Graph API não aceita upload: ela busca o vídeo por URL.
  `PUBLIC_BASE_URL` precisa apontar para este servidor acessível pela Meta
  (cloudflared/ngrok resolve). Requer conta Business/Creator.
- **TikTok** — upload direto de arquivo. Enquanto o app não passar pela auditoria
  do TikTok, os posts saem como rascunho privado.
- **Telegram** — envio direto ao canal; sujeito à regra de licença acima.

## Transcrição: por que roda em subprocesso

O `faster-whisper` roda num processo separado (`app/pipeline/_transcribe_worker.py`),
não na mesma thread do worker. O motivo é concreto: quando falta VRAM, o
CTranslate2 às vezes **não levanta erro — ele simplesmente para de responder**.
Uma thread presa em código nativo não pode ser interrompida, então a única forma
confiável de aplicar timeout é poder matar o processo inteiro (o que ainda
devolve toda a VRAM de uma vez).

Em torno disso há três defesas:

| Mecanismo | O que faz |
|---|---|
| Watchdog de silêncio (6 min) | Distingue "lento" de "travado" pelo progresso reportado |
| Cascata de modos | `float16` → `int8_float16` → CPU, degradando em vez de falhar |
| Seleção por VRAM livre | Não tenta um modo que comprovadamente não cabe na memória |

### A armadilha do `cublas64_12.dll`

Se aparecer `Library cublas64_12.dll is not found or cannot be loaded`, **quase
sempre não é o que parece**. O CTranslate2 carrega essa biblioteca sob demanda, na
primeira operação de GPU, e nesse caminho ele não consulta os diretórios
registrados via `os.add_dll_directory` — só o `PATH` do processo. Por isso
`app/cuda_bootstrap.py` acrescenta os diretórios das bibliotecas NVIDIA ao `PATH`.

Consequência prática: **carregar o modelo não prova que a GPU funciona.** Carregar
aloca VRAM e parece sucesso; o `cublas` só é exercitado numa inferência de
verdade. Qualquer teste de GPU precisa transcrever algo, não apenas instanciar o
modelo.

## Ações sob demanda

Duas operações caras ficam fora do fluxo automático e são disparadas por botão no
painel do episódio (o worker as executa em fila):

- **Gerar vídeo legendado** — queima a legenda no vídeo inteiro. Re-codifica tudo
  e leva de 10 a 40 minutos, por isso não roda sozinho. O `.srt` já sai pronto e
  serve para subir como faixa no YouTube sem re-codificar nada.
- **Refazer cortes** — re-renderiza os cortes já selecionados com o estilo atual de
  legenda. Não repete transcrição nem tradução, então **não gasta API**.

## Estilo de legenda

Os tamanhos no arquivo ASS são relativos ao `PlayRes` declarado, não a pixels da
tela — e o `PlayResY` do episódio (1080) é diferente do corte (1920), então o
mesmo número significa tamanhos diferentes. A referência usada: altura da fonte em
torno de 5% da altura do quadro.

O campo `Alignment` segue a disposição do teclado numérico: **2 = inferior
centralizado**, 5 = centro da tela. Os dois estilos usam 2. Nos cortes, a margem
inferior é alta o bastante para a legenda não ficar atrás da interface do
TikTok/Reels.

Depois de mexer em `STYLE_CLIP`, use *Refazer cortes* para aplicar sem reprocessar.

## Limites conhecidos

- Não faz dublagem, só legenda. A dublagem é o próximo módulo (TTS + alinhamento).
- Um worker por vez: transcrição e render competem pela mesma GPU.
- O reframe 9:16 recorta focando no rosto (YuNet, com Haar de reserva; cai para o
  centro quando não acha rosto). Em quadro largo com dois interlocutores, a janela
  vai para o rosto maior — vale revisar antes de postar.
