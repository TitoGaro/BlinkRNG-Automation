# Arquitectura

Cómo está organizado RNG Automation y por qué. Para usar la aplicación, ver la
[guía de uso](03-guia-puesta-en-marcha.md).

## 1. Principios

1. **Responsabilidades separadas**:
   - la búsqueda de objetivos no sabe pulsar botones;
   - el mando no sabe qué es Xorshift;
   - la visión no sabe qué naturaleza se quiere.
2. **Observar antes de actuar**: cada acción relevante tiene una confirmación con la imagen o un plazo. Ante
   una duda seria, el programa **se para** y lo dice; nunca sigue pulsando a ciegas.
3. **Reproducible**: todo lo temporal pasa por un reloj inyectable. La consola real, la Switch simulada y el
   replay de grabaciones comparten el mismo código.
4. **Datos, no `if pokemon == ...`**: cada encuentro es un TOML (`EncounterConfiguration`) que ejecuta una
   estrategia genérica.
5. **Explicable**: cada decisión deja un evento en el registro con su porqué (advance actual, objetivo,
   margen, confianza).
6. **Sin sobrearquitectura**: abstracciones solo donde ya hay dos implementaciones (real y simulada).

## 2. Vista por capas

```text
                 gui · app  (aplicación guiada, CLI, sesión de hardware)
                        │   orquesta, diagnostica, persiste
        ┌───────────────┼──────────────────────────────┐
        │             core  (independiente del juego)  │
        │ clock · timeline · tracker (AdvanceClock)     │
        │ target · calibration · statemachine          │
        │ encounter (configuración TOML)               │
        └───────────────┬──────────────────────────────┘
                        │ implementa contratos
              games/bdsp
              xorshift · blink · recovery · scene (simulación de la escena)
              static_gen · encounters · planner · fast_advance
              static_strategy + static_run/ (fases de la ejecución)
              screens · shiny · battle · summary (pantallas del juego)
                        │ usa
        ┌───────────────┴──────────────┐
      vision                         control
      frames · capture · replay      controller (contrato)
      blink_signal · screen · ocr    esp32 (PABotBase2) · RecordingController
        │                              │
   capturadora                     ESP32 → Switch
```

Reglas de dependencia: `core` no importa nada de `games`, `vision` ni `control`; `vision` y `control` no
importan `games`. Solo `app` y `gui` conocen las implementaciones concretas: `app/session.py` monta las
dependencias (`StaticDeps`) y se las pasa a la estrategia.

## 3. Módulos

| Módulo | Responsabilidad |
|---|---|
| `core/gf2.py` | Sistemas lineales sobre GF(2) con espacio nulo (recuperar el estado del RNG) |
| `core/pokemon.py`, `target.py`, `observation.py`, `stats.py` | Rasgos de un Pokémon, criterios del objetivo, contrato de búsqueda, estadísticas ↔ IVs |
| `core/clock.py`, `timeline.py`, `tracker.py` | Reloj real o simulado, registro de eventos (JSONL), `AdvanceClock` con incertidumbre |
| `core/calibration.py` | Historial de intentos (confirmados e inciertos), votación y estimación del delay final |
| `core/statemachine.py` | Estados con plazo, reintentos, intervención y parada inmediata |
| `core/encounter.py` | Carga y validación de los TOML de encuentros |
| `games/bdsp/xorshift.py`, `blink.py`, `recovery.py` | RNG de BDSP, modelo de parpadeo y recuperación del estado |
| `games/bdsp/blink_pipeline.py`, `reidentify_pokemon.py`, `scene.py` | Parpadeos → estado → reloj; reidentificación con el legendario en escena; simulación por eventos |
| `games/bdsp/static_gen.py`, `encounters.py`, `identify.py` | Generación de estáticos, búsqueda de objetivos y del advance obtenido |
| `games/bdsp/planner.py` | Plan de pulsaciones con márgenes (centro del periodo y del hueco) |
| `games/bdsp/fast_advance.py`, `data/fast_advance.toml` | Avance rápido con la Pokédex (acciones como datos) y lo aprendido de cada acción |
| `games/bdsp/screens.py`, `shiny.py`, `shiny_sound.py`, `battle.py`, `battle_clip.py`, `summary.py` | Pantallas del juego: menú, diálogos, shiny (destellos y sonido), Master Ball, ficha |
| `games/bdsp/static_strategy.py` | `StaticEncounterRun`: combina las fases y define la máquina de estados |
| `games/bdsp/static_run/` | Fases de la ejecución (ver §4) |
| `vision/` | Captura con hilo propio, señal del ojo, firmas de pantallas, OCR de Windows, grabación y replay, imágenes con cualquier ruta |
| `control/` | Contrato `Controller`, cliente PABotBase2 para el ESP32 y mando de ensayo que no pulsa nada |
| `app/` | Sesión de hardware, preparación guardada, detección de puertos y capturadoras, CLI, Switch simulada |
| `gui/` | Aplicación guiada de 6 pasos, en español e inglés |

### `static_run/`: la ejecución por fases

`StaticEncounterRun` (en `static_strategy.py`) hereda de varios *mixins*, uno por fase. Todos comparten el
mismo estado (`self.d`, `self.cfg`, `self.status`), que crea `RunBase`.

| Módulo | Contenido |
|---|---|
| `config.py` | Modos, `RunStatus` (lo que ve la interfaz), `StaticRunConfig`, `StaticDeps` y constantes compartidas |
| `base.py` | `RunBase`: vídeo, ojo, mensaje de la escena, pulsaciones, comprobación de pantallas |
| `seed.py` | `SeedMixin`: COLLECT, PLAN, FAST_ADVANCE, REIDENTIFY y WAIT_TRIGGER |
| `route.py` | `RouteMixin`: pasos de macro (diálogos, andar y hablar), ruta hasta el legendario y TIMELINE |
| `front.py` | `FrontMixin`: reidentificación y re-planificación delante del legendario |
| `capture.py` | `CaptureMixin`: ENCOUNTER, CATCH y VERIFY |

## 4. Flujo de un intento

```text
PRECHECK ─► COLLECT ─► PLAN ─► [FAST_ADVANCE ─► REIDENTIFY ─► PLAN]* ─► WAIT_TRIGGER ─► TIMELINE
              │          │                                                  │              │
        parpadeos →   objetivo y                                    ruta hasta el   pulsación final
        estado (+ con-  plan con                                    legendario (y   en el centro
        firmación)     márgenes                                     reidentificar   del hueco
                                                                    delante si hace
                                                                    falta)
TIMELINE ─► ENCOUNTER ─┬─ shiny ─► SE PARA (el usuario lo captura)
                       └─ no ────► CATCH (Master Ball, ficha) ─► VERIFY (advance obtenido, calibración) ─► RESET ─► COLLECT
```

Cada estado tiene plazo y número de reintentos. La **parada** cancela la cola del ESP32 y deja el mando en
neutro al instante, sin esperar a que termine una acción larga. Si algo no cuadra y el juego se puede
reiniciar sin riesgo, se reinicia solo (con un tope de reinicios seguidos); si no, se para y avisa.

### Avance rápido con la Pokédex

Si la espera hasta el objetivo es larga, el programa abre la Pokédex y la recorre el tiempo justo (cada
movimiento por la lista gasta RNG). Después reidentifica el estado con unos 7 parpadeos, buscando el patrón
cerca de lo esperado, y lo confirma con 2 más. Repite con recorridos más cortos hasta que la espera es corta.

- **Elección** (`choose_method`): la acción y su duración que más avanzan sin pasarse, contando su
  incertidumbre y lo que avanza el juego mientras tanto. Si se pasa, PLAN busca el siguiente objetivo.
- **Aprendizaje** (`AdvanceStats`): cuánto consume cada acción en la partida de cada usuario.
- **Seguridad**: cada paso se comprueba con la imagen. Nunca se usa la cruceta sin ver el menú abierto (si
  no, movería al personaje) ni se recorre la Pokédex sin reconocerla.

## 5. Modelo temporal

- **Reloj único**: `time.perf_counter()`. Cada frame lleva su instante de llegada; cada comando, el de envío.
- **Advance a partir del tiempo**: `AdvanceClock` (ancla, periodo, llamadas por tirada). El periodo de las
  tiradas se estima en cada ejecución y la incertidumbre crece con la distancia al ancla. Los parpadeos
  posteriores vuelven a anclar el reloj.
- **Timeline**: tras la pulsación de inicio, el RNG avanza según la simulación por eventos (`scene.py`),
  determinista dado el estado. El planificador busca el hueco entre la llamada `objetivo − delay` y la
  siguiente, y pulsa **en su centro**.
- **Latencias**: el ESP32 ejecuta cada pulsación desde su propia cola, con precisión de milisegundos. El
  resto del desfase (vídeo y Bluetooth) lo absorbe la calibración del delay final con cada intento.

## 6. Configuración de encuentros

`encounters/bdsp/<id>.toml` describe un encuentro sin código. Los propios ficheros están comentados para
servir de plantilla.

| Bloque | Contenido |
|---|---|
| `[encounter]` | id, versión, plantilla de generación, juegos (BD/SP), nombres, `status` (comprobado o no) |
| `[start]` | punto de inicio e instrucciones para el usuario |
| `[blink_scene]` | caja del ojo, mensaje de la escena, parpadeos necesarios, NPC que gastan RNG |
| `[approach]` | ruta hasta el legendario (pasos de macro) y, si hace falta, reidentificación delante |
| `[[timeline]]` | fases tras la pulsación de inicio: lecturas en blanco, pausa, escena |
| `[final]` | botón final, delay final inicial (se calibra solo) y márgenes |
| `[fast_advance]` | acciones de Pokédex permitidas |
| `[verification]` | qué se comprueba del Pokémon obtenido |
| `[recovery]` | cómo reiniciar el juego |

Añadir un Pokémon es añadir su TOML. El estado comprobado/sin probar vive en el propio TOML; un test
comprueba que los README coinciden.

## 7. Datos del usuario

Todo en `%USERPROFILE%\.rngauto`:

| Carpeta o fichero | Contenido |
|---|---|
| `preferences.json` | Idioma, puerto, volumen, último encuentro… |
| `setups/` | Ojos marcados (plantilla y zona de búsqueda) |
| `calibration/<encuentro>.json` | Historial de intentos y delay aprendido |
| `fast_advance/`, `screens/`, `sonidos/` | Lo aprendido de la Pokédex, firmas de pantallas y sonido shiny |
| `sessions/<fecha>-<encuentro>/` | Registro de cada ejecución (`timeline.jsonl`, `attempts.jsonl`) y grabación del ojo |
| `combates/` | Vídeo y audio de cada entrada en combate |
| `logs/` | Errores inesperados |

Las escrituras importantes son atómicas (fichero temporal y renombrado) y un fichero dañado no impide
arrancar: se ignora o se aparta como `.bad`.

## 8. Decisiones

| Decisión | Motivo |
|---|---|
| **Python** con numpy y OpenCV | La precisión de milisegundos la pone el ESP32; en el PC basta con décimas. Iteración rápida y tests |
| Firmware **PABotBase2** en el ESP32 con versión fijada | La temporización se hace en el dispositivo; el protocolo está probado |
| **Reloj inyectable** y mando de ensayo | Ensayo, replay y tests deterministas |
| Delays como **valor inicial + calibración** | Varían por montaje; cada intento corrige el siguiente |
| Pulsaciones **en el centro del hueco** | Tolerancia a la latencia y al jitter |
| **Avance rápido + reidentificación** | Esperas de horas pasan a minutos |
| **Nada espera para siempre** | La parada debe funcionar siempre y la interfaz no puede quedarse colgada |
| Encuentros en **TOML**, datos en **JSON/JSONL** | Sin dependencias, legible y versionable |
| OCR de **Windows** para las cifras de la ficha y colores para la naturaleza | Sin instalar nada más |

## 9. Otros juegos

Un juego nuevo aportaría su `games/<juego>/` (RNG, modelo de observación, búsqueda de objetivos, pantallas y
estrategias). `core`, `control` y `vision` se reutilizan sin cambios, y el solver GF(2) sirve para cualquier
RNG lineal.
