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


def list_patients(page=1, page_size=25, search=""):
    start = (page - 1) * page_size
    end = start + page_size - 1
    q = supabase.table("fp5_patients").select("patient_id,nhc,display_name,updated_at")
    search = clean(search)
    if search:
        q = q.or_(f"nhc.ilike.%{search}%,display_name.ilike.%{search}%")
    return q.order("updated_at", desc=True).range(start, end).execute().data or []


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


def import_fp5(content, filename, dry_run=False):
    df = parse_fp5_csv_bytes(content)
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
# Session
# -----------------------------
for k, default in {
    "page": "dashboard",
    "patient_id": "",
    "episode_id": "",
    "search": "",
    "patient_page": 1,
    "clinical_text": "",
    "user_label": "web",
}.items():
    st.session_state.setdefault(k, default)

# Seed definitions lazily.
try:
    seed_field_definitions()
except Exception as e:
    st.warning(f"No se pudo sincronizar el diccionario de campos todavía: {e}")

st.markdown("""
<div style="padding:18px 22px;margin-bottom:14px;border-radius:18px;background:linear-gradient(135deg,#f7fbff,#eef7f4,#ffffff);border:1px solid #dbe6ee;">
<div style="font-size:.75rem;font-weight:700;letter-spacing:.08em;color:#2b6f7e;">FP5 · CARDIOLOGÍA</div>
<div style="font-size:2rem;font-weight:800;color:#102a43;">Base maestra hospitalaria</div>
<div style="color:#5b7083;">Original FP5 → datos normalizados → revisión humana → propuestas IA</div>
</div>
""", unsafe_allow_html=True)

nav = st.columns(4)
if nav[0].button("📊 Dashboard", use_container_width=True): st.session_state.page = "dashboard"
if nav[1].button("👥 Pacientes", use_container_width=True): st.session_state.page = "patients"
if nav[2].button("🧠 IA clínica", use_container_width=True): st.session_state.page = "ai"
if nav[3].button("⚙️ Administración", use_container_width=True): st.session_state.page = "admin"

if st.session_state.page == "dashboard":
    try:
        pcount = supabase.table("fp5_patients").select("patient_id", count="exact").limit(1).execute().count or 0
        ecount = supabase.table("fp5_episodes").select("episode_id", count="exact").limit(1).execute().count or 0
    except Exception:
        pcount = ecount = 0
    a,b,c,d = st.columns(4)
    a.metric("Pacientes", pcount)
    b.metric("Episodios", ecount)
    c.metric("Variables activas", len(FIELDS))
    d.metric("IA", GEMINI_MODEL or "No configurada")
    st.info(f"Export original: {len(SOURCE_FIELDS)} columnas · modelo activo: {len(FIELDS)} · retiradas: {len(RETIRED_FIELDS)} · `555` = missing revisado · vacío = missing no revisado · el 0 se interpreta por variable.")
    st.success("El original FP5 queda protegido en `raw_data`; las modificaciones van a `validated_data` y a auditoría.")

elif st.session_state.page == "patients":
    st.subheader("Pacientes")
    st.caption("Cada paciente puede tener uno o varios ingresos/episodios.")
    c1,c2 = st.columns([3,1])
    with c1:
        st.session_state.search = st.text_input("Buscar por NHC o nombre", st.session_state.search)
    with c2:
        page_size = st.selectbox("Registros/página", [25,50,100], index=0)
    rows = list_patients(st.session_state.patient_page, page_size, st.session_state.search)
    if rows:
        st.dataframe(pd.DataFrame(rows).rename(columns={"patient_id":"ID","nhc":"NHC","display_name":"Nombre","updated_at":"Actualizado"}), use_container_width=True, hide_index=True)
        selected = st.selectbox("Abrir paciente", [r["patient_id"] for r in rows])
        if st.button("Abrir paciente", type="primary"):
            st.session_state.patient_id = selected
            eps = patient_episodes(selected)
            st.session_state.episode_id = eps[0]["episode_id"] if eps else ""
            st.session_state.page = "ai"
            st.rerun()
    else:
        st.info("No hay pacientes para esa búsqueda.")

elif st.session_state.page == "ai":
    pid = st.session_state.patient_id
    if not pid:
        st.info("Selecciona primero un paciente.")
        st.stop()
    patient = get_patient(pid)
    if not patient:
        st.error("Paciente no encontrado.")
        st.stop()
    episodes = patient_episodes(pid)
    st.subheader(f"{patient.get('display_name') or 'Paciente'} · NHC {patient.get('nhc') or '—'}")
    st.caption("Cada fila de FileMaker se conserva como un episodio/ingreso independiente.")
    if not episodes:
        st.info("Este paciente no tiene episodios cargados.")
        st.stop()
    labels = [f"Fila {e['source_row']} · {((e.get('raw_data') or {}).get('FECHA_INGR') or 'sin fecha')} · {((e.get('raw_data') or {}).get('DIAG_INGRE') or '')[:70]}" for e in episodes]
    selected_label = st.selectbox("Episodio", labels, index=max(0, next((i for i,e in enumerate(episodes) if e['episode_id']==st.session_state.episode_id),0)))
    ep = episodes[labels.index(selected_label)]
    st.session_state.episode_id = ep["episode_id"]
    raw, validated, statuses = ep.get("raw_data") or {}, ep.get("validated_data") or {}, ep.get("field_status") or {}

    tabs = st.tabs(["📋 Original FP5", "✏️ Validado", "🧠 Extraer con IA", "🕘 Auditoría"])
    with tabs[0]:
        items=[]
        for f in FIELDS:
            name=f["field"]; val=raw.get(name,"")
            if val not in ("",None): items.append({"Campo":name,"Descripción":f["label"],"Valor original":val,"Estado":statuses.get(name,"")})
        st.dataframe(pd.DataFrame(items), use_container_width=True, hide_index=True)

    with tabs[1]:
        st.caption("Los cambios se guardan en validated_data y nunca alteran raw_data.")
        visible=[f for f in FIELDS if f["category"] not in {"derived_legacy", "hidden_legacy"} and (raw.get(f["field"],"") not in ("",None) or f["field"] in validated)]
        for group_label, group_key in CLINICAL_GROUPS:
            group=[f for f in visible if f["category"]==group_key]
            if not group: continue
            with st.expander(group_label, expanded=(group_key in {"risk_history","diagnosis"})):
                for f in group:
                    name=f["field"]; current=validated.get(name); original=raw.get(name,"")
                    status=statuses.get(name,"")
                    label=f["label"] if f["label"]!=name else name
                    if f["data_kind"]=="text" and (len(str(current or original))>120 or name in {"DIAG_ALTA","DIAG_INGRE","TRATAMIENT","EVOLUCION","ECOCARDIO","OBS_EVOL","OBSERVACIO"}):
                        new_val=st.text_area(label, value="" if current is None else str(current), key=f"v_{ep['episode_id']}_{name}", height=90)
                    else:
                        new_val=st.text_input(label, value="" if current is None else str(current), key=f"v_{ep['episode_id']}_{name}")
                    st.caption(f"Original: {original!r} · estado: {status}")
                    if st.button(f"Guardar {name}", key=f"save_{ep['episode_id']}_{name}"):
                        norm,status2=normalize_field(name,new_val)
                        newdata=dict(validated); newstatus=dict(statuses); old=newdata.get(name)
                        newdata[name]=norm; newstatus[name]=status2
                        save_episode_validated(ep,newdata,newstatus,name,old,norm,"manual")
                        st.success(f"Guardado: {name}")
                        st.rerun()

    with tabs[2]:
        text=st.text_area("Pega aquí el informe/alta/evolución", value=st.session_state.clinical_text, height=320)
        st.session_state.clinical_text=text
        st.caption("La IA solo propone cambios sobre variables del FP5; cada propuesta incluye evidencia textual y requiere aceptación humana.")
        if st.button("🧠 Extraer variables", type="primary"):
            if not text.strip():
                st.warning("Pega primero el texto clínico.")
            else:
                model_display = GEMINI_MODEL or "modelo configurado"
                with st.spinner(f"Analizando con {model_display}..."):
                    try:
                        props=extract_ai(text)
                        save_ai_proposals(ep["episode_id"], props)
                        st.success(f"Propuestas generadas: {len(props)}")
                        st.rerun()
                    except Exception as e:
                        st.error(f"No se pudo extraer el texto: {e}")
        pending=pending_ai(ep["episode_id"])
        if pending:
            st.markdown("### Propuestas pendientes")
            for row in pending:
                st.markdown(f"**{FIELD_LABELS.get(row['field'],row['field'])}** → `{row['proposed_value']}`")
                st.caption(f"Confianza técnica: {row.get('confidence') if row.get('confidence') is not None else '—'} · modelo: {row.get('model','—')}")
                with st.expander("Ver evidencia"):
                    st.write(row.get("evidence") or "Sin evidencia validable.")
                x,y=st.columns(2)
                if x.button("✓ Aceptar", key=f"acc_{row['id']}"):
                    try:
                        accept_ai(ep,row); st.rerun()
                    except Exception as e: st.error(str(e))
                if y.button("✕ Rechazar", key=f"rej_{row['id']}"):
                    reject_ai(row); st.rerun()
        else:
            st.info("No hay propuestas IA pendientes.")

    with tabs[3]:
        audit=audit_for_episode(ep["episode_id"])
        st.dataframe(pd.DataFrame(audit), use_container_width=True, hide_index=True) if audit else st.info("Sin movimientos registrados.")

elif st.session_state.page == "admin":
    st.subheader("Administración")
    st.write("Diccionario: **208 variables FP5**")
    uploaded=st.file_uploader("CSV de FileMaker (sin cabecera)", type=["csv"])
    if uploaded:
        content=uploaded.getvalue()
        try:
            profile=profile_import(parse_fp5_csv_bytes(content))
            a,b,c,d=st.columns(4)
            a.metric("Filas",profile["rows"]); b.metric("Columnas",profile["cols"]); c.metric("NHC distintos",profile["nonempty_nhc"]); d.metric("Duplicados exactos",profile["exact_duplicate_rows"])
            st.info(f"`555`: {profile['missing_555_cells']} · vacíos: {profile['blank_cells']} · ceros: {profile['zero_cells']}")
            st.write("Campos completamente vacíos:", ", ".join(profile["empty_fields"]) or "ninguno")
            st.warning("Primero usa la auditoría. No importes hasta verificar el perfil.")
            if st.button("🔎 Auditar sin importar"):
                st.json(profile)
            if st.button("⬆️ Importar los 14.297 episodios", type="primary"):
                with st.spinner("Importando a Supabase..."):
                    result=import_fp5(content, uploaded.name, dry_run=False)
                st.success(f"Importados: {result['episodes_created']} episodios y {result['patients_created']} pacientes." )
        except Exception as e:
            st.error(f"CSV no válido: {e}")

    st.markdown("### Reglas de normalización")
    st.write("- `555` → missing revisado")
    st.write("- vacío → missing no revisado")
    st.write("- `0` → se aplica la regla específica de cada variable")
    st.write("- `raw_data` conserva siempre el valor original")
    st.write("- `NUM_PAC` es un campo técnico/legado y no se usa como clave ni como variable clínica")
    st.write("- `NHC` agrupa episodios cuando está informado")
