# QA final · FP5 Cardio Cloud v15

## Estructura
- 208 campos de origen comprobados.
- 106 campos activos comprobados.
- 102 campos archivados comprobados.
- Sin solapamiento entre activos y archivados.
- Sin funciones Python duplicadas.
- `app.py` compila con `py_compile`.

## Datos reales
- El export analizado contiene 14.297 episodios y 208 columnas.
- 11.211 NHC distintos están informados; existen 4 filas sin NHC.
- Hay 2 filas que forman una pareja de duplicado exacto; se conservan como episodios distintos por `source_row`.
- La regla operativa de ingreso actual es: fecha de ingreso presente y fecha de alta ausente.

## Fechas
Las 12 fechas activas usan entrada con calendario. La interfaz muestra `DD/MM/YYYY`. Se comprueba además la cronología básica: asignación y alta no pueden preceder al ingreso.

## Desplegables
Los campos categóricos repetitivos seleccionados a partir del CSV real tienen las opciones ordenadas por frecuencia descendente y un valor personalizado de reserva:
`CAMA`, `CARDIOLOGO`, `CARDIOLOGO1`, `CENTRO`, `CSIP`, `DESTINO_AL`, `GRUPO_DX`, `PROCEDENCI`.

No se usan desplegables para NHC, nombre, diagnóstico libre o tratamiento libre porque su cardinalidad es demasiado alta.

## Integridad de formularios
Los formularios de edición filtran siempre las variables archivadas. Un campo retirado no puede llegar al normalizador/editable por error.

## Supabase
La aplicación comprueba al arrancar que las seis tablas y columnas críticas necesarias existen antes de permitir escrituras.

## Ingreso manual
Los episodios nuevos utilizan `source_row` negativo, evitando el desbordamiento que ocurría al usar una marca temporal de milisegundos en un campo PostgreSQL `integer`.

## Importación
La aplicación conserva `episode_id` al reimportar una fila existente mediante `source_row` y hace `upsert` por `source_row`, evitando duplicados por reimportación.

## Seguridad
No se incluye en el repositorio ningún CSV clínico, FP5, secreto, contraseña ni clave de Supabase/Gemini.
