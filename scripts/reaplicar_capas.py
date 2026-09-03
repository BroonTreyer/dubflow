r"""Reaplica a capa propria nos videos ja publicados sem ela.

    .venv\Scripts\python.exe -m scripts.reaplicar_capas            # simulacao
    .venv\Scripts\python.exe -m scripts.reaplicar_capas --apply
    .venv\Scripts\python.exe -m scripts.reaplicar_capas --canal 6 --apply

Existe porque a capa e opcional no caminho de publicacao: `_set_thumbnail` so
avisa e segue quando o YouTube recusa (HTTP 403 "doesn't have permissions to
upload and set custom video thumbnails"), entao o video sobe com a capa
automatica do YouTube e nada volta a tentar. A recusa vem de canal sem
verificacao por telefone — corrigido o canal, os videos antigos continuavam
com a capa errada.

`thumbnails.set` custa ~50 unidades (contra ~1600 de um upload), entao passar
o acervo inteiro e barato. Idempotente: reaplicar uma capa correta nao faz mal.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.publishers.youtube import THUMBNAIL_URL, _access_token  # noqa: E402


def _capa(post: dict) -> Path | None:
    """A capa acompanha a orientacao do post, igual ao caminho de publicacao."""
    if (post.get("orientation") or "vertical") == "horizontal":
        escolha = post.get("thumb_path")
    else:
        escolha = post.get("thumb_vertical_path") or post.get("thumb_path")
    if not escolha:
        return None
    caminho = Path(escolha)
    return caminho if caminho.exists() else None


def _publicados(canal: int | None) -> list[dict]:
    sql = ("SELECT p.id, p.channel_id, p.remote_id, p.orientation, p.permalink,"
           " c.thumb_path, c.thumb_vertical_path"
           " FROM posts p JOIN clips c ON c.id = p.clip_id"
           " WHERE p.status = 'published' AND p.platform = 'youtube'"
           "   AND p.remote_id IS NOT NULL")
    args: list = []
    if canal:
        sql += " AND p.channel_id = ?"
        args.append(canal)
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY p.id", args)]


def reaplicar(canal: int | None, aplicar: bool) -> tuple[int, int]:
    posts = _publicados(canal)
    tokens: dict[int | None, str | None] = {}
    ok = falhou = 0
    for post in posts:
        capa = _capa(post)
        if capa is None:
            print(f"  post {post['id']}: sem arquivo de capa — pulado")
            continue
        cid = post["channel_id"]
        if cid not in tokens:
            tokens[cid] = _access_token(cid)
        token = tokens[cid]
        if token is None:
            print(f"  post {post['id']}: canal {cid} sem token valido — pulado")
            falhou += 1
            continue
        if not aplicar:
            print(f"  post {post['id']} ({post['remote_id']}): aplicaria {capa.name}")
            ok += 1
            continue
        with capa.open("rb") as fh:
            r = requests.post(THUMBNAIL_URL,
                              params={"videoId": post["remote_id"], "uploadType": "media"},
                              headers={"Authorization": f"Bearer {token}",
                                       "Content-Type": "image/jpeg"},
                              data=fh, timeout=120)
        if r.status_code == 200:
            print(f"  post {post['id']} ({post['remote_id']}): capa aplicada")
            ok += 1
        else:
            print(f"  post {post['id']} ({post['remote_id']}): HTTP {r.status_code} {r.text[:120]}")
            falhou += 1
    return ok, falhou


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--canal", type=int, default=None, help="so este canal")
    ap.add_argument("--apply", action="store_true", help="aplica (sem isso, so simula)")
    args = ap.parse_args()
    bons, ruins = reaplicar(args.canal, args.apply)
    verbo = "aplicadas" if args.apply else "seriam aplicadas"
    print(f"\n{bons} capas {verbo}, {ruins} falharam.")
