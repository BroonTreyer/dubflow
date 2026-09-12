"""Importa um pacote vindo do PC e o coloca na fila de publicacao do servidor.

    .venv/bin/python -m scripts.importar_pacote pacotes/pacote_2026...      # simula
    .venv/bin/python -m scripts.importar_pacote pacotes/pacote_2026... --apply

O PC processa e agenda; aqui so se publica. A importacao recria as linhas de
episodes/clips/posts que o worker DESTE servidor ja sabe ler — nao existe
publicador novo, e por isso nao existe um segundo caminho de publicacao para
manter em dia.

E idempotente: cada item traz uma chave `origem` unica, e reenviar o mesmo pacote
nao duplica publicacao. O indice unico em posts(origem) garante isso ate se dois
processos importarem ao mesmo tempo.

O canal e casado por NOME + PLATAFORMA, nunca por id. Os ids do banco do PC e do
servidor sao independentes; casar por numero publicaria no canal errado, que e um
erro silencioso e irreversivel. Canal que nao existir aqui INTERROMPE a
importacao em vez de virar post sem canal.
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


def mapear_canais(con: sqlite3.Connection, itens: list[dict]) -> dict[tuple, int]:
    """Nome+plataforma -> id do canal AQUI. Falha alto se faltar algum."""
    querido = {(i.get("canal_nome"), i.get("canal_plataforma"))
               for i in itens if i.get("canal_nome")}
    mapa: dict[tuple, int] = {}
    faltando = []
    for nome, plataforma in querido:
        linha = con.execute(
            "SELECT id FROM channels WHERE name = ? AND platform = ?", (nome, plataforma)
        ).fetchone()
        if linha:
            mapa[(nome, plataforma)] = linha[0]
        else:
            faltando.append(nome + " (" + str(plataforma) + ")")
    if faltando:
        raise SystemExit(
            "Canais do pacote que nao existem neste servidor:\n  - "
            + "\n  - ".join(faltando)
            + "\n\nCadastre-os no painel (mesmo nome e plataforma) e reautorize antes de importar."
        )
    return mapa


def episodio_para(con: sqlite3.Connection, ep: dict, aplicar: bool) -> int | None:
    """Reaproveita o episodio pela URL de origem, ou cria um novo.

    O episodio existe aqui so para carregar o CREDITO da fonte (url e canal
    original), que o publicador cola no fim da descricao. Nao ha pipeline de
    processamento neste servidor.
    """
    url = ep.get("source_url")
    if url:
        achado = con.execute("SELECT id FROM episodes WHERE source_url = ?", (url,)).fetchone()
        if achado:
            return achado[0]
    if not aplicar:
        return None
    agora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur = con.execute(
        "INSERT INTO episodes (source_url, title, channel, meta, status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, 'done', ?, ?)",
        (url, ep.get("title"), ep.get("channel"), ep.get("meta"), agora, agora),
    )
    return cur.lastrowid


def importar(pacote: Path, aplicar: bool) -> int:
    manifesto = json.loads((pacote / "manifesto.json").read_text(encoding="utf-8"))
    itens = manifesto.get("itens", [])
    if not itens:
        print("Pacote sem itens.")
        return 0

    con = sqlite3.connect(settings.db_path)
    mapa = mapear_canais(con, itens)

    ja_existiam = 0
    novos = []
    for item in itens:
        existe = con.execute(
            "SELECT 1 FROM posts WHERE origem = ?", (item["origem"],)
        ).fetchone()
        if existe:
            ja_existiam += 1
        else:
            novos.append(item)

    print("pacote " + str(manifesto.get("pacote")))
    print("  itens no manifesto ... " + str(len(itens)))
    print("  ja importados ........ " + str(ja_existiam))
    print("  a importar ........... " + str(len(novos)))

    if not aplicar:
        print("")
        print("Simulacao. Rode com --apply para gravar.")
        con.close()
        return len(novos)

    destino_raiz = Path(settings.data_dir) / "importados" if hasattr(settings, "data_dir") \
        else Path(settings.db_path).parent / "importados"
    agora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    gravados = 0

    for item in novos:
        origem = item["origem"]
        pasta = destino_raiz / origem.replace(":", "_")
        pasta.mkdir(parents=True, exist_ok=True)

        video_destino = pasta / "video.mp4"
        shutil.copy2(pacote / item["midia"]["video"], video_destino)
        capa_destino = None
        if "capa" in item["midia"]:
            capa_origem = pacote / item["midia"]["capa"]
            capa_destino = pasta / capa_origem.name
            shutil.copy2(capa_origem, capa_destino)

        ep_id = episodio_para(con, item.get("episodio", {}), aplicar=True)
        clip = item.get("clip", {})
        vertical = item["orientation"] != "horizontal"
        cur = con.execute(
            "INSERT INTO clips (episode_id, idx, start, end, hook, score, title, caption,"
            " yt_title, yt_description,"
            " path, path_wide, thumb_path, thumb_vertical_path, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'done', ?)",
            (
                ep_id, clip.get("idx"),
                # NOT NULL no schema; vem do pacote em vez de ser inventado.
                clip.get("start") or 0, clip.get("end") or 0,
                clip.get("hook"), clip.get("score"),
                clip.get("title"), clip.get("caption"),
                clip.get("yt_title"), clip.get("yt_description"),
                # O worker escolhe o arquivo pela orientacao do post. Gravar o
                # mesmo caminho nas duas colunas faria o corte vertical ser
                # publicado como horizontal se alguem trocar a orientacao depois.
                str(video_destino) if vertical else None,
                None if vertical else str(video_destino),
                str(capa_destino) if capa_destino and not vertical else None,
                str(capa_destino) if capa_destino and vertical else None,
                agora,
            ),
        )
        clip_id = cur.lastrowid
        canal_id = mapa.get((item.get("canal_nome"), item.get("canal_plataforma")))
        con.execute(
            "INSERT INTO posts (clip_id, platform, status, scheduled_at, created_at,"
            " attempts, orientation, channel_id, origem)"
            " VALUES (?, ?, 'pending', ?, ?, 0, ?, ?, ?)",
            (clip_id, item["platform"], item.get("scheduled_at"), agora,
             item["orientation"], canal_id, origem),
        )
        gravados += 1

    con.commit()
    con.close()
    print("")
    print(str(gravados) + " posts entraram na fila deste servidor.")
    print("A publicacao segue o scheduled_at que veio do PC.")
    return gravados


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pacote", help="pasta do pacote (a que contem manifesto.json)")
    ap.add_argument("--apply", action="store_true", help="grava (sem isso, so simula)")
    args = ap.parse_args()
    importar(Path(args.pacote), args.apply)
