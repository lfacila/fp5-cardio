# FP5 Cardio — Modelo de datos v0.3

## Unidad de análisis
Los 14.297 registros exportados de FileMaker se tratan como **ingresos/episodios**, no como 14.297 pacientes únicos. Un mismo NHC puede tener múltiples episodios.

- 14.297 filas de origen.
- 11.211 NHC distintos entre los registros con NHC informado.
- 4 filas sin NHC.
- `NUM_PAC` aparece esencialmente constante (=1) y **no es una clave utilizable**.
- Existe una pareja de filas idénticas; se conserva mediante `source_row` único.

## Identidad
- `patient_id`: `NHC:<NHC>` cuando NHC está informado.
- Cuando falta NHC: `ROW:<source_row>` para evitar fusionar pacientes de forma especulativa.
- `episode_id`: identificador estable del registro de origen, basado en la fila original y un hash del contenido.
- `source_row`: número de fila 1-based del CSV original; nunca se reutiliza.

## Capas del dato
### raw_data
Replica los valores del FP5 sin reinterpretarlos.

### validated_data
Versión clínica normalizada/validada por el usuario.

### field_status
Estado independiente por campo:
- `available`
- `missing_revisado` (`555` en origen)
- `missing_no_revisado` (vacío en origen)
- `invalid_zero_to_missing` (0 imposible para esa variable)

### AI proposals
La IA nunca modifica `raw_data`. Crea propuestas separadas con evidencia, modelo y estado pending/accepted/rejected.

## Regla de missing
- `555` = **missing revisado**.
- vacío = **missing no revisado**.
- el `0` se interpreta según la variable.

## Campos derivados/legado
`MEDIANA`, `MEDIA_EDAD`, `MEDIA_DIAS` y `NUM_PAC` se conservan en `raw_data`, pero no se muestran como variables clínicas normales hasta revisar su fórmula/semántica.

## Campos confirmados por el usuario

Se han incorporado las definiciones clínicas aportadas para ACXFA, ADE, ADE_2, ANTIARRITM, ATORVASTAT, COMP_VASC, CVE, EVO_REINGR2, HASBLED, HB1AC_2, NACOS, NITRITOS, PITAVASTAT, PORCENT, ROSUVASTAT y VARON.

Los campos DOSIS/DOSIS1/DOSIS2, MEDIANA, MEDIA_DIAS, MEDIA_EDAD y NUM_PAC quedan ocultos de la interfaz clínica pero permanecen en raw_data.
