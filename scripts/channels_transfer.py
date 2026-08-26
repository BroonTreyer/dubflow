"""Migra canais + cofres de credenciais entre maquinas.

O banco (`data/dubflow.db`) e os cofres (`data/credentials.json` e
`data/channels/<id>/credentials.json`) ficam FORA do git — entao `git pull` nao
os carrega. Este script exporta tudo num arquivo portavel num PC e importa no
outro (o de render, que vira o servidor unico).

    exportar (neste PC):
        .venv\\Scripts\\python.exe -m scripts.channels_transfer export

    importar (no PC de render, depois de copiar o arquivo):
        .venv\\Scripts\\python.exe -m scripts.channels_transfer import

O arquivo (default data/channels_transfer.json, ja no gitignore por estar em
data/) CONTEM SEGREDOS (client secrets, tokens). Copie por canal seguro
(USB/LAN), importe no destino e APAGUE depois. Nunca comite.

Identidade: no import cada canal ganha um id novo; o cofre e reescrito sob o id
novo, entao o mapeamento canal->credenciais se mantem. Idempotente por
(plataforma, nome): reimportar pula o que ja existe (use --replace para recriar).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from app import credentials, db
from app.config import settings

DEFAULT_FILE = settings.data_dir / "channels_transfer.json"


def do_export(path: Path) -> int:
    db.init_db()
    channels = []
    for ch in db.list_channels():
        channels.append({
            "name": ch["name"], "platform": ch["platform"], "market": ch["market"],
            "niche": ch["niche"], "project": ch["project"],
            "posts_per_day": ch["posts_per_day"], "status": ch["status"],
            "credentials": credentials.load(ch["id"]),
        })
    payload = {
        "version": 1,
        "global_credentials": credentials.load(None),
        "channels": channels,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)  # segredos: so o dono le
    except OSError:
        pass
    n_creds = sum(1 for c in channels if c["credentials"])
    print(f"Exportado: {len(channels)} canais ({n_creds} com credenciais) + cofre global")
    print(f"Arquivo: {path}")
    print("!! CONTEM SEGREDOS: copie por canal seguro, importe no destino e APAGUE. Nunca comite.")
    return 0


def do_import(path: Path, replace: bool) -> int:
    if not path.exists():
        print(f"arquivo nao encontrado: {path}")
        return 1
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"arquivo invalido: {exc}")
        return 1

    db.init_db()
    existentes = {(c["platform"], c["name"]) for c in db.list_channels()}
    criados = pulados = 0
    for ch in data.get("channels", []):
        chave = (ch["platform"], ch["name"])
        if chave in existentes and not replace:
            print(f"[skip] ja existe: {ch['name']} ({ch['platform']}) — use --replace para recriar")
            pulados += 1
            continue
        cid = db.create_channel(
            ch["name"], ch["platform"], ch.get("market") or "BR",
            ch.get("niche"), ch.get("posts_per_day") or 3, ch.get("project"),
        )
        if ch.get("status") == "paused":
            db.update_channel(cid, status="paused")
        creds = ch.get("credentials") or {}
        if creds:
            credentials.save(creds, cid)
        criados += 1
        print(f"[ok] {ch['name']} ({ch['platform']}) id={cid} creds={sorted(creds)}")

    gc = data.get("global_credentials") or {}
    if gc:
        credentials.save(gc, None)
        print(f"[ok] cofre global: {sorted(gc)}")

    print(f"\nImportado: {criados} canais criados, {pulados} pulados.")
    print("Feito. Agora APAGUE o arquivo de transferencia (contem segredos).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Migra canais + credenciais entre maquinas.")
    ap.add_argument("acao", choices=["export", "import"])
    ap.add_argument("--file", type=Path, default=DEFAULT_FILE,
                    help=f"arquivo de transferencia (default {DEFAULT_FILE})")
    ap.add_argument("--replace", action="store_true",
                    help="no import, recria o canal mesmo se ja existir o par (plataforma, nome)")
    args = ap.parse_args()
    return do_export(args.file) if args.acao == "export" else do_import(args.file, args.replace)


if __name__ == "__main__":
    sys.exit(main())
