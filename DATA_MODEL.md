# FP5 Cardio v12 · Modelo de datos

- `fp5_patients`: un paciente lógico por NHC cuando existe.
- `fp5_episodes`: **cada fila del FileMaker = un ingreso/episodio**.
- `raw_data`: **los 208 campos originales del CSV**, sin recortes ni normalización.
- `validated_data`: solo las variables activas del modelo v12, normalizadas y editables.
- `field_status`: estado de cada variable activa (`available`, `missing_revisado`, `missing_no_revisado`, `invalid_*`).
- `fp5_field_definitions`: diccionario activo (v12).
- `fp5_ai_extractions`: propuestas de IA sobre campos activos.
- `fp5_audit_log`: trazabilidad de cambios.

El criterio de activación es >10% de episodios con valor no vacío. Se mantienen además algunas fechas estructuralmente necesarias para eventos que sí están activos. Las variables archivadas no se borran del original: permanecen en `raw_data`.
