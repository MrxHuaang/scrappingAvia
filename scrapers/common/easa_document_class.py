"""
Clase de documento de una publicacion EASA: AD, EAD, PAD, SIB, SD o ADR.

El Safety Publications Tool mezcla en un solo listado todas sus publicaciones,
no solo Airworthiness Directives. La clase se lee de dos senales del propio
listado, en este orden:

1. **La cola del link del adjunto.** El ultimo segmento de cada link `blob`
   empieza por la clase de la publicacion a la que pertenece el adjunto, aunque
   el archivo sea de otra (el ZIP del PAD de un AD final cuelga como
   `.../EASA_PAD_26_031.zip/AD_2026-0052R2_2`):

       https://ad.easa.europa.eu/blob/EASA_SIB_2007_12.pdf/SIB_2007-12_1
       https://ad.easa.europa.eu/blob/EASA_AD_2024_0109_CN.pdf/AD_2024-0109-CN_1
       https://ad.easa.europa.eu/blob/EASA_PAD_26_092.pdf/PAD_26-092_1
       https://ad.easa.europa.eu/blob/2001-08-52.pdf/EAD_US-2001-08-52_1

   En produccion clasifica 18.478 de 18.480 filas EASA.
2. **El icono de clase** del Subject (`<img src=".../adclass_sib.gif"
   alt="SIB">`), que solo llevan las clases distintas de AD.

Una senal que no es una de las seis clases no clasifica nada: la clase queda
`None` (desconocida) en vez de adivinarse. Un AD comun sin adjuntos no trae
ni link ni icono y por eso tambien queda `None`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

#: Clases que se escriben en `ads.document_class`. El contrato con ad-scrapper
#: es este conjunto exacto; cualquier otra cosa es "desconocida".
DOCUMENT_CLASSES = frozenset({"AD", "EAD", "PAD", "SIB", "SD", "ADR"})

# Cola del link: ".../<CLASE>_<numero>_<n>" -> CLASE.
_LINK_TAIL_RE = re.compile(r"/([A-Z]+)_[^/]*$")


def class_from_link(href: str | None) -> str | None:
    """Clase declarada por el ultimo segmento de un link de adjunto EASA."""
    m = _LINK_TAIL_RE.search((href or "").strip())
    if not m:
        return None
    cls = m.group(1)
    return cls if cls in DOCUMENT_CLASSES else None


def class_from_icon(alt: str | None) -> str | None:
    """Clase declarada por el `alt` del icono de clase del Subject."""
    cls = (alt or "").strip().upper()
    return cls if cls in DOCUMENT_CLASSES else None


def document_class(links: Iterable[str | None] = (), icon_alt: str | None = None) -> str | None:
    """
    Clase de la publicacion: la del primer link que la declare y, si ninguno
    lo hace, la del icono. `None` cuando ninguna senal es una clase conocida.
    """
    for href in links:
        cls = class_from_link(href)
        if cls:
            return cls
    return class_from_icon(icon_alt)
