# FP5 Cardio Cloud · v15

Aplicación Streamlit + Supabase para sustituir la operativa diaria de la base FileMaker de Cardiología.

## Modelo
- Cada fila del export FP5 corresponde a un ingreso/episodio.
- Un mismo NHC puede tener múltiples episodios.
- La pantalla principal está orientada a **Ingresos**: todos, pendientes de asignación, ingresados actualmente, altas y reingresos.
- La ficha sigue el flujo real: **Datos iniciales del ingreso → Datos de alta → Evolución → Exploraciones y tecnología → Original / IA / Auditoría**.

## Datos y trazabilidad
- El export real contiene **208 columnas**. `raw_data` conserva los 208 valores originales.
- El modelo clínico activo contiene **106 variables**; las restantes permanecen archivadas y no se pierden.
- `555` = missing revisado; vacío = missing no revisado; el significado de `0` depende de cada variable.
- Cada cambio manual o de IA aceptado queda auditado.

## Entrada de datos
- Todos los campos de fecha editables usan calendario y se presentan en formato **DD/MM/YYYY**.
- Los campos categóricos repetitivos seleccionados a partir del CSV real usan desplegable, colocando primero las opciones más frecuentes y permitiendo introducir un valor no presente en la lista.
- Las fechas tienen validación básica de cronología: asignación y alta no pueden preceder al ingreso.

## Importación
- El CSV debe ser el export FP5 sin cabecera, con **14.297 filas y 208 columnas** para la carga completa.
- Se recomienda primero el piloto de 100 episodios.
- Reimportar ese piloto actualiza los mismos episodios por `source_row`; no crea duplicados.
- No subir el CSV clínico a GitHub.

## IA
Gemini solo propone cambios sobre variables activas. Las propuestas quedan pendientes de revisión humana antes de modificar datos validados.

## Seguridad
Las claves de Supabase y Gemini se guardan únicamente en Streamlit Secrets. El repositorio no contiene secretos ni datos clínicos.
