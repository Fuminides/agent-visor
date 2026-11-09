# Visor

Una ventana local para que Codex y Claude Code te enseñen imágenes y gráficos desde la terminal. El propio comando abre la app cuando hace falta y reutiliza la ventana existente.

## Uso

```bash
visor show grafico.png --title "Resultados" --caption "El error baja tras el ajuste."
visor show antes.png despues.png --session "Mi análisis"
visor show charla.pdf --title "Resultados del experimento"
visor show s1.png s2.png s3.png --deck --title "Resumen"
visor demo
visor status --json
visor history --limit 10
visor stop
```

La carpeta actual identifica el proyecto por defecto. También puedes definir `VISOR_SESSION` o pasar `--session` para separar conversaciones. El historial guarda copias: sobrescribir o borrar el archivo original no elimina las versiones anteriores.

## Presentaciones

Un PDF de varias páginas se guarda como una sola entrada del historial con todas sus páginas como diapositivas, en orden. Sirve cualquier generador: Beamer, Quarto, Marp, `PdfPages` de matplotlib… Con `--deck`, todas las imágenes y PDF de la misma llamada forman una única presentación con el título del primer archivo. Sin `--deck`, cada imagen sigue siendo una entrada propia. Una llamada admite hasta 100 imágenes o páginas. Las páginas del PDF se convierten a PNG con 2400 px en el lado largo.

Si regeneras el PDF (o cualquier archivo de la presentación) con **Seguir lo nuevo** activado, entra una versión nueva y la ventana se queda en la misma diapositiva.

## Preguntar a la sesión

Cuando Claude Code muestra una imagen, `visor show` guarda también qué sesión la mostró (`CLAUDE_CODE_SESSION_ID`). El botón **Preguntar** abre un panel para hacer preguntas sobre la imagen o presentación seleccionada a esa sesión, sin interrumpirla. La primera pregunta crea una bifurcación (`claude --resume <id> --fork-session`) y las siguientes la continúan, así que recuerda lo hablado. **Nuevo hilo** empieza otra bifurcación. Con clic derecho en la figura marcas un punto: la siguiente pregunta lleva sus coordenadas y dos imágenes que el modelo mira con `Read`, la figura con el punto rodeado en rojo y una ampliación alrededor. Se guardan en `~/.local/share/agent-visor/points/`. **Copiar comando** copia `claude --resume <bifurcación>` para seguir esa conversación en la terminal. Mientras responde, **Enviar** pasa a ser **Detener**.

La bifurcación solo puede leer y buscar (`Read`, `Grep`, `Glob`, sin servidores MCP): no edita archivos ni ejecuta comandos, porque desde la ventana nadie puede aprobar permisos. La sesión original no cambia. La bifurcación copia la sesión tal como está al preguntar, así que también conoce lo que pasó después de la imagen. Cada versión de una figura tiene su propio hilo. Las imágenes que no vienen de Claude Code (de Codex o abiertas a mano) no tienen sesión a la que preguntar.

## Historial reciente

Los botones **Todo**, **Hoy** y **Última hora** bajo HISTORIAL filtran las entradas por fecha. Las demás siguen guardadas. El filtro elegido se recuerda.

La imagen seleccionada se actualiza al cambiar su archivo original cuando es la última versión de ese archivo y está activado **Seguir lo nuevo**. El cambio debe permanecer estable durante dos comprobaciones (aproximadamente dos segundos). Al desactivar esa opción puedes revisar versiones mientras el agente sigue enviando resultados. El botón de novedades lleva al resultado más reciente.

La primera llamada abre la ventana. Las siguientes actualizan su contenido sin elevarla ni pedir el foco. `--focus` la trae al frente. Cerrar la ventana detiene la app; la siguiente imagen vuelve a abrirla y recupera el historial. No hay servicio de arranque permanente.

## Controles

- Rueda del ratón: zoom alrededor del puntero; arrastrar: desplazar.
- Doble clic o `F`: ajustar la imagen; `1`: tamaño real.
- Flechas izquierda/derecha: diapositiva anterior/siguiente en una presentación; versiones anteriores/siguientes en las demás imágenes.
- Re Pág/Av Pág: diapositiva anterior/siguiente (sirve con mandos de presentación).
- Flechas arriba/abajo y botones ← →: entrada más reciente/anterior del historial.
- `Ctrl+O`: abrir imágenes manualmente.
- Clic derecho en la imagen: señala un punto para preguntar por el elemento que hay ahí. Abre el panel de preguntas si está cerrado.
- En el panel de preguntas: `Intro` envía y `Mayús+Intro` añade una línea. Mientras escribes, los atajos de la ventana no actúan; haz clic en la imagen para recuperarlos.

Admite PDF, PNG, JPEG, WebP, GIF (primer fotograma), TIFF, BMP e ICO, hasta 24 megapíxeles o 100 MB por archivo. Exporta los SVG a PNG o PDF. La ventana muestra hasta 200 entradas recientes por proyecto; las anteriores siguen guardadas y se pueden consultar con `history --limit 1000`.

## Instalación

Linux con escritorio gráfico, Python 3.10+, Tk y Pillow. Para PDF hace falta además `poppler-utils` (`pdfinfo` y `pdftoppm`), que suele venir instalado. Esta instalación usa las bibliotecas que ya hay en el equipo; no descarga dependencias.

```bash
python3 install.py
~/.local/bin/visor demo
```

El instalador crea el comando `~/.local/bin/visor`, un acceso en el menú de aplicaciones y enlaces a la habilidad en `~/.codex/skills/agent-visor` y `~/.claude/skills/agent-visor`. Las sesiones nuevas de ambos agentes podrán descubrirla y usarla automáticamente cuando ayude mostrar algo visual. La selección de habilidades depende del agente; la instalación no modifica sus permisos de ejecución ni configura MCP. En una sesión ya abierta se puede leer directamente `skills/agent-visor/SKILL.md`.

Mantén esta carpeta en su ubicación: el comando y las habilidades enlazan a ella. Si la mueves, vuelve a ejecutar el instalador y actualiza los enlaces existentes. `~/.local/bin` debe estar en `PATH` para usar el nombre corto; la ruta absoluta funciona siempre.

## Datos y funcionamiento

- Imágenes e historial: `~/.local/share/agent-visor/`.
- Registro de errores: `~/.local/share/agent-visor/visor.log`.
- IPC: socket Unix privado en `$XDG_RUNTIME_DIR/agent-visor/`, o `~/.cache/agent-visor/`.
- Sin puertos de red, cuentas ni claves API. Solo el panel de preguntas llama a un modelo, mediante tu comando `claude`, y únicamente cuando envías una pregunta.

Los directorios XDG se respetan. `VISOR_DATA_DIR` y `VISOR_RUNTIME_DIR` permiten aislar datos y procesos, por ejemplo durante pruebas. El historial permanece en disco hasta que lo borres.

## Desarrollo y pruebas

```bash
python3 -m unittest discover -s tests -v
python3 /home/javier/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/agent-visor
```

Las pruebas de integración gráfica se omiten si no hay acceso al escritorio. Con escritorio prueban el comando real, arranque simultáneo, reutilización, actualizaciones, cierre y restauración.

Para desinstalar, cierra con `visor stop` y elimina el comando, los dos enlaces a habilidades y `~/.local/share/applications/agent-visor.desktop`. Los datos quedan separados para que puedas conservarlos.
