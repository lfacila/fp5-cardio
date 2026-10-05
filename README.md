# FP5 Cardio Cloud v12

Aplicación Streamlit + Supabase para sustituir la operativa diaria de la base FileMaker de Cardiología.

## Modelo de trabajo
- Cada fila del export FP5 es un **ingreso/episodio**.
- Un mismo NHC puede tener múltiples episodios.
- La pantalla principal está orientada a **Ingresos**: todos, pendientes de asignación, ingresados actualmente, altas y reingresos.
- La ficha sigue las presentaciones reales de FileMaker: **Datos iniciales del ingreso → Datos de alta → Evolución → Exploraciones y tecnología → Trazabilidad**.

## Datos
- El CSV de origen tiene 208 columnas.
- `raw_data` conserva los 208 valores originales.
- El modelo activo v12 contiene 106 variables: campos con >10% de episodios con valor, más algunas fechas estructuralmente necesarias para eventos activos.
- Las variables retiradas quedan fuera de la recogida activa y de la IA, pero **no se borran de `raw_data`**.
- `555` = missing revisado; vacío = missing no revisado; el significado del `0` depende de la variable.

## IA
Gemini solo propone cambios sobre variables activas y la aceptación siempre requiere revisión humana.

## Seguridad
Las claves de Supabase y Gemini solo se guardan en Streamlit Secrets. El CSV clínico no debe subirse a GitHub.
