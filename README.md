## Campos eliminados del modelo activo
Por decisión del responsable de la base: DLAS, TIEMPO_3AT, VEG_TAM y DOSIS_MAX_. No forman parte del diccionario activo ni de la interfaz/IA.

# FP5 Cardio · Cloud v0.6

Aplicación Streamlit + Supabase para reconstruir y revisar la base histórica de cardiología exportada desde FileMaker.

## Fuente real

- 14.297 registros de origen.
- 208 variables por registro.
- `nombre variables.csv` define los 208 campos y su tipo/formato de FileMaker.
- La aplicación conserva el valor original en `raw_data`.

## Modelo

Los registros se tratan como **episodios**, no como pacientes únicos.

- `NHC` agrupa los episodios cuando está informado.
- `source_row` identifica de forma inmutable la fila original.
- `episode_id` se deriva de la fila + hash del contenido.
- `NUM_PAC` no se usa como clave porque el export muestra un valor esencialmente constante.

Capas:

1. `raw_data`: copia exacta del FP5.
2. `validated_data`: valor clínico normalizado/editable.
3. `field_status`: disponible / missing revisado / missing no revisado / valor inválido normalizado a missing.
4. `fp5_ai_extractions`: propuestas IA con evidencia y revisión humana.
5. `fp5_audit_log`: trazabilidad de todos los cambios.

## Missing

- `555` = **missing revisado**.
- vacío = **missing no revisado**.
- `0` se interpreta **por variable**. En variables cuantitativas donde 0 sea imposible, `validated_data` queda en missing y `raw_data` conserva el 0.

## Hallazgo importante de tipos

Seis campos están declarados como numéricos por FileMaker pero contienen fechas en el CSV real. Se normalizan como fechas mediante `effective_type`:

- `EVO_ACVHEM1`
- `EVO_ACVISQ1`
- `EVO_EXITUS1`
- `EVO_REINGR1`
- `EVO_REINGR3`
- `EVO_SANG_G1`

También hay unas pocas fechas malformadas en el origen. **No se corrigen automáticamente**.

## Despliegue

1. Crear las tablas ejecutando `sql/schema.sql` en el proyecto Supabase.
2. Subir el contenido de esta carpeta a GitHub.
3. Desplegar `app.py` en Streamlit Community Cloud.
4. Configurar los secretos:

```toml
SUPABASE_URL = "..."
SUPABASE_SECRET_KEY = "..."
APP_PASSWORD = "..."
GEMINI_API_KEY = "..."
GEMINI_MODEL = "nombre-del-modelo-habilitado-en-tu-cuenta"
```

La clave de servicio debe existir únicamente en Streamlit Secrets.

## Importación

Desde **Administración** se puede subir el CSV original de 21 MB directamente. La aplicación primero muestra una auditoría y después permite importar los 14.297 episodios por lotes.

**No subir nunca el CSV clínico a GitHub.**


## Guía rápida

Consulta `SEGUIR_AHORA.txt` para los pasos exactos. No subas el CSV clínico a GitHub.


### Unidad de análisis
Cada registro de FileMaker es un ingreso/episodio. Un mismo NHC puede aparecer en varios registros. La aplicación los agrupa por NHC pero conserva cada episodio por separado.

### Campos retirados de la interfaz
DOSIS, DOSIS1, DOSIS2, MEDIANA, MEDIA_DIAS y NUM_PAC se conservan en raw_data, pero se ocultan de la edición clínica y de la IA.


### Modelo de exportación FP5
El CSV histórico conserva 208 columnas. La aplicación activa trabaja con 204 variables: `DLAS`, `TIEMPO_3AT`, `VEG_TAM` y `DOSIS_MAX_` han sido retiradas por autorización explícita del usuario. Esos campos no se importan al modelo activo. El fichero fuente de 208 columnas permanece intacto en el hospital.
