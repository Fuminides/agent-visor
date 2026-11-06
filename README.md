# Visor

Una ventana local para que Codex y Claude Code te enseñen imágenes y gráficos desde la terminal. El propio comando abre la app cuando hace falta y reutiliza la ventana existente.

## Uso

```bash
visor show grafico.png --title "Resultados" --caption "El error baja tras el ajuste."
visor show antes.png despues.png --session "Mi análisis"
visor demo
visor status --json
visor history --limit 10
visor stop
```

La carpeta actual identifica el proyecto por defecto. También puedes definir `VISOR_SESSION` o pasar `--session` para separar conversaciones. El historial guarda copias: sobrescribir o borrar el archivo original no elimina las versiones anteriores.

La imagen seleccionada se actualiza al cambiar su archivo original cuando es la última versión de ese archivo y está activado **Seguir lo nuevo**. El cambio debe permanecer estable durante dos comprobaciones (aproximadamente dos segundos). Al desactivar esa opción puedes revisar versiones mientras el agente sigue enviando resultados. El botón de novedades lleva al resultado más reciente.

La primera llamada abre la ventana. Las siguientes actualizan su contenido sin elevarla ni pedir el foco. `--focus` la trae al frente. Cerrar la ventana detiene la app; la siguiente imagen vuelve a abrirla y recupera el historial. No hay servicio de arranque permanente.

## Controles

- Rueda del ratón: zoom alrededor del puntero; arrastrar: desplazar.
- Doble clic o `F`: ajustar la imagen; `1`: tamaño real.
- Flechas izquierda/derecha: versiones anteriores/siguientes.
- `Ctrl+O`: abrir imágenes manualmente.

Admite PNG, JPEG, WebP, GIF (primer fotograma), TIFF, BMP e ICO, hasta 24 megapíxeles o 100 MB. Exporta gráficos SVG o PDF a PNG para esta primera versión. La ventana muestra hasta 200 entradas recientes por proyecto; las anteriores siguen guardadas y se pueden consultar con `history --limit 1000`.

## Instalación

Linux con escritorio gráfico, Python 3.10+, Tk y Pillow. Esta instalación usa las bibliotecas que ya hay en el equipo; no descarga dependencias.

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
- Sin puertos de red, cuentas, claves API ni llamadas a modelos.

Los directorios XDG se respetan. `VISOR_DATA_DIR` y `VISOR_RUNTIME_DIR` permiten aislar datos y procesos, por ejemplo durante pruebas. El historial permanece en disco hasta que lo borres.

## Desarrollo y pruebas

```bash
python3 -m unittest discover -s tests -v
python3 /home/javier/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/agent-visor
```

Las pruebas de integración gráfica se omiten si no hay acceso al escritorio. Con escritorio prueban el comando real, arranque simultáneo, reutilización, actualizaciones, cierre y restauración.

Para desinstalar, cierra con `visor stop` y elimina el comando, los dos enlaces a habilidades y `~/.local/share/applications/agent-visor.desktop`. Los datos quedan separados para que puedas conservarlos.
