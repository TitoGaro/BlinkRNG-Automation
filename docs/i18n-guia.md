# Guía de traducción (español / inglés)

La aplicación es bilingüe. Los textos van en el código con sus dos versiones juntas:

```python
from rngauto.i18n import t
label.setText(t("Conecta la Switch", "Connect the Switch"))
msg = t("Faltan {n} avances", "{n} advances left", n=remaining)   # con formato
```

- `t(es, en, **fmt)` devuelve el texto del idioma actual (`rngauto.i18n.language()`), formateado con
  `str.format` si se pasan argumentos. No uses f-strings dentro de `t`: usa `{nombre}` y argumentos.
- Llaves literales dentro de un texto con formato: `{{` y `}}`.
- Textos de los encuentros (TOML): claves `*_en` junto a las españolas (p. ej. `display_name_en`); el cargador
  elige según el idioma.

## Reglas

1. **El texto español no cambia ni una letra** (los tests lo comprueban): solo se envuelve en `t(...)`.
2. **Nunca** se decide nada comparando textos de mensajes; usa tipos de excepción o campos (`data=`).
3. **Terminología oficial de Pokémon** en cada idioma: Canalave City (Ciudad Canal), Fullmoon Island (Isla
   Plenilunio), Ramanas Park (Parque Hansa), Snowpoint Temple (Templo Puntaneva), Spear Pillar (Columna Lanza),
   Hall of Origin (Sala del Origen), Pokédex, Master Ball, Bag (Bolsa), Medicine (Botiquín), Summary (Datos),
   Trainer Memo (Notas del Entrenador), Rowan (Serbal), naturalezas en inglés (Modest, Naive, Timid…), Slate
   (Losa), IVs, PID, shiny, seed, advance(s), blink(s).
4. **Tono:** profesional, conciso y directo, como una herramienta de escritorio seria. Sin emojis, sin signos de
   exclamación innecesarios, sin frases de relleno («¡Genial!», «No te preocupes»). En inglés, frases naturales,
   no traducciones literales; imperativo corto en botones («Start», «Reconnect», «Mark eye»).
5. Los mensajes de registro técnico (`ctx.log`) también se traducen.
6. Identificadores, claves de configuración, nombres de ficheros y valores guardados en disco **no** se traducen.
