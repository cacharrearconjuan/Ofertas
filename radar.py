#!/usr/bin/env python3
"""
Radar de chollos
================
Vigila Chollometro, se queda solo con las ofertas que encajan con tus
categorías (palabras.txt) y avisa por Telegram cuando la comunidad las ha
votado por encima de un mínimo de grados (200° por defecto).

Una oferta nace a 0° y va subiendo con los votos, así que el radar guarda
las que encajan "en seguimiento" y vuelve a mirar sus grados cada cierto
tiempo hasta que superan el mínimo o caducan.

En las ofertas de Amazon añade Keepa (historial de precios) para saber si
la rebaja es real: siempre el enlace y la gráfica, y con una clave de la
API de Keepa, además, un veredicto automático.

Solo usa la librería estándar de Python (3.9 o superior).

Uso:
    python3 radar.py               # ronda normal
    python3 radar.py --prueba      # manda un mensaje de prueba a Telegram
    python3 radar.py --diagnostico # muestra lo que lee, sin avisar ni guardar
    python3 radar.py --palabras    # comprueba palabras.txt y enseña cómo lo entiende

Variables de entorno:
    TELEGRAM_TOKEN       token del bot (BotFather)
    TELEGRAM_CHAT_ID     id del grupo (empieza por -100...)
    TELEGRAM_THREAD_ID   opcional: tema por defecto si el grupo usa temas
    KEEPA_API_KEY        opcional: clave de la API de Keepa (de pago)
    RADAR_CONFIG         opcional: ruta a otro config.json
    RADAR_PALABRAS       opcional: ruta a otro palabras.txt
    RADAR_DATOS          opcional: carpeta donde guardar estado y ofertas
"""
from __future__ import annotations

import argparse
import email.utils
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    ZoneInfo = None

BASE = Path(__file__).resolve().parent
RUTA_CONFIG = Path(os.environ.get("RADAR_CONFIG", BASE / "config.json"))
RUTA_PALABRAS = Path(os.environ.get("RADAR_PALABRAS", BASE / "palabras.txt"))
DIR_DATOS = Path(os.environ.get("RADAR_DATOS", BASE / "datos"))
RUTA_ESTADO = DIR_DATOS / "estado.json"
RUTA_OFERTAS = DIR_DATOS / "ofertas.json"

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

TIENDAS_CONOCIDAS = [
    "AliExpress", "Amazon", "PcComponentes", "MediaMarkt", "El Corte Inglés",
    "Carrefour", "Fnac", "GAME", "Worten", "Coolmod", "Miravia",
    "eBay", "Lidl", "Alcampo", "Decathlon",
    "PlayStation Store", "Nintendo eShop", "Steam", "Epic Games",
    "Microsoft Store", "Back Market", "Wallapop", "Temu", "Shein", "Aussar",
    "Neobyte", "LDLC", "Vodafone", "Orange", "Movistar",
]


def log(*args):
    print(*args, flush=True)


# --------------------------------------------------------------------------
# Texto y patrones
# --------------------------------------------------------------------------
def normalizar(texto: str | None) -> str:
    """Minúsculas, sin tildes y con espacios simples."""
    t = unicodedata.normalize("NFKD", texto or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t.lower()).strip()


def quitar_html(texto: str | None) -> str:
    t = re.sub(r"<[^>]+>", " ", texto or "")
    return re.sub(r"\s+", " ", html.unescape(t)).strip()


def patron(termino: str) -> str:
    """Convierte un término en una expresión regular de palabra completa."""
    if termino.startswith("re:"):
        return termino[3:]
    t = re.escape(normalizar(termino)).replace("\\ ", " ").replace(" ", r"\s+")
    return rf"(?<![a-z0-9]){t}(?![a-z0-9])"


def compilar_lista(terminos):
    terminos = [t for t in (terminos or []) if t]
    if not terminos:
        return None
    return re.compile("|".join(f"(?:{patron(t)})" for t in terminos))


def slug(texto: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", normalizar(texto)).strip("_") or "categoria"


# --------------------------------------------------------------------------
# palabras.txt
# --------------------------------------------------------------------------
RE_OPCION = re.compile(r"^(grados|precio\s*m[aá]x(?:imo)?|precio\s*m[ií]n(?:imo)?|tema|aviso|necesita)\s*:\s*(.+)$", re.I)


def _partir(linea: str) -> list[str]:
    if linea.lstrip("+-").startswith("re:"):
        return [linea]
    return [p.strip() for p in linea.split(",") if p.strip()]


NO_SON_JUEGOS: list[str] = []   # palabras de hardware/accesorios (sección [No son juegos])


def leer_palabras(ruta: Path = RUTA_PALABRAS) -> tuple[list[dict], list[str], list[str]]:
    """Devuelve (categorías, exclusiones globales, avisos de formato)."""
    NO_SON_JUEGOS.clear()
    cats: list[dict] = []
    globales: list[str] = []
    problemas: list[str] = []
    actual = None
    for n, bruta in enumerate(ruta.read_text(encoding="utf-8").splitlines(), 1):
        linea = bruta.strip()
        if not linea or linea.startswith("#"):
            continue
        m = re.fullmatch(r"\[(.+)\]", linea)
        if m:
            cab = m.group(1).strip()
            if "excluir siempre" in normalizar(cab):
                actual = "GLOBAL"
                continue
            if "no son juegos" in normalizar(cab):
                actual = "NO_JUEGOS"
                continue
            emoji, nombre = "🔹", cab
            partes = cab.split(maxsplit=1)
            if len(partes) == 2 and not partes[0][0].isalnum():
                emoji, nombre = partes
            ids = {c["id"] for c in cats}
            cid = slug(nombre)
            while cid in ids:
                cid += "_2"
            actual = {"id": cid, "nombre": nombre, "emoji": emoji, "activo": True,
                      "incluir": [], "excluir": [], "forzar": [], "avisos": [], "necesita": [],
                      "grados": None, "precio_max": None, "precio_min": None, "tema": None}
            cats.append(actual)
            continue
        if actual is None:
            problemas.append(f"línea {n}: «{linea}» está fuera de una [categoría]; se ignora")
            continue
        if actual == "GLOBAL":
            globales += [p.lstrip("-") for p in _partir(linea)]
            continue
        if actual == "NO_JUEGOS":
            NO_SON_JUEGOS.extend(_partir(linea))
            continue
        if normalizar(linea) in ("apagada", "apagado", "desactivada"):
            actual["activo"] = False
            continue
        op = RE_OPCION.match(linea)
        if op:
            clave, valor = normalizar(op.group(1)).replace(" ", ""), op.group(2).strip()
            if clave == "necesita":
                actual["necesita"] += [p for p in _partir(valor)]
                continue
            if clave == "aviso":
                if "=>" not in valor:
                    problemas.append(f"línea {n}: el aviso necesita «palabras => texto»")
                    continue
                si, texto = valor.split("=>", 1)
                actual["avisos"].append({"si": _partir(si), "texto": texto.strip()})
                continue
            numero = a_numero(re.sub(r"[^\d,.\-]", "", valor))
            if numero is None:
                problemas.append(f"línea {n}: «{valor}» no es un número")
                continue
            campo = {"grados": "grados", "tema": "tema"}.get(clave) or (
                "precio_max" if clave.startswith("preciomax") else "precio_min")
            actual[campo] = int(numero) if campo == "tema" else numero
            continue
        for p in _partir(linea):
            if p.startswith("-"):
                actual["excluir"].append(p[1:].strip())
            elif p.startswith("+"):
                actual["forzar"].append(p[1:].strip())
            else:
                actual["incluir"].append(p)
    for c in cats:
        if c["activo"] and not c["incluir"] and not c["forzar"]:
            problemas.append(f"categoría «{c['nombre']}» no tiene palabras")
    return cats, globales, problemas


def compilar_categorias(cats_txt: list[dict]) -> list[dict]:
    salida = []
    for c in cats_txt:
        if not c["activo"]:
            continue
        try:
            salida.append({
                **c,
                "re_incluir": compilar_lista(c["incluir"]),
                "re_excluir": compilar_lista(c["excluir"]),
                "re_forzar": compilar_lista(c["forzar"]),
                "re_necesita": compilar_lista(c.get("necesita")),
                "re_avisos": [(compilar_lista(a["si"]), a["texto"]) for a in c["avisos"]],
            })
        except re.error as ex:
            log(f"⚠️ Categoría «{c['nombre']}»: expresión regular no válida ({ex}); se salta")
    return salida


# --------------------------------------------------------------------------
# Precios, fechas y tiendas
# --------------------------------------------------------------------------
RE_PRECIO = re.compile(r"(\d{1,3}(?:[.\s]\d{3})+(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)\s?(?:€|eur\b|euros\b)", re.I)


def a_numero(valor) -> float | None:
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    s = str(valor).strip().replace("€", "").replace("EUR", "").replace("\xa0", "").strip()
    if not s:
        return None
    if "," in s and "." in s:          # 1.299,99
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:                      # 29,99
        s = s.replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+", s):  # 1.299
        s = s.replace(".", "")
    s = s.replace(" ", "")
    try:
        return float(s)
    except ValueError:
        return None


def precio_en_texto(texto: str) -> float | None:
    m = RE_PRECIO.search(texto or "")
    return a_numero(m.group(1)) if m else None


def formatear_precio(n: float | None) -> str:
    if n is None:
        return ""
    if n == 0:
        return "GRATIS"
    if abs(n - round(n)) < 0.005:
        return f"{int(round(n)):,} €".replace(",", ".")
    entero, dec = f"{n:,.2f}".split(".")
    return f"{entero.replace(',', '.')},{dec} €"


def leer_fecha(texto: str | None) -> float | None:
    if not texto:
        return None
    try:
        return email.utils.parsedate_to_datetime(texto).timestamp()
    except (TypeError, ValueError, IndexError):
        pass
    try:
        return datetime.fromisoformat(texto.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def adivinar_tienda(*textos) -> str | None:
    junto = normalizar(" ".join(t for t in textos if t))
    for tienda in TIENDAS_CONOCIDAS:
        if re.search(patron(tienda), junto):
            return tienda
    return None


def id_de_enlace(url: str | None) -> str | None:
    """En Chollometro los enlaces acaban en -<número>; ese número es el id del hilo."""
    if not url:
        return None
    m = re.search(r"-(\d{4,})(?:[/?#]|$)", url)
    return m.group(1) if m else None


def es_amazon(o: dict) -> bool:
    return "amazon" in normalizar(o.get("tienda")) or "amazon." in (o.get("destino") or "") \
        or "amzn." in (o.get("destino") or "")


# --------------------------------------------------------------------------
# Descargas
# --------------------------------------------------------------------------
def descargar(url: str, intentos: int = 2) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.9",
    })
    ultimo = None
    for i in range(intentos + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as ex:
            ultimo = ex
            if ex.code in (401, 403, 404, 410):
                break
        except (urllib.error.URLError, TimeoutError, ConnectionError) as ex:
            ultimo = ex
        time.sleep(3 * (i + 1))
    raise ultimo or RuntimeError(f"No se pudo descargar {url}")


class _SinRedireccion(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_abridor_sin_redireccion = urllib.request.build_opener(_SinRedireccion)


def seguir_redirecciones(url: str, saltos: int = 5) -> str:
    """Sigue redirecciones una a una sin descargar la página final (para sacar el ASIN)."""
    for _ in range(saltos):
        if extraer_asin(url):
            return url
        req = urllib.request.Request(url, headers={"User-Agent": UA}, method="GET")
        try:
            with _abridor_sin_redireccion.open(req, timeout=15) as r:
                cuerpo = r.read(400_000).decode("utf-8", "replace")
                m = re.search(r'https?:(?:\\?/){2}(?:www\.)?(?:amazon\.[a-z.]+|amzn\.(?:to|eu))(?:\\?/)[^"\'\s<>]+', cuerpo)
                return html.unescape(m.group(0).replace("\\/", "/")) if m else r.geturl()
        except urllib.error.HTTPError as ex:
            destino = ex.headers.get("Location") if ex.code in (301, 302, 303, 307, 308) else None
            if not destino:
                return url
            url = urllib.parse.urljoin(url, destino)
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            return url
    return url


def _es_de_la_fuente(url: str) -> bool:
    host = urllib.parse.urlparse(url or "").netloc.lower()
    return any(x in host for x in ("chollometro", "pepper", "ofertitas", "hardzone"))


def destino_real(url: str, saltos: int = 6) -> str | None:
    """Convierte el enlace de salida de la fuente (chollometro.com/visit/...) en el enlace real de la tienda,
    para que al reenviar el aviso no aparezca la web de origen. Devuelve None si no se puede."""
    for _ in range(saltos):
        if not url:
            return None
        if not _es_de_la_fuente(url):
            return url
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        dentro = next((q[k][0] for k in ("url", "u", "target", "dest") if q.get(k)), None)
        if dentro and dentro.startswith("http"):
            url = dentro
            continue
        req = urllib.request.Request(url, headers={"User-Agent": UA}, method="GET")
        try:
            with _abridor_sin_redireccion.open(req, timeout=15) as r:
                cuerpo = r.read(300_000).decode("utf-8", "replace")
                m = re.search(r'(?:url=|location(?:\.href)?\s*=\s*["\']|href=["\'])(https?://[^"\'\s<>]+)', cuerpo, re.I)
                nuevo = html.unescape(m.group(1)) if m else None
                if not nuevo or nuevo == url:
                    return None
                url = nuevo
        except urllib.error.HTTPError as ex:
            loc = ex.headers.get("Location") if ex.code in (301, 302, 303, 307, 308) else None
            if not loc:
                return None
            url = urllib.parse.urljoin(url, loc)
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            return None
    return None if _es_de_la_fuente(url) else url


# --------------------------------------------------------------------------
# Lectura del RSS
# --------------------------------------------------------------------------
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


RE_ENLACE_TIENDA = re.compile(r'href=["\'](https?://(?:www\.)?(?:amazon\.[a-z.]+|amzn\.(?:to|eu)|a\.co)/[^"\']+)', re.I)


def leer_rss(datos: bytes):
    raiz = ET.fromstring(datos)
    for it in (e for e in raiz.iter() if _local(e.tag) in ("item", "entry")):
        o = {"titulo": "", "enlace": "", "guid": "", "descripcion": "",
             "cats_rss": [], "tienda": None, "precio": None, "imagen": None,
             "fecha": None, "grados": None, "tipo": None, "destino": None}
        for h in it:
            n, txt, at = _local(h.tag), (h.text or "").strip(), h.attrib
            if n == "title":
                o["titulo"] = html.unescape(txt)
            elif n == "link":
                o["enlace"] = txt or at.get("href", "")
            elif n in ("guid", "id"):
                o["guid"] = txt
            elif n in ("description", "summary", "encoded", "content") and len(txt) > len(o["descripcion"]):
                o["descripcion"] = txt
            elif n == "category":
                o["cats_rss"].append(txt or at.get("term", ""))
            elif n in ("pubdate", "published", "updated", "date") and not o["fecha"]:
                o["fecha"] = leer_fecha(txt)
            elif n == "merchant":
                o["tienda"] = at.get("name") or txt or None
                if at.get("price"):
                    o["precio"] = a_numero(at.get("price"))
            elif n == "price" and o["precio"] is None:
                o["precio"] = a_numero(txt or at.get("value"))
            elif n in ("temperature", "temp", "heat", "grados"):
                o["grados"] = a_numero(txt or at.get("value"))
            elif n in ("enclosure", "thumbnail", "content") and at.get("url") and not o["imagen"]:
                if "image" in at.get("type", "image") or n == "thumbnail":
                    o["imagen"] = at["url"]
        desc = html.unescape(o["descripcion"])
        if not o["imagen"]:
            m = re.search(r'<img[^>]+src=["\']([^"\']+)', desc)
            if m:
                o["imagen"] = m.group(1)
        m = RE_ENLACE_TIENDA.search(desc)
        if m:
            o["destino"] = m.group(1)
        o["id"] = id_de_enlace(o["enlace"]) or id_de_enlace(o["guid"]) or o["guid"] or o["enlace"]
        if o["id"]:
            yield o


# --------------------------------------------------------------------------
# Lectura de páginas HTML (listados y página de cada oferta)
# Chollometro incrusta los datos de cada hilo como JSON en atributos
# data-vue3 / bloques <script type="application/json">. Se busca de forma
# tolerante cualquier objeto que tenga id de hilo y temperatura.
# --------------------------------------------------------------------------
WEB = "https://www.chollometro.com"
STATIC = "https://static.chollometro.com"
RE_VUE = re.compile(r'data-vue3=(["\'])(.*?)\1', re.S)
RE_THREAD_ID = re.compile(r'"threadId"\s*:\s*"?(\d+)"?')
RE_SCRIPT_JSON = re.compile(r'<script[^>]*type=["\']application/(?:ld\+)?json["\'][^>]*>(.*?)</script>', re.S | re.I)


def _recorrer(x):
    if isinstance(x, dict):
        yield x
        for v in x.values():
            yield from _recorrer(v)
    elif isinstance(x, list):
        for v in x:
            yield from _recorrer(v)


def _url(v) -> str | None:
    return v if isinstance(v, str) and v.startswith(("http", "file:")) else None


def _hilo_desde_json(d: dict) -> dict | None:
    hid = d.get("threadId") or d.get("thread_id")
    if hid is None or "temperature" not in d:
        return None
    merchant = d.get("merchant")
    tienda = None
    if isinstance(merchant, dict):
        tienda = merchant.get("merchantName") or merchant.get("name")
    tienda = tienda or d.get("merchantName")
    tipo = d.get("type") or d.get("threadType")
    if isinstance(tipo, dict):
        tipo = tipo.get("name") or tipo.get("type")
    precio = a_numero(d.get("price"))
    antes = a_numero(d.get("nextBestPrice") or d.get("previousPrice") or d.get("originalPrice"))
    publicado = d.get("publishedAt") if isinstance(d.get("publishedAt"), (int, float)) and d.get("publishedAt") else None
    fin = (d.get("endDate") or {}).get("timestamp") if isinstance(d.get("endDate"), dict) else None
    imagen = d.get("imageUrl") or d.get("image")
    img = d.get("mainImage")
    if isinstance(img, dict) and img.get("path") and img.get("name"):
        imagen = f"{STATIC}/{img['path']}/{img['name']}/re/768x768/qt/60/{img['name']}.jpg"
    enlace = (f"{WEB}/ofertas/{d['titleSlug']}-{hid}" if d.get("titleSlug") else None)
    visita = d.get("linkCloakedItemMainButton") or (f"{WEB}/visit/threadmain/{hid}"
                                                    if d.get("linkHost") or d.get("titleSlug") else None)
    return {
        "id": str(hid),
        "titulo": d.get("title") or "",
        "grados": a_numero(d.get("temperature")),
        "precio": precio,
        "precio_antes": antes if antes and precio is not None and antes > precio else None,
        "codigo": d.get("voucherCode") or None,
        "tienda": tienda,
        # Página del chollo en Chollometro y enlace real de la tienda
        "enlace": enlace or _url(d.get("url")) or _url(d.get("shareableLink")),
        "destino": _url(d.get("link")) or _url(visita),
        "imagen": _url(imagen),
        "caducada": bool(d.get("isExpired") or d.get("expired")) or bool(fin and fin < time.time()),
        "fecha": float(publicado) if publicado else None,
        "tipo": str(tipo).lower() if tipo else None,
        "es_cupon": bool(d.get("isVoucher") or d.get("voucherCode")) and precio is None,
        "grupo": (d.get("mainGroup") or {}).get("threadGroupUrlName") if isinstance(d.get("mainGroup"), dict) else None,
        "grupos": [g["threadGroupUrlName"] for g in d["groups"] if isinstance(g, dict) and g.get("threadGroupUrlName")]
        if isinstance(d.get("groups"), list) and d["groups"] else None,
    }


_DECODER = json.JSONDecoder()


def _objetos_sueltos(texto: str):
    """Saca los objetos JSON que contienen "threadId" aunque estén dentro de un <script> cualquiera."""
    vistos = set()
    for m in RE_THREAD_ID.finditer(texto):
        inicio, intentos = m.start(), 0
        while intentos < 60:
            inicio = texto.rfind("{", 0, inicio)
            if inicio < 0:
                break
            intentos += 1
            try:
                obj, fin = _DECODER.raw_decode(texto, inicio)
            except ValueError:
                continue
            if fin > m.start() and isinstance(obj, dict) and "threadId" in obj:
                if inicio not in vistos:
                    vistos.add(inicio)
                    yield obj
                break


def hilos_en_html(texto: str) -> dict:
    encontrados: dict[str, dict] = {}
    datos = []
    for b in [html.unescape(m.group(2)) for m in RE_VUE.finditer(texto)] + \
             [m.group(1) for m in RE_SCRIPT_JSON.finditer(texto)]:
        try:
            datos.append(json.loads(b))
        except (ValueError, TypeError):
            continue
    datos += list(_objetos_sueltos(texto))
    for bloque in datos:
        for d in _recorrer(bloque):
            h = _hilo_desde_json(d)
            if h:
                previo = encontrados.get(h["id"], {})
                encontrados[h["id"]] = {**previo, **{k: v for k, v in h.items() if v not in (None, "")}}
            if d.get("threadId") is not None and isinstance(d.get("additionalInfo"), list):
                pr = precioreal_desde_info(d["additionalInfo"])
                if pr:
                    encontrados.setdefault(str(d["threadId"]), {"id": str(d["threadId"])})["precioreal"] = pr
    return encontrados


RE_GRADOS_VISIBLES = re.compile(r'class=["\'][^"\']*(?:vote-temp|cept-vote-temp)[^"\']*["\'][^>]*>\s*(-?\d+)\s*°')
RE_VISITA = re.compile(r'(https?://(?:www\.)?chollometro\.com/visit/[^"\'\s<>]+)')


def datos_de_pagina(url: str, hid: str) -> dict | None:
    """Abre la página de la oferta y devuelve sus datos (grados, caducada, enlace a tienda...)."""
    texto = descargar(url).decode("utf-8", "replace")
    hilos = hilos_en_html(texto)
    d = hilos.get(hid)
    if d is None or d.get("grados") is None:
        m = re.search(rf'"threadId"\s*:\s*"?{re.escape(hid)}"?[^{{}}]*?"temperature"\s*:\s*(-?\d+(?:\.\d+)?)', texto)
        if not m:
            m = RE_GRADOS_VISIBLES.search(texto)
        if m:
            d = {**(d or {}), "id": hid, "grados": float(m.group(1))}
    if d is not None and not d.get("destino"):
        m = RE_ENLACE_TIENDA.search(texto) or RE_VISITA.search(texto)
        if m:
            d["destino"] = html.unescape(m.group(1))
    return d


# --------------------------------------------------------------------------
# Keepa (historial de precios de Amazon)
# --------------------------------------------------------------------------
RE_ASIN = re.compile(r"(?:/dp/|/gp/product/|/gp/aw/d/|/product/|/exec/obidos/asin/|/o/asin/|/ASIN/|[?&]asin=)([A-Z0-9]{10})(?![A-Z0-9])", re.I)
KEEPA_DOMINIO_ES = 9


def extraer_asin(url: str | None) -> str | None:
    m = RE_ASIN.search(urllib.parse.unquote(url or ""))
    return m.group(1).upper() if m else None


def buscar_asin(o: dict) -> str | None:
    if o.get("asin"):
        return o["asin"]
    asin = extraer_asin(o.get("destino"))
    if not asin and o.get("destino", "") and o["destino"].startswith("http"):
        final = seguir_redirecciones(o["destino"])
        asin = extraer_asin(final)
        if asin:
            o["destino"] = final
    return asin


def keepa_enlace(asin: str) -> str:
    return f"https://keepa.com/#!product/{KEEPA_DOMINIO_ES}-{asin}"


def keepa_grafica(asin: str) -> str:
    """Gráfica pública de Keepa (sin clave). No está documentada oficialmente: se usa si funciona."""
    return f"https://graph.keepa.com/pricehistory.png?asin={asin}&domain=es&range=90"


def keepa_grafica_bytes(asin: str) -> bytes | None:
    """Descarga la gráfica para subirla a Telegram. Con clave usa la oficial (1 token) y la clave no se expone."""
    clave = os.environ.get("KEEPA_API_KEY")
    if clave:
        url = (os.environ.get("KEEPA_API", "https://api.keepa.com") + "/graphimage?" + urllib.parse.urlencode(
            {"key": clave, "domain": KEEPA_DOMINIO_ES, "asin": asin, "range": 90, "width": 800, "height": 400}))
    else:
        url = keepa_grafica(asin)
    try:
        datos = descargar(url, intentos=1)
    except Exception as ex:  # noqa: BLE001
        log(f"  Gráfica de Keepa no disponible ({ex})")
        return None
    return datos if datos[:4] == b"\x89PNG" or datos[:3] == b"\xff\xd8\xff" else None


def _keepa_valor(lista, i):
    """Los precios de Keepa vienen en céntimos; -1/-2 = sin datos. min/max son pares [hora, precio]."""
    if not isinstance(lista, list) or i >= len(lista):
        return None
    v = lista[i]
    if isinstance(v, list):
        v = v[1] if len(v) > 1 else None
    if v is None or v < 0:
        return None
    return v / 100.0


def keepa_veredicto(asin: str, precio_oferta: float | None, cfg_keepa: dict) -> dict | None:
    """Consulta la API de Keepa (de pago) y compara el precio con la media y el mínimo de 90 días."""
    clave = os.environ.get("KEEPA_API_KEY")
    if not clave:
        return None
    url = (os.environ.get("KEEPA_API", "https://api.keepa.com") + "/product?" + urllib.parse.urlencode(
        {"key": clave, "domain": KEEPA_DOMINIO_ES, "asin": asin, "stats": 90}))
    try:
        datos = json.loads(descargar(url, intentos=1))
    except Exception as ex:  # noqa: BLE001
        log(f"  Keepa {asin}: error ({ex})")
        return None
    if datos.get("error"):
        log(f"  Keepa {asin}: {datos['error']}")
        return None
    productos = datos.get("products") or []
    stats = (productos[0] or {}).get("stats") if productos else None
    if not stats:
        return None
    # 0 = vendido por Amazon, 1 = nuevo (cualquier vendedor)
    media = _keepa_valor(stats.get("avg90") or stats.get("avg"), 0) or _keepa_valor(stats.get("avg90") or stats.get("avg"), 1)
    minimo = _keepa_valor(stats.get("minInInterval"), 0) or _keepa_valor(stats.get("minInInterval"), 1)
    actual = _keepa_valor(stats.get("current"), 0) or _keepa_valor(stats.get("current"), 1)
    precio = precio_oferta if precio_oferta else actual
    if not precio or not media:
        return {"texto": "📈 Keepa: sin historial suficiente", "nivel": "gris"}
    rebaja = round((1 - precio / media) * 100)
    exigida = cfg_keepa.get("rebaja_minima_pct", 10)
    if minimo and precio <= minimo * 1.01:
        texto, nivel = f"🟢 Keepa: mínimo de 90 días ({rebaja}% bajo la media)", "verde"
    elif rebaja >= exigida:
        texto, nivel = f"🟢 Keepa: {rebaja}% por debajo de la media de 90 días ({formatear_precio(media)})", "verde"
    elif rebaja > 0:
        texto, nivel = f"🟡 Keepa: solo un {rebaja}% bajo la media de 90 días ({formatear_precio(media)})", "amarillo"
    else:
        texto, nivel = f"🔴 Keepa: no es rebaja, la media de 90 días es {formatear_precio(media)}", "rojo"
    return {"texto": texto, "nivel": nivel, "media90": media, "minimo90": minimo, "rebaja": rebaja}


# --------------------------------------------------------------------------
# PrecioReal (el comparador de precios que usa Chollometro)
# --------------------------------------------------------------------------
PRECIOREAL = "https://precioreal.com"
RE_PR_ID = re.compile(r"precioreal\.com/oferta/([a-z0-9]+_[a-z0-9-]+)", re.I)
RE_PR_IMAGEN = re.compile(r'<img[^>]+src="(https://static\.chollometro\.com/thread_additional_info/[^"]+)"', re.I)
MESES = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")


def precioreal_desde_info(info) -> dict | None:
    """Veredicto que PrecioReal deja en la «Información adicional» de la oferta en Chollometro
    («Mínimo histórico», «Buen precio», «Buen momento»...), con su medidor barato/habitual/caro."""
    for a in info if isinstance(info, list) else []:
        desc = (a or {}).get("description") if isinstance(a, dict) else None
        m = RE_PR_ID.search(desc or "")
        if not m:
            continue
        titulo = re.search(r"<strong>(.*?)</strong>", desc, re.S)
        img = RE_PR_IMAGEN.search(desc)
        return {"id": m.group(1).lower(),
                "veredicto": quitar_html(titulo.group(1)) if titulo else None,
                "medidor": img.group(1) if img else None}
    return None


def _precioreal_json(ruta: str):
    req = urllib.request.Request(PRECIOREAL + ruta, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as ex:  # noqa: BLE001
        log(f"  PrecioReal {ruta[:70]}: {ex}")
        return None


def precioreal_datos(pr_id: str, hilo: str | None = None, otras_tiendas: bool = True) -> dict:
    """API pública de la web de PrecioReal (sin cuenta): precios de los chollos anteriores del mismo
    producto y su precio en otras tiendas. No está documentada: si cambia, el aviso sale igual sin esto."""
    salida = {}
    h = _precioreal_json(f"/api/community-history/{urllib.parse.quote(pr_id)}?offset=0") or {}
    puntos = []
    for p in h.get("points") or []:
        if not isinstance(p, dict):
            continue
        q = p.get("quantity") if isinstance(p.get("quantity"), dict) else {}
        precio = q.get("comparisonPrice") or p.get("price")
        if not isinstance(precio, (int, float)) or precio <= 0:
            continue
        if hilo and (str(q.get("threadId")) == str(hilo) or p.get("seller") == f"chollometro_{hilo}"):
            continue          # el propio chollo que se está avisando
        puntos.append((precio, (p.get("timestamp") or 0) / 1000))
    if puntos:
        precio, cuando = min(puntos)
        salida["min_anterior"], salida["min_anterior_fecha"], salida["chollos_anteriores"] = precio, cuando, len(puntos)
    if otras_tiendas:
        tienda_propia = pr_id.split("_", 1)[0]
        c = _precioreal_json("/api/catalog/offer?" + urllib.parse.urlencode({"id": pr_id, "shop": tienda_propia})) or {}
        prod = c.get("product") if isinstance(c.get("product"), dict) else {}
        if prod.get("variantKnown"):       # solo si sabe que es el mismo modelo y capacidad
            ofertas = [o for o in prod.get("offers") or [] if isinstance(o, dict) and o.get("available")
                       and o.get("condition", "new") == "new" and isinstance(o.get("price"), (int, float))
                       and o.get("shop") != tienda_propia and o.get("channel", "normal") == "normal"]
            if ofertas:
                o = min(ofertas, key=lambda o: o["price"] + (o.get("shipping") or 0))
                salida["otra_tienda"] = {"tienda": o.get("shopName") or o.get("shop"),
                                         "precio": round(o["price"] + (o.get("shipping") or 0), 2)}
    return salida


def enriquecer_precioreal(s: dict, cfg: dict):
    """Junta el veredicto de PrecioReal (si Chollometro lo tiene) con los datos de su API."""
    cfg_pr = cfg.get("precioreal") or {}
    if cfg_pr.get("activo") is False:
        return
    pr = dict(s.get("precioreal") or {})
    if not pr.get("id") and s.get("asin"):
        pr["id"] = f"amazon_{s['asin'].lower()}"
    if not pr.get("id"):
        return
    hilo = s.get("id") if str(s.get("id", "")).isdigit() else None
    pr.update(precioreal_datos(pr["id"], hilo, cfg_pr.get("otras_tiendas", True)))
    pr["enlace"] = f"{PRECIOREAL}/oferta/{pr['id']}"
    s["precioreal"] = pr


def _mes(ts: float) -> str:
    """Fecha corta: «21 nov 2025»."""
    try:
        f = datetime.fromtimestamp(ts)
        return f"{f.day} {MESES[f.month - 1]} {f.year}"
    except (ValueError, OSError, OverflowError):
        return ""


def precioreal_lineas(o: dict) -> list[str]:
    pr = o.get("precioreal") or {}
    lineas = []
    v = (pr.get("veredicto") or "").strip()
    if v:
        n = normalizar(v)
        emoji = "🏆" if "minimo" in n else "🟢" if ("buen" in n or "barato" in n) else "🔴" if "caro" in n else "🟡"
        lineas.append(f"{emoji} PrecioReal: {v[:1].lower() + v[1:]}")
    precio = o.get("precio")
    if pr.get("min_anterior"):
        minimo, cuando = pr["min_anterior"], _mes(pr.get("min_anterior_fecha") or 0)
        n = pr.get("chollos_anteriores", 1)
        if precio and precio < minimo - 0.009:
            lineas.append(f"🏆 Más barato que en {'los ' + str(n) + ' chollos anteriores' if n > 1 else 'el chollo anterior'} "
                          f"(mín. {formatear_precio(minimo)})")
        elif precio and precio <= minimo + 0.009:
            lineas.append(f"🟰 Igual que el mejor chollo anterior ({formatear_precio(minimo)})")
        else:
            lineas.append(f"📉 Ya estuvo a {formatear_precio(minimo)}" + (f" el {cuando}" if cuando else ""))
    otra = pr.get("otra_tienda")
    if otra and precio:
        if otra["precio"] < precio - 0.5:
            lineas.append(f"⚠️ Más barato en {otra['tienda']}: {formatear_precio(otra['precio'])}")
        elif otra["precio"] <= precio + 0.5:
            lineas.append(f"🟰 Mismo precio en {otra['tienda']}")
        else:
            lineas.append(f"✅ Más barato que en {otra['tienda']} ({formatear_precio(otra['precio'])})")
    return lineas


# --------------------------------------------------------------------------
# Discos: precio por TB
# --------------------------------------------------------------------------
RE_CAPACIDAD = re.compile(r"(?<![\d.,])(\d{1,4}(?:[.,]\d{1,2})?)\s?(tb|gb)(?![a-z0-9])")
RE_CAPACIDAD_PACK = re.compile(r"(?<![a-z0-9])(\d{1,2})\s?(?:x|uds?\.?\s?de|unidades de)\s?"
                               r"(\d{1,4}(?:[.,]\d{1,2})?)\s?(tb|gb)(?![a-z0-9])")


def _a_tb(valor: str, unidad: str) -> float | None:
    try:
        x = float(valor.replace(",", "."))
    except ValueError:
        return None
    return x if unidad == "tb" else x / 1000


def capacidad_tb(titulo: str) -> float | None:
    """Capacidad en TB sacada del título («2TB», «512 GB», «Pack 2 x 4TB»). Si el título trae varias
    capacidades distintas (opciones, RAM + disco...), no se inventa: devuelve None."""
    t = normalizar(titulo)
    m = RE_CAPACIDAD_PACK.search(t)
    if m:
        n, cap = int(m.group(1)), _a_tb(m.group(2), m.group(3))
        return round(n * cap, 3) if cap and 1 < n <= 12 else None
    caps = {c for c in (_a_tb(v, u) for v, u in RE_CAPACIDAD.findall(t)) if c and 0.03 <= c <= 40}
    return caps.pop() if len(caps) == 1 else None


# El €/TB solo tiene sentido en discos sueltos: si el disco va dentro de un portátil, un ordenador o una
# consola, no se calcula. Consola = su nombre sale ANTES que «para»/«compatible», que la capacidad y que
# cualquier palabra o marca de disco («ROG Ally 512GB SSD» sí; «microSD 1TB para Steam Deck», «NVMe para PS5»
# o «FireCuda 530 2TB PS5» no).
RE_CONSOLA_CON_DISCO = re.compile(
    r"^(?:(?!para |compatible|for |ssd|nvme|hdd|disco|m\.?2|micro ?sd|sdxc|sdhc|tarjeta|game drive|expansion|"
    r"\d+\s?[tg]b\b|seagate|firecuda|western digital|wd_black|wd black|crucial|kingston|lexar|sandisk|corsair|"
    r"solidigm|pny|hikvision|fikwot|sabrent|fanxiang|ediloca|kingspec|netac|orico|lacie|toshiba|hynix|samsung|"
    r"teamgroup|team group|adata|xpg|patriot|transcend|silicon power|nextorage|aorus|spatium).)*"
    r"(?<![a-z0-9])(consola|steam deck|rog ally|legion go|msi claw|ayaneo|ps5|ps4|playstation|xbox)(?![a-z0-9])")
RE_DISCO_DE_CONSOLA = re.compile(r"game drive|expansion card|tarjeta de expansion|tarjeta de almacenamiento|"
                                 r"micro ?sd|sdxc|sdhc")


def es_ordenador(o: dict) -> bool:
    return any(c.startswith("portatil") or "sobremesa" in c or "mini_pc" in c for c in o.get("categorias", []))


def es_de_discos(o: dict) -> bool:
    if not any("ssd" in c or "disco" in c for c in o.get("categorias", [])) or es_ordenador(o):
        return False
    t = normalizar(o.get("titulo"))
    return not (RE_CONSOLA_CON_DISCO.search(t) and not RE_DISCO_DE_CONSOLA.search(t))


def euros_por_tb(o: dict) -> float | None:
    if o.get("eur_tb") and not es_ordenador(o):
        return o["eur_tb"]
    if not es_de_discos(o) or not o.get("precio"):
        return None
    cap = capacidad_tb(o.get("titulo", ""))
    return round(o["precio"] / cap, 2) if cap else None


# --------------------------------------------------------------------------
# Errores de precio
# --------------------------------------------------------------------------
RE_ERROR_PRECIO = re.compile(r"(?<![a-z0-9])(errore?s? de precio|posible error|fallo de precio|precio erroneo|"
                             r"bug de precio|error en el precio|price error|price mistake|preisfehler)(?![a-z0-9])")


def error_en_titulo(o: dict) -> bool:
    return bool(RE_ERROR_PRECIO.search(normalizar(o.get("titulo"))))


def marcar_error_de_precio(s: dict):
    """⚠️ si el título lo dice o si el precio es la mitad (o menos) del mejor chollo anterior."""
    if error_en_titulo(s):
        s["error_precio"] = True
        return
    pr, precio = s.get("precioreal") or {}, s.get("precio")
    if precio and pr.get("min_anterior") and precio <= pr["min_anterior"] * 0.5:
        s["error_precio"] = True
    if precio and s.get("precio_antes") and s.get("fuente") == "Radar €/TB" and precio <= s["precio_antes"] * 0.5:
        s["error_precio"] = True


def recortar_medidor(datos: bytes) -> bytes | None:
    """El medidor de PrecioReal trae mucho margen blanco: se queda solo con el veredicto y la barra."""
    try:
        from io import BytesIO
        from PIL import Image
        im = Image.open(BytesIO(datos)).convert("RGB")
        w, h = im.size
        im = im.crop((int(w * 0.04), int(h * 0.25), int(w * 0.935), int(h * 0.68)))
        salida = BytesIO()
        im.save(salida, "PNG", optimize=True)
        return salida.getvalue()
    except Exception:  # noqa: BLE001
        return None


def repasar_pagina(s: dict):
    """Justo antes de avisar abre la oferta: confirma que sigue activa y recoge el veredicto de PrecioReal."""
    if not str(s.get("id", "")).isdigit() or not (s.get("enlace") or "").startswith(WEB):
        return
    try:
        d = datos_de_pagina(s["enlace"], s["id"]) or {}
    except urllib.error.HTTPError as ex:
        if ex.code in (404, 410):
            s["descartar"] = True
        return
    except Exception:  # noqa: BLE001
        return
    for k in CAMPOS_PAGINA:
        if d.get(k) not in (None, ""):
            s[k] = d[k]
    for k in ("precio", "imagen"):
        if s.get(k) in (None, "") and d.get(k) not in (None, ""):
            s[k] = d[k]


def completar_destino(s: dict):
    """Si no sabemos el enlace de la tienda, lo busca en la página de la oferta (una consulta por aviso)."""
    if s.get("destino"):
        return
    if str(s.get("id", "")).isdigit() and (s.get("enlace") or "").startswith(WEB):
        s["destino"] = f"{WEB}/visit/threadmain/{s['id']}"
        return
    if not (s.get("enlace") or "").startswith(("http", "file:")):
        return
    try:
        d = datos_de_pagina(s["enlace"], s["id"]) or {}
    except Exception:  # noqa: BLE001
        return
    for k in ("destino", "codigo", "precio_antes"):
        if d.get(k) and not s.get(k):
            s[k] = d[k]


def unir_imagenes(*partes: bytes, ancho: int = 800) -> bytes | None:
    """Pone una debajo de otra la foto del artículo, la gráfica de Keepa y el medidor de PrecioReal
    en una sola imagen (necesita Pillow). La primera es la foto: se reduce sin estirarla."""
    try:
        from io import BytesIO
        from PIL import Image
    except ImportError:
        return None
    try:
        def abrir(b):
            im = Image.open(BytesIO(b))
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                fondo = Image.new("RGB", im.size, "white")
                fondo.paste(im, mask=im.split()[-1])
                return fondo
            return im.convert("RGB")
        imagenes = []
        for i, b in enumerate(p for p in partes if p):
            im = abrir(b)
            if i == 0:
                im.thumbnail((ancho, 520))
            else:
                im = im.resize((ancho, max(1, round(im.height * ancho / im.width))))
            imagenes.append(im)
        if len(imagenes) < 2:
            return None
        hueco = 12
        lienzo = Image.new("RGB", (ancho, sum(im.height for im in imagenes) + hueco * (len(imagenes) - 1)), "white")
        y = 0
        for im in imagenes:
            lienzo.paste(im, ((ancho - im.width) // 2, y))
            y += im.height + hueco
        salida = BytesIO()
        lienzo.save(salida, "PNG", optimize=True)
        return salida.getvalue()
    except Exception as ex:  # noqa: BLE001
        log(f"  No se pudieron unir las imágenes ({ex})")
        return None


def fotos_para(s: dict, cfg: dict) -> list:
    """Fotos a probar, por orden: artículo + gráfica de Keepa + medidor de PrecioReal juntos,
    luego combinaciones más sencillas y, al final, solo la foto del artículo."""
    def bajar(url):
        try:
            return descargar(url, intentos=1)
        except Exception:  # noqa: BLE001
            return None
    grafica = keepa_grafica_bytes(s["asin"]) if s.get("asin") and (cfg.get("keepa") or {}).get("grafica", True) else None
    medidor = None
    pr = s.get("precioreal") or {}
    if pr.get("medidor") and (cfg.get("precioreal") or {}).get("imagen", True):
        b = bajar(pr["medidor"])
        medidor = recortar_medidor(b) if b else None
    foto = bajar(s["imagen"]) if s.get("imagen") and (grafica or medidor) else None
    fotos = []
    for combo in ((foto, grafica, medidor), (foto, grafica), (grafica, medidor), (foto, medidor)):
        if all(combo):
            unida = unir_imagenes(*combo)
            if unida:
                fotos.append(unida)
    if grafica:
        fotos.append(grafica)
    fotos.append(s.get("imagen"))
    return fotos


# --------------------------------------------------------------------------
# Vigilancia de precios (vigilar.txt): precio actual leído de la gráfica de Keepa con OCR
# --------------------------------------------------------------------------
RUTA_VIGILAR = Path(os.environ.get("RADAR_VIGILAR", BASE / "vigilar.txt"))


def leer_vigilar() -> list[dict]:
    """Cada línea: enlace de Amazon o ASIN, y opcionalmente « | nombre»."""
    salida = []
    try:
        lineas = RUTA_VIGILAR.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    for linea in lineas:
        linea = linea.strip()
        if not linea or linea.startswith("#"):
            continue
        parte, _, nombre = linea.partition("|")
        parte = parte.strip()
        asin = extraer_asin(parte) or (parte.upper() if re.fullmatch(r"[A-Za-z0-9]{10}", parte) else None)
        if asin and asin not in {v["asin"] for v in salida}:
            salida.append({"asin": asin, "nombre": nombre.strip() or None})
    return salida


def _ocr(ruta: str, psm: str) -> str:
    import subprocess
    try:
        return subprocess.run(["tesseract", ruta, "-", "--psm", psm], capture_output=True, text=True, timeout=60).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def precio_keepa_ocr(asin: str) -> dict | None:
    """Lee de la leyenda de la gráfica de Keepa el precio actual vendido por Amazon y el nuevo más barato."""
    import tempfile
    try:
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(descargar(keepa_grafica(asin) + "&width=1200&height=500", intentos=1)))
    except Exception as ex:  # noqa: BLE001
        log(f"  Vigilar {asin}: sin gráfica ({ex})")
        return None
    g = img.convert("L")
    with tempfile.TemporaryDirectory() as tmp:
        grande = f"{tmp}/g.png"
        g.resize((g.width * 3, g.height * 3)).save(grande)
        texto = _ocr(grande, "11")
        cab = f"{tmp}/t.png"
        g.crop((0, 0, g.width, max(20, int(g.height * 0.09)))).resize((g.width * 3, max(20, int(g.height * 0.09)) * 3)).save(cab)
        titulo = " ".join(_ocr(cab, "7").split())
    def buscar(etiqueta):
        m = re.search(etiqueta + r"\W{0,6}€?\s?(\d{1,4}[.,]\d{2})", texto, re.I)
        return a_numero(m.group(1)) if m else None
    amazon, nuevo = buscar("amazon"), buscar("new")
    if amazon is None and nuevo is None:
        log(f"  Vigilar {asin}: no se pudo leer el precio (OCR: {texto[:120]!r})")
        return None
    return {"amazon": amazon, "nuevo": nuevo, "titulo": titulo[:120] or None, "grafica": img}


def vigilar_precios(cfg: dict, estado: dict, silencioso: bool):
    """Avisa cuando baja el precio de los productos de vigilar.txt respecto al que tenían al añadirlos."""
    productos = productos_vigilados(estado)
    datos = estado.setdefault("precios", {})
    if not productos:
        datos.clear()
        return
    cada = (cfg.get("vigilar") or {}).get("cada_min", 30) * 60
    ahora = time.time()
    tag = ((cfg.get("afiliados") or {}).get("amazon_tag") or "").strip()
    for p in productos:
        asin = p["asin"]
        d = datos.setdefault(asin, {})
        if ahora - d.get("comprobado", 0) < cada:
            continue
        r = precio_keepa_ocr(asin)
        d["comprobado"] = ahora
        if not r:
            continue
        nombre = p["nombre"] or d.get("nombre") or r["titulo"] or asin
        d["nombre"] = nombre
        actual = min(x for x in (r["amazon"], r["nuevo"]) if x)
        quien = "🟠 vendido por Amazon" if r["amazon"] and actual == r["amazon"] else "🏪 otro vendedor (nuevo)"
        enlace = f"https://www.amazon.es/dp/{asin}" + (f"?tag={urllib.parse.quote(tag)}" if tag else "")
        btns = [[{"text": "🛒 Amazon", "url": enlace}],
                [{"text": "📈 Keepa", "url": keepa_enlace(asin)},
                 {"text": "📊 PrecioReal", "url": f"{PRECIOREAL}/oferta/amazon_{asin.lower()}"}]]
        from io import BytesIO
        buf = BytesIO()
        r["grafica"].save(buf, "PNG")
        if "referencia" not in d:          # primera vez: se apunta el precio de ahora
            d["referencia"] = actual
            d["minimo"] = actual
            telegram(f"👀 <b>Vigilando precio</b>\n<b>{e(nombre)}</b>\n💶 <b>{formatear_precio(actual)}</b> ({quien})\n"
                     f"Te aviso si baja de este precio.", silencioso, foto=[buf.getvalue()], botones=btns)
            log(f"  Vigilar {asin}: referencia {actual}")
            continue
        d["ultimo"] = actual
        if actual < d["referencia"] - 0.009:
            pct = round((1 - actual / d["referencia"]) * 100)
            ok = telegram(f"📉 <b>BAJADA DE PRECIO</b>  ·  −{pct}%\n<b>{e(nombre)}</b>\n"
                          f"💶 <b>{formatear_precio(actual)}</b> <s>{formatear_precio(d['referencia'])}</s>\n{quien}",
                          silencioso, foto=[buf.getvalue()], botones=btns)
            if ok or not os.environ.get("TELEGRAM_TOKEN"):
                d["referencia"] = actual   # la próxima vez avisa solo si baja todavía más
            d["minimo"] = min(d.get("minimo", actual), actual)
        log(f"  Vigilar {asin}: {actual} (referencia {d['referencia']})")
    for asin in [a for a in datos if a not in {p["asin"] for p in productos}]:
        datos.pop(asin)   # quitado de vigilar.txt


def enriquecer_amazon(s: dict, cfg: dict):
    """Añade ASIN, enlaces y veredicto de Keepa a una oferta de Amazon."""
    if not es_amazon(s):
        return
    try:
        asin = buscar_asin(s)
    except Exception:  # noqa: BLE001
        asin = None
    if not asin:
        return
    s["asin"] = asin
    s["keepa"] = keepa_enlace(asin)
    tag = ((cfg.get("afiliados") or {}).get("amazon_tag") or "").strip()
    if tag:
        s["destino"] = f"https://www.amazon.es/dp/{asin}?tag={urllib.parse.quote(tag)}"
        s["afiliado"] = True
    v = keepa_veredicto(asin, s.get("precio"), cfg.get("keepa") or {})
    if v:
        s["keepa_veredicto"] = v


# --------------------------------------------------------------------------
# Clasificación
# --------------------------------------------------------------------------
def tipo_hilo(o: dict) -> str:
    """'oferta', 'cupon' o 'discusion'."""
    enlace = (o.get("enlace") or "").lower()
    tipo = (o.get("tipo") or "").lower()
    if "/discusiones/" in enlace or any(t in tipo for t in ("discussion", "discusion")):
        return "discusion"
    if "/cupones/" in enlace or "/codigos-descuento" in enlace or any(t in tipo for t in ("voucher", "cupon")) \
            or o.get("es_cupon"):
        return "cupon"
    return "oferta"


def no_es_oferta(o: dict, cfg: dict | None = None) -> bool:
    """True si el hilo no interesa: discusiones siempre; cupones solo si están desactivados."""
    t = tipo_hilo(o)
    return t == "discusion" or (t == "cupon" and not (cfg or {}).get("incluir_cupones", True))


GRUPO_GAMING = "consolas-y-videojuegos"
RE_PLATAFORMA = re.compile(r"(?<![a-z0-9])(ps5|ps4|playstation|nintendo|switch 2|switch oled|para switch|xbox|"
                           r"steam|epic games|gog|prime gaming|para pc|ubisoft connect|ea app|battle\.net|videojuegos?|juego para|juego de (ps|switch|xbox|nintendo))(?![a-z0-9])")
RE_GRATIS = re.compile(r"(?<![a-z0-9])(gratis|gratuito|gratuita|free to keep|free)(?![a-z0-9])")
RE_ENVIO_GRATIS = re.compile(r"(envio|envios|gastos de envio|devolucion)\s+(gratis|gratuito)")
_re_no_juegos = None


def es_juego(o: dict) -> bool:
    """Un juego: está en el grupo Gaming (o el título/categorías hablan de una plataforma)
    y no lleva ninguna palabra de consola/accesorio de la sección [No son juegos]."""
    global _re_no_juegos
    if _re_no_juegos is None:
        _re_no_juegos = compilar_lista(NO_SON_JUEGOS) or re.compile(r"(?!x)x")
    titulo = normalizar(o.get("titulo"))
    if o.get("grupo"):
        contexto = o["grupo"] == GRUPO_GAMING
    else:
        cats = normalizar(" ".join(o.get("cats_rss") or []))
        contexto = bool(RE_PLATAFORMA.search(titulo)) or any(
            p in cats for p in ("videojuego", "consola", "gaming", "juegos"))
    if (o.get("precio") or 0) >= PRECIO_MIN_CONSOLA:   # a ese precio es una consola o un pack, no un juego
        return False
    return contexto and not _re_no_juegos.search(titulo)


PRECIO_MIN_CONSOLA = 120


RE_NO_ES_GRATIS = re.compile(r"(?<![a-z0-9])(recopil\w*|rebaj\w*|descuentos?|ofertas|hasta|desde|seleccion|"
                             r"black friday|cyber monday|\d{1,2}\s?%)(?![a-z0-9])")


def es_gratis(o: dict) -> bool:
    """Gratis de verdad. Las recopilaciones de rebajas también salen a 0 € en Chollometro."""
    if (o.get("precio") or 0) > 0:          # "actualización gratis", "envío gratis"... pero cuesta dinero
        return False
    titulo = RE_ENVIO_GRATIS.sub(" ", normalizar(o.get("titulo")))
    if RE_GRATIS.search(titulo):
        return True
    return o.get("precio") == 0 and tipo_hilo(o) == "oferta" and not RE_NO_ES_GRATIS.search(titulo)


# Juegos y apps/programas gratis (p. ej. «1 Año GRATIS de Norton 360», apps de pago gratis en Play Store)
RE_SOFTWARE = re.compile(r"(?<![a-z0-9])(apps?|aplicacion(es)?|android|ios|iphone|ipad|software|programa|licencia|"
                         r"antivirus|vpn|windows|macos|norton|mcafee|kaspersky|bitdefender|avast|eset|nordvpn|surfshark|"
                         r"protonvpn|office|microsoft 365|adobe|juegos?|videojuegos?|steam|epic games|gog|prime gaming|"
                         r"ubisoft|play store|google play|app store)(?![a-z0-9])")
TIENDAS_SOFTWARE = {"play store", "google play", "app store", "apple", "microsoft store", "microsoft", "epic games",
                    "epic games store", "steam", "gog", "gog.com", "prime gaming", "ubisoft", "ubisoft store", "ea",
                    "humble bundle", "itch.io", "norton", "mcafee", "kaspersky", "bitdefender", "avast", "nordvpn",
                    "surfshark", "adobe", "amazon luna", "xbox", "playstation store"}
# Pruebas gratis de suscripciones (Spotify, Netflix, Audible, Game Pass...): también las quiere
RE_SUSCRIPCION = re.compile(r"(?<![a-z0-9])(spotify|netflix|disney\+?|hbo|max|prime video|amazon prime|prime|audible|"
                            r"kindle unlimited|music unlimited|apple music|apple tv|apple one|youtube premium|"
                            r"youtube music|dazn|movistar|filmin|skyshowtime|crunchyroll|deezer|tidal|atresplayer|"
                            r"mitele plus|rakuten|game pass|ps plus|playstation plus|ea play|ubisoft\+?|geforce now|"
                            r"suscripcion|suscripciones|premium|meses gratis|mes gratis|dias gratis|prueba gratis|"
                            r"prueba gratuita|periodo de prueba)(?![a-z0-9])")
RE_GRUPO_SOFTWARE = re.compile(r"aplicacion|software|antivirus|vpn|videojuego|(^|-)juegos?(-|$)|suscripcion|streaming")
RE_NINTENDO = re.compile(r"(?<![a-z0-9])(nintendo|switch|joy-?con|amiibo)(?![a-z0-9])")


def es_app_o_juego_gratis(o: dict) -> bool:
    """¿Es un juego, una app, un programa o una prueba de suscripción gratis? Con los grupos de la página de la oferta en Chollometro
    («Totalmente gratis», «Aplicaciones Android», «Antivirus», «Juegos gratis»...) se decide por ellos;
    en los listados solo hay el grupo principal, así que se usan el título y la tienda."""
    titulo = RE_ENVIO_GRATIS.sub(" ", normalizar(o.get("titulo")))
    if RE_NINTENDO.search(titulo):            # nada de Nintendo, ni gratis
        return False
    grupos = [normalizar(g) for g in (o.get("grupos") or [])]
    if grupos:
        gratis = "gratis" in grupos or ((o.get("precio") or 0) == 0 and bool(RE_GRATIS.search(titulo)))
        return gratis and any(RE_GRUPO_SOFTWARE.search(g) and "mesa" not in g for g in grupos)
    if not es_gratis(o):
        return False
    return bool(RE_SOFTWARE.search(titulo) or RE_SUSCRIPCION.search(titulo)
                or normalizar(o.get("tienda")) in TIENDAS_SOFTWARE or o.get("grupo") == GRUPO_GAMING)


CATEGORIA_JUEGOS_GRATIS = {"id": "juegos_gratis", "nombre": "Gratis: juego, app o suscripción", "emoji": "🎁", "activo": True,
                           "incluir": [], "excluir": [], "forzar": [], "avisos": [], "necesita": [],
                           "grados": None, "precio_max": None, "precio_min": None, "tema": None,
                           "re_incluir": None, "re_excluir": None, "re_forzar": None, "re_necesita": None,
                           "re_avisos": []}


def _encaja(c: dict, texto: str) -> bool:
    """¿El texto (ya normalizado) entra en la categoría c? (sin mirar precios)"""
    if c["re_forzar"] and c["re_forzar"].search(texto):
        return True
    if not c["re_incluir"] or not c["re_incluir"].search(texto):
        return False
    if c["re_excluir"] and c["re_excluir"].search(texto):
        return False
    return not (c["re_necesita"] and not c["re_necesita"].search(texto))


def es_almacenamiento(o: dict, cats) -> bool:
    """Discos, SSD, M.2 y tarjetas nunca son juegos, aunque Chollometro los meta en el grupo Gaming
    (p. ej. «SanDisk microSDXC para Switch» o «Seagate Game Drive para PS5»)."""
    texto = normalizar(o.get("titulo"))
    return any(_encaja(c, texto) for c in cats
               if "ssd" in normalizar(c["nombre"]) or "disco" in normalizar(c["nombre"]))


def clasificar(o: dict, cats, cfg, excluir_global) -> list[str]:
    # Juegos de cualquier plataforma: solo si son gratis (el almacenamiento nunca cuenta como juego)
    quiere_gratis = (cfg.get("juegos") or {}).get("avisar_gratis", True)
    if es_juego(o) and not es_almacenamiento(o, cats):
        return ["juegos_gratis"] if quiere_gratis and es_app_o_juego_gratis(o) and \
            not (excluir_global and excluir_global.search(normalizar(o.get("titulo")))) else []
    texto = " ".join([o.get("titulo", ""), " ".join(o.get("cats_rss", []))])
    if cfg.get("buscar_en_descripcion"):
        texto += " " + quitar_html(o.get("descripcion"))[:600]
    texto = normalizar(texto)
    if excluir_global and excluir_global.search(texto):
        return []
    tiendas = [normalizar(t) for t in cfg.get("tiendas_permitidas", []) if t]
    if tiendas and normalizar(o.get("tienda")) not in tiendas:
        return []
    precio = o.get("precio")
    elegidas = []
    for c in cats:
        if not _encaja(c, texto):
            continue
        if precio is not None and c["precio_max"] is not None and precio > c["precio_max"]:
            continue
        if precio is not None and c["precio_min"] is not None and precio < c["precio_min"]:
            continue
        elegidas.append(c["id"])
    if quiere_gratis and "juegos_gratis" not in elegidas and es_app_o_juego_gratis(o):
        elegidas.append("juegos_gratis")
    return elegidas


def umbral(o: dict, cats_por_id: dict, minimo_global: float) -> float:
    """El mínimo de grados de una oferta es el más bajo de sus categorías, salvo que alguna tenga un mínimo
    propio MÁS ALTO que el general (p. ej. portátiles a 500°): entonces manda ese, para que un
    «portátil + auriculares de regalo» no se cuele a 200° por la categoría de auriculares."""
    cats = [cats_por_id[c] for c in o.get("categorias", []) if c in cats_por_id]
    estrictos = [c["grados"] for c in cats if c["grados"] is not None and c["grados"] > minimo_global]
    if estrictos:
        return max(estrictos)
    valores = [c["grados"] if c["grados"] is not None else minimo_global for c in cats]
    return min(valores) if valores else minimo_global


def advertencias(o: dict, cats) -> list[str]:
    texto = normalizar(o.get("titulo", ""))
    salida = []
    for c in cats:
        if c["id"] not in o.get("categorias", []):
            continue
        for regla, aviso in c["re_avisos"]:
            if regla and regla.search(texto) and aviso not in salida:
                salida.append(aviso)
    return salida


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------
def en_silencio(cfg) -> bool:
    s = cfg.get("silencio") or {}
    if not s.get("activo") or ZoneInfo is None:
        return False
    ahora = datetime.now(ZoneInfo(s.get("zona", "Europe/Madrid"))).strftime("%H:%M")
    desde, hasta = s.get("desde", "00:00"), s.get("hasta", "00:00")
    return (desde <= ahora < hasta) if desde <= hasta else (ahora >= desde or ahora < hasta)


ULTIMO_ENVIO: dict = {}     # message_id del último aviso enviado (para marcarlo luego como agotado)


def _apuntar_envio(respuesta: dict, foto: bool):
    res = respuesta.get("result") if isinstance(respuesta, dict) else None
    if isinstance(res, dict) and res.get("message_id"):
        ULTIMO_ENVIO.clear()
        ULTIMO_ENVIO.update({"message_id": res["message_id"], "foto": foto})


def telegram_api(metodo: str, datos: dict) -> tuple[bool, str, dict]:
    """Igual que _telegram_llamar, pero devuelve también la respuesta de Telegram."""
    token = os.environ.get("TELEGRAM_TOKEN")
    base = os.environ.get("TELEGRAM_API", "https://api.telegram.org")
    url = f"{base}/bot{token}/{metodo}"
    err = "sin respuesta"
    for _ in range(3):
        req = urllib.request.Request(url, data=json.dumps(datos).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                resp = json.load(r)
                return bool(resp.get("ok")), "", resp
        except urllib.error.HTTPError as ex:
            cuerpo = ex.read().decode("utf-8", "replace")
            if ex.code == 429:
                try:
                    espera = json.loads(cuerpo)["parameters"]["retry_after"]
                except (KeyError, ValueError):
                    espera = 10
                time.sleep(espera + 1)
                continue
            return False, f"{ex.code}: {cuerpo[:300]}", {}
        except (urllib.error.URLError, TimeoutError) as ex:
            err = str(ex)
            time.sleep(5)
    return False, err, {}


def _telegram_llamar(metodo: str, datos: dict) -> tuple[bool, str]:
    token = os.environ.get("TELEGRAM_TOKEN")
    base = os.environ.get("TELEGRAM_API", "https://api.telegram.org")
    url = f"{base}/bot{token}/{metodo}"
    for _ in range(3):
        req = urllib.request.Request(url, data=json.dumps(datos).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                resp = json.load(r)
                if metodo in ("sendMessage", "sendPhoto"):
                    _apuntar_envio(resp, metodo == "sendPhoto")
                return bool(resp.get("ok")), ""
        except urllib.error.HTTPError as ex:
            cuerpo = ex.read().decode("utf-8", "replace")
            if ex.code == 429:
                try:
                    espera = json.loads(cuerpo)["parameters"]["retry_after"]
                except (KeyError, ValueError):
                    espera = 10
                time.sleep(espera + 1)
                continue
            return False, f"{ex.code}: {cuerpo[:300]}"
        except (urllib.error.URLError, TimeoutError) as ex:
            err = str(ex)
            time.sleep(5)
    return False, locals().get("err", "sin respuesta")


def _telegram_subir_foto(datos: dict, imagen: bytes) -> tuple[bool, str]:
    token = os.environ.get("TELEGRAM_TOKEN")
    base = os.environ.get("TELEGRAM_API", "https://api.telegram.org")
    limite = "----radar" + str(int(time.time() * 1000))
    partes = []
    for k, v in datos.items():
        v = json.dumps(v) if isinstance(v, (dict, list, bool)) else str(v)
        partes.append(f'--{limite}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    partes.append(f'--{limite}\r\nContent-Disposition: form-data; name="photo"; filename="oferta.png"\r\n'
                  f"Content-Type: image/png\r\n\r\n".encode() + imagen + b"\r\n")
    partes.append(f"--{limite}--\r\n".encode())
    cuerpo_envio = b"".join(partes)
    err = "sin respuesta"
    for _ in range(3):
        req = urllib.request.Request(f"{base}/bot{token}/sendPhoto", data=cuerpo_envio,
                                     headers={"Content-Type": f"multipart/form-data; boundary={limite}"})
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                resp = json.load(r)
                _apuntar_envio(resp, True)
                return bool(resp.get("ok")), ""
        except urllib.error.HTTPError as ex:
            cuerpo = ex.read().decode("utf-8", "replace")
            if ex.code == 429:          # demasiados mensajes: Telegram dice cuánto esperar
                try:
                    espera = json.loads(cuerpo)["parameters"]["retry_after"]
                except (KeyError, ValueError):
                    espera = 10
                time.sleep(espera + 1)
                continue
            return False, f"{ex.code}: {cuerpo[:200]}"
        except (urllib.error.URLError, TimeoutError) as ex:
            err = str(ex)
            time.sleep(5)
    return False, err


def telegram(texto: str, silencioso: bool = False, foto: str | bytes | list | None = None,
             botones: list | None = None, tema: int | None = None) -> bool:
    if not os.environ.get("TELEGRAM_TOKEN") or not os.environ.get("TELEGRAM_CHAT_ID"):
        log("  (sin TELEGRAM_TOKEN/TELEGRAM_CHAT_ID: no se envía)")
        log("  " + texto.replace("\n", "\n  "))
        return False
    base = {"chat_id": os.environ["TELEGRAM_CHAT_ID"], "parse_mode": "HTML",
            "disable_notification": silencioso}
    tema = tema or (int(os.environ["TELEGRAM_THREAD_ID"]) if os.environ.get("TELEGRAM_THREAD_ID") else None)
    if tema:
        base["message_thread_id"] = tema
    if botones:
        base["reply_markup"] = {"inline_keyboard": botones}
    ULTIMO_ENVIO.clear()
    for f in ([foto] if isinstance(foto, (str, bytes)) else (foto or [])):
        if not f or len(texto) > 1024:
            continue
        if isinstance(f, bytes):
            ok, err = _telegram_subir_foto({**base, "caption": texto}, f)
        elif f.startswith("http"):
            ok, err = _telegram_llamar("sendPhoto", {**base, "photo": f, "caption": texto})
        else:
            continue
        if ok:
            return True
        log(f"  No se pudo mandar con foto ({err[:120]}); pruebo la siguiente opción")
    ok, err = _telegram_llamar("sendMessage", {**base, "text": texto})
    if not ok:
        log(f"  Telegram: {err}")
    return ok


def e(texto) -> str:
    return html.escape(str(texto or ""), quote=False)


EMOJI_TIENDA = {"amazon": "🟠", "aliexpress": "🔴"}


def cabecera_tienda(o: dict) -> str:
    tienda = o.get("tienda") or "Tienda sin identificar"
    emoji = next((v for k, v in EMOJI_TIENDA.items() if k in normalizar(tienda)), "🏪")
    cupon = "🎟️ <b>CUPÓN</b> · " if tipo_hilo(o) == "cupon" else ""
    return f"{cupon}{emoji} <b>{e(tienda.upper())}</b>"


def mensaje(o: dict, cats_por_id: dict) -> str:
    etiquetas = " · ".join(f"{cats_por_id[c]['emoji']} {cats_por_id[c]['nombre']}"
                           for c in o.get("categorias", []) if c in cats_por_id)
    if o.get("grados") is not None:
        medida = f"🔥 <b>{int(o['grados'])}°</b>"
    else:
        medida = (f"🏷️ <b>−{o['descuento']}%</b>" if o.get("descuento") else "🏷️") + \
            f" · <i>{e(o.get('fuente', ''))}</i>"
    lineas = [f"{cabecera_tienda(o)}  ·  {medida}"]
    if o.get("error_precio"):
        lineas.append("⚠️ <b>¿ERROR DE PRECIO?</b> Suelen durar poco")
    lineas.append(f"<b>{e(o['titulo'])}</b>")
    precio = ""
    if o.get("precio") is not None:
        precio = f"💶 <b>{formatear_precio(o['precio'])}</b>"
        if o.get("precio_antes"):
            pct = round((1 - o["precio"] / o["precio_antes"]) * 100)
            precio += f" <s>{formatear_precio(o['precio_antes'])}</s> (−{pct}%)"
    if precio:
        lineas.append(precio)
    eur_tb = euros_por_tb(o)
    if eur_tb:
        lineas.append(f"💾 {formatear_precio(eur_tb)}/TB")
    if o.get("codigo"):
        lineas.append(f"🏷️ Código: <code>{e(o['codigo'])}</code>")
    if o.get("keepa_veredicto"):
        lineas.append(e(o["keepa_veredicto"]["texto"]))
    lineas += [e(x) for x in precioreal_lineas(o)]
    lineas += [e(a) for a in o.get("advertencias", [])]
    if etiquetas:
        lineas.append(f"<i>{e(etiquetas)}</i>")
    return "\n".join(lineas)


MOSTRAR_BOTON_FUENTE = False


def botones(o: dict) -> list:
    fila = []
    if MOSTRAR_BOTON_FUENTE and (o.get("enlace") or "").startswith("http"):
        fuente = o.get("fuente") or "Chollometro"
        fila.append({"text": ("🔥 " if fuente == "Chollometro" else "📰 ") + fuente, "url": o["enlace"]})
    if (o.get("destino") or "").startswith("http") and (MOSTRAR_BOTON_FUENTE or not _es_de_la_fuente(o["destino"])):
        fila.append({"text": f"🛒 {o.get('tienda') or 'Tienda'}"[:30], "url": o["destino"]})
    filas = [fila] if fila else []
    comparadores = []
    if o.get("keepa"):
        comparadores.append({"text": "📈 Keepa", "url": o["keepa"]})
    if (o.get("precioreal") or {}).get("enlace"):
        comparadores.append({"text": "📊 PrecioReal", "url": o["precioreal"]["enlace"]})
    if comparadores:
        filas.append(comparadores)
    return filas


def tema_de(o: dict, cats_por_id: dict, cfg: dict | None = None) -> int | None:
    for tienda, tema in ((cfg or {}).get("temas_por_tienda") or {}).items():
        if tienda.startswith("_") or not str(tema).isdigit():
            continue
        if normalizar(tienda) in normalizar(o.get("tienda")):
            return int(tema)
    for c in o.get("categorias", []):
        if c in cats_por_id and cats_por_id[c].get("tema"):
            return cats_por_id[c]["tema"]
    return None


# --------------------------------------------------------------------------
# Estado
# --------------------------------------------------------------------------
def cargar_json(ruta: Path, defecto):
    try:
        return json.loads(ruta.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return defecto


def guardar_json(ruta: Path, datos):
    ruta.parent.mkdir(parents=True, exist_ok=True)
    tmp = ruta.with_suffix(".tmp")
    tmp.write_text(json.dumps(datos, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(ruta)


CAMPOS_WEB = ("id", "afiliado", "fuente", "descuento", "tipo_hilo", "titulo", "enlace", "destino", "tienda", "precio", "precio_antes", "codigo",
              "grados", "categorias", "imagen", "fecha", "detectado", "avisado", "advertencias",
              "asin", "keepa", "keepa_veredicto", "precioreal", "precioreal_lineas", "eur_tb", "error_precio")


def para_web(o: dict) -> dict:
    o = {**o, "tipo_hilo": tipo_hilo(o), "precioreal_lineas": precioreal_lineas(o), "eur_tb": euros_por_tb(o)}
    return {k: o.get(k) for k in CAMPOS_WEB if o.get(k) not in (None, "", [])}


# --------------------------------------------------------------------------
# Ronda principal
# --------------------------------------------------------------------------
RE_DESCUENTO = re.compile(r"\(\s*[-−]\s*(\d{1,2})\s*%\s*\)|[-−]\s?(\d{1,2})\s?%|(\d{1,2})\s?%\s*(?:de\s+)?(?:descuento|dto|rebaja)",
                          re.I)
RE_ENLACE_OTRAS = re.compile(r'href=["\'](https?://(?:[a-z]+\.)?(?:amazon\.es|amzn\.(?:to|eu)|a\.co|aliexpress\.[a-z]+|'
                             r's\.click\.aliexpress\.com|pccomponentes\.com|mediamarkt\.es|elcorteingles\.es)[^"\']*)', re.I)


RE_TIENDA_CON_ASIN = re.compile(r'href=["\']https?://(?:www\.)?ofertitas\.es/[^"\']*[?&](?:amp;)?asin=([A-Z0-9]{10})(?![A-Z0-9])',
                                re.I)


def descuento_en_texto(texto: str) -> int | None:
    m = RE_DESCUENTO.search(texto or "")
    return int(next(g for g in m.groups() if g)) if m else None


def parecido(a: str, b: str) -> float:
    from difflib import SequenceMatcher
    return SequenceMatcher(None, normalizar(a), normalizar(b)).ratio()


PALABRAS_VACIAS = set("""de del la el los las un una por para con sin y o en a al solo oferta ofertas precio envio gratis
    disco duro discos ssd externo externa interno interna portatil tarjeta memoria nuevo nueva color negro blanco
    pack unidad unidades version modelo edicion euros eur""".split())


VARIANTES = set("""extreme ultra pro plus evo select max mini lite portable slim shield go play high endurance
    elite prime basic basics red blue black green purple gold one touch expansion barracuda ironwolf canvio""".split())


def _huella(titulo: str) -> tuple[set, set, set]:
    """(capacidades, códigos de modelo, palabras) de un título, sin precios ni relleno."""
    t = normalizar(titulo)
    t = re.sub(r"\d+(?:[.,]\d+)?\s*(?:€|eur\b|euros?)|\(?[-−]\s?\d{1,2}\s?%\)?", " ", t)
    t = re.sub(r"(\d+(?:[.,]\d+)?)\s+(tb|gb)\b", r"\1\2", t)
    palabras = {p for p in re.findall(r"[a-z0-9]+", t) if p not in PALABRAS_VACIAS and len(p) > 1}
    capacidades = {p for p in palabras if re.fullmatch(r"\d+(?:[.,]\d+)?(tb|gb)", p)}
    modelos = {p for p in palabras - capacidades if re.search(r"\d", p) and re.search(r"[a-z]", p)}
    return capacidades, modelos, palabras


def mismo_producto(a: str, b: str) -> bool:
    """¿Dos títulos de webs distintas son el mismo producto? («Disco duro Toshiba Canvio 4TB» y
    «Disco duro Toshiba Canvio de 4 TB por sólo 147,99€») Capacidad y modelo tienen que coincidir."""
    if parecido(a, b) >= 0.85:
        return True
    ca, ma, pa = _huella(a)
    cb, mb, pb = _huella(b)
    if (ca or cb) and ca != cb:
        return False
    corto, largo, mc, ml = (pa, pb, ma, mb) if len(pa) <= len(pb) else (pb, pa, mb, ma)
    if mc and ml and not (mc & ml):
        return False
    if len(corto) < 2 or (pa ^ pb) & VARIANTES:     # «Extreme» frente a «Ultra», «P3» frente a «P3 Plus»
        return False
    return len(corto & largo) / len(corto) >= 0.75


def leer_fuentes_extra(cfg, cats, excluir_global, avisados: dict, recientes: list[str], iniciadas: dict):
    """Ofertas de otras webs (sin grados): entran si encajan y tienen el descuento mínimo.
    Devuelve (para_avisar, para_marcar_sin_avisar)."""
    salida, silenciosas = [], []
    for f in cfg.get("fuentes_extra", []):
        if not isinstance(f, dict) or not f.get("url") or f.get("activa") is False:
            continue
        nombre = f.get("nombre") or urllib.parse.urlparse(f["url"]).netloc
        try:
            items = list(leer_rss(descargar(f["url"])))
        except Exception as ex:  # noqa: BLE001
            log(f"Fuente {nombre}: ERROR {ex}")
            continue
        filtro = re.compile(f["solo_si_titulo"], re.I) if f.get("solo_si_titulo") else None
        minimo = f.get("descuento_minimo", 20)
        validas = 0
        for o in items:
            o["id"] = f"{slug(nombre)}-{o['id'].rstrip('/').rsplit('/', 1)[-1] or o['id']}"
            o["fuente"] = nombre
            if o["id"] in avisados or (filtro and not filtro.search(o["titulo"])):
                continue
            texto = o["titulo"] + " " + quitar_html(o.get("descripcion"))[:300]
            o["descuento"] = descuento_en_texto(o["titulo"]) or descuento_en_texto(texto)
            o["precio"] = o.get("precio") or precio_en_texto(o["titulo"])
            o["tienda"] = o.get("tienda") or adivinar_tienda(" ".join(o.get("cats_rss", [])), o["titulo"]) \
                or nombre
            gratis_juego = es_juego(o) and es_gratis(o) and not es_almacenamiento(o, cats)
            if not gratis_juego and (o["descuento"] or 0) < minimo:
                continue
            o["categorias"] = clasificar(o, cats, cfg, excluir_global)
            solo = f.get("solo_categorias")
            if solo:
                o["categorias"] = [c for c in o["categorias"] if c in solo]
            if not o["categorias"]:
                continue
            if any(mismo_producto(o["titulo"], t) for t in recientes):
                log(f"  {nombre}: «{o['titulo'][:50]}» ya avisada por otra fuente")
                avisados[o["id"]] = time.time()
                continue
            validas += 1
            o["detectado"] = time.time()
            (salida if iniciadas.get(nombre) else silenciosas).append(o)
            recientes.append(o["titulo"])
        log(f"Fuente {nombre}: {len(items)} entradas, {validas} encajan")
        iniciadas[nombre] = True
    return salida, silenciosas


def ya_avisado_mismo_asin(s: dict, destacadas: dict, ahora: float) -> str | None:
    """Mismo producto de Amazon avisado en los últimos 3 días (desde otra fuente o en otro chollo).
    Solo se vuelve a avisar si ahora está al menos un 3 % más barato."""
    asin = s.get("asin")
    if not asin:
        return None
    for d in destacadas.values():
        if d.get("id") == s.get("id") or d.get("asin") != asin or ahora - (d.get("avisado") or 0) > 72 * 3600:
            continue
        antes, ahora_precio = d.get("precio"), s.get("precio")
        if antes and ahora_precio and ahora_precio < antes * 0.97:
            continue
        return d.get("id")
    return None


def destino_desde_pagina(o: dict):
    """En fuentes externas, busca en su página el enlace a la tienda (para el botón y Keepa)."""
    if o.get("destino") or not (o.get("enlace") or "").startswith("http"):
        return
    try:
        texto = descargar(o["enlace"], intentos=0).decode("utf-8", "replace")
    except urllib.error.HTTPError as ex:
        if ex.code in (404, 410):          # la han retirado
            o["descartar"] = True
        return
    except Exception:  # noqa: BLE001
        return
    # Ofertitas (nov. 2026) enlaza a Amazon con su propia redirección: ofertitas.es/tienda/?asin=XXXXXXXXXX.
    # El primero es el del producto; los siguientes son otras ofertas de la barra lateral.
    m = RE_TIENDA_CON_ASIN.search(texto)
    if m:
        o["destino"] = f"https://www.amazon.es/dp/{m.group(1).upper()}"
        o["tienda"] = "Amazon"
    # En la página hay muchos enlaces (anuncios de Prime, otras ofertas...): solo vale el del producto.
    # Amazon: el primero que lleve ASIN (/dp/...) o un enlace corto amzn.to; otras tiendas: el primero de producto.
    enlaces = [html.unescape(m.group(1)) for m in RE_ENLACE_OTRAS.finditer(texto)] if not o.get("destino") else []
    amazon = [u for u in enlaces if "amazon." in u or "amzn." in u or "a.co/" in u]
    elegido = next((u for u in amazon if extraer_asin(u)), None) \
        or next((u for u in amazon if re.search(r"//(amzn\.(to|eu)|a\.co)/", u)), None)
    if not elegido and not amazon:
        elegido = next((u for u in enlaces if re.search(r"/item/\d+|s\.click\.aliexpress|\.html|/p/|/producto", u)), None)
    if elegido and not o.get("destino"):
        o["destino"] = elegido
    if not o.get("imagen"):
        m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', texto)
        if m:
            o["imagen"] = m.group(1)


CAMPOS_PAGINA = ("grados", "tienda", "caducada", "tipo", "es_cupon", "destino", "codigo", "precio_antes", "grupo",
                 "precioreal", "grupos")


def estimar_fechas(ofertas: dict):
    """Las búsquedas de Chollometro no traen la fecha de publicación y mezclan ofertas de hace semanas.
    Como el número de oferta va en orden, la fecha se estima con las que sí la traen (portada y nuevos)."""
    pares = sorted((int(h), o["fecha"]) for h, o in ofertas.items() if str(h).isdigit() and o.get("fecha"))
    if len(pares) < 10:
        return
    n = len(pares)
    mx, my = sum(p[0] for p in pares) / n, sum(p[1] for p in pares) / n
    var = sum((p[0] - mx) ** 2 for p in pares)
    if not var:
        return
    pendiente = sum((p[0] - mx) * (p[1] - my) for p in pares) / var   # segundos por número de oferta
    if pendiente <= 0:
        return
    for h, o in ofertas.items():
        if str(h).isdigit() and not o.get("fecha"):
            o["fecha"] = min(time.time(), my + pendiente * (int(h) - mx))
            o["fecha_estimada"] = True


def descubrir(cfg) -> tuple[dict, int, int, int]:
    """Lee feeds y listados. Devuelve {id: oferta}, fuentes, fallos y fallos del RSS."""
    ofertas: dict[str, dict] = {}
    fuentes = fallos = fallos_rss = 0
    for url in cfg.get("feeds_rss", []):
        fuentes += 1
        try:
            n = 0
            for o in leer_rss(descargar(url)):
                ofertas.setdefault(o["id"], o)
                n += 1
            log(f"RSS {url}: {n} entradas")
        except Exception as ex:  # noqa: BLE001
            fallos += 1
            fallos_rss += 1
            log(f"RSS {url}: ERROR {ex}")
    for url in cfg.get("paginas_listado", []):
        fuentes += 1
        try:
            hilos = hilos_en_html(descargar(url).decode("utf-8", "replace"))
            log(f"Listado {url}: {len(hilos)} ofertas con grados")
            for hid, h in hilos.items():
                o = ofertas.get(hid)
                if o:
                    # Los datos de la página mandan: grados, tienda real y estado
                    for k in CAMPOS_PAGINA:
                        if h.get(k) not in (None, ""):
                            o[k] = h[k]
                    for k in ("precio", "imagen", "fecha"):
                        if o.get(k) in (None, "") and h.get(k) not in (None, ""):
                            o[k] = h[k]
                elif h.get("enlace") and h.get("titulo"):
                    ofertas[hid] = {**h, "cats_rss": [], "descripcion": "", "fecha": None, "guid": ""}
        except Exception as ex:  # noqa: BLE001
            fallos += 1
            log(f"Listado {url}: ERROR {ex}")
    estimar_fechas(ofertas)
    for o in ofertas.values():
        if o.get("precio") is None:
            o["precio"] = precio_en_texto(o.get("titulo", ""))
        if not o.get("tienda"):
            o["tienda"] = adivinar_tienda(o.get("titulo"), quitar_html(o.get("descripcion"))[:300])
    return ofertas, fuentes, fallos, fallos_rss


# --------------------------------------------------------------------------
# Radar €/TB: todo el almacenamiento de Amazon.es (storagediskprices.com), cada hora
# --------------------------------------------------------------------------
TECNOLOGIAS_RADAR = ("hdd", "ssd", "nvme", "microsd")
RE_NO_ES_DISCO = re.compile(r"(?<![a-z0-9])(carcasa|carcasas|caja|cajas|enclosure|dock|docking|estacion de acoplamiento|"
                            r"adaptador|bahia|bahias|servidor|cinta|cintas|tape|lto|ultrium|cartucho|duplicador|"
                            r"clonador|lector|cable|rack|funda|estuche|reacondicionado|renovado)(?![a-z0-9])")


def titulo_corto(t: str, maximo: int = 110) -> str:
    """Los títulos de Amazon son kilométricos: se cortan por una palabra, con «…»."""
    t = re.sub(r"\s+", " ", t or "").strip()
    if len(t) <= maximo:
        return t
    corte = t[:maximo].rsplit(" ", 1)[0].rstrip(",;:-–|(")
    return corte + "…"


def _celda(fila: str, etiqueta: str) -> str:
    m = re.search(rf'data-label="{etiqueta}"[^>]*>(.*?)</td>', fila, re.S)
    return html.unescape(re.sub(r"<[^>]+>", " ", m.group(1))).strip() if m else ""


def leer_storagediskprices(texto: str) -> list[dict]:
    """Filas de la tabla: ASIN, título, capacidad (TB), precio, tecnología y si es usado."""
    salida = []
    for fila in re.findall(r'<tr><td class="prod".*?</tr>', texto, re.S):
        m = re.search(r'href="https?://www\.amazon\.es/dp/([A-Z0-9]{10})', fila)
        tit = re.search(r'<a [^>]*title="([^"]+)"', fila)
        if not m or not tit:
            continue
        cap = re.match(r"([\d.,]+)\s*(TB|GB)", _celda(fila, "Capacidad"), re.I)
        salida.append({
            "asin": m.group(1),
            "titulo": html.unescape(tit.group(1)),
            "cap": _a_tb(cap.group(1), cap.group(2).lower()) if cap else None,
            "precio": a_numero(_celda(fila, "Precio").replace("€", "")),
            "tec": _celda(fila, "Tecnología"),
            "usado": "badge-cond" in fila,
        })
    return salida


def _vale_para_radar(p: dict, cats, excluir_global, precio_min: float) -> bool:
    titulo = normalizar(p["titulo"])
    if p["usado"] or not p["precio"] or p["precio"] < precio_min or not p["cap"]:
        return False
    if normalizar(p["tec"]).split(" ")[0] not in TECNOLOGIAS_RADAR or "sas" in normalizar(p["tec"]):
        return False
    if RE_NO_ES_DISCO.search(titulo) or (excluir_global and excluir_global.search(titulo)):
        return False
    cap_titulo = capacidad_tb(p["titulo"])         # la del título tiene que cuadrar (descarta carcasas «hasta 24TB»)
    if not cap_titulo or abs(cap_titulo - p["cap"]) > p["cap"] * 0.1:
        return False
    return es_almacenamiento({"titulo": p["titulo"]}, cats)


def radar_eurtb(cfg, estado, cats, excluir_global, silencioso, destacadas, avisados, cats_por_id) -> int:
    conf = cfg.get("radar_eurtb") or {}
    if not conf.get("activo") or not conf.get("url"):
        return 0
    st = estado.setdefault("radar_eurtb", {"ultima": 0, "p": {}})
    ahora = time.time()
    if ahora - st.get("ultima", 0) < conf.get("cada_min", 60) * 60 - 30:
        return 0
    st["ultima"] = ahora
    try:
        filas = leer_storagediskprices(descargar(conf["url"]).decode("utf-8", "replace"))
    except Exception as ex:  # noqa: BLE001
        log(f"Radar €/TB: no se pudo leer ({ex})")
        return 0
    hoy = str(int(ahora // 86400))
    precio_min = float(conf.get("precio_minimo", 15))
    bajada = float(conf.get("bajada_minima", 15)) / 100
    dias_min = int(conf.get("dias_minimos", 2))
    id_discos = next((c["id"] for c in cats if "ssd" in c["id"] or "disco" in c["id"]), "ssd_m_2_y_discos")
    productos, candidatos, validos = st.setdefault("p", {}), [], 0
    for p in filas:
        if not _vale_para_radar(p, cats, excluir_global, precio_min):
            continue
        validos += 1
        d = productos.setdefault(p["asin"], {"h": {}})
        d["vis"] = ahora
        h = d["h"]
        antes = {k: v for k, v in h.items() if k != hoy}
        h[hoy] = min(h.get(hoy, p["precio"]), p["precio"])
        for k in sorted(h)[:-8]:
            h.pop(k)
        if len(antes) < dias_min:
            continue
        valores = sorted(antes.values())
        ref = valores[len(valores) // 2]
        precio = p["precio"]
        if precio <= ref * (1 - bajada) and precio < min(valores) - 0.009 and \
                (not d.get("av") or precio < d["av"] * 0.97 or ahora - d.get("avt", 0) > 7 * 86400):
            candidatos.append((1 - precio / ref, p, ref, d))
    for asin in [a for a, d in productos.items() if ahora - d.get("vis", 0) > 8 * 86400]:
        productos.pop(asin)
    log(f"Radar €/TB: {len(filas)} productos, {validos} discos vigilados, {len(candidatos)} bajadas")
    enviados = 0
    for caida, p, ref, d in sorted(candidatos, key=lambda x: -x[0])[: conf.get("max_avisos_por_ronda", 3)]:
        s = {"id": f"eurtb-{p['asin']}-{hoy}", "fuente": "Radar €/TB", "titulo": titulo_corto(p["titulo"]), "tienda": "Amazon",
             "precio": p["precio"], "precio_antes": round(ref, 2), "descuento": round(caida * 100),
             "destino": f"https://www.amazon.es/dp/{p['asin']}", "categorias": [id_discos],
             "eur_tb": round(p["precio"] / p["cap"], 2), "detectado": ahora}
        if s["id"] in avisados:
            continue
        enriquecer_amazon(s, cfg)
        enriquecer_precioreal(s, cfg)
        marcar_error_de_precio(s)
        if ya_avisado_mismo_asin(s, destacadas, ahora):
            log(f"  Radar €/TB {p['asin']}: ya avisado hace poco")
            d["av"], d["avt"] = p["precio"], ahora
            continue
        if telegram(mensaje(s, cats_por_id), silencioso, foto=fotos_para(s, cfg), botones=botones(s),
                    tema=tema_de(s, cats_por_id, cfg)) or not os.environ.get("TELEGRAM_TOKEN"):
            enviados += 1
            s["avisado"] = ahora
            avisados[s["id"]] = ahora
            destacadas[s["id"]] = para_web(s)
            apuntar_semana(estado, s)
            d["av"], d["avt"] = p["precio"], ahora
            guardar_json(RUTA_ESTADO, estado)
            time.sleep(3)
    return enviados


# --------------------------------------------------------------------------
# Keepa: tus seguimientos de precio, por RSS (cuenta gratuita de Keepa)
# --------------------------------------------------------------------------
RE_ASIN_KEEPA = re.compile(r"product/\d+-([A-Z0-9]{10})")


def keepa_rss(cfg, estado, silencioso, destacadas, avisados, cats_por_id) -> int:
    url = (os.environ.get("KEEPA_RSS") or "").strip()
    if not url:
        return 0
    st = estado.setdefault("keepa_rss", {"vistos": [], "iniciado": False})
    try:
        items = list(leer_rss(descargar(url)))
    except Exception as ex:  # noqa: BLE001
        log(f"Keepa RSS: no se pudo leer ({ex})")
        return 0
    vistos = set(st["vistos"])
    nuevos = [it for it in items if (it["guid"] or it["enlace"] or it["titulo"]) not in vistos]
    st["vistos"] = (st["vistos"] + [it["guid"] or it["enlace"] or it["titulo"] for it in nuevos])[-300:]
    if not st.get("iniciado"):
        st["iniciado"] = True
        log(f"Keepa RSS: primera lectura, {len(items)} entradas guardadas sin avisar")
        return 0
    enviados = 0
    for it in nuevos[:5]:
        desc = html.unescape(it.get("descripcion") or "")
        texto = f"{it['titulo']} {quitar_html(desc)}"
        asin = extraer_asin(it["enlace"]) or extraer_asin(desc)
        if not asin:
            m = RE_ASIN_KEEPA.search(it["enlace"] + " " + desc)
            asin = m.group(1) if m else None
        s = {"id": f"keepa-{asin or slug(it['titulo'])[:40]}-{int(time.time())}", "fuente": "Keepa",
             "titulo": it["titulo"] or (asin or "Producto de Amazon"), "tienda": "Amazon",
             "precio": precio_en_texto(it["titulo"]) or precio_en_texto(quitar_html(desc)),
             "destino": f"https://www.amazon.es/dp/{asin}" if asin else it["enlace"], "categorias": []}
        if asin:
            enriquecer_amazon(s, cfg)
            enriquecer_precioreal(s, cfg)
        resumen = re.sub(r"\s+", " ", quitar_html(desc)).strip()
        cuerpo = mensaje(s, cats_por_id).replace("🏷️ · <i>Keepa</i>", "🔔 <b>Tu seguimiento de Keepa</b>", 1)
        if resumen and resumen not in it["titulo"]:
            cuerpo += f"\n<i>{e(resumen[:200])}</i>"
        if telegram(cuerpo, silencioso, foto=[keepa_grafica_bytes(asin)] if asin else None, botones=botones(s)):
            enviados += 1
            time.sleep(3)
    log(f"Keepa RSS: {len(items)} entradas, {len(nuevos)} nuevas, {enviados} avisos")
    return enviados


# --------------------------------------------------------------------------
# Avisos que caducan: se marcan como «❌ Ya no está disponible»
# --------------------------------------------------------------------------
def apuntar_mensaje(estado: dict, s: dict, texto: str, botones_: list):
    """Guarda lo justo para poder editar luego el aviso (no se guarda el chat: el estado es público)."""
    if not ULTIMO_ENVIO.get("message_id") or not (s.get("enlace") or "").startswith("http"):
        return
    estado.setdefault("mensajes", {})[s["id"]] = {
        "m": ULTIMO_ENVIO["message_id"], "f": ULTIMO_ENVIO.get("foto", False), "t": texto, "b": botones_,
        "e": s["enlace"], "fu": s.get("fuente"), "ts": time.time(), "chk": time.time()}


def marcar_agotadas(estado: dict, ofertas: dict, cfg: dict) -> int:
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    msgs = estado.setdefault("mensajes", {})
    ahora, revisadas, editadas = time.time(), 0, 0
    for hid, m in list(msgs.items()):
        if ahora - m.get("ts", 0) > 72 * 3600:
            msgs.pop(hid)
            continue
        agotada = bool((ofertas.get(hid) or {}).get("caducada"))
        if not agotada and revisadas < 6 and ahora - m.get("chk", 0) > 2 * 3600:
            revisadas += 1
            m["chk"] = ahora
            try:
                if m.get("fu"):
                    descargar(m["e"], intentos=0)
                else:
                    agotada = bool((datos_de_pagina(m["e"], hid) or {}).get("caducada"))
            except urllib.error.HTTPError as ex:
                agotada = ex.code in (404, 410)
            except Exception:  # noqa: BLE001
                pass
        if not agotada or not chat:
            continue
        lineas = ["❌ <b>YA NO ESTÁ DISPONIBLE</b>"] + m["t"].split("\n")
        while m.get("f") and len("\n".join(lineas)) > 1024 and len(lineas) > 3:
            lineas.pop()      # quita líneas enteras del final (categorías...) para no romper el HTML
        datos = {"chat_id": chat, "message_id": m["m"], "parse_mode": "HTML",
                 "reply_markup": {"inline_keyboard": m.get("b") or []}}
        datos["caption" if m.get("f") else "text"] = "\n".join(lineas)
        ok, err, _ = telegram_api("editMessageCaption" if m.get("f") else "editMessageText", datos)
        if ok:
            editadas += 1
        else:
            log(f"  No se pudo marcar {hid} como agotada ({err[:100]})")
        msgs.pop(hid)
    if editadas:
        log(f"Avisos marcados como agotados: {editadas}")
    return editadas


# --------------------------------------------------------------------------
# Comandos por Telegram: /vigilar, /dejar, /lista, /ayuda
# --------------------------------------------------------------------------
AYUDA_COMANDOS = ("🤖 <b>Comandos del radar</b>\n"
                  "/vigilar <i>enlace de Amazon</i> [nombre] → vigilo su precio y te aviso si baja\n"
                  "/lista → lo que estoy vigilando\n"
                  "/dejar <i>número o enlace</i> → dejo de vigilarlo\n"
                  "Por privado también vale mandarme solo el enlace.")
RE_URL = re.compile(r"https?://\S+")


def productos_vigilados(estado: dict) -> list[dict]:
    quitados = set(estado.get("vigilar_quitados", []))
    lista = [p for p in leer_vigilar() if p["asin"] not in quitados]
    for p in estado.get("vigilar_extra", []):
        if p["asin"] not in {x["asin"] for x in lista}:
            lista.append({"asin": p["asin"], "nombre": p.get("nombre")})
    return lista


def _asin_de_texto(texto: str) -> str | None:
    for u in RE_URL.findall(texto):
        asin = extraer_asin(u) or extraer_asin(seguir_redirecciones(u))
        if asin:
            return asin
    m = re.search(r"(?<![A-Z0-9])(B0[A-Z0-9]{8})(?![A-Z0-9])", texto.upper())
    return m.group(1) if m else None


def atender_comandos(estado: dict) -> int:
    grupo = os.environ.get("TELEGRAM_CHAT_ID")
    if not os.environ.get("TELEGRAM_TOKEN") or not grupo:
        return 0
    datos = {"timeout": 0, "allowed_updates": ["message"]}
    if estado.get("tg_offset"):
        datos["offset"] = estado["tg_offset"] + 1
    ok, err, resp = telegram_api("getUpdates", datos)
    if not ok:
        log(f"Comandos: no se pudieron leer ({err[:120]})")
        return 0
    atendidos = 0
    for upd in resp.get("result", []):
        estado["tg_offset"] = max(estado.get("tg_offset", 0), upd.get("update_id", 0))
        msg = upd.get("message") or {}
        texto = (msg.get("text") or "").strip()
        chat = msg.get("chat") or {}
        if not texto:
            continue
        privado = chat.get("type") == "private"
        palabra = texto.split()[0].split("@")[0].lower() if texto.startswith("/") else ""
        if palabra not in ("/vigilar", "/dejar", "/lista", "/ayuda", "/start") and not (privado and RE_URL.search(texto)):
            continue
        if str(chat.get("id")) != str(grupo):          # por privado: solo admins del grupo
            okm, _, r = telegram_api("getChatMember", {"chat_id": grupo, "user_id": (msg.get("from") or {}).get("id")})
            if not okm or (r.get("result") or {}).get("status") not in ("creator", "administrator"):
                continue
        respuesta = responder_comando(estado, palabra or "/vigilar", texto)
        destino = {"chat_id": chat.get("id"), "text": respuesta, "parse_mode": "HTML",
                   "reply_parameters": {"message_id": msg.get("message_id"), "allow_sending_without_reply": True}}
        if msg.get("message_thread_id") and msg.get("is_topic_message"):
            destino["message_thread_id"] = msg["message_thread_id"]
        telegram_api("sendMessage", destino)
        atendidos += 1
    if atendidos:
        log(f"Comandos atendidos: {atendidos}")
    return atendidos


def responder_comando(estado: dict, palabra: str, texto: str) -> str:
    extra = estado.setdefault("vigilar_extra", [])
    quitados = estado.setdefault("vigilar_quitados", [])
    lista = productos_vigilados(estado)
    precios = estado.get("precios", {})
    if palabra in ("/ayuda", "/start"):
        return AYUDA_COMANDOS
    if palabra == "/lista":
        if not lista:
            return "No estoy vigilando nada. Usa /vigilar con un enlace de Amazon."
        filas = []
        for i, p in enumerate(lista, 1):
            d = precios.get(p["asin"], {})
            ref = f" · {formatear_precio(d['referencia'])}" if d.get("referencia") else " · (aún sin precio)"
            filas.append(f"{i}. {e(p.get('nombre') or d.get('nombre') or p['asin'])}{ref}")
        return "👀 <b>Vigilando</b>\n" + "\n".join(filas) + "\n\nPara quitar uno: /dejar número"
    partes = texto.split(None, 1)
    resto = (partes[1] if len(partes) > 1 else "") if texto.startswith("/") else texto
    if palabra == "/dejar":
        objetivo = None
        if resto.strip().isdigit() and 1 <= int(resto.strip()) <= len(lista):
            objetivo = lista[int(resto.strip()) - 1]["asin"]
        else:
            objetivo = _asin_de_texto(resto)
        if not objetivo or objetivo not in {p["asin"] for p in lista}:
            return "No lo encuentro en la lista. Mira los números con /lista."
        estado["vigilar_extra"] = [p for p in extra if p["asin"] != objetivo]
        if objetivo in {p["asin"] for p in leer_vigilar()} and objetivo not in quitados:
            quitados.append(objetivo)
        return f"🗑️ Dejo de vigilar {e(objetivo)}."
    asin = _asin_de_texto(resto)
    if not asin:
        return "No he encontrado un producto de Amazon en el mensaje. Ejemplo: /vigilar https://www.amazon.es/dp/B0CV7PGS46"
    if asin in {p["asin"] for p in lista}:
        return f"Ya lo estoy vigilando ({e(asin)})."
    nombre = re.sub(r"\s+", " ", RE_URL.sub(" ", resto)).strip() or None
    if asin in quitados:
        quitados.remove(asin)
    if asin not in {p["asin"] for p in productos_vigilados(estado)}:
        extra.append({"asin": asin, "nombre": nombre})
    return f"✅ Vigilo {e(nombre or asin)}. En unos minutos te mando su precio de ahora y te aviso si baja."


# --------------------------------------------------------------------------
# Resumen semanal
# --------------------------------------------------------------------------
def apuntar_semana(estado: dict, s: dict):
    estado.setdefault("semana", []).append({
        "ts": time.time(), "t": (s.get("titulo") or "")[:90], "c": s.get("categorias", []), "g": s.get("grados"),
        "p": s.get("precio"), "d": s.get("destino") if (s.get("destino") or "").startswith("http") else None,
        "e": euros_por_tb(s), "ti": s.get("tienda")})
    estado["semana"] = [x for x in estado["semana"] if time.time() - x["ts"] < 8 * 86400]


def resumen_semanal(cfg: dict, estado: dict, cats_por_id: dict) -> bool:
    conf = cfg.get("resumen_semanal") or {}
    if not conf.get("activo") or ZoneInfo is None or not os.environ.get("TELEGRAM_CHAT_ID"):
        return False
    local = datetime.now(ZoneInfo("Europe/Madrid"))
    semana = local.strftime("%G-%V")
    if local.weekday() != int(conf.get("dia", 6)) or local.hour < int(conf.get("hora", 20)) \
            or estado.get("resumen_enviado") == semana:
        return False
    avisos = [x for x in estado.get("semana", []) if time.time() - x["ts"] < 7 * 86400]
    estado["resumen_enviado"] = semana
    if not avisos:
        return False
    def enlace(x):
        t = e(x["t"][:70])
        return f'<a href="{html.escape(x["d"], quote=True)}">{t}</a>' if x.get("d") else t
    from collections import Counter
    por_cat = Counter(c for x in avisos for c in x["c"] if c in cats_por_id)
    lineas = [f"📊 <b>Resumen de la semana</b> · {len(avisos)} avisos"]
    if por_cat:
        lineas.append(" · ".join(f"{cats_por_id[c]['emoji']} {n}" for c, n in por_cat.most_common(8)))
    top = sorted((x for x in avisos if x.get("g")), key=lambda x: -x["g"])[:5]
    if top:
        lineas.append("\n🔥 <b>Los más votados</b>")
        lineas += [f"• {enlace(x)} — {formatear_precio(x['p']) if x.get('p') is not None else ''} · {int(x['g'])}°"
                   for x in top]
    discos = sorted((x for x in avisos if x.get("e")), key=lambda x: x["e"])[:3]
    if discos:
        lineas.append("\n💾 <b>Mejor precio por TB</b>")
        lineas += [f"• {enlace(x)} — {formatear_precio(x['e'])}/TB ({formatear_precio(x['p'])})" for x in discos]
    datos = {"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": "\n".join(lineas), "parse_mode": "HTML",
             "link_preview_options": {"is_disabled": True}}
    if os.environ.get("TELEGRAM_THREAD_ID"):
        datos["message_thread_id"] = int(os.environ["TELEGRAM_THREAD_ID"])
    ok, err, _ = telegram_api("sendMessage", datos)
    log(f"Resumen semanal: {'enviado' if ok else 'ERROR ' + err[:100]}")
    return ok


def ronda(cfg, cats, excluir_global, diagnostico=False) -> int:
    ahora = time.time()
    cats_por_id = {c["id"]: c for c in cats}
    minimo = float(cfg.get("grados_minimos", 200))
    cfg_keepa = cfg.get("keepa") or {}
    cfg_err = cfg.get("errores_de_precio") or {}

    def umbral_de(s: dict) -> float:
        """Mínimo de grados de la oferta. Vía rápida: los errores de precio de tus categorías (no los gratis)
        avisan antes, porque suelen durar minutos. Las categorías con un mínimo propio más alto que el
        general (p. ej. portátiles a 500°) no tienen vía rápida: esperan a su mínimo aunque sea un error."""
        u = umbral(s, cats_por_id, minimo)
        if u <= minimo and cfg_err.get("via_rapida") and error_en_titulo(s) and \
                any(c != "juegos_gratis" for c in s.get("categorias", [])):
            u = min(u, float(cfg_err.get("grados_minimos", 100)))
        return u

    primera = not RUTA_ESTADO.exists()
    estado = cargar_json(RUTA_ESTADO, {})
    seguimiento: dict = estado.setdefault("seguimiento", {})
    avisados: dict = estado.setdefault("avisados", {})
    salud: dict = estado.setdefault("salud", {"rondas_sin_grados": 0, "aviso_salud": 0})
    web = cargar_json(RUTA_OFERTAS, {})
    destacadas = {o["id"]: o for o in web.get("destacadas", [])}

    ofertas, fuentes, fallos, fallos_rss = descubrir(cfg)
    if fuentes and fallos == fuentes:
        log("No se ha podido leer ninguna fuente.")
        return 2

    # 1) Clasificar lo nuevo y meterlo en seguimiento
    nuevas = 0
    for hid, o in ofertas.items():
        if hid in avisados:
            continue
        if no_es_oferta(o, cfg) or o.get("caducada"):
            seguimiento.pop(hid, None)
            continue
        max_h = cfg.get("max_antiguedad_horas")
        if max_h and o.get("fecha") and ahora - o["fecha"] > max_h * 3600:
            seguimiento.pop(hid, None)
            continue
        elegidas = clasificar(o, cats, cfg, excluir_global)
        if not elegidas:
            seguimiento.pop(hid, None)  # p. ej. si has quitado una palabra
            continue
        s = seguimiento.get(hid)
        if s is None:
            nuevas += 1
            s = {"detectado": ahora, "comprobado": 0}
        for k in ("titulo", "enlace", "tienda", "precio", "precio_antes", "codigo", "imagen",
                  "fecha", "caducada", "destino", "tipo", "es_cupon", "grupo"):
            if o.get(k) not in (None, ""):
                s[k] = o[k]
        s["id"], s["categorias"] = hid, elegidas
        if o.get("grados") is not None:
            s["grados"], s["comprobado"] = o["grados"], ahora
        seguimiento[hid] = s
    log(f"En seguimiento: {len(seguimiento)} ({nuevas} nuevas)")

    # 2) Volver a mirar los grados de las que lo necesitan
    cada = cfg.get("recomprobar_cada_min", 30) * 60
    pendientes = sorted((s for s in seguimiento.values()
                         if s.get("enlace") and ahora - s.get("comprobado", 0) >= cada
                         and (s.get("grados") or 0) < umbral_de(s)),
                        key=lambda s: s.get("comprobado", 0))
    limite = 3 if diagnostico else cfg.get("max_consultas_por_ronda", 40)
    leidas = intentadas = 0
    for s in pendientes[:limite]:
        intentadas += 1
        try:
            d = datos_de_pagina(s["enlace"], s["id"])
        except urllib.error.HTTPError as ex:
            if ex.code in (404, 410):   # Chollometro la ha borrado
                s["descartar"] = True
            else:
                log(f"  {s['id']}: error al abrir la oferta ({ex})")
            d = None
        except Exception as ex:  # noqa: BLE001
            log(f"  {s['id']}: error al abrir la oferta ({ex})")
            d = None
        s["comprobado"] = ahora
        if d and d.get("grados") is not None:
            leidas += 1
            for k in CAMPOS_PAGINA:
                if d.get(k) not in (None, ""):
                    s[k] = d[k]
            for k in ("precio", "imagen"):
                if s.get(k) in (None, "") and d.get(k) not in (None, ""):
                    s[k] = d[k]
            if no_es_oferta({**s, **d}, cfg):
                s["descartar"] = True
        if diagnostico:
            log(f"  {s['id']} → {d}")
        time.sleep(cfg.get("pausa_entre_consultas_seg", 1.0))
    if intentadas:
        log(f"Grados leídos: {leidas}/{intentadas}")

    # 3) Elegir las que ya pasan el mínimo
    listas = sorted((s for s in seguimiento.values()
                     if (s.get("grados") or 0) >= umbral_de(s)
                     and not s.get("caducada") and not s.get("descartar")),
                    key=lambda s: -s["grados"])

    if diagnostico:
        log("\n— DIAGNÓSTICO —")
        con_grados = sum(1 for o in ofertas.values() if o.get("grados") is not None)
        log(f"Ofertas leídas: {len(ofertas)} · con grados ya en el listado: {con_grados}")
        for o in list(ofertas.values())[:8]:
            log(f"  [{o['id']}] {o.get('grados')}° · {formatear_precio(o.get('precio'))} · "
                f"{o.get('tienda')} · {o.get('titulo', '')[:70]}")
            log(f"      {o.get('enlace')}  →  tienda: {o.get('destino')}")
        muestra = next((o for o in ofertas.values() if "amazon" in normalizar(o.get("tienda"))), None) \
            or next(iter(ofertas.values()), None)
        if muestra and muestra.get("enlace"):
            log(f"\nOferta de muestra: {muestra.get('titulo', '')[:60]}\n  {muestra['enlace']}")
            try:
                d = datos_de_pagina(muestra["enlace"], muestra["id"]) or {}
                log(f"  Página de la oferta → grados: {d.get('grados')} · tienda: {d.get('tienda')} · "
                    f"caducada: {d.get('caducada')} · enlace tienda: {d.get('destino')}")
                if "amazon" in normalizar(muestra.get("tienda")):
                    prueba = {**muestra, **{k: v for k, v in d.items() if v}}
                    asin = buscar_asin(prueba)
                    log(f"  ASIN: {asin} · destino final: {str(prueba.get('destino'))[:120]}")
                    if asin:
                        for nombre, url in (("Keepa", keepa_grafica(asin)),):
                            try:
                                b = descargar(url, intentos=0)
                                log(f"  Gráfica {nombre}: {len(b)} bytes · cabecera {b[:8]!r} · {url}")
                            except Exception as ex:  # noqa: BLE001
                                log(f"  Gráfica {nombre}: ERROR {ex} · {url}")
            except Exception as ex:  # noqa: BLE001
                log(f"  error: {ex}")
        log(f"Pasarían el mínimo: {len(listas)}")
        for s in listas[:3]:
            repasar_pagina(s)
            completar_destino(s)
            enriquecer_amazon(s, cfg)
            enriquecer_precioreal(s, cfg)
            log("\n" + mensaje({**s, "advertencias": advertencias(s, cats)}, cats_por_id))
            log(f"  botones: {botones(s)}")
        return 0

    # 3b) Otras fuentes (sin grados: criterio de descuento mínimo)
    recientes = [o.get("titulo", "") for o in destacadas.values()
                 if ahora - (o.get("avisado") or 0) < 72 * 3600] + [s.get("titulo", "") for s in listas]
    iniciadas = estado.setdefault("fuentes_iniciadas", {})
    extra, silenciosas = leer_fuentes_extra(cfg, cats, excluir_global, avisados, recientes, iniciadas)
    for o in silenciosas:   # primera vez que se lee una fuente: no inundar el grupo
        o["avisado"] = ahora
        avisados[o["id"]] = ahora
        destacadas[o["id"]] = para_web(o)
    if silenciosas:
        log(f"Fuentes nuevas: {len(silenciosas)} ofertas guardadas sin avisar (primera lectura)")
    listas = listas + extra

    # 4) Avisar
    silencioso = en_silencio(cfg)
    enviados = 0
    if primera:
        for s in listas:
            s["avisado"] = ahora
            s["advertencias"] = advertencias(s, cats)
            avisados[s["id"]] = ahora
            destacadas[s["id"]] = para_web(s)
            seguimiento.pop(s["id"], None)
        activas = ", ".join(f"{c['emoji']} {c['nombre']}" for c in cats)
        telegram(f"✅ <b>Radar de chollos en marcha</b>\nVigilando: {e(activas)}\n"
                 f"Solo aviso de ofertas con {int(minimo)}° o más.", silencioso)
        log(f"Primera ronda: {len(listas)} ofertas ya calientes guardadas sin avisar.")
    else:
        sin_telegram = not os.environ.get("TELEGRAM_TOKEN")
        for s in listas[: cfg.get("max_avisos_por_ronda", 10)]:
            # Volver a clasificar con las palabras de ahora (puede llevar días en seguimiento)
            s["categorias"] = clasificar(s, cats, cfg, excluir_global)
            if not s["categorias"]:
                log(f"  {s['id']}: ya no encaja con palabras.txt; no se avisa")
                seguimiento.pop(s["id"], None)
                continue
            s["advertencias"] = advertencias(s, cats)
            if s.get("fuente"):
                destino_desde_pagina(s)
                if s.get("descartar") or not (s.get("destino") or "").startswith("http"):
                    log(f"  {s['id']}: {'ya no está en ' + s['fuente'] if s.get('descartar') else 'sin enlace a la tienda'}"
                        f"; no se avisa")
                    avisados[s["id"]] = ahora
                    continue
            else:
                repasar_pagina(s)
                if s.get("caducada") or s.get("descartar"):
                    log(f"  {s['id']}: ya no está activa en Chollometro; no se avisa")
                    seguimiento.pop(s["id"], None)
                    continue
                if s.get("grupos"):     # con los grupos de la página se confirma (sobre todo los gratis)
                    s["categorias"] = clasificar(s, cats, cfg, excluir_global)
                    if not s["categorias"]:
                        log(f"  {s['id']}: vista la oferta, no encaja (grupos: {', '.join(s['grupos'])}); no se avisa")
                        avisados[s["id"]] = ahora
                        seguimiento.pop(s["id"], None)
                        continue
                completar_destino(s)
            enriquecer_amazon(s, cfg)
            enriquecer_precioreal(s, cfg)
            marcar_error_de_precio(s)
            if s.get("destino") and _es_de_la_fuente(s["destino"]) and not MOSTRAR_BOTON_FUENTE:
                s["destino"] = destino_real(s["destino"]) or s["destino"]
            previo = ya_avisado_mismo_asin(s, destacadas, ahora)
            if previo:
                log(f"  {s['id']}: es el mismo producto que {previo}, avisado hace menos de 3 días; no se repite")
                avisados[s["id"]] = ahora
                seguimiento.pop(s["id"], None)
                continue
            veredicto = (s.get("keepa_veredicto") or {}).get("nivel")
            if cfg_keepa.get("descartar_falsas") and veredicto == "rojo":
                log(f"  {s['id']}: Keepa dice que no es rebaja; no se avisa")
                avisados[s["id"]] = ahora
                seguimiento.pop(s["id"], None)
                continue
            fotos = fotos_para(s, cfg)
            texto_aviso, botones_aviso = mensaje(s, cats_por_id), botones(s)
            if telegram(texto_aviso, silencioso, foto=fotos, botones=botones_aviso,
                        tema=tema_de(s, cats_por_id, cfg)) or sin_telegram:
                enviados += 1
                s["avisado"] = ahora
                avisados[s["id"]] = ahora
                destacadas[s["id"]] = para_web(s)
                seguimiento.pop(s["id"], None)
                apuntar_mensaje(estado, s, texto_aviso, botones_aviso)
                apuntar_semana(estado, s)
                guardar_json(RUTA_ESTADO, estado)   # si algo falla después, no se repite el aviso
                if not sin_telegram:
                    time.sleep(3)  # Telegram limita los mensajes por minuto en grupos
        log(f"Avisos enviados: {enviados}")

    # 5) Salud: avisar una vez al día si algo va mal de forma continuada
    if intentadas and leidas == 0:
        salud["rondas_sin_grados"] = salud.get("rondas_sin_grados", 0) + 1
    elif leidas:
        salud["rondas_sin_grados"] = 0
    salud["rondas_sin_rss"] = salud.get("rondas_sin_rss", 0) + 1 if fallos_rss else 0
    if (salud["rondas_sin_grados"] >= 12 or salud["rondas_sin_rss"] >= 12) \
            and ahora - salud.get("aviso_salud", 0) > 86400:
        telegram("⚠️ <b>Radar de chollos</b>: llevo un par de horas sin poder leer bien Chollometro. "
                 "Puede que hayan cambiado la web o que estén bloqueando el acceso. "
                 "Lanza el modo «diagnostico» en GitHub → Actions para ver qué pasa.")
        salud["aviso_salud"] = ahora

    # 6) Limpieza
    horas = cfg.get("horas_seguimiento", 72) * 3600
    for hid in [h for h, s in seguimiento.items()
                if ahora - s.get("detectado", ahora) > horas or s.get("caducada") or s.get("descartar")]:
        seguimiento.pop(hid)
    for hid in [h for h, t in avisados.items() if ahora - t > 30 * 86400]:
        avisados.pop(hid)

    extras_ronda = (
        ("Radar €/TB", lambda: radar_eurtb(cfg, estado, cats, excluir_global, silencioso, destacadas, avisados,
                                           cats_por_id)),
        ("Keepa RSS", lambda: keepa_rss(cfg, estado, silencioso, destacadas, avisados, cats_por_id)),
        ("Agotadas", lambda: marcar_agotadas(estado, ofertas, cfg)),
        ("Comandos", lambda: atender_comandos(estado)),
        ("Vigilancia de precios", lambda: vigilar_precios(cfg, estado, silencioso)),
        ("Resumen semanal", lambda: resumen_semanal(cfg, estado, cats_por_id)),
    )
    for nombre_extra, tarea in extras_ronda:
        if primera and nombre_extra in ("Agotadas", "Resumen semanal"):
            continue
        try:
            tarea()
        except Exception as ex:  # noqa: BLE001
            log(f"{nombre_extra}: error {ex}")

    estado["ultima_ronda"] = ahora
    guardar_json(RUTA_ESTADO, estado)

    calentando = sorted(seguimiento.values(), key=lambda s: -(s.get("grados") or 0))[:60]
    guardar_json(RUTA_OFERTAS, {
        "generado": ahora,
        "grados_minimos": minimo,
        "categorias": [{"id": c["id"], "nombre": c["nombre"], "emoji": c["emoji"]} for c in cats],
        "destacadas": sorted(destacadas.values(), key=lambda o: -(o.get("avisado") or 0))[:200],
        "calentando": [para_web(s) for s in calentando],
    })
    return 0


def ejemplo_amazon(cfg, cats):
    """Manda al grupo un aviso de ejemplo con una oferta real de Amazon, para ver Keepa funcionando."""
    try:
        ofertas, *_ = descubrir(cfg)
    except Exception as ex:  # noqa: BLE001
        log(f"No se pudo leer Chollometro para el ejemplo: {ex}")
        return
    amazon = sorted((o for o in ofertas.values() if "amazon" in normalizar(o.get("tienda"))
                     and o.get("grados") is not None and tipo_hilo(o) == "oferta"),
                    key=lambda o: -o["grados"])
    if not amazon:
        log("Ahora mismo no hay ofertas de Amazon en portada para el ejemplo.")
        return
    s = None
    for o in amazon[:8]:            # mejor uno que tenga veredicto de PrecioReal, para verlo todo
        o = dict(o)
        repasar_pagina(o)
        if s is None or (o.get("precioreal") and not s.get("precioreal")):
            s = o
        if s.get("precioreal"):
            break
    s["categorias"] = clasificar(s, cats, cfg, None)
    completar_destino(s)
    enriquecer_amazon(s, cfg)
    enriquecer_precioreal(s, cfg)
    fotos = fotos_para(s, cfg)
    texto = "🧪 <b>EJEMPLO</b> (no es un aviso real)\n" + mensaje(s, {c["id"]: c for c in cats})
    ok = telegram(texto, foto=fotos, botones=botones(s))
    log(f"Ejemplo de Amazon ({s.get('asin')} · PrecioReal {s.get('precioreal')}): {'enviado' if ok else 'no enviado'}")


def revisar_palabras(cats_txt, globales, problemas):
    log(f"palabras.txt → {len(cats_txt)} categorías")
    for c in cats_txt:
        estado = "✅" if c["activo"] else "⏸️ apagada"
        extra = ", ".join(x for x in [
            f"grados ≥ {c['grados']:g}" if c["grados"] is not None else "",
            f"precio ≤ {c['precio_max']:g}" if c["precio_max"] is not None else "",
            f"precio ≥ {c['precio_min']:g}" if c["precio_min"] is not None else "",
            f"tema {c['tema']}" if c["tema"] else ""] if x)
        log(f"\n{c['emoji']} {c['nombre']}  [{estado}] {extra}")
        log(f"   entra:   {', '.join(c['incluir'])}")
        if c["forzar"]:
            log(f"   siempre: {', '.join(c['forzar'])}")
        if c["excluir"]:
            log(f"   fuera:   {', '.join(c['excluir'])}")
        if c.get("necesita"):
            log(f"   necesita alguna de: {', '.join(c['necesita'])}")
        for a in c["avisos"]:
            log(f"   aviso si {', '.join(a['si'])} → {a['texto']}")
    if globales:
        log(f"\n⛔ Excluir siempre: {', '.join(globales)}")
    log(f"\n🕹️ No son juegos (consolas y accesorios): {', '.join(NO_SON_JUEGOS) or '—'}")
    log("   Los juegos de cualquier plataforma solo avisan si son GRATIS.")
    compilar_categorias(cats_txt)
    for p in problemas:
        log(f"⚠️ {p}")
    return 1 if problemas else 0


def explorar(cfg):
    """Oculto: manda solo el aviso de ejemplo (para revisar cómo se ve)."""
    cats_txt, _, _ = leer_palabras()
    ejemplo_amazon(cfg, compilar_categorias(cats_txt) + [dict(CATEGORIA_JUEGOS_GRATIS)])


def main() -> int:
    ap = argparse.ArgumentParser(description="Radar de chollos de Chollometro → Telegram")
    ap.add_argument("--prueba", action="store_true", help="manda un mensaje de prueba a Telegram")
    ap.add_argument("--diagnostico", action="store_true", help="muestra lo que lee, sin avisar ni guardar")
    ap.add_argument("--palabras", action="store_true", help="comprueba palabras.txt")
    ap.add_argument("--explorar", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    cfg = json.loads(RUTA_CONFIG.read_text(encoding="utf-8"))
    if args.explorar:
        explorar(cfg)
        return 0
    cats_txt, globales, problemas = leer_palabras()
    if args.palabras:
        return revisar_palabras(cats_txt, globales, problemas)
    for p in problemas:
        log(f"⚠️ palabras.txt: {p}")
    sin_telegram = not os.environ.get("TELEGRAM_TOKEN") or not os.environ.get("TELEGRAM_CHAT_ID")
    if sin_telegram and not args.diagnostico and os.environ.get("GITHUB_ACTIONS"):
        log("⏸️ Falta configurar TELEGRAM_TOKEN y TELEGRAM_CHAT_ID en Settings → Secrets and variables → "
            "Actions. El radar no hace nada hasta entonces.")
        return 0
    if args.prueba:
        ok = telegram("🧪 <b>Prueba del radar de chollos</b>\nSi lees esto, el bot puede escribir en este grupo.",
                      botones=[[{"text": "📈 Así se verán los botones", "url": "https://keepa.com"}]])
        log("Mensaje de prueba enviado." if ok else "No se pudo enviar el mensaje de prueba.")
        if ok:
            ejemplo_amazon(cfg, compilar_categorias(cats_txt) + [dict(CATEGORIA_JUEGOS_GRATIS)])
        return 0 if ok else 1
    global MOSTRAR_BOTON_FUENTE
    MOSTRAR_BOTON_FUENTE = bool(cfg.get("boton_fuente", False))
    cats = compilar_categorias(cats_txt)
    if (cfg.get("juegos") or {}).get("avisar_gratis", True):
        cats.append(dict(CATEGORIA_JUEGOS_GRATIS))
    return ronda(cfg, cats, compilar_lista(globales), diagnostico=args.diagnostico)


if __name__ == "__main__":
    sys.exit(main())
