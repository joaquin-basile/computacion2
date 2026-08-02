# Monitor de Procesos y Threads

**Computación II — Universidad de Mendoza — 2026**
**Alumno: Joaquín Basile**

Monitor de procesos en tiempo real, estilo `htop`, con énfasis en la **anatomía interna** de cada proceso y sus threads: memoria, file descriptors, threads, máscaras de señales y scheduling. Toda la información se extrae leyendo directamente el filesystem `/proc` — **sin `psutil`** ni herramientas equivalentes.

---

## Descripción general

El monitor es un **sistema multiproceso**: un recolector central lista los PIDs de `/proc` y distribuye el trabajo entre **7 analizadores especializados** que corren en paralelo (cada uno con su propio intervalo de refresco); un agregador vigila que el snapshot global esté fresco; y una **TUI con curses** muestra los datos en 7 vistas alternables. El intervalo de cada vista se ajusta en vivo con `+` / `-`.

Para correr:

```bash
docker compose run --rm monitor
```

Teclas principales: `1`–`7` cambian de vista, `↑`/`↓` navegan la lista, `Enter` pinnea un proceso, `/` filtra por comando, `u` por usuario, `c` cambia el ordenamiento, `+`/`-` ajustan el intervalo de la vista activa, `q` sale.

---

## Arquitectura

```
                 ┌─────────────────────────────────────────────┐
                 │      SNAPSHOT GLOBAL (Manager.dict)         │
                 │  resumen | memoria | fds | threads |        │
                 │  senales | scheduling | sistema  + _ts      │
                 └────────▲────────────────────────▲───────────┘
                          │ escriben (clave disjunta)│ lee (1 copia
        ┌─────────────────┴──────────┐  ┌────────────┴────┐   por repaint)
        │                            │  │                 │
 ┌──────▼───────┐   ┌─────────────┐  │  │ ┌─────────────┐ │
 │  collector   │──▶│ 7 analyzers   │  │  │   display    │ │
 │  lista PIDs  │   │ (procesos,   │  │  │  TUI curses  │ │
 │  cada 2s     │   │  ritmo propio│  │  │  (proceso    │ │
 └──────────────┘   └─────────────┘  │  │   main)      │ │
                                     │  └─────────────┘ │
                              ┌──────┴─────┐  ┌─────────┴──────┐
                              │  aggregator │  │  self-pipe     │
                              │  stale ≥30s│  │  señales (OS)  │
                              └────────────┘  └────────────────┘
```

En total corren **11 procesos**: el proceso `main` (que ejecuta la TUI), el servidor interno del `Manager`, el recolector, el agregador y los 7 analizadores. Los hijos ignoran las señales de control (`SIG_IGN`, main.py:41-42) y redirigen su stdout/stderr a `logs/monitor.log` para no ensuciar el TTY (main.py:38-40).

Comunicación entre componentes:

| Flecha | Mecanismo | Por qué |
|--------|-----------|---------|
| recolector → analizadores | 1 `Queue` por vista (main.py:84) | Productor único, consumidores múltiples. El recolector publica la misma lista de PIDs en las 7 colas (collector.py:31-35); cada analizador hace `drain_queue` *latest-wins* (base.py:20-32): descarta ticks atrasados, sin backpressure |
| analizadores → snapshot | `Manager.dict` (main.py:86) | Proxy con lock interno del server; cada analizador escribe **solo su clave** con reemplazo atómico del sub-dict en una asignación (`snapshot["resumen"] = result`, summary.py:105) + timestamp en `_ts` |
| display → analizadores | `multiprocessing.Value("d")` por vista (main.py:79-82) | Ajuste de intervalos en vivo: el display escribe `intervalo.value` y el loop del analizador lo relee en cada tick (base.py:44) |
| display → shutdown | `Manager.Event` (main.py:76) | Flag cooperativo de salida, chequeado por todos los loops en chunks de 0.2s |
| OS → main | self-pipe + `signal.set_wakeup_fd` (signals.py:64-68) | Ver sección de señales |

---

## Interfaz (TUI)

La pantalla tiene 4 zonas: header (título, uptime, modo verbose, edad de cada analizador), lista de procesos (45% del alto), panel de detalle que cambia según la vista activa, y footer con los keybindings. Si no hay TTY disponible, el display cae a un fallback plain-print de top-10 por CPU (display.py:1057-1087).

| Tecla | Acción |
|-------|--------|
| `1`–`7` / `r m f t s p g` | Cambiar de vista |
| `↑` `↓` | Navegar la lista de procesos |
| `Enter` | Pin del proceso seleccionado (sobrevive reordenamientos; se suelta si el proceso muere) |
| `/` | Filtrar por nombre de comando |
| `u` | Filtrar por usuario (username o uid) |
| `c` | Ciclar orden: CPU% → RSS → PID |
| `+` / `-` | Subir / bajar intervalo de la vista activa (clamp contra `intervalo_minimo` de config, máx 60s) |
| `Esc` | Limpiar filtros |
| `h` / `?` | Ayuda |
| `q` | Salir limpiamente |

### Las 7 vistas y sus datos

| # | Tecla | Vista | Datos | Intervalo (min) |
|---|-------|-------|-------|-----------------|
| 1 | `1`/`r` | Resumen | PID, PPID, UID/GID + usuario, estado, comm, cmdline, CPU%, threads | 2s (0.5) |
| 2 | `2`/`m` | Memoria | VmSize/RSS/Data/Stk/Exe/Lib/HWM/Swap, minor/major faults, segmentos agrupados (text/data/heap/stack/shared) desde `/proc/<pid>/maps` | 3s (1) |
| 3 | `3`/`f` | File descriptors | Lista de FDs, destino (`readlink`) y tipo inferido (tty/socket/pipe/file) | 5s (2) |
| 4 | `4`/`t` | Threads | LWPs de `/proc/<pid>/task`: tid, nombre, estado, CPU% por thread, context switches | 2s (0.5) |
| 5 | `5`/`s` | Señales | SigBlk, SigIgn, SigCgt, SigPnd, ShdPnd decodificados a nombres legibles (SIGTERM, SIGINT, ...) | 10s (5) |
| 6 | `6`/`p` | Scheduling | nice, priority, policy (OTHER/FIFO/RR/...), RT priority, affinity, ctx switches, utime/stime, SID/PGID | 10s (5) |
| 7 | `7`/`g` | Sistema | CPU global con breakdown, load average, memoria/swap, procesos por estado, uptime/btime, top-3 por CPU y RSS | 2s (1) |

---

## Decisiones de diseño

### ¿Por qué `Manager.dict` y no `Value`/`Array` para el snapshot?

El snapshot es un dict anidado de estructura variable (una subclave por vista, con per-PID adentro). `Value`/`Array` son de tipo fijo y no lo modelan. `Manager.dict` aporta dos cosas: serializa cada operación sobre el proxy (lock interno del server) y permite reemplazo atómico del sub-dict completo en una sola asignación. Como cada analizador escribe **solo su propia clave** y los timestamps en `_ts`, nunca hay dos procesos escribiendo el mismo lugar: **no hace falta ningún lock explícito** (no hay `Lock`/`RLock` en todo `src/`). El display lee una copia local una vez por repaint (display.py:1030-1041), a 10 Hz máximo, para no castigar al proxy.

`Value`/`Array` sí se usan donde el dato es un escalar: los intervalos (`Value("d")`) y el flag verbose (`Value("b")`, main.py:93). Con `fork` como start method (main.py:232) los hijos heredan estos objetos por copia de memoria, sin serialización ni servidor intermedio; una lectura/escritura simple de `Value` es atómica.

### Race conditions: cómo se manejaron

- **El filesystem muta bajo los pies**: el recolector lista los PIDs una sola vez por tick y reparte esa "foto" congelada (collector.py:31-35); los analizadores nunca releen `/proc` para listar. Si un proceso muere a mitad de lectura, se tolera `FileNotFoundError`/`ProcessLookupError`/`PermissionError` y se salta (summary.py:26-32). Los estados internos (previos de jiffies, PIDs vistos) se podan cada tick.
- **Backpressure de colas**: `drain_queue` toma solo el **último** ítem y descarta los viejos (base.py:20-32); el recolector ignora `queue.Full` y sigue (collector.py:34-35).
- **Snapshot**: escritores disjuntos + reemplazo atómico + serialización del Manager (ver arriba).
- **Shutdown**: todos los loops pollean el `Event` en chunks de 0.2s; `main` hace `join(timeout=3)` y `terminate()` de emergencia (main.py:199-206), drena las colas y cierra el Manager en un `finally` (main.py:226-227).
- **Señales**: ver sección siguiente — nada no-async-signal-safe se ejecuta dentro de un handler.

### ¿Por qué self-pipe y `set_wakeup_fd`?

`Manager.Event.set()` y otras primitivas de multiprocessing **no son async-signal-safe**: llamarlas desde un handler puede dejar el proceso en deadlock o corromper estado. Por eso los handlers registrados son **no-op** (solo evitan la acción por defecto) y el runtime de CPython, vía `signal.set_wakeup_fd`, escribe un byte con el número de señal en un pipe (`O_NONBLOCK`, signals.py:64-68). Un thread daemon (`signal-reader`) lee el pipe y ejecuta toda la lógica (shutdown, reload, dump, toggle verbose) en contexto seguro. Es el patrón *self-pipe* visto en clase 6.

### Intervalos por defecto

Resumen 2s, threads 2s y sistema 2s son datos que cambian rápido y baratos de leer. Memoria 3s y FDs 5s son más caros (hay que recorrer `/proc/<pid>/maps` o cada FD con `readlink`). Señales y scheduling 10s: las máscaras de señales y las políticas de scheduling cambian poco, y la vista es legible con datos de hace 10 segundos. Todos se ajustan en vivo con `+`/`-` y desde `config.json` con SIGHUP.

### ¿Por qué curses y no rich?

`rich` aporta layout por bloques pero con menos control fino del terminal raw, y agrega una dependencia; con curses la entrada raw (`nodelay` + `getch` polled a 10 Hz), el redimensionamiento (`KEY_RESIZE`) y el fallback sin TTY se manejan de una. Además `requirements.txt` queda vacío: cero dependencias, el contenedor es mínimo.

---

## Señales del monitor

| Señal | Acción |
|-------|--------|
| **SIGINT** (Ctrl+C) / **SIGTERM** | Shutdown limpio: `event.set()` → todos los procesos salen, se joinean (con terminate de emergencia), se drenan las colas y se cierra el Manager |
| **SIGHUP** | Recarga `config.json`: actualiza intervalos en los `Value` compartidos, `intervalo_minimo` y filtros default en el `shared_config` (signals.py:117-170); la TUI los polea por tick y los aplica si cambiaron |
| **SIGUSR1** | Dump del snapshot completo a `dump_<unix_ts>.json` (signals.py:173-203), aplanando los proxies del Manager a dicts puros |
| **SIGUSR2** | Toggle modo verbose: más FDs visibles en la vista fds; se muestra `verbose=on/off` en el header |
| **SIGWINCH** | No se registra: curses lo entrega como `KEY_RESIZE` y el display repinta (display.py:1001-1007) |

Solo el proceso `main` maneja señales; los hijos las ponen en `SIG_IGN` al arrancar (main.py:41-42).

---

## Conceptos del curso aplicados

| Concepto (clase) | Dónde se aplica |
|------------------|-----------------|
| Anatomía de procesos y `/proc` (clase 3) | `src/procfs.py` parsea `stat`, `status`, `cmdline`, `maps`, `task`, `fd`; la división parser-puro / I/O permite testear los parsers fuera de Linux |
| fork, exec, wait — zombies (clase 4) | La vista Sistema cuenta procesos por estado leyendo el campo `State` de `/proc/<pid>/stat` (system.py:73-87): un zombie es un proceso terminado cuyo padre todavía no llamó a `wait()`, y aparece como `Z` |
| Pipes y file descriptors (clase 5) | La vista FDs recorre `/proc/<pid>/fd` con `os.readlink` para inferir el destino; las colas del recolector son pipes a nivel de kernel con buffers |
| Señales, máscaras, async-signal-safe (clase 6) | self-pipe + `set_wakeup_fd` para coordinar señales con el loop; la vista Señales decodifica las máscaras hex de 64 bits de `status` con el layout del kernel `bit (N-1) == señal N` (procfs.py:583-608) |
| Memoria compartida (clase 7) | `Manager.dict`/`Value` como capa de memoria compartida entre procesos; segmentos de `maps` agrupados por permisos y `[heap]`/`[stack]` (procfs.py:376-452) |
| Multiprocessing: `Process`, `Queue`, `Manager`, `Value` (clases 8-9) | `spawn()` en main.py:29-47; cada analizador es un `Process` con su ritmo; los intervalos viajan por `Value` para ajuste en vivo |
| Threading, GIL, LWPs (clase 10) | La vista Threads lee `/proc/<pid>/task/<tid>/*`: cada thread de Python (o de C) es un LWP visible en `/proc`, con su propio estado y jiffies; el CPU% por thread usa delta de jiffies igual que el proceso |

Detalle de CPU%: se calcula como delta de jiffies entre lecturas (`utime + stime`) dividido el tiempo transcurrido × `os.sysconf("SC_CLK_TCK")` (procfs.py:615-639); el resultado es "% de un núcleo" (100 = un core al máximo). Los previos se guardan por PID (summary.py:72) y por tupla `(pid, tid)` para threads (threads.py:52), porque un tid puede reciclarse entre pids.

---

## Limitaciones conocidas

- **Un analizador muerto no se reinicia**: si un proceso analizador muere (p. ej. `kill`), su clave del snapshot queda stale; el agregador loguea `WARN` cuando la edad supera 30s (aggregator.py:33-43), pero el monitor no lo revive.
- **Dos SIGUSR1 en el mismo segundo**: el segundo dump pisa al primero (mismo nombre de archivo `dump_<ts>.json`).
- **Ctrl+C en corrida nativa** (fuera de Docker): el broadcast de SIGINT puede matar el servidor del Manager y dejar hijos huérfanos; dentro del contenedor la señal llega solo al proceso principal.
- **Ajuste de intervalo con `+`/`-` concurrente con SIGHUP**: el read-modify-write del `Value` no es atómico contra la escritura del thread de señales — *last-writer-wins*, benigno.
- **Procesos de otros usuarios**: sin privilegios, algunos archivos de `/proc/<pid>` dan `PermissionError` y se omiten (sin dañar el resto del snapshot).

---

## Cómo correr

```bash
docker compose run --rm monitor
```

`docker compose up --build` no funciona con esta TUI interactiva: es necesario `run` para que el contenedor sea interactivo (stdin abierto + TTY), como está configurado en `docker-compose.yml`.

El `Dockerfile` usa `python:3.11-slim` con el entrypoint `python -u src/main.py` (sin dependencias); el compose monta `./src` y `./config.json` en bind-mount para iterar sin rebuild.

- **Logs**: `logs/monitor.log` — todos los procesos escriben ahí (sus stdout/stderr se redirigen con `dup2` para no ensuciar la TUI).
- **Dumps**: `dump_<unix_ts>.json` tras SIGUSR1.
- **Config**: `config.json` — intervalos, intervalos mínimos y filtros default; recargable con `kill -HUP <pid>`.
- **Otras señales**: `kill -USR1 <pid>` (dump), `kill -USR2 <pid>` (verbose), `kill -TERM <pid>` (shutdown limpio).
