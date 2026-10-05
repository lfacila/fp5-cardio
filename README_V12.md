FP5 Cardio Cloud v12

CAMBIO PRINCIPAL
- Se deja de ordenar por las 204 variables internas. La interfaz sigue el flujo real de FileMaker: Ingresos → Datos iniciales → Alta → Evolución → Exploraciones → historial.
- El CSV original continúa teniendo 208 columnas y se conserva completo en raw_data.
- El modelo activo usa 106 variables: criterio de frecuencia estrictamente >10% de episodios con valor, más fechas estructuralmente necesarias para eventos activos.
- 102 variables quedan archivadas fuera de la recogida activa; no se pierden del original.
- NUM_PAC, MEDIANA, MEDIA_EDAD y MEDIA_DIAS quedan fuera del modelo activo por ser identificadores/estadísticos/legacy, aunque sus valores se conservan en raw_data.
- Cada fila del FP5 sigue siendo un ingreso/episodio; un NHC puede tener múltiples episodios.

PROTECCIÓN
- raw_data: copia completa de los 208 campos originales.
- validated_data: solo variables activas y normalizadas.
- IA: solo variables activas.
- Auditoría: cambios humanos o aceptaciones IA.
