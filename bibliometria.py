#!/usr/bin/env python3
"""Red de citas de artículos de filosofía reunidos de varias bases bibliográficas.

Sin clave: OpenAlex, Crossref, las revistas de Oxford y Cambridge University Press (a través
de Crossref) y PhilPapers (su repositorio, indexado en OpenAlex).
Con clave: Scopus, Springer Nature y Google Académico (a través de SerpApi).
Sólo se conservan artículos de filosofía: los que OpenAlex clasifica en temas
filosóficos, más los de PhilPapers y los de la materia Philosophy de Springer.

Además del ranking de artículos, analiza los autores (autores.csv), busca grupos temáticos con
varios métodos de clustering para grafos dirigidos (clusters.csv, clusters_resumen.csv) y
ordena los libros de OUP y CUP que cita el corpus (libros_oup_cup.csv). Las referencias de
Crossref sin DOI se enlazan por título y año. Centralidad, autores y clusters se calculan en tres
versiones: sólo artículos, sólo libros (cocitación y citas entre libros) y libros y artículos
(archivos con sufijo _libros y _mixto). grafo.html reúne las vistas en pestañas.

Python 3.10+ | python -m pip install numpy scipy networkx
Opcional:  python -m pip install infomap igraph leidenalg   (activa Infomap y Leiden)
Ejemplo:   python bibliometria.py "emergence" --subarea mente,metafisica --nodos 60
Subáreas:  python bibliometria.py --listar-subareas
Ayuda:     python bibliometria.py --help
"""
from __future__ import annotations

import argparse
import csv
import difflib
import errno
import gzip
import html
import http.client
import json
import math
import os
import re
import sys
import tempfile
import time
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode
from urllib.request import Request, urlopen

try:
    import networkx as nx
    import numpy as np
    from scipy import sparse
    from scipy.sparse import csgraph
    from scipy.sparse import linalg as sla
except ImportError as error:
    sys.exit(f"Falta una dependencia ({error}). Instala con: python -m pip install numpy scipy networkx")


API = "https://api.openalex.org/works"
API_CROSSREF = "https://api.crossref.org/works"
API_SCOPUS = "https://api.elsevier.com/content/search/scopus"
API_SPRINGER = "https://api.springernature.com/meta/v2/json"
API_SERPAPI = "https://serpapi.com/search.json"
# Repositorio «PhilPapers (PhilPapers Foundation)» en OpenAlex: los depósitos de PhilArchive.
# PhilPapers no ofrece búsqueda pública por API (su API JSON está tras un control
# antibots y su OAI-PMH sólo permite cosechar por fechas), así que se consulta por aquí.
REPOSITORIO_PHILPAPERS = "S4306402130"
AGENTE = "BibliometriaFilosofia/2.0"
SELECT = ("id,display_name,publication_year,doi,type,authorships,primary_location,"
          "cited_by_count,referenced_works,topics,primary_topic")
CAMPO = "search.title_and_abstract"
ID = re.compile(r"(?:https?://(?:www\.)?openalex\.org/)?(W\d+)/?", re.I)
TEMA = re.compile(r"(?:https?://(?:www\.)?openalex\.org/)?(T\d+)/?", re.I)
AUTOR = re.compile(r"(?:https?://(?:www\.)?openalex\.org/)?(A\d+)/?", re.I)
DOI_VALIDO = re.compile(r"10\.\d{4,9}/\S+")
# Rutas de editoriales que incluyen el DOI del propio artículo.
DOI_EN_URL = re.compile(r"(?:doi\.org/|[?&]doi=|/doi/(?:abs/|full/|pdf/|epdf/|epub/)?|"
                        r"link\.springer\.com/(?:article/|chapter/|content/pdf/))(10\.\d{4,9}/[^?#&\s]+)", re.I)
PHILPAPERS_ID = re.compile(r"phil(?:papers|archive)\.org/(?:rec|archive)/([A-Za-z0-9-]+)", re.I)
CONTROL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
EPS_RADIO = 1e-9
NODOS_VISTA = 40
LOTE_OPENALEX = 50
LOTE_CROSSREF = 40
TOPE_EXTERNO = 2000  # máximo por fuente sin filtro temático propio cuando --limite es 0
MAX_OR = 100  # OpenAlex admite hasta 100 valores unidos con "|" en un filtro
SEMILLA = 7  # agrupamientos y disposiciones reproducibles
NODOS_AUTORES = 40
ALFA_PAGERANK = 0.85
VECINOS_SIMILITUD = 15  # vecinos más parecidos que conserva cada artículo en las redes de similitud
MAX_K_ESPECTRAL = 15
CAJAS_CLUSTERS = 8  # clusters que se dibujan en el mapa: uno por color de la paleta categórica
ARTICULOS_POR_CAJA = 15

FUENTES = ("openalex", "crossref", "oupcup", "philpapers", "scopus", "springer", "google")
NOMBRE_FUENTE = {"openalex": "OpenAlex", "crossref": "Crossref", "oupcup": "OUP y CUP (Crossref)",
                 "philpapers": "PhilPapers", "scopus": "Scopus", "springer": "Springer Nature",
                 "google": "Google Académico"}
ALIAS_FUENTE = {"openalex": "openalex", "crossref": "crossref", "philpapers": "philpapers",
                "philarchive": "philpapers", "scopus": "scopus", "elsevier": "scopus",
                "springer": "springer", "springer nature": "springer", "google": "google",
                "google academico": "google", "google scholar": "google", "scholar": "google",
                "serpapi": "google", "oupcup": "oupcup", "oup cup": "oupcup", "oup y cup": "oupcup",
                "oup": "oupcup", "cup": "oupcup", "oxford": "oupcup", "cambridge": "oupcup"}
# Oxford University Press y Cambridge University Press como miembros de Crossref.
MIEMBROS_OUP_CUP = ("member:286", "member:56")
LIBROS_VISTA = 50  # libros que muestra la pestaña «Libros OUP/CUP»
LIBROS_CON_METADATOS = 1000  # tope de libros que se consultan en Crossref y entran como nodos
LOTE_ISBN = 5  # libros por consulta de referencias (cada uno trae todos sus capítulos)
EDITORIALES = {"OUP": "Oxford University Press", "CUP": "Cambridge University Press"}
# Versiones de la red: sólo artículos (la principal), sólo libros, y libros y artículos juntos.
VERSIONES = (("articulos", "Artículos"), ("libros", "Libros"), ("mixta", "Libros y artículos"))
UNIDADES = {"articulos": ("artículo", "artículos"), "libros": ("libro", "libros"), "mixta": ("obra", "obras")}
SUFIJOS = {"articulos": "", "libros": "_libros", "mixta": "_mixto"}
VARIABLES_CLAVE = {"scopus": ("SCOPUS_API_KEY", "ELSEVIER_API_KEY"), "springer": ("SPRINGER_API_KEY",),
                   "google": ("SERPAPI_API_KEY", "SERPAPI_KEY")}
# Si dos fuentes describen la misma obra, se conservan los metadatos de la de menor valor.
PRIORIDAD = {"openalex": 0, "crossref": 1, "scopus": 2, "springer": 3, "google": 4}
CITAS = (("openalex", "citas_openalex"), ("crossref", "citas_crossref"), ("scopus", "citas_scopus"),
         ("google", "citas_google_academico"))

# Temas de OpenAlex (dominio > campo > subcampo > tema) con contenido filosófico: todos los
# del subcampo Philosophy (1211) y los de otros subcampos que tratan ramas de la filosofía.
TEMAS_FILOSOFIA = {
    "T11463": "Classical Philosophy and Thought",
    "T11592": "Phenomenology and Existential Philosophy",
    "T10297": "Philosophical Ethics and Theory",
    "T13150": "Study and Philosophy of Religion",
    "T10258": "Philosophy and Theoretical Science",
    "T12517": "Seventeenth-Century Political and Philosophical Thought",
    "T12857": "Medieval Philosophy and Theology",
    "T11477": "Epistemology, Ethics, and Metaphysics",
    "T13931": "Philosophy and Historical Thought",
    "T14278": "Marxism and Critical Theory",
    "T12440": "Karl Barth and Christian Theology",
    "T13153": "Rousseau and Enlightenment Thought",
    "T14059": "Nietzsche, Schopenhauer, and Hegel",
    "T14024": "Education, Philosophy, and Society",
    "T13712": "Kantian Philosophy and Modern Interpretations",
    "T14092": "Ethics, Aesthetics, and Art",
    "T14198": "Philosophical Thought and Analysis",
    "T13887": "Augustinian Studies and Theology",
    "T12366": "Philosophy, Ethics, and Existentialism",
    "T12308": "Mental Health and Psychiatry",
    "T12511": "Medieval and Classical Philosophy",
    "T13462": "Spanish Philosophy and Literature",
    "T13545": "Philosophical and Theoretical Analysis",
    "T12133": "Pragmatism in Philosophy and Education",
    "T12957": "War, Ethics, and Justification",
    "T13497": "Hermeneutics and Narrative Identity",
    "T11953": "Neuroethics, Human Enhancement, Biomedical Innovations",
    "T12558": "Wittgensteinian philosophy and applications",
    "T13144": "Theology and Philosophy of Evil",
    "T11997": "Free Will and Agency",
    "T12160": "African cultural and philosophical studies",
    "T12726": "Philosophical and Cultural Analysis",
    "T14314": "Violence, Religion, and Philosophy",
    "T13087": "Simone de Beauvoir and Sartre",
    "T13902": "Indian History and Philosophy",
    "T13661": "Reformed Theology and Governance",
    "T14148": "Kierkegaardian Philosophy and Influence",
    "T13264": "Walter Benjamin Studies Compilation",
    "T13908": "Scientific and Historical Analyses",
    "T13351": "Critical Theory and Political Philosophy",
    "T13940": "Philosophy and Literary Analysis",
    "T14371": "David Hume Studies",
    "T13933": "Whitehead's Philosophy and Applications",
    "T14460": "Literary, Cultural, Historical Analysis",
    "T14401": "Technology, Environment, Urban Planning",
    "T13558": "Logic and Philosophy of Science (subcampo 1207)",
    "T10778": "Philosophy of Biology and Scientific Explanation (1207)",
    "T11137": "Historical Philosophy and Science (1207)",
    "T12170": "History and Theory of Mathematics (1207)",
    "T14511": "Medicine, History, and Philosophy (1207)",
    "T13308": "Cybernetics and Technology in Society (1207)",
    "T14307": "Bioethics and Human Rights Issues (1207)",
    "T12777": "Philosophy, History, and Historiography (1202)",
    "T11010": "Logic, Reasoning, and Knowledge (1702)",
    "T10883": "Ethics and Social Impacts of AI (1702)",
    "T12819": "Environmental Philosophy and Ethics (2309)",
    "T13147": "Ethics and bioethics in healthcare (2739)",
    "T12889": "Psychoanalysis, Philosophy, and Politics (3203)",
    "T14300": "Legal and Social Philosophy (3312)",
    "T12295": "Foucault, Power, and Ethics (3312)",
    "T12397": "Critical Theory and Philosophy (3312)",
    "T12320": "Feminist Epistemology and Gender Studies (3312)",
    "T13660": "Philosophical and Historical Studies (3312)",
    "T14080": "Philosophy, Sociology, Political Theory (3312)",
    "T13119": "Philosophy and Social Theory (3312)",
    "T13610": "Society, Economy, and Ethics Research (3312)",
    "T13783": "Feminism, Gender, and Intersectionality (3312)",
    "T14123": "Decolonial Thought and Epistemologies (3314)",
    "T10738": "Posthumanist Ethics and Activism (3316)",
    "T10718": "Political Philosophy and Ethics (3320)",
    "T12723": "Hannah Arendt's Political Philosophy (3320)",
}

# Subárea: (nombre, alias separados por "|", temas de OpenAlex). Un tema puede estar en varias.
SUBAREAS = {
    "etica": ("Ética normativa y metaética", "ethics|moral|filosofia moral|moral philosophy|metaetica|metaethics",
              ("T10297", "T13783")),
    "etica-aplicada": ("Ética aplicada y bioética",
                       "bioetica|bioethics|applied ethics|neuroetica|etica de la ia|etica ambiental",
                       ("T13147", "T11953", "T10883", "T12819", "T12957", "T14307", "T13610")),
    "epistemologia": ("Epistemología", "epistemology|teoria del conocimiento|gnoseologia", ("T11477", "T12320")),
    "metafisica": ("Metafísica, tiempo y filosofía del proceso", "metaphysics|ontologia|ontology",
                   ("T11477", "T11997", "T13545", "T13933")),
    "mente": ("Filosofía de la mente y de la psiquiatría",
              "filosofia de la mente|philosophy of mind|conciencia|consciousness", ("T10258", "T11997", "T12308")),
    "lenguaje": ("Filosofía del lenguaje y hermenéutica",
                 "filosofia del lenguaje|philosophy of language|hermeneutica|hermeneutics", ("T12558", "T13497")),
    "logica": ("Lógica y filosofía de la matemática y la probabilidad",
               "logic|filosofia de la matematica|philosophy of mathematics", ("T11010", "T13558", "T12170", "T14460")),
    "ciencia": ("Filosofía e historia de la ciencia y de la medicina",
                "filosofia de la ciencia|philosophy of science|historia de la ciencia|epistemologia de la ciencia",
                ("T13558", "T10778", "T11137", "T12170", "T14511", "T13308")),
    "tecnologia": ("Filosofía de la tecnología y de la inteligencia artificial",
                   "filosofia de la tecnologia|philosophy of technology|inteligencia artificial|posthumanismo",
                   ("T13308", "T10883", "T11953", "T10738")),
    "politica": ("Filosofía política, social y del derecho",
                 "filosofia politica|political philosophy|filosofia social|filosofia del derecho|"
                 "philosophy of law|justicia",
                 ("T10718", "T12723", "T14300", "T13119", "T13660", "T14080", "T14278", "T13351", "T12397",
                  "T12295", "T12957")),
    "feminista": ("Filosofía feminista", "feminismo|feminist philosophy|genero", ("T12320", "T13783", "T13087")),
    "estetica": ("Estética y filosofía del arte y la literatura", "aesthetics|filosofia del arte|philosophy of art|arte",
                 ("T14092", "T13264", "T13940")),
    "religion": ("Filosofía de la religión y teología",
                 "filosofia de la religion|philosophy of religion|teologia|theology",
                 ("T13150", "T13144", "T12440", "T13887", "T14314", "T13661", "T14148")),
    "educacion": ("Filosofía de la educación", "filosofia de la educacion|philosophy of education",
                  ("T14024", "T12133")),
    "historia": ("Filosofía de la historia", "filosofia de la historia|philosophy of history|historiografia",
                 ("T12777", "T13931")),
    "antigua": ("Filosofía antigua", "ancient philosophy|filosofia griega|clasica", ("T11463",)),
    "medieval": ("Filosofía medieval", "medieval philosophy|escolastica|scholasticism", ("T12857", "T12511", "T13887")),
    "moderna": ("Filosofía moderna (siglos XVII a XIX)", "modern philosophy|ilustracion|enlightenment|idealismo aleman",
                ("T12517", "T13153", "T13712", "T14371", "T14059", "T13931")),
    "continental": ("Filosofía continental, fenomenología y teoría crítica",
                    "continental philosophy|fenomenologia|phenomenology|existencialismo|teoria critica|critical theory",
                    ("T11592", "T12366", "T13087", "T14148", "T13497", "T13545", "T12295", "T12397", "T14278",
                     "T13351", "T10738", "T12889", "T13264", "T14059")),
    "pragmatismo": ("Pragmatismo", "pragmatism", ("T12133",)),
    "no-occidental": ("Filosofías africana, india, islámica, judía y decolonial",
                      "no occidental|non western|africana|india|islamica|decolonial",
                      ("T12160", "T13902", "T12511", "T14123")),
    "hispanica": ("Filosofía española e hispanoamericana",
                  "filosofia espanola|filosofia latinoamericana|latinoamericana|hispanoamericana", ("T13462", "T14123")),
    "general": ("Metafilosofía y temas generales", "metafilosofia|metaphilosophy",
                ("T14198", "T12726", "T13908", "T14401")),
}


def texto(valor):
    return " ".join(CONTROL_XML.sub("", str(valor or "")).split())


def normalizar(valor):
    sin_tildes = unicodedata.normalize("NFKD", str(valor or "")).encode("ascii", "ignore").decode().casefold()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", sin_tildes).split())


def id_obra(valor):
    m = ID.fullmatch(str(valor or "").strip())
    return m.group(1).upper() if m else ""


def id_tema(valor):
    m = TEMA.fullmatch(str(valor or "").strip())
    return m.group(1).upper() if m else ""


def id_autor(valor):
    m = AUTOR.fullmatch(str(valor or "").strip())
    return m.group(1).upper() if m else ""


def normalizar_doi(valor):
    doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", texto(valor).lower())
    return doi if DOI_VALIDO.fullmatch(doi) else ""


def doi_de_url(url):
    m = DOI_EN_URL.search(unquote(str(url or "")))
    if not m:
        return ""
    return normalizar_doi(re.sub(r"(?:\.pdf|/full|/abstract|/epdf|/html)$", "", m.group(1), flags=re.I))


def entero(valor):
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None


def primero(valor):
    if isinstance(valor, list):
        return valor[0] if valor else ""
    return valor or ""


def dentro(anio, desde, hasta):
    return anio is None or ((not desde or anio >= desde) and (not hasta or anio <= hasta))


def variable(*nombres):
    return next((v for n in nombres if (v := os.environ.get(n, "").strip())), "")


ALIAS_SUBAREA = {normalizar(a): clave for clave, (nombre, alias, _) in SUBAREAS.items()
                 for a in (clave, nombre, *alias.split("|"))}


def resolver_subareas(valores):
    """Devuelve claves de subárea o identificadores de tema, sin duplicados."""
    elegidas = []
    for bruto in valores or []:
        for parte in (x.strip() for x in bruto.split(",")):
            if not parte:
                continue
            if tema := id_tema(parte):
                if tema not in TEMAS_FILOSOFIA:
                    raise ValueError(f"El tema {tema} no está entre los temas de filosofía. Usa --listar-subareas.")
                clave = tema
            elif not (clave := ALIAS_SUBAREA.get(normalizar(parte))):
                parecidas = difflib.get_close_matches(normalizar(parte), ALIAS_SUBAREA, n=3, cutoff=.6)
                pista = f" ¿Quisiste decir: {', '.join(parecidas)}?" if parecidas else ""
                raise ValueError(f"Subárea desconocida: «{parte}».{pista} Usa --listar-subareas.")
            if clave not in elegidas:
                elegidas.append(clave)
    return elegidas


def temas_de(elegidas):
    if not elegidas:
        return set(TEMAS_FILOSOFIA)
    return {t for k in elegidas for t in ((k,) if k in TEMAS_FILOSOFIA else SUBAREAS[k][2])}


def nombre_subarea(clave):
    return f"{clave} {TEMAS_FILOSOFIA[clave]}" if clave in TEMAS_FILOSOFIA else SUBAREAS[clave][0]


def subareas_de(temas):
    return [k for k, (_, _, propios) in SUBAREAS.items() if set(propios) & set(temas)]


def listar_subareas():
    print("Sin --subarea se busca en toda la filosofía (los temas de OpenAlex de abajo).\n")
    for clave, (nombre, alias, temas) in SUBAREAS.items():
        print(f"{clave}: {nombre}\n    alias: {alias.replace('|', ', ')}")
        for t in temas:
            print(f"    {t}  {TEMAS_FILOSOFIA[t]}")
    print("\nTambién se admite un tema concreto, p. ej. --subarea T11477. Los temas sin número de "
          "subcampo pertenecen al subcampo Philosophy (1211).")


def resolver_fuentes(valor):
    """Devuelve (fuentes activas, {fuente omitida: motivo})."""
    explicitas = normalizar(valor) not in ("todas", "all")
    pedidas = []
    for parte in (FUENTES if not explicitas else valor.split(",")):
        if not normalizar(parte):
            continue
        fuente = ALIAS_FUENTE.get(normalizar(parte))
        if not fuente:
            raise ValueError(f"Fuente desconocida: «{parte.strip()}». Usa: {', '.join(FUENTES)} o todas.")
        if fuente not in pedidas:
            pedidas.append(fuente)
    activas, omitidas = [], {}
    for fuente in pedidas:
        if fuente in VARIABLES_CLAVE and not variable(*VARIABLES_CLAVE[fuente]):
            motivo = f"falta la variable de entorno {VARIABLES_CLAVE[fuente][0]}"
            if explicitas:
                raise ValueError(f"{NOMBRE_FUENTE[fuente]}: {motivo}.")
            omitidas[fuente] = motivo
        else:
            activas.append(fuente)
    if not activas:
        raise ValueError("No queda ninguna fuente disponible.")
    return activas, omitidas


class ErrorAcceso(RuntimeError):
    """La fuente rechazó la clave o el derecho de acceso."""


class NoEncontrado(RuntimeError):
    """La fuente respondió HTTP 404."""


class PresupuestoAgotado(RuntimeError):
    """OpenAlex agotó el presupuesto diario de la clave o de la IP."""


def espera_http(valor, intento):
    if valor:
        try:
            return max(0, float(valor))
        except ValueError:
            try:
                return max(0, (parsedate_to_datetime(valor) - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    return 2 ** intento


def pedir_json(url, parametros=None, cabeceras=None, servicio="OpenAlex"):
    cab = {"User-Agent": AGENTE, "Accept": "application/json", "Accept-Encoding": "gzip", **(cabeceras or {})}
    req = Request(url + ("?" + urlencode(parametros) if parametros else ""), headers=cab)
    for intento in range(5):
        try:
            with urlopen(req, timeout=60) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                    data = gzip.decompress(data)
            return json.loads(data)
        except HTTPError as exc:
            with exc:
                detalle = texto(exc.read(1500).decode("utf-8", "replace"))
                demora = espera_http(exc.headers.get("Retry-After"), intento)
            if exc.code == 404:
                raise NoEncontrado(f"{servicio}: recurso no encontrado.") from exc
            if exc.code in (401, 403):
                raise ErrorAcceso(f"{servicio} denegó el acceso (HTTP {exc.code}): {detalle[:300]}") from exc
            if "daily_budget" in detalle or "Insufficient budget" in detalle:
                raise PresupuestoAgotado(f"{servicio}: se agotó el presupuesto diario de OpenAlex. Define "
                                         "OPENALEX_API_KEY (gratuita: https://openalex.org/settings/api) "
                                         "o espera a que se renueve a medianoche UTC.") from exc
            reintentable = exc.code in (408, 429, 500, 502, 503, 504)
            if not reintentable or intento == 4 or demora > 60:
                raise RuntimeError(f"{servicio} HTTP {exc.code}: {detalle[:300]}") from exc
            causa = f"HTTP {exc.code}"
        except (URLError, TimeoutError, ConnectionError, http.client.IncompleteRead, EOFError,
                gzip.BadGzipFile, json.JSONDecodeError, UnicodeDecodeError) as exc:
            if intento == 4:
                raise RuntimeError(f"No se pudo leer la respuesta de {servicio}: {exc}") from exc
            demora, causa = 2 ** intento, type(exc).__name__
        print(f"{servicio}: reintento por {causa} en {demora:g} s.", file=sys.stderr)
        time.sleep(demora)
    raise AssertionError("Secuencia de reintentos agotada")


def nuevo_registro(fuente, origen, **campos):
    """fuente: base donde se encontró (None si sólo completa datos); origen: quién da los metadatos."""
    # autores_ids: identificadores de OpenAlex alineados con los nombres de «autores» (vacía en otras fuentes).
    registro = {"ids": {}, "titulo": "", "anio": None, "autores": "", "autores_ids": [], "revista": "", "url": "",
                "tipo": None, "temas": None, "tema_principal": None, "nombres_temas": {}, "citas": {}, "refs": set(),
                "fuentes": {fuente} if fuente else set(), "cargado": {origen},
                "prioridad": PRIORIDAD[origen], "filosofia_por_fuente": False}
    registro.update(campos)
    return registro


def dois(r):
    return {d for d in (r["ids"].get("doi"), r["ids"].get("doi_alt")) if d}


def claves_registro(r):
    # Un artículo puede tener dos DOI (p. ej., Project MUSE y la editorial): ambos lo identifican.
    for tipo, valor in r["ids"].items():
        yield ("doi" if tipo == "doi_alt" else tipo), valor
    titulo = normalizar(r["titulo"])
    # Sólo títulos largos con año: evita fundir reseñas o editoriales homónimas.
    if len(titulo) >= 25 and r["anio"]:
        yield "titulo", f"{titulo}|{r['anio']}"


def compatibles(a, b):
    da, db = dois(a), dois(b)
    wa, wb = a["ids"].get("openalex"), b["ids"].get("openalex")
    return not (da and db and not da & db) and not (wa and wb and wa != wb)


def fusionar(destino, origen):
    mejor = origen["prioridad"] < destino["prioridad"]
    for campo in ("titulo", "anio", "autores", "revista", "url", "tipo"):
        valor = origen.get(campo)
        if valor not in (None, "") and (mejor or destino.get(campo) in (None, "")):
            destino[campo] = valor
            if campo == "autores":
                # Los identificadores viajan con la lista de nombres de la que proceden.
                destino["autores_ids"] = list(origen.get("autores_ids") or [])
    destino.setdefault("nombres_temas", {}).update(origen.get("nombres_temas") or {})
    for tipo, valor in origen["ids"].items():
        if tipo not in ("doi", "doi_alt"):
            destino["ids"].setdefault(tipo, valor)
    # El DOI de la fuente prioritaria queda como principal; un segundo se guarda como alternativo.
    orden = (origen, destino) if mejor else (destino, origen)
    unicos = list(dict.fromkeys(d for r in orden for d in (r["ids"].get("doi"), r["ids"].get("doi_alt")) if d))
    if unicos:
        destino["ids"]["doi"] = unicos[0]
    if len(unicos) > 1:
        destino["ids"]["doi_alt"] = unicos[1]
    if origen["temas"] is not None and (destino["temas"] is None or mejor):
        destino["temas"], destino["tema_principal"] = origen["temas"], origen["tema_principal"]
    destino["citas"].update({k: v for k, v in origen["citas"].items() if v is not None})
    destino["refs"] |= origen["refs"]
    destino["fuentes"] |= origen["fuentes"]
    destino["cargado"] |= origen["cargado"]
    destino["filosofia_por_fuente"] |= origen["filosofia_por_fuente"]
    destino["prioridad"] = min(destino["prioridad"], origen["prioridad"])


class Corpus:
    """Registros únicos: une los de distintas fuentes por DOI, ID o título y año."""

    def __init__(self):
        self.registros, self.indice = [], {}

    def agregar(self, r):
        candidatos = []
        for clave in claves_registro(r):
            c = self.indice.get(clave)
            if c is not None and all(c is not x for x in candidatos) and compatibles(c, r):
                candidatos.append(c)
        if not candidatos:
            self.registros.append(r)
            destino = r
        else:
            destino = candidatos[0]
            fusionar(destino, r)
            for otro in candidatos[1:]:
                if compatibles(destino, otro):
                    fusionar(destino, otro)
                    self.registros = [x for x in self.registros if x is not otro]
                    for clave in claves_registro(otro):
                        self.indice[clave] = destino
        for clave in claves_registro(destino):
            self.indice[clave] = destino
        return destino

    def reindexar(self, r):
        """Tras añadir un DOI a r, lo une con el registro que ya tuviera ese DOI."""
        self.registros = [x for x in self.registros if x is not r]
        self.indice = {k: v for k, v in self.indice.items() if v is not r}
        return self.agregar(r)


def cabeceras_openalex(clave):
    return {"Authorization": "Bearer " + clave} if clave else {}


def validar_lista_openalex(datos):
    if not isinstance(datos, dict) or not isinstance(datos.get("results"), list) or not isinstance(datos.get("meta"), dict):
        raise RuntimeError("OpenAlex devolvió una respuesta sin results/meta válidos.")
    return datos


def registro_openalex(obra, fuente=None, filosofia=False):
    w = id_obra(obra.get("id"))
    if not w:
        raise RuntimeError("OpenAlex devolvió un registro sin identificador de obra válido.")
    if "referenced_works" not in obra:
        raise RuntimeError("Falta el campo referenced_works en la respuesta de OpenAlex.")
    lugar = obra.get("primary_location") or {}
    autorias = [(texto(autor.get("display_name")), id_autor(autor.get("id")))
                for a in (obra.get("authorships") or []) for autor in [(a or {}).get("author") or {}]]
    autorias = [(nombre, ident) for nombre, ident in autorias if nombre]
    temas = [x or {} for x in (obra.get("topics") or [])]
    principal = obra.get("primary_topic") or {}
    r = nuevo_registro(fuente, "openalex", titulo=texto(obra.get("display_name")), anio=obra.get("publication_year"),
                       autores="; ".join(nombre for nombre, _ in autorias),
                       autores_ids=[ident for _, ident in autorias],
                       revista=texto((lugar.get("source") or {}).get("display_name")),
                       url=texto(lugar.get("landing_page_url")), tipo=obra.get("type"),
                       temas=[t for x in temas if (t := id_tema(x.get("id")))],
                       tema_principal=id_tema(principal.get("id")) or None,
                       nombres_temas={t: nombre for x in (*temas, principal)
                                      if (t := id_tema(x.get("id"))) and (nombre := texto(x.get("display_name")))},
                       filosofia_por_fuente=filosofia)
    r["ids"]["openalex"] = w
    if doi := normalizar_doi(obra.get("doi")):
        r["ids"]["doi"] = doi
    for lugar_alt in obra.get("locations") or []:
        if m := PHILPAPERS_ID.search(f'{lugar_alt.get("landing_page_url") or ""} {lugar_alt.get("pdf_url") or ""}'):
            r["ids"]["philpapers"] = m.group(1)
            break
    r["refs"] = {("openalex", x) for ref in (obra["referenced_works"] or []) if (x := id_obra(ref))}
    r["citas"]["openalex"] = obra.get("cited_by_count")
    return r


def buscar_openalex(tema, limite, desde, hasta, clave, filtros_extra, fuente, select=SELECT, filosofia=False):
    filtros = ["type:article", *filtros_extra]
    if desde:
        filtros.append(f"from_publication_date:{desde:04d}-01-01")
    if hasta:
        filtros.append(f"to_publication_date:{hasta:04d}-12-31")
    por_pagina = min(100, limite or 100)
    base = {CAMPO: tema, "filter": ",".join(filtros), "select": select, "per_page": por_pagina}
    nombre = NOMBRE_FUENTE[fuente]
    registros, vistos, cursores_usados = [], set(), set()
    numero, cursor, filas_leidas, total = 1, "*", 0, None
    while True:
        if not clave and numero * por_pagina > 10_000:
            raise RuntimeError(f"{nombre}: para paginar más de 10 000 resultados define OPENALEX_API_KEY "
                               "o acota la búsqueda con --desde, --hasta o --limite.")
        arg_pagina = {"cursor": cursor} if clave else {"page": numero}
        respuesta = validar_lista_openalex(pedir_json(API, {**base, **arg_pagina}, cabeceras_openalex(clave), nombre))
        meta, filas = respuesta["meta"], respuesta["results"]
        if numero == 1:
            total = meta.get("count")
        filas_leidas += len(filas)
        for obra in filas:
            r = registro_openalex(obra, fuente, filosofia)
            if r["ids"]["openalex"] in vistos:
                continue
            vistos.add(r["ids"]["openalex"])
            registros.append(r)
            if limite and len(registros) >= limite:
                break
        print(f"{nombre}: página {numero}, {len(registros)} artículos (coincidencias: {total}).",
              file=sys.stderr, flush=True)
        if not filas or (limite and len(registros) >= limite):
            break
        if clave:
            siguiente = meta.get("next_cursor")
            if not siguiente:
                break
            if siguiente == cursor or siguiente in cursores_usados:
                raise RuntimeError(f"{nombre}: OpenAlex repitió el cursor de paginación.")
            cursores_usados.add(cursor)
            cursor = siguiente
        elif len(filas) < por_pagina or (isinstance(total, int) and filas_leidas >= total):
            break
        numero += 1
    consulta = {k: v for k, v in base.items() if k != "select"}
    return registros, {"consulta": consulta, "coincidencias": total, "paginas": numero}


def parametros_crossref(correo):
    # Crossref atiende mejor a quien se identifica; el correo sólo se envía si el usuario lo define.
    return {"mailto": correo} if correo else {}


def registro_crossref(it, fuente=None):
    doi = normalizar_doi(it.get("DOI"))
    if not doi:
        return None
    fecha = primero((it.get("issued") or {}).get("date-parts")) or [None]
    autores = []
    for a in it.get("author") or []:
        if nombre := texto(" ".join(x for x in (a.get("given"), a.get("family")) if x)) or texto(a.get("name")):
            autores.append(nombre)
    tipo = it.get("type")
    r = nuevo_registro(fuente, "crossref", titulo=texto(primero(it.get("title"))),
                       anio=fecha[0] if isinstance(fecha[0], int) else None, autores="; ".join(autores),
                       revista=texto(primero(it.get("container-title"))), url=texto(it.get("URL")),
                       tipo="article" if tipo == "journal-article" else tipo)
    r["ids"]["doi"] = doi
    r["refs"] = referencias_crossref(it.get("reference"))
    r["citas"]["crossref"] = entero(it.get("is-referenced-by-count"))
    return r


def desescapar(valor):
    """Texto de Crossref sin etiquetas ni entidades HTML; a veces vienen escapadas dos veces (&amp;#39;)."""
    return texto(re.sub(r"<[^>]+>", " ", html.unescape(html.unescape(texto(valor)))))


def cita_sin_doi(ref):
    """Referencia estructurada sin DOI: ("cita", tipo, autor, título, año), con el texto normalizado.

    tipo es "a" si cita un artículo o capítulo (article-title) y "l" si sólo nombra un libro (volume-title).
    """
    articulo = desescapar(ref.get("article-title"))
    titulo = normalizar(articulo or desescapar(ref.get("volume-title")))
    if len(titulo) < 15:  # «Introduction», «Reply»: demasiado ambiguos
        return None
    # La palabra más larga del autor es el apellido tanto en «Bobzien, S.» como en «S. Bobzien».
    palabras = normalizar(desescapar(ref.get("author"))).split()
    anio = re.search(r"\d{4}", str(ref.get("year") or ""))
    return ("cita", "a" if articulo else "l", max(palabras, key=len) if palabras else "", titulo,
            anio.group(0) if anio else "")


def referencias_crossref(lista):
    """Referencias de Crossref: ("doi", d) si traen DOI y, si no, la cita estructurada. Se ignoran las que
    sólo tienen texto libre (unstructured)."""
    refs = set()
    for ref in lista or []:
        if not isinstance(ref, dict):
            continue
        if doi := normalizar_doi(ref.get("DOI")):
            refs.add(("doi", doi))
        elif cita := cita_sin_doi(ref):
            refs.add(cita)
    return refs


def lista_crossref(datos):
    mensaje = datos.get("message") if isinstance(datos, dict) else None
    if not isinstance(mensaje, dict) or not isinstance(mensaje.get("items"), list):
        raise RuntimeError("Crossref devolvió una respuesta sin items válidos.")
    return mensaje


def buscar_crossref(tema, limite, desde, hasta, correo, filtros_extra=(), fuente="crossref"):
    """Búsqueda de artículos en Crossref; filtros_extra limita, p. ej., a ciertas editoriales (member:…)."""
    nombre = NOMBRE_FUENTE[fuente]
    filtros = ["type:journal-article", *filtros_extra]
    if desde:
        filtros.append(f"from-pub-date:{desde:04d}-01-01")
    if hasta:
        filtros.append(f"until-pub-date:{hasta:04d}-12-31")
    maximo = limite or TOPE_EXTERNO
    base = {"query": tema, "filter": ",".join(filtros), "rows": min(100, maximo),
            "select": "DOI,title,author,issued,container-title,is-referenced-by-count,reference,type,URL"}
    registros, cursor, numero, total = [], "*", 1, None
    while True:
        mensaje = lista_crossref(pedir_json(API_CROSSREF, {**base, "cursor": cursor, **parametros_crossref(correo)},
                                            servicio="Crossref"))
        if numero == 1:
            total = mensaje.get("total-results")
        for it in mensaje["items"]:
            if r := registro_crossref(it, fuente):
                registros.append(r)
            if len(registros) >= maximo:
                break
        print(f"{nombre}: página {numero}, {len(registros)} registros (coincidencias: {total}).",
              file=sys.stderr, flush=True)
        siguiente = mensaje.get("next-cursor")
        if not mensaje["items"] or len(registros) >= maximo or not siguiente or siguiente == cursor:
            break
        cursor, numero = siguiente, numero + 1
    consulta = {k: v for k, v in base.items() if k != "select"}
    return registros, {"consulta": consulta, "coincidencias": total, "paginas": numero}


def registro_scopus(e):
    sid = texto(e.get("dc:identifier")).removeprefix("SCOPUS_ID:")
    url = next((texto(x.get("@href")) for x in e.get("link") or [] if isinstance(x, dict) and x.get("@ref") == "scopus"), "")
    r = nuevo_registro("scopus", "scopus", titulo=texto(e.get("dc:title")),
                       anio=entero(texto(e.get("prism:coverDate"))[:4]), autores=texto(e.get("dc:creator")),
                       revista=texto(e.get("prism:publicationName")), url=url, tipo="article")
    if sid.isdigit():
        r["ids"]["scopus"] = sid
    if doi := normalizar_doi(e.get("prism:doi")):
        r["ids"]["doi"] = doi
    r["citas"]["scopus"] = entero(e.get("citedby-count"))
    return r


def buscar_scopus(tema, limite, desde, hasta, clave, token):
    # Scopus sólo filtra por áreas amplias: ARTS es Artes y Humanidades. La filosofía se
    # verifica después con OpenAlex. Los paréntesis explícitos evitan depender de la precedencia.
    partes = [f"TITLE-ABS-KEY({tema})", "SUBJAREA(ARTS)", "DOCTYPE(ar)"]
    if desde:
        partes.append(f"PUBYEAR > {desde - 1}")
    if hasta:
        partes.append(f"PUBYEAR < {hasta + 1}")
    consulta = " AND ".join(f"({x})" for x in partes)
    cabeceras = {"X-ELS-APIKey": clave, **({"X-ELS-Insttoken": token} if token else {})}
    maximo = min(limite or TOPE_EXTERNO, 5000)  # sin cursor, Scopus entrega hasta 5000 resultados
    registros, inicio, total = [], 0, None
    while True:
        datos = pedir_json(API_SCOPUS, {"query": consulta, "start": inicio, "count": 25}, cabeceras, "Scopus")
        resultados = datos.get("search-results") if isinstance(datos, dict) else None
        if not isinstance(resultados, dict):
            raise RuntimeError("Scopus devolvió una respuesta sin search-results.")
        if total is None:
            total = entero(resultados.get("opensearch:totalResults")) or 0
        entradas = [e for e in resultados.get("entry") or [] if isinstance(e, dict) and "error" not in e]
        for e in entradas:
            registros.append(registro_scopus(e))
            if len(registros) >= maximo:
                break
        inicio += 25
        print(f"Scopus: {len(registros)} registros (coincidencias: {total}).", file=sys.stderr, flush=True)
        if not entradas or len(registros) >= maximo or inicio >= min(total, 5000):
            break
    return registros, {"consulta": consulta, "coincidencias": total}


def registro_springer(rec):
    doi = normalizar_doi(rec.get("doi")) or normalizar_doi(texto(rec.get("identifier")).removeprefix("doi:"))
    tipo = texto(rec.get("contentType")).lower()
    if not doi or (tipo and tipo != "article"):
        return None
    autores = [texto(c.get("creator") if isinstance(c, dict) else c) for c in rec.get("creators") or []]
    url = next((texto(u.get("value")) for u in rec.get("url") or [] if isinstance(u, dict) and u.get("value")), "")
    fecha = next((texto(rec.get(k)) for k in ("coverDate", "printDate", "publicationDate", "onlineDate") if rec.get(k)), "")
    r = nuevo_registro("springer", "springer", titulo=texto(rec.get("title")), anio=entero(fecha[:4]),
                       autores="; ".join(a for a in autores if a), revista=texto(rec.get("publicationName")),
                       url=url, tipo="article", filosofia_por_fuente=True)
    r["ids"]["doi"] = doi
    return r


def buscar_springer(tema, limite, desde, hasta, clave):
    consulta = f'{tema} subject:"Philosophy"'
    maximo = limite or TOPE_EXTERNO
    registros, inicio, total, examinados = [], 1, None, 0
    while True:
        datos = pedir_json(API_SPRINGER, {"q": consulta, "s": inicio, "p": 25, "api_key": clave},
                           servicio="Springer Nature")
        lista = datos.get("records") if isinstance(datos, dict) else None
        if not isinstance(lista, list):
            raise RuntimeError("Springer Nature devolvió una respuesta sin records.")
        if total is None:
            resumen = primero(datos.get("result"))
            total = entero(resumen.get("total")) if isinstance(resumen, dict) else None
        for rec in lista:
            examinados += 1
            r = registro_springer(rec) if isinstance(rec, dict) else None
            if r and dentro(r["anio"], desde, hasta):  # el año se filtra aquí: la API no lo hace por rango
                registros.append(r)
                if len(registros) >= maximo:
                    break
        inicio += len(lista)
        print(f"Springer Nature: {len(registros)} artículos de {examinados} registros (coincidencias: {total}).",
              file=sys.stderr, flush=True)
        if (not lista or len(registros) >= maximo or (total is not None and inicio > total)
                or examinados >= 5 * maximo):
            break
    return registros, {"consulta": consulta, "coincidencias": total, "examinados": examinados}


def registro_google(item):
    if not isinstance(item, dict) or not item.get("title"):
        return None
    if str(item.get("type", "")).lower() in ("book", "citation"):
        return None
    resumen = texto((item.get("publication_info") or {}).get("summary"))
    # Formato habitual: "A Autor, B Autor - Revista, 2019 - editorial.com"
    partes = [p.strip() for p in re.split(r"\s+[-‐–]\s+", resumen)] if resumen else []
    anio, revista = None, ""
    if len(partes) >= 2:
        m = re.search(r"(?:^|,\s*)((?:1[5-9]|20)\d\d)\s*$", partes[1])
        revista = partes[1][:m.start()].strip(" ,") if m else partes[1]
        anio = int(m.group(1)) if m else None
    if anio is None and (m := re.search(r"\b(?:1[5-9]|20)\d\d\b", resumen)):
        anio = int(m.group(0))
    enlaces = [item.get("link")] + [x.get("link") for x in item.get("resources") or [] if isinstance(x, dict)]
    r = nuevo_registro("google", "google", titulo=texto(item["title"]), anio=anio,
                       autores=partes[0].replace(", ", "; ") if partes else "", revista=revista,
                       url=texto(item.get("link")))
    if item.get("result_id"):
        r["ids"]["google"] = texto(item["result_id"])
    if doi := next((d for e in enlaces if (d := doi_de_url(e))), ""):
        r["ids"]["doi"] = doi
    r["citas"]["google"] = entero(((item.get("inline_links") or {}).get("cited_by") or {}).get("total"))
    return r


def buscar_google(tema, limite, desde, hasta, clave):
    # Google Académico no tiene API oficial; SerpApi la ofrece como servicio de pago.
    parametros = {"engine": "google_scholar", "q": tema, "num": 20, "start": 0, "api_key": clave}
    if desde:
        parametros["as_ylo"] = desde
    if hasta:
        parametros["as_yhi"] = hasta
    registros, busquedas, total = [], 0, None
    while len(registros) < limite:
        datos = pedir_json(API_SERPAPI, parametros, servicio="Google Académico (SerpApi)")
        busquedas += 1
        if not isinstance(datos, dict):
            raise RuntimeError("SerpApi devolvió una respuesta no válida.")
        if datos.get("error"):
            if "hasn't returned any results" in str(datos["error"]):
                break
            raise RuntimeError(f"SerpApi: {texto(datos['error'])}")
        if total is None:
            total = (datos.get("search_information") or {}).get("total_results")
        resultados = datos.get("organic_results") or []
        for item in resultados:
            if r := registro_google(item):
                registros.append(r)
            if len(registros) >= limite:
                break
        print(f"Google Académico: {len(registros)} resultados (coincidencias: {total}).", file=sys.stderr, flush=True)
        if len(resultados) < parametros["num"] or not (datos.get("serpapi_pagination") or {}).get("next"):
            break
        parametros["start"] += len(resultados)
    return registros, {"consulta": tema, "coincidencias": total, "busquedas_serpapi": busquedas}


def titulos_equivalentes(a, b):
    a, b = normalizar(a), normalizar(b)
    if not a or not b:
        return False
    if difflib.SequenceMatcher(None, a, b).ratio() >= .9:
        return True
    corto, largo = sorted((a, b), key=len)
    return len(corto) >= 30 and largo.startswith(corto)  # Google recorta títulos largos con «…»


def resolver_doi_google(corpus, correo):
    """Busca en Crossref el DOI de los resultados de Google Académico que no lo traen."""
    pendientes = [r for r in corpus.registros if "google" in r["fuentes"] and not r["ids"].get("doi")
                  and not r["ids"].get("openalex")]
    resueltos = 0
    for r in pendientes:
        primer_apellido = (r["autores"].split(";")[0].split() or [""])[-1]
        consulta = " ".join(str(x) for x in (r["titulo"], primer_apellido, r["anio"]) if x)
        mensaje = lista_crossref(pedir_json(API_CROSSREF, {"query.bibliographic": consulta, "rows": 3,
                                                           "select": "DOI,title,issued", **parametros_crossref(correo)},
                                            servicio="Crossref"))
        for it in mensaje["items"]:
            fecha = primero((it.get("issued") or {}).get("date-parts")) or [None]
            anio = fecha[0] if isinstance(fecha[0], int) else None
            doi = normalizar_doi(it.get("DOI"))
            if (doi and titulos_equivalentes(r["titulo"], primero(it.get("title")))
                    and (not r["anio"] or not anio or abs(anio - r["anio"]) <= 1)):
                r["ids"]["doi"] = doi
                corpus.reindexar(r)
                resueltos += 1
                break
    if pendientes:
        print(f"Google Académico: DOI encontrado para {resueltos} de {len(pendientes)} resultados sin DOI.",
              file=sys.stderr, flush=True)
    return {"sin_doi": len(pendientes), "resueltos": resueltos}


def completar_openalex(corpus, clave):
    """Trae de OpenAlex, por DOI, temas, tipo y referencias de los registros de otras fuentes."""
    pendientes = [r for r in corpus.registros if r["ids"].get("doi") and "openalex" not in r["cargado"]]
    resumen = {"doi_consultados": len(pendientes), "encontrados": 0, "consultas_por_lote": 0,
               "consultas_individuales": 0}
    cabeceras = cabeceras_openalex(clave)
    # Las comas y las barras romperían el filtro por lotes: esos DOI van uno a uno.
    individuales = [r for r in pendientes if set(r["ids"]["doi"]) & set("|,")]
    agrupables = [r for r in pendientes if not set(r["ids"]["doi"]) & set("|,")]
    for i in range(0, len(agrupables), LOTE_OPENALEX):
        lote = {r["ids"]["doi"]: r for r in agrupables[i:i + LOTE_OPENALEX]}
        try:
            datos = validar_lista_openalex(pedir_json(API, {"filter": "doi:" + "|".join(lote), "select": SELECT,
                                                            "per_page": 100}, cabeceras, "OpenAlex"))
        except PresupuestoAgotado:
            # Consultar una obra por su DOI no consume presupuesto: se sigue uno a uno.
            print("OpenAlex: presupuesto agotado; los DOI restantes se consultan uno a uno "
                  "(gratis, pero más lento).", file=sys.stderr, flush=True)
            individuales.extend(agrupables[i:])
            break
        resumen["consultas_por_lote"] += 1
        hallados = set()
        for obra in datos["results"]:
            nuevo = registro_openalex(obra)
            if (doi := nuevo["ids"].get("doi")) in lote:
                corpus.agregar(nuevo)
                hallados.add(doi)
                lote[doi]["cargado"].add("openalex")
        resumen["encontrados"] += len(hallados)
        # El filtro sólo reconoce el DOI principal de cada obra; la consulta individual
        # también resuelve los DOI secundarios.
        individuales.extend(r for doi, r in lote.items() if doi not in hallados)
        print(f"OpenAlex: {min(i + LOTE_OPENALEX, len(agrupables))}/{len(agrupables)} DOI completados por lotes.",
              file=sys.stderr, flush=True)
    for n, r in enumerate(individuales, 1):
        try:
            obra = pedir_json(f"{API}/doi:{quote(r['ids']['doi'], safe='/')}", {"select": SELECT}, cabeceras, "OpenAlex")
        except NoEncontrado:
            obra = None
        resumen["consultas_individuales"] += 1
        if isinstance(obra, dict) and obra.get("id"):
            nuevo = registro_openalex(obra)
            pedido = r["ids"]["doi"]  # OpenAlex lo resolvió a esta obra: es un DOI suyo
            if nuevo["ids"].get("doi", pedido) != pedido:
                nuevo["ids"]["doi_alt"] = pedido
            else:
                nuevo["ids"]["doi"] = pedido
            corpus.agregar(nuevo)
            resumen["encontrados"] += 1
        r["cargado"].add("openalex")
        if n % 25 == 0 or n == len(individuales):
            print(f"OpenAlex: {n}/{len(individuales)} DOI consultados uno a uno.", file=sys.stderr, flush=True)
    return resumen


def completar_crossref(registros, correo):
    """Añade las referencias que Crossref conoce para los artículos conservados, con DOI o sin él."""
    pendientes = [r for r in registros if r["ids"].get("doi") and "crossref" not in r["cargado"]
                  and "," not in r["ids"]["doi"]]
    consultas = 0
    for i in range(0, len(pendientes), LOTE_CROSSREF):
        lote = {r["ids"]["doi"]: r for r in pendientes[i:i + LOTE_CROSSREF]}
        mensaje = lista_crossref(pedir_json(API_CROSSREF, {"filter": ",".join("doi:" + d for d in lote),
                                                           "rows": len(lote), **parametros_crossref(correo),
                                                           "select": "DOI,reference,is-referenced-by-count"},
                                            servicio="Crossref"))
        consultas += 1
        for it in mensaje["items"]:
            if r := lote.get(normalizar_doi(it.get("DOI"))):
                r["refs"] |= referencias_crossref(it.get("reference"))
                if (citas := entero(it.get("is-referenced-by-count"))) is not None:
                    r["citas"]["crossref"] = citas
        for r in lote.values():
            r["cargado"].add("crossref")
        print(f"Crossref: referencias de {min(i + LOTE_CROSSREF, len(pendientes))}/{len(pendientes)} artículos.",
              file=sys.stderr, flush=True)
    return {"doi_consultados": len(pendientes), "consultas": consultas}


# Libros y capítulos de OUP (Oxford Scholarship Online, Oxford Handbooks…) y de CUP (Cambridge Core,
# Cambridge Companions). Un capítulo lleva el DOI del libro con un sufijo propio.
LIBRO_OUP = re.compile(r"10\.1093/((?:acprof:oso|acprof:osobl|oso|oxfordhb)/)?(97[89]\d{10}|\d{9}[\dx])\.\d{3}\.\d{4}")
LIBRO_CUP = re.compile(r"10\.1017/(cbo|ccol)?(97[89]\d{10}|\d{9}[\dx])(?:\.\d+)?")


def doi_libro(doi):
    """(editorial, DOI del libro) si el DOI es de un libro o capítulo de OUP o CUP; None si no lo es."""
    if m := LIBRO_OUP.fullmatch(doi):
        return "OUP", f"10.1093/{m.group(1) or ''}{m.group(2)}.001.0001"
    if m := LIBRO_CUP.fullmatch(doi):
        return "CUP", f"10.1017/{m.group(1) or ''}{m.group(2)}"
    return None


def contar_libros(registros):
    """Libros de OUP y CUP citados con DOI: artículos del corpus que los citan y capítulos citados."""
    libros = {}
    for r in registros:
        for ref in r["refs"]:
            if ref[0] == "doi" and (libro := doi_libro(ref[1])):
                editorial, doi = libro
                datos = libros.setdefault(doi, {"editorial": editorial, "articulos": set(), "capitulos": set()})
                datos["articulos"].add(clave_nodo(r))
                if ref[1] != doi:
                    datos["capitulos"].add(ref[1])
    return libros


def completar_libros(dois, correo):
    """Título, autoría, año, tipo y citas de libros, por lotes de DOI en Crossref."""
    metadatos = {}
    for i in range(0, len(dois), LOTE_CROSSREF):
        lote = set(dois[i:i + LOTE_CROSSREF])
        mensaje = lista_crossref(pedir_json(API_CROSSREF, {"filter": ",".join("doi:" + d for d in sorted(lote)),
                                                           "rows": len(lote), **parametros_crossref(correo),
                                                           "select": "DOI,title,subtitle,author,editor,issued,"
                                                                     "type,is-referenced-by-count"},
                                            servicio="Crossref"))
        for it in mensaje["items"]:
            if (doi := normalizar_doi(it.get("DOI"))) not in lote:
                continue
            titulo, subtitulo = desescapar(primero(it.get("title"))), desescapar(primero(it.get("subtitle")))
            if subtitulo and subtitulo.casefold() not in titulo.casefold():
                titulo = f"{titulo}: {subtitulo}"
            personas = it.get("author") or it.get("editor") or []
            nombres = [texto(" ".join(x for x in (p.get("given"), p.get("family")) if x)) or texto(p.get("name"))
                       for p in personas if isinstance(p, dict)]
            fecha = primero((it.get("issued") or {}).get("date-parts")) or [None]
            metadatos[doi] = {"titulo": titulo, "autores": "; ".join(n for n in nombres if n),
                              "editores": not it.get("author") and bool(it.get("editor")),
                              "anio": fecha[0] if isinstance(fecha[0], int) else None, "tipo": texto(it.get("type")),
                              "citas_crossref": entero(it.get("is-referenced-by-count"))}
        print(f"Crossref: datos de {min(i + LOTE_CROSSREF, len(dois))}/{len(dois)} libros de OUP y CUP.",
              file=sys.stderr, flush=True)
    return metadatos


def analizar_libros(registros, correo):
    """Ranking de los libros de OUP y CUP que cita el corpus.

    Sólo cuentan las citas con DOI que Crossref conoce: las referencias que OpenAlex enlaza por su
    identificador no dicen qué editorial publicó la obra citada.
    """
    libros = contar_libros(registros)
    orden = sorted(libros, key=lambda d: (-len(libros[d]["articulos"]), d))
    # Los libros más citados, hasta el tope, son nodos de las versiones con libros: necesitan
    # título, autoría y sus propias referencias.
    pedir = orden[:LIBROS_CON_METADATOS]
    resumen = {"libros": len(libros), "citas": sum(len(e["articulos"]) for e in libros.values()),
               "articulos_que_citan": len(set().union(*(e["articulos"] for e in libros.values()))),
               "consultados": len(pedir)}
    try:
        metadatos = completar_libros(pedir, correo)
    except RuntimeError as exc:
        metadatos = {}
        resumen["error"] = str(exc)
        print(f"Aviso: no se obtuvieron los títulos de los libros de OUP y CUP. {exc}", file=sys.stderr, flush=True)
    resumen["con_metadatos"] = len(metadatos)
    try:
        propias = referencias_libros(pedir, correo)
    except RuntimeError as exc:
        propias = {}
        resumen["error_referencias"] = str(exc)
        print(f"Aviso: no se obtuvieron las referencias de los libros de OUP y CUP; la red de libros sólo usará "
              f"la cocitación. {exc}", file=sys.stderr, flush=True)
    registros_libros = [registro_libro(d, libros[d], metadatos.get(d, {}), propias.get(d, ())) for d in pedir]
    resumen["con_referencias"] = sum(1 for r in registros_libros if r["refs"])
    filas = []
    for doi, e in libros.items():
        m = metadatos.get(doi, {})
        filas.append({"doi": doi, "editorial": e["editorial"], "titulo": m.get("titulo", ""),
                      "autores": m.get("autores", ""), "editores": m.get("editores", False), "anio": m.get("anio"),
                      "tipo": m.get("tipo", ""), "citas_corpus": len(e["articulos"]),
                      "capitulos_citados": len(e["capitulos"]), "citas_crossref": m.get("citas_crossref"),
                      "articulos": sorted(e["articulos"])})
    filas.sort(key=lambda f: (-f["citas_corpus"], -(f["citas_crossref"] or 0), not f["titulo"],
                              f["titulo"].casefold(), f["doi"]))
    for i, f in enumerate(filas, 1):
        f["puesto"] = i
    return {"filas": filas, "resumen": resumen, "registros": registros_libros}


def isbn13(isbn):
    """ISBN-13 sin guiones a partir de un ISBN-10 o 13."""
    isbn = isbn.lower()
    if len(isbn) == 13:
        return isbn
    base = "978" + isbn[:9]
    suma = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(base))
    return base + str((10 - suma % 10) % 10)


def isbn_de_libro(doi):
    """ISBN-13 que figura en el DOI de un libro de OUP o CUP, o "" si no lo tiene."""
    m = LIBRO_OUP.fullmatch(doi) or LIBRO_CUP.fullmatch(doi)
    return isbn13(m.group(2)) if m else ""


def referencias_libros(dois, correo):
    """Referencias que los libros, o sus capítulos, depositan en Crossref: {DOI del libro: referencias}.

    El filtro isbn: trae el libro y todos sus capítulos. OUP repite la bibliografía del libro en cada
    capítulo; el conjunto de referencias quita esas repeticiones.
    """
    pedidos, por_isbn = set(dois), {}
    for d in dois:
        if isbn := isbn_de_libro(d):
            por_isbn.setdefault(isbn, d)
    isbns, refs = sorted(por_isbn), defaultdict(set)
    for i in range(0, len(isbns), LOTE_ISBN):
        cursor = "*"
        while True:
            mensaje = lista_crossref(pedir_json(API_CROSSREF, {"filter": ",".join("isbn:" + x for x in isbns[i:i + LOTE_ISBN]),
                                                               "rows": 1000, "cursor": cursor, "select": "DOI,reference",
                                                               **parametros_crossref(correo)}, servicio="Crossref"))
            for it in mensaje["items"]:
                libro = doi_libro(normalizar_doi(it.get("DOI")))
                if libro and libro[1] in pedidos:
                    # Las citas a otros capítulos del mismo libro no son referencias del libro.
                    refs[libro[1]] |= {ref for ref in referencias_crossref(it.get("reference"))
                                       if not (ref[0] == "doi" and (otro := doi_libro(ref[1])) and otro[1] == libro[1])}
            siguiente = mensaje.get("next-cursor")
            if len(mensaje["items"]) < 1000 or not siguiente or siguiente == cursor:
                break
            cursor = siguiente
        print(f"Crossref: referencias de {min(i + LOTE_ISBN, len(isbns))}/{len(isbns)} libros de OUP y CUP.",
              file=sys.stderr, flush=True)
    return dict(refs)


def registro_libro(doi, datos, metadatos, refs):
    """Un libro de OUP o CUP como obra de las versiones con libros."""
    r = nuevo_registro(None, "crossref", titulo=metadatos.get("titulo", ""), anio=metadatos.get("anio"),
                       autores=metadatos.get("autores", ""), url="https://doi.org/" + doi, tipo="libro")
    r["ids"]["doi"] = doi
    r["refs"] = set(refs)
    r["citas"]["crossref"] = metadatos.get("citas_crossref")
    r["es_libro"], r["editorial"] = True, EDITORIALES[datos["editorial"]]
    r["citas_corpus"] = len(datos["articulos"])
    return r


def clasificar(corpus, temas_objetivo, con_subareas, criterio, desde, hasta):
    """Conserva los artículos de filosofía (o de las subáreas pedidas) y cuenta los descartes."""
    conservados, descartes = [], Counter()
    for r in corpus.registros:
        if not r["fuentes"]:
            continue
        if r["tipo"] not in (None, "article"):
            descartes["no_es_articulo"] += 1
            continue
        if not dentro(r["anio"], desde, hasta):
            descartes["fuera_del_periodo"] += 1
            continue
        temas = r["temas"]
        if temas is not None and criterio == "principal":
            temas = [r["tema_principal"]] if r["tema_principal"] else []
        r["subareas"] = subareas_de(temas or [])
        if temas and temas_objetivo & set(temas):
            conservados.append(r)
        elif r["filosofia_por_fuente"] and not con_subareas:
            conservados.append(r)
        elif temas is None:
            descartes["sin_clasificacion_openalex"] += 1
        elif set(TEMAS_FILOSOFIA) & set(temas) or r["filosofia_por_fuente"]:
            descartes["fuera_de_las_subareas"] += 1
        else:
            descartes["fuera_de_filosofia"] += 1
    return conservados, descartes


def clave_nodo(r):
    if r["ids"].get("doi"):
        return r["ids"]["doi"]
    for tipo in ("openalex", "scopus", "philpapers", "google"):
        if r["ids"].get(tipo):
            return f"{tipo}:{r['ids'][tipo]}"
    return "titulo:" + normalizar(r["titulo"])[:80]


def url_registro(r):
    ids = r["ids"]
    if ids.get("doi"):
        return "https://doi.org/" + ids["doi"]
    if r["url"]:
        return r["url"]
    if ids.get("openalex"):
        return "https://openalex.org/" + ids["openalex"]
    return "https://philpapers.org/rec/" + ids["philpapers"] if ids.get("philpapers") else ""


def atributos_nodo(r):
    datos = {"titulo": r["titulo"] or clave_nodo(r), "anio": entero(r["anio"]), "autores": r["autores"],
             "revista": r["revista"], "doi": r["ids"].get("doi"), "doi_alternativo": r["ids"].get("doi_alt"),
             "url": url_registro(r),
             "fuentes": "; ".join(NOMBRE_FUENTE[f] for f in FUENTES if f in r["fuentes"]),
             "subareas": "; ".join(r.get("subareas") or []), "id_openalex": r["ids"].get("openalex"),
             "id_scopus": r["ids"].get("scopus"), "id_philpapers": r["ids"].get("philpapers"),
             "referencias_enlazadas": len(r["refs"]), "tipo_obra": "libro" if r.get("es_libro") else "articulo",
             "editorial": r.get("editorial"), "citas_corpus": r.get("citas_corpus")}
    datos["label"] = datos["titulo"]
    for fuente, columna in CITAS:
        datos[columna] = entero(r["citas"].get(fuente))
    return {k: v for k, v in datos.items() if v not in (None, "")}


def indice_claves(registros):
    """Identificadores (DOI, OpenAlex, Scopus...) de cada artículo -> clave de su nodo."""
    indice = {}
    for r in registros:
        k = clave_nodo(r)
        for clave in claves_registro(r):
            if clave[0] != "titulo":
                indice.setdefault(clave, k)
    return indice


def indice_titulos(registros):
    """Título normalizado -> [(año, nodo)], para enlazar las citas sin DOI. Como en claves_registro,
    sólo cuentan los títulos de al menos 25 caracteres: los cortos («Reply», «Freedom») se repiten."""
    titulos = defaultdict(list)
    for r in registros:
        # Los libros se enlazan sólo por DOI, igual que en su ranking.
        if not r.get("es_libro") and len(titulo := normalizar(r["titulo"])) >= 25:
            titulos[titulo].append((r["anio"], clave_nodo(r)))
    return titulos


def destino_referencia(ref, indice, titulos):
    """Nodo del corpus al que apunta una referencia, o None.

    Una cita sin DOI se enlaza si cita un artículo cuyo título coincide, el año difiere a lo sumo en uno
    (la versión en línea y la impresa suelen diferir) y el candidato es único.
    """
    if ref[0] != "cita":
        if (nodo := indice.get(ref)) or ref[0] != "doi":
            return nodo
        # La cita a un capítulo apunta al libro, si el libro es un nodo (versiones con libros).
        libro = doi_libro(ref[1])
        return indice.get(("doi", libro[1])) if libro else None
    _, tipo, _, titulo, anio = ref
    if tipo != "a":
        return None
    candidatos = {nodo for a, nodo in titulos.get(titulo, ())
                  if not anio or a is None or abs(int(a) - int(anio)) <= 1}
    return next(iter(candidatos)) if len(candidatos) == 1 else None


def grafo_citas(registros):
    g = nx.DiGraph()
    indice, titulos = indice_claves(registros), indice_titulos(registros)
    for r in registros:
        g.add_node(clave_nodo(r), **atributos_nodo(r))
    nuevas = 0
    for r in registros:
        citante = clave_nodo(r)
        # Las citas sin DOI van al final: así se cuentan sólo las aristas que nadie más aportó.
        for ref in sorted(r["refs"], key=lambda x: (x[0] == "cita", x)):
            citado = destino_referencia(ref, indice, titulos)
            if not citado or citado == citante:
                continue
            if ref[0] == "cita" and not g.has_edge(citante, citado):
                nuevas += 1
            g.add_edge(citante, citado)
    g.graph.update(citas_sin_doi=sum(ref[0] == "cita" for r in registros for ref in r["refs"]),
                   citas_sin_doi_enlazadas=nuevas)
    return g


def grafo_libros(registros_libros, articulos):
    """Red de libros sin orientación: cocitación más citas directas entre libros.

    El peso de un par es el número de artículos del corpus que citan ambos libros más las veces que uno
    cita al otro según las referencias del libro en Crossref. Se guarda en los dos sentidos, para que
    `calcular` use la matriz simétrica con pesos.
    """
    g = nx.DiGraph(simetrico=True)
    for r in registros_libros:
        g.add_node(clave_nodo(r), **atributos_nodo(r))
    cocitas, directas = Counter(), Counter()
    for r in articulos:
        citados = sorted({libro[1] for ref in r["refs"] if ref[0] == "doi" and (libro := doi_libro(ref[1]))
                          and libro[1] in g})
        for i, a in enumerate(citados):
            for b in citados[i + 1:]:
                cocitas[a, b] += 1
    indice = indice_claves(registros_libros)
    for r in registros_libros:
        citante = clave_nodo(r)
        for citado in {destino_referencia(ref, indice, {}) for ref in r["refs"]} - {None, citante}:
            directas[min(citante, citado), max(citante, citado)] += 1
            g.nodes[citante]["cita_libros"] = g.nodes[citante].get("cita_libros", 0) + 1
    for a, b in sorted(set(cocitas) | set(directas)):
        datos = {"weight": cocitas[a, b] + directas[a, b], "cocitaciones": cocitas[a, b], "citas": directas[a, b]}
        g.add_edge(a, b, **datos)
        g.add_edge(b, a, **datos)
    g.graph.update(pares_cocitados=len(cocitas), citas_directas=sum(directas.values()),
                   libros_con_referencias=sum(1 for r in registros_libros if r["refs"]))
    return g


@dataclass
class Medida:
    valores: np.ndarray | None
    radio: float | None
    nota: str


def validar_vector(b, x, radio):
    x = np.asarray(x, dtype=float).ravel()
    if x.sum() < 0:
        x = -x
    escala = np.max(np.abs(x))
    if not (np.isfinite(radio) and np.isfinite(escala) and escala > 0) or np.min(x) < -1e-8 * escala:
        raise ArithmeticError("el cálculo produjo valores no finitos o de signo incompatible")
    x = np.maximum(x / escala, 0)
    x /= np.linalg.norm(x)
    if np.linalg.norm(b @ x - radio * x) > 1e-7 * max(1.0, radio):
        raise ArithmeticError("el residuo de la ecuación de vector propio excede la tolerancia")
    return x


def perron_bloque(b, simetrico):
    """Bloque irreducible. b ya lleva el sentido elegido para la puntuación."""
    n = b.shape[0]
    grados = np.asarray(b.sum(axis=1)).ravel()
    if np.all(grados == grados[0]):
        return float(grados[0]), np.full(n, 1 / math.sqrt(n))
    if n <= 64:
        descomponer = np.linalg.eigh if simetrico else np.linalg.eig
        lambdas, vectores = descomponer(b.toarray())
        j = int(lambdas.real.argmax())
        lam, x = lambdas[j], vectores[:, j]
    else:
        resolver = sla.eigsh if simetrico else sla.eigs
        lambdas, vectores = resolver(b, k=1, which="LA" if simetrico else "LR",
                                     v0=np.ones(n), tol=1e-11, maxiter=max(2500, 5 * n))
        lam, x = lambdas[0], vectores[:, 0]
    if abs(np.imag(lam)) > 1e-8 * max(1, abs(lam)):
        raise ArithmeticError("el valor propio dominante calculado tiene parte imaginaria")
    radio = float(np.real(lam))
    return radio, validar_vector(b, np.real(x), radio)


def mismo_radio(a, b):
    return math.isclose(a, b, rel_tol=EPS_RADIO, abs_tol=1e-12)


def centralidad_simetrica(a, componentes):
    # max(A, A.T) da un solo enlace aunque existan citas en ambos sentidos.
    b = a.maximum(a.T).tocsr()
    if not b.nnz:
        return Medida(None, 0.0, "No hay citas internas para calcular centralidad.")
    bloques = []
    for indices in componentes:
        if len(indices) > 1:
            radio, vector = perron_bloque(b[indices][:, indices], True)
            bloques.append((indices, radio, vector))
    maximo = max(r for _, r, _ in bloques)
    x = np.zeros(a.shape[0])
    dominantes = 0
    for indices, radio, vector in bloques:
        if mismo_radio(radio, maximo):
            x[indices] = vector
            dominantes += 1
    nota = "Vector propio global de la red sin orientación. Las componentes de menor radio reciben cero."
    if dominantes > 1:
        nota += (f" Empate espectral entre {dominantes} componentes: se asignó la misma norma"
                 " a cada componente dominante antes de normalizar el vector global.")
    return Medida(validar_vector(b, x, maximo), maximo, nota)


def centralidad_dirigida(g, a, pos):
    """Perron izquierdo: resuelve A.T @ x = rho * x, con A[citante,citado]=1."""
    if nx.is_directed_acyclic_graph(g):
        return Medida(None, 0.0, "El grafo es acíclico. A es nilpotente y su radio espectral es cero.")
    b = a.T.tocsr()
    cond = nx.condensation(g)
    indices = {j: np.array(sorted(pos[w] for w in d["members"]), dtype=int)
               for j, d in cond.nodes(data=True)}
    bloques = {j: perron_bloque(b[idx][:, idx], False) for j, idx in indices.items() if len(idx) > 1}
    radio = max(r for r, _ in bloques.values())
    criticos = {j for j, (r, _) in bloques.items() if mismo_radio(r, radio)}
    # Con flujo citante -> citado, sólo los bloques críticos sin otro crítico
    # aguas abajo pueden originar un vector de Perron no negativo.
    alcanza_critico, finales = set(), []
    for j in reversed(list(nx.topological_sort(cond))):
        aguas_abajo = any(dest in alcanza_critico for dest in cond.successors(j))
        if j in criticos and not aguas_abajo:
            finales.append(j)
        if j in criticos or aguas_abajo:
            alcanza_critico.add(j)
    if len(finales) != 1:
        return Medida(None, radio, f"Hay {len(finales)} bloques dominantes finales con el mismo radio "
                                   "a la precisión usada. No se escoge un vector dirigido arbitrariamente.")
    raiz = finales[0]
    base = indices[raiz]
    x = np.zeros(a.shape[0])
    x[base] = bloques[raiz][1]
    sucesores = nx.descendants(cond, raiz)
    if sucesores:
        idx = np.concatenate([indices[j] for j in sorted(sucesores)])
        sistema = sparse.eye(len(idx), format="csc") * radio - b[idx][:, idx].tocsc()
        termino = b[idx][:, base] @ x[base]
        x[idx] = sla.spsolve(sistema.tocsc(), termino)
    return Medida(validar_vector(b, x, radio), radio,
                  "Centralidad entrante. Sólo pueden puntuar el bloque dominante final y los artículos que alcanza.")


def calcular(g):
    ids = sorted(g)
    if not g.number_of_edges():
        motivo = Medida(None, 0.0, "El corpus carece de citas internas. No hay ranking.")
        return ids, {"dirigido": motivo, "simetrico": motivo}
    pos = {w: i for i, w in enumerate(ids)}
    a = sparse.csr_matrix(nx.to_scipy_sparse_array(g, nodelist=ids, dtype=float, format="csr"))
    componentes = [np.array(sorted(pos[w] for w in c), dtype=int) for c in nx.weakly_connected_components(g)]
    medidas = {}
    for modo, funcion, args in (("dirigido", centralidad_dirigida, (g, a, pos)),
                                ("simetrico", centralidad_simetrica, (a, componentes))):
        try:
            medidas[modo] = funcion(*args)
        except (ArithmeticError, np.linalg.LinAlgError, sla.ArpackError) as exc:
            medidas[modo] = Medida(None, None, f"No se pudo verificar el vector propio: {exc}")
    return ids, medidas


COLUMNAS = ("puesto", "centralidad", "metrica_ranking", "id", "titulo", "anio", "autores", "revista", "doi",
            "doi_alternativo", "url",
            "fuentes", "subareas", "citas_recibidas_corpus", "referencias_en_corpus", "citas_openalex",
            "citas_crossref", "citas_scopus", "citas_google_academico", "referencias_enlazadas",
            "id_openalex", "id_scopus", "id_philpapers", "vector_propio_dirigido", "vector_propio_simetrico")


def ordenar(g, ids, medidas, modo):
    if modo == "auto":
        elegida = next((k for k in ("dirigido", "simetrico") if medidas[k].valores is not None), None)
    else:
        elegida = modo if medidas[modo].valores is not None else None
    for i, w in enumerate(ids):
        d = g.nodes[w]
        d["metrica_ranking"] = elegida or "sin_ranking"
        d["citas_recibidas_corpus"] = g.in_degree(w)
        d["referencias_en_corpus"] = g.out_degree(w)
        for k, medida in medidas.items():
            if medida.valores is not None:
                d["vector_propio_" + k] = float(medida.valores[i])
        if elegida:
            d["centralidad"] = d["vector_propio_" + elegida]
    orden = sorted(ids, key=lambda w: (-g.nodes[w].get("centralidad", 0), g.nodes[w]["titulo"].casefold(), w))
    # Un empate numérico comparte puesto. Los ceros y los datos ausentes
    # carecen de puesto, pero sus artículos siguen en el CSV y en el grafo.
    inicio = 0
    while inicio < len(orden):
        valor = g.nodes[orden[inicio]].get("centralidad", 0)
        if valor <= 0:
            break
        fin = inicio + 1
        while fin < len(orden) and math.isclose(g.nodes[orden[fin]]["centralidad"], valor, rel_tol=1e-9):
            fin += 1
        orden[inicio:fin] = sorted(orden[inicio:fin], key=lambda w: (g.nodes[w]["titulo"].casefold(), w))
        for w in orden[inicio:fin]:
            g.nodes[w]["puesto"] = inicio + 1
        inicio = fin
    return orden, elegida or "sin_ranking"


def autorias(r):
    """Pares (nombre, id de OpenAlex o "") de un registro, en el orden de la publicación."""
    nombres = r["autores"].split("; ") if r["autores"] else []
    ids = r.get("autores_ids") or []
    if len(ids) != len(nombres):
        ids = [""] * len(nombres)
    # Google Académico recorta la lista con «…»: esos restos no son nombres.
    return [(n.strip(), i) for n, i in zip(nombres, ids) if normalizar(n)]


def indice_h(citas):
    return sum(1 for i, c in enumerate(sorted(citas, reverse=True), 1) if c >= i)


def citas_globales(d):
    """Contador global de citas de una obra: OpenAlex para los artículos y Crossref para los libros."""
    return (d.get("citas_crossref") if d.get("tipo_obra") == "libro" else d.get("citas_openalex")) or 0


def importancia_articulo(g):
    """Clave de orden: centralidad del ranking, citas internas, citas globales y título."""
    return lambda w: (-g.nodes[w].get("centralidad", 0.0), -g.in_degree(w),
                      -citas_globales(g.nodes[w]), g.nodes[w]["titulo"].casefold(), w)


METRICAS_AUTOR = (  # clave, nombre, nombre corto, entero, nota
    ("pagerank", "PageRank en la red de citas entre autores", "PageRank", False,
     "Prestigio recursivo: un autor puntúa más si lo citan autores que a su vez reciben citas. "
     "Se excluyen las autocitas."),
    ("citas_internas", "Citas internas recibidas", "citas internas", True,
     "Artículos del corpus que citan algún artículo del autor, sin autocitas."),
    ("h_interno", "Índice h interno", "índice h", True,
     "h artículos del autor con al menos h citas internas cada uno, sin autocitas."),
    ("articulos", "Artículos en el corpus", "artículos", True, "Artículos firmados por el autor."),
    ("centralidad_acumulada", "Centralidad acumulada", "centralidad acumulada", False,
     "Suma de la centralidad de vector propio de sus artículos según la métrica del ranking."),
    ("citas_openalex", "Citas globales (OpenAlex)", "citas globales", True,
     "Suma del contador global de citas de OpenAlex de sus artículos, dentro y fuera del corpus."),
)


def analizar_autores(registros, g, citas=None):
    """Métricas por autor y red de autores derivada de g.

    En una red de citas, A -> B si una obra de A cita una de B. En una red simétrica (la de libros) cada
    par se recorre una vez y une a los autores en ambos sentidos con el peso del par: es una red de
    cocitación de autores. `citas` da las citas recibidas por obra cuando no son las aristas de g (en
    la red de libros, los artículos del corpus que citan cada libro).
    """
    por_nodo = {clave_nodo(r): r for r in registros}
    pares = {k: autorias(r) for k, r in por_nodo.items()}
    # Un nombre sin identificador se une al autor de OpenAlex que lo lleva, si sólo hay uno.
    ids_por_nombre = defaultdict(set)
    for lista in pares.values():
        for nombre, ident in lista:
            if ident:
                ids_por_nombre[normalizar(nombre)].add(ident)
    autores_de, variantes, unidas = {}, defaultdict(Counter), 0
    for k, lista in pares.items():
        claves = []
        for nombre, ident in lista:
            if not ident and len(candidatos := ids_por_nombre.get(normalizar(nombre), ())) == 1:
                ident = next(iter(candidatos))
                unidas += 1
            clave = ident or "nombre:" + normalizar(nombre)
            variantes[clave][nombre] += 1
            if clave not in claves:
                claves.append(clave)
        autores_de[k] = claves
    nombre = {c: max(v.items(), key=lambda x: (x[1], len(x[0]), x[0]))[0] for c, v in variantes.items()}

    # Una cita entre artículos que comparten un autor es autocita: no cuenta para nadie.
    red = nx.DiGraph()
    red.add_nodes_from(sorted(nombre))
    recibidas, autocitas = Counter(), 0
    simetrico = g.graph.get("simetrico", False)

    def unir(x, y, peso):
        red.add_edge(x, y, weight=(red.edges[x, y]["weight"] if red.has_edge(x, y) else 0) + peso)

    for u, v in sorted(g.edges()):
        if simetrico and u > v:
            continue  # el par ya se contó en el otro sentido
        de, a = autores_de.get(u, []), autores_de.get(v, [])
        if set(de) & set(a):
            autocitas += 1
            continue
        recibidas[v] += 1
        peso = g.edges[u, v].get("weight", 1)
        for x in de:
            for y in a:
                unir(x, y, peso)
                if simetrico:
                    unir(y, x, peso)
    if citas is not None:
        recibidas = Counter(citas)
    con_pagerank = red.number_of_edges() > 0
    pagerank = nx.pagerank(red, alpha=ALFA_PAGERANK, weight="weight") if con_pagerank else {}
    con_centralidad = any("centralidad" in g.nodes[w] for w in g)

    articulos = defaultdict(list)
    for k in sorted(autores_de):
        for c in autores_de[k]:
            articulos[c].append(k)
    clave_articulo = importancia_articulo(g)
    filas = []
    for c, arts in articulos.items():
        arts.sort(key=clave_articulo)
        coautores = {x for k in arts for x in autores_de[k]} - {c}
        filas.append({
            "clave": c, "autor": nombre[c], "id_openalex": "" if c.startswith("nombre:") else c,
            "variantes_nombre": "; ".join(sorted(set(variantes[c]) - {nombre[c]})),
            "articulos": len(arts),
            "articulos_fraccionales": round(sum(1 / len(autores_de[k]) for k in arts), 4),
            "citas_internas": sum(recibidas[k] for k in arts),
            "h_interno": indice_h([recibidas[k] for k in arts]),
            "citas_openalex": sum(citas_globales(g.nodes[k]) for k in arts),
            "centralidad_acumulada": float(sum(g.nodes[k].get("centralidad", 0.0) for k in arts)),
            "pagerank": float(pagerank.get(c, 0.0)),
            "citas_emitidas": int(red.out_degree(c, weight="weight")),
            "coautores": len(coautores),
            "articulos_ids": arts,
        })
    filas.sort(key=lambda f: (-f["pagerank"], -f["citas_internas"], -f["citas_openalex"], f["autor"].casefold(),
                              f["clave"]))
    for i, f in enumerate(filas, 1):
        f["puesto"] = i
    disponibles = {"pagerank": con_pagerank, "centralidad_acumulada": con_centralidad}
    con_id = sum(1 for c in nombre if not c.startswith("nombre:"))
    resumen = {"autores": len(filas), "con_id_openalex": con_id, "solo_nombre": len(filas) - con_id,
               "autorias_unidas_por_nombre": unidas,
               # En una red simétrica cada vínculo figura en los dos sentidos.
               "citas_entre_autores": int(red.size(weight="weight") / (2 if simetrico else 1)),
               "autocitas_excluidas": autocitas, "metrica_ranking": "pagerank" if con_pagerank else "citas_internas",
               "pagerank_alfa": ALFA_PAGERANK,
               "nota": ("Orden: PageRank, citas internas, citas globales y nombre." if con_pagerank else
                        "No hay citas entre autores distintos: se ordena por citas internas, citas globales y nombre.")}
    return {"filas": filas, "red": red, "autores_de": autores_de, "nombre": nombre,
            "disponibles": disponibles, "resumen": resumen}


# ---------------------------------------------------------------------------------------------
# Clusters

METODOS_CLUSTER = (  # clave, nombre, descripción
    ("louvain_dirigido", "Louvain dirigido",
     "Louvain que maximiza la modularidad dirigida de Leicht y Newman en la red de citas internas."),
    ("leiden", "Leiden dirigido",
     "Leiden con la misma modularidad dirigida; a diferencia de Louvain, garantiza grupos conexos."),
    ("infomap", "Infomap",
     "Ecuación de mapa: agrupa los artículos donde el flujo de citas, que sigue el sentido de las flechas, "
     "queda atrapado más tiempo."),
    ("acoplamiento", "Acoplamiento bibliográfico",
     "Louvain sobre la similitud del coseno entre bibliografías completas, incluidas las obras citadas que no "
     "están en el corpus. Agrupa frentes de investigación y cubre también artículos sin citas internas."),
    ("cocitacion", "Cocitación",
     "Louvain sobre la similitud del coseno entre los conjuntos de artículos del corpus que citan a cada "
     "artículo. Agrupa la base intelectual: obras que se citan juntas."),
    ("espectral", "Espectral bibliométrico",
     "Clustering espectral sobre la simetrización bibliométrica con descuento de grado (Satuluri y "
     "Parthasarathy, 2011), que suma acoplamiento y cocitación. Cada componente conexa se divide en el número "
     "de grupos, entre 2 y 15, que da la partición de mayor modularidad."),
)
PAQUETES_OPCIONALES = {"infomap": "infomap", "leiden": "igraph leidenalg"}

PALABRA = re.compile(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*")
# Palabras vacías (sin tildes) en inglés, español, francés, alemán, italiano y portugués, y rótulos
# editoriales. Quedan fuera las que nombran conceptos filosóficos: will, being, other, part, otro.
VACIAS = frozenset("""
a about after against all also among an and any are as at be been before between beyond both but by can
could did do does during each either for from had has have he her here his how i if in into is it its may might
more most must my no nor not of on one or our over own per s she should so some such than that the their
them then there these they this those through to too toward towards two under upon us via vs versus was we were
what when where whether which while who whom why with within without would yet your new ii iii iv
reply response comment comments note notes introduction review essay essays study studies analysis approach
revisited special issue symposium chapter case
el la los las un una unos unas y e o u de del al en por para con sin sobre entre hacia desde hasta segun que se
su sus lo es son como mas pero ni este esta estos estas ese esa eso esos esas cual cuales quien donde cuando
porque ya si le les nos mi tu muy tambien otra otros otras todo toda todos todas uno dos acerca estudio
nuevo nueva nota introduccion resena respuesta comentario
le les des du une et ou dans par pour avec sans sur chez vers au aux ce cette ces est sont qui quoi dont sa son
ses leur leurs pas ne plus
der die das den dem des ein eine einer eines einem einen und oder im zu zur zum von vom mit auf fur uber bei aus
nach als ist sind nicht sich an am wie zwischen
il gli di della dei delle ed nel nella per tra fra da che non
os um uma do da dos das em no na nas seu sua ao aos
""".split())


def terminos_titulo(titulo):
    """Palabras y pares de palabras consecutivas con contenido de un título."""
    utiles = []
    for p in PALABRA.findall(str(titulo or "").casefold()):
        p = re.sub(r"['’]s$", "", re.sub(r"^(?:l|d|qu|n|s|c|j|m|t)['’]", "", p))
        utiles.append(p if len(p) > 1 and normalizar(p) not in VACIAS else None)
    return {p for p in utiles if p} | {f"{x} {y}" for x, y in zip(utiles, utiles[1:]) if x and y}


def terminos_distintivos(miembros, terminos, frecuencia, total, cuantos=6):
    """Términos frecuentes en el cluster y raros en el corpus (tf-idf a nivel de cluster)."""
    en_cluster = Counter(t for w in miembros for t in terminos[w])
    minimo = 2 if len(miembros) >= 4 else 1
    puntos = sorted(((cnt / len(miembros)) * math.log(total / frecuencia[t]), t)
                    for t, cnt in en_cluster.items() if cnt >= minimo and frecuencia[t] < total)
    elegidos = []
    for _, t in sorted(puntos, key=lambda x: (-x[0], x[1])):
        if " " in t:
            # Un par de palabras sustituye a las palabras sueltas que lo forman.
            elegidos = [e for e in elegidos if e not in t.split(" ")]
        elif any(t in e.split(" ") for e in elegidos):
            continue
        elegidos.append(t)
        if len(elegidos) == cuantos:
            break
    return elegidos


def matriz_referencias(registros, ids, indice, titulos):
    """Matriz binaria artículo × obra citada, con las obras de fuera del corpus incluidas."""
    pos = {w: i for i, w in enumerate(ids)}
    columnas, filas, cols = {}, [], []
    for r in registros:
        k = clave_nodo(r)
        for ref in sorted(r["refs"]):  # orden fijo de columnas: resultados reproducibles
            destino = destino_referencia(ref, indice, titulos)
            if destino == k:
                continue
            if destino:
                columna = ("nodo", destino)
            elif ref[0] == "cita":
                columna = ("cita", ref[2], ref[3])  # autor y título, sin año: une ediciones de un libro
            elif libro := doi_libro(ref[1]):
                # Un capítulo y el libro entero son la misma obra citada: comparten columna.
                columna = ("libro", libro[1])
            else:
                columna = ref
            filas.append(pos[k])
            cols.append(columnas.setdefault(columna, len(columnas)))
    m = sparse.csr_matrix((np.ones(len(filas)), (filas, cols)), shape=(len(ids), len(columnas)))
    m.sum_duplicates()
    m.data[:] = 1.0
    return m


def potencia_inversa(v, p):
    v = np.asarray(v, dtype=float).ravel()
    return np.divide(1.0, v ** p, out=np.zeros_like(v), where=v > 0)


def normalizar_filas(m):
    m = sparse.csr_matrix(m, dtype=float)
    return (sparse.diags(potencia_inversa(np.asarray(m.multiply(m).sum(axis=1)), .5)) @ m).tocsr()


def vecinos_similares(x, k):
    """Matriz simétrica con, para cada fila, los k productos escalares mayores con otras filas."""
    x = sparse.csr_matrix(x, dtype=float)
    n = x.shape[0]
    xt = x.T.tocsc()
    filas, cols, pesos = [], [], []
    for inicio in range(0, n, 512):
        bloque = (x[inicio:inicio + 512] @ xt).tocsr()
        for i in range(bloque.shape[0]):
            fila = inicio + i
            js = bloque.indices[bloque.indptr[i]:bloque.indptr[i + 1]]
            vs = bloque.data[bloque.indptr[i]:bloque.indptr[i + 1]]
            utiles = (js != fila) & (vs > 1e-12)
            js, vs = js[utiles], vs[utiles]
            if len(js) > k:
                orden = np.lexsort((js, -vs))[:k]  # desempate por índice: resultado reproducible
                js, vs = js[orden], vs[orden]
            filas.extend([fila] * len(js))
            cols.extend(js.tolist())
            pesos.extend(vs.tolist())
    w = sparse.csr_matrix((pesos, (filas, cols)), shape=(n, n))
    return w.maximum(w.T).tocsr()


def grafo_desde_matriz(w, ids):
    red = nx.Graph()
    red.add_nodes_from(ids)
    triangulo = sparse.triu(w, k=1).tocoo()
    red.add_weighted_edges_from((ids[i], ids[j], float(v)) for i, j, v in
                                zip(triangulo.row, triangulo.col, triangulo.data))
    return red


def simetrizacion_bibliometrica(refs, a):
    """Filas Z con Z·Zᵀ = acoplamiento y cocitación con descuento de grado (Satuluri y Parthasarathy, 2011).

    Acoplamiento: D_r^-1/2 R D_f^-1/2 Rᵀ D_r^-1/2, con R artículo × obra citada (también externas).
    Cocitación:   D_e^-1/2 Aᵀ D_s^-1/2 A D_e^-1/2, con A[i, j] = 1 si i cita a j dentro del corpus.
    Las obras muy citadas y los artículos con bibliografías largas pesan menos.
    """
    x = (sparse.diags(potencia_inversa(refs.sum(axis=1), .5)) @ refs
         @ sparse.diags(potencia_inversa(refs.sum(axis=0), .25)))
    y = (sparse.diags(potencia_inversa(a.sum(axis=0), .5)) @ a.T
         @ sparse.diags(potencia_inversa(a.sum(axis=1), .25)))
    return sparse.hstack([x, y]).tocsr()


def kmedias(x, k, rng, intentos=10, iteraciones=100):
    """k-medias con inicio k-means++; conserva la mejor de varias ejecuciones."""
    mejor = None
    for _ in range(intentos):
        centros = [x[rng.integers(len(x))]]
        d2 = ((x - centros[0]) ** 2).sum(axis=1)
        for _ in range(1, k):
            total = d2.sum()
            j = rng.choice(len(x), p=d2 / total) if total > 0 else rng.integers(len(x))
            centros.append(x[j])
            d2 = np.minimum(d2, ((x - x[j]) ** 2).sum(axis=1))
        c = np.array(centros)
        for _ in range(iteraciones):
            distancias = ((x[:, None, :] - c[None, :, :]) ** 2).sum(axis=2)
            etiquetas = distancias.argmin(axis=1)
            nuevos = np.array([x[etiquetas == j].mean(axis=0) if np.any(etiquetas == j) else c[j] for j in range(k)])
            if np.allclose(nuevos, c):
                break
            c = nuevos
        inercia = distancias[np.arange(len(x)), etiquetas].sum()
        if mejor is None or inercia < mejor[0]:
            mejor = (inercia, etiquetas)
    return mejor[1]


def modularidad_matriz(w, etiquetas):
    """Modularidad de Newman de una partición de un grafo no dirigido dado por su matriz simétrica de pesos."""
    total = w.sum()
    if not total:
        return 0.0
    h = sparse.csr_matrix((np.ones(len(etiquetas)), (np.arange(len(etiquetas)), etiquetas)))
    volumen = h.T @ np.asarray(w.sum(axis=1)).ravel()
    return float((h.T @ w @ h).diagonal().sum() / total - np.sum((volumen / total) ** 2))


def espectral(w, minimo):
    """Clustering espectral (Ng, Jordan y Weiss) de cada componente conexa de w, que es simétrica.

    Para cada k entre 2 y MAX_K_ESPECTRAL se agrupan con k-medias las filas normalizadas de los k primeros
    vectores propios de D^-1/2 W D^-1/2, y se conserva el k de mayor modularidad (White y Smyth, 2005).
    La regla de la mayor brecha entre valores propios falla en redes ruidosas: la primera brecha, trivial,
    suele ganar aunque haya grupos claros. Una componente sólo queda entera si ninguna división mejora la
    modularidad.
    """
    if not w.shape[0] or not w.nnz:
        return [], []
    _, componente = csgraph.connected_components(w, directed=False)
    rng = np.random.default_rng(SEMILLA)
    grupos, detalle = [], []
    for c in range(componente.max() + 1):
        idx = np.flatnonzero(componente == c)
        if len(idx) < minimo:
            continue
        tope = min(MAX_K_ESPECTRAL, len(idx) // minimo, len(idx) - 2)
        calidad, k, etiquetas = 0.0, 1, np.zeros(len(idx), dtype=int)
        if tope >= 2:
            sub = w[idx][:, idx]
            d = potencia_inversa(sub.sum(axis=1), .5)
            m = (sparse.diags(d) @ sub @ sparse.diags(d)).tocsr()
            if len(idx) <= 600:
                mu, v = np.linalg.eigh(m.toarray())
                v = v[:, ::-1][:, :tope]
            else:
                mu, v = sla.eigsh(m, k=tope, which="LA", v0=np.ones(len(idx)), tol=1e-9,
                                  maxiter=max(3000, 20 * len(idx)))
                v = v[:, np.argsort(-mu)]
            for candidato in range(2, tope + 1):
                x = v[:, :candidato]
                x = x / np.maximum(np.linalg.norm(x, axis=1, keepdims=True), 1e-12)
                propuesta = kmedias(x, candidato, rng)
                q = modularidad_matriz(sub, propuesta)
                if q > calidad + 1e-9:
                    calidad, k, etiquetas = q, candidato, propuesta
        grupos.extend(idx[etiquetas == j] for j in range(k))
        detalle.append({"articulos": int(len(idx)), "k": k, "modularidad": round(calidad, 4)})
    return grupos, detalle


def infomap_dirigido(g):
    from infomap import Infomap  # dependencia opcional
    if not g.number_of_edges():
        return []
    nodos = sorted(g)
    pos = {w: i for i, w in enumerate(nodos)}
    im = Infomap(silent=True, directed=True, two_level=True, seed=SEMILLA, num_trials=10)
    for u, v in sorted(g.edges()):
        im.add_link(pos[u], pos[v], float(g.edges[u, v].get("weight", 1)))
    im.run()
    grupos = defaultdict(set)
    for i, modulo in im.get_modules().items():
        grupos[modulo].add(nodos[i])
    return list(grupos.values())


def leiden_dirigido(g, resolucion):
    import igraph  # dependencias opcionales
    import leidenalg
    nodos = sorted(g)
    pos = {w: i for i, w in enumerate(nodos)}
    aristas = sorted(g.edges())
    red = igraph.Graph(n=len(nodos), edges=[(pos[u], pos[v]) for u, v in aristas], directed=True)
    pesos = [float(g.edges[u, v].get("weight", 1)) for u, v in aristas]
    particion = leidenalg.find_partition(red, leidenalg.RBConfigurationVertexPartition, weights=pesos,
                                         resolution_parameter=resolucion, seed=SEMILLA, n_iterations=-1)
    grupos = defaultdict(set)
    for i, c in enumerate(particion.membership):
        grupos[c].add(nodos[i])
    return list(grupos.values())


def particion(grupos, minimo):
    """{artículo: número de cluster}; 1 es el mayor. Los grupos con menos de «minimo» quedan sin asignar."""
    validos = sorted((sorted(c) for c in grupos if len(c) >= minimo), key=lambda c: (-len(c), c[0]))
    return {w: i for i, c in enumerate(validos, 1) for w in c}


def modularidad(grafo, asignacion):
    if grafo is None or not grafo.size(weight="weight"):
        return None
    grupos = defaultdict(set)
    for w in grafo:
        grupos[asignacion.get(w, ("sin_grupo", w))].add(w)
    return float(nx.community.modularity(grafo, list(grupos.values()), weight="weight"))


def codigos(valores):
    vistos = {}
    return np.array([vistos.setdefault(v, len(vistos)) for v in valores], dtype=int)


def contingencia(a, b):
    ia, ib = codigos(a), codigos(b)
    m = np.zeros((ia.max() + 1, ib.max() + 1))
    np.add.at(m, (ia, ib), 1)
    return m


def ari(a, b):
    """Índice de Rand ajustado (Hubert y Arabie) entre dos particiones de los mismos artículos."""
    m = contingencia(a, b)
    pares = lambda x: float((x * (x - 1) / 2).sum())  # noqa: E731
    suma, fa, fb = pares(m), pares(m.sum(axis=1)), pares(m.sum(axis=0))
    esperado = fa * fb / (len(a) * (len(a) - 1) / 2)
    maximo = (fa + fb) / 2
    if math.isclose(maximo, esperado):
        return 1.0 if math.isclose(suma, maximo) else 0.0
    return (suma - esperado) / (maximo - esperado)


def nmi(a, b):
    """Información mutua normalizada por la media aritmética de las entropías."""
    p = contingencia(a, b)
    p /= p.sum()
    pa, pb = p.sum(axis=1), p.sum(axis=0)
    entropia = lambda q: -float(np.sum(q[q > 0] * np.log(q[q > 0])))  # noqa: E731
    ha, hb = entropia(pa), entropia(pb)
    if not ha and not hb:
        return 1.0
    if not ha or not hb:
        return 0.0
    hay = p > 0
    mutua = float(np.sum(p[hay] * np.log(p[hay] / np.outer(pa, pb)[hay])))
    return min(1.0, max(0.0, mutua / ((ha + hb) / 2)))


def resumen_metodo(entrada, g, principal):
    asignacion = entrada["asignacion"]
    con_tema = [(c, principal[w]) for w, c in asignacion.items() if principal.get(w)]
    coherencia = base = alineacion = None
    if con_tema:
        por_cluster = defaultdict(Counter)
        for c, t in con_tema:
            por_cluster[c][t] += 1
        coherencia = sum(max(x.values()) for x in por_cluster.values()) / len(con_tema)
        base = max(Counter(t for _, t in con_tema).values()) / len(con_tema)
        alineacion = nmi(*zip(*con_tema)) if len(con_tema) > 1 else None
    return {"clusters": len(set(asignacion.values())), "articulos_asignados": len(asignacion),
            "cobertura": len(asignacion) / len(g) if len(g) else 0.0,
            "modularidad_propia": modularidad(entrada["grafo"], asignacion) if asignacion else None,
            "modularidad_citas": modularidad(g, asignacion) if asignacion else None,
            "coherencia_tematica": coherencia, "coherencia_base": base, "nmi_temas": alineacion}


def describir_clusters(asignacion, g, por_nodo, terminos, frecuencia, nombres_temas, autores, libros=None):
    miembros = defaultdict(list)
    for w, c in asignacion.items():
        miembros[c].append(w)
    clave_articulo = importancia_articulo(g)
    puesto = {f["clave"]: f["puesto"] for f in autores["filas"]} if autores else {}
    # Libros de OUP y CUP que cita cada artículo, con su puesto en el ranking general.
    libros_de, libro = defaultdict(list), {}
    for f in (libros or {}).get("filas", []):
        libro[f["doi"]] = (f["puesto"], recortar(f["titulo"] or f["doi"], 90))
        for k in f["articulos"]:
            libros_de[k].append(f["doi"])
    flujo = Counter()
    for u, v in g.edges():
        cu, cv = asignacion.get(u), asignacion.get(v)
        if cu is not None and cu == cv:
            flujo[cu, "dentro"] += 1
        else:
            if cu is not None:
                flujo[cu, "hacia_fuera"] += 1
            if cv is not None:
                flujo[cv, "desde_fuera"] += 1
    fichas = []
    for c in sorted(miembros):
        arts = sorted(miembros[c], key=clave_articulo)
        n = len(arts)
        temas = Counter(t for w in arts for t in (por_nodo[w]["temas"] or []))
        subareas = Counter(s for w in arts for s in (por_nodo[w].get("subareas") or []))
        anios = sorted(a for w in arts if (a := g.nodes[w].get("anio")) is not None)
        firmas = Counter(x for w in arts for x in (autores["autores_de"].get(w, []) if autores else []))
        citados = Counter(d for w in arts for d in libros_de.get(w, ()))
        fichas.append({
            "c": c, "n": n,
            "anios": (f"{anios[0]}–{anios[-1]}" if anios[0] != anios[-1] else str(anios[0])) if anios else "s/f",
            "mediana_anio": int(anios[len(anios) // 2]) if anios else None,
            "terminos": terminos_distintivos(arts, terminos, frecuencia, len(g)),
            "temas": [[nombres_temas.get(t, t), cnt / n] for t, cnt in
                      sorted(temas.items(), key=lambda x: (-x[1], x[0]))[:3]],
            "subareas": [[SUBAREAS[s][0], cnt / n] for s, cnt in
                         sorted(subareas.items(), key=lambda x: (-x[1], x[0]))[:3]],
            "articulos": arts[:3],
            # Más artículos en el cluster primero; a igualdad, el mejor puesto del ranking de autores.
            "autores": [[autores["nombre"][x], cnt] for x, cnt in
                        sorted(firmas.items(), key=lambda x: (-x[1], puesto.get(x[0], math.inf), x[0]))[:3]],
            # Libros de OUP y CUP más citados por el cluster; a igualdad, el mejor puesto general.
            "libros": [[libro[d][1], cnt] for d, cnt in
                       sorted(citados.items(), key=lambda x: (-x[1], libro[x[0]][0]))[:3]],
            "citas_dentro": flujo[c, "dentro"], "citas_hacia_fuera": flujo[c, "hacia_fuera"],
            "citas_desde_fuera": flujo[c, "desde_fuera"]})
    return fichas


# Fallos de cálculo que un análisis secundario anota sin detener el programa.
ERRORES_ANALISIS = (ArithmeticError, LookupError, TypeError, ValueError, RuntimeError, MemoryError,
                    np.linalg.LinAlgError, nx.NetworkXException)


def analizar_clusters(registros, g, autores, resolucion, minimo, libros=None):
    """Ejecuta los métodos de clustering, los describe y mide su concordancia."""
    ids = sorted(g)
    por_nodo = {clave_nodo(r): r for r in registros}
    a = refs = None
    if ids:
        a = sparse.csr_matrix(nx.to_scipy_sparse_array(g, nodelist=ids, dtype=float, format="csr"))
        refs = matriz_referencias(registros, ids, indice_claves(registros), indice_titulos(registros))
    metodos = {}
    for clave, nombre, descripcion in METODOS_CLUSTER:
        entrada = {"clave": clave, "nombre": nombre, "descripcion": descripcion, "disponible": True, "nota": "",
                   "asignacion": {}, "grafo": None, "detalle": []}
        try:
            if not ids:
                grupos = []
            elif clave == "louvain_dirigido":
                entrada["grafo"] = g
                grupos = nx.community.louvain_communities(g, resolution=resolucion, seed=SEMILLA)
            elif clave == "leiden":
                entrada["grafo"] = g
                grupos = leiden_dirigido(g, resolucion)
            elif clave == "infomap":
                entrada["grafo"] = g
                grupos = infomap_dirigido(g)
            elif clave in ("acoplamiento", "cocitacion"):
                base = refs if clave == "acoplamiento" else a.T
                entrada["grafo"] = grafo_desde_matriz(vecinos_similares(normalizar_filas(base), VECINOS_SIMILITUD), ids)
                grupos = nx.community.louvain_communities(entrada["grafo"], weight="weight", resolution=resolucion,
                                                          seed=SEMILLA)
            else:
                w = vecinos_similares(simetrizacion_bibliometrica(refs, a), VECINOS_SIMILITUD)
                entrada["grafo"] = grafo_desde_matriz(w, ids)
                indices, entrada["detalle"] = espectral(w, minimo)
                grupos = [{ids[i] for i in idx} for idx in indices]
            entrada["asignacion"] = particion(grupos, minimo)
        except ImportError as exc:
            entrada.update(disponible=False, nota=f"falta el paquete opcional {exc.name or ''}; instálalo con "
                                                  f"python -m pip install {PAQUETES_OPCIONALES[clave]}")
        except ERRORES_ANALISIS as exc:
            entrada.update(disponible=False, nota=f"el cálculo falló ({type(exc).__name__}: {exc})")
        if entrada["disponible"] and not entrada["asignacion"]:
            entrada["nota"] = (f"No se formaron grupos de al menos {minimo} artículos"
                               + (" porque la red no tiene enlaces." if entrada["grafo"] is None
                                  or not entrada["grafo"].number_of_edges() else "."))
        metodos[clave] = entrada

    # Descripción de cada cluster: temas, subáreas, términos del título, artículos y autores clave.
    principal = {w: r.get("tema_principal") for w, r in por_nodo.items()}
    nombres_temas = dict(TEMAS_FILOSOFIA)
    for r in registros:
        nombres_temas.update(r.get("nombres_temas") or {})
    terminos = {w: terminos_titulo(g.nodes[w]["titulo"]) for w in ids}
    frecuencia = Counter(t for w in ids for t in terminos[w])
    for entrada in metodos.values():
        entrada["resumen"] = resumen_metodo(entrada, g, principal)
        entrada["fichas"] = describir_clusters(entrada["asignacion"], g, por_nodo, terminos, frecuencia,
                                               nombres_temas, autores, libros)
        if entrada["detalle"]:
            partes = sorted(entrada["detalle"], key=lambda d: -d["articulos"])
            divididas = [d for d in partes if d["k"] > 1]
            entrada["nota"] = (entrada["nota"] + " " if entrada["nota"] else "") + (
                f"{len(partes)} componentes con al menos {minimo} artículos; "
                + (", ".join(f"la de {d['articulos']} artículos se dividió en {d['k']}" for d in divididas[:3])
                   if divididas else "ninguna división mejoró la modularidad") + ".")
    concordancia = []
    validos = [m for m in metodos.values() if m["asignacion"]]
    for i, x in enumerate(validos):
        for y in validos[i + 1:]:
            comunes = sorted(set(x["asignacion"]) & set(y["asignacion"]))
            fila = {"a": x["clave"], "b": y["clave"], "comunes": len(comunes), "ari": None, "nmi": None}
            if len(comunes) > 1:
                pa = [x["asignacion"][w] for w in comunes]
                pb = [y["asignacion"][w] for w in comunes]
                fila.update(ari=ari(pa, pb), nmi=nmi(pa, pb))
            concordancia.append(fila)
    return {"metodos": metodos, "concordancia": concordancia, "resolucion": resolucion, "min_cluster": minimo,
            "vecinos_similitud": VECINOS_SIMILITUD}


def resumen_medidas(medidas):
    return {k: {"radio_espectral": m.radio, "nota": m.nota,
                "articulos_positivos": int(np.count_nonzero(m.valores > 0)) if m.valores is not None else None}
            for k, m in medidas.items()}


def analizar_version(clave, articulos, libros, modo, resolucion, minimo):
    """Red, centralidad, autores y clusters de la versión «libros» (sólo libros) o «mixta» (libros y artículos)."""
    registros_libros = libros["registros"]
    if clave == "libros":
        registros, g = registros_libros, grafo_libros(registros_libros, articulos)
    else:
        registros = articulos + registros_libros
        g = grafo_citas(registros)
    ids, medidas = calcular(g)
    simetrico = g.graph.get("simetrico", False)
    if simetrico:
        medidas["dirigido"] = Medida(None, None, "La red de libros no tiene orientación: suma cocitaciones y citas "
                                                 "en ambos sentidos. Se usa el vector propio con pesos.")
        modo = "simetrico"
    orden, metrica = ordenar(g, ids, medidas, modo)
    if clave == "libros":
        # En la red de libros, «citas recibidas» son los artículos del corpus que citan el libro y
        # «referencias» los libros de la red que cita; los grados de g cuentan vecinos de cocitación.
        for w in g:
            d = g.nodes[w]
            d["citas_recibidas_corpus"], d["referencias_en_corpus"] = d.get("citas_corpus", 0), d.get("cita_libros", 0)
    n_libros = sum(1 for w in g if g.nodes[w].get("tipo_obra") == "libro")
    resumen = {"nodos": len(g), "libros": n_libros, "articulos": len(g) - n_libros,
               "enlaces": g.number_of_edges() // (2 if simetrico else 1), "metrica_ranking": metrica,
               "medidas": resumen_medidas(medidas)}
    resumen.update({k: g.graph[k] for k in ("pares_cocitados", "citas_directas", "libros_con_referencias",
                                            "citas_sin_doi", "citas_sin_doi_enlazadas") if k in g.graph})
    version = {"clave": clave, "registros": registros, "g": g, "orden": orden, "metrica": metrica,
               "medidas": medidas, "autores": None, "clusters": None, "resumen": resumen}
    nombre = dict(VERSIONES)[clave].lower()
    try:
        citas = {clave_nodo(r): r["citas_corpus"] for r in registros_libros} if clave == "libros" else None
        version["autores"] = analizar_autores(registros, g, citas)
        resumen["autores"] = version["autores"]["resumen"]
    except ERRORES_ANALISIS as exc:
        resumen["autores"] = {"error": f"{type(exc).__name__}: {exc}"}
        print(f"Aviso: no se completó el análisis por autor ({nombre}). {exc}", file=sys.stderr, flush=True)
    try:
        version["clusters"] = analizar_clusters(registros, g, version["autores"], resolucion, minimo, libros)
        for metodo, m in version["clusters"]["metodos"].items():
            for w, c in m["asignacion"].items():
                g.nodes[w]["cluster_" + metodo] = c
        resumen["clusters"] = resumen_clusters(version["clusters"])
    except ERRORES_ANALISIS as exc:
        resumen["clusters"] = {"error": f"{type(exc).__name__}: {exc}"}
        print(f"Aviso: no se completó el análisis de clusters ({nombre}). {exc}", file=sys.stderr, flush=True)
    return version


ESTILO = """
:root{color-scheme:light dark;--fondo:#f5f7f6;--panel:#fff;--lienzo:#fbfcfa;--borde:#cbd9d9;--linea:#e3e9e9;
--tinta:#182e3b;--tinta2:#455c66;--tenue:#5d6d74;--enlace:#8f9ca4;--flecha:#536873;--activo:#162f3a;
--resalte:#e2f1ed;--cero:#d3d9dc;--pista:#e6ecee;--barra:#2a78d6;--acento:#006753;--anillo:#fbfcfa;
font:16px/1.45 Inter,system-ui,sans-serif;color:var(--tinta);background:var(--fondo)}
@media(prefers-color-scheme:dark){:root{--fondo:#111618;--panel:#182023;--lienzo:#141b1e;--borde:#2d3a3f;
--linea:#243034;--tinta:#e4edef;--tinta2:#a8b7bd;--tenue:#8d9ea4;--enlace:#5f6f77;--flecha:#9aabb3;
--activo:#e4edef;--resalte:#20333a;--cero:#3a4549;--pista:#263236;--barra:#5598e7;--acento:#63c7b0;--anillo:#141b1e}}
*{box-sizing:border-box}body{max-width:1530px;margin:0 auto;padding:28px 3vw}
h1{font-size:clamp(24px,3vw,41px);line-height:1.15;margin:0 0 10px}p{margin:.45rem 0;color:var(--tinta2)}
.rejilla{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(280px,1fr);gap:20px;margin-top:16px}
section{background:var(--panel);border:1px solid var(--borde);border-radius:9px;overflow:hidden}
h2{font-size:17px;margin:12px 16px}.ayuda,.nota,.aviso{padding:0 16px;font-size:14px}.ayuda{padding-bottom:12px}
.herramientas{display:flex;flex-wrap:wrap;gap:12px 24px;align-items:center;justify-content:space-between;padding:12px 16px 4px}
fieldset{border:0;margin:0;padding:0;display:flex;flex-wrap:wrap;gap:6px;align-items:center}
legend{float:left;font-weight:650;font-size:14px;margin-right:6px}
.opcion{font-size:14px;border:1px solid var(--borde);border-radius:999px;padding:4px 12px;cursor:pointer;user-select:none}
.opcion:has(input:checked){background:var(--resalte);border-color:var(--acento);font-weight:600}
.opcion:has(input:disabled){opacity:.5;cursor:not-allowed}.opcion input{accent-color:var(--acento);margin:0 6px 0 0}
.leyenda{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--tinta2);font-variant-numeric:tabular-nums}
.gradiente{width:150px;height:10px;border-radius:4px}.leyenda .cero{width:12px;height:12px;border-radius:50%;background:var(--cero);border:1px dashed var(--tenue)}
.lienzo{position:relative}svg{width:100%;display:block;height:auto;background:var(--lienzo)}
.lienzo.real{overflow:auto;max-height:80vh}.lienzo.real svg{width:var(--ancho);max-width:none}
.escala{font:inherit;font-size:14px;border:1px solid var(--borde);border-radius:999px;padding:4px 12px;background:none;color:inherit;cursor:pointer}
.escala[aria-pressed=true]{background:var(--resalte);border-color:var(--acento);font-weight:600}
.enlace{fill:none;stroke:var(--enlace);stroke-opacity:.75;stroke-width:1.2}marker path{fill:var(--flecha)}
.nodo{cursor:pointer;outline:none}.nodo circle{stroke:var(--anillo);stroke-width:2;transition:r .25s,fill .25s}
.nodo.nulo circle{stroke:var(--tenue);stroke-dasharray:2 2;stroke-width:1}
.nodo text{font:bold 10px system-ui;text-anchor:middle;dominant-baseline:central;pointer-events:none}
.nodo:focus-visible circle,.nodo.seleccion circle{stroke:var(--activo);stroke-width:3;stroke-dasharray:none}
.apagado{opacity:.13}.enlace.activo{stroke:var(--activo);stroke-width:2;stroke-opacity:1}
.tip{position:absolute;z-index:2;width:max-content;max-width:320px;padding:8px 10px;border-radius:7px;background:var(--panel);
border:1px solid var(--borde);box-shadow:0 4px 14px #0003;font-size:13px;pointer-events:none}
.tip strong{display:block;line-height:1.3;margin-bottom:3px}.tip span{display:block;color:var(--tinta2);font-variant-numeric:tabular-nums}
ol{list-style:none;margin:0;padding:0;max-height:640px;overflow:auto}
li button{width:100%;text-align:left;padding:8px 14px;border:0;border-top:1px solid var(--linea);display:flex;align-items:start;gap:10px;background:none;color:inherit;cursor:pointer;font:inherit}
li button:hover,li button[aria-pressed=true]{background:var(--resalte)}
.num{flex:none;min-width:27px;height:27px;line-height:27px;text-align:center;font-size:13px;font-weight:650;border-radius:50%}
.cuerpo{flex:1;min-width:0}small{display:block;color:var(--tenue);margin-top:2px;font-variant-numeric:tabular-nums}
.pista{display:block;height:6px;margin-top:5px;border-radius:3px;background:var(--pista);overflow:hidden}
.relleno{display:block;height:100%;border-radius:0 3px 3px 0;background:var(--barra);transition:width .25s}
.pestanas{display:flex;flex-wrap:wrap;gap:2px;border-bottom:1px solid var(--borde);margin:22px 0 6px}
.pestanas button{font:inherit;font-size:15px;font-weight:600;color:var(--tinta2);background:none;border:0;
border-bottom:3px solid transparent;margin-bottom:-1px;padding:10px 16px;border-radius:6px 6px 0 0;cursor:pointer}
.pestanas button:hover{color:var(--tinta);background:var(--resalte)}
.pestanas button[aria-selected=true]{color:var(--tinta);border-bottom-color:var(--acento)}
a{color:var(--acento)}button:focus-visible,a:focus-visible,.opcion:has(input:focus-visible){outline:2px solid #176a81;outline-offset:-3px}
@media(max-width:800px){.rejilla{grid-template-columns:1fr}body{padding:16px}ol{max-height:420px}}
"""
JS_COLOR = r"""
// Rampa secuencial azul de un solo tono: claro -> oscuro (en modo oscuro, oscuro -> claro).
const RAMPAS = {claro: ['#b7d3f6','#86b6ef','#5598e7','#2a78d6','#256abf','#1c5cab','#104281','#0d366b'],
                oscuro: ['#184f95','#1c5cab','#256abf','#2a78d6','#3987e5','#5598e7','#86b6ef','#cde2fb']};
const temaOscuro = matchMedia('(prefers-color-scheme: dark)');
const rampa = () => RAMPAS[temaOscuro.matches ? 'oscuro' : 'claro'];
const rgb = h => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16));
function tono(t){
  const r = rampa(), x = Math.min(Math.max(t, 0), 1) * (r.length - 1);
  const i = Math.min(Math.floor(x), r.length - 2), f = x - i, a = rgb(r[i]), b = rgb(r[i + 1]);
  return 'rgb(' + a.map((v, j) => Math.round(v + (b[j] - v) * f)).join(',') + ')';
}
function tintaSobre(color){
  const [r, g, b] = color.match(/\d+/g).map(v => { v /= 255; return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; });
  return .2126 * r + .7152 * g + .0722 * b > .2 ? '#0d1b24' : '#ffffff';
}
const crear = (etiqueta, clase) => { const e = document.createElement(etiqueta); if (clase) e.className = clase; return e; };
// Coloca la ventana emergente junto al círculo, dentro del lienzo (que puede estar desplazado).
function situar(tip, lienzo, circulo){
  const caja = lienzo.getBoundingClientRect(), c = circulo.getBoundingClientRect();
  const w = tip.offsetWidth, h = tip.offsetHeight, sx = lienzo.scrollLeft, sy = lienzo.scrollTop;
  let x = c.left + c.width / 2 - caja.left - w / 2 + sx, y = c.top - caja.top - h - 8 + sy;
  if (y < sy + 4) y = c.bottom - caja.top + 8 + sy;
  tip.style.left = Math.max(sx + 4, Math.min(x, sx + caja.width - w - 4)) + 'px';
  tip.style.top = y + 'px';
}
// Arista curva sólo si hay cita recíproca; termina en el borde de los círculos.
function trazarCurva(p, o, d, reciproca){
  const dx = d.x - o.x, dy = d.y - o.y, L = Math.hypot(dx, dy);
  if (L < 1e-9){ p.removeAttribute('d'); return; }
  let cx = o.x + dx / 2, cy = o.y + dy / 2;
  if (reciproca){ const s = Math.min(30, L * .24) / L; cx -= dy * s; cy += dx * s; }
  const l1 = Math.hypot(cx - o.x, cy - o.y), l2 = Math.hypot(d.x - cx, d.y - cy);
  const ax = o.x + (cx - o.x) / l1 * (o.r + 1), ay = o.y + (cy - o.y) / l1 * (o.r + 1);
  const bx = d.x - (d.x - cx) / l2 * (d.r + 2), by = d.y - (d.y - cy) / l2 * (d.r + 2);
  p.setAttribute('d', `M${ax.toFixed(2)} ${ay.toFixed(2)} Q${cx.toFixed(2)} ${cy.toFixed(2)} ${bx.toFixed(2)} ${by.toFixed(2)}`);
}
"""
# Pestañas «Red de artículos» y «Autores»: red con selector de métrica y lista ordenada.
JS_RED = r"""
function iniciarRed(raiz){
const datos = JSON.parse(raiz.querySelector('script.datos').textContent);
const NS = 'http://www.w3.org/2000/svg';
const svg = raiz.querySelector('svg.red'), lienzo = raiz.querySelector('.lienzo');
const flecha = `url(#${svg.querySelector('marker').id})`;
const capaAristas = svg.querySelector('.aristas'), capaNodos = svg.querySelector('.nodos');
const lista = raiz.querySelector('.lista'), tip = raiz.querySelector('.tip');
const medidas = Object.fromEntries(datos.metricas.filter(m => m.disponible).map(m => [m.clave, m]));
let actual = datos.metrica, seleccionado = null;

const fmt = (v, m) => v == null ? 's/d' : v === 0 ? '0' : m && m.entero ? String(Math.round(v)) : v.toPrecision(4);
const valor = n => actual && n.valores[actual] != null ? n.valores[actual] : null;
const corto = m => m ? m.corto : datos.corto;
const mayuscula = s => s.charAt(0).toUpperCase() + s.slice(1);

const porId = new Map();
for (const n of datos.nodos){
  const g = document.createElementNS(NS, 'g'), c = document.createElementNS(NS, 'circle'), t = document.createElementNS(NS, 'text');
  g.setAttribute('class', 'nodo'); g.setAttribute('tabindex', '0'); g.setAttribute('role', 'button');
  g.setAttribute('aria-pressed', 'false'); g.setAttribute('transform', `translate(${n.x} ${n.y})`);
  g.dataset.k = n.id; g.append(c, t); capaNodos.append(g);
  let aro = null;
  if (n.libro){  // los libros de la red mixta llevan un anillo punteado exterior
    aro = document.createElementNS(NS, 'circle'); aro.setAttribute('class', 'aro-libro'); g.prepend(aro);
  }
  const li = crear('li'), b = crear('button'), num = crear('span', 'num'), cuerpo = crear('span', 'cuerpo');
  const titulo = crear('span'), meta = crear('small'), pista = crear('span', 'pista'), relleno = crear('span', 'relleno');
  b.type = 'button'; b.dataset.k = n.id; b.setAttribute('aria-pressed', 'false'); titulo.textContent = n.titulo;
  pista.append(relleno); cuerpo.append(titulo, meta, pista); b.append(num, cuerpo); li.append(b);
  porId.set(n.id, {n, g, c, t, li, b, num, meta, relleno, aro, r: 9});
}
const pares = new Set(datos.aristas.map(([de, a]) => de + '>' + a));
const pesoMaximo = datos.aristas.reduce((x, [, , peso]) => Math.max(x, peso ?? 1), 1);
const aristas = datos.aristas.map(([de, a, peso]) => {
  const p = document.createElementNS(NS, 'path');
  p.setAttribute('class', 'enlace');
  if (!datos.sinFlechas) p.setAttribute('marker-end', flecha);
  p.dataset.de = de; p.dataset.a = a;
  if (peso != null){
    // El grosor crece con el número de citas que resume la arista.
    p.style.strokeWidth = (1.2 + 2.8 * Math.sqrt(peso / pesoMaximo)).toFixed(2);
    const t = document.createElementNS(NS, 'title');
    t.textContent = `${porId.get(de).n.titulo} ${datos.sinFlechas ? '—' : '→'} ${porId.get(a).n.titulo}: `
      + `${peso} ${datos.unidad[peso === 1 ? 0 : 1]}`;
    p.append(t);
  }
  capaAristas.append(p);
  return {de, a, p};
});

function trazar({de, a, p}){
  const o = porId.get(de), d = porId.get(a);
  trazarCurva(p, {x: o.n.x, y: o.n.y, r: o.r}, {x: d.n.x, y: d.n.y, r: d.r}, pares.has(a + '>' + de));
}

function dibujar(){
  const m = medidas[actual], maximo = m ? m.maximo : 0;
  const orden = [...datos.nodos].sort((p, q) => ((valor(q) ?? -1) - (valor(p) ?? -1))
    || p.titulo.localeCompare(q.titulo, 'es', {sensitivity: 'base'}) || (p.id < q.id ? -1 : 1));
  orden.forEach((n, i) => {
    const e = porId.get(n.id), x = valor(n), t = maximo && x > 0 ? x / maximo : 0;
    const color = m && x > 0 ? tono(t) : null;
    e.r = !m ? 9 : x > 0 ? 8 + 10 * Math.sqrt(t) : 8;
    e.c.setAttribute('r', e.r.toFixed(2));
    if (e.aro) e.aro.setAttribute('r', (e.r + 3.5).toFixed(2));
    e.c.style.fill = color || 'var(--cero)';
    e.g.classList.toggle('nulo', !!m && !(x > 0));
    e.t.textContent = e.num.textContent = i + 1;
    e.t.style.fontSize = i + 1 > 99 ? '8px' : '';
    e.t.style.fill = e.num.style.color = color ? tintaSobre(color) : 'var(--tinta)';
    e.num.style.background = color || 'var(--cero)';
    // Un dato fijo de la línea se omite cuando repite la métrica elegida.
    const repetido = (n.repite || {})[actual];
    e.meta.textContent = [repetido === 'sub' ? '' : n.sub, `${corto(m)} ${fmt(x, m)}`, repetido === 'pie' ? '' : n.pie]
      .filter(Boolean).join(' · ');
    e.relleno.style.width = (t * 100).toFixed(1) + '%';
    e.g.setAttribute('aria-label', `${i + 1}. ${n.titulo}. ${mayuscula(corto(m))} ${fmt(x, m)}.`);
    lista.append(e.li);
  });
  for (const arista of aristas) trazar(arista);
  raiz.querySelector('.leyenda').hidden = !m;
  raiz.querySelector('.gradiente').style.background = `linear-gradient(90deg,${rampa().join(',')})`;
  raiz.querySelector('.maximo').textContent = fmt(maximo, m);
  raiz.querySelector('.nota').textContent = m
    ? `${m.nombre}${m.radio != null ? ': radio espectral ' + fmt(m.radio) : ''}. ${m.nota} Color y área son proporcionales al valor respecto del máximo del corpus.`
    : datos.sinMedida;
  aplicarSeleccion();
}

function aplicarSeleccion(){
  const cerca = new Set([seleccionado]);
  for (const {de, a, p} of aristas){
    const incidente = de === seleccionado || a === seleccionado;
    p.classList.toggle('activo', incidente);
    p.classList.toggle('apagado', !!seleccionado && !incidente);
    if (incidente){ cerca.add(de); cerca.add(a); }
  }
  for (const [k, e] of porId){
    e.g.classList.toggle('apagado', !!seleccionado && !cerca.has(k));
    e.g.classList.toggle('seleccion', k === seleccionado);
    e.g.setAttribute('aria-pressed', String(k === seleccionado));
    e.b.setAttribute('aria-pressed', String(k === seleccionado));
  }
}
function seleccionar(k){ seleccionado = seleccionado === k ? null : k; aplicarSeleccion(); }

function mostrar(e){
  const {n} = e, renglon = texto => { const s = crear('span'); s.textContent = texto; return s; };
  const titulo = crear('strong'); titulo.textContent = n.titulo;
  tip.replaceChildren(titulo, ...n.antes.map(renglon),
    ...datos.metricas.filter(m => m.disponible).map(m =>
      renglon(`${m.clave === actual ? '▸ ' : ''}${m.nombre}: ${fmt(n.valores[m.clave], m)}`)),
    ...n.despues.map(renglon));
  tip.hidden = false;
  situar(tip, lienzo, e.c);
}
const ocultar = () => { tip.hidden = true; };
const botonEscala = raiz.querySelector('.escala');
if (botonEscala) botonEscala.addEventListener('click', () => {
  const real = lienzo.classList.toggle('real');
  botonEscala.setAttribute('aria-pressed', String(real));
  botonEscala.textContent = real ? 'Ajustar al panel' : 'Tamaño real';
  ocultar();
});

for (const e of porId.values()){
  e.g.addEventListener('click', () => seleccionar(e.n.id));
  e.b.addEventListener('click', () => {
    seleccionar(e.n.id);
    if (lienzo.classList.contains('real')) e.g.scrollIntoView({block: 'nearest', inline: 'center'});
  });
  e.g.addEventListener('pointerenter', () => mostrar(e));
  e.g.addEventListener('pointerleave', ocultar);
  e.g.addEventListener('focus', () => mostrar(e));
  e.g.addEventListener('blur', ocultar);
  e.b.addEventListener('pointerenter', () => mostrar(e));
  e.b.addEventListener('pointerleave', ocultar);
  e.g.addEventListener('keydown', ev => {
    if (ev.key === 'Enter' || ev.key === ' '){ ev.preventDefault(); seleccionar(e.n.id); }
  });
}
document.addEventListener('keydown', ev => { if (ev.key === 'Escape'){ seleccionado = null; aplicarSeleccion(); ocultar(); } });
for (const r of raiz.querySelectorAll('.herramientas input[type=radio]')){
  r.addEventListener('change', () => { if (r.checked){ actual = r.value; dibujar(); } });
}
temaOscuro.addEventListener('change', dibujar);
dibujar();
}
"""
NOMBRES_MEDIDA = {"dirigido": "Dirigida (prestigio por citas recibidas)", "simetrico": "Sin orientación"}
VISTAS = (("articulos", "Red de artículos"), ("autores", "Autores"), ("clusters", "Clusters y subtemas"),
          ("libros", "Libros OUP/CUP"))


def radio_nodo(valor, maximo):
    """Área del círculo creciente con la centralidad; 8 px para ceros."""
    return 8 + 10 * math.sqrt(valor / maximo) if valor and maximo else 8


def repartir(pesos, inferior, tam):
    """Divide un rectángulo en piezas de área proporcional a los pesos, cortando por el lado más largo."""
    if len(pesos) == 1:
        return [(inferior, tam)]
    acumulado = np.cumsum(pesos)
    corte = int(np.argmin(np.abs(acumulado[:-1] - acumulado[-1] / 2))) + 1
    eje = tam.argmax()
    primera = tam.copy()
    primera[eje] *= acumulado[corte - 1] / acumulado[-1]
    desplazar = np.zeros(2)
    desplazar[eje] = primera[eje]
    return repartir(pesos[:corte], inferior, primera) + repartir(pesos[corte:], inferior + desplazar, tam - desplazar)


def posiciones_vista(sub, radios, ancho, alto):
    """Reserva sitio para cada componente; separa los círculos en píxeles."""
    red = sub.to_undirected()
    grupos = sorted((sorted(c) for c in nx.connected_components(red)), key=lambda c: (-len(c), c[0]))
    puntos = {}

    def colocar(grupo, inferior, tam):
        centro = inferior + tam / 2
        if len(grupo) == 1:
            puntos[grupo[0]] = centro
            return
        if len(grupo) == 2:
            delta = np.zeros(2)
            eje = tam.argmax()
            delta[eje] = min(38, tam[eje] / 4)
            puntos[grupo[0]], puntos[grupo[1]] = centro - delta, centro + delta
            return
        grande = len(grupo) > 300  # Kamada-Kawai crece con el cuadrado de los nodos
        # Kamada-Kawai lee el peso como distancia: los vínculos fuertes quedarían lejos. El trazado
        # usa sólo la topología; el grosor de la arista ya muestra el peso.
        pos = (nx.spring_layout(red.subgraph(grupo), seed=SEMILLA) if grande
               else nx.kamada_kawai_layout(red.subgraph(grupo), weight=None))
        xy = np.array([pos[w] for w in grupo])
        xy -= (xy.min(axis=0) + xy.max(axis=0)) / 2
        xy = centro + xy * (np.maximum(tam - 44, 1) / np.maximum(np.ptp(xy, axis=0), 1e-8))
        r = np.array([radios[w] for w in grupo])
        for _ in range(30 if grande else 80):
            falta = 0.
            for i in range(len(grupo) - 1):
                dist = xy[i + 1:] - xy[i]
                d = np.linalg.norm(dist, axis=1)
                coinciden = d < 1e-8
                if np.any(coinciden):
                    dist[coinciden], d[coinciden] = [1, 0], 1
                ajuste = np.maximum(0., r[i] + r[i + 1:] + 7 - d)
                falta = max(falta, ajuste.max())
                mover = (ajuste / (2 * d))[:, None] * dist
                xy[i] -= mover.sum(axis=0)
                xy[i + 1:] += mover
            xy = np.clip(xy, inferior + r[:, None] + 2, inferior + tam - r[:, None] - 2)
            if falta < .1:
                break
        puntos.update(zip(grupo, xy))

    if grupos:
        rectangulos = repartir([len(c) + 2 for c in grupos], np.array([8., 8.]), np.array([ancho - 16., alto - 16.]))
        for grupo, (inferior, tam) in zip(grupos, rectangulos):
            colocar(grupo, inferior, tam)
    return puntos


def descargas(archivos):
    """Enlaces a los archivos de datos de una pestaña."""
    return '<p class="descargas">' + " · ".join(f'<a href="{p}">{html.escape(t)}</a>' for p, t in archivos) + "</p>"


def marcador(clave):
    """Punta de flecha de una pestaña; cada una tiene la suya porque los id no pueden repetirse."""
    return (f'<marker id="flecha-{clave}" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" '
            'orient="auto" markerUnits="userSpaceOnUse"><path d="M0 0L8 4L0 8z"/></marker>')


def datos_json(datos):
    # "<" se escapa para que el JSON no pueda cerrar la etiqueta <script>.
    carga = json.dumps(datos, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c")
    return f'<script type="application/json" class="datos">{carga}</script>'


def panel_red(clave, datos, *, intro, seccion, selector, aria, vacio, ayuda, titulo_lista, ancho, alto, escala):
    """Pestaña con una red en SVG, selector de métrica y lista ordenada (artículos y autores)."""
    opciones, avisos = [], []
    for m in datos["metricas"]:
        marcado = " checked" if m["clave"] == datos["metrica"] else ""
        apagado = "" if m["disponible"] else " disabled"
        opciones.append(f'<label class="opcion"><input type="radio" name="metrica-{clave}" value="{m["clave"]}"'
                        f'{marcado}{apagado}>{html.escape(m["nombre"])}</label>')
        if not m["disponible"]:
            avisos.append(f'<p class="aviso">{html.escape(m["nombre"])} no disponible: {html.escape(m["nota"])}</p>')
    sin_nodos = ('' if datos["nodos"] else f'<text x="{ancho / 2:g}" y="{alto / 2:g}" text-anchor="middle" '
                 f'fill="currentColor">{html.escape(vacio)}</text>')
    return "\n".join([
        *intro, f'<div class="rejilla"><section><h2>{html.escape(seccion)}</h2>',
        f'<div class="herramientas"><fieldset><legend>{html.escape(selector)}</legend>', *opciones, '</fieldset>',
        '<div class="leyenda"><span class="cero" title="Valor cero"></span><span>0</span>'
        '<span class="gradiente"></span><span class="maximo"></span></div>',
        # Con muchos nodos el lienzo supera el panel: se puede ver a escala nativa.
        '<button type="button" class="escala" aria-pressed="false">Tamaño real</button>' if escala > 1 else '',
        '</div>', '<p class="nota"></p>', *avisos,
        f'<div class="lienzo" style="--ancho:{ancho}px"><svg class="red" viewBox="0 0 {ancho} {alto}" role="img" '
        f'aria-label="{html.escape(aria)}"><defs>{marcador(clave)}</defs>'
        f'<g class="aristas"></g><g class="nodos"></g>{sin_nodos}</svg>'
        '<div class="tip" role="tooltip" hidden></div></div>',
        f'<p class="ayuda">{ayuda}</p></section>',
        f'<section><h2>{html.escape(titulo_lista)}</h2><ol class="lista"></ol></section></div>',
        datos_json(datos)])


def clave_panel(base, version):
    """Clave única de un subpanel: los id y los nombres de los controles no pueden repetirse."""
    return base if version == "articulos" else f"{base}-{version}"


TEXTOS_RED = {  # sección, aria, vacío, explicación de las aristas, título de la lista
    "articulos": ("Centralidad de vector propio en la red de citas",
                  "Grafo dirigido de citas coloreado por centralidad de vector propio",
                  "No hay citas entre los artículos recuperados.",
                  "Cada flecha va del artículo que cita al citado.", "Artículos de la figura, por centralidad"),
    "libros": ("Centralidad de vector propio en la red de libros",
               "Red sin orientación de libros de OUP y CUP coloreada por centralidad de vector propio",
               "Ningún par de libros se cita junto ni se cita entre sí.",
               "Dos libros se enlazan cuando un mismo artículo del corpus cita a ambos o cuando uno cita al otro; "
               "el grosor crece con el número de vínculos.", "Libros de la figura, por centralidad"),
    "mixta": ("Centralidad de vector propio en la red de artículos y libros",
              "Grafo dirigido de citas entre artículos y libros coloreado por centralidad de vector propio",
              "No hay citas entre las obras recuperadas.",
              "Cada flecha va de la obra que cita a la citada. Los libros llevan un anillo punteado exterior.",
              "Obras de la figura, por centralidad"),
}


def panel_articulos(g, orden, info, max_nodos, version="articulos"):
    """Pestaña «Red» de una versión. La figura dibuja hasta max_nodos obras con enlaces (0 = todas); el
    ranking y el GraphML usan toda la red. El lienzo crece con el número de nodos."""
    sin_orientacion = g.graph.get("simetrico", False)
    conectados = [w for w in orden if g.degree(w)]
    vertices = conectados[:max_nodos] if max_nodos else conectados
    escala = max(1.0, math.sqrt(len(vertices) / NODOS_VISTA))
    ancho, alto = round(730 * escala), round(510 * escala)
    sub = g.subgraph(vertices).copy()
    metrica, medidas = info["metrica_ranking"], info["medidas"]
    disponibles = [k for k in ("dirigido", "simetrico")
                   if (medidas.get(k) or {}).get("articulos_positivos") is not None]
    # La escala se fija con el máximo de la red, no sólo con el de la figura.
    maximos = {k: max((g.nodes[w].get("vector_propio_" + k, 0) for w in g), default=0) for k in disponibles}
    radios = {w: max((radio_nodo(g.nodes[w].get("vector_propio_" + k, 0), maximos[k]) for k in disponibles),
                     default=9) for w in vertices}
    puntos = posiciones_vista(sub, radios, ancho, alto)
    nodos = []
    for w in vertices:
        d = g.nodes[w]
        autores = [a for a in d.get("autores", "").split("; ") if a]
        autoria = "; ".join(autores[:3]) + (" et al." if len(autores) > 3 else "")
        fecha = str(d["anio"]) if d.get("anio") is not None else "s/f"
        libro = d.get("tipo_obra") == "libro"
        if sin_orientacion:
            citan = d.get("citas_corpus", 0)
            pie = f"{plural(citan, 'artículo del corpus lo cita', 'artículos del corpus lo citan')}"
            despues = [f"{pie} · enlazado con {plural(g.out_degree(w), 'libro', 'libros')} de la red",
                       *([f"Cita {plural(d['cita_libros'], 'libro', 'libros')} de la red"] if d.get("cita_libros") else [])]
        else:
            recibidas, emitidas = g.in_degree(w), g.out_degree(w)
            pie = f"{recibidas} citas internas recibidas"
            despues = [f"{recibidas} citas recibidas · {emitidas} referencias dentro del corpus",
                       *([f"Subáreas: {d['subareas']}"] if d.get("subareas") else []),
                       *([f"Fuentes: {d['fuentes']}"] if d.get("fuentes") else [])]
        nodo = {"id": w, "titulo": d["titulo"],
                "x": round(float(puntos[w][0]), 2), "y": round(float(puntos[w][1]), 2),
                "sub": f"Libro · {fecha}" if libro and version == "mixta" else fecha, "pie": pie,
                "antes": [fecha + (f" · {autoria}" if autoria else ""),
                          *([f"Libro · {d['editorial']}"] if libro and d.get("editorial") else [])],
                "despues": despues, "valores": {k: d.get("vector_propio_" + k) for k in disponibles}}
        if libro and version == "mixta":
            nodo["libro"] = True
        nodos.append(nodo)
    inicial = metrica if metrica in disponibles else (disponibles[0] if disponibles else None)
    if sin_orientacion:  # una arista por par, con su peso y sin flecha
        aristas = [[u, v, d["weight"]] for u, v, d in sorted(sub.edges(data=True)) if u < v]
    else:
        aristas = [list(e) for e in sorted(sub.edges())]
    datos = {"metrica": inicial, "corto": "centralidad",
             "unidad": ["vínculo", "vínculos"] if sin_orientacion else ["cita", "citas"],
             "sinMedida": "No hay una medida de centralidad disponible: los nodos se muestran sin escala.",
             "nodos": nodos, "aristas": aristas,
             "metricas": [{"clave": k, "nombre": NOMBRES_MEDIDA[k], "corto": "centralidad",
                           "disponible": k in disponibles, "radio": medidas[k]["radio_espectral"],
                           "nota": medidas[k]["nota"], "maximo": maximos.get(k, 0)}
                          for k in ("dirigido", "simetrico") if k in medidas]}
    if sin_orientacion:
        datos["sinFlechas"] = True
    seccion, aria, vacio, enlaces, titulo_lista = TEXTOS_RED[version]
    unidad = UNIDADES[version]
    recorte = (f' Se muestran los {len(vertices)} primeros de {len(conectados)} {unidad[1]} con enlaces según la '
               'métrica del ranking; cambia el número con --nodos.' if len(vertices) < len(conectados) else '')
    sufijo = SUFIJOS[version]
    archivos = [(f"ranking{sufijo}.csv", "Ranking completo (CSV)"), (f"grafo{sufijo}.graphml", "Grafo completo (GraphML)")]
    if version == "articulos":
        archivos.append(("citas.csv", "Pares de artículos que se citan (CSV)"))
    n_aristas = sub.number_of_edges() // (2 if sin_orientacion else 1)
    vinculo = ("pares enlazados" if sin_orientacion else "citas entre ellos")
    return panel_red(clave_panel("articulos", version), datos, intro=[descargas(archivos)], seccion=seccion,
                     selector="Vector propio", aria=aria, vacio=vacio,
                     ayuda=(f'Vista de {len(vertices)} {unidad[1]} con enlaces y {n_aristas} {vinculo}.{recorte} '
                            f'{enlaces} El color sigue la escala de la leyenda y el área del círculo crece con la '
                            'centralidad; los círculos grises punteados tienen centralidad cero. Pasa el cursor para '
                            f'ver los valores y selecciona un{"" if version == "articulos" else "a"} '
                            f'{"artículo" if version == "articulos" else "obra"} para resaltar sus conexiones '
                            'visibles (Esc para limpiar).'),
                     titulo_lista=titulo_lista, ancho=ancho, alto=alto, escala=escala)


def recortar(texto_largo, maximo):
    return texto_largo if len(texto_largo) <= maximo else texto_largo[:maximo - 1].rstrip() + "…"


def plural(n, singular, varios):
    return f"{n} {singular if n == 1 else varios}"


# Nombres y notas de las métricas de autor que cambian en las versiones con libros.
TEXTOS_AUTOR = {
    "libros": {
        "pagerank": ("PageRank en la red de autores de libros", "PageRank",
                     "Centralidad en la red de autores cuyos libros se citan juntos o se citan entre sí. No cuentan "
                     "los pares de libros con un autor común."),
        "citas_internas": ("Artículos que citan sus libros", "citas del corpus",
                           "Artículos del corpus que citan algún libro del autor, sin descontar autocitas."),
        "h_interno": ("Índice h por citas del corpus", "índice h",
                      "h libros del autor citados por al menos h artículos del corpus cada uno."),
        "articulos": ("Libros citados por el corpus", "libros", "Libros del autor, como autor o editor."),
        "centralidad_acumulada": ("Centralidad acumulada", "centralidad acumulada",
                                  "Suma de la centralidad de sus libros en la red de libros."),
        "citas_openalex": ("Citas globales (Crossref)", "citas globales",
                           "Suma del contador global de citas de Crossref de sus libros."),
    },
    "mixta": {
        "citas_internas": ("Citas internas recibidas", "citas internas",
                           "Obras de la red, artículos o libros, que citan alguna obra del autor, sin autocitas."),
        "articulos": ("Obras en la red", "obras", "Artículos y libros del autor en la red."),
        "centralidad_acumulada": ("Centralidad acumulada", "centralidad acumulada",
                                  "Suma de la centralidad de sus obras en la red de artículos y libros."),
        "citas_openalex": ("Citas globales", "citas globales",
                           "Contador global de citas: OpenAlex para los artículos y Crossref para los libros."),
    },
}


def panel_autores(g, res, max_autores, version="articulos"):
    filas, red = res["filas"], res["red"]
    sin_orientacion = g.graph.get("simetrico", False)
    por_clave = {f["clave"]: f for f in filas}
    # Con --autores N se dibujan los N primeros del ranking, tengan o no vínculos entre sí;
    # con 0, todos los que tienen alguno con otro autor.
    elegidos = ([f["clave"] for f in filas[:max_autores]] if max_autores
                else [f["clave"] for f in filas if red.degree(f["clave"])])
    escala = max(1.0, math.sqrt(len(elegidos) / NODOS_VISTA))
    ancho, alto = round(730 * escala), round(510 * escala)
    sub = red.subgraph(elegidos).copy()
    motivos = {"pagerank": "no hay citas entre autores distintos dentro del corpus.",
               "centralidad_acumulada": "no hay ranking de centralidad: el corpus carece de citas internas útiles."}
    disponibles = [k for k, *_ in METRICAS_AUTOR if res["disponibles"].get(k, True)]
    maximos = {k: max((f[k] for f in filas), default=0) for k in disponibles}
    radios = {c: max((radio_nodo(por_clave[c][k], maximos[k]) for k in disponibles), default=9) for c in elegidos}
    puntos = posiciones_vista(sub, radios, ancho, alto)
    unidad = UNIDADES[version]
    nodos = []
    for c in elegidos:
        f = por_clave[c]
        identidad = (f"OpenAlex {f['id_openalex']}" if f["id_openalex"]
                     else "Sin identificador de OpenAlex: agrupado por el nombre")
        if sin_orientacion:
            pie = plural(f["citas_internas"], "artículo del corpus cita sus libros",
                         "artículos del corpus citan sus libros")
            vinculos = (f"{plural(f['coautores'], 'coautor', 'coautores')} en los libros · "
                        f"{plural(f['citas_emitidas'], 'vínculo', 'vínculos')} con otros autores")
        else:
            pie = plural(f["citas_internas"], "cita interna recibida", "citas internas recibidas")
            vinculos = (f"{plural(f['coautores'], 'coautor', 'coautores')} en el corpus · "
                        f"{plural(f['citas_emitidas'], 'cita', 'citas')} a otros autores")
        nodos.append({"id": c, "titulo": f["autor"],
                      "x": round(float(puntos[c][0]), 2), "y": round(float(puntos[c][1]), 2),
                      "sub": plural(f["articulos"], *unidad), "pie": pie,
                      "repite": {"articulos": "sub", "citas_internas": "pie"},
                      "antes": [identidad, *([f"También como: {f['variantes_nombre']}"] if f["variantes_nombre"] else [])],
                      "despues": [vinculos, *(f"• {recortar(g.nodes[k]['titulo'], 90)}" for k in f["articulos_ids"][:3])],
                      "valores": {k: f[k] for k in disponibles}})
    resumen = res["resumen"]
    inicial = resumen["metrica_ranking"] if resumen["metrica_ranking"] in disponibles else disponibles[0]
    textos = TEXTOS_AUTOR.get(version, {})
    metricas = []
    for k, nombre, corto, entero, nota in METRICAS_AUTOR:
        nombre, corto, nota = textos.get(k, (nombre, corto, nota))
        metricas.append({"clave": k, "nombre": nombre, "corto": corto, "entero": entero,
                         "nota": nota if k in disponibles else motivos.get(k, nota),
                         "disponible": k in disponibles, "maximo": maximos.get(k, 0)})
    aristas = [[u, v, d["weight"]] for u, v, d in sorted(sub.edges(data=True)) if not sin_orientacion or u < v]
    datos = {"metrica": inicial, "corto": "valor",
             "unidad": ["vínculo", "vínculos"] if sin_orientacion else ["cita", "citas"],
             "sinMedida": "No hay una métrica disponible: los nodos se muestran sin escala.",
             "nodos": nodos, "aristas": aristas, "metricas": metricas}
    if sin_orientacion:
        datos["sinFlechas"] = True
    recorte = (f" (de {len(filas)} autores; cambia el número con --autores)"
               if max_autores and len(filas) > max_autores else "")
    identificados = (f'{resumen["con_id_openalex"]} autores identificados por OpenAlex y {resumen["solo_nombre"]} '
                     'sólo por su nombre')
    if version == "libros":
        intro_texto = (f'{resumen["autores"]} autores y editores de los {len(g)} libros · '
                       f'{resumen["citas_entre_autores"]} vínculos entre autores distintos · '
                       f'{resumen["autocitas_excluidas"]} pares de libros con un autor común excluidos. Crossref sólo '
                       'da nombres: los autores de libros se identifican por el nombre.')
        seccion, aria = "Red de autores de libros", "Red sin orientación de autores de libros coloreada por la métrica elegida"
        enlaces = ('Dos autores se enlazan cuando sus libros se citan juntos en un artículo del corpus o uno cita '
                   'al otro; el grosor crece con el número de vínculos.')
    else:
        obras = "artículos" if version == "articulos" else "artículos y libros"
        intro_texto = (f'{resumen["autores"]} autores firman los {len(g)} {obras} · {resumen["citas_entre_autores"]} '
                       f'citas entre autores distintos · {resumen["autocitas_excluidas"]} autocitas excluidas. '
                       f'{identificados}.' + (' Un autor de libro se une a su perfil de OpenAlex cuando su nombre '
                                              'coincide con uno solo.' if version == "mixta" else ''))
        seccion = "Red de citas entre autores" + ("" if version == "articulos" else " de artículos y libros")
        aria = "Grafo dirigido de citas entre autores coloreado por la métrica elegida"
        enlaces = ('Cada flecha va del autor que cita al citado y su grosor crece con el número de citas; una cita '
                   f'entre {"artículos" if version == "articulos" else "obras"} que comparten un autor no cuenta.')
    intro = [f'<p>{intro_texto} {html.escape(resumen["nota"])}</p>',
             descargas([(f"autores{SUFIJOS[version]}.csv", "Tabla completa de autores (CSV)")])]
    return panel_red(clave_panel("autores", version), datos, intro=intro, seccion=seccion, selector="Métrica",
                     aria=aria, vacio="No hay autores para mostrar.",
                     ayuda=(f'Vista de los {len(elegidos)} primeros autores del ranking{recorte}. {enlaces} El color y '
                            'el área del círculo siguen la métrica elegida; los círculos grises punteados tienen valor '
                            'cero. Pasa el cursor para ver los valores y selecciona un autor para resaltar sus '
                            'conexiones visibles (Esc para limpiar).'),
                     titulo_lista="Autores de la figura, por la métrica elegida", ancho=ancho, alto=alto,
                     escala=escala)


ESTILO_CLUSTERS = """
:root{--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--s5:#e87ba4;--s6:#008300;--s7:#6250d6;--s8:#e34948}
@media(prefers-color-scheme:dark){:root{--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;
--s6:#0ca30c;--s7:#9085e9;--s8:#e66767}}
.caja rect{fill-opacity:.07;stroke-opacity:.6;stroke-width:1.2;cursor:pointer}
.caja text{font:600 15px system-ui,sans-serif;fill:var(--tinta);cursor:pointer}
.caja text.detalle{font:400 13.5px system-ui,sans-serif;fill:var(--tinta2)}
.enlace.cruza{stroke-dasharray:4 3}.nodo circle{stroke-width:1.5}
.vacio{position:absolute;inset:40% 0 auto;text-align:center}
.fichas{max-height:900px}.ficha{border-top:1px solid var(--linea);padding:10px 14px}
.ficha .cabeza{display:block;width:100%;text-align:left;border:0;background:none;color:inherit;font:inherit;
padding:4px 6px;margin:-4px -6px 2px;border-radius:6px}
button.cabeza{cursor:pointer}button.cabeza:hover,.cabeza[aria-pressed=true]{background:var(--resalte)}
.muestra{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:8px;vertical-align:-1px}
.terminos{display:block;color:var(--tinta2);font-size:14px;margin-top:2px}
.detalle p{margin:.3rem 0;font-size:13px}.detalle b{font-weight:600;color:var(--tinta)}
ol.claves{list-style:decimal;padding-left:20px;margin:.3rem 0;max-height:none;overflow:visible;font-size:13px}
.claves li{margin:.15rem 0;color:var(--tinta2)}.vacia{padding:12px 16px;color:var(--tinta2)}
.ancho{grid-column:1/-1}.tabla{overflow:auto;padding:0 16px 14px}
table{border-collapse:collapse;font-size:14px;font-variant-numeric:tabular-nums;min-width:100%}
th,td{padding:6px 10px;border-top:1px solid var(--linea);text-align:right;white-space:nowrap}
th:first-child{text-align:left}thead th{border-top:0;color:var(--tinta2);font-weight:600;white-space:normal}
td[colspan]{text-align:left;white-space:normal;color:var(--tenue)}
.matriz{min-width:0}.matriz td{min-width:56px;text-align:center}.matriz thead th{text-align:center}
.izquierda{text-align:left}
.versiones{margin:14px 0 4px}.aro-libro{fill:none;stroke:var(--tinta2);stroke-width:1.4;stroke-dasharray:3 2;
pointer-events:none}
table.libros td{white-space:normal;vertical-align:top}table.libros small{font-size:13px}
td.celda-barra{text-align:left;min-width:170px}.celda-barra>span:first-child{display:inline-block;min-width:2.5em}
.celda-barra .pista{display:inline-block;width:120px;vertical-align:middle;margin:0}
"""
# Pestaña «Clusters y subtemas».
JS_CLUSTERS = r"""
function iniciarClusters(raiz){
const datos = JSON.parse(raiz.querySelector('script.datos').textContent);
const NS = 'http://www.w3.org/2000/svg';
const svg = raiz.querySelector('svg.red'), lienzo = raiz.querySelector('.lienzo');
const flecha = `url(#${svg.querySelector('marker').id})`;
const capaCajas = svg.querySelector('.cajas'), capaAristas = svg.querySelector('.aristas'), capaNodos = svg.querySelector('.nodos');
const listaFichas = raiz.querySelector('.fichas'), tip = raiz.querySelector('.tip'), vacio = raiz.querySelector('.vacio');
const metodos = Object.fromEntries(datos.metodos.map(m => [m.clave, m]));
const pares = new Set(datos.aristas.map(([de, a]) => de + '>' + a));
let actual = datos.inicial, foco = null, seleccionado = null;
let nodos = new Map(), aristas = [], cajas = new Map(), botones = new Map();

const pct = v => v == null ? 's/d' : Math.round(v * 100) + ' %';
// Los clusters 1-8 llevan los colores de la paleta; el recuadro y su rótulo los identifican.
const color = c => c <= datos.colores ? `var(--s${c})` : 'var(--cero)';
const elemento = (etiqueta, clase) => { const e = document.createElementNS(NS, etiqueta); if (clase) e.setAttribute('class', clase); return e; };

function dibujarRed(m){
  capaCajas.replaceChildren(); capaAristas.replaceChildren(); capaNodos.replaceChildren();
  nodos = new Map(); aristas = []; cajas = new Map();
  svg.setAttribute('viewBox', `0 0 ${m.ancho} ${m.alto}`);
  vacio.hidden = m.nodos.length > 0;
  vacio.textContent = m.disponible ? 'Este método no formó grupos del tamaño mínimo.' : 'Método no disponible.';
  for (const k of m.cajas){
    const g = elemento('g', 'caja'), r = elemento('rect'), t = elemento('text'), d = elemento('text', 'detalle');
    for (const [a, v] of [['x', k.x], ['y', k.y], ['width', k.w], ['height', k.h], ['rx', 6]]) r.setAttribute(a, v);
    r.style.fill = r.style.stroke = color(k.c);
    t.setAttribute('x', k.x + 8); t.setAttribute('y', k.y + 18); t.textContent = k.etiqueta;
    d.setAttribute('x', k.x + 8); d.setAttribute('y', k.y + 36); d.textContent = k.detalle;
    g.append(r, t, d); capaCajas.append(g); cajas.set(k.c, g);
    g.addEventListener('click', () => enfocar(k.c));
  }
  for (const [id, x, y, c] of m.nodos){
    const info = datos.articulos[id], g = elemento('g', 'nodo'), circulo = elemento('circle');
    g.setAttribute('tabindex', '0'); g.setAttribute('role', 'button'); g.setAttribute('aria-pressed', 'false');
    g.setAttribute('transform', `translate(${x} ${y})`);
    g.setAttribute('aria-label', `${info.t}. Cluster C${c}.`);
    circulo.setAttribute('r', info.r); circulo.style.fill = color(c);
    g.append(circulo); capaNodos.append(g);
    const e = {id, x, y, c, r: info.r, g, circulo};
    nodos.set(id, e);
    g.addEventListener('click', () => seleccionar(id));
    g.addEventListener('keydown', ev => { if (ev.key === 'Enter' || ev.key === ' '){ ev.preventDefault(); seleccionar(id); } });
    g.addEventListener('pointerenter', () => mostrar(e)); g.addEventListener('pointerleave', ocultar);
    g.addEventListener('focus', () => mostrar(e)); g.addEventListener('blur', ocultar);
  }
  for (const [de, a] of datos.aristas){
    const o = nodos.get(de), d = nodos.get(a);
    if (!o || !d) continue;
    const p = elemento('path', 'enlace' + (o.c !== d.c ? ' cruza' : ''));
    if (!datos.sinFlechas) p.setAttribute('marker-end', flecha);
    trazarCurva(p, o, d, pares.has(a + '>' + de));
    capaAristas.append(p);
    aristas.push({de, a, p, o, d});
  }
}

function dibujarFichas(m){
  listaFichas.replaceChildren(); botones = new Map();
  if (!m.fichas.length){
    const li = crear('li', 'vacia');
    li.textContent = m.disponible ? 'Este método no formó grupos del tamaño mínimo.' : 'Método no disponible: ' + m.nota;
    listaFichas.append(li); return;
  }
  const dibujados = new Set(m.cajas.map(k => k.c));
  for (const f of m.fichas){
    const li = crear('li', 'ficha'), cabeza = crear(dibujados.has(f.c) ? 'button' : 'div', 'cabeza');
    const muestra = crear('span', 'muestra'), nombre = crear('strong'), meta = crear('span'), terminos = crear('span', 'terminos');
    muestra.style.background = color(f.c);
    nombre.textContent = `C${f.c}`;
    meta.textContent = ` · ${f.n} ${datos.unidad[f.n === 1 ? 0 : 1]} · ${f.anios}`;
    terminos.textContent = f.terminos.length ? f.terminos.join(' · ') : 'Sin términos distintivos en los títulos.';
    cabeza.append(muestra, nombre, meta, terminos);
    if (dibujados.has(f.c)){
      cabeza.type = 'button'; cabeza.setAttribute('aria-pressed', 'false');
      cabeza.addEventListener('click', () => enfocar(f.c));
      botones.set(f.c, cabeza);
    }
    const detalle = crear('div', 'detalle');
    const linea = (etiqueta, texto) => {
      if (!texto) return;
      const p = crear('p'), b = crear('b'); b.textContent = etiqueta + ': '; p.append(b, texto); detalle.append(p);
    };
    linea('Temas', f.temas.map(([t, p]) => `${t} (${pct(p)})`).join('; '));
    linea('Subáreas', f.subareas.map(([t, p]) => `${t} (${pct(p)})`).join('; '));
    linea('Autores', f.autores.map(([a, n]) => `${a} (${n})`).join('; '));
    linea('Libros OUP/CUP', (f.libros || []).map(([t, n]) => `${t} (${n})`).join('; '));
    linea('Citas', `${f.citas_dentro} dentro del grupo · ${f.citas_hacia_fuera} hacia otros · ${f.citas_desde_fuera} desde otros`);
    const ol = crear('ol', 'claves');
    for (const id of f.articulos){
      // La dirección viene de las fuentes: sólo se enlaza si es http(s).
      const info = datos.articulos[id], web = /^https?:\/\//i.test(info.u || '');
      const item = crear('li'), enlace = crear(web ? 'a' : 'span');
      if (web){ enlace.href = info.u; enlace.target = '_blank'; enlace.rel = 'noopener'; }
      enlace.textContent = info.t; item.append(enlace, ` (${info.a ?? 's/f'})`); ol.append(item);
    }
    detalle.append(ol);
    li.append(cabeza, detalle); listaFichas.append(li);
  }
}

function dibujarMatriz(m){
  const tabla = raiz.querySelector('.matriz'), nota = raiz.querySelector('.nota-matriz');
  tabla.replaceChildren();
  if (!m.matriz){ nota.textContent = 'Sin clusters para este método.'; return; }
  nota.textContent = 'Filas: cluster del artículo que cita; columnas: cluster del citado. El color muestra qué '
    + 'proporción de las citas de cada fila va a cada columna; pasa el cursor para ver el porcentaje. '
    + (m.matriz.etiquetas.includes('Resto') ? '«Resto» reúne los clusters no dibujados y los artículos sin grupo.' : '');
  const {etiquetas, valores} = m.matriz, cabecera = crear('thead'), fila0 = crear('tr'), cuerpo = crear('tbody');
  const esquina = crear('th'); esquina.textContent = 'Cita ↓ · citado →'; fila0.append(esquina);
  for (const e of etiquetas){ const th = crear('th'); th.scope = 'col'; th.textContent = e; fila0.append(th); }
  cabecera.append(fila0);
  valores.forEach((fila, i) => {
    const tr = crear('tr'), th = crear('th'), total = fila.reduce((s, v) => s + v, 0);
    th.scope = 'row'; th.textContent = etiquetas[i]; tr.append(th);
    fila.forEach((v, j) => {
      const td = crear('td');
      td.textContent = v;
      if (v > 0){ const fondo = tono(v / total); td.style.background = fondo; td.style.color = tintaSobre(fondo); }
      td.title = `${etiquetas[i]} → ${etiquetas[j]}: ${v} ${v === 1 ? 'cita' : 'citas'}`
        + (total ? ` (${pct(v / total)} de las que emite ${etiquetas[i]})` : '');
      tr.append(td);
    });
    cuerpo.append(tr);
  });
  tabla.append(cabecera, cuerpo);
}

function resaltar(){
  const cerca = new Set();
  for (const {de, a, p, o, d} of aristas){
    const activo = seleccionado ? de === seleccionado || a === seleccionado : foco ? o.c === foco || d.c === foco : false;
    p.classList.toggle('activo', activo);
    p.classList.toggle('apagado', !!(seleccionado || foco) && !activo);
    if (activo){ cerca.add(de); cerca.add(a); }
  }
  for (const [id, e] of nodos){
    const visible = seleccionado ? id === seleccionado || cerca.has(id) : foco ? e.c === foco || cerca.has(id) : true;
    e.g.classList.toggle('apagado', !visible);
    e.g.classList.toggle('seleccion', id === seleccionado);
    e.g.setAttribute('aria-pressed', String(id === seleccionado));
  }
  for (const [c, g] of cajas) g.classList.toggle('apagado', !!foco && c !== foco);
  for (const [c, b] of botones) b.setAttribute('aria-pressed', String(c === foco));
}
function enfocar(c){ foco = foco === c ? null : c; seleccionado = null; resaltar(); }
function seleccionar(id){ seleccionado = seleccionado === id ? null : id; foco = null; resaltar(); }

function mostrar(e){
  const info = datos.articulos[e.id], renglon = texto => { const s = crear('span'); s.textContent = texto; return s; };
  const titulo = crear('strong'); titulo.textContent = info.t;
  tip.replaceChildren(titulo, renglon(`${info.a ?? 's/f'}${info.au ? ' · ' + info.au : ''}`),
    ...datos.metodos.filter(m => m.fichas.length).map(m => {
      const c = info.c[m.clave];
      return renglon(`${m.clave === actual ? '▸ ' : ''}${m.nombre}: ${c ? 'C' + c : 'sin grupo'}`);
    }));
  tip.hidden = false;
  situar(tip, lienzo, e.circulo);
}
const ocultar = () => { tip.hidden = true; };

function cambiar(){
  const m = metodos[actual];
  foco = seleccionado = null;
  dibujarRed(m); dibujarFichas(m); dibujarMatriz(m);
  raiz.querySelector('.nota').textContent = m.texto;
  resaltar(); ocultar();
}
for (const r of raiz.querySelectorAll('.herramientas input[type=radio]')){
  r.addEventListener('change', () => { if (r.checked){ actual = r.value; cambiar(); } });
}
document.addEventListener('keydown', ev => { if (ev.key === 'Escape'){ foco = seleccionado = null; resaltar(); ocultar(); } });
temaOscuro.addEventListener('change', () => dibujarMatriz(metodos[actual]));
cambiar();
}
"""
JS_PESTANAS = r"""
// Pestañas accesibles: clic, flechas, Inicio y Fin. La dirección (#autores, #clusters) abre una pestaña.
function iniciarPestanas(){
  const pestanas = [...document.querySelectorAll('.pestanas [role=tab]')];
  const activar = (p, enfocar) => {
    for (const q of pestanas){
      const activa = q === p;
      q.setAttribute('aria-selected', String(activa));
      q.tabIndex = activa ? 0 : -1;
      document.getElementById(q.getAttribute('aria-controls')).hidden = !activa;
    }
    if (enfocar) p.focus();
    try { history.replaceState(null, '', '#' + p.dataset.vista); } catch (e) { /* sin historial en este contexto */ }
  };
  pestanas.forEach((p, i) => {
    p.addEventListener('click', () => activar(p, false));
    p.addEventListener('keydown', ev => {
      const destino = {ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: pestanas.length - 1}[ev.key];
      if (destino === undefined) return;
      ev.preventDefault();
      activar(pestanas[(destino + pestanas.length) % pestanas.length], true);
    });
  });
  const pedida = pestanas.find(p => '#' + p.dataset.vista === location.hash);
  if (pedida) activar(pedida, false);
}
"""
# Un fallo en una vista no impide usar las demás.
SCRIPT_PAGINA = "(() => {" + JS_COLOR + JS_RED + JS_CLUSTERS + JS_PESTANAS + r"""
const iniciar = {red: iniciarRed, clusters: iniciarClusters};
for (const raiz of document.querySelectorAll('[data-tipo]')){
  try { if (iniciar[raiz.dataset.tipo]) iniciar[raiz.dataset.tipo](raiz); }
  catch (error) { console.error('No se pudo dibujar ' + (raiz.id || raiz.dataset.version), error); }
}
// La versión elegida (artículos, libros, o ambos) se aplica a todas las pestañas a la vez.
const radiosVersion = [...document.querySelectorAll('.versiones input[type=radio]')];
for (const r of radiosVersion){
  r.addEventListener('change', () => {
    if (!r.checked) return;
    for (const otro of radiosVersion) otro.checked = otro.value === r.value;
    for (const p of document.querySelectorAll('.version')) p.hidden = p.dataset.version !== r.value;
  });
}
iniciarPestanas();
})();
"""


def porcentaje(x):
    return "s/d" if x is None else f"{round(x * 100)} %"


def decimal(x, cifras=2):
    return "s/d" if x is None else f"{x:.{cifras}f}".replace(".", ",")


def disposicion_clusters(g, entrada, radios, clave_articulo, unidad=UNIDADES["articulos"]):
    """Un recuadro por cluster grande («group-in-a-box»), con sus obras más importantes dentro."""
    miembros = defaultdict(list)
    for w, c in entrada["asignacion"].items():
        miembros[c].append(w)
    elegidos, ancho = sorted(miembros)[:CAJAS_CLUSTERS], 1000
    if not elegidos:
        return ancho, 240, [], []
    mostrados = {c: sorted(miembros[c], key=clave_articulo)[:ARTICULOS_POR_CAJA] for c in elegidos}
    pesos = [len(mostrados[c]) + 3 for c in elegidos]
    alto = max(420, math.ceil(sum(pesos) * 3400 / ancho))
    terminos = {f["c"]: f["terminos"] for f in entrada["fichas"]}
    cajas, nodos = [], []
    for c, (inferior, tam) in zip(elegidos, repartir(pesos, np.array([2., 2.]), np.array([ancho - 4., alto - 4.]))):
        (x, y), (w, h) = inferior + 3, tam - 6
        # Dos renglones: el cluster y su tamaño; debajo, sus términos distintivos.
        etiqueta = recortar(f"C{c} · {plural(len(miembros[c]), *unidad)}", max(6, int((w - 16) / 8.2)))
        detalle = recortar(", ".join(terminos.get(c) or []), max(6, int((w - 16) / 6.4)))
        cajas.append({"c": c, "x": round(float(x), 1), "y": round(float(y), 1), "w": round(float(w), 1),
                      "h": round(float(h), 1), "etiqueta": etiqueta, "detalle": detalle})
        puntos = posiciones_vista(g.subgraph(mostrados[c]), radios, float(w), float(h) - 44)
        for k in mostrados[c]:
            nodos.append([k, round(float(x + puntos[k][0]), 2), round(float(y + 44 + puntos[k][1]), 2), c])
    return ancho, alto, cajas, nodos


def matriz_clusters(g, asignacion):
    """Citas entre los clusters dibujados; «Resto» reúne los demás y los artículos sin grupo."""
    visibles = sorted(set(asignacion.values()))[:CAJAS_CLUSTERS]
    resto = len(set(asignacion.values())) > len(visibles) or len(asignacion) < len(g)
    etiquetas = [f"C{c}" for c in visibles] + (["Resto"] if resto else [])
    indice = {c: i for i, c in enumerate(visibles)}
    valores = np.zeros((len(etiquetas), len(etiquetas)), dtype=int)
    for u, v in g.edges():
        valores[indice.get(asignacion.get(u), len(visibles)), indice.get(asignacion.get(v), len(visibles))] += 1
    return {"etiquetas": etiquetas, "valores": valores.tolist()}


def texto_metodo(m, minimo, unidad=UNIDADES["articulos"]):
    if not m["disponible"]:
        return f'{m["descripcion"]} No disponible: {m["nota"]}'
    r = m["resumen"]
    partes = [m["descripcion"]]
    if r["clusters"]:
        alcance = "del corpus" if unidad == UNIDADES["articulos"] else "de la red"
        partes.append(f'{r["clusters"]} clusters de al menos {minimo} {unidad[1]}, que reúnen el '
                      f'{porcentaje(r["cobertura"])} {alcance} ({r["articulos_asignados"]} {unidad[1]}).')
        if r["modularidad_propia"] is not None:
            partes.append(f'Modularidad en su red: {decimal(r["modularidad_propia"])}.')
        if r["coherencia_tematica"] is not None:
            partes.append(f'Coherencia temática: {porcentaje(r["coherencia_tematica"])} '
                          f'(base {porcentaje(r["coherencia_base"])}).')
        if r["clusters"] > CAJAS_CLUSTERS:
            partes.append(f'Se dibujan los {CAJAS_CLUSTERS} mayores; las fichas describen todos.')
    if m["nota"]:
        partes.append(m["nota"])
    return " ".join(partes)


def tabla_metodos(metodos):
    filas = ['<table><thead><tr><th scope="col">Método</th><th scope="col">Clusters</th>'
             '<th scope="col">Cobertura</th><th scope="col">Modularidad en su red</th>'
             '<th scope="col">Modularidad en la red de citas</th><th scope="col">Coherencia temática</th>'
             '<th scope="col">Base temática</th><th scope="col">NMI con los temas</th></tr></thead><tbody>']
    for m in metodos.values():
        nombre = f'<th scope="row">{html.escape(m["nombre"])}</th>'
        if not m["disponible"]:
            filas.append(f'<tr>{nombre}<td colspan="7">No disponible: {html.escape(m["nota"])}</td></tr>')
            continue
        r = m["resumen"]
        celdas = (r["clusters"], porcentaje(r["cobertura"]), decimal(r["modularidad_propia"]),
                  decimal(r["modularidad_citas"]), porcentaje(r["coherencia_tematica"]),
                  porcentaje(r["coherencia_base"]), decimal(r["nmi_temas"]))
        filas.append(f'<tr>{nombre}' + "".join(f"<td>{x}</td>" for x in celdas) + "</tr>")
    return "".join(filas) + "</tbody></table>"


def tabla_concordancia(res):
    if not res["concordancia"]:
        return '<p class="ayuda">Se necesitan al menos dos métodos con clusters para compararlos.</p>'
    nombres = {k: m["nombre"] for k, m in res["metodos"].items()}
    filas = ['<table><thead><tr><th scope="col">Método</th><th scope="col" class="izquierda">Comparado con</th>'
             '<th scope="col">Artículos en común</th><th scope="col">ARI</th><th scope="col">NMI</th></tr></thead><tbody>']
    for c in res["concordancia"]:
        filas.append(f'<tr><th scope="row">{html.escape(nombres[c["a"]])}</th><td class="izquierda">'
                     f'{html.escape(nombres[c["b"]])}</td><td>{c["comunes"]}</td><td>{decimal(c["ari"])}</td>'
                     f'<td>{decimal(c["nmi"])}</td></tr>')
    return "".join(filas) + "</tbody></table>"


def panel_clusters(g, res, version="articulos"):
    metodos, unidad, clave_vista = res["metodos"], UNIDADES[version], clave_panel("clusters", version)
    maximo = max((g.nodes[w].get("centralidad", 0.0) for w in g), default=0.0)
    radios = {w: 6 + 7 * math.sqrt(c / maximo) if maximo and (c := g.nodes[w].get("centralidad", 0.0)) > 0 else 6
              for w in g}
    clave_articulo = importancia_articulo(g)
    usados, pagina_metodos = set(), []
    for clave, m in metodos.items():
        ancho, alto, cajas, nodos = disposicion_clusters(g, m, radios, clave_articulo, unidad)
        usados.update(x[0] for x in nodos)
        usados.update(w for f in m["fichas"] for w in f["articulos"])
        pagina_metodos.append({"clave": clave, "nombre": m["nombre"], "disponible": m["disponible"], "nota": m["nota"],
                               "texto": texto_metodo(m, res["min_cluster"], unidad), "ancho": ancho, "alto": alto,
                               "cajas": cajas, "nodos": nodos, "fichas": m["fichas"],
                               "matriz": matriz_clusters(g, m["asignacion"]) if m["asignacion"] else None})
    articulos = {}
    for w in sorted(usados):
        d = g.nodes[w]
        autores = [a for a in d.get("autores", "").split("; ") if a]
        articulos[w] = {"t": d["titulo"], "a": d.get("anio"), "u": d.get("url", ""), "r": round(radios[w], 2),
                        "au": "; ".join(autores[:3]) + (" et al." if len(autores) > 3 else ""),
                        "c": {k: m["asignacion"][w] for k, m in metodos.items() if w in m["asignacion"]}}
    con_grupos = [m["clave"] for m in pagina_metodos if m["fichas"]]
    inicial = "acoplamiento" if "acoplamiento" in con_grupos else (con_grupos or [pagina_metodos[0]["clave"]])[0]
    datos = {"inicial": inicial, "colores": CAJAS_CLUSTERS, "unidad": list(unidad), "metodos": pagina_metodos,
             "articulos": articulos,
             # En la red de libros, sin orientación, cada par se dibuja una vez y sin flecha.
             "aristas": [list(e) for e in sorted(g.subgraph(usados).edges())
                         if not g.graph.get("simetrico") or e[0] < e[1]]}
    if g.graph.get("simetrico"):
        datos["sinFlechas"] = True
    opciones = []
    for m in pagina_metodos:
        marcado = " checked" if m["clave"] == inicial else ""
        apagado = "" if m["disponible"] else " disabled"
        opciones.append(f'<label class="opcion"><input type="radio" name="metodo-{clave_vista}" value="{m["clave"]}"'
                        f'{marcado}{apagado}>{html.escape(m["nombre"])}</label>')
    return "\n".join([
        f'<p>Resolución {decimal(res["resolucion"], 2)} · tamaño mínimo de un cluster: {res["min_cluster"]} '
        f'{unidad[1]}. Cada método agrupa {"las" if unidad[1] == "obras" else "los"} {unidad[1]} con datos distintos: las citas directas, las referencias '
        'compartidas o la cocitación. Un subtema es sólido cuando varios métodos lo encuentran: compáralos abajo.'
        + (' En la red de libros, Louvain, Leiden e Infomap trabajan sobre la cocitación y las citas entre libros, '
           'y el acoplamiento sólo ve a los libros que depositan sus referencias en Crossref.'
           if version == "libros" else '') + '</p>',
        descargas([(f"clusters{SUFIJOS[version]}.csv", f"Cluster de cada {unidad[0]} (CSV)"),
                   (f"clusters_resumen{SUFIJOS[version]}.csv", "Resumen de cada cluster (CSV)")]),
        '<div class="rejilla"><section><h2>Mapa de clusters</h2>',
        '<div class="herramientas"><fieldset><legend>Método</legend>', *opciones, '</fieldset></div>',
        '<p class="nota"></p>',
        '<div class="lienzo"><svg class="red" viewBox="0 0 1000 420" role="img" '
        f'aria-label="{unidad[1].capitalize()} agrupados por cluster: un recuadro por cluster"><defs>'
        f'{marcador(clave_vista)}</defs>'
        '<g class="cajas"></g><g class="aristas"></g><g class="nodos"></g></svg>'
        '<p class="vacio aviso" hidden></p><div class="tip" role="tooltip" hidden></div></div>',
        f'<p class="ayuda">Cada recuadro es un cluster y muestra hasta {ARTICULOS_POR_CAJA} de sus {unidad[1]} más '
        'centrales; el área del círculo crece con la centralidad del ranking. '
        + ('Las líneas continuas unen libros del mismo cluster y las discontinuas, libros de clusters distintos. '
           if version == "libros" else 'Las flechas continuas son citas dentro de un cluster y las discontinuas, '
                                        'citas entre clusters. ')
        + f'Pulsa un recuadro o una ficha para ver con qué otros grupos se conecta, o {"un artículo" if version == "articulos" else "una obra"} '
          f'para ver sus {"vínculos" if version == "libros" else "citas"} (Esc para limpiar).</p></section>',
        '<section><h2>Fichas de los clusters</h2><ol class="fichas"></ol></section>',
        '<section class="ancho"><h2>Citas entre clusters</h2><p class="ayuda nota-matriz"></p>'
        '<div class="tabla"><table class="matriz"></table></div></section>',
        '<section class="ancho"><h2>Comparación de métodos</h2>',
        '<p class="ayuda">Cobertura: proporción del corpus que queda en algún cluster. Modularidad: cuánto más '
        'densas son las conexiones dentro de los grupos que entre ellos, respecto del azar; por encima de 0,3 '
        'suele indicar una estructura clara. La columna de la red de citas mide a todos los métodos con la misma '
        'vara. Coherencia temática: proporción de artículos que comparten el tema principal de OpenAlex más '
        'frecuente de su cluster; la base es esa proporción si no hubiera grupos. NMI con los temas: 0 si los '
        'clusters no dicen nada de los temas de OpenAlex y 1 si los reproducen.</p>',
        f'<div class="tabla">{tabla_metodos(metodos)}</div>',
        '<h2>Concordancia entre métodos</h2>',
        '<p class="ayuda">ARI (índice de Rand ajustado) y NMI miden cuánto coinciden dos particiones en los '
        'artículos que ambos métodos agrupan. 1 es acuerdo total y un ARI cercano a 0 equivale al azar. Si métodos '
        'basados en datos distintos coinciden, los subtemas no dependen del método.</p>',
        f'<div class="tabla">{tabla_concordancia(res)}</div></section></div>',
        datos_json(datos)])


def panel_libros(libros, versiones=None):
    """Pestaña estática: los libros de OUP y CUP más citados por el corpus."""
    filas, resumen = libros["filas"], libros["resumen"]
    intro = [f'<p>{plural(resumen["libros"], "libro", "libros")} de Oxford University Press y Cambridge University '
             f'Press citados por {plural(resumen["articulos_que_citan"], "artículo", "artículos")} del corpus.</p>',
             descargas([("libros_oup_cup.csv", "Todos los libros citados (CSV)")])]
    if not filas:
        return "\n".join([*intro, '<p class="aviso">Ningún artículo del corpus cita, con DOI en Crossref, un libro '
                                  'de OUP o CUP.</p>'])
    maximo = filas[0]["citas_corpus"]
    celdas = []
    for f in filas[:LIBROS_VISTA]:
        autoria = f["autores"] + (" (eds.)" if f["editores"] and f["autores"] else "")
        titulo = html.escape(f["titulo"] or f["doi"])
        celdas.append(
            f'<tr><td>{f["puesto"]}</td><td class="izquierda"><a href="{html.escape("https://doi.org/" + f["doi"])}" '
            f'target="_blank" rel="noopener">{titulo}</a>'
            + (f'<small>{html.escape(autoria)}</small>' if autoria else '') + '</td>'
            f'<td>{f["editorial"]}</td><td>{f["anio"] or "s/f"}</td>'
            f'<td class="celda-barra"><span>{f["citas_corpus"]}</span><span class="pista"><span class="relleno" '
            f'style="width:{100 * f["citas_corpus"] / maximo:.1f}%"></span></span></td>'
            f'<td>{f["capitulos_citados"] or ""}</td><td>{"" if f["citas_crossref"] is None else f["citas_crossref"]}</td>'
            f'<td>{posicion_libro(versiones, "libros", f["doi"])[0]}</td></tr>')
    recorte = (f' Se muestran los {LIBROS_VISTA} primeros de {len(filas)}; el CSV los tiene todos.'
               if len(filas) > LIBROS_VISTA else '')
    return "\n".join([
        *intro, '<div class="rejilla"><section class="ancho"><h2>Libros más citados por el corpus</h2>',
        '<p class="ayuda">Cuentan los artículos del corpus que citan el libro o alguno de sus capítulos, según las '
        'referencias con DOI que las editoriales depositan en Crossref. Las referencias que OpenAlex enlaza por su '
        'identificador no indican la editorial y no se cuentan; tampoco las referencias sin DOI. «Citas en '
        f'Crossref» es el contador global del libro, dentro y fuera del corpus.{recorte}</p>',
        '<div class="tabla"><table class="libros"><thead><tr><th scope="col">#</th><th scope="col" class="izquierda">'
        'Libro</th><th scope="col">Editorial</th><th scope="col">Año</th><th scope="col" class="izquierda">Artículos '
        'del corpus que lo citan</th><th scope="col">Capítulos citados</th><th scope="col">Citas en Crossref</th>'
        '<th scope="col">Puesto en la red de libros</th>'
        '</tr></thead><tbody>', *celdas, '</tbody></table></div></section></div>'])


def aviso_fallo(analisis, resumen):
    detalle = (resumen or {}).get("error", "error desconocido")
    return (f'<p class="aviso">No se completó {analisis}: {html.escape(detalle)}. El resto de los resultados se '
            'guardó igual; consulta.json tiene el detalle.</p>')


def info_version(version):
    """Lo que panel_articulos necesita de una versión: métrica del ranking y resumen de medidas."""
    return {"metrica_ranking": version["metrica"], "medidas": resumen_medidas(version["medidas"])}


def contenido_vista(vista, base, versiones, info, constructor):
    """Selector «Artículos · Libros · Libros y artículos» y un subpanel por versión dentro de una pestaña."""
    opciones, partes = [], []
    for i, (version, nombre) in enumerate(VERSIONES):
        opciones.append(f'<label class="opcion"><input type="radio" name="version-{vista}" value="{version}"'
                        f'{"" if i else " checked"}>{html.escape(nombre)}</label>')
        if version == "articulos":
            tipo, cuerpo = base
        elif "g" in (calculada := (versiones or {}).get(version, {})):
            tipo, cuerpo = constructor(calculada)
        else:
            motivo = (info.get("versiones") or {}).get(version, {}).get("error", "error desconocido")
            tipo, cuerpo = "aviso", (f'<p class="aviso">La versión «{html.escape(nombre)}» no está disponible: '
                                     f'{html.escape(motivo)}.</p>')
        partes.append(f'<div class="version" data-version="{version}" data-tipo="{tipo}"{" hidden" if i else ""}>'
                      f'\n{cuerpo}\n</div>')
    return "\n".join([f'<fieldset class="versiones"><legend>Obras</legend>{"".join(opciones)}</fieldset>', *partes])


def pagina_unica(ruta, g, orden, info, max_nodos, max_autores, autores, clusters, libros=None, versiones=None):
    """grafo.html: red, autores, clusters y libros en pestañas de un solo archivo. Las tres primeras
    pestañas tienen un selector de versión: artículos, libros, o libros y artículos."""
    tema = info["tema"]
    base = {"articulos": ("red", panel_articulos(g, orden, info, max_nodos)),
            "autores": (("red", panel_autores(g, autores, max_autores)) if autores
                        else ("aviso", aviso_fallo("el análisis por autor", info.get("autores")))),
            "clusters": (("clusters", panel_clusters(g, clusters)) if clusters
                         else ("aviso", aviso_fallo("el análisis de clusters", info.get("clusters"))))}
    constructores = {
        "articulos": lambda v: ("red", panel_articulos(v["g"], v["orden"], info_version(v), max_nodos, v["clave"])),
        "autores": lambda v: (("red", panel_autores(v["g"], v["autores"], max_autores, v["clave"])) if v["autores"]
                              else ("aviso", aviso_fallo("el análisis por autor", v["resumen"].get("autores")))),
        "clusters": lambda v: (("clusters", panel_clusters(v["g"], v["clusters"], v["clave"])) if v["clusters"]
                               else ("aviso", aviso_fallo("el análisis de clusters", v["resumen"].get("clusters")))),
    }
    pestanas, vistas = [], []
    for i, (clave, nombre) in enumerate(VISTAS):
        if clave in constructores:
            tipo, contenido = None, contenido_vista(clave, base[clave], versiones, info, constructores[clave])
        elif libros:
            tipo, contenido = "estatico", panel_libros(libros, versiones)
        else:
            tipo, contenido = "aviso", aviso_fallo("el ranking de libros", info.get("libros_oup_cup"))
        pestanas.append(f'<button type="button" role="tab" id="pestana-{clave}" data-vista="{clave}" '
                        f'aria-controls="vista-{clave}" aria-selected="{"true" if not i else "false"}" '
                        f'tabindex="{0 if not i else -1}">{html.escape(nombre)}</button>')
        vistas.append(f'<div class="vista" role="tabpanel" id="vista-{clave}" aria-labelledby="pestana-{clave}"'
                      + (f' data-tipo="{tipo}"' if tipo else '') + f'{"" if not i else " hidden"}>\n{contenido}\n</div>')
    partes = ['<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" '
              'content="width=device-width,initial-scale=1">',
              f'<title>Red de citas en filosofía · {html.escape(tema)}</title>',
              f'<style>{ESTILO}{ESTILO_CLUSTERS}</style>',
              f'<h1>Red de citas en filosofía · {html.escape(tema)}</h1>',
              f'<p>{len(g)} artículos · {g.number_of_edges()} citas internas · métrica del ranking: '
              f'{info["metrica_ranking"]}.</p>',
              f'<p>Subáreas: {html.escape(info["subareas_texto"])}. Fuentes: {html.escape(info["fuentes_texto"])}.</p>',
              '<main><div class="pestanas" role="tablist" aria-label="Vistas">', *pestanas, '</div>', *vistas,
              '</main>', f'<script>{SCRIPT_PAGINA}</script></html>']
    ruta.write_text("\n".join(partes), encoding="utf-8")


COLUMNAS_AUTORES = ("puesto", "autor", "id_openalex", "articulos", "articulos_fraccionales", "citas_internas",
                    "h_interno", "citas_openalex", "centralidad_acumulada", "pagerank", "citas_emitidas", "coautores",
                    "variantes_nombre", "articulos_principales", "ids_articulos")
COLUMNAS_RESUMEN_CLUSTERS = ("metodo", "cluster", "articulos", "anios", "mediana_anio", "terminos", "temas",
                             "subareas", "autores_clave", "libros_clave", "articulos_clave", "citas_dentro",
                             "citas_hacia_fuera", "citas_desde_fuera")
COLUMNAS_LIBROS = ("puesto", "editorial", "doi", "titulo", "autores", "editores", "anio", "tipo", "citas_corpus",
                   "capitulos_citados", "citas_crossref", "puesto_libros", "centralidad_libros", "puesto_mixta",
                   "centralidad_mixta", "articulos_que_citan")
# Ranking de las versiones con libros. En la de libros, «citas_recibidas_corpus» son los artículos del
# corpus que citan el libro y «referencias_en_corpus» los libros de la red que el libro cita.
COLUMNAS_VERSIONES = ("puesto", "centralidad", "metrica_ranking", "tipo_obra", "id", "titulo", "anio", "autores",
                      "revista", "editorial", "doi", "url", "fuentes", "subareas", "citas_recibidas_corpus",
                      "referencias_en_corpus", "citas_openalex", "citas_crossref", "id_openalex",
                      "vector_propio_dirigido", "vector_propio_simetrico")


def guardar_autores(carpeta, g, autores, sufijo=""):
    with (carpeta / f"autores{sufijo}.csv").open("w", newline="", encoding="utf-8-sig") as f:
        escritor = csv.DictWriter(f, fieldnames=COLUMNAS_AUTORES, extrasaction="ignore")
        escritor.writeheader()
        for fila in autores["filas"]:
            escritor.writerow({**fila, "articulos_principales": " | ".join(g.nodes[k]["titulo"]
                                                                            for k in fila["articulos_ids"][:3]),
                               "ids_articulos": "; ".join(fila["articulos_ids"])})


def guardar_clusters(carpeta, g, orden, clusters, sufijo=""):
    metodos = [m for m in clusters["metodos"].values() if m["disponible"]]
    with (carpeta / f"clusters{sufijo}.csv").open("w", newline="", encoding="utf-8-sig") as f:
        escritor = csv.writer(f)
        escritor.writerow(["id", "titulo", "anio", "puesto", *("cluster_" + m["clave"] for m in metodos)])
        for w in orden:
            d = g.nodes[w]
            escritor.writerow([w, d["titulo"], d.get("anio", ""), d.get("puesto", ""),
                               *(m["asignacion"].get(w, "") for m in metodos)])
    proporciones = lambda pares: "; ".join(f"{t} ({porcentaje(p)})" for t, p in pares)  # noqa: E731
    with (carpeta / f"clusters_resumen{sufijo}.csv").open("w", newline="", encoding="utf-8-sig") as f:
        escritor = csv.DictWriter(f, fieldnames=COLUMNAS_RESUMEN_CLUSTERS, extrasaction="ignore")
        escritor.writeheader()
        for m in metodos:
            for ficha in m["fichas"]:
                escritor.writerow({**ficha, "metodo": m["clave"], "cluster": ficha["c"], "articulos": ficha["n"],
                                   "terminos": "; ".join(ficha["terminos"]), "temas": proporciones(ficha["temas"]),
                                   "subareas": proporciones(ficha["subareas"]),
                                   "autores_clave": "; ".join(f"{a} ({n})" for a, n in ficha["autores"]),
                                   "libros_clave": "; ".join(f"{t} ({n})" for t, n in ficha.get("libros", [])),
                                   "articulos_clave": " | ".join(g.nodes[w]["titulo"] for w in ficha["articulos"])})


def resumen_clusters(clusters):
    """Lo que se guarda en consulta.json: diagnósticos por método y concordancia."""
    return {"resolucion": clusters["resolucion"], "min_cluster": clusters["min_cluster"],
            "vecinos_similitud": clusters["vecinos_similitud"],
            "metodos": {k: {"nombre": m["nombre"], "disponible": m["disponible"], "nota": m["nota"], **m["resumen"],
                            **({"componentes_espectrales": m["detalle"]} if m["detalle"] else {})}
                        for k, m in clusters["metodos"].items()},
            "concordancia": clusters["concordancia"]}


def posicion_libro(versiones, clave, doi):
    """(puesto, centralidad) de un libro en la red de una versión, o ("", "") si no figura."""
    g = (versiones or {}).get(clave, {}).get("g")
    d = g.nodes[doi] if g is not None and doi in g else {}
    return d.get("puesto", ""), d.get("centralidad", "")


def guardar_libros(carpeta, libros, versiones=None):
    with (carpeta / "libros_oup_cup.csv").open("w", newline="", encoding="utf-8-sig") as f:
        escritor = csv.DictWriter(f, fieldnames=COLUMNAS_LIBROS, extrasaction="ignore")
        escritor.writeheader()
        for fila in libros["filas"]:
            (puesto_l, centralidad_l), (puesto_m, centralidad_m) = (posicion_libro(versiones, v, fila["doi"])
                                                                    for v in ("libros", "mixta"))
            escritor.writerow({**fila, "editores": "sí" if fila["editores"] else "",
                               "puesto_libros": puesto_l, "centralidad_libros": centralidad_l,
                               "puesto_mixta": puesto_m, "centralidad_mixta": centralidad_m,
                               "articulos_que_citan": "; ".join(fila["articulos"])})


def guardar_version(carpeta, version, info):
    """ranking, grafo, autores y clusters de una versión con libros, con su sufijo."""
    sufijo, g = SUFIJOS[version["clave"]], version["g"]
    with (carpeta / f"ranking{sufijo}.csv").open("w", newline="", encoding="utf-8-sig") as f:
        escritor = csv.DictWriter(f, fieldnames=COLUMNAS_VERSIONES, extrasaction="ignore")
        escritor.writeheader()
        for w in version["orden"]:
            escritor.writerow({**g.nodes[w], "id": w})
    g.graph.update(consulta=info["tema"], fecha_utc=info["fecha_consulta_utc"], metrica_ranking=version["metrica"],
                   version=dict(VERSIONES)[version["clave"]],
                   sentido="sin orientación (cocitación y citas)" if g.graph.get("simetrico") else "citante -> citado")
    nx.write_graphml(g, carpeta / f"grafo{sufijo}.graphml", encoding="utf-8")
    if version["autores"]:
        guardar_autores(carpeta, g, version["autores"], sufijo)
    if version["clusters"]:
        guardar_clusters(carpeta, g, version["orden"], version["clusters"], sufijo)


def guardar(carpeta, g, orden, info, max_nodos, max_autores, autores, clusters, libros=None, versiones=None):
    with (carpeta / "ranking.csv").open("w", newline="", encoding="utf-8-sig") as f:
        escritor = csv.DictWriter(f, fieldnames=COLUMNAS, extrasaction="ignore")
        escritor.writeheader()
        for w in orden:
            escritor.writerow({**g.nodes[w], "id": w})
    with (carpeta / "citas.csv").open("w", newline="", encoding="utf-8-sig") as f:
        escritor = csv.writer(f)
        escritor.writerow(["id_citante", "id_citado", "titulo_citante", "titulo_citado"])
        for u, v in sorted(g.edges()):
            escritor.writerow([u, v, g.nodes[u]["titulo"], g.nodes[v]["titulo"]])
    g.graph.update(consulta=info["tema"], fecha_utc=info["fecha_consulta_utc"],
                   metrica_ranking=info["metrica_ranking"], sentido="citante -> citado",
                   subareas=info["subareas_texto"], fuentes=info["fuentes_texto"])
    nx.write_graphml(g, carpeta / "grafo.graphml", encoding="utf-8")
    if autores:
        guardar_autores(carpeta, g, autores)
    if clusters:
        guardar_clusters(carpeta, g, orden, clusters)
    if libros:
        guardar_libros(carpeta, libros, versiones)
    for version in (versiones or {}).values():
        guardar_version(carpeta, version, info)
    pagina_unica(carpeta / "grafo.html", g, orden, info, max_nodos, max_autores, autores, clusters, libros,
                 versiones)
    (carpeta / "consulta.json").write_text(json.dumps(info, ensure_ascii=False, indent=2,
                                                       allow_nan=False) + "\n", encoding="utf-8")


def publicar_temporal(temporal, salida):
    # Una ejecución nueva no sobrescribe los resultados de otra.
    i = 1
    while True:
        candidata = salida if i == 1 else salida.with_name(f"{salida.name}-{i}")
        if not os.path.lexists(candidata):
            try:
                return temporal.rename(candidata)
            except OSError as exc:
                if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                    raise
        i += 1


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("tema", nargs="?", help='palabra clave o expresión; para frase exacta: \'"modal logic"\'')
    p.add_argument("--subarea", action="append", metavar="SUBÁREA",
                   help="restringe a subáreas de la filosofía (etica, epistemologia, mente, logica, politica...) "
                        "o a un tema de OpenAlex (T11477); repetible o separada por comas. Sin ella se usa "
                        "toda la filosofía")
    p.add_argument("--criterio", choices=("cualquiera", "principal"), default="cualquiera",
                   help="cualquiera: basta con que un tema filosófico figure entre los asignados al artículo; "
                        "principal: debe ser su tema principal (más estricto)")
    p.add_argument("--listar-subareas", action="store_true", help="muestra las subáreas y sus temas y termina")
    p.add_argument("--fuentes", default="todas", metavar="LISTA",
                   help=f"{', '.join(FUENTES)}, separadas por comas (oupcup: revistas de Oxford y Cambridge "
                        "University Press en Crossref); «todas» usa las que no piden clave y las que tengan su "
                        "clave definida")
    p.add_argument("--limite", type=int, default=1500,
                   help="máximo de registros por fuente; 0 recorre todas las coincidencias de OpenAlex y "
                        f"PhilPapers y hasta {TOPE_EXTERNO} en las demás")
    p.add_argument("--limite-google", type=int, default=40,
                   help="máximo de resultados de Google Académico; cada 20 consumen una búsqueda de SerpApi")
    p.add_argument("--desde", type=int, metavar="AÑO", help="primer año de publicación (inclusive)")
    p.add_argument("--hasta", type=int, metavar="AÑO", help="último año de publicación (inclusive)")
    p.add_argument("--modo", choices=("auto", "dirigido", "simetrico"), default="auto",
                   help="métrica para ordenar; auto usa el vector dirigido cuando es informativo y único")
    p.add_argument("--top", type=int, default=10, help="máximo de títulos que se imprimen en la terminal")
    p.add_argument("--nodos", type=int, default=NODOS_VISTA,
                   help="artículos que se dibujan en la pestaña de la red de artículos de grafo.html; 0 dibuja todos los que tienen enlaces")
    p.add_argument("--autores", type=int, default=NODOS_AUTORES,
                   help="autores que se dibujan en la pestaña de autores de grafo.html; 0 dibuja todos los que citan o son citados por otro")
    p.add_argument("--resolucion", type=float, default=1.0,
                   help="resolución de Louvain y Leiden: valores mayores dan más clusters y más pequeños")
    p.add_argument("--min-cluster", type=int, default=3, metavar="N",
                   help="artículos mínimos para considerar un cluster; los grupos menores quedan sin asignar")
    p.add_argument("--salida", type=Path, default=Path("resultados_bibliometria"),
                   help="carpeta de resultados; si existe, se añade -2, -3, etc.")
    args = p.parse_args()
    if args.listar_subareas:
        listar_subareas()
        return
    tema = (args.tema or "").strip()
    if not tema or min(args.limite, args.top, args.nodos, args.autores) < 0 or args.limite_google < 1:
        p.error("indica un tema; --limite, --top, --nodos y --autores no pueden ser negativos y --limite-google "
                "debe ser al menos 1")
    if not (math.isfinite(args.resolucion) and args.resolucion > 0) or args.min_cluster < 2:
        p.error("--resolucion debe ser un número positivo y --min-cluster al menos 2")
    if any(y is not None and not 1 <= y <= 9999 for y in (args.desde, args.hasta)):
        p.error("los años deben estar entre 1 y 9999")
    if args.desde and args.hasta and args.desde > args.hasta:
        p.error("--desde debe ser anterior o igual a --hasta")
    salida = args.salida.expanduser().absolute()
    if not salida.name or args.salida.name in ("", ".."):
        p.error("--salida debe especificar el nombre de una carpeta nueva de resultados")
    try:
        elegidas = resolver_subareas(args.subarea)
        fuentes, omitidas = resolver_fuentes(args.fuentes)
    except ValueError as exc:
        p.error(str(exc))
    temas = temas_de(elegidas)
    if len(temas) > MAX_OR:
        p.error(f"la selección equivale a {len(temas)} temas y OpenAlex admite {MAX_OR}")
    prefijo = "primary_topic.id" if args.criterio == "principal" else "topics.id"
    filtro_temas = f"{prefijo}:" + "|".join(sorted(temas))
    clave = variable("OPENALEX_API_KEY")
    correo = variable("CROSSREF_MAILTO")

    corpus = Corpus()
    estado = {f: {"estado": "omitida", "detalle": motivo} for f, motivo in omitidas.items()}
    for fuente in fuentes:
        try:
            if fuente == "openalex":
                registros, detalle = buscar_openalex(tema, args.limite, args.desde, args.hasta, clave,
                                                     [filtro_temas], "openalex")
            elif fuente == "philpapers":
                filtros = [f"locations.source.id:{REPOSITORIO_PHILPAPERS}"] + ([filtro_temas] if elegidas else [])
                registros, detalle = buscar_openalex(tema, args.limite, args.desde, args.hasta, clave, filtros,
                                                     "philpapers", SELECT + ",locations", filosofia=True)
            elif fuente == "crossref":
                registros, detalle = buscar_crossref(tema, args.limite, args.desde, args.hasta, correo)
            elif fuente == "oupcup":
                registros, detalle = buscar_crossref(tema, args.limite, args.desde, args.hasta, correo,
                                                     MIEMBROS_OUP_CUP, "oupcup")
            elif fuente == "scopus":
                registros, detalle = buscar_scopus(tema, args.limite, args.desde, args.hasta,
                                                   variable(*VARIABLES_CLAVE["scopus"]), variable("SCOPUS_INSTTOKEN"))
            elif fuente == "springer":
                registros, detalle = buscar_springer(tema, args.limite, args.desde, args.hasta,
                                                     variable(*VARIABLES_CLAVE["springer"]))
            else:
                registros, detalle = buscar_google(tema, args.limite_google, args.desde, args.hasta,
                                                   variable(*VARIABLES_CLAVE["google"]))
        except (RuntimeError, KeyError, TypeError, AttributeError, ValueError) as exc:
            # Un fallo o un formato inesperado en una fuente no anula las demás.
            detalle = str(exc) if isinstance(exc, RuntimeError) else f"respuesta inesperada ({type(exc).__name__}: {exc})"
            estado[fuente] = {"estado": "error", "detalle": detalle}
            print(f"Aviso: se omite {NOMBRE_FUENTE[fuente]}. {detalle}", file=sys.stderr, flush=True)
            continue
        for r in registros:
            corpus.agregar(r)
        estado[fuente] = {"estado": "ok", "registros": len(registros), **detalle}
    if not any(e["estado"] == "ok" for e in estado.values()):
        raise RuntimeError("Ninguna fuente respondió. Revisa los avisos anteriores.")

    # Las etapas de enriquecimiento no detienen el programa: si fallan, se registra el aviso
    # y los artículos que no se pudieron clasificar quedan fuera del corpus.
    enriquecimiento = {}
    for etapa, funcion in (("doi_google", lambda: resolver_doi_google(corpus, correo)),
                           ("openalex", lambda: completar_openalex(corpus, clave))):
        if etapa == "doi_google" and estado.get("google", {}).get("estado") != "ok":
            continue
        try:
            enriquecimiento[etapa] = funcion()
        except RuntimeError as exc:
            enriquecimiento[etapa] = {"error": str(exc)}
            print(f"Aviso: no se completó la etapa {etapa}. {exc}", file=sys.stderr, flush=True)
    conservados, descartes = clasificar(corpus, temas, bool(elegidas), args.criterio, args.desde, args.hasta)
    try:
        enriquecimiento["crossref"] = completar_crossref(conservados, correo)
    except RuntimeError as exc:
        enriquecimiento["crossref"] = {"error": str(exc)}
        print(f"Aviso: no se completaron las referencias de Crossref. {exc}", file=sys.stderr, flush=True)
    aportes = Counter(f for r in conservados for f in r["fuentes"])
    for fuente, e in estado.items():
        if e["estado"] == "ok":
            e["en_corpus"] = aportes[fuente]

    g = grafo_citas(conservados)
    ids, medidas = calcular(g)
    orden, metrica = ordenar(g, ids, medidas, args.modo)
    # Los análisis de libros, autores y clusters son secundarios: si fallan, se guarda el ranking igualmente.
    autores = clusters = libros = None
    try:
        libros = analizar_libros(conservados, correo)
        resumen_libros = libros["resumen"]
    except ERRORES_ANALISIS as exc:
        resumen_libros = {"error": f"{type(exc).__name__}: {exc}"}
        print(f"Aviso: no se completó el ranking de libros de OUP y CUP. {exc}", file=sys.stderr, flush=True)
    try:
        autores = analizar_autores(conservados, g)
        resumen_autores = autores["resumen"]
    except ERRORES_ANALISIS as exc:
        resumen_autores = {"error": f"{type(exc).__name__}: {exc}"}
        print(f"Aviso: no se completó el análisis por autor. {exc}", file=sys.stderr, flush=True)
    try:
        clusters = analizar_clusters(conservados, g, autores, args.resolucion, args.min_cluster, libros)
        for clave, m in clusters["metodos"].items():
            for w, c in m["asignacion"].items():
                g.nodes[w]["cluster_" + clave] = c
        resumen_grupos = resumen_clusters(clusters)
    except ERRORES_ANALISIS as exc:
        resumen_grupos = {"error": f"{type(exc).__name__}: {exc}"}
        print(f"Aviso: no se completó el análisis de clusters. {exc}", file=sys.stderr, flush=True)
    # Versiones con libros: sólo libros, y libros y artículos juntos.
    versiones, resumen_versiones = {}, {}
    for clave in ("libros", "mixta"):
        if not (libros and libros["registros"]):
            resumen_versiones[clave] = {"error": "el corpus no cita libros de OUP o CUP con DOI en Crossref"}
            continue
        try:
            versiones[clave] = analizar_version(clave, conservados, libros, args.modo, args.resolucion,
                                                args.min_cluster)
            resumen_versiones[clave] = versiones[clave]["resumen"]
        except ERRORES_ANALISIS as exc:
            resumen_versiones[clave] = {"error": f"{type(exc).__name__}: {exc}"}
            print(f"Aviso: no se completó la versión «{dict(VERSIONES)[clave]}». {exc}", file=sys.stderr, flush=True)
    info = {"tema": tema, "subareas": [{"clave": k, "nombre": nombre_subarea(k)} for k in elegidas],
            "subareas_texto": ", ".join(nombre_subarea(k) for k in elegidas) or "toda la filosofía",
            "criterio": args.criterio, "filtro_temas_openalex": filtro_temas, "desde": args.desde,
            "hasta": args.hasta, "limite_por_fuente": args.limite, "limite_google": args.limite_google,
            "fecha_consulta_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "fuentes": {f: estado[f] for f in FUENTES if f in estado},
            "fuentes_texto": ", ".join(f"{NOMBRE_FUENTE[f]} ({estado[f]['en_corpus']})" for f in FUENTES
                                       if estado.get(f, {}).get("estado") == "ok"),
            "enriquecimiento": enriquecimiento, "descartados": dict(descartes),
            "articulos": len(g), "citas_internas": g.number_of_edges(),
            "sin_referencias_enlazadas": sum(not r["refs"] for r in conservados),
            "aislados": nx.number_of_isolates(g), "modo_solicitado": args.modo, "metrica_ranking": metrica,
            "nodos_vista": args.nodos,
            "normalizacion": "L2 global; empates de puntuación a tolerancia relativa 1e-9",
            "medidas": resumen_medidas(medidas),
            "referencias_sin_doi": {"leidas": g.graph["citas_sin_doi"],
                                    "enlazadas": g.graph["citas_sin_doi_enlazadas"]},
            "nodos_vista_autores": args.autores, "autores": resumen_autores, "clusters": resumen_grupos,
            "libros_oup_cup": resumen_libros, "versiones": resumen_versiones}
    salida.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=salida.parent, prefix=".bibliometria-") as tmp:
        guardar(Path(tmp), g, orden, info, args.nodos, args.autores, autores, clusters, libros, versiones)
        destino = publicar_temporal(Path(tmp), salida)

    print("\nFuentes:")
    for fuente in FUENTES:
        if e := estado.get(fuente):
            if e["estado"] == "ok":
                print(f"  {NOMBRE_FUENTE[fuente]}: {e['registros']} registros, {e['en_corpus']} en el corpus")
            else:
                print(f"  {NOMBRE_FUENTE[fuente]}: {e['estado']} ({e['detalle']})")
    if descartes:
        print("Descartados: " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in descartes.most_common()))
    print(f"Subáreas: {info['subareas_texto']} (criterio: {args.criterio}).")
    print(f"{len(g)} artículos | {g.number_of_edges()} citas internas | {info['aislados']} aislados")
    if g.graph["citas_sin_doi"]:
        print(f"Referencias de Crossref sin DOI: {g.graph['citas_sin_doi']} leídas; "
              f"{g.graph['citas_sin_doi_enlazadas']} citas internas nuevas enlazadas por título y año.")
    for k, m in medidas.items():
        print(f"Vector {k}: {m.nota}")
    print(f"Métrica del ranking: {metrica}.")
    for w in [w for w in orden if "puesto" in g.nodes[w]][:args.top]:
        d = g.nodes[w]
        print(f'{d["puesto"]:>3}. {d["centralidad"]:.6f}  {d["titulo"]} ({d.get("anio", "s/f")})')
    if autores and autores["filas"]:
        con_pagerank = autores["resumen"]["metrica_ranking"] == "pagerank"
        print(f"\nAutores más relevantes ({'PageRank en la red de citas entre autores' if con_pagerank else 'citas internas'};"
              f" {autores['resumen']['autocitas_excluidas']} autocitas excluidas):")
        for f in autores["filas"][:args.top]:
            print(f"{f['puesto']:>3}. {f['autor']}: {f['articulos']} art., {f['citas_internas']} citas internas, "
                  f"h {f['h_interno']}" + (f", PageRank {f['pagerank']:.4f}" if con_pagerank else ""))
    if clusters:
        print(f"\nClusters (al menos {args.min_cluster} artículos; resolución {args.resolucion:g}):")
        for m in clusters["metodos"].values():
            r = m["resumen"]
            if not m["disponible"]:
                print(f"  {m['nombre']}: no disponible ({m['nota']})")
                continue
            calidad = f", modularidad {r['modularidad_propia']:.2f}" if r["modularidad_propia"] is not None else ""
            print(f"  {m['nombre']}: {r['clusters']} clusters, {r['cobertura']:.0%} del corpus{calidad}")
            for ficha in m["fichas"][:5]:
                rotulo = ", ".join(ficha["terminos"][:4]) or (ficha["temas"][0][0] if ficha["temas"] else "")
                print(f"     C{ficha['c']} ({ficha['n']} art.): {rotulo}")
    if libros and libros["filas"]:
        print(f"\nLibros de OUP y CUP más citados ({libros['resumen']['libros']} libros citados con DOI):")
        for f in libros["filas"][:5]:
            autoria = f" — {f['autores'].split('; ')[0]}" if f["autores"] else ""
            print(f"{f['puesto']:>3}. {f['titulo'] or f['doi']}{autoria} ({f['editorial']}, {f['anio'] or 's/f'}): "
                  f"{plural(f['citas_corpus'], 'artículo', 'artículos')} del corpus")
    for clave, version in versiones.items():
        g_v, r = version["g"], version["resumen"]
        print(f"\nVersión «{dict(VERSIONES)[clave]}»: {r['nodos']} obras ({r['libros']} libros), {r['enlaces']} "
              f"enlaces; métrica del ranking: {version['metrica']}.")
        for w in [w for w in version["orden"] if "puesto" in g_v.nodes[w]][:3]:
            d = g_v.nodes[w]
            print(f'{d["puesto"]:>3}. {d["centralidad"]:.6f}  {d["titulo"]} '
                  f'({"libro" if d.get("tipo_obra") == "libro" else "artículo"}, {d.get("anio", "s/f")})')
        if version["autores"] and version["autores"]["filas"]:
            print("     Autores: " + "; ".join(f["autor"] for f in version["autores"]["filas"][:3]))
    print(f"Resultados: {destino}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError) as error:
        sys.exit(f"Error: {error}")
    except KeyboardInterrupt:
        sys.exit("Ejecución interrumpida.")
