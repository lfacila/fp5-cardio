import hashlib
import io
import json
import math
import re
from datetime import datetime, date, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st
from supabase import create_client

st.set_page_config(
    page_title="FP5 Cardio",
    page_icon="❤️",
    layout="wide",
    initial_sidebar_state="expanded",
)

BASE_DIR = Path(__file__).resolve().parent
REGISTRY = json.loads((BASE_DIR / "field_registry_v4.json").read_text(encoding="utf-8"))
SOURCE_REGISTRY = json.loads((BASE_DIR / "source_fields_208.json").read_text(encoding="utf-8"))
RETIRED_FIELDS = {x["field"] for x in SOURCE_REGISTRY["fields"]} - {f["field"] for f in REGISTRY["fields"]}
SOURCE_FIELDS = SOURCE_REGISTRY["fields"]
FIELDS = REGISTRY["fields"]
FIELD_BY_NAME = {f["field"]: f for f in FIELDS}
PREVALENCE_THRESHOLD = float(REGISTRY.get("threshold_nonempty_pct_strict_gt", 10.0))
RETIRED_META = json.loads((BASE_DIR / "retired_fields_v4.json").read_text(encoding="utf-8"))["fields"]
PREVALENCE_META = json.loads((BASE_DIR / "field_prevalence_v1.json").read_text(encoding="utf-8"))["fields"]
FIELD_LABELS = {f["field"]: f["label"] for f in FIELDS}
FIELD_OPTIONS = json.loads((BASE_DIR / "field_options_v1.json").read_text(encoding="utf-8")).get("fields", {})
APP_VERSION = "FP5 Cardio Cloud v15 · QA integral"

# Structural QA: refuse to run if the shipped registry is inconsistent.
if len(SOURCE_FIELDS) != 208:
    st.error(f"Diccionario FP5 corrupto: se esperaban 208 campos de origen y hay {len(SOURCE_FIELDS)}.")
    st.stop()
if len(FIELDS) != 106:
    st.error(f"Diccionario FP5 corrupto: se esperaban 106 variables activas y hay {len(FIELDS)}.")
    st.stop()
if len(RETIRED_FIELDS) != 102 or set(FIELD_BY_NAME) & RETIRED_FIELDS:
    st.error("Inconsistencia entre variables activas y archivadas.")
    st.stop()
if len({f["field"] for f in SOURCE_FIELDS}) != len(SOURCE_FIELDS) or len({f["field"] for f in FIELDS}) != len(FIELDS):
    st.error("Hay campos duplicados en el diccionario FP5.")
    st.stop()
if not set(FIELD_OPTIONS).issubset(set(FIELD_BY_NAME)):
    st.error("Hay opciones de frecuencia asociadas a campos que no están activos.")
    st.stop()
CLINICAL_GROUPS = [
    ("Antecedentes / riesgo", "risk_history"),
    ("Diagnóstico", "diagnosis"),
    ("Analítica", "laboratory"),
    ("Exploraciones / procedimientos", "procedure_test"),
    ("Tratamiento", "treatment"),
    ("Eventos / seguimiento", "event_followup"),
    ("Seguimiento", "followup"),
    ("Texto clínico", "clinical_text"),
    ("Administración", "administrative"),
]

SUPABASE_URL = st.secrets.get("SUPABASE_URL", "")
SUPABASE_SECRET_KEY = st.secrets.get("SUPABASE_SECRET_KEY", "")
SUPABASE_SERVICE_ROLE_KEY = st.secrets.get("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_BACKEND_KEY = SUPABASE_SECRET_KEY or SUPABASE_SERVICE_ROLE_KEY
APP_PASSWORD = st.secrets.get("APP_PASSWORD", "")
GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "")
GEMINI_MODEL = st.secrets.get("GEMINI_MODEL", "")

if not SUPABASE_URL or not SUPABASE_BACKEND_KEY:
    st.error("Faltan SUPABASE_URL y SUPABASE_SECRET_KEY (o, temporalmente, SUPABASE_SERVICE_ROLE_KEY) en Streamlit Secrets.")
    st.stop()

supabase = create_client(SUPABASE_URL, SUPABASE_BACKEND_KEY)

@st.cache_resource(show_spinner=False)
def check_database_contract():
    """Fail early with a readable message when the deployed Supabase schema is incompatible."""
    checks = [
        ("fp5_patients", "patient_id,nhc,display_name,updated_at"),
        ("fp5_episodes", "episode_id,patient_id,source_row,raw_data,validated_data,field_status,updated_at"),
        ("fp5_field_definitions", "field,label,field_order,effective_type"),
        ("fp5_ai_extractions", "id,episode_id,field,proposed_value,status"),
        ("fp5_audit_log", "id,episode_id,patient_id,action,field"),
        ("fp5_import_runs", "id,file_name,status"),
    ]
    for table, columns in checks:
        try:
            supabase.table(table).select(columns).limit(1).execute()
        except Exception as exc:
            st.error(
                f"La base de datos no coincide con la versión de la aplicación (tabla `{table}`). "
                "No se ha realizado ninguna escritura."
            )
            st.caption(f"Detalle técnico: {type(exc).__name__}: {str(exc)[:500]}")
            st.stop()

check_database_contract()


# -----------------------------
# Seguridad básica
# -----------------------------
if APP_PASSWORD:
    st.session_state.setdefault("authenticated", False)
    if not st.session_state.authenticated:
        st.markdown("# ❤️ FP5 Cardio")
        st.caption("Acceso restringido")
        pwd = st.text_input("Contraseña", type="password")
        if st.button("Entrar", type="primary"):
            if pwd == APP_PASSWORD:
                st.session_state.authenticated = True
                st.rerun()
            st.error("Contraseña incorrecta.")
        st.stop()


# -----------------------------
# Helpers
# -----------------------------
def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def clean(v):
    if v is None:
        return ""
    if isinstance(v, float) and math.isnan(v):
        return ""
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return str(v).strip()


def to_date_obj(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def normalize_date_value(raw):
    dt = to_date_obj(raw)
    if dt is None:
        return clean(raw), "invalid_date"
    if dt.year < 1900 or dt.year > datetime.now().year + 1:
        return clean(raw), "invalid_date"
    return dt.isoformat(), "available"


def normalized_number(raw, decimals):
    text = clean(raw).replace(",", ".")
    if text == "":
        return None
    try:
        value = float(text)
        if decimals == 0 and value.is_integer():
            return int(value)
        return value
    except Exception:
        return text


def normalize_field(field, raw):
    meta = FIELD_BY_NAME[field]
    if raw is None:
        raw = ""
    if isinstance(raw, datetime):
        raw = raw.date()
    if isinstance(raw, date):
        if meta.get("effective_type", meta["filemaker_type"]) == "D":
            return normalize_date_value(raw)
        raw = raw.isoformat()
    raw = clean(raw)
    if raw == "":
        return None, "missing_no_revisado"
    if raw == "555":
        return None, "missing_revisado"
    if meta.get("effective_type", meta["filemaker_type"]) == "D":
        return normalize_date_value(raw)
    if meta.get("effective_type", meta["filemaker_type"]) == "N":
        if raw == "0" and meta["zero_policy"] == "impossible_zero_to_missing":
            return None, "invalid_zero_to_missing"
        value = normalized_number(raw, int(meta["decimals"]) if str(meta["decimals"]).isdigit() else 0)
        if isinstance(value, str):
            return value, "invalid_numeric"
        return value, "available"
    return raw, "available"


def normalize_row(raw_dict):
    data, statuses = {}, {}
    for f in FIELDS:
        name = f["field"]
        value, status = normalize_field(name, raw_dict.get(name, ""))
        data[name] = value
        statuses[name] = status
    return data, statuses


def patient_key(nhc, source_row):
    nhc = clean(nhc)
    return f"NHC:{nhc}" if nhc else f"ROW:{source_row}"


def episode_key(source_row, row_values):
    payload = "\x1f".join(clean(v) for v in row_values).encode("utf-8", errors="replace")
    digest = hashlib.sha256(payload).hexdigest()[:12]
    return f"EP-{source_row:05d}-{digest}"


def parse_fp5_csv_bytes(content):
    df = pd.read_csv(
        io.BytesIO(content),
        header=None,
        dtype=str,
        encoding="cp1252",
        keep_default_na=False,
        na_filter=False,
        engine="python",
    )
    if df.shape[1] != len(SOURCE_FIELDS):
        raise ValueError(f"El CSV tiene {df.shape[1]} columnas; el export FP5 esperado tiene {len(SOURCE_FIELDS)}.")
    df.columns = [f["field"] for f in SOURCE_FIELDS]
    return df

def profile_import(df):
    active_names = [f["field"] for f in FIELDS]
    active_df = df[active_names]
    nhc = df["NHC"] if "NHC" in df else pd.Series([], dtype=str)
    dup_full = df.duplicated(keep=False)
    return {
        "rows": len(df),
        "cols": len(active_df.columns),
        "source_cols": len(SOURCE_FIELDS),
        "active_cols": len(active_names),
        "retired_cols": len(SOURCE_FIELDS) - len(active_names),
        "nonempty_nhc": int(nhc[nhc != ""].nunique()) if len(nhc) else 0,
        "blank_nhc": int((nhc == "").sum()) if len(nhc) else 0,
        "repeated_nhc_rows": int(nhc[nhc != ""].duplicated(keep=False).sum()) if len(nhc) else 0,
        "exact_duplicate_rows": int(dup_full.sum()),
        "blank_cells": int((active_df == "").sum().sum()),
        "blank_cells_source": int((df == "").sum().sum()),
        "missing_555_cells": int((active_df == "555").sum().sum()),
        "missing_555_cells_source": int((df == "555").sum().sum()),
        "zero_cells": int((active_df == "0").sum().sum()),
        "zero_cells_source": int((df == "0").sum().sum()),
        "empty_fields": [f for f in active_names if (df[f] == "").all()],
    }

@st.cache_resource(show_spinner=False)
def seed_field_definitions():
    rows = []
    for f in FIELDS:
        rows.append({
            "field": f["field"],
            "label": f["label"],
            "field_order": f["order"],
            "filemaker_type": f["filemaker_type"],
            "effective_type": f.get("effective_type", f["filemaker_type"]),
            "length": f["length"],
            "decimals": f["decimals"],
            "category": f["category"],
            "data_kind": f["data_kind"],
            "allow_ai": f["ai_allowed"],
            "zero_policy": f["zero_policy"],
            "missing_blank": f["missing_blank"],
            "missing_555": f["missing_555"],
            "review_status": f["review_status"],
            "notes": f.get("notes", ""),
            "updated_at": now_iso(),
        })
    # small batches to avoid large request payloads
    for i in range(0, len(rows), 100):
        supabase.table("fp5_field_definitions").upsert(rows[i:i+100], on_conflict="field").execute()


def get_patient(pid):
    res = supabase.table("fp5_patients").select("*").eq("patient_id", pid).limit(1).execute()
    return res.data[0] if res.data else None


@st.cache_data(ttl=20, show_spinner=False)
def _all_patients_cached():
    out = []
    batch = 1000
    offset = 0
    while True:
        part = (
            supabase.table("fp5_patients")
            .select("patient_id,nhc,display_name,updated_at")
            .order("updated_at", desc=True)
            .range(offset, offset + batch - 1)
            .execute().data or []
        )
        out.extend(part)
        if len(part) < batch:
            break
        offset += batch
    return out


@st.cache_data(ttl=20, show_spinner=False)
def _episode_counts_cached():
    counts = {}
    batch = 1000
    offset = 0
    while True:
        part = (
            supabase.table("fp5_episodes")
            .select("patient_id")
            .range(offset, offset + batch - 1)
            .execute().data or []
        )
        for row in part:
            pid = row.get("patient_id")
            if pid:
                counts[pid] = counts.get(pid, 0) + 1
        if len(part) < batch:
            break
        offset += batch
    return counts


def list_patients(page=1, page_size=25, search="", episode_filter="Todos"):
    patients = _all_patients_cached()
    counts = _episode_counts_cached()
    search = clean(search).lower()
    filtered = []
    for patient in patients:
        pid = patient.get("patient_id")
        n = counts.get(pid, 0)
        # Hide orphan patient rows left by aborted/legacy manual inserts; an actual patient
        # in the UI must have at least one episode.
        if n == 0:
            continue
        if episode_filter == "Solo 1 ingreso" and n != 1:
            continue
        if episode_filter == "Más de 1 ingreso" and n <= 1:
            continue
        if search:
            nhc = clean(patient.get("nhc")).lower()
            name = clean(patient.get("display_name")).lower()
            if search not in nhc and search not in name:
                continue
        item = dict(patient)
        item["episode_count"] = n
        filtered.append(item)
    start = (page - 1) * page_size
    return filtered[start:start + page_size], len(filtered)


def patient_episodes(pid):
    return (
        supabase.table("fp5_episodes")
        .select("episode_id,source_row,updated_at,updated_by,raw_data,validated_data,field_status")
        .eq("patient_id", pid)
        .order("source_row", desc=True)
        .execute()
        .data or []
    )


def get_episode(eid):
    res = supabase.table("fp5_episodes").select("*").eq("episode_id", eid).limit(1).execute()
    return res.data[0] if res.data else None


def save_episode_validated(ep, validated, statuses, field=None, old_value=None, new_value=None, source="manual"):
    eid = ep["episode_id"]
    old_updated = ep.get("updated_at")
    if field in FIELD_BY_NAME:
        validate_episode_changes(ep, {field: (validated.get(field), statuses.get(field, "available"))})
    payload = {
        "validated_data": validated,
        "field_status": statuses,
        "updated_at": now_iso(),
        "updated_by": st.session_state.get("user_label", "web"),
    }
    q = supabase.table("fp5_episodes").update(payload).eq("episode_id", eid)
    if old_updated:
        q = q.eq("updated_at", old_updated)
    res = q.select("*").execute()
    if not res.data:
        raise RuntimeError("El episodio ha cambiado desde que se abrió. Recarga el registro y vuelve a guardar.")
    supabase.table("fp5_audit_log").insert({
        "episode_id": eid,
        "patient_id": ep["patient_id"],
        "action": "UPDATE",
        "field": field,
        "old_value": old_value,
        "new_value": new_value,
        "snapshot": validated,
        "source": source,
        "changed_at": now_iso(),
        "changed_by": st.session_state.get("user_label", "web"),
    }).execute()
    return res.data[0]


def existing_episode_ids_by_source_row():
    out = {}
    offset = 0
    batch = 1000
    while True:
        part = (supabase.table("fp5_episodes").select("source_row,episode_id").gte("source_row",1).order("source_row").range(offset,offset+batch-1).execute().data or [])
        for row in part:
            try: out[int(row["source_row"])] = row["episode_id"]
            except (TypeError,ValueError): pass
        if len(part) < batch: break
        offset += batch
    return out


def import_fp5(content, filename, dry_run=False, limit=None):
    df=parse_fp5_csv_bytes(content)
    if limit is not None:
        limit=int(limit)
        if len(df)<limit:
            raise ValueError(f"El CSV solo contiene {len(df)} filas; el piloto necesita al menos {limit}.")
        df=df.head(limit).copy()
    profile=profile_import(df)
    if dry_run: return profile
    file_hash=hashlib.sha256(content).hexdigest()
    run=supabase.table("fp5_import_runs").insert({"file_name":filename,"file_sha256":file_hash,"source_rows":len(df),"status":"started","started_by":"web","started_at":now_iso()}).select("id").execute()
    run_id=run.data[0]["id"] if run.data else None
    try:
        existing_ids=existing_episode_ids_by_source_row()
        patients={}; episode_rows=[]
        source_names=[f["field"] for f in SOURCE_FIELDS]; active_names=[f["field"] for f in FIELDS]; now=now_iso()
        for idx,row in enumerate(df.itertuples(index=False,name=None),start=1):
            raw={source_names[j]:clean(row[j]) for j in range(len(source_names))}
            active_raw={name:raw.get(name,"") for name in active_names}
            nhc=raw.get("NHC",""); pkey=patient_key(nhc,idx); name=raw.get("NOMBRE","")
            if pkey not in patients:
                patients[pkey]={"patient_id":pkey,"nhc":nhc or None,"display_name":name or None,"updated_at":now}
            elif not patients[pkey].get("display_name") and name:
                patients[pkey]["display_name"]=name
            validated,statuses=normalize_row(active_raw)
            ep_id=existing_ids.get(idx) or episode_key(idx,row)
            episode_rows.append({"episode_id":ep_id,"patient_id":pkey,"source_row":idx,"raw_data":raw,"validated_data":validated,"field_status":statuses,"updated_at":now,"updated_by":"FP5_IMPORT"})
        patient_rows=list(patients.values())
        for i in range(0,len(patient_rows),100):
            supabase.table("fp5_patients").upsert(patient_rows[i:i+100],on_conflict="patient_id").execute()
        progress=st.progress(0,text="Importando episodios…")
        for i in range(0,len(episode_rows),100):
            batch=episode_rows[i:i+100]
            supabase.table("fp5_episodes").upsert(batch,on_conflict="source_row").execute()
            progress.progress(min(1.0,(i+len(batch))/max(1,len(episode_rows))),text=f"Episodios {i+len(batch)} / {len(episode_rows)}")
        progress.empty()
        if run_id:
            supabase.table("fp5_import_runs").update({"inserted_patients":len(patient_rows),"inserted_episodes":len(episode_rows),"status":"completed","completed_at":now_iso()}).eq("id",run_id).execute()
        profile["patients_created"]=len(patient_rows); profile["episodes_created"]=len(episode_rows)
        return profile
    except Exception as exc:
        try:
            if run_id:
                supabase.table("fp5_import_runs").update({"inserted_patients":0,"inserted_episodes":0,"status":"failed","completed_at":now_iso(),"notes":str(exc)[:1000]}).eq("id",run_id).execute()
        except Exception:
            pass
        raise


@st.cache_resource(show_spinner=False)
def gemini_client(api_key):
    from google import genai
    return genai.Client(api_key=api_key)


def ai_schema():
    allowed = []
    for f in FIELDS:
        if not f["ai_allowed"]:
            continue
        allowed.append({
            "field": f["field"],
            "label": f["label"],
            "kind": f["data_kind"],
            "category": f["category"],
        })
    return allowed


def extract_ai(text):
    if not GEMINI_API_KEY:
        raise RuntimeError("Falta GEMINI_API_KEY en Streamlit Secrets.")
    client = gemini_client(GEMINI_API_KEY)
    prompt = f"""
Eres un extractor estructurado para una base de datos cardiológica histórica.

REGLAS:
- Extrae SOLO información explícita en el texto.
- Respeta negaciones, fechas y cifras.
- No diagnostiques, no completes y no calcules.
- Devuelve SOLO variables que tengan evidencia textual clara.
- Para una variable binaria/codificada, usa 1 para afirmativo y 0 para negativo solo cuando la evidencia lo permita de forma inequívoca.
- Para variables numéricas usa el número explícito y NO alteres unidades.
- Para fechas usa YYYY-MM-DD si la fecha es explícita.
- Para texto clínico, conserva el contenido explícito sin resumir.
- La evidencia debe ser una cita exacta y breve que aparezca literalmente en el texto.
- confidence es una estimación técnica, no una validación clínica.
- Devuelve un objeto JSON con clave 'proposals'.

FORMATO:
{{"proposals":[{{"field":"FE","value":35,"evidence":"FE del 35%","confidence":0.99}}]}}

CAMPOS PERMITIDOS:
{json.dumps(ai_schema(), ensure_ascii=False, indent=2)}

TEXTO CLÍNICO:
{text}
"""
    from google.genai import types
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            max_output_tokens=8000,
            thinking_config=types.ThinkingConfig(thinking_level="low"),
        ),
    )
    raw = re.sub(r"^```json\s*|\s*```$", "", (response.text or "").strip(), flags=re.I)
    data = json.loads(raw)
    proposals = data.get("proposals", []) if isinstance(data, dict) else []
    allowed = {f["field"] for f in FIELDS if f["ai_allowed"]}
    clean_props = []
    for p in proposals:
        field = p.get("field")
        value = p.get("value")
        evidence = clean(p.get("evidence", ""))
        if field not in allowed or value in (None, ""):
            continue
        if evidence and evidence not in text:
            evidence = ""
        try:
            confidence = float(p.get("confidence")) if p.get("confidence") is not None else None
        except Exception:
            confidence = None
        clean_props.append({"field": field, "value": value, "evidence": evidence[:500], "confidence": confidence})
    return clean_props


def save_ai_proposals(episode_id, proposals):
    if not proposals:
        return
    proposal_fields={p["field"] for p in proposals}
    if proposal_fields:
        supabase.table("fp5_ai_extractions").update({"status":"rejected","reviewed_at":now_iso(),"reviewed_by":"auto_reemplazo_extraccion"}).eq("episode_id",episode_id).eq("status","pending").in_("field",list(proposal_fields)).execute()
    rows = []
    for p in proposals:
        field = p["field"]
        value = p["value"]
        # Normalize AI proposal to the same internal representation, but retain a clean proposal.
        normalized, status = normalize_field(field, value)
        if status.startswith("invalid"):
            normalized = value
        rows.append({
            "episode_id": episode_id,
            "field": field,
            "proposed_value": normalized,
            "evidence": p["evidence"],
            "confidence": p["confidence"],
            "model": GEMINI_MODEL,
            "status": "pending",
            "created_at": now_iso(),
        })
    supabase.table("fp5_ai_extractions").insert(rows).execute()


def pending_ai(episode_id):
    return (
        supabase.table("fp5_ai_extractions")
        .select("*")
        .eq("episode_id", episode_id)
        .eq("status", "pending")
        .order("created_at")
        .execute().data or []
    )


def accept_ai(ep, row):
    validated = dict(ep.get("validated_data") or {})
    statuses = dict(ep.get("field_status") or {})
    field = row["field"]
    old = validated.get(field)
    validated[field] = row["proposed_value"]
    statuses[field] = "available"
    save_episode_validated(ep, validated, statuses, field, old, row["proposed_value"], "IA")
    supabase.table("fp5_ai_extractions").update({
        "status": "accepted", "reviewed_at": now_iso(), "reviewed_by": st.session_state.get("user_label", "web")
    }).eq("id", row["id"]).execute()


def reject_ai(row):
    supabase.table("fp5_ai_extractions").update({
        "status": "rejected", "reviewed_at": now_iso(), "reviewed_by": st.session_state.get("user_label", "web")
    }).eq("id", row["id"]).execute()


def audit_for_episode(eid):
    return (
        supabase.table("fp5_audit_log")
        .select("action,field,old_value,new_value,source,changed_at,changed_by")
        .eq("episode_id", eid)
        .order("changed_at", desc=True)
        .limit(100).execute().data or []
    )



# -----------------------------
# Operational / clinical UI helpers
# -----------------------------
INGRESO_FIELDS = [
    "NHC","NOMBRE","EDAD","VARON","FECHA_INGR","FECHA_ASIG","CAMA",
    "CARDIOLOGO","PROCEDENCI","CENTRO","INGRESO_PR","PRIMER_EPI",
    "DIAG_INGRE","OBSERVACIO","DIAS_ASIG","CP","CSIP"
]

ALTA_RISK_FIELDS = [
    "HTA","DM","DL","OBESIDAD","FUMADOR","EXFUMADOR","ECV_PREVIA","ERC",
    "CAR_FAM","ACXFA","ARRITMIA","EMBOLIA","ENF_VALV_P","VALVULA_AF",
    "VALV_MEC","TROMBO","HASBLED","FISTULA","IAMSEST2","IAM_ANT40","IC"
]
ALTA_DIAG_FIELDS=["DIAG_ALTA","GRUPO_DX"]
ALTA_LAB_FIELDS=["HB","HB1AC","ADE","GLUC","CR","NA","LDL","HDL","TRIG","ALBUMINA","PCR","INR_2","INR_3","INR_TOTALE","NITRITOS","FE","LPA","AREA"]
ALTA_TX_FIELDS=[
    "AAS","ACO","NACOS","APIXABAN","DABIGATRAN","RIVAROXABA","IECA","ARA2","BETABLOQ","ANTAG_ALDO","DIURETICOS",
    "ESTAT","ATORVASTAT","ROSUVASTAT","PITAVASTAT","EZETIM","EZE1","EZE2","BEMPE","EZE","PCSK9","VAZK","IVABRADINA","RANOLAZINA",
    "CLOPI","PRASUG","TICA","INSULINA","ADO","DPP4","GLINIDAS","GLP1","ISLGT2","ENTRESTO","CALCIOANT","ANTIARRITM"
]
ALTA_ADMIN_FIELDS=["FECHA_ALTA","CARDIOLOGO1","DESTINO_AL","FIRMADO_EC","TRATAMIENT"]
TECH_IMAGE_FIELDS=["ECO","FE","ECOCARDIO","ETE","HOLTER","ERGOMETRIA","RMC","SPECT","TC_CORON"]
TECH_CORONARY_FIELDS=["CATE","ACTP_PRIM","ACTP_TCI","STENT","CIR_CAR","CCA","COMP_VASC","FECHA_CCA"]
TECH_DEVICE_FIELDS=["EEF","CVE","MCP","TAVI","UCO","FISTULA"]
EV_EVENT_FIELDS=[
    "EVO_IAM","EVO_IAM_FE","EVO_IC","EVO_IC_FEC","EVO_AVC","EVO_AVC_FE","EVO_ACVISQ","EVO_ACVISQ1",
    "EVO_ACVHEM","EVO_ACVHEM1","EVO_CABG","EVO_CABG_F","EVO_REVSC","EVO_REVSC_","EVO_REINGR","EVO_REINGR1",
    "EVO_REINGR2","EVO_REINGR3","EVO_EXITUS","EVO_EXITUS1","EVO_SANG","EVO_SANGFE","EVO_SANG_G","EVO_SANG_G1","EVO_AI","EVO_AI_FEC"
]
EV_LAB_FIELDS=["ADE_2","FECHA_ADE_","HB1AC_2","FECHA_A1C2","LDL2","LDL3","HDL2","HDL3","CR_2","CR2","TG2","K2","FECHA2"]
EV_FOLLOW_FIELDS=["OPTIMZ","FASE2B","ENT_SEGUIM","ISLGT2_SEG","BETA_SEGUI","AMR_SEGUIM"]
EV_TEXT_FIELDS=["EVOLUCION","OBS_EVOL","OBSERVACIO"]

SECTION_ORDER=[
    ("Datos Iniciales del ingreso", INGRESO_FIELDS),
    ("Datos de alta", ALTA_RISK_FIELDS+ALTA_DIAG_FIELDS+ALTA_LAB_FIELDS+ALTA_TX_FIELDS+ALTA_ADMIN_FIELDS),
    ("Evolución tras el alta", EV_EVENT_FIELDS+EV_LAB_FIELDS+EV_FOLLOW_FIELDS+EV_TEXT_FIELDS),
    ("Exploraciones y tecnología", TECH_IMAGE_FIELDS+TECH_CORONARY_FIELDS+TECH_DEVICE_FIELDS),
]

BINARY_OPTIONS={"No revisado":"", "Sí":"1", "No":"0", "No disponible (revisado)":"555"}
DATE_STATE_OPTIONS=["Fecha válida","No revisado","No disponible (revisado)","Fecha original no válida · revisar"]
CUSTOM_OPTION="✍️ Escribir otro valor"
MISSING_NOT_REVIEWED="No revisado"
MISSING_REVIEWED="No disponible (revisado)"

def fmeta(name):
    return FIELD_BY_NAME.get(name,{"field":name,"label":name,"data_kind":"text","filemaker_type":"C","effective_type":"C","observed_domain_preview":[]})

def flabel(name): return fmeta(name).get("label") or name

def is_date_field(name): return fmeta(name).get("effective_type",fmeta(name).get("filemaker_type"))=="D"

def is_simple_binary(name):
    dom=set(str(x) for x in fmeta(name).get("observed_domain_preview",[]))
    return bool(dom) and dom.issubset({"0","1","555"})

def field_value(ep,name):
    raw=ep.get("raw_data") or {}; val=ep.get("validated_data") or {}; sts=ep.get("field_status") or {}
    if name in val:
        status=sts.get(name,"available")
        if val[name] is None: return None,status or ("missing_revisado" if raw.get(name)=="555" else "missing_no_revisado")
        return val[name],status
    rv=raw.get(name,"")
    if rv=="555": return None,"missing_revisado"
    if rv=="": return None,"missing_no_revisado"
    return rv,sts.get(name,"available")

def value_text(name,value,status):
    if is_date_field(name) and value not in (None,""): return display_date(value)
    if status=="available" and is_simple_binary(name): return {"1":"Sí","0":"No"}.get(str(value),str(value))
    if status=="missing_revisado": return "No disponible · revisado"
    if status=="missing_no_revisado": return "No revisado"
    if status=="invalid_zero_to_missing": return "No disponible"
    if status in {"invalid_date","invalid_numeric"}: return f"{value} · revisar"
    return "—" if value in (None,"") else str(value)

def badge(status):
    classes={"available":"ok","missing_revisado":"reviewed","missing_no_revisado":"notreviewed","invalid_zero_to_missing":"reviewed","invalid_date":"bad","invalid_numeric":"bad"}
    labels={"available":"Disponible","missing_revisado":"Revisado · no disponible","missing_no_revisado":"No revisado","invalid_zero_to_missing":"No disponible","invalid_date":"Fecha no válida","invalid_numeric":"Valor no válido"}
    return f'<span class="status {classes.get(status,"notreviewed")}">{labels.get(status,status or "")}</span>'

def render_field_card(ep,name,show_missing=False):
    v,status=field_value(ep,name)
    if not show_missing and v in (None,""): return False
    st.markdown(f'<div class="field-card"><div class="field-title">{flabel(name)}</div><div class="field-value">{value_text(name,v,status)}</div><div class="field-footer">{badge(status)} <span>{name}</span></div></div>',unsafe_allow_html=True)
    return True

def render_group(ep,title,names,cols=4,show_missing=False):
    names=[n for n in names if n in FIELD_BY_NAME and (show_missing or field_value(ep,n)[0] not in (None,""))]
    if not names: return
    st.markdown(f'#### {title}')
    for i in range(0,len(names),cols):
        cs=st.columns(cols)
        for j,n in enumerate(names[i:i+cols]):
            with cs[j]: render_field_card(ep,n,show_missing=True)

def valid_date_text(v):
    dt=to_date_obj(v)
    return datetime.combine(dt,datetime.min.time()) if dt else None

def parse_date_key(v):
    return valid_date_text(v) or datetime(1900,1,1)

def display_date(v):
    dt=to_date_obj(v)
    return dt.strftime("%d/%m/%Y") if dt else clean(v)

def frequency_entries(name): return FIELD_OPTIONS.get(name,[])

def categorical_widget(name,current="",status="missing_no_revisado",key="cat",allow_custom=True):
    current_text="" if current is None else str(current)
    values=[str(x.get("value","")) for x in frequency_entries(name) if str(x.get("value",""))!=""]
    custom_label=CUSTOM_OPTION if not current_text or current_text in values else CUSTOM_OPTION+" · valor actual"
    option_values=list(values)
    if allow_custom: option_values.append(custom_label)
    option_values += [MISSING_NOT_REVIEWED,MISSING_REVIEWED]
    if current_text in values: index=values.index(current_text)
    elif current_text: index=len(values)
    elif status=="missing_revisado": index=len(option_values)-1
    else: index=len(option_values)-2
    selected=st.selectbox(flabel(name),option_values,index=index,key=key,help=f"Valores más frecuentes del CSV, primero los más frecuentes · {name}")
    if selected==custom_label:
        return st.text_input(f"{flabel(name)} · otro valor",value=current_text if current_text not in values else "",key=f"{key}_custom")
    if selected==MISSING_NOT_REVIEWED: return ""
    if selected==MISSING_REVIEWED: return "555"
    return selected

def date_widget(name,current="",status="missing_no_revisado",key="date",required=False):
    current_dt=to_date_obj(current); invalid=status=="invalid_date" and current not in (None,"")
    state="Fecha válida" if current_dt else "Fecha original no válida · revisar" if invalid else "No disponible (revisado)" if status=="missing_revisado" else "No revisado"
    if required and not current_dt: state="Fecha válida"
    selected=st.selectbox(f"Estado · {flabel(name)}",DATE_STATE_OPTIONS,index=DATE_STATE_OPTIONS.index(state),key=f"{key}_state")
    if selected=="Fecha válida": return st.date_input(flabel(name),value=current_dt or date.today(),key=f"{key}_date",format="DD/MM/YYYY")
    if selected=="Fecha original no válida · revisar":
        st.caption(f"Valor original conservado: {current}")
        return current
    return "555" if selected=="No disponible (revisado)" else ""

def active_edit_fields(names):
    """Only active registry fields can be edited; archived fields remain raw/technical only."""
    return list(dict.fromkeys(n for n in names if n in FIELD_BY_NAME))

def editor_widget(ep,name,keyprefix="edit"):
    v,status=field_value(ep,name); text="" if v is None else str(v); key=f"{keyprefix}_{ep['episode_id']}_{name}"
    if is_simple_binary(name):
        current="No revisado" if status=="missing_no_revisado" else "No disponible (revisado)" if status in {"missing_revisado","invalid_zero_to_missing"} else "Sí" if str(v)=="1" else "No" if str(v)=="0" else "No revisado"
        return st.selectbox(flabel(name),list(BINARY_OPTIONS),index=list(BINARY_OPTIONS).index(current),key=key)
    if is_date_field(name): return date_widget(name,text,status,key=key)
    if name in FIELD_OPTIONS: return categorical_widget(name,text,status,key=key)
    m=fmeta(name)
    if m.get("data_kind")=="text" or name in {"DIAG_INGRE","DIAG_ALTA","TRATAMIENT","EVOLUCION","ECOCARDIO","OBS_EVOL","OBSERVACIO"} or len(text)>180:
        return st.text_area(flabel(name),value=text,height=90,key=key)
    return st.text_input(flabel(name),value=text,key=key)

def normalize_editor(name,display_value):
    if is_simple_binary(name): return normalize_field(name,BINARY_OPTIONS[display_value])
    return normalize_field(name,display_value)

@st.cache_data(ttl=30,show_spinner=False)
def _all_episode_summaries_cached():
    out=[]; offset=0; batch=1000
    while True:
        part=(supabase.table("fp5_episodes").select("episode_id,patient_id,source_row,raw_data,validated_data,updated_at").range(offset,offset+batch-1).execute().data or [])
        out.extend(part)
        if len(part)<batch: break
        offset += batch
    return out

def _clear_data_caches():
    try: _all_episode_summaries_cached.clear()
    except Exception: pass
    try: _all_patients_cached.clear()
    except Exception: pass
    try: _episode_counts_cached.clear()
    except Exception: pass

def episode_counts_from_rows(rows):
    c={}
    for e in rows:
        pid=e.get("patient_id")
        if pid: c[pid]=c.get(pid,0)+1
    return c

def effective_value(e, field):
    """Value used by the UI: explicit validated override, otherwise original FP5."""
    validated = e.get("validated_data") or {}
    raw = e.get("raw_data") or {}
    if field in validated:
        return validated.get(field)
    return raw.get(field, "")

def current_status(e):
    ing=clean(effective_value(e,"FECHA_INGR")); alt=clean(effective_value(e,"FECHA_ALTA")); asig=clean(effective_value(e,"FECHA_ASIG"))
    if ing and not alt and not asig: return "Pendiente asignación"
    if ing and not alt and asig: return "Ingresado"
    if alt: return "Alta"
    return "Sin fecha"


def filter_operational_rows(view="Ingresados ahora",search="",doctor="Todos",date_from=None,date_to=None):
    rows=_all_episode_summaries_cached(); counts=episode_counts_from_rows(rows); search=clean(search).lower()
    out=[]
    for e in rows:
        ing=clean(effective_value(e,"FECHA_INGR")); alt=clean(effective_value(e,"FECHA_ALTA")); asig=clean(effective_value(e,"FECHA_ASIG"))
        nhc=clean(effective_value(e,"NHC")); name=clean(effective_value(e,"NOMBRE")); doc=clean(effective_value(e,"CARDIOLOGO"))
        d=valid_date_text(ing) if ing else None
        if view=="Ingresados ahora" and not (ing and not alt): continue
        if view=="Pendientes de asignación" and not (ing and not asig and not alt): continue
        if view=="Dados de alta" and not alt: continue
        if view=="Reingresos" and counts.get(e.get("patient_id"),0)<=1: continue
        if view=="Todos" and not (ing or alt or nhc or name): continue
        if search and not any(search in clean(x).lower() for x in [nhc,name,clean(effective_value(e,"DIAG_INGRE")),clean(effective_value(e,"DIAG_ALTA"))]): continue
        if doctor!="Todos" and doc!=doctor: continue
        if date_from and (not d or d.date()<date_from): continue
        if date_to and (not d or d.date()>date_to): continue
        out.append({
            "episode_id":e["episode_id"],"patient_id":e["patient_id"],"source_row":e.get("source_row"),
            "NHC":nhc,"Nombre":name,"Edad":clean(effective_value(e,"EDAD")),"Cama":clean(effective_value(e,"CAMA")),
            "Fecha ingreso":display_date(ing),"Fecha asignación":display_date(clean(effective_value(e,"FECHA_ASIG"))),"Fecha alta":display_date(clean(effective_value(e,"FECHA_ALTA"))),
            "Días":clean(effective_value(e,"DIAS_ASIG")),"Médico":doc,"Procedencia":clean(effective_value(e,"PROCEDENCI")),
            "Diagnóstico ingreso":clean(effective_value(e,"DIAG_INGRE")),"Episodios paciente":counts.get(e.get("patient_id"),1),"_episode":e
        })
    out.sort(key=lambda x: (valid_date_text(x["Fecha ingreso"]) or datetime(1900,1,1), clean(x["Cama"]), clean(x["Nombre"])), reverse=True)
    return out


def ensure_patient_for_episode(ep, validated, changed_fields):
    current_nhc=clean(effective_value(ep,"NHC"))
    current_name=clean(effective_value(ep,"NOMBRE"))
    new_nhc=clean(validated.get("NHC",current_nhc)) if "NHC" in changed_fields else current_nhc
    new_name=clean(validated.get("NOMBRE",current_name)) if "NOMBRE" in changed_fields else current_name
    target_pid=patient_key(new_nhc,int(ep["source_row"]))
    patient_payload={"patient_id":target_pid,"nhc":new_nhc or None,"updated_at":now_iso()}
    if "NOMBRE" in changed_fields:
        patient_payload["display_name"]=new_name or None
    elif target_pid != ep["patient_id"]:
        patient_payload["display_name"]=current_name or None
    supabase.table("fp5_patients").upsert(patient_payload,on_conflict="patient_id").execute()
    return target_pid

def validate_episode_changes(ep, changes):
    """Cross-field checks applied before any clinical change is persisted."""
    merged = {}
    for field in FIELD_BY_NAME:
        merged[field] = effective_value(ep, field)
    for field, pair in changes.items():
        value, _status = pair
        merged[field] = value

    fi = to_date_obj(merged.get("FECHA_INGR"))
    fa = to_date_obj(merged.get("FECHA_ASIG"))
    fd = to_date_obj(merged.get("FECHA_ALTA"))
    if fa and fi and fa < fi:
        raise ValueError("La fecha de asignación no puede ser anterior a la fecha de ingreso.")
    if fd and fi and fd < fi:
        raise ValueError("La fecha de alta no puede ser anterior a la fecha de ingreso.")
    return True

def safe_save_bulk(ep,changes,section):
    try:
        return save_bulk(ep,changes,section)
    except Exception as exc:
        st.error(f"No se pudo guardar {section.lower()}. El registro no se ha quedado a medias.")
        st.caption(f"Detalle técnico: {type(exc).__name__}: {str(exc)[:500]}")
        return None

def save_bulk(ep,changes,section):
    if not changes:
        st.info(f"{section}: no hay cambios."); return
    validated=dict(ep.get("validated_data") or {}); statuses=dict(ep.get("field_status") or {})
    audits=[]
    for field,(new,status) in changes.items():
        old=validated.get(field) if field in validated else effective_value(ep,field)
        if old==new and statuses.get(field)==status: continue
        validated[field]=new; statuses[field]=status; audits.append((field,old,new))
    if not audits:
        st.info(f"{section}: no hay cambios."); return
    validate_episode_changes(ep, {field: (newv, statuses[field]) for field, _old, newv in audits})
    changed_fields={x[0] for x in audits}
    target_pid=ensure_patient_for_episode(ep,validated,changed_fields)
    payload={"validated_data":validated,"field_status":statuses,"patient_id":target_pid,"updated_at":now_iso(),"updated_by":st.session_state.get("user_label","web")}
    q=supabase.table("fp5_episodes").update(payload).eq("episode_id",ep["episode_id"])
    if ep.get("updated_at"): q=q.eq("updated_at",ep["updated_at"])
    res=q.select("episode_id,patient_id,updated_at,validated_data,field_status").execute()
    if not res.data: raise RuntimeError("El episodio ha cambiado desde que se abrió. Recarga el registro y vuelve a guardar.")
    if target_pid != ep["patient_id"]:
        old_count=(supabase.table("fp5_episodes").select("episode_id").eq("patient_id",ep["patient_id"]).limit(1).execute().data or [])
        if not old_count:
            supabase.table("fp5_patients").delete().eq("patient_id",ep["patient_id"]).execute()
    for field,old,newv in audits:
        supabase.table("fp5_audit_log").insert({"episode_id":ep["episode_id"],"patient_id":target_pid,"action":"UPDATE","field":field,"old_value":old,"new_value":newv,"snapshot":validated,"source":"manual","changed_at":now_iso(),"changed_by":st.session_state.get("user_label","web")}).execute()
    _clear_data_caches()
    st.success(f"{section}: guardados {len(audits)} cambios.")
    st.rerun()

def next_manual_source_row():
    # source_row is an INTEGER in the existing Supabase schema and imported
    # FP5 rows use positive values (1..14297). Manual episodes therefore use
    # a separate negative sequence, avoiding timestamp-in-milliseconds overflow.
    res = (
        supabase.table("fp5_episodes")
        .select("source_row")
        .lt("source_row", 0)
        .order("source_row", desc=True)
        .limit(1)
        .execute()
    )
    if res.data:
        return int(res.data[0]["source_row"]) - 1
    return -1


def create_manual_episode(data):
    for _ in range(5):
        source_row=next_manual_source_row(); nhc=clean(data.get("NHC")); pid=patient_key(nhc,source_row); eid=f"EP-MAN-{abs(source_row)}"
        try:
            supabase.table("fp5_patients").upsert({"patient_id":pid,"nhc":nhc or None,"display_name":clean(data.get("NOMBRE")) or None,"updated_at":now_iso()},on_conflict="patient_id").execute()
            raw={f["field"]:"" for f in SOURCE_FIELDS}
            for k,v in data.items():
                if k in {f["field"] for f in SOURCE_FIELDS}: raw[k]=clean(v)
            normalized,statuses=normalize_row({f["field"]:raw.get(f["field"],"") for f in FIELDS})
            supabase.table("fp5_episodes").insert({"episode_id":eid,"patient_id":pid,"source_row":source_row,"raw_data":raw,"validated_data":normalized,"field_status":statuses,"updated_at":now_iso(),"updated_by":"web"}).execute()
            _clear_data_caches(); return pid,eid
        except Exception as exc:
            msg=str(exc).lower()
            if "duplicate key" in msg or "23505" in msg or "unique constraint" in msg: continue
            raise
    raise RuntimeError("No se pudo generar un identificador único para el nuevo ingreso. Vuelve a intentarlo.")


OP_CSS="""
<style>
.main-title{font-size:2.0rem;font-weight:800;color:#102a43;margin:0}
.subtitle{color:#62778a;margin-top:3px;margin-bottom:14px}
.viewbar{background:#f6f9fb;border:1px solid #dbe6ee;border-radius:14px;padding:6px}
.field-card{border:1px solid #e1e8ee;border-radius:10px;padding:9px 11px;background:#fff;min-height:84px;margin-bottom:8px}
.field-title{font-size:.74rem;font-weight:750;color:#657887;text-transform:uppercase;letter-spacing:.025em}
.field-value{font-size:1rem;font-weight:650;color:#102a43;margin-top:6px;word-break:break-word}
.field-footer{font-size:.68rem;color:#8897a4;margin-top:7px;display:flex;gap:7px;align-items:center}
.status{padding:2px 7px;border-radius:999px;font-weight:700}.status.ok{background:#edf8f0;color:#1d6b3d}.status.reviewed{background:#fff3df;color:#94600d}.status.notreviewed{background:#eef2f5;color:#667785}.status.bad{background:#fde9e7;color:#a53b32}
.patient-banner{border:1px solid #dce7ee;border-radius:16px;padding:15px 18px;background:linear-gradient(135deg,#f7fbff,#eff8f4,#fff);margin-bottom:12px}.patient-name{font-size:1.55rem;font-weight:800;color:#102a43}.patient-meta{color:#5b7182;margin-top:4px}.patient-diag{font-weight:650;color:#183b56;margin-top:8px}
.st-key-quicklist{background:#fbfdff}
</style>
"""

# -----------------------------
# Session / app UI
# -----------------------------
for k,d in {"page":"home","selected_episode":"","selected_patient":"","search":"","clinical_text":"","user_label":"web"}.items():
    st.session_state.setdefault(k,d)

try:
    seed_field_definitions()
except Exception as e:
    st.error(f"No se pudo sincronizar el diccionario con Supabase: {e}")
    st.stop()

st.markdown(OP_CSS,unsafe_allow_html=True)

# Header
st.markdown('<div class="main-title">🏥 Cardiología · Ingresos</div><div class="subtitle">Gestión diaria del ingreso → recogida al alta → evolución → historial del paciente · v15</div>',unsafe_allow_html=True)

nav=st.columns(8)
nav_labels=[("📋 Ingresos","home"),("🟠 Pendientes","pending"),("✅ Ingresados","current"),("🟢 Altas","discharged"),("🔁 Reingresos","readmit"),("➕ Nuevo ingreso","new_episode"),("👤 Pacientes","patients"),("⚙️ Administración","admin")]
for i,(lab,page) in enumerate(nav_labels):
    if nav[i].button(lab,use_container_width=True,key=f"nav_{page}"):
        st.session_state.page=page
        st.rerun()

if st.session_state.page in {"home","pending","current","discharged","readmit"}:
    view={"home":"Todos","pending":"Pendientes de asignación","current":"Ingresados ahora","discharged":"Dados de alta","readmit":"Reingresos"}[st.session_state.page]
    st.subheader({"Todos":"Listado de ingresos","Pendientes de asignación":"Ingresos pendientes de asignación","Ingresados ahora":"Ingresados actualmente","Dados de alta":"Ingresos dados de alta","Reingresos":"Episodios de pacientes con más de un ingreso"}[view])
    c1,c2,c3,c4=st.columns([3,1.5,1.5,1.2])
    with c1: search=st.text_input("Buscar NHC, nombre o diagnóstico",value=st.session_state.search,key="op_search")
    with c2:
        all_eps=_all_episode_summaries_cached()
        docs_freq=[str(x.get("value")) for x in frequency_entries("CARDIOLOGO") if str(x.get("value"))]
        docs_current=[clean(effective_value(e,"CARDIOLOGO")) for e in all_eps if clean(effective_value(e,"CARDIOLOGO"))]
        docs=[]
        for d in docs_freq+sorted(set(docs_current)):
            if d and d not in docs: docs.append(d)
        doctor=st.selectbox("Médico",["Todos"]+docs,key="op_doc")
    with c3:
        quick_date=st.selectbox("Fecha",["Todas","Hoy","Últimos 7 días","Últimos 30 días"],key="op_date")
    with c4:
        sort_mode=st.selectbox("Orden",["Fecha ingreso","Cama","Nombre","Médico"],key="op_sort")
    today=date.today(); df=None; dt=None
    if quick_date=="Hoy": df=dt=today
    elif quick_date=="Últimos 7 días": df=today-timedelta(days=7); dt=today
    elif quick_date=="Últimos 30 días": df=today-timedelta(days=30); dt=today
    rows=filter_operational_rows(view,search,doctor,df,dt)
    if sort_mode=="Cama": rows.sort(key=lambda x:clean(x["Cama"]))
    elif sort_mode=="Nombre": rows.sort(key=lambda x:clean(x["Nombre"]))
    elif sort_mode=="Médico": rows.sort(key=lambda x:clean(x["Médico"]))
    if view=="Todos":
        all_rows=rows
        metric_rows={
            "Pendientes": filter_operational_rows("Pendientes de asignación",search,doctor,df,dt),
            "Ingresados": filter_operational_rows("Ingresados ahora",search,doctor,df,dt),
            "Altas": filter_operational_rows("Dados de alta",search,doctor,df,dt),
            "Reingresos": filter_operational_rows("Reingresos",search,doctor,df,dt),
        }
        m1,m2,m3,m4=st.columns(4)
        m1.metric("Pendientes de asignación",len(metric_rows["Pendientes"]))
        m2.metric("Ingresados actualmente",len(metric_rows["Ingresados"]))
        m3.metric("Dados de alta",len(metric_rows["Altas"]))
        m4.metric("Episodios de reingreso",len(metric_rows["Reingresos"]))
    st.caption(f"{len(rows)} episodios")
    if rows:
        display_cols=["Cama","NHC","Nombre","Edad","Fecha ingreso","Días","Médico","Fecha asignación","Fecha alta","Diagnóstico ingreso"]
        table=pd.DataFrame([{k:r[k] for k in display_cols} for r in rows])
        st.dataframe(table,use_container_width=True,hide_index=True,height=430)
        idx=st.selectbox("Seleccionar ingreso",range(len(rows)),format_func=lambda i:f"{rows[i]['Cama'] or '—'} · {rows[i]['NHC'] or 'sin NHC'} · {rows[i]['Nombre'] or 'sin nombre'} · {rows[i]['Fecha ingreso'] or 'sin fecha'}")
        r=rows[idx]; st.session_state.selected_episode=r["episode_id"]; st.session_state.selected_patient=r["patient_id"]
        b1,b2,b3,b4,b5=st.columns(5)
        if b1.button("📥 Abrir ingreso",type="primary",use_container_width=True): st.session_state.page="episode"; st.rerun()
        if b2.button("👤 Historial paciente",use_container_width=True): st.session_state.page="patient"; st.rerun()
        if b3.button("✏️ Asignación",use_container_width=True): st.session_state.quick_edit=True; st.rerun()
        if b4.button("🏁 Alta",use_container_width=True): st.session_state.quick_discharge=True; st.rerun()
        if b5.download_button("⬇️ CSV",data=table.to_csv(index=False).encode("utf-8-sig"),file_name="listado_ingresos.csv",mime="text/csv",use_container_width=True): pass
        if st.session_state.get("quick_edit"):
            ep=get_episode(r["episode_id"])
            st.markdown("### Cambio rápido de asignación")
            q=st.form("quick_assign")
            with q:
                a,b,c=q.columns(3)
                bed=categorical_widget("CAMA",clean(effective_value(ep,"CAMA")),(ep.get("field_status") or {}).get("CAMA","missing_no_revisado"),key="quick_assign_bed")
                doc=categorical_widget("CARDIOLOGO",clean(effective_value(ep,"CARDIOLOGO")),(ep.get("field_status") or {}).get("CARDIOLOGO","missing_no_revisado"),key="quick_assign_doctor")
                fa=date_widget("FECHA_ASIG",clean(effective_value(ep,"FECHA_ASIG")),(ep.get("field_status") or {}).get("FECHA_ASIG","missing_no_revisado"),key="quick_assign_date")
                if q.form_submit_button("Guardar asignación",type="primary"):
                    safe_save_bulk(ep,{"CAMA":normalize_field("CAMA",bed),"CARDIOLOGO":normalize_field("CARDIOLOGO",doc),"FECHA_ASIG":normalize_field("FECHA_ASIG",fa)},"Asignación")
        if st.session_state.get("quick_discharge"):
            ep=get_episode(r["episode_id"])
            st.markdown("### Dar de alta")
            q=st.form("quick_discharge_form")
            with q:
                fa=date_widget("FECHA_ALTA",clean(effective_value(ep,"FECHA_ALTA")),(ep.get("field_status") or {}).get("FECHA_ALTA","missing_no_revisado"),key="quick_discharge_date",required=True)
                dest=categorical_widget("DESTINO_AL",clean(effective_value(ep,"DESTINO_AL")),(ep.get("field_status") or {}).get("DESTINO_AL","missing_no_revisado"),key="quick_discharge_dest")
                if q.form_submit_button("Guardar alta",type="primary"):
                    if fa in ("", "555", None):
                        st.error("La fecha de alta es obligatoria para cerrar el episodio.")
                    else:
                        safe_save_bulk(ep,{"FECHA_ALTA":normalize_field("FECHA_ALTA",fa),"DESTINO_AL":normalize_field("DESTINO_AL",dest)},"Alta")
    else: st.info("No hay episodios que cumplan esos criterios.")

elif st.session_state.page=="patients":
    st.subheader("Pacientes")
    c1,c2=st.columns([3,2])
    with c1: q=st.text_input("Buscar por NHC o nombre",value=st.session_state.search,key="patient_search")
    with c2: ef=st.selectbox("Historial",["Todos","Solo 1 ingreso","Más de 1 ingreso"],key="patient_filter")
    rows,total=list_patients(1,200,q,"Todos")
    rows=[r for r in rows if ef=="Todos" or (ef=="Solo 1 ingreso" and r["episode_count"]==1) or (ef=="Más de 1 ingreso" and r["episode_count"]>1)]
    st.caption(f"{len(rows)} pacientes")
    if rows:
        table=pd.DataFrame([{"NHC":r.get("nhc"),"Nombre":r.get("display_name"),"Ingresos":r.get("episode_count",0)} for r in rows])
        st.dataframe(table,use_container_width=True,hide_index=True)
        i=st.selectbox("Abrir paciente",range(len(rows)),format_func=lambda i:f"{rows[i].get('nhc') or '—'} · {rows[i].get('display_name') or 'sin nombre'} · {rows[i].get('episode_count',0)} ingresos")
        if st.button("Abrir historial",type="primary"): st.session_state.selected_patient=rows[i]["patient_id"]; st.session_state.page="patient"; st.rerun()
    else: st.info("No se han encontrado pacientes.")

elif st.session_state.page=="patient":
    pid=st.session_state.selected_patient
    patient=get_patient(pid); episodes=patient_episodes(pid) if pid else []
    if not patient or not episodes: st.error("No se encontró el paciente."); st.stop()
    name=patient.get("display_name") or "Paciente"; nhc=patient.get("nhc") or "—"
    st.markdown(f'<div class="patient-banner"><div class="patient-name">{name}</div><div class="patient-meta">NHC {nhc} · {len(episodes)} ingresos</div></div>',unsafe_allow_html=True)
    if st.button("← Volver al listado de pacientes"): st.session_state.page="patients"; st.rerun()
    rows=[]
    for e in episodes:
        rows.append((e,clean(effective_value(e,"FECHA_INGR")),clean(effective_value(e,"FECHA_ALTA")),clean(effective_value(e,"DIAG_ALTA")) or clean(effective_value(e,"DIAG_INGRE"))))
    st.dataframe(pd.DataFrame([{ "Fecha ingreso":display_date(x[1]),"Fecha alta":display_date(x[2]),"Diagnóstico":x[3],"Médico":clean(effective_value(x[0],"CARDIOLOGO")),"Cama":clean(effective_value(x[0],"CAMA"))} for x in rows]),use_container_width=True,hide_index=True)
    i=st.selectbox("Seleccionar ingreso",range(len(rows)),format_func=lambda i:f"{display_date(rows[i][1]) or 'sin fecha'} · {rows[i][3][:90]}")
    st.session_state.selected_episode=rows[i][0]["episode_id"]
    if st.button("Abrir episodio",type="primary"): st.session_state.page="episode"; st.rerun()

elif st.session_state.page=="episode":
    ep=get_episode(st.session_state.selected_episode) if st.session_state.selected_episode else None
    if not ep: st.error("No se encontró el episodio."); st.stop()
    patient=get_patient(ep["patient_id"])
    r=ep.get("raw_data") or {}; name=patient.get("display_name") if patient else r.get("NOMBRE"); nhc=patient.get("nhc") if patient else r.get("NHC")
    diag=clean(effective_value(ep,"DIAG_INGRE")) or clean(effective_value(ep,"DIAG_ALTA"))
    st.markdown(f'<div class="patient-banner"><div class="patient-name">{name or "Paciente"}</div><div class="patient-meta">NHC {nhc or "—"} · Ingreso {display_date(clean(effective_value(ep,"FECHA_INGR"))) or "—"} · Alta {display_date(clean(effective_value(ep,"FECHA_ALTA"))) or "—"} · Médico {clean(effective_value(ep,"CARDIOLOGO")) or "—"}</div><div class="patient-diag">{diag or "Sin diagnóstico"}</div></div>',unsafe_allow_html=True)
    if st.button("← Volver a ingresos"): st.session_state.page="home"; st.rerun()
    show_missing=st.checkbox("Mostrar campos no revisados / no disponibles",value=False,key=f"show_missing_{ep['episode_id']}")
    tabs=st.tabs(["📥 Datos Iniciales del ingreso","🏁 Datos de alta","📈 Evolución","🧪 Exploraciones y tecnología","🧾 Original / IA / Auditoría"])
    with tabs[0]:
        render_group(ep,"Identificación y asignación",["NHC","NOMBRE","EDAD","VARON","FECHA_INGR","FECHA_ASIG","CAMA","CARDIOLOGO"],cols=4,show_missing=show_missing)
        render_group(ep,"Procedencia y organización",["PROCEDENCI","CENTRO","INGRESO_PR","PRIMER_EPI","DIAS_ASIG"],cols=4,show_missing=show_missing)
        render_group(ep,"Motivo del ingreso",["DIAG_INGRE","OBSERVACIO"],cols=2,show_missing=show_missing)
        with st.expander("✏️ Editar datos iniciales"):
            form=st.form("edit_ingreso")
            values={}
            with form:
                for n in active_edit_fields(INGRESO_FIELDS):
                    values[n]=editor_widget(ep,n)
                if form.form_submit_button("Guardar ingreso",type="primary"):
                    changes={n:normalize_editor(n,values[n]) for n in values}; safe_save_bulk(ep,changes,"Datos iniciales")
    with tabs[1]:
        render_group(ep,"Factores de riesgo y antecedentes",ALTA_RISK_FIELDS,cols=5,show_missing=show_missing)
        render_group(ep,"Diagnóstico de alta",ALTA_DIAG_FIELDS,cols=2,show_missing=show_missing)
        render_group(ep,"Analítica",ALTA_LAB_FIELDS,cols=5,show_missing=show_missing)
        render_group(ep,"Tratamiento al alta",ALTA_TX_FIELDS,cols=5,show_missing=show_missing)
        render_group(ep,"Cierre del episodio",ALTA_ADMIN_FIELDS,cols=3,show_missing=show_missing)
        if st.button("✏️ Editar datos de alta",key="open_alta_edit"): st.session_state.edit_sheet="alta"; st.rerun()
        if st.session_state.get("edit_sheet")=="alta":
            form=st.form("edit_alta"); values={}; fields=active_edit_fields(ALTA_RISK_FIELDS+ALTA_DIAG_FIELDS+ALTA_LAB_FIELDS+ALTA_TX_FIELDS+ALTA_ADMIN_FIELDS)
            with form:
                for n in fields: values[n]=editor_widget(ep,n)
                if form.form_submit_button("Guardar datos de alta",type="primary"):
                    safe_save_bulk(ep,{n:normalize_editor(n,values[n]) for n in values},"Datos de alta")
    with tabs[2]:
        render_group(ep,"Eventos cardiovasculares y reingresos",EV_EVENT_FIELDS,cols=4,show_missing=show_missing)
        render_group(ep,"Seguimiento analítico",EV_LAB_FIELDS,cols=5,show_missing=show_missing)
        render_group(ep,"Seguimiento terapéutico",EV_FOLLOW_FIELDS,cols=4,show_missing=show_missing)
        render_group(ep,"Notas",EV_TEXT_FIELDS,cols=2,show_missing=show_missing)
        if st.button("✏️ Editar evolución",key="open_ev_edit"): st.session_state.edit_sheet="evolution"; st.rerun()
        if st.session_state.get("edit_sheet")=="evolution":
            form=st.form("edit_evolution"); values={}; fields=active_edit_fields(EV_EVENT_FIELDS+EV_LAB_FIELDS+EV_FOLLOW_FIELDS+EV_TEXT_FIELDS)
            with form:
                for n in fields: values[n]=editor_widget(ep,n)
                if form.form_submit_button("Guardar evolución",type="primary"):
                    safe_save_bulk(ep,{n:normalize_editor(n,values[n]) for n in values},"Evolución")
    with tabs[3]:
        render_group(ep,"Imagen y pruebas funcionales",TECH_IMAGE_FIELDS,cols=4,show_missing=show_missing)
        render_group(ep,"Coronario e intervencionismo",TECH_CORONARY_FIELDS,cols=4,show_missing=show_missing)
        render_group(ep,"Electrofisiología y dispositivos",TECH_DEVICE_FIELDS,cols=4,show_missing=show_missing)
        if st.button("✏️ Editar exploraciones y tecnología",key="open_tech_edit"): st.session_state.edit_sheet="tech"; st.rerun()
        if st.session_state.get("edit_sheet")=="tech":
            form=st.form("edit_tech"); values={}; fields=active_edit_fields(TECH_IMAGE_FIELDS+TECH_CORONARY_FIELDS+TECH_DEVICE_FIELDS)
            with form:
                for n in fields: values[n]=editor_widget(ep,n)
                if form.form_submit_button("Guardar exploraciones / tecnología",type="primary"):
                    safe_save_bulk(ep,{n:normalize_editor(n,values[n]) for n in values},"Exploraciones / tecnología")
    with tabs[4]:
        t=st.tabs(["Original FP5","Validado","IA","Auditoría"])
        with t[0]:
            st.caption("Copia de los valores importados de FileMaker. Solo lectura.")
            items=[{"Campo":f["field"],"Descripción":f.get("label",f["field"]),"Valor original":(ep.get("raw_data") or {}).get(f["field"],"")} for f in SOURCE_FIELDS]
            st.dataframe(pd.DataFrame(items),use_container_width=True,hide_index=True,height=480)
        with t[1]:
            items=[{"Campo":f["field"],"Descripción":f["label"],"Valor validado":(ep.get("validated_data") or {}).get(f["field"]),"Estado":(ep.get("field_status") or {}).get(f["field"],"")} for f in FIELDS if f["field"] in (ep.get("validated_data") or {})]
            st.dataframe(pd.DataFrame(items),use_container_width=True,hide_index=True,height=480)
        with t[2]:
            st.session_state.patient_id=ep["patient_id"]
            st.text_area("Texto clínico para extracción",value=st.session_state.get("clinical_text",""),height=220,key="episode_ai_text")
            if st.button("🧠 Extraer con IA",type="primary"):
                text=st.session_state.get("episode_ai_text","")
                if text.strip():
                    if not GEMINI_API_KEY or not GEMINI_MODEL:
                        st.error("La IA no está configurada en Streamlit Secrets (GEMINI_API_KEY y GEMINI_MODEL).")
                    else:
                        try:
                            with st.spinner(f"Analizando con {GEMINI_MODEL}…"):
                                props=extract_ai(text); save_ai_proposals(ep["episode_id"],props); st.success(f"Propuestas: {len(props)}"); st.rerun()
                        except Exception as exc:
                            st.error("No se pudo completar la extracción con IA.")
                            st.caption(f"Detalle técnico: {type(exc).__name__}: {str(exc)[:500]}")
            pending=pending_ai(ep["episode_id"])
            for row in pending:
                st.markdown(f"**{FIELD_LABELS.get(row['field'],row['field'])}** → `{row['proposed_value']}`")
                with st.expander("Evidencia"): st.write(row.get("evidence") or "Sin evidencia")
                a,b=st.columns(2)
                if a.button("✓ Aceptar",key=f"aiok{row['id']}"):
                    try:
                        accept_ai(ep,row); st.rerun()
                    except Exception as exc:
                        st.error("No se pudo aceptar la propuesta de IA.")
                        st.caption(f"Detalle técnico: {type(exc).__name__}: {str(exc)[:500]}")
                if b.button("✕ Rechazar",key=f"aino{row['id']}"):
                    try:
                        reject_ai(row); st.rerun()
                    except Exception as exc:
                        st.error("No se pudo rechazar la propuesta de IA.")
                        st.caption(f"Detalle técnico: {type(exc).__name__}: {str(exc)[:500]}")
        with t[3]:
            a=audit_for_episode(ep["episode_id"]); st.dataframe(pd.DataFrame(a),use_container_width=True,hide_index=True,height=480) if a else st.info("Sin movimientos")

elif st.session_state.page=="new_episode":
    st.subheader("Nuevo ingreso")
    st.caption("Crea un nuevo episodio sin borrar ni modificar el original FP5.")
    with st.form("new_episode"):
        c1,c2,c3=st.columns(3)
        nhc=c1.text_input("NHC")
        name=c2.text_input("Nombre")
        bed=categorical_widget("CAMA",key="new_bed")
        c1,c2,c3=st.columns(3)
        with c1: fin=st.date_input("Fecha de ingreso",value=date.today(),key="new_fecha_ingr",format="DD/MM/YYYY")
        with c2: fa=date_widget("FECHA_ASIG",status="missing_no_revisado",key="new_fecha_asig")
        with c3: doc=categorical_widget("CARDIOLOGO",key="new_doctor")
        proc=categorical_widget("PROCEDENCI",key="new_procedencia")
        diag=st.text_area("Diagnóstico de ingreso",height=90)
        if st.form_submit_button("Crear ingreso",type="primary"):
            try:
                pid,eid=create_manual_episode({"NHC":nhc,"NOMBRE":name,"CAMA":bed,"FECHA_INGR":fin,"FECHA_ASIG":fa,"CARDIOLOGO":doc,"PROCEDENCI":proc,"DIAG_INGRE":diag})
                st.session_state.selected_patient=pid; st.session_state.selected_episode=eid; st.session_state.page="episode"; st.rerun()
            except Exception as exc:
                st.error("No se pudo crear el nuevo ingreso. No se ha guardado un episodio incompleto.")
                st.caption(f"Detalle técnico: {type(exc).__name__}: {str(exc)[:500]}")

elif st.session_state.page=="admin":
    st.subheader("Administración")
    st.write(f"Diccionario: **{len(SOURCE_FIELDS)} variables de origen · {len(FIELDS)} activas** · criterio: **> {PREVALENCE_THRESHOLD:.0f}% de episodios con valor**, más fechas estructuralmente necesarias para eventos activos. Las restantes quedan solo en raw_data.")
    uploaded=st.file_uploader("CSV de FileMaker (sin cabecera)",type=["csv"])
    if uploaded:
        content=uploaded.getvalue()
        try:
            profile=profile_import(parse_fp5_csv_bytes(content)); a,b,c,d=st.columns(4)
            a.metric("Filas",profile["rows"]); b.metric("Columnas FP5",profile["source_cols"]); c.metric("NHC distintos",profile["nonempty_nhc"]); d.metric("Duplicados exactos",profile["exact_duplicate_rows"])
            st.info(f"Variables activas: {profile['active_cols']} · retiradas/archivadas: {profile['retired_cols']} · `555`: {profile['missing_555_cells']} · vacíos: {profile['blank_cells']} · ceros: {profile['zero_cells']}")
            if st.button("🔎 Auditar sin importar"): st.json(profile)
            import_mode=st.radio("Modo de carga",["Piloto · primeros 100 episodios","Carga completa · exactamente 14.297 episodios"],horizontal=True)
            is_pilot=import_mode.startswith("Piloto")
            full_ok=True
            if not is_pilot:
                full_ok=profile["rows"]==14297 and st.checkbox("He comprobado que este CSV contiene exactamente 14.297 filas y 208 columnas.",key="confirm_full_import")
                if profile["rows"]!=14297: st.error(f"La carga completa está bloqueada: el CSV tiene {profile['rows']} filas, no 14.297.")
            button_label="⬆️ Importar piloto (100)" if is_pilot else "⬆️ Importar los 14.297 episodios"
            if st.button(button_label,type="primary",disabled=not full_ok):
                result=import_fp5(content,uploaded.name,dry_run=False,limit=100 if is_pilot else None)
                st.success(f"Procesados: {result['episodes_created']} episodios y {result['patients_created']} pacientes.")
                _clear_data_caches()
        except Exception as e:
            st.error("No se pudo completar la carga del CSV. No se ha considerado la importación como completada.")
            st.caption(f"Detalle técnico: {type(e).__name__}: {str(e)[:500]}")
    st.markdown("### Variables archivadas")
    st.caption(f"{len(RETIRED_META)} variables quedan fuera de la recogida activa por baja frecuencia (≤10%), legado/constantes o por decisión previa; sus valores originales siguen conservados en `raw_data`.")
    if st.checkbox("Ver variables archivadas", value=False):
        st.dataframe(pd.DataFrame(RETIRED_META), use_container_width=True, hide_index=True)
    st.markdown("### Reglas FP5")
    st.write("`555` = missing revisado · vacío = missing no revisado · el 0 se interpreta por variable · raw_data conserva el original · cada fila = un ingreso")
