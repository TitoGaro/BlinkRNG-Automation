# Guía de uso

[English](user-guide.md)

Dónde guardar para cada legendario, con capturas: [guía de encuentros](guia-encuentros.md).

## Abrir la aplicación

Haz doble clic en **`RNG Automation.bat`** (en la carpeta `rng-automation`). Se abre una ventana
con seis pasos a la izquierda. Solo puedes avanzar cuando el paso actual está listo; si falta algo,
abajo a la derecha se te dice qué.

El idioma (Español o English) se elige abajo en la barra lateral izquierda. La primera vez sigue el idioma de
Windows. No se puede cambiar mientras hay una ejecución en marcha.

> **Todo en uno:** no necesitas otros programas. La imagen y el sonido del juego se ven y se oyen
> en la aplicación (botón «Ver juego» arriba, con pantalla completa al hacer doble clic; el botón del altavoz
> que hay al lado activa o silencia el sonido). Si tienes un programa de grabación o streaming abierto con la capturadora, ciérralo: Windows no
> deja que dos programas la usen a la vez.

## Antes de empezar (una sola vez)

- **Mando (ESP32):** grábale el firmware PABotBase2 y emparéjalo con la Switch siguiendo las
  [guías de Pokémon Automation](https://pokemonautomation.github.io/ControllerList.html) (opción «ESP32»).
- **Switch:** desactiva el modo de espera automático (Configuración de la consola → Modo de espera). Las
  esperas hasta un shiny pueden durar horas. El programa toca el stick derecho cada 3 minutos para que no
  se atenúe la pantalla, pero no debe suspenderse.
- **Perfil de usuario:** en la consola, tu perfil debe ser el único o el primero de la izquierda. Al
  reiniciar el juego, el programa lo elige pulsando A.
- **PC:** mientras la aplicación trabaja, Windows no se suspende ni apaga la pantalla (lo gestiona el
  programa). Deja el portátil enchufado.
- **Mando físico:** no lo uses mientras el programa trabaja. La Switch atendería a los dos mandos.
- **Partida (para que capture y lea la ficha solo):**
  - equipo de **5** Pokémon (el capturado queda en el 6.º puesto), con el **primero no shiny**;
  - al menos **una Master Ball** en la Bolsa (la busca por su icono, esté donde esté en el bolsillo);
  - en las opciones del juego, **«Poner mote» desactivado** y **velocidad del texto rápida**;
  - conviene que ningún Pokémon del equipo vaya a aprender un movimiento ni a evolucionar al capturar. Si
    pasa, el programa elige «Conservar movimientos», pero pierde tiempo;
  - en los encuentros que se observan con el mensaje del profesor Serbal (todos salvo Dialga y Palkia), la
    **caña registrada** en la rueda de «+». En el paso Escena indicas en qué dirección de la rueda está;
  - la partida guardada en el punto de inicio, mirando a la cámara. El programa nunca guarda: al reiniciar
    se vuelve a ese punto.

## Los seis pasos

| Paso | Qué haces | Qué hace el programa |
|---|---|---|
| **1. Objetivo** | Eliges el Pokémon, si lo quieres shiny, la naturaleza, las IVs y el modo. «Avance rápido con la Pokédex (recomendado)» viene activado | Te dice lo raro que es ese objetivo y cuánto se tardará en llegar a él |
| **2. Conexión** | Enchufas el ESP32 y la capturadora; si hace falta, emparejas el mando en «Cambiar agarre/orden» | Los encuentra y los conecta solo, muestra la imagen, activa el sonido y te dice qué falta |
| **3. Escena** | Te colocas donde indica, pulsas «Congelar imagen para marcar el ojo», recuadras el ojo con el ratón y pulsas «Guardar este ojo». Si el encuentro usa la caña, eliges en «¿Dónde está la caña en la rueda de «+»?» su dirección. Con **Regigigas, Cresselia, Darkrai y Heatran** son **dos ojos**: el del punto de inicio y otro delante del legendario (sin guardar: vas hasta él, marcas y vuelves a cargar) | Guarda la plantilla del ojo (solo hace falta una vez por encuentro). Delante del legendario el programa se vuelve a situar teniendo en cuenta también sus parpadeos (gastan RNG) |
| **4. Detección** | Esperas ~1–2 min quieto | Muestra una lupa del ojo en directo (marco rojo al parpadear), la gráfica y los parpadeos que detecta. Con 3 parpadeos detectados puedes seguir (o marcando «Lo he comprobado y quiero continuar igualmente») |
| **5. Ejecutar** | Pulsas **Empezar** y no tocas el mando | Lee el RNG, busca el objetivo, pulsa en el instante exacto, mira si es shiny, captura, lee la ficha, calibra y reinicia, una y otra vez |
| **6. Historial** | — | Intentos y calibración aprendida |

### Durante la ejecución

- **Parpadeos (barra de progreso):** en la mayoría de encuentros necesita 33 parpadeos (hasta 40 si con 33
  no basta) y 2 más para confirmar; en Arceus, Dialga, Palkia y Uxie, 40 y 6 más. Son unos 4–6 minutos. La
  cuenta avanza desde el primer parpadeo; durante los primeros ~3 minutos va con «≈» porque el detector aún
  está ajustando su sensibilidad a tu ojo (al terminar puede corregirse en uno o dos).
- **Plan:** en modo **Asistido** aparece una ventana con el objetivo y el tiempo de espera; acéptala.
  Desde ese momento el programa pulsa **solo**, en el instante exacto. Si no respondes a tiempo, la
  ventana se cierra sola y el plan se recalcula. En este modo también te pregunta antes del primer avance
  rápido.
- **Avance rápido:** si falta mucho para el objetivo, el programa abre la Pokédex, recorre sus fichas el
  tiempo justo según lo que falte y la cierra. Después **reidentifica** el estado con unos 7 parpadeos y lo
  confirma con 2 más. Repite con recorridos cada vez más cortos (al final solo entra y sale) hasta que la
  espera es corta. Si se pasa del objetivo, va a por el siguiente que cumpla tus criterios.
  - La primera vez te pregunta si ve bien la Pokédex en la imagen: responde «Sí» y la reconocerá solo.
  - Llega solo a la Pokédex, esté donde esté el cursor del menú: X, «+» (mapa), B (el cursor queda en
    «Mapa», justo debajo de la Pokédex), ARRIBA y A.
  - Comprueba con la imagen cada paso: que el menú está abierto antes de usar la cruceta (si no, ARRIBA
    movería a tu personaje) y que se ve la Pokédex antes de recorrerla. Si algo no cuadra, **no sigue
    pulsando**: cierra lo abierto y continúa esperando de forma normal.
  - Aprende cuántos avances da cada acción en tu partida y lo guarda para la próxima vez.
- **Cuenta atrás:** muestra el tiempo hasta la próxima pulsación (y la hora aproximada). Puedes dejarlo
  trabajando.
- **Combate:** lo detecta solo y mira si el Pokémon es shiny: los destellos en la imagen, lo que dura la
  animación de entrada y, cuando ya lo ha aprendido, el sonido de las estrellas.
  - **Si es shiny, se para** con el menú de combate en pantalla, avisa y no vuelve a tocar el mando: lo
    capturas tú. Guarda la grabación de ese combate y aprende el sonido para las siguientes veces.
  - **Si no lo es**, lo captura deprisa: Bolsa, bolsillo de Poké Balls, Master Ball (por su icono), Usar.
- **Ficha:** pasa los textos (si alguien quiere aprender un movimiento, elige «Conservar»), abre la ficha
  del capturado y lee la naturaleza y las estadísticas, sin depender del idioma del juego. Las cifras las
  lee con el OCR de Windows. La naturaleza la saca de los colores: rojo para la que sube y azul para la que
  baja; si no hay colores, es neutra y lee cuál en «Notas del Entrenador» (en cualquier idioma del juego; si no
  la reconoce, sigue con «una de las 5 neutras»). Con eso deduce las IVs y qué
  advance obtuviste.
- **Calibración y reinicio:** ajusta el *delay* con lo obtenido, cierra el juego desde HOME (sin guardar),
  lo vuelve a abrir y repite. Si no puede leer la ficha, te pide los datos en una ventana.
- **Detener** (botón rojo arriba, o tecla **F12**): detiene al instante lo que esté en marcha, cierra las
  ventanas pendientes y deja el mando en neutro. Las ventanas del programa nunca bloquean la parada.

### Latencia

No hace falta medirla. La pulsación cae en el centro de un hueco de ±0,5 s y el desfase que quede lo
absorbe la calibración del *delay* con cada intento.

### Dónde se guardan las cosas

En la carpeta `%USERPROFILE%\.rngauto\` (botón «Abrir carpeta de datos» en el Historial):

- `setups\`: los ojos que has marcado.
- `calibration\`: lo aprendido de cada encuentro.
- `fast_advance\` y `screens\`: lo aprendido del avance rápido (avances por acción y la pantalla de la
  Pokédex). «Opciones avanzadas → Volver a aprender la pantalla de la Pokédex» la olvida.
- `sessions\`: el registro detallado de cada sesión, incluidas grabaciones pequeñas del ojo
  (`ojo_XX.avi`) mientras se cuentan parpadeos. Si algo falla en la detección, sirven para analizarlo
  sin repetir la prueba.
- `logs\errores.log`: si algo falla inesperadamente.

## Si algo no va

| Síntoma | Solución |
|---|---|
| Mando «no encontrado» | Enchufa el ESP32 con un cable USB de datos; la página lo detecta sola en unos segundos |
| «El ESP32 no responde» al conectar el mando | El ESP32 necesita el firmware de Pokémon Automation (`PABotBase2-ESP32-2026092800.bin`). Cómo grabarlo y emparejarlo: [guías de Pokémon Automation](https://pokemonautomation.github.io/ControllerList.html) (opción «ESP32») |
| «Sin emparejar» | En la Switch: Mandos → Cambiar agarre/orden. Espera unos segundos: se pone en verde solo. Si la placa se queda sin corriente, hay que emparejar de nuevo |
| «Está ocupado por otro programa» | Cierra cualquier otro programa u otra ventana de esta aplicación que use el puerto y pulsa «Reconectar» |
| La capturadora no da imagen | Cierra cualquier otro programa que la use y pulsa «Elegir otra capturadora» |
| No detecta parpadeos | Recuadra el ojo más ajustado; si es muy pequeño, activa el zoom de la Switch y vuelve a marcarlo |
| «Necesito tu ayuda» | El programa ha parado porque algo no cuadra (no actúa a ciegas). Lee el mensaje, corrígelo y pulsa Empezar |
| «Avance rápido cancelado» | El juego no mostró el menú o la Pokédex a tiempo (los detalles técnicos dicen cuál). Si cambiaste de capturadora, usa «Volver a aprender la pantalla de la Pokédex» |
| Algo tarda y no responde (mando o capturadora) | Ninguna operación espera para siempre: tras unos segundos aparece un aviso y puedes reintentar. Si se repite, desenchufa y vuelve a enchufar el dispositivo |

## Estado de los encuentros

En el paso Objetivo, la lista separa los encuentros comprobados en una consola real («Comprobados en consola»)
de los que usan el mismo mecanismo pero aún no se han probado («Mismo mecanismo, sin probar aún»). La lista
actualizada también está en el [README](../README.md#encuentros).

Con un encuentro sin probar, haz primero una sesión en modo **Ensayo**: comprueba que la detección y el plan
funcionan con tu capturadora antes de pulsar nada.

## Uso avanzado (consola)

Los mismos procesos existen como comandos (`rngauto --help`): `encounters`, `targets`, `simulate`, `recover`,
`diagnose`, `run` y `record`. No hacen falta para el uso normal.

## Cresselia (y otros que no entran en combate al generarse)

Cresselia se genera **al hablarle** y huye (queda errante), así que no hay combate que vigilar. El flujo es:

1. El programa saca la semilla, va hasta ella, se reidentifica delante (sus parpadeos gastan RNG) y le habla en el
   instante exacto.
2. Aparece el aviso **«Tu turno»**: ve a capturarla tú, **sin guardar**. Con el equipo de 5, entra en el 6.º hueco.
3. Suelta el mando y pulsa **«Listo»**, estés donde estés (mapa, equipo, Bolsa…). Antes de tocar nada, el programa
   te pregunta si ha salido shiny. Si respondes «No», sale con B de lo que tengas abierto, abre el menú, va a su
   ficha, calcula en qué avance cayó, ajusta el delay y reinicia el juego para el siguiente intento.
4. Si respondes «Sí», o si por el avance obtenido puede ser el shiny, **se para** y te pide que guardes la
   partida. Nunca reinicia con el shiny en la mano. «Detener» en el aviso para la ejecución en cualquier momento.
