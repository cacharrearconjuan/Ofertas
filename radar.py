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
RE_OPCION = re.compile(r"^(grados|precio\s*m[aá]x(?:imo)?|precio\s*m[ií]n(?:imo)?|tema|aviso)\s*:\s*(.+)$", re.I)


def _partir(linea: str) -> list[str]:
    if linea.lstrip("+-").startswith("re:"):
        return [linea]
    return [p.strip() for p in linea.split(",") if p.strip()]


def leer_palabras(ruta: Path = RUTA_PALABRAS) -> tuple[list[dict], list[str], list[str]]:
    """Devuelve (categorías, exclusiones globales, avisos de formato)."""
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
            emoji, nombre = "🔹", cab
            partes = cab.split(maxsplit=1)
            if len(partes) == 2 and not partes[0][0].isalnum():
                emoji, nombre = partes
            ids = {c["id"] for c in cats}
            cid = slug(nombre)
            while cid in ids:
                cid += "_2"
            actual = {"id": cid, "nombre": nombre, "emoji": emoji, "activo": True,
                      "incluir": [], "excluir": [], "forzar": [], "avisos": [],
                      "grados": None, "precio_max": None, "precio_min": None, "tema": None}
            cats.append(actual)
            continue
        if actual is None:
            problemas.append(f"línea {n}: «{linea}» está fuera de una [categoría]; se ignora")
            continue
        if actual == "GLOBAL":
            globales += [p.lstrip("-") for p in _partir(linea)]
            continue
        if normalizar(linea) in ("apagada", "apagado", "desactivada"):
            actual["activo"] = False
            continue
        op = RE_OPCION.match(linea)
        if op:
            clave, valor = normalizar(op.group(1)).replace(" ", ""), op.group(2).strip()
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
        "caducada": bool(d.get("isExpired") or d.get("expired")),
        "tipo": str(tipo).lower() if tipo else None,
        "es_cupon": bool(d.get("isVoucher") or d.get("voucherCode")) and precio is None,
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
    return encontrados


RE_GRADOS_VISIBLES = re.compile(r'class=["\'][^"\']*(?:vote-temp|cept-vote-temp)[^"\']*["\'][^>]*>\s*(-?\d+)\s*°')
RE_VISITA = re.compile(r'(https?://(?:www\.)?chollometro\.com/visit/[^"\'\s<>]+)')


def datos_de_pagina(url: str, hid: str) -> dict | None:
    """Abre la página de la oferta y devuelve sus datos (grados, caducada, enlace a tienda...)."""
    texto = descargar(url).decode("utf-8", "replace")
    hilos = hilos_en_html(texto)
    d = hilos.get(hid)
    if d is None:
        m = re.search(rf'"threadId"\s*:\s*"?{re.escape(hid)}"?[^{{}}]*?"temperature"\s*:\s*(-?\d+(?:\.\d+)?)', texto)
        if not m:
            m = RE_GRADOS_VISIBLES.search(texto)
        d = {"id": hid, "grados": float(m.group(1))} if m else None
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
    v = keepa_veredicto(asin, s.get("precio"), cfg.get("keepa") or {})
    if v:
        s["keepa_veredicto"] = v


# --------------------------------------------------------------------------
# Clasificación
# --------------------------------------------------------------------------
def no_es_oferta(o: dict) -> bool:
    enlace = (o.get("enlace") or "").lower()
    if "/cupones/" in enlace or "/discusiones/" in enlace or "/codigos-descuento" in enlace:
        return True
    tipo = (o.get("tipo") or "").lower()
    if tipo and any(t in tipo for t in ("voucher", "discussion", "cupon", "discusion")):
        return True
    return bool(o.get("es_cupon"))


def clasificar(o: dict, cats, cfg, excluir_global) -> list[str]:
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
        forzada = bool(c["re_forzar"] and c["re_forzar"].search(texto))
        if not forzada:
            if not c["re_incluir"] or not c["re_incluir"].search(texto):
                continue
            if c["re_excluir"] and c["re_excluir"].search(texto):
                continue
        if precio is not None and c["precio_max"] is not None and precio > c["precio_max"]:
            continue
        if precio is not None and c["precio_min"] is not None and precio < c["precio_min"]:
            continue
        elegidas.append(c["id"])
    return elegidas


def umbral(o: dict, cats_por_id: dict, minimo_global: float) -> float:
    """El mínimo de grados de una oferta es el más bajo de sus categorías."""
    valores = [cats_por_id[c]["grados"] if cats_por_id[c]["grados"] is not None else minimo_global
               for c in o.get("categorias", []) if c in cats_por_id]
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


def _telegram_llamar(metodo: str, datos: dict) -> tuple[bool, str]:
    token = os.environ.get("TELEGRAM_TOKEN")
    base = os.environ.get("TELEGRAM_API", "https://api.telegram.org")
    url = f"{base}/bot{token}/{metodo}"
    for _ in range(3):
        req = urllib.request.Request(url, data=json.dumps(datos).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return bool(json.load(r).get("ok")), ""
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
    partes.append(f'--{limite}\r\nContent-Disposition: form-data; name="photo"; filename="keepa.png"\r\n'
                  f"Content-Type: image/png\r\n\r\n".encode() + imagen + b"\r\n")
    partes.append(f"--{limite}--\r\n".encode())
    req = urllib.request.Request(f"{base}/bot{token}/sendPhoto", data=b"".join(partes),
                                 headers={"Content-Type": f"multipart/form-data; boundary={limite}"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return bool(json.load(r).get("ok")), ""
    except urllib.error.HTTPError as ex:
        return False, f"{ex.code}: {ex.read().decode('utf-8', 'replace')[:200]}"
    except (urllib.error.URLError, TimeoutError) as ex:
        return False, str(ex)


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


def mensaje(o: dict, cats_por_id: dict) -> str:
    etiquetas = " · ".join(f"{cats_por_id[c]['emoji']} {cats_por_id[c]['nombre']}"
                           for c in o.get("categorias", []) if c in cats_por_id)
    lineas = [f"🔥 <b>{int(o['grados'])}°</b>  {e(etiquetas)}", f"<b>{e(o['titulo'])}</b>"]
    precio = ""
    if o.get("precio") is not None:
        precio = f"💶 <b>{formatear_precio(o['precio'])}</b>"
        if o.get("precio_antes"):
            pct = round((1 - o["precio"] / o["precio_antes"]) * 100)
            precio += f" <s>{formatear_precio(o['precio_antes'])}</s> (−{pct}%)"
    detalle = " · ".join(x for x in [precio, f"🏪 {e(o.get('tienda'))}" if o.get("tienda") else ""] if x)
    if detalle:
        lineas.append(detalle)
    if o.get("codigo"):
        lineas.append(f"🏷️ Código: <code>{e(o['codigo'])}</code>")
    if o.get("keepa_veredicto"):
        lineas.append(e(o["keepa_veredicto"]["texto"]))
    lineas += [e(a) for a in o.get("advertencias", [])]
    return "\n".join(lineas)


def botones(o: dict) -> list:
    fila = []
    if (o.get("enlace") or "").startswith("http"):
        fila.append({"text": "🔥 Chollometro", "url": o["enlace"]})
    if (o.get("destino") or "").startswith("http"):
        fila.append({"text": f"🛒 {o.get('tienda') or 'Tienda'}"[:30], "url": o["destino"]})
    filas = [fila] if fila else []
    if o.get("keepa"):
        filas.append([{"text": "📈 Historial de precios (Keepa)", "url": o["keepa"]}])
    return filas


def tema_de(o: dict, cats_por_id: dict) -> int | None:
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
    tmp.write_text(json.dumps(datos, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(ruta)


CAMPOS_WEB = ("id", "titulo", "enlace", "destino", "tienda", "precio", "precio_antes", "codigo",
              "grados", "categorias", "imagen", "fecha", "detectado", "avisado", "advertencias",
              "asin", "keepa", "keepa_veredicto")


def para_web(o: dict) -> dict:
    return {k: o.get(k) for k in CAMPOS_WEB if o.get(k) not in (None, "", [])}


# --------------------------------------------------------------------------
# Ronda principal
# --------------------------------------------------------------------------
CAMPOS_PAGINA = ("grados", "tienda", "caducada", "tipo", "es_cupon", "destino", "codigo", "precio_antes")


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
                    for k in ("precio", "imagen"):
                        if o.get(k) in (None, "") and h.get(k) not in (None, ""):
                            o[k] = h[k]
                elif h.get("enlace") and h.get("titulo"):
                    ofertas[hid] = {**h, "cats_rss": [], "descripcion": "", "fecha": None, "guid": ""}
        except Exception as ex:  # noqa: BLE001
            fallos += 1
            log(f"Listado {url}: ERROR {ex}")
    for o in ofertas.values():
        if o.get("precio") is None:
            o["precio"] = precio_en_texto(o.get("titulo", ""))
        if not o.get("tienda"):
            o["tienda"] = adivinar_tienda(o.get("titulo"), quitar_html(o.get("descripcion"))[:300])
    return ofertas, fuentes, fallos, fallos_rss


def ronda(cfg, cats, excluir_global, diagnostico=False) -> int:
    ahora = time.time()
    cats_por_id = {c["id"]: c for c in cats}
    minimo = float(cfg.get("grados_minimos", 200))
    cfg_keepa = cfg.get("keepa") or {}

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
        if cfg.get("solo_ofertas", True) and no_es_oferta(o):
            continue
        max_h = cfg.get("max_antiguedad_horas")
        if max_h and o.get("fecha") and ahora - o["fecha"] > max_h * 3600:
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
                  "fecha", "caducada", "destino"):
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
                         and (s.get("grados") or 0) < umbral(s, cats_por_id, minimo)),
                        key=lambda s: s.get("comprobado", 0))
    limite = 3 if diagnostico else cfg.get("max_consultas_por_ronda", 40)
    leidas = intentadas = 0
    for s in pendientes[:limite]:
        intentadas += 1
        try:
            d = datos_de_pagina(s["enlace"], s["id"])
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
            if no_es_oferta({**s, **d}):
                s["descartar"] = True
        if diagnostico:
            log(f"  {s['id']} → {d}")
        time.sleep(cfg.get("pausa_entre_consultas_seg", 1.0))
    if intentadas:
        log(f"Grados leídos: {leidas}/{intentadas}")

    # 3) Elegir las que ya pasan el mínimo
    listas = sorted((s for s in seguimiento.values()
                     if (s.get("grados") or 0) >= umbral(s, cats_por_id, minimo)
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
                    log(f"  ASIN: {buscar_asin(prueba)} · destino final: {str(prueba.get('destino'))[:120]}")
            except Exception as ex:  # noqa: BLE001
                log(f"  error: {ex}")
        log(f"Pasarían el mínimo: {len(listas)}")
        for s in listas[:3]:
            completar_destino(s)
            enriquecer_amazon(s, cfg)
            log("\n" + mensaje({**s, "advertencias": advertencias(s, cats)}, cats_por_id))
            log(f"  botones: {botones(s)}")
        return 0

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
            s["advertencias"] = advertencias(s, cats)
            completar_destino(s)
            enriquecer_amazon(s, cfg)
            veredicto = (s.get("keepa_veredicto") or {}).get("nivel")
            if cfg_keepa.get("descartar_falsas") and veredicto == "rojo":
                log(f"  {s['id']}: Keepa dice que no es rebaja; no se avisa")
                avisados[s["id"]] = ahora
                seguimiento.pop(s["id"], None)
                continue
            fotos = [s.get("imagen")]
            if s.get("asin") and cfg_keepa.get("grafica", True):
                grafica = keepa_grafica_bytes(s["asin"])  # gráfica de Keepa y, si no hay, la foto
                if grafica:
                    fotos.insert(0, grafica)
            if telegram(mensaje(s, cats_por_id), silencioso, foto=fotos, botones=botones(s),
                        tema=tema_de(s, cats_por_id)) or sin_telegram:
                enviados += 1
                s["avisado"] = ahora
                avisados[s["id"]] = ahora
                destacadas[s["id"]] = para_web(s)
                seguimiento.pop(s["id"], None)
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
        for a in c["avisos"]:
            log(f"   aviso si {', '.join(a['si'])} → {a['texto']}")
    if globales:
        log(f"\n⛔ Excluir siempre: {', '.join(globales)}")
    compilar_categorias(cats_txt)
    for p in problemas:
        log(f"⚠️ {p}")
    return 1 if problemas else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Radar de chollos de Chollometro → Telegram")
    ap.add_argument("--prueba", action="store_true", help="manda un mensaje de prueba a Telegram")
    ap.add_argument("--diagnostico", action="store_true", help="muestra lo que lee, sin avisar ni guardar")
    ap.add_argument("--palabras", action="store_true", help="comprueba palabras.txt")
    args = ap.parse_args()

    cfg = json.loads(RUTA_CONFIG.read_text(encoding="utf-8"))
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
        return 0 if ok else 1
    return ronda(cfg, compilar_categorias(cats_txt), compilar_lista(globales), diagnostico=args.diagnostico)


if __name__ == "__main__":
    sys.exit(main())
