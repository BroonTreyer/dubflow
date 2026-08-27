"""Fontes autorizadas: de onde o sistema pode puxar episodio sozinho.

So entra aqui canal cuja politica de cortes esta PUBLICADA. Cada entrada carrega
a regra daquela fonte, porque elas nao sao iguais: o Vaca Cast exige 48h de
espera, o Flow exige o programa ter acabado e link do completo na descricao.

Nao adivinhe autorizacao. Sem politica publica (ou um "sim" por escrito), o canal
NAO entra nesta lista — cortar sem permissao e o que derruba canal por conteudo
reaproveitado. `license` diz como o episodio nasce; `licensed` significa que a
politica da fonte cobre o uso, nao que voce e dono do conteudo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Source:
    name: str
    url: str                       # aba /videos do canal
    policy_url: str                # onde a regra esta publicada (auditavel)
    min_age_hours: int             # janela de espera exigida pela fonte
    license: str = "licensed"      # nasce assim no banco (ver db.LICENSE_STATES)
    min_minutes: int = 20          # abaixo disso ja e corte, nao episodio
    max_minutes: int = 240         # acima disso o custo de GPU nao compensa
    min_views: int = 0             # 0 = sem piso
    enabled: bool = True
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "url": self.url, "policy_url": self.policy_url,
                "min_age_hours": self.min_age_hours, "license": self.license}


# Verificadas em 26/08/2026. Ao acrescentar, cole o link da politica em
# `policy_url` — e o que permite auditar depois por que aquele canal esta aqui.
SOURCES: list[Source] = [
    Source(
        name="Flow Podcast",
        url="https://www.youtube.com/@FlowPodcast/videos",
        policy_url="https://flowpodcast.com.br/cortes/regras",
        # A regra e "esperar o programa acabar". Puxamos da aba /videos, onde o
        # VOD ja esta fechado, mas 12h de margem evita pegar corte no ar ou
        # republicacao. Tres avisos derrubam o canal, entao a margem e barata.
        min_age_hours=12,
        min_minutes=25,
        notes="Link do episodio completo na descricao e obrigatorio (attribution.py ja faz).",
    ),
    Source(
        name="Vaca Cast",
        url="https://www.youtube.com/@vacacast/videos",
        policy_url="https://www.vacacast.com.br/tenha-um-canal-de-cortes",
        # A politica pede minimo de 48h antes de veicular em qualquer plataforma.
        min_age_hours=48,
        min_minutes=20,
        notes="Nao copiar a identidade do canal oficial de cortes.",
    ),
]


def enabled_sources() -> list[Source]:
    return [s for s in SOURCES if s.enabled]


def by_name(name: str) -> Source | None:
    alvo = (name or "").strip().lower()
    return next((s for s in SOURCES if s.name.lower() == alvo), None)
