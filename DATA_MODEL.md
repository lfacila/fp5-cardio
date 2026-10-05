# FP5 Cardio Cloud · Modelo de datos v15

- `fp5_patients`: un paciente lógico por NHC cuando existe.
- `fp5_episodes`: cada fila de FileMaker corresponde a un ingreso/episodio.
- `raw_data`: conserva los 208 campos originales del export, sin recorte.
- `validated_data`: contiene las 106 variables activas, normalizadas y editables.
- `field_status`: estado de cada variable activa (`available`, `missing_revisado`, `missing_no_revisado`, `invalid_*`).
- `fp5_field_definitions`: diccionario de variables activas.
- `fp5_ai_extractions`: propuestas de IA separadas de los datos validados.
- `fp5_audit_log`: trazabilidad de cambios manuales y de IA aceptados.
- `fp5_import_runs`: historial de cargas del CSV.

El criterio de activación es >10% de episodios con valor no vacío, más las fechas estructuralmente necesarias para eventos activos. Las 102 variables restantes quedan archivadas: no se eliminan y permanecen en `raw_data`.

Para nuevos registros manuales, `source_row` utiliza enteros negativos para no interferir con las filas originales positivas del FP5.
