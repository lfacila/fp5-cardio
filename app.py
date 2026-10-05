import hashlib
import io
import json
import math
import re
from datetime import datetime
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
REGISTRY = json.loads((BASE_DIR / "field_registry_v3.json").read_text(encoding="utf-8"))
SOURCE_REGISTRY = json.loads((BASE_DIR / "source_fields_208.json").read_text(encoding="utf-8"))
RETIRED_FIELDS = {x["field"] for x in SOURCE_REGISTRY["fields"]} - {f["field"] for f in REGISTRY["fields"]}
SOURCE_FIELDS = SOURCE_REGISTRY["fields"]
FIELDS = REGISTRY["fields"]
FIELD_BY_NAME = {f["field"]: f for f in FIELDS}
FIELD_LABELS = {f["field"]: f["label"] for f in FIELDS}
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
    return "" if v is None or (isinstance(v, float) and math.isnan(v)) else str(v).strip()


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
    raw = clean(raw)
    if raw == "":
        return None, "missing_no_revisado"
    if raw == "555":
        return None, "missing_revisado"

    if meta.get("effective_type", meta["filemaker_type"]) == "D":
        try:
            dt = datetime.strptime(raw, "%d/%m/%Y")
            if dt.year < 1900 or dt.year > datetime.now().year + 1:
                return raw, "invalid_date"
            return dt.date().isoformat(), "available"
        except ValueError:
            try:
                dt = datetime.strptime(raw, "%d-%m-%Y")
                if dt.year < 1900 or dt.year > datetime.now().year + 1:
                    return raw, "invalid_date"
                return dt.date().isoformat(), "available"
            except ValueError:
                return raw, "invalid_date"

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
    # El export original tiene 208 columnas. La aplicación activa usa 204:
    # cuatro campos han sido retirados explícitamente del modelo clínico.
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
    # Mantiene únicamente las variables activas (retired fields quedan fuera del modelo).
    active_cols = [f["field"] for f in FIELDS]
    return df[active_cols].copy()


def profile_import(df):
    nhc = df["NHC"] if "NHC" in df else pd.Series([], dtype=str)
    dup_full = df.duplicated(keep=False)
    return {
        "rows": len(df),
        "cols": len(df.columns),
        "source_cols": len(SOURCE_FIELDS),
        "active_cols": len(FIELDS),
        "retired_cols": len(RETIRED_FIELDS),
        "nonempty_nhc": int(nhc[nhc != ""].nunique()) if len(nhc) else 0,
        "blank_nhc": int((nhc == "").sum()) if len(nhc) else 0,
        "repeated_nhc_rows": int(nhc[nhc != ""].duplicated(keep=False).sum()) if len(nhc) else 0,
        "exact_duplicate_rows": int(dup_full.sum()),
        "blank_cells": int((df == "").sum().sum()),
        "missing_555_cells": int((df == "555").sum().sum()),
        "zero_cells": int((df == "0").sum().sum()),
        "empty_fields": [f["field"] for f in FIELDS if (df[f["field"]] == "").all()],
    }


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
        if episode_filter == "Solo 1 episodio" and n != 1:
            continue
        if episode_filter == "Más de 1 episodio" and n <= 1:
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


def import_fp5(content, filename, dry_run=False, limit=None):
    df = parse_fp5_csv_bytes(content)
    if limit is not None:
        df = df.head(int(limit)).copy()
    profile = profile_import(df)
    if dry_run:
        return profile

    file_hash = hashlib.sha256(content).hexdigest()
    run = supabase.table("fp5_import_runs").insert({
        "file_name": filename,
        "file_sha256": file_hash,
        "source_rows": len(df),
        "status": "started",
        "started_by": "web",
        "started_at": now_iso(),
    }).select("id").execute()
    run_id = run.data[0]["id"] if run.data else None

    patients = {}
    episode_rows = []
    for idx, row in enumerate(df.itertuples(index=False, name=None), start=1):
        raw = {FIELDS[j]["field"]: clean(row[j]) for j in range(len(FIELDS))}
        nhc = raw.get("NHC", "")
        pkey = patient_key(nhc, idx)
        ep_id = episode_key(idx, row)
        name = raw.get("NOMBRE", "")
        if pkey not in patients:
            patients[pkey] = {
                "patient_id": pkey,
                "nhc": nhc or None,
                "display_name": name or None,
                "updated_at": now_iso(),
            }
        elif not patients[pkey].get("display_name") and name:
            patients[pkey]["display_name"] = name
        validated, statuses = normalize_row(raw)
        episode_rows.append({
            "episode_id": ep_id,
            "patient_id": pkey,
            "source_row": idx,
            "raw_data": raw,
            "validated_data": validated,
            "field_status": statuses,
            "updated_at": now_iso(),
            "updated_by": "FP5_IMPORT",
        })

    patient_rows = list(patients.values())
    for i in range(0, len(patient_rows), 100):
        supabase.table("fp5_patients").upsert(patient_rows[i:i+100], on_conflict="patient_id").execute()

    progress = st.progress(0, text="Importando episodios...")
    for i in range(0, len(episode_rows), 100):
        batch = episode_rows[i:i+100]
        supabase.table("fp5_episodes").upsert(batch, on_conflict="episode_id").execute()
        progress.progress(min(1.0, (i + len(batch)) / len(episode_rows)), text=f"Episodios {i+len(batch)} / {len(episode_rows)}")
    progress.empty()

    if run_id:
        supabase.table("fp5_import_runs").update({
            "inserted_patients": len(patient_rows),
            "inserted_episodes": len(episode_rows),
            "status": "completed",
            "completed_at": now_iso(),
        }).eq("id", run_id).execute()
    profile["patients_created"] = len(patient_rows)
    profile["episodes_created"] = len(episode_rows)
    return profile


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
    "DIAG_INGRE","OBSERVACIO","DIAS_ASIG"
]

ALTA_RISK_FIELDS = [
    "HTA","DM","DL","OBESIDAD","FUMADOR","EXFUMADOR","ECV_PREVIA","ERC",
    "CAR_FAM","ACXFA","ARRITMIA","EMBOLIA","ENF_VALV_P","VALVULA_AF",
    "VALV_MEC","TROMBO","HASBLED","FISTULA","IAMSEST2","IAM_ANT40","IC"
]
ALTA_DIAG_FIELDS=["DIAG_ALTA","GRUPO_DX"]
ALTA_LAB_FIELDS=["HB","HB1AC","ADE","GLUC","CR","NA","LDL","HDL","TRIG","ALBUMINA","PCR","INR_2","INR_3","INR_TOTALE","NITRITOS","FE","LPA"]
ALTA_TX_FIELDS=[
    "AAS","ACO","NACOS","APIXABAN","DABIGATRAN","RIVAROXABA","IECA","ARA2","BETABLOQ","ANTAG_ALDO","DIURETICOS",
    "ESTAT","ATORVASTAT","ROSUVASTAT","PITAVASTAT","EZETIM","EZE1","EZE2","BEMPE","PCSK9","VAZK","IVABRADINA","RANOLAZINA",
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

def fmeta(name):
    return FIELD_BY_NAME.get(name, {"field":name,"label":name,"data_kind":"text","filemaker_type":"C","observed_domain_preview":[]})

def flabel(name):
    return fmeta(name).get("label") or name

def is_simple_binary(name):
    dom=set(str(x) for x in fmeta(name).get("observed_domain_preview",[]))
    return bool(dom) and dom.issubset({"0","1","555"})

def field_value(ep,name):
    raw=ep.get("raw_data") or {}; val=ep.get("validated_data") or {}; sts=ep.get("field_status") or {}
    status=sts.get(name,"")
    if name in val and val[name] is not None: return val[name], status or "available"
    rv=raw.get(name,"")
    if rv=="555": return None,"missing_revisado"
    if rv=="": return None,"missing_no_revisado"
    return rv,status or "available"

def value_text(name,value,status):
    if status=="available" and is_simple_binary(name):
        return {"1":"Sí","0":"No"}.get(str(value),str(value))
    if status=="missing_revisado": return "No disponible · revisado"
    if status=="missing_no_revisado": return "No revisado"
    if status=="invalid_zero_to_missing": return "No disponible"
    return "—" if value in (None,"") else str(value)

def badge(status):
    classes={"available":"ok","missing_revisado":"reviewed","missing_no_revisado":"notreviewed","invalid_zero_to_missing":"reviewed","invalid_date":"bad","invalid_numeric":"bad"}
    labels={"available":"Disponible","missing_revisado":"Revisado · no disponible","missing_no_revisado":"No revisado","invalid_zero_to_missing":"No disponible","invalid_date":"Fecha no válida","invalid_numeric":"Valor no válido"}
    c=classes.get(status,"notreviewed"); lab=labels.get(status,status or "")
    return f'<span class="status {c}">{lab}</span>'

def render_field_card(ep,name,show_missing=False):
    v,st=field_value(ep,name)
    if not show_missing and v in (None,""): return False
    text=value_text(name,v,st)
    st.markdown(f'<div class="field-card"><div class="field-title">{flabel(name)}</div><div class="field-value">{text}</div><div class="field-footer">{badge(st)} <span>{name}</span></div></div>',unsafe_allow_html=True)
    return True

def render_group(ep,title,names,cols=4,show_missing=False):
    names=[n for n in names if n in FIELD_BY_NAME]
    names=[n for n in names if show_missing or field_value(ep,n)[0] not in (None,"")]
    if not names:
        return
    st.markdown(f'#### {title}')
    for i in range(0,len(names),cols):
        cs=st.columns(cols)
        for j,n in enumerate(names[i:i+cols]):
            with cs[j]: render_field_card(ep,n,show_missing=True)

def valid_date_text(v):
    try:
        dt=datetime.strptime(str(v),"%d/%m/%Y"); return dt
    except Exception: return None

def parse_date_key(v):
    dt=valid_date_text(v)
    return dt or datetime(1900,1,1)

@st.cache_data(ttl=30,show_spinner=False)
def _all_episode_summaries_cached():
    out=[]; offset=0; batch=1000
    while True:
        part=(supabase.table("fp5_episodes").select("episode_id,patient_id,source_row,raw_data,validated_data,field_status,updated_at").range(offset,offset+batch-1).execute().data or [])
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

def current_status(e):
    r=e.get("raw_data") or {}; ing=clean(r.get("FECHA_INGR")); alt=clean(r.get("FECHA_ALTA")); asig=clean(r.get("FECHA_ASIG"))
    if ing and not alt and not asig: return "Pendiente asignación"
    if ing and not alt and asig: return "Ingresado"
    if alt: return "Alta"
    return "Sin fecha"

def filter_operational_rows(view="Ingresados ahora",search="",doctor="Todos",date_from=None,date_to=None):
    rows=_all_episode_summaries_cached(); counts=episode_counts_from_rows(rows); search=clean(search).lower()
    out=[]
    for e in rows:
        r=e.get("raw_data") or {}; ing=clean(r.get("FECHA_INGR")); alt=clean(r.get("FECHA_ALTA")); asig=clean(r.get("FECHA_ASIG")); nhc=clean(r.get("NHC")); name=clean(r.get("NOMBRE")); doc=clean(r.get("CARDIOLOGO"))
        if view=="Ingresados ahora" and not (ing and not alt): continue
        if view=="Pendientes de asignación" and not (ing and not asig and not alt): continue
        if view=="Dados de alta" and not alt: continue
        if view=="Reingresos" and counts.get(e.get("patient_id"),0)<=1: continue
        if view=="Todos" and not (ing or alt or nhc or name): continue
        if search and not any(search in x.lower() for x in [nhc,name,clean(r.get("DIAG_INGRE")).lower(),clean(r.get("DIAG_ALTA")).lower()]): continue
        if doctor!="Todos" and doc!=doctor: continue
        d=valid_date_text(ing)
        if date_from and d and d.date()<date_from: continue
        if date_to and d and d.date()>date_to: continue
        item={"episode_id":e["episode_id"],"patient_id":e["patient_id"],"source_row":e.get("source_row"),"NHC":nhc,"Nombre":name,"Edad":clean(r.get("EDAD")),"Cama":clean(r.get("CAMA")),"Fecha ingreso":ing,"Fecha asignación":asig,"Fecha alta":alt,"Días":clean(r.get("DIAS_ASIG")),"Médico":doc,"Procedencia":clean(r.get("PROCEDENCI")),"Diagnóstico ingreso":clean(r.get("DIAG_INGRE")),"Episodios paciente":counts.get(e.get("patient_id"),1),"_raw":r}
        out.append(item)
    out.sort(key=lambda x: (parse_date_key(x["Fecha ingreso"]), clean(x["Cama"]), clean(x["Nombre"])), reverse=True)
    return out

def save_bulk(ep,changes,section):
    if not changes: st.info(f"{section}: no hay cambios."); return
    validated=dict(ep.get("validated_data") or {}); statuses=dict(ep.get("field_status") or {})
    audits=[]
    for field,(new,status) in changes.items():
        old=validated.get(field)
        if old==new and statuses.get(field)==status: continue
        validated[field]=new; statuses[field]=status
        audits.append((field,old,new))
    if not audits: st.info(f"{section}: no hay cambios."); return
    res=supabase.table("fp5_episodes").update({"validated_data":validated,"field_status":statuses,"updated_at":now_iso(),"updated_by":st.session_state.get("user_label","web")}).eq("episode_id",ep["episode_id"]).execute()
    if not res.data: raise RuntimeError("No se pudo guardar el episodio.")
    for field,old,new in audits:
        supabase.table("fp5_audit_log").insert({"episode_id":ep["episode_id"],"patient_id":ep["patient_id"],"action":"UPDATE","field":field,"old_value":old,"new_value":new,"snapshot":validated,"source":"manual","changed_at":now_iso(),"changed_by":st.session_state.get("user_label","web")}).execute()
    _clear_data_caches()
    st.success(f"{section}: guardados {len(audits)} cambios.")
    st.rerun()

def editor_widget(ep,name,keyprefix="edit"):
    v,st=field_value(ep,name); raw=(ep.get("raw_data") or {}).get(name,"")
    key=f"{keyprefix}_{ep['episode_id']}_{name}"
    if is_simple_binary(name):
        current = "No revisado" if st=="missing_no_revisado" else "No disponible (revisado)" if st in {"missing_revisado","invalid_zero_to_missing"} else "Sí" if str(v)=="1" else "No" if str(v)=="0" else "No revisado"
        return st.selectbox(flabel(name),list(BINARY_OPTIONS),index=list(BINARY_OPTIONS).index(current),key=key,help=f"FP5: {name}")
    m=fmeta(name); text="" if v is None else str(v)
    if m.get("effective_type",m.get("filemaker_type"))=="D":
        return st.text_input(flabel(name),value=text,key=key,help="Formato: dd/mm/aaaa")
    if m.get("data_kind")=="text" or name in {"DIAG_INGRE","DIAG_ALTA","TRATAMIENT","EVOLUCION","ECOCARDIO","OBS_EVOL","OBSERVACIO"} or len(text)>180:
        return st.text_area(flabel(name),value=text,height=90,key=key)
    return st.text_input(flabel(name),value=text,key=key)

def normalize_editor(name,display_value):
    if is_simple_binary(name):
        raw=BINARY_OPTIONS[display_value]; return normalize_field(name,raw)
    return normalize_field(name,display_value)

def create_manual_episode(data):
    nhc=clean(data.get("NHC")); source_row=-int(datetime.now().timestamp()*1000)
    pid=patient_key(nhc,source_row)
    existing=supabase.table("fp5_patients").select("patient_id").eq("patient_id",pid).limit(1).execute().data
    if not existing:
        supabase.table("fp5_patients").insert({"patient_id":pid,"nhc":nhc,"display_name":clean(data.get("NOMBRE")),"updated_at":now_iso()}).execute()
    raw={f["field"]:"" for f in FIELDS}
    for k,v in data.items():
        if k in FIELD_BY_NAME: raw[k]=clean(v)
    normalized,statuses=normalize_row(raw)
    eid=f"EP-MAN-{abs(source_row)}"
    supabase.table("fp5_episodes").insert({"episode_id":eid,"patient_id":pid,"source_row":source_row,"raw_data":raw,"validated_data":normalized,"field_status":statuses,"updated_at":now_iso(),"updated_by":"web"}).execute()
    _clear_data_caches(); return pid,eid

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
    pass

st.markdown(OP_CSS,unsafe_allow_html=True)

# Header
st.markdown('<div class="main-title">🏥 Cardiología · Ingresos</div><div class="subtitle">Gestión diaria del ingreso → recogida al alta → evolución → historial del paciente</div>',unsafe_allow_html=True)

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
        docs=sorted({clean((e.get("raw_data") or {}).get("CARDIOLOGO")) for e in _all_episode_summaries_cached() if clean((e.get("raw_data") or {}).get("CARDIOLOGO"))})
        doctor=st.selectbox("Médico",["Todos"]+docs,key="op_doc")
    with c3:
        quick_date=st.selectbox("Fecha",["Todas","Hoy","Últimos 7 días","Últimos 30 días"],key="op_date")
    with c4:
        sort_mode=st.selectbox("Orden",["Fecha ingreso","Cama","Nombre","Médico"],key="op_sort")
    from datetime import date,timedelta
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
                bed=a.text_input("Cama",value=clean((ep.get("raw_data") or {}).get("CAMA")))
                doc=b.text_input("Médico asignado",value=clean((ep.get("raw_data") or {}).get("CARDIOLOGO")))
                fa=c.text_input("Fecha asignación",value=clean((ep.get("raw_data") or {}).get("FECHA_ASIG")))
                if q.form_submit_button("Guardar asignación",type="primary"):
                    changes={}
                    for f,v in {"CAMA":bed,"CARDIOLOGO":doc,"FECHA_ASIG":fa}.items(): changes[f]=normalize_field(f,v)
                    save_bulk(ep,changes,"Asignación")
        if st.session_state.get("quick_discharge"):
            ep=get_episode(r["episode_id"])
            st.markdown("### Dar de alta")
            q=st.form("quick_discharge_form")
            with q:
                fa=q.text_input("Fecha de alta",value=clean((ep.get("raw_data") or {}).get("FECHA_ALTA")) or today.strftime("%d/%m/%Y"))
                dest=q.text_input("Destino al alta",value=clean((ep.get("raw_data") or {}).get("DESTINO_AL")))
                if q.form_submit_button("Guardar alta",type="primary"):
                    save_bulk(ep,{"FECHA_ALTA":normalize_field("FECHA_ALTA",fa),"DESTINO_AL":normalize_field("DESTINO_AL",dest)},"Alta")
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
        r=e.get("raw_data") or {}; rows.append((e,clean(r.get("FECHA_INGR")),clean(r.get("FECHA_ALTA")),clean(r.get("DIAG_ALTA")) or clean(r.get("DIAG_INGRE"))))
    st.dataframe(pd.DataFrame([{ "Fecha ingreso":x[1],"Fecha alta":x[2],"Diagnóstico":x[3],"Médico":clean((x[0].get('raw_data') or {}).get('CARDIOLOGO')),"Cama":clean((x[0].get('raw_data') or {}).get('CAMA'))} for x in rows]),use_container_width=True,hide_index=True)
    i=st.selectbox("Seleccionar ingreso",range(len(rows)),format_func=lambda i:f"{rows[i][1] or 'sin fecha'} · {rows[i][3][:90]}")
    st.session_state.selected_episode=rows[i][0]["episode_id"]
    if st.button("Abrir episodio",type="primary"): st.session_state.page="episode"; st.rerun()

elif st.session_state.page=="episode":
    ep=get_episode(st.session_state.selected_episode) if st.session_state.selected_episode else None
    if not ep: st.error("No se encontró el episodio."); st.stop()
    patient=get_patient(ep["patient_id"])
    r=ep.get("raw_data") or {}; name=patient.get("display_name") if patient else r.get("NOMBRE"); nhc=patient.get("nhc") if patient else r.get("NHC")
    diag=clean(r.get("DIAG_INGRE")) or clean(r.get("DIAG_ALTA"))
    st.markdown(f'<div class="patient-banner"><div class="patient-name">{name or "Paciente"}</div><div class="patient-meta">NHC {nhc or "—"} · Ingreso {clean(r.get("FECHA_INGR")) or "—"} · Alta {clean(r.get("FECHA_ALTA")) or "—"} · Médico {clean(r.get("CARDIOLOGO")) or "—"}</div><div class="patient-diag">{diag or "Sin diagnóstico"}</div></div>',unsafe_allow_html=True)
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
                for i,n in enumerate(INGRESO_FIELDS):
                    if n not in FIELD_BY_NAME: continue
                    values[n]=editor_widget(ep,n)
                if form.form_submit_button("Guardar ingreso",type="primary"):
                    changes={n:normalize_editor(n,values[n]) for n in values}; save_bulk(ep,changes,"Datos iniciales")
    with tabs[1]:
        render_group(ep,"Factores de riesgo y antecedentes",ALTA_RISK_FIELDS,cols=5,show_missing=show_missing)
        render_group(ep,"Diagnóstico de alta",ALTA_DIAG_FIELDS,cols=2,show_missing=show_missing)
        render_group(ep,"Analítica",ALTA_LAB_FIELDS,cols=5,show_missing=show_missing)
        render_group(ep,"Tratamiento al alta",ALTA_TX_FIELDS,cols=5,show_missing=show_missing)
        render_group(ep,"Cierre del episodio",ALTA_ADMIN_FIELDS,cols=3,show_missing=show_missing)
        if st.button("✏️ Editar datos de alta",key="open_alta_edit"): st.session_state.edit_sheet="alta"; st.rerun()
        if st.session_state.get("edit_sheet")=="alta":
            form=st.form("edit_alta"); values={}; fields=list(dict.fromkeys(ALTA_RISK_FIELDS+ALTA_DIAG_FIELDS+ALTA_LAB_FIELDS+ALTA_TX_FIELDS+ALTA_ADMIN_FIELDS))
            with form:
                for n in fields: values[n]=editor_widget(ep,n)
                if form.form_submit_button("Guardar datos de alta",type="primary"):
                    save_bulk(ep,{n:normalize_editor(n,values[n]) for n in values},"Datos de alta")
    with tabs[2]:
        render_group(ep,"Eventos cardiovasculares y reingresos",EV_EVENT_FIELDS,cols=4,show_missing=show_missing)
        render_group(ep,"Seguimiento analítico",EV_LAB_FIELDS,cols=5,show_missing=show_missing)
        render_group(ep,"Seguimiento terapéutico",EV_FOLLOW_FIELDS,cols=4,show_missing=show_missing)
        render_group(ep,"Notas",EV_TEXT_FIELDS,cols=2,show_missing=show_missing)
        if st.button("✏️ Editar evolución",key="open_ev_edit"): st.session_state.edit_sheet="evolution"; st.rerun()
        if st.session_state.get("edit_sheet")=="evolution":
            form=st.form("edit_evolution"); values={}; fields=list(dict.fromkeys(EV_EVENT_FIELDS+EV_LAB_FIELDS+EV_FOLLOW_FIELDS+EV_TEXT_FIELDS))
            with form:
                for n in fields: values[n]=editor_widget(ep,n)
                if form.form_submit_button("Guardar evolución",type="primary"):
                    save_bulk(ep,{n:normalize_editor(n,values[n]) for n in values},"Evolución")
    with tabs[3]:
        render_group(ep,"Imagen y pruebas funcionales",TECH_IMAGE_FIELDS,cols=4,show_missing=show_missing)
        render_group(ep,"Coronario e intervencionismo",TECH_CORONARY_FIELDS,cols=4,show_missing=show_missing)
        render_group(ep,"Electrofisiología y dispositivos",TECH_DEVICE_FIELDS,cols=4,show_missing=show_missing)
        if st.button("✏️ Editar exploraciones y tecnología",key="open_tech_edit"): st.session_state.edit_sheet="tech"; st.rerun()
        if st.session_state.get("edit_sheet")=="tech":
            form=st.form("edit_tech"); values={}; fields=list(dict.fromkeys(TECH_IMAGE_FIELDS+TECH_CORONARY_FIELDS+TECH_DEVICE_FIELDS))
            with form:
                for n in fields: values[n]=editor_widget(ep,n)
                if form.form_submit_button("Guardar exploraciones / tecnología",type="primary"):
                    save_bulk(ep,{n:normalize_editor(n,values[n]) for n in values},"Exploraciones / tecnología")
    with tabs[4]:
        t=st.tabs(["Original FP5","Validado","IA","Auditoría"])
        with t[0]:
            st.caption("Copia de los valores importados de FileMaker. Solo lectura.")
            items=[{"Campo":f["field"],"Descripción":f["label"],"Valor original":(ep.get("raw_data") or {}).get(f["field"],"")} for f in FIELDS]
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
                    with st.spinner(f"Analizando con {GEMINI_MODEL or 'modelo configurado'}…"):
                        props=extract_ai(text); save_ai_proposals(ep["episode_id"],props); st.success(f"Propuestas: {len(props)}"); st.rerun()
            pending=pending_ai(ep["episode_id"])
            for row in pending:
                st.markdown(f"**{FIELD_LABELS.get(row['field'],row['field'])}** → `{row['proposed_value']}`")
                with st.expander("Evidencia"): st.write(row.get("evidence") or "Sin evidencia")
                a,b=st.columns(2)
                if a.button("✓ Aceptar",key=f"aiok{row['id']}"): accept_ai(ep,row); st.rerun()
                if b.button("✕ Rechazar",key=f"aino{row['id']}"): reject_ai(row); st.rerun()
        with t[3]:
            a=audit_for_episode(ep["episode_id"]); st.dataframe(pd.DataFrame(a),use_container_width=True,hide_index=True,height=480) if a else st.info("Sin movimientos")

elif st.session_state.page=="new_episode":
    st.subheader("Nuevo ingreso")
    st.caption("Crea un nuevo episodio sin borrar ni modificar el original FP5.")
    with st.form("new_episode"):
        c1,c2,c3=st.columns(3); nhc=c1.text_input("NHC"); name=c2.text_input("Nombre"); bed=c3.text_input("Cama")
        c1,c2,c3=st.columns(3); fin=c1.text_input("Fecha de ingreso",value=date.today().strftime("%d/%m/%Y")); fa=c2.text_input("Fecha de asignación",value=date.today().strftime("%d/%m/%Y")); doc=c3.text_input("Médico asignado")
        proc=st.text_input("Procedencia"); diag=st.text_area("Diagnóstico de ingreso",height=90)
        if st.form_submit_button("Crear ingreso",type="primary"):
            pid,eid=create_manual_episode({"NHC":nhc,"NOMBRE":name,"CAMA":bed,"FECHA_INGR":fin,"FECHA_ASIG":fa,"CARDIOLOGO":doc,"PROCEDENCI":proc,"DIAG_INGRE":diag})
            st.session_state.selected_patient=pid; st.session_state.selected_episode=eid; st.session_state.page="episode"; st.rerun()

elif st.session_state.page=="admin":
    st.subheader("Administración")
    st.write(f"Diccionario: **{len(SOURCE_FIELDS)} variables de origen · {len(FIELDS)} activas**")
    uploaded=st.file_uploader("CSV de FileMaker (sin cabecera)",type=["csv"])
    if uploaded:
        content=uploaded.getvalue()
        try:
            profile=profile_import(parse_fp5_csv_bytes(content)); a,b,c,d=st.columns(4)
            a.metric("Filas",profile["rows"]); b.metric("Columnas",profile["cols"]); c.metric("NHC distintos",profile["nonempty_nhc"]); d.metric("Duplicados exactos",profile["exact_duplicate_rows"])
            st.info(f"`555`: {profile['missing_555_cells']} · vacíos: {profile['blank_cells']} · ceros: {profile['zero_cells']}")
            if st.button("🔎 Auditar sin importar"): st.json(profile)
            import_mode=st.radio("Modo de carga",["Piloto · primeros 100 episodios","Carga completa · 14.297 episodios"],horizontal=True)
            is_pilot=import_mode.startswith("Piloto")
            if st.button("⬆️ Importar piloto (100)" if is_pilot else "⬆️ Importar los 14.297 episodios",type="primary"):
                result=import_fp5(content,uploaded.name,dry_run=False,limit=100 if is_pilot else None); st.success(f"Importados: {result['episodes_created']} episodios y {result['patients_created']} pacientes."); _clear_data_caches()
        except Exception as e: st.error(f"CSV no válido: {e}")
    st.markdown("### Reglas FP5")
    st.write("`555` = missing revisado · vacío = missing no revisado · el 0 se interpreta por variable · raw_data conserva el original · cada fila = un ingreso")
