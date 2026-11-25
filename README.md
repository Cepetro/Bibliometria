# Análisis bibliométrico

`bibliometria.py` busca artículos de filosofía en varias bases bibliográficas, une los registros que describen el mismo artículo y construye la red de citas entre ellos. Después calcula la centralidad de vector propio de cada artículo, identifica a los autores más relevantes, busca grupos de artículos, o subtemas, con seis métodos de clustering para grafos dirigidos, y ordena los libros de Oxford University Press y Cambridge University Press que cita el corpus. La centralidad, los autores y los clusters se calculan en tres versiones: sólo artículos, sólo libros, y libros y artículos juntos. Las vistas interactivas están en un solo archivo, `grafo.html`, organizadas en pestañas.

## Ejecutar

Necesitas Python 3.10 o posterior. Desde el directorio que contiene el programa, ejecuta en Linux o macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python bibliometria.py "epistemic injustice" --salida red_injusticia
```

Dos métodos de clustering, Infomap y Leiden, necesitan paquetes opcionales. Sin ellos, el programa ejecuta los demás y marca esos dos como no disponibles:

```bash
python -m pip install infomap igraph leidenalg
```

Más ejemplos:

```bash
python bibliometria.py "emergence" --subarea mente,metafisica --nodos 80
python bibliometria.py '"modal logic"' --subarea logica --desde 2000 --hasta 2025 --fuentes openalex,philpapers,crossref
python bibliometria.py --listar-subareas
```

Sustituye el tema por tu palabra clave o expresión. Para exigir una frase exacta, las comillas dobles interiores deben llegar a la fuente, como en el ejemplo de `"modal logic"` en Bash o Zsh. Crossref ignora las comillas y busca las palabras por separado.

| Opción | Efecto |
| --- | --- |
| `--subarea` | Restringe a subáreas de la filosofía. Sin ella se usa toda la filosofía. |
| `--criterio` | `cualquiera` (predeterminado) o `principal`. Ver más abajo. |
| `--fuentes` | Lista separada por comas: `openalex`, `crossref`, `oupcup` (también `oup` o `cup`), `philpapers`, `scopus`, `springer`, `google`. `todas` (predeterminado) usa las que no piden clave y las que tengan su clave definida. |
| `--limite` | Máximo de registros por fuente (1500 por defecto). `0` recorre todas las coincidencias de OpenAlex y PhilPapers y hasta 2000 en las demás. |
| `--limite-google` | Máximo de resultados de Google Académico (40 por defecto). |
| `--desde`, `--hasta` | Años de publicación, inclusivos. |
| `--nodos` | Artículos que se dibujan en la pestaña «Red de artículos» de `grafo.html` (40 por defecto). `0` dibuja todos los que tienen citas. No afecta los cálculos. |
| `--modo` | Métrica del ranking: `auto`, `dirigido` o `simetrico`. |
| `--autores` | Autores que se dibujan en la pestaña «Autores» de `grafo.html` (40 por defecto). `0` dibuja todos los que citan a otro autor o reciben citas de otro. No afecta los cálculos. |
| `--resolucion` | Resolución de Louvain y Leiden (1 por defecto). Valores mayores dan más clusters y más pequeños. |
| `--min-cluster` | Artículos mínimos de un cluster (3 por defecto). Los grupos menores quedan sin asignar. |
| `--top` | Títulos y autores que se imprimen en la terminal. |
| `--salida` | Carpeta de resultados. Si ya existe, se añade `-2`, `-3`, etc. |

## Fuentes y claves

| Fuente | Clave | Qué aporta | Cómo se asegura que sea filosofía |
| --- | --- | --- | --- |
| OpenAlex | Opcional: `OPENALEX_API_KEY` (gratuita) | Búsqueda en título y resumen, temas, tipo de obra y referencias. | Filtra por los temas filosóficos de la lista de abajo. |
| PhilPapers | No (consulta a través de OpenAlex) | Artículos depositados en PhilArchive, el archivo de PhilPapers. | Todo lo que contiene es filosofía. |
| Crossref | No. Opcional: `CROSSREF_MAILTO` | Búsqueda en metadatos y referencias, con DOI o sin él. | Comprueba cada DOI con los temas de OpenAlex. |
| OUP y CUP (Crossref) | No. Opcional: `CROSSREF_MAILTO` | Búsqueda en Crossref limitada a las revistas de Oxford University Press y Cambridge University Press, con sus referencias. | Comprueba cada DOI con los temas de OpenAlex. |
| Scopus | `SCOPUS_API_KEY` y, fuera de la red institucional, `SCOPUS_INSTTOKEN` | Búsqueda `TITLE-ABS-KEY` y número de citas en Scopus. | Limita a `SUBJAREA(ARTS)` (Artes y Humanidades) y comprueba cada DOI con OpenAlex. |
| Springer Nature | `SPRINGER_API_KEY` | Búsqueda en la Meta API de Springer. | Limita a la materia `Philosophy` de Springer. |
| Google Académico | `SERPAPI_API_KEY` (SerpApi, de pago) | Resultados y número de citas en Google Académico. | Obtiene el DOI y lo comprueba con OpenAlex. |

Define las claves como variables de entorno antes de ejecutar el programa:

```bash
export OPENALEX_API_KEY="tu_clave"
export SPRINGER_API_KEY="tu_clave"
```

Si pides una fuente con `--fuentes` y falta su clave, el programa se detiene y te lo indica. Con `--fuentes todas`, simplemente la omite. Si una fuente rechaza la clave o falla durante la ejecución, el programa sigue con las demás y anota el error en `consulta.json`. Las claves no se guardan en ningún archivo de resultados. Crossref responde mejor a quien se identifica; el programa sólo le envía un correo si defines `CROSSREF_MAILTO`.

Particularidades de cada fuente:

* **OpenAlex** cobra por uso. Sin clave, la API da 0,10 USD diarios compartidos por todos los usuarios de tu dirección IP. Con una [clave gratuita](https://openalex.org/settings/api) el presupuesto es de 1 USD diario y se puede paginar más allá de 10 000 resultados. Cada página de búsqueda (100 artículos) cuesta 0,001 USD. Comprobar 50 DOI de otras fuentes en un solo lote cuesta 0,0001 USD. Consultar un DOI suelto es gratis, de modo que si el presupuesto se agota, el programa sigue comprobando los DOI uno a uno, más despacio. Las búsquedas de OpenAlex y PhilPapers, en cambio, sí necesitan presupuesto.
* **PhilPapers** no ofrece búsqueda pública por API: su API JSON está protegida por un control antibots y su interfaz OAI-PMH sólo permite cosechar todo el índice por fechas. El programa consulta el repositorio de PhilPapers que indexa OpenAlex (`S4306402130`). Ahí están los artículos depositados en PhilArchive, no todo el catálogo de PhilPapers.
* **Crossref** no clasifica los artículos por materia y ordena por relevancia en todas las disciplinas. Por eso muchos de sus resultados se descartan al comprobarlos con OpenAlex.
* **OUP y CUP (Crossref)** busca en todas las revistas de Oxford University Press y Cambridge University Press, que en Crossref son los miembros 286 y 56. Ahí están Mind, The Philosophical Quarterly, Analysis, Proceedings of the Aristotelian Society, Philosophy, Episteme, Journal of the American Philosophical Association, Kantian Review, Religious Studies, Utilitas o Canadian Journal of Philosophy, pero también revistas de medicina, derecho o astronomía. Lo que no es filosofía se descarta con los temas de OpenAlex, igual que en las demás fuentes. Cada registro supone una consulta de su DOI en OpenAlex: si el presupuesto se agota, el programa sigue uno a uno, más despacio. Las dos editoriales depositan referencias abiertas en Crossref, aunque no para todos sus artículos.
* **Scopus** exige una clave de Elsevier con derechos institucionales. Sólo permite filtrar por áreas amplias, no por filosofía. Sin paginación por cursor, entrega como máximo 5000 resultados por consulta.
* **Springer Nature** no filtra por rango de años, así que el programa filtra localmente. Deja de pedir páginas tras examinar cinco veces el límite. El adaptador se escribió sin una clave para probarlo: si la API responde con un formato inesperado, el programa avisa y sigue con las demás fuentes.
* **Google Académico** no tiene API oficial. SerpApi la ofrece como servicio comercial: cada 20 resultados consumen una búsqueda de tu plan. Google no da DOI. El programa lo toma de la dirección del artículo cuando la editorial lo incluye. Si no, lo busca en Crossref por título, autor y año. Los resultados sin DOI quedan fuera, porque no se puede comprobar que sean filosofía.

## Filosofía y subáreas

La clasificación procede de los temas de OpenAlex, que asigna hasta tres temas a cada obra. «Filosofía» abarca 71 temas: los 45 del subcampo *Philosophy* (1211) y 26 temas filosóficos de otros subcampos. Entre estos están lógica, filosofía de la ciencia, filosofía política, bioética, ética ambiental y filosofía de la historia.

| Subárea | Contenido |
| --- | --- |
| `etica` | Ética normativa y metaética |
| `etica-aplicada` | Ética aplicada y bioética |
| `epistemologia` | Epistemología |
| `metafisica` | Metafísica, tiempo y filosofía del proceso |
| `mente` | Filosofía de la mente y de la psiquiatría |
| `lenguaje` | Filosofía del lenguaje y hermenéutica |
| `logica` | Lógica y filosofía de la matemática y la probabilidad |
| `ciencia` | Filosofía e historia de la ciencia y de la medicina |
| `tecnologia` | Filosofía de la tecnología y de la inteligencia artificial |
| `politica` | Filosofía política, social y del derecho |
| `feminista` | Filosofía feminista |
| `estetica` | Estética y filosofía del arte y la literatura |
| `religion` | Filosofía de la religión y teología |
| `educacion` | Filosofía de la educación |
| `historia` | Filosofía de la historia |
| `antigua`, `medieval`, `moderna` | Historia de la filosofía por periodos |
| `continental` | Filosofía continental, fenomenología y teoría crítica |
| `pragmatismo` | Pragmatismo |
| `no-occidental` | Filosofías africana, india, islámica, judía y decolonial |
| `hispanica` | Filosofía española e hispanoamericana |
| `general` | Metafilosofía y temas generales |

`--subarea` acepta la clave, el nombre o un alias, en español o inglés y con o sin tildes. Por ejemplo, `etica`, `ética`, `ethics` o `bioethics`. Puedes repetir la opción o separar subáreas con comas; entran los artículos de cualquiera de ellas. También acepta un tema concreto, como `--subarea T11477`. `--listar-subareas` muestra qué temas de OpenAlex componen cada subárea.

Los temas de OpenAlex no cubren todas las ramas por igual. Metafísica y filosofía del lenguaje no tienen temas propios y se aproximan con los más cercanos, como el libre albedrío, la filosofía del proceso, Wittgenstein o la hermenéutica. Conviene revisar `--listar-subareas` antes de interpretar los resultados de esas subáreas.

`--criterio cualquiera` acepta un artículo si un tema de la selección figura entre sus temas. `--criterio principal` exige que sea su tema principal y deja fuera muchos más artículos.

Sin `--subarea`, los artículos de PhilPapers y los de Springer con materia *Philosophy* se aceptan aunque OpenAlex los clasifique en otra disciplina. Con `--subarea`, todos deben coincidir por sus temas de OpenAlex.

## Cómo se combinan las fuentes

1. Cada fuente devuelve hasta `--limite` registros.
2. Los registros se unen cuando comparten DOI, identificador de OpenAlex o, para títulos largos, título y año. Un mismo artículo puede tener dos DOI, por ejemplo el de Project MUSE y el de la editorial. Ambos sirven para unir registros y enlazar citas. Cuando dos fuentes difieren en los metadatos, manda la primera de esta lista: OpenAlex, Crossref, Scopus, Springer, Google Académico.
3. Los DOI de las demás fuentes se comprueban en OpenAlex, que aporta los temas, el tipo de obra y las referencias.
4. Sólo quedan artículos de revista (tipo `article` en OpenAlex) de filosofía, o de las subáreas elegidas, dentro del periodo. Reseñas, editoriales y capítulos quedan fuera. `consulta.json` cuenta los descartes por motivo.
5. Las referencias combinan las que enlaza OpenAlex con las que Crossref publica, con DOI o sin él. Hay una arista **i → j** si el artículo **i** cita a **j** y ambos están en el corpus. Una referencia de Crossref sin DOI, que suele traer autor, año y título, cita a un artículo del corpus cuando coinciden el título (de al menos 25 caracteres, sin distinguir mayúsculas, tildes ni puntuación) y el año, con un año de margen por la diferencia entre la versión en línea y la impresa, y sólo hay un candidato. Las referencias que sólo traen texto libre se ignoran. `consulta.json` cuenta las referencias sin DOI leídas y las citas internas nuevas que aportan.

## Leer los resultados

| Archivo | Contenido |
| --- | --- |
| `ranking.csv` | Una fila por artículo: puesto, centralidad, título, año, autoría, revista, DOI, enlace, fuentes, subáreas, citas en cada base e identificadores. |
| `citas.csv` | Una fila por cita interna con los identificadores y títulos del artículo citante y el citado. |
| `grafo.graphml` | Red dirigida completa con atributos por artículo, lista para abrir en Gephi o NetworkX. Incluye los nodos aislados. Los atributos `cluster_<método>` permiten colorear los clusters en Gephi. |
| `grafo.html` | Visualización interactiva en un solo archivo, con cuatro pestañas: «Red de artículos», «Autores», «Clusters y subtemas» y «Libros OUP/CUP». Se describen abajo. |
| `autores.csv` | Una fila por autor con sus métricas, su identificador de OpenAlex, las variantes de su nombre, sus tres artículos principales y los identificadores de todos sus artículos. |
| `clusters.csv` | Una fila por artículo con el cluster que le asigna cada método. Una celda vacía significa que el método no lo agrupó. |
| `clusters_resumen.csv` | Una fila por método y cluster: tamaño, años, términos distintivos, temas, subáreas, autores, libros de OUP y CUP y artículos clave, y citas dentro, hacia fuera y desde fuera del cluster. |
| `libros_oup_cup.csv` | Una fila por libro de OUP o CUP citado: editorial, DOI, título, autores o editores, año, tipo, artículos del corpus que lo citan, capítulos citados, citas en Crossref, puesto y centralidad en la red de libros y en la de libros y artículos, e identificadores de los artículos que lo citan. |
| `ranking_libros.csv`, `ranking_mixto.csv` | Ranking de las versiones «Libros» y «Libros y artículos», con la columna `tipo_obra`. En el de libros, `citas_recibidas_corpus` son los artículos del corpus que citan el libro y `referencias_en_corpus` los libros de la red que el libro cita. |
| `autores_libros.csv`, `autores_mixto.csv` | Métricas de autor de esas versiones. En la de libros, `articulos` cuenta libros. |
| `clusters_libros.csv`, `clusters_resumen_libros.csv`, `clusters_mixto.csv`, `clusters_resumen_mixto.csv` | Clusters de esas versiones. |
| `grafo_libros.graphml`, `grafo_mixto.graphml` | Redes de esas versiones. En la de libros, cada arista lleva `weight`, `cocitaciones` y `citas`. |
| `consulta.json` | Tema, subáreas, filtro aplicado en OpenAlex, fecha UTC, estado y aporte de cada fuente, descartes, tamaño del corpus, métrica aplicada y diagnósticos numéricos. Los bloques `autores`, `clusters` y `libros_oup_cup` resumen esos análisis, `versiones` resume las versiones «Libros» y «Libros y artículos», y `referencias_sin_doi` cuenta las referencias de Crossref sin DOI. Si uno falla, el bloque guarda el error, su pestaña de `grafo.html` lo indica y el resto de los archivos se escribe igual. |

`grafo.html` no necesita conexión ni otros archivos para mostrarse. Los enlaces de cada pestaña a los CSV y al GraphML funcionan mientras estén en la misma carpeta. Las pestañas se cambian con el ratón o con las flechas del teclado. Para abrir una pestaña concreta, añade a la dirección `#autores`, `#clusters` o `#libros`. Las pestañas «Red de artículos», «Autores» y «Clusters y subtemas» tienen arriba un selector «Obras: Artículos · Libros · Libros y artículos». La versión elegida se aplica a las tres pestañas a la vez.

| Pestaña | Contenido |
| --- | --- |
| Red de artículos | Los primeros `--nodos` artículos con citas. Color y tamaño muestran la centralidad de vector propio. Un selector alterna entre el vector dirigido y el simétrico, y los no disponibles aparecen desactivados con su motivo. Al pasar el cursor se ven valores, subáreas y fuentes. Seleccionar un artículo resalta sus citas. Con muchos nodos, el botón «Tamaño real» muestra el grafo a escala nativa con desplazamiento. |
| Autores | Red de citas entre los primeros `--autores` autores del ranking y lista ordenada. Un selector cambia la métrica que dan el color, el tamaño y el orden de la lista. |
| Clusters y subtemas | Mapa de clusters por método, fichas descriptivas, matriz de citas entre clusters y comparación de métodos. |
| Libros OUP/CUP | Los 50 libros de Oxford University Press y Cambridge University Press más citados por el corpus, con enlace a su DOI y su puesto en la red de libros. |

Columnas de `ranking.csv` que conviene conocer:

* `id` es el DOI del artículo o, si no tiene, `openalex:W…`.
* `doi_alternativo` recoge un segundo DOI del mismo artículo cuando existe.
* `fuentes` indica en qué bases apareció.
* `subareas` indica qué subáreas corresponden a sus temas.
* `citas_recibidas_corpus` cuenta cuántos artículos del corpus lo citan, y `referencias_en_corpus` cuántos artículos del corpus figuran en su bibliografía.
* `citas_openalex`, `citas_crossref`, `citas_scopus` y `citas_google_academico` recogen el contador global de cada base.
* `referencias_enlazadas` cuenta las referencias que se pudieron leer, con identificador o, si vienen de Crossref sin DOI, con título. Un artículo con cero puede tener bibliografía en el original que las bases no enlazan.

## Qué calcula la centralidad

Cada nodo es un artículo del corpus. El programa cuenta cada par de artículos una sola vez, excluye las autorreferencias de una obra y conserva las citas entre artículos distintos del mismo autor. Cambiar las fechas, la expresión, las fuentes o el límite puede cambiar el ranking.

* **Dirigido**. Con la convención `A[i,j] = 1` cuando *i* cita a *j*, el programa resuelve `A.T @ x = ρ x`, donde ρ es el radio espectral. Cada artículo recibe puntuación de los artículos que lo citan, en proporción a la puntuación de éstos. El orden de las fechas suele producir redes acíclicas. En una red acíclica la matriz es nilpotente y ρ = 0, de modo que esta medida pierde su interpretación recursiva. El programa deja esa columna en blanco. También la deja en blanco si hay varios bloques dominantes finales indistinguibles a la precisión usada o si falla la verificación numérica.
* **Simétrico**. El programa construye `S = maximum(A, A.T)` y resuelve `S @ x = ρ x`. Puntúan tanto los vínculos entrantes como los salientes con artículos centrales. Una cita en ambos sentidos da un solo vínculo en S. Los vectores tienen norma euclídea 1. En una red desconectada, las componentes con radio menor que el máximo reciben cero. Si varias componentes alcanzan el máximo a la tolerancia usada, se asigna la misma norma a cada una antes de la normalización global.

`--modo auto` ordena por el vector dirigido cuando está disponible y, en su defecto, por el simétrico. La columna `metrica_ranking` identifica el usado. Las puntuaciones positivas empatadas comparten puesto. Un cero corresponde a un valor calculado y una celda de centralidad vacía indica una medida no disponible. Si el corpus no tiene citas internas, se exportan los artículos y el grafo sin asignar puestos.

## Análisis por autor

Cada autor reúne los artículos del corpus que firma y se identifica por su identificador de OpenAlex. Crossref, Scopus y Google Académico sólo dan nombres. Un nombre sin identificador se une al autor de OpenAlex que lo lleva si sólo hay uno con ese nombre, sin distinguir tildes ni mayúsculas. Si no, el autor queda identificado sólo por su nombre. Variantes como «J. Smith» y «John Smith» no se unen, y un autor que OpenAlex divide en dos perfiles aparece dos veces.

Una cita entre dos artículos que comparten al menos un autor es una autocita y no cuenta en ninguna métrica de autor. `consulta.json` informa cuántas se excluyeron.

| Columna de `autores.csv` | Qué mide |
| --- | --- |
| `pagerank` | PageRank (α = 0,85) en la red dirigida de citas entre autores. Hay una arista A → B si un artículo de A cita uno de B, con peso igual al número de esas citas. Un autor puntúa más si lo citan autores que a su vez reciben citas. |
| `citas_internas` | Artículos del corpus que citan algún artículo del autor. |
| `h_interno` | Índice h con las citas internas: h artículos con al menos h citas cada uno. |
| `articulos`, `articulos_fraccionales` | Artículos firmados. En el recuento fraccional, un artículo con n autores cuenta 1/n para cada uno. |
| `centralidad_acumulada` | Suma de la centralidad de vector propio de sus artículos según la métrica del ranking. |
| `citas_openalex` | Suma del contador global de citas de OpenAlex de sus artículos, dentro y fuera del corpus. |
| `citas_emitidas`, `coautores` | Citas a otros autores del corpus y número de coautores distintos. |

El ranking ordena por PageRank y desempata por citas internas, citas globales y nombre. Se usa PageRank y no el vector propio dirigido porque la red de autores, como la de artículos, suele ser casi acíclica, y en ella el vector propio dirigido se anula. El teletransporte de PageRank da una puntuación definida en cualquier red. Si no hay citas entre autores distintos, el orden empieza por las citas internas.

## Clusters y subtemas

El programa agrupa los artículos con seis métodos que parten de la dirección de las citas, pero usan datos distintos:

| Método | Datos | Qué agrupa |
| --- | --- | --- |
| Louvain dirigido | Citas internas | Maximiza la modularidad dirigida de Leicht y Newman. |
| Leiden dirigido (opcional) | Citas internas | La misma modularidad con el algoritmo de Leiden, que además garantiza grupos conexos. Requiere `igraph` y `leidenalg`. |
| Infomap (opcional) | Citas internas | Minimiza la ecuación de mapa del flujo dirigido de citas. Un grupo es una región donde quien sigue las referencias queda atrapado más tiempo. Requiere `infomap`. |
| Acoplamiento bibliográfico | Referencias compartidas | Louvain sobre la similitud del coseno entre bibliografías completas, incluidas las obras citadas que no están en el corpus. Agrupa frentes de investigación. |
| Cocitación | Artículos citados juntos | Louvain sobre la similitud del coseno entre los conjuntos de artículos del corpus que citan a cada artículo. Agrupa la base intelectual. |
| Espectral bibliométrico | Acoplamiento y cocitación | Clustering espectral de Ng, Jordan y Weiss sobre la simetrización bibliométrica con descuento de grado de Satuluri y Parthasarathy (2011), que resta peso a las obras muy citadas y a las bibliografías largas. Cada componente conexa se divide en el número de grupos, entre 2 y 15, que da la partición de mayor modularidad. |

Las citas internas suelen ser escasas. En el ejemplo `red_causalidad`, 1595 de 2093 artículos no citan a otro del corpus ni reciben citas de él. Los métodos que sólo usan citas internas dejan esos artículos sin grupo. El acoplamiento y el método espectral usan también las referencias a obras de fuera del corpus, así que agrupan casi todos los artículos con bibliografía enlazada.

Detalles del cálculo:

* Las redes de similitud conservan, para cada artículo, los 15 artículos más parecidos. Así el cálculo es rápido y una obra que cita casi todo el corpus no une todos los artículos entre sí.
* Una misma obra externa puede figurar con su identificador de OpenAlex y con el DOI que da Crossref. En ese caso cuenta como dos referencias.
* Los grupos con menos de `--min-cluster` artículos quedan sin asignar. `--resolucion` cambia el tamaño de los grupos de Louvain y Leiden.
* Los métodos usan una semilla fija: la misma consulta da los mismos clusters.
* Los métodos basados en modularidad siempre encuentran algún grupo, incluso en una red sin estructura. Por eso conviene leer la modularidad y la concordancia entre métodos antes de interpretar un grupo como subtema.

Cada cluster se describe con:

* los términos distintivos de sus títulos, que son palabras o pares de palabras frecuentes en el cluster y raros en el corpus;
* los temas de OpenAlex y las subáreas más frecuentes, con su proporción;
* sus tres artículos más centrales, los autores con más artículos en él y los libros de OUP y CUP que más cita;
* el número de citas dentro del grupo, hacia otros grupos y desde otros grupos.

En la pestaña «Clusters y subtemas» de `grafo.html`, un selector elige el método. El mapa dibuja un recuadro por cada uno de los ocho clusters mayores con hasta 15 de sus artículos más centrales. Cada recuadro lleva su color, su número y sus términos. Las flechas continuas son citas dentro de un cluster y las discontinuas, citas entre clusters. Pulsar un recuadro o una ficha resalta con qué otros grupos se conecta. Las fichas describen todos los clusters, también los que no caben en el mapa. La matriz de citas muestra qué proporción de las citas de cada cluster va a cada uno de los demás.

La tabla de comparación ayuda a decidir si los subtemas son reales:

* **Cobertura**: proporción del corpus que queda en algún cluster.
* **Modularidad**: cuánto más densas son las conexiones dentro de los grupos que entre ellos, respecto del azar. Por encima de 0,3 suele indicar una estructura clara. La columna «en su red» usa la red con la que agrupa cada método. La columna «en la red de citas» mide todos los métodos con la misma vara.
* **Coherencia temática**: proporción de artículos que comparten el tema principal de OpenAlex más frecuente de su cluster. La base es esa proporción si no hubiera grupos. Una coherencia muy superior a la base indica que los clusters separan temas.
* **NMI con los temas**: 0 si los clusters no dicen nada de los temas de OpenAlex y 1 si los reproducen. Un valor intermedio puede indicar subtemas que OpenAlex no distingue.
* **ARI y NMI entre métodos**: cuánto coinciden dos métodos en los artículos que ambos agrupan. 1 es acuerdo total, y un ARI cercano a 0 equivale al azar. Si métodos basados en datos distintos coinciden, los subtemas no dependen del método.

## Libros de OUP y CUP

Buena parte de la bibliografía filosófica son libros, y muchos de ellos los publican Oxford University Press y Cambridge University Press. Sus libros y capítulos tienen DOI propios: `10.1093/acprof:oso/…`, `10.1093/oso/…` y `10.1093/oxfordhb/…` en OUP, y `10.1017/CBO…`, `10.1017/CCOL…` y `10.1017/978…` en CUP. El programa lee esos DOI en las referencias de Crossref de los artículos del corpus y cuenta, para cada libro, cuántos artículos lo citan. Una cita a un capítulo cuenta para su libro. Luego pide a Crossref el título, la autoría, el año y el número global de citas de los libros más citados, por lotes y sin coste.

El recuento tiene dos límites. Sólo ve las referencias con DOI que Crossref conoce: las que OpenAlex enlaza por su identificador no indican la editorial de la obra citada, y las referencias sin DOI tampoco. En la versión principal, la de artículos, los libros no son nodos; sí lo son en las versiones «Libros» y «Libros y artículos». Las referencias sin DOI a libros sí cuentan en el acoplamiento bibliográfico: dos artículos que citan el mismo libro, aunque sea en ediciones distintas, comparten esa referencia.

## Versiones: artículos, libros y ambos

La centralidad de vector propio, el análisis por autor y los seis métodos de clustering se calculan sobre tres redes. Los libros son los de Oxford University Press y Cambridge University Press citados por el corpus, hasta 1000, ordenados por citas. Sus títulos, autores o editores y años vienen de Crossref.

| Versión | Nodos | Enlaces |
| --- | --- | --- |
| Artículos | Artículos del corpus | Citas entre artículos. Es la versión principal: `ranking.csv`, `autores.csv`, `clusters.csv`… |
| Libros | Libros de OUP y CUP citados | Cocitación y citas directas. Dos libros se enlazan cuando un mismo artículo del corpus cita a ambos y cuando uno cita al otro según las referencias que el libro deposita en Crossref. El peso suma las dos cosas y el enlace no tiene orientación. |
| Libros y artículos | Artículos y libros | Citas dirigidas: artículo → artículo, artículo → libro (también a través de un capítulo), libro → libro y libro → artículo. |

Detalles:

* **Referencias de los libros.** El programa pide a Crossref, por ISBN, el libro y todos sus capítulos, y une sus referencias sin repetir: OUP copia la bibliografía del libro en cada capítulo. Sólo una parte de los libros las deposita, en torno a uno de cada cinco capítulos, así que en la red de libros la cocitación pesa más que las citas directas. Las referencias de un libro a sus propios capítulos no cuentan.
* **Centralidad de la red de libros.** Como la red no tiene orientación, se usa el vector propio con pesos («Sin orientación»). La medida dirigida aparece como no disponible.
* **Autores de libros.** Crossref sólo da nombres. En la versión de libros, cada autor se identifica por su nombre; sus «citas internas» son los artículos del corpus que citan sus libros, sin descontar autocitas; y su PageRank se calcula en una red de cocitación de autores. Dos autores quedan unidos cuando sus libros se citan juntos o uno cita al otro, y no cuentan los pares de libros con un autor común. En la versión mixta, el autor de un libro se une a su perfil de OpenAlex cuando su nombre coincide con el de un solo autor de artículos, y las citas globales de los libros vienen de Crossref.
* **Clusters.** En la versión de libros, Louvain, Leiden e Infomap trabajan sobre la red con pesos. El acoplamiento sólo ve a los libros que depositan sus referencias. Los temas y subáreas de OpenAlex no existen para los libros, así que su coherencia temática aparece como «s/d».
* **Grafo.** La red de libros se dibuja sin flechas y con un grosor que crece con el peso. En la mixta, los libros llevan un anillo punteado exterior.

## Ejemplo incluido

La carpeta `ejemplo_okishio` procede de una versión anterior del programa, de propósito general, aplicada a un tema de economía. Sirve para ver la vista interactiva, pero sus columnas no coinciden con las actuales.
