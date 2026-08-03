"""
Cache JSON atomica compartida.

El patron correcto (tmp + os.replace) vivia solo en faa_govinfo; los otros
caches escribian directo al archivo final, asi que un corte a mitad de
escritura dejaba un JSON truncado que el siguiente run descartaba en silencio
(json.JSONDecodeError -> cache vacio) y forzaba re-descargar miles de
cuerpos/PDFs.
"""

import json
import os


def load_json_cache(path: str, default=None):
    """Lee un cache JSON; un archivo ausente o corrupto devuelve `default`
    (dict vacio si no se pasa otro)."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {} if default is None else default


def save_json_cache(path: str, data, *, indent: int | None = None) -> None:
    """Escritura atomica: volcar a `<path>.tmp` y os.replace. Un corte deja
    el cache anterior intacto, nunca uno truncado. Best-effort: un fallo de
    disco no debe tumbar el scrape."""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
        os.replace(tmp, path)
    except OSError:
        pass
