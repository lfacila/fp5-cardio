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
# Patient sheet design
# -----------------------------
INGRESO_FIELDS = [
    "NHC","NOMBRE","EDAD","VARON","FECHA_INGR","FECHA_ASIG","CAMA",
    "CARDIOLOGO","PROCEDENCI","CENTRO","INGRESO_PR","PRIMER_EPI",
    "DIAG_INGRE","OBSERVACIO","CP","CSIP","DIAS_ASIG"
]
ALTA_RISK_FIELDS = [
    "HTA","DM","DL","OBESIDAD","FUMADOR","EXFUMADOR","ECV_PREVIA","ERC",
    "CAR_FAM","ACXFA","ARRITMIA","EMBOLIA","ENF_VALV_P","VALVULA_AF",
    "VALV_MEC","TROMBO","HASBLED","FISTULA","IAMSEST2","IAM_ANT40","IC"
]
ALTA_DIAG_FIELDS = ["DIAG_ALTA","GRUPO_DX"]
ALTA_LAB_FIELDS = [
    "HB","HB1AC","ADE","GLUC","CR","NA","LDL","HDL","TRIG","ALBUMINA","PCR",
    "INR_2","INR_3","INR_TOTALE","NITRITOS","FE","AREA","LPA","PAS2"
]
ALTA_TX_FIELDS = [
    "AAS","ACO","NACOS","APIXABAN","DABIGATRAN","RIVAROXABA",
    "IECA","ARA2","BETABLOQ","ANTAG_ALDO","DIURETICOS",
    "ESTAT","ATORVASTAT","ROSUVASTAT","PITAVASTAT","EZETIM","EZE1","EZE2",
    "BEMPE","PCSK9","VAZK","IVABRADINA","RANOLAZINA",
    "CLOPI","PRASUG","TICA","INSULINA","ADO","DPP4","GLINIDAS","GLP1",
    "ISLGT2","ENTRESTO","CALCIOANT","ANTIARRITM"
]
ALTA_ADMIN_FIELDS = ["FECHA_ALTA","CARDIOLOGO1","DESTINO_AL","FIRMADO_EC","TRATAMIENT"]
TECH_IMAGE_FIELDS = ["ECO","FE","ECOCARDIO","ETE","HOLTER","ERGOMETRIA","RMC","SPECT","TC_CORON"]
TECH_CORONARY_FIELDS = ["CATE","ACTP_PRIM","ACTP_TCI","STENT","CIR_CAR","CCA","COMP_VASC","FECHA_CCA"]
TECH_DEVICE_FIELDS = ["EEF","CVE","MCP","TAVI","UCO","FISTULA"]
EV_EVENT_FIELDS = [
    "EVO_IAM","EVO_IAM_FE","EVO_IC","EVO_IC_FEC","EVO_AVC","EVO_AVC_FE",
    "EVO_ACVISQ","EVO_ACVISQ1","EVO_ACVHEM","EVO_ACVHEM1","EVO_CABG","EVO_CABG_F",
    "EVO_REVSC","EVO_REVSC_","EVO_REINGR","EVO_REINGR1","EVO_REINGR2","EVO_REINGR3",
    "EVO_EXITUS","EVO_EXITUS1","EVO_SANG","EVO_SANGFE","EVO_SANG_G","EVO_SANG_G1",
    "EVO_AI","EVO_AI_FEC"
]
EV_LAB_FIELDS = ["ADE_2","FECHA_ADE_","HB1AC_2","FECHA_A1C2","LDL2","LDL3","HDL2","HDL3","CR_2","CR2","TG2","K2","FECHA2"]
EV_FOLLOW_FIELDS = ["OPTIMZ","FASE2B","ENT_SEGUIM","ISLGT2_SEG","BETA_SEGUI","AMR_SEGUIM"]
EV_TEXT_FIELDS = ["EVOLUCION","OBS_EVOL","OBSERVACIO"]

def field_meta(name):
    return FIELD_BY_NAME.get(name, {"field": name, "label": name, "data_kind": "text"})

def field_label(name):
    return field_meta(name).get("label") or name

def shown_value(ep, field):
    raw = ep.get("raw_data") or {}
    validated = ep.get("validated_data") or {}
    statuses = ep.get("field_status") or {}
    status = statuses.get(field, "")
    if field in validated and validated.get(field) is not None:
        return validated.get(field), status or "available"
    rv = raw.get(field, "")
    if rv == "555": return None, "missing_revisado"
    if rv == "": return None, "missing_no_revisado"
    return rv, status or "available"

def status_label(status):
    return {"available":"Dato disponible","missing_revisado":"Missing revisado","missing_no_revisado":"No revisado","invalid_zero_to_missing":"0 tratado como missing","invalid_date":"Fecha no válida","invalid_numeric":"Valor no numérico"}.get(status, status or "")

def status_html(status):
    cls = {"available":"ok","missing_revisado":"reviewed","missing_no_revisado":"notreviewed","invalid_zero_to_missing":"reviewed","invalid_date":"error","invalid_numeric":"error"}.get(status,"notreviewed")
    return f'<span class="fp5-status {cls}">{status_label(status)}</span>'

def format_display(value,status):
    return "—" if status in {"missing_revisado","missing_no_revisado"} or value in (None,"") else str(value)

def render_value_card(ep,field):
    value,status=shown_value(ep,field); label=field_label(field); text=format_display(value,status)
    if len(text)>360: text=text[:360]+"…"
    st.markdown(f"""
    <div class="fp5-card"><div class="fp5-label">{label}</div><div class="fp5-value">{text}</div><div class="fp5-meta">{status_html(status)} <span class="fp5-code">{field}</span></div></div>
    """,unsafe_allow_html=True)

def render_cards(ep,fields,cols=3,show_missing=False):
    fields=[f for f in fields if f in FIELD_BY_NAME]
    if not show_missing: fields=[f for f in fields if shown_value(ep,f)[0] not in (None,"")]
    if not fields: st.info("No hay datos disponibles en esta sección para este episodio."); return
    for i in range(0,len(fields),cols):
        c=st.columns(cols)
        for j,f in enumerate(fields[i:i+cols]):
            with c[j]: render_value_card(ep,f)

def render_text_block(ep,field,title=None):
    value,status=shown_value(ep,field); title=title or field_label(field)
    st.markdown(f"**{title}** {status_html(status)}",unsafe_allow_html=True)
    if value in (None,""): st.caption("Sin dato")
    else: st.text_area("",value=str(value),height=135,disabled=True,key=f"view_{ep['episode_id']}_{field}")

def editor_input(ep,field):
    meta=field_meta(field); current,status=shown_value(ep,field); label=field_label(field); txt="" if current is None else str(current)
    long_text=meta.get("data_kind")=="text" or field in {"DIAG_INGRE","DIAG_ALTA","TRATAMIENT","EVOLUCION","ECOCARDIO","OBS_EVOL","OBSERVACIO"} or len(txt)>180
    if long_text: new_val=st.text_area(label,value=txt,height=120,key=f"edit_{ep['episode_id']}_{field}")
    else: new_val=st.text_input(label,value=txt,key=f"edit_{ep['episode_id']}_{field}")
    st.caption(f"Original FP5: {(ep.get('raw_data') or {}).get(field,'')!r} · {status_label(status)}")
    return new_val

def save_section(ep,fields,title):
    validated=dict(ep.get('validated_data') or {}); statuses=dict(ep.get('field_status') or {}); changed=[]
    for field in fields:
        key=f"edit_{ep['episode_id']}_{field}"
        if field not in FIELD_BY_NAME or key not in st.session_state: continue
        norm,status=normalize_field(field,st.session_state[key]); old=validated.get(field)
        if old!=norm or statuses.get(field)!=status:
            validated[field]=norm; statuses[field]=status
            save_episode_validated(ep,validated,statuses,field,old,norm,"manual"); changed.append(field)
    st.success(f"{title}: guardados {len(changed)} cambios.") if changed else st.info(f"{title}: no hay cambios pendientes.")

def episode_date_label(ep):
    raw=ep.get('raw_data') or {}; fin=raw.get('FECHA_INGR') or 'sin fecha'; falt=raw.get('FECHA_ALTA') or 'sin alta'; diag=(raw.get('DIAG_INGRE') or '').replace('\n',' ').strip()
    return f"{fin} → {falt} · {diag[:80]}"

def render_patient_header(patient,episodes,current_ep):
    raw=current_ep.get('raw_data') or {}; name=patient.get('display_name') or raw.get('NOMBRE') or 'Paciente'; nhc=patient.get('nhc') or raw.get('NHC') or '—'; diag=raw.get('DIAG_INGRE') or raw.get('DIAG_ALTA') or 'Sin diagnóstico'; fin=raw.get('FECHA_INGR') or '—'; falt=raw.get('FECHA_ALTA') or '—'
    st.markdown(f"""
    <div class="patient-shell"><div class="patient-kicker">EPISODIO CARDIOLÓGICO</div><div class="patient-name">{name}</div><div class="patient-line"><b>NHC</b> {nhc} &nbsp; · &nbsp; <b>Ingreso</b> {fin} &nbsp; · &nbsp; <b>Alta</b> {falt}</div><div class="patient-diag">{diag}</div></div>
    """,unsafe_allow_html=True)
    a,b,c,d=st.columns(4); a.metric("Ingresos del paciente",len(episodes)); b.metric("Episodio seleccionado",current_ep.get('source_row') or '—'); c.metric("Médico asignado",raw.get('CARDIOLOGO') or '—'); d.metric("Cama",raw.get('CAMA') or '—')

PATIENT_CSS="""
<style>
.patient-shell{border:1px solid #dbe6ee;border-radius:18px;padding:18px 22px;background:linear-gradient(135deg,#f7fbff,#eef7f4,#fff);margin-bottom:14px}.patient-kicker{font-size:.72rem;font-weight:800;letter-spacing:.08em;color:#2b6f7e}.patient-name{font-size:1.65rem;font-weight:800;color:#102a43;margin-top:2px}.patient-line{color:#5f7386;margin-top:5px}.patient-diag{font-size:.96rem;color:#183b56;margin-top:10px;font-weight:650}.fp5-card{border:1px solid #e2e8ee;border-radius:13px;padding:12px 14px;min-height:98px;background:#fff;box-shadow:0 2px 9px rgba(15,49,72,.035);margin-bottom:10px}.fp5-label{font-size:.78rem;color:#627486;text-transform:uppercase;letter-spacing:.025em;font-weight:750}.fp5-value{font-size:1rem;color:#102a43;font-weight:650;margin-top:7px;word-break:break-word}.fp5-meta{font-size:.72rem;color:#8190a0;margin-top:8px;display:flex;align-items:center;gap:8px}.fp5-code{font-family:ui-monospace,monospace;color:#9aa6b2}.fp5-status{display:inline-block;padding:2px 7px;border-radius:999px;font-weight:700}.fp5-status.ok{background:#edf8f0;color:#1e6b3a}.fp5-status.reviewed{background:#fff4df;color:#95610d}.fp5-status.notreviewed{background:#f1f4f7;color:#687887}.fp5-status.error{background:#fdebea;color:#a33b33}div[data-testid="stTabs"] button{font-weight:700}
</style>
"""

# -----------------------------
# Session / UI
# -----------------------------
for k, default in {"page":"dashboard","patient_id":"","episode_id":"","search":"","patient_page":1,"clinical_text":"","user_label":"web"}.items(): st.session_state.setdefault(k,default)
try: seed_field_definitions()
except Exception as e: st.warning(f"No se pudo sincronizar el diccionario de campos todavía: {e}")
st.markdown(PATIENT_CSS,unsafe_allow_html=True)
st.markdown("""
<div style="padding:18px 22px;margin-bottom:14px;border-radius:18px;background:linear-gradient(135deg,#f7fbff,#eef7f4,#ffffff);border:1px solid #dbe6ee"><div style="font-size:.75rem;font-weight:700;letter-spacing:.08em;color:#2b6f7e">FP5 · CARDIOLOGÍA</div><div style="font-size:2rem;font-weight:800;color:#102a43">Base maestra hospitalaria</div><div style="color:#5b7083">Ingreso → alta → evolución tras el alta → exploraciones y tecnología → trazabilidad</div></div>
""",unsafe_allow_html=True)
nav=st.columns(4)
if nav[0].button("📊 Dashboard",use_container_width=True): st.session_state.page="dashboard"
if nav[1].button("👥 Pacientes",use_container_width=True): st.session_state.page="patients"
if nav[2].button("🧠 IA clínica",use_container_width=True): st.session_state.page="ai"
if nav[3].button("⚙️ Administración",use_container_width=True): st.session_state.page="admin"

if st.session_state.page=="dashboard":
    try: pcount=supabase.table("fp5_patients").select("patient_id",count="exact").limit(1).execute().count or 0; ecount=supabase.table("fp5_episodes").select("episode_id",count="exact").limit(1).execute().count or 0
    except Exception: pcount=ecount=0
    a,b,c,d=st.columns(4); a.metric("Pacientes",pcount); b.metric("Episodios",ecount); c.metric("Variables activas",len(FIELDS)); d.metric("IA",GEMINI_MODEL or "No configurada")
    st.info(f"Export original: {len(SOURCE_FIELDS)} columnas · modelo activo: {len(FIELDS)} · retiradas: {len(RETIRED_FIELDS)} · `555` = missing revisado · vacío = missing no revisado · el 0 se interpreta por variable.")
    st.success("El original FP5 queda protegido en `raw_data`; las modificaciones van a `validated_data` y a auditoría.")

elif st.session_state.page=="patients":
    st.subheader("Pacientes"); st.caption("Un paciente puede tener múltiples ingresos. La búsqueda se hace por NHC o nombre.")
    c1,c2,c3,c4=st.columns([3,2,1,1])
    with c1: st.session_state.search=st.text_input("Buscar por NHC o nombre",st.session_state.search)
    with c2: episode_filter=st.selectbox("Ingresos",["Todos","Solo 1 episodio","Más de 1 episodio"])
    with c3: page_size=st.selectbox("Por página",[25,50,100],index=0)
    with c4: st.session_state.patient_page=st.number_input("Página",min_value=1,value=int(st.session_state.patient_page),step=1)
    rows,total_filtered=list_patients(st.session_state.patient_page,page_size,st.session_state.search,episode_filter); st.caption(f"{total_filtered} pacientes que cumplen el filtro")
    if rows:
        st.dataframe(pd.DataFrame([{"NHC":r.get("nhc"),"Nombre":r.get("display_name"),"Ingresos":r.get("episode_count",0),"Actualizado":r.get("updated_at")} for r in rows]),use_container_width=True,hide_index=True)
        selected=st.selectbox("Seleccionar paciente",[r["patient_id"] for r in rows],format_func=lambda x: next((r["display_name"] or r["nhc"] or x for r in rows if r["patient_id"]==x),x))
        if st.button("Abrir ficha",type="primary",use_container_width=True):
            st.session_state.patient_id=selected; eps=patient_episodes(selected); st.session_state.episode_id=eps[0]["episode_id"] if eps else ""; st.session_state.page="patient"; st.rerun()
    else: st.info("No hay pacientes para esa búsqueda.")

elif st.session_state.page=="patient":
    pid=st.session_state.patient_id
    if not pid: st.info("Selecciona primero un paciente."); st.stop()
    patient=get_patient(pid); episodes=patient_episodes(pid)
    if not patient or not episodes: st.error("No se ha encontrado la ficha."); st.stop()
    if st.button("← Volver a pacientes"): st.session_state.page="patients"; st.rerun()
    labels=[episode_date_label(e) for e in episodes]; idx=max(0,next((i for i,e in enumerate(episodes) if e["episode_id"]==st.session_state.episode_id),0)); selected_label=st.selectbox("Ingreso / episodio",labels,index=idx); ep=episodes[labels.index(selected_label)]; st.session_state.episode_id=ep["episode_id"]
    render_patient_header(patient,episodes,ep)
    show_missing=st.checkbox("Mostrar también campos vacíos / no disponibles",value=False,key=f"show_missing_{ep['episode_id']}"); edit_mode=st.toggle("Modo edición",value=False,key=f"edit_mode_{ep['episode_id']}")
    tabs=st.tabs(["📥 Ingreso","🏁 Alta clínica","📈 Evolución tras el alta","🧪 Exploraciones y tecnología","🧾 Trazabilidad"])

    with tabs[0]:
        st.markdown("### Datos administrativos y clínicos del ingreso")
        if edit_mode:
            for i in range(0,len(INGRESO_FIELDS),3):
                cols=st.columns(3)
                for j,f in enumerate(INGRESO_FIELDS[i:i+3]):
                    with cols[j]: editor_input(ep,f)
            if st.button("💾 Guardar datos de ingreso",type="primary"): save_section(ep,INGRESO_FIELDS,"Ingreso")
        else:
            st.markdown("#### Identificación y asignación"); render_cards(ep,["NHC","NOMBRE","EDAD","VARON","FECHA_INGR","FECHA_ASIG","CAMA","CARDIOLOGO"],cols=4,show_missing=show_missing)
            st.markdown("#### Procedencia, asignación y diagnóstico"); render_cards(ep,["PROCEDENCI","CENTRO","INGRESO_PR","PRIMER_EPI","DIAS_ASIG","DIAG_INGRE","OBSERVACIO","CP","CSIP"],cols=3,show_missing=show_missing)

    with tabs[1]:
        sections=[("Factores de riesgo cardiovascular y antecedentes",ALTA_RISK_FIELDS),("Diagnóstico de alta",ALTA_DIAG_FIELDS),("Analítica",ALTA_LAB_FIELDS),("Tratamiento al alta",ALTA_TX_FIELDS),("Cierre del episodio",ALTA_ADMIN_FIELDS)]
        st.markdown("### Datos clínicos recogidos durante el ingreso y al alta")
        if edit_mode:
            all_fields=[]
            for title,fields in sections:
                st.markdown(f"#### {title}"); all_fields+=fields
                for i in range(0,len(fields),3):
                    cols=st.columns(3)
                    for j,f in enumerate(fields[i:i+3]):
                        with cols[j]: editor_input(ep,f)
            if st.button("💾 Guardar alta clínica",type="primary"): save_section(ep,list(dict.fromkeys(all_fields)),"Alta clínica")
        else:
            for title,fields in sections: st.markdown(f"#### {title}"); render_cards(ep,fields,cols=4,show_missing=show_missing)
            st.markdown("#### Textos clínicos"); left,right=st.columns(2)
            with left: render_text_block(ep,"DIAG_ALTA","Diagnóstico de alta"); render_text_block(ep,"ECOCARDIO","Informe ecocardiográfico")
            with right: render_text_block(ep,"TRATAMIENT","Tratamiento al alta"); render_text_block(ep,"EVOLUCION","Evolución durante el ingreso")

    with tabs[2]:
        st.markdown("### Evolución después del alta")
        groups=[("Eventos cardiovasculares y reingresos",EV_EVENT_FIELDS),("Seguimiento analítico",EV_LAB_FIELDS),("Seguimiento terapéutico",EV_FOLLOW_FIELDS),("Notas de evolución",EV_TEXT_FIELDS)]
        if edit_mode:
            all_fields=[]
            for title,fields in groups:
                st.markdown(f"#### {title}"); all_fields+=fields
                for i in range(0,len(fields),3):
                    cols=st.columns(3)
                    for j,f in enumerate(fields[i:i+3]):
                        with cols[j]: editor_input(ep,f)
            if st.button("💾 Guardar evolución",type="primary"): save_section(ep,list(dict.fromkeys(all_fields)),"Evolución")
        else:
            for title,fields in groups:
                st.markdown(f"#### {title}")
                if fields and fields[0] in EV_TEXT_FIELDS:
                    for f in fields: render_text_block(ep,f)
                else: render_cards(ep,fields,cols=4,show_missing=show_missing)

    with tabs[3]:
        st.markdown("### Exploraciones, procedimientos y tecnología")
        groups=[("Imagen y pruebas funcionales",TECH_IMAGE_FIELDS),("Coronario e intervencionismo",TECH_CORONARY_FIELDS),("Electrofisiología y dispositivos",TECH_DEVICE_FIELDS)]
        if edit_mode:
            all_fields=[]
            for title,fields in groups:
                st.markdown(f"#### {title}"); all_fields+=fields
                for i in range(0,len(fields),3):
                    cols=st.columns(3)
                    for j,f in enumerate(fields[i:i+3]):
                        with cols[j]: editor_input(ep,f)
            if st.button("💾 Guardar exploraciones / tecnología",type="primary"): save_section(ep,list(dict.fromkeys(all_fields)),"Exploraciones / tecnología")
        else:
            for title,fields in groups: st.markdown(f"#### {title}"); render_cards(ep,fields,cols=4,show_missing=show_missing)

    with tabs[4]:
        st.markdown("### Trazabilidad y capas técnicas")
        tech_tabs=st.tabs(["Original FP5","Validado","IA","Auditoría"])
        with tech_tabs[0]:
            items=[]
            for f in FIELDS:
                n=f["field"]; v=(ep.get("raw_data") or {}).get(n,"")
                if show_missing or v not in ("",None): items.append({"Campo":n,"Descripción":field_label(n),"Valor original":v,"Estado":(ep.get("field_status") or {}).get(n,"")})
            st.dataframe(pd.DataFrame(items),use_container_width=True,hide_index=True)
        with tech_tabs[1]:
            items=[]
            for f in FIELDS:
                n=f["field"]; v=(ep.get("validated_data") or {}).get(n); status=(ep.get("field_status") or {}).get(n,"")
                if show_missing or v not in ("",None): items.append({"Campo":n,"Descripción":field_label(n),"Valor validado":v,"Estado":status})
            st.dataframe(pd.DataFrame(items),use_container_width=True,hide_index=True)
        with tech_tabs[2]:
            pending=pending_ai(ep["episode_id"])
            if pending:
                for row in pending:
                    st.markdown(f"**{FIELD_LABELS.get(row['field'],row['field'])}** → `{row['proposed_value']}`")
                    with st.expander("Evidencia"): st.write(row.get("evidence") or "Sin evidencia")
                    x,y=st.columns(2)
                    if x.button("✓ Aceptar",key=f"ai_acc_{row['id']}"): accept_ai(ep,row); st.rerun()
                    if y.button("✕ Rechazar",key=f"ai_rej_{row['id']}"): reject_ai(row); st.rerun()
            else: st.info("No hay propuestas IA pendientes.")
        with tech_tabs[3]:
            audit=audit_for_episode(ep["episode_id"])
            if audit: st.dataframe(pd.DataFrame(audit),use_container_width=True,hide_index=True)
            else: st.info("Sin movimientos registrados.")

elif st.session_state.page=="ai":
    st.subheader("IA clínica"); st.caption("La IA propone únicamente variables existentes en el FP5 y requiere revisión humana.")
    pid=st.session_state.patient_id
    if pid:
        patient=get_patient(pid); episodes=patient_episodes(pid); ep=episodes[0] if episodes else None
        if ep: render_patient_header(patient,episodes,ep)
    else: ep=None
    text=st.text_area("Pega aquí informe, evolución o alta",value=st.session_state.clinical_text,height=280); st.session_state.clinical_text=text
    if st.button("🧠 Extraer variables",type="primary"):
        if not text.strip(): st.warning("Pega primero el texto clínico.")
        elif ep is None: st.warning("Selecciona primero un episodio.")
        else:
            with st.spinner(f"Analizando con {GEMINI_MODEL or 'modelo configurado'}..."):
                try: props=extract_ai(text); save_ai_proposals(ep["episode_id"],props); st.success(f"Propuestas generadas: {len(props)}"); st.rerun()
                except Exception as e: st.error(f"No se pudo extraer el texto: {e}")
    if ep:
        pending=pending_ai(ep["episode_id"])
        if pending:
            st.markdown("### Propuestas pendientes")
            for row in pending:
                st.markdown(f"**{FIELD_LABELS.get(row['field'],row['field'])}** → `{row['proposed_value']}`")
                with st.expander("Evidencia"): st.write(row.get("evidence") or "Sin evidencia")
                x,y=st.columns(2)
                if x.button("✓ Aceptar",key=f"ai_acc2_{row['id']}"): accept_ai(ep,row); st.rerun()
                if y.button("✕ Rechazar",key=f"ai_rej2_{row['id']}"): reject_ai(row); st.rerun()

elif st.session_state.page=="admin":
    st.subheader("Administración"); st.write(f"Diccionario: **{len(SOURCE_FIELDS)} variables de origen · {len(FIELDS)} activas**")
    uploaded=st.file_uploader("CSV de FileMaker (sin cabecera)",type=["csv"])
    if uploaded:
        content=uploaded.getvalue()
        try:
            profile=profile_import(parse_fp5_csv_bytes(content)); a,b,c,d=st.columns(4); a.metric("Filas",profile["rows"]); b.metric("Columnas",profile["cols"]); c.metric("NHC distintos",profile["nonempty_nhc"]); d.metric("Duplicados exactos",profile["exact_duplicate_rows"]); st.info(f"`555`: {profile['missing_555_cells']} · vacíos: {profile['blank_cells']} · ceros: {profile['zero_cells']}"); st.write("Campos completamente vacíos:",", ".join(profile["empty_fields"]) or "ninguno")
            if st.button("🔎 Auditar sin importar"): st.json(profile)
            st.markdown("### Importación"); import_mode=st.radio("Modo de carga",["Piloto · primeros 100 episodios","Carga completa · 14.297 episodios"],horizontal=True); is_pilot=import_mode.startswith("Piloto"); st.info("Solo se cargarán los primeros 100 registros del CSV." if is_pilot else "La carga completa escribirá los 14.297 episodios."); button_label="⬆️ Importar piloto (100)" if is_pilot else "⬆️ Importar los 14.297 episodios"
            if st.button(button_label,type="primary"):
                with st.spinner("Importando a Supabase..."):
                    result=import_fp5(content,uploaded.name,dry_run=False,limit=100 if is_pilot else None)
                st.success(f"Importados: {result['episodes_created']} episodios y {result['patients_created']} pacientes.")
        except Exception as e: st.error(f"CSV no válido: {e}")
    st.markdown("### Reglas de normalización"); st.write("`555` = missing revisado · vacío = missing no revisado · el `0` se interpreta por variable.")
