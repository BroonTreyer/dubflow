"""Credito da fonte na descricao de tudo que e publicado.

Por que isto existe: o dubflow republica trecho de video de terceiro. Sem credito
visivel, um corte e indistinguivel de reupload — e e assim que ele e tratado por
quem denuncia e pelo sistema de conteudo reaproveitado da plataforma. Com o link
do video original e o @ do canal na descricao, o corte se apresenta como corte.

Isso NAO substitui licenca nem autorizacao (o episodio tem `license_status` para
isso). Credito reduz atrito; nao cria direito de uso.

O bloco vai no FIM da descricao: os primeiros caracteres sao o que aparece no
feed e pertencem ao gancho, nao ao credito.
"""

from __future__ import annotations

import re
from typing import Any

from app.config import settings

# "@canal" quando o handle e conhecido. O YouTube moderno devolve uploader_id ja
# no formato @handle; versoes antigas devolviam o id cru do canal (UC...), que
# nao serve como mencao — por isso a checagem.
_HANDLE = re.compile(r"^@[\w.\-]{2,}$")


def handle(episode: dict[str, Any]) -> str:
    """O @ do canal de origem, ou string vazia se nao der para saber."""
    meta = episode.get("meta") or {}
    bruto = (meta.get("uploader_id") or "").strip()
    if _HANDLE.match(bruto):
        return bruto
    # Fallback: extrai o @ da URL do canal (…/@nome).
    url = (meta.get("channel_url") or "").strip()
    m = re.search(r"/(@[\w.\-]{2,})", url)
    return m.group(1) if m else ""


def credit_block(episode: dict[str, Any]) -> str:
    """Bloco de credito pronto para colar no fim da descricao/legenda.

    Devolve string vazia quando nao ha nada para creditar — nunca inventa canal
    nem monta um bloco pela metade, que ficaria pior que credito nenhum.
    """
    if not settings.attribution_enabled:
        return ""

    meta = episode.get("meta") or {}
    url = (episode.get("source_url") or meta.get("webpage_url") or "").strip()
    canal = (episode.get("channel") or meta.get("channel") or "").strip()
    arroba = handle(episode)

    if not url and not canal:
        return ""

    linhas = [settings.attribution_header.strip()]
    if canal:
        # O @ entre parenteses vira mencao clicavel no YouTube e no Instagram.
        linhas.append(f"Canal: {canal}" + (f" ({arroba})" if arroba else ""))
    if url:
        linhas.append(f"Episodio completo: {url}")
    if settings.attribution_footer.strip():
        linhas.append(settings.attribution_footer.strip())
    return "\n".join(linhas)


def apply(texto: str, episode: dict[str, Any] | None) -> str:
    """Acrescenta o credito ao fim do texto, sem duplicar se ja estiver la."""
    bloco = credit_block(episode or {})
    if not bloco:
        return texto
    base = (texto or "").rstrip()
    # Reprocessar ou republicar nao pode empilhar dois blocos de credito.
    url = (episode or {}).get("source_url") or ""
    if url and url in base:
        return base
    if settings.attribution_header.strip() and settings.attribution_header.strip() in base:
        return base
    return f"{base}\n\n{bloco}" if base else bloco
