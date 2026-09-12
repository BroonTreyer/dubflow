r"""Embala publicacoes agendadas para o servidor: cortes + manifesto.

    .venv\Scripts\python.exe -m scripts.exportar_pacote                 # simula
    .venv\Scripts\python.exe -m scripts.exportar_pacote --apply
    .venv\Scripts\python.exe -m scripts.exportar_pacote --limite 40 --apply

O PC processa (GPU, render) e agenda; o servidor so publica. Este script pega os
posts pendentes, copia os arquivos que o publicador vai precisar e escreve um
manifesto com todos os metadados.

A parte que importa mais que a copia: com --apply, os posts exportados SAEM da
fila do PC (status 'exported'). Sem isso as duas maquinas publicariam o mesmo
corte no mesmo canal — o pending_posts() filtra por status = 'pending', entao
trocar o status basta, sem tocar no worker.

Nada e apagado: o corte continua no disco e o post continua no banco, so que fora
da fila. Para trazer de volta: UPDATE posts SET status='pending' WHERE ...
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402

# Os campos que o worker le em pending_posts() para publicar. Manter esta lista
# alinhada com o worker e o que evita o servidor descobrir um campo faltando so
# na hora do upload.
CONSULTA = (
    "SELECT p.id AS post_id, p.platform, p.orientation, p.scheduled_at,"
    " c.title AS clip_title, c.caption AS clip_caption, c.start, c.end, c.hook, c.score,"
    " c.yt_title, c.yt_description,"
    " c.path AS clip_path, c.path_wide AS clip_path_wide,"
    " c.thumb_path, c.thumb_vertical_path, c.idx AS clip_idx,"
    " e.source_url AS ep_source_url, e.channel AS ep_channel, e.meta AS ep_meta,"
    " e.title AS ep_title,"
    " ch.name AS canal_nome, ch.platform AS canal_plataforma"
    " FROM posts p"
    " JOIN clips c ON c.id = p.clip_id"
    " JOIN episodes e ON e.id = c.episode_id"
    " LEFT JOIN channels ch ON ch.id = p.channel_id"
    " WHERE p.status = 'pending'"
    " ORDER BY p.scheduled_at, p.id"
)


def escolher_midia(post: dict) -> tuple[str | None, str | None]:
    """O par (video, capa) que ESTE post vai usar.

    Mesma escolha do worker: horizontal usa o 16:9 e a capa 16:9; vertical usa o
    9:16 e a capa vertical, caindo na 16:9 quando o corte e antigo e nao tem.
    Resolver aqui evita mandar o arquivo errado — ou os dois, dobrando o pacote.
    """
    horizontal = (post.get("orientation") or "vertical") == "horizontal"
    video = post["clip_path_wide"] if horizontal else post["clip_path"]
    capa = post["thumb_path"] if horizontal else (post["thumb_vertical_path"] or post["thumb_path"])
    return video, capa


def exportar(destino: Path, limite: int | None, aplicar: bool) -> int:
    con = sqlite3.connect(settings.db_path)
    con.row_factory = sqlite3.Row
    posts = [dict(r) for r in con.execute(CONSULTA)]
    if limite:
        posts = posts[:limite]
    if not posts:
        print("Nenhum post pendente para exportar.")
        con.close()
        return 0

    pacote_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    raiz = destino / ("pacote_" + pacote_id)
    itens: list[tuple[dict, str, str | None, int]] = []
    sem_arquivo: list[tuple[int, str | None]] = []

    for post in posts:
        video, capa = escolher_midia(post)
        if not video or not Path(video).exists():
            sem_arquivo.append((post["post_id"], video))
            continue

        item = {
            # Chave estavel: reenviar o mesmo pacote nao duplica publicacao.
            "origem": "pc:" + pacote_id + ":" + str(post["post_id"]),
            "platform": post["platform"],
            "orientation": post.get("orientation") or "vertical",
            "scheduled_at": post["scheduled_at"],
            # Canal casado por NOME + PLATAFORMA, nunca por id: os ids do banco do
            # servidor sao outros, e casar por numero publicaria no canal errado.
            "canal_nome": post.get("canal_nome"),
            "canal_plataforma": post.get("canal_plataforma"),
            "clip": {
                "idx": post.get("clip_idx"),
                # start/end nao servem para publicar (o corte ja esta renderizado),
                # mas clips.start e NOT NULL no schema. Levar o valor real e melhor
                # que inventar zero: se um dia alguem olhar a linha no servidor, ela
                # diz de onde o corte saiu.
                "start": post.get("start"),
                "end": post.get("end"),
                "hook": post.get("hook"),
                "score": post.get("score"),
                "title": post.get("clip_title"),
                "caption": post.get("clip_caption"),
                "yt_title": post.get("yt_title"),
                "yt_description": post.get("yt_description"),
            },
            "episodio": {
                "title": post.get("ep_title"),
                # Credito da fonte. Sem estes tres o corte sai sem atribuicao e
                # passa a parecer reupload aos olhos de quem denuncia.
                "source_url": post.get("ep_source_url"),
                "channel": post.get("ep_channel"),
                "meta": post.get("ep_meta"),
            },
            "midia": {"video": "media/" + str(post["post_id"]) + "/video.mp4"},
        }
        if capa and Path(capa).exists():
            item["midia"]["capa"] = "media/" + str(post["post_id"]) + "/capa" + Path(capa).suffix
        itens.append((item, video, capa, post["post_id"]))

    print("pacote " + pacote_id)
    print("  posts pendentes .... " + str(len(posts)))
    print("  prontos para enviar  " + str(len(itens)))
    if sem_arquivo:
        print("  SEM ARQUIVO ........ " + str(len(sem_arquivo)) + " (ficam na fila do PC)")
        for pid, caminho in sem_arquivo[:5]:
            print("      post " + str(pid) + ": " + (caminho or "(sem caminho)"))

    total = sum(Path(v).stat().st_size for _, v, _, _ in itens)
    print("  tamanho ............ " + format(total / 1048576, ".0f") + " MB")

    if not aplicar:
        print("")
        print("Simulacao. Rode com --apply para gravar e tirar os posts da fila do PC.")
        con.close()
        return len(itens)

    (raiz / "media").mkdir(parents=True, exist_ok=True)
    for item, video, capa, pid in itens:
        (raiz / "media" / str(pid)).mkdir(parents=True, exist_ok=True)
        shutil.copy2(video, raiz / item["midia"]["video"])
        if "capa" in item["midia"]:
            shutil.copy2(capa, raiz / item["midia"]["capa"])

    manifesto = {
        "pacote": pacote_id,
        "gerado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "itens": [i for i, _, _, _ in itens],
    }
    (raiz / "manifesto.json").write_text(
        json.dumps(manifesto, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # So AGORA os posts saem da fila: se a copia falhar no meio, eles continuam
    # publicaveis pelo PC em vez de sumirem dos dois lados.
    con.executemany(
        "UPDATE posts SET status = 'exported' WHERE id = ? AND status = 'pending'",
        [(pid,) for _, _, _, pid in itens],
    )
    con.commit()
    con.close()

    print("")
    print("pacote em " + str(raiz))
    print(str(len(itens)) + " posts sairam da fila do PC (status 'exported').")
    print("")
    print("Envie e importe no servidor:")
    print("  rsync -avP " + str(raiz) + " servidor:/opt/dubflow/pacotes/")
    print("  ssh servidor 'cd /opt/dubflow && .venv/bin/python -m scripts.importar_pacote"
          " pacotes/pacote_" + pacote_id + " --apply'")
    return len(itens)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--destino", default="pacotes", help="pasta onde gravar (padrao: ./pacotes)")
    ap.add_argument("--limite", type=int, default=None, help="exporta no maximo N posts")
    ap.add_argument("--apply", action="store_true", help="grava (sem isso, so simula)")
    args = ap.parse_args()
    exportar(Path(args.destino), args.limite, args.apply)
