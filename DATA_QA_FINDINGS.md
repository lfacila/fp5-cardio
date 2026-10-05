# Hallazgos QA FP5 v0.3

## 1. El CSV tiene 14.297 filas y 208 columnas

Todas las filas se pueden parsear con 208 campos.

## 2. Identidad

- 11.211 NHC distintos entre los registros con NHC informado.
- 4 registros no tienen NHC.
- `NUM_PAC` tiene un único valor no vacío (1) y no se utilizará como clave.
- Hay una pareja de filas idénticas; se conserva mediante `source_row`.

## 3. Missing

- `555` = missing revisado.
- vacío = missing no revisado.
- `0` = se interpreta por variable.

## 4. Hallazgo importante de tipos

Se han detectado seis campos definidos como numéricos en `nombre variables.csv` que contienen fechas `dd/mm/yyyy`. Por ello el proyecto conserva el tipo FileMaker original, pero añade `effective_type = D` para la normalización: 

`EVO_ACVHEM1`, `EVO_ACVISQ1`, `EVO_EXITUS1`, `EVO_REINGR1`, `EVO_REINGR3`, `EVO_SANG_G1`.

Esto es un ajuste basado en el contenido real del CSV, no una suposición clínica.

## 5. Fechas anómalas

Hay 3 valores con formato de fecha no estándar (`05-09-2017`, `01-09-2017`) y 9 valores con años menores de 1900 (`0201`, `0214`, `0216`, `0212`, `0218`, `0223`, `0205`, `0224`). No se corrigen automáticamente. Se conservan en `raw_data` y pasan a estado `invalid_date` hasta revisión.

## 6. Derivadas / legado

`MEDIANA` y `MEDIA_EDAD` son constantes globales replicadas en todos los registros. `NUM_PAC` es esencialmente constante. `MEDIA_DIAS` presenta comportamiento de cálculo/derivado y requiere revisión de su fórmula. Se conservan, pero no se muestran como variables clínicas normales.
