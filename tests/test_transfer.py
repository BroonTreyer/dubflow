"""Teste do migrador de canais + credenciais entre maquinas (scripts.channels_transfer).

Simula duas maquinas trocando o DATA_DIR: exporta na origem, importa no destino
(banco vazio) e confere que campos, credenciais por canal, isolamento de
identidade e o cofre global sobrevivem — e que reimportar nao duplica.

    py -m tests.test_transfer
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DUBFLOW_PASSWORD", "senha-de-teste")
os.environ.setdefault("SECRET_KEY", "chave-fixa-para-teste")

from app.config import settings  # noqa: E402
from app import credentials, db  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} -> {detail}")
        failures.append(label)


def _use_datadir(d: Path) -> None:
    """Aponta o app para um DATA_DIR novo (simula trocar de maquina)."""
    settings.data_dir = d
    settings.db_path = d / "dubflow.db"
    for sub in ("episodes", "archive", "tmp", "logs"):
        (d / sub).mkdir(parents=True, exist_ok=True)


def main() -> int:
    from scripts import channels_transfer as ct

    # -------- origem: cria canais + credenciais + cofre global --------
    _use_datadir(Path(tempfile.mkdtemp(prefix="dub_src_")))
    db.init_db()
    c1 = db.create_channel("Fin US 1", "youtube", market="US", niche="financas",
                           posts_per_day=4, project="proj-a")
    credentials.save({"YOUTUBE_CLIENT_ID": "cid-1", "YOUTUBE_CLIENT_SECRET": "sec-1",
                      "YOUTUBE_REFRESH_TOKEN": "ref-1"}, c1)
    c2 = db.create_channel("Games BR", "tiktok", market="BR", niche="games")
    db.update_channel(c2, status="paused")
    credentials.save({"TIKTOK_ACCESS_TOKEN": "tok-2"}, c2)
    credentials.save({"PUBLIC_BASE_URL": "https://x", "IG_ACCESS_TOKEN": "glob"}, None)

    print("export")
    xfer = settings.data_dir / "xfer.json"
    ct.do_export(xfer)
    check("export gera arquivo", xfer.exists())
    payload = json.loads(xfer.read_text(encoding="utf-8"))
    check("export tem os 2 canais", len(payload["channels"]) == 2, len(payload["channels"]))
    check("export inclui credenciais do canal",
          any(c["credentials"].get("YOUTUBE_CLIENT_ID") == "cid-1" for c in payload["channels"]))
    check("export inclui o cofre global",
          payload["global_credentials"].get("PUBLIC_BASE_URL") == "https://x")

    # -------- destino: outra maquina, banco vazio --------
    print("import (maquina destino, banco vazio)")
    _use_datadir(Path(tempfile.mkdtemp(prefix="dub_dst_")))
    db.init_db()
    check("destino comeca sem canais", len(db.list_channels()) == 0)
    ct.do_import(xfer, replace=False)

    chans = {c["name"]: c for c in db.list_channels()}
    check("importou os 2 canais", len(chans) == 2, list(chans))
    fin = chans.get("Fin US 1")
    check("canal preserva campos",
          bool(fin) and fin["platform"] == "youtube" and fin["market"] == "US"
          and fin["project"] == "proj-a" and fin["posts_per_day"] == 4, fin)
    check("credencial do canal migrada",
          credentials.get("YOUTUBE_REFRESH_TOKEN", fin["id"]) == "ref-1")
    check("identidade nao vaza entre canais (games nao herda o ref do youtube)",
          credentials.get("YOUTUBE_REFRESH_TOKEN", chans["Games BR"]["id"]) == "")
    check("canal pausado preserva o status", chans["Games BR"]["status"] == "paused")
    check("cofre global migrado", credentials.get("PUBLIC_BASE_URL") == "https://x")

    print("idempotencia")
    ct.do_import(xfer, replace=False)
    check("reimport sem --replace nao duplica", len(db.list_channels()) == 2,
          len(db.list_channels()))

    if failures:
        print(f"\n{len(failures)} FALHOU: {failures}")
        return 1
    print("\nmigrador de canais OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
