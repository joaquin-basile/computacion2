# Dudas y decisiones

Honestidad intelectual: lista de cuestiones que aparecieron durante el
desarrollo y cómo se resolvieron (o si quedaron abiertas). No más de un
párrafo por duda.

---

## 1. Field numbering de `/proc/<pid>/stat`

La consigna dice que `pgrp` es el campo 7 y `session_id` el campo 6.
El kernel real de Linux tiene `pgrp` en el campo 5, `session_id` en el
campo 6, y `tty_nr` en el campo 7. Decidi seguir el kernel,
`src/procfs.py::parse_stat` mapea por nombre (`"pgrp"`, `"session_id"`,
`"tty_nr"`), no por índice.

---

## 2. Decodificación de signal masks

`decode_signal_mask` sigue el kernel real: `bit (N-1) == signal N`
(`0x04` → `SIGQUIT`). Una versión previa usaba `bit N == signal N`
(`0x04` → `SIGINT`) argumentando que la consigna lo mandaba, pero la
consigna solo pide decodificar máscaras a nombres legibles; corregí el
off-by-one en la auditoría para que `SigIgn`/`SigBlk` reales se muestren
con el nombre correcto (era la pregunta 4 de los correctores).

---

## 3. Pin del proceso y navegación

La consigna dice que el pin (Enter) "no cambia aunque cambie el orden".
Lo hice persistente también al navegar con ↑/↓ (antes se perdía al
mover el cursor). Si el proceso pineado muere, el pin se suelta solo.

---

## 4. SIGHUP y filtros default

La consigna pide que SIGHUP recargue "intervalos por vista, filtros
default". config.json ahora tiene una sección `filtros` (`name`/`user`);
el reader thread de señales la reescribe en el `Manager.dict`
`shared_config` y la TUI la polea por tick, aplicando los filtros
default solo cuando el valor cambia. Los filtros interactivos (teclas
`/` y `u`) siguen funcionando igual.

---

## 5. Async-signal-safety de SIGINT/SIGTERM

SIGINT/SIGTERM pasan por el mismo wakeup pipe que SIGHUP/SIGUSR1/SIGUSR2:
el handler es no-op (el C-level escribe el byte) y el reader thread hace
`event.set()`. No se llama a `event.set()` dentro del handler porque el
proxy de `Manager.Event` usa socket + pickle + locks y no es
async-signal-safe. Quedó documentado como riesgo que en corrida nativa
(sin Docker) un `Ctrl+C` manda SIGINT a todo el process group, lo que
puede matar el proceso servidor del `Manager`; en Docker (PID 1) no
pasa. Defendible, no corregido.

---

## 6. Leaks si se mata un analizador

Si un corrector hace `kill` de un analizador: (a) su cola `Queue`
unbounded deja de ser drenada y crece — mitigado por la regla
latest-wins (el recolector sobrescribe), y (b) el proceso queda zombie
hasta el `join()` del shutdown. No hay `SIGCHLD`/`waitpid` a propósito:
el reaping ocurre en `main.py` al terminar. Es una decisión de diseño
defendible; se documenta en lugar de agregar complejidad cerca de la
entrega.

---

## 7. Ctrl+C nativo y el Manager

`Manager()` se crea antes de instalar los handlers (necesario para
pasar el `Event`). En corrida nativa, el broadcast de SIGINT al process
group puede matar el servidor del Manager antes de que el reader thread
haga `event.set()`, dejando hijos huérfanos con SIG_IGN. Es un caso
exclusivo de corrida fuera de Docker; se documenta y se defiende.
