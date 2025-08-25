# app.py — BITS Hyderabad Pooler (Streamlit)
# Stable build (presets UI, no Maps required) with:
# - Google Sign-In (restricted to @hyderabad.bits-pilani.ac.in)
# - Supabase backend if configured, else SQLite fallback
# - Create / join / leave / delete pools
# - Members list visible to members/host
# - Simple in-app chat for members (Supabase/SQLite)
# - Live updates (auto-refresh) option
# - Pickup field; creator & members can leave; host delete hides card immediately
# - Share links via URL query (?pool=...)
# - Spam guard: 1 pool per user per 15 minutes
# - Seats cap 1..10

from __future__ import annotations

import re
import math
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any

import streamlit as st

# Optional imports (handled gracefully)
try:
    from supabase import create_client, Client  # type: ignore
except Exception:  # pragma: no cover
    Client = None  # type: ignore
    create_client = None  # type: ignore

try:
    from streamlit_autorefresh import st_autorefresh  # type: ignore
except Exception:  # pragma: no cover
    st_autorefresh = None  # type: ignore

try:
    import requests
except Exception:  # pragma: no cover
    requests = None  # type: ignore

# Google OAuth imports
try:
    from google_auth_oauthlib.flow import Flow
    from google.oauth2 import id_token as google_id_token
    from google.auth.transport import requests as google_requests
except Exception:  # pragma: no cover
    Flow = None  # type: ignore
    google_id_token = None  # type: ignore
    google_requests = None  # type: ignore

# ---------------------------------
# Config & constants
# ---------------------------------
st.set_page_config(page_title="BITS-H Pooler", page_icon="🚕", layout="wide")

# --- UI polish (pure CSS; no functional changes) ---
def inject_css():
    st.markdown(
        """
        <style>
        :root {
          --bg: #0f1116;
          --panel: #151823;
          --muted: #9aa4b2;
          --text: #e6e6e6;
          --accent: #6366f1; /* indigo-500 */
          --accent-2: #22c55e; /* green-500 */
          --danger: #ef4444; /* red-500 */
          --radius: 14px;
        }
        /* App background */
        .stApp {
          background: radial-gradient(1200px 600px at 0% -10%, #1a1f2e 0%, var(--bg) 35%), var(--bg);
          color: var(--text);
        }
        /* Sidebar */
        div[data-testid="stSidebar"] {
          background: linear-gradient(180deg,#0d1020,#0c111a 60%);
          border-right: 1px solid rgba(255,255,255,.06);
        }
        /* Containers & expanders */
        .stExpander, .stTabs, .stAlert, .stTextInput, .stNumberInput, .stSelectbox, .stDateInput, .stTimeInput {
          border-radius: var(--radius) !important;
        }
        .stExpander {
          background: var(--panel);
          border: 1px solid rgba(255,255,255,.06);
        }
        /* Inputs */
        .stTextInput > div > div > input,
        .stNumberInput input,
        .stDateInput input,
        .stTimeInput input {
          background: #0f1320 !important;
          color: var(--text) !important;
          border: 1px solid rgba(255,255,255,.08) !important;
          border-radius: 10px !important;
        }
        /* Selects */
        div[data-baseweb="select"] > div { 
          background: #0f1320 !important; 
          color: var(--text) !important; 
          border: 1px solid rgba(255,255,255,.08) !important; 
          border-radius: 10px !important;
        }
        /* Buttons */
        .stButton > button, .stLinkButton > a {
          background: var(--accent) !important;
          color: #fff !important;
          border: 0 !important;
          border-radius: 12px !important;
          padding: .6rem 1rem !important;
          box-shadow: 0 6px 20px rgba(99,102,241,.25) !important;
        }
        .stButton > button:hover, .stLinkButton > a:hover { filter: brightness(1.06); }
        /* Danger button */
        .stButton:has(button[kind="secondary"]) > button {
          background: var(--danger) !important;
          box-shadow: 0 6px 20px rgba(239,68,68,.25) !important;
        }
        /* Metrics as mini cards */
        .stMetric {
          background: var(--panel);
          border: 1px solid rgba(255,255,255,.06);
          border-radius: var(--radius);
          padding: 10px 14px;
        }
        .stMetric label { color: var(--muted); }
        /* Subheaders spacing */
        .stMarkdown h2 { margin-top: 0.6rem; }
        /* Share link input slightly dim */
        .stTextInput input[readonly] { opacity: .9; }
        </style>
        """,
        unsafe_allow_html=True,
    )

DB_PATH = Path("pools.db")
COOLDOWN_MINUTES = 15
SEATS_MIN, SEATS_MAX = 1, 10

# OAuth scopes
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

# Fixed preset list for destination/pickup dropdowns (no Maps API required)
DEST_PICKUP_CHOICES = [
    "JBS",
    "Viceroy family dhaba",
    "Taaza",
    "Punjabi haveli",
    "Udupi",
    "Sereno",
    "Tandoor",
    "Shameerpet Bus Stand",
    "Evergreen",
    "Village",
    "RedBucket Biryani",
    "Maruti Wine shop",
    "BPHC Main Gate",
    "Mess 1",
    "Mess 2",
    "Kondapur",
    "Miyapur",
    "Gachibowli",
    "jubilee hills",
    "nallagandla",
    "Tellapur",
    "Nanakramguda",
    "Narsingi",
    "Madhapur",
    "Banjara Hills",
    "Panjagutta",
    "Begumpet",
    "Alwal",
    "Sainikpuri",
    "JNTU",
    "Lakdi ka Pul",
]

# ---------------------------------
# Utilities
# ---------------------------------

def is_bits_email(email: str) -> bool:
    return bool(re.search(r"@hyderabad\.bits-pilani\.ac\.in$", email.strip(), re.IGNORECASE))


def now_iso() -> str:
    return datetime.now().isoformat()


def _slug(s: str) -> str:
    """Slugify a name for destination_id/pickup ids without external deps."""
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def haversine_km(a: Dict[str, float], b: Dict[str, float]) -> float:
    if not a or not b:
        return float("inf")
    R = 6371.0
    lat1, lon1 = math.radians(a["lat"]), math.radians(a["lng"]) if a else (0.0, 0.0)
    lat2, lon2 = math.radians(b["lat"]), math.radians(b["lng"]) if b else (0.0, 0.0)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    x = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(x), math.sqrt(1 - x))
    return R * c

# ---------------------------------
# Data layer (Supabase or SQLite)
# ---------------------------------
SB: Optional[Client] = None
USE_SUPABASE = False


def get_supabase_cfg() -> Optional[Dict[str, str]]:
    try:
        cfg = st.secrets["supabase"]
        url = cfg.get("url")
        key = cfg.get("anon_key")
        if url and key:
            return {"url": url, "anon_key": key}
    except Exception:
        pass
    return None


def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def init_db():
    global SB, USE_SUPABASE
    sb_cfg = get_supabase_cfg()
    if sb_cfg and create_client is not None:
        try:
            SB = create_client(sb_cfg["url"], sb_cfg["anon_key"])  # type: ignore
            USE_SUPABASE = True
            return
        except Exception as e:
            st.sidebar.warning(f"Supabase disabled: {e}. Falling back to SQLite.")
            USE_SUPABASE = False

    # SQLite fallback schema
    with get_conn() as con:
        cur = con.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS pools (
                id TEXT PRIMARY KEY,
                destination_id TEXT,
                destination_name TEXT,
                lat REAL,
                lng REAL,
                when_iso TEXT,
                seats INTEGER,
                mode TEXT,
                notes TEXT,
                host_name TEXT,
                host_email TEXT,
                created_at TEXT,
                pickup TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS members (
                pool_id TEXT,
                name TEXT,
                email TEXT,
                UNIQUE(pool_id, email)
            )
            """
        )
        # Chat messages (SQLite)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                pool_id TEXT,
                sender_email TEXT,
                sender_name TEXT,
                content TEXT,
                created_at TEXT
            )
            """
        )
        con.commit()

# Spam guard --------------------------------------------------------------

def can_host_create(email: str) -> bool:
    """Allow only one new pool per user within the last COOLDOWN_MINUTES to prevent spam."""
    since = datetime.now() - timedelta(minutes=COOLDOWN_MINUTES)
    if USE_SUPABASE and SB is not None:
        try:
            res = (
                SB.table("pools")
                .select("id,created_at")
                .eq("host_email", email)
                .gte("created_at", since.isoformat())
                .limit(1)
                .execute()
            )
            return not (res.data and len(res.data) > 0)
        except Exception:
            return True
    with get_conn() as con:
        cur = con.cursor()
        try:
            cur.execute("SELECT created_at FROM pools WHERE host_email = ?", (email,))
            rows = cur.fetchall()
            for (created_at,) in rows:
                try:
                    if datetime.fromisoformat(created_at) >= since:
                        return False
                except Exception:
                    continue
        except Exception:
            return True
    return True

# Core DB functions -------------------------------------------------------

def add_pool(pool: Dict[str, Any]):
    if USE_SUPABASE and SB is not None:
        # Let DB generate UUID and return it
        payload = pool.copy()
        payload.pop("id", None)
        ins = SB.table("pools").insert(payload).execute()
        new_id = None
        try:
            if isinstance(ins.data, list) and ins.data:
                new_id = ins.data[0].get("id")
            elif isinstance(ins.data, dict):
                new_id = ins.data.get("id")
        except Exception:
            pass
        if new_id:
            SB.table("members").insert({
                "pool_id": new_id,
                "name": pool["host_name"],
                "email": pool["host_email"],
            }).execute()
        return

    # SQLite fallback
    with get_conn() as con:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO pools (id, destination_id, destination_name, lat, lng, when_iso, seats, mode, notes, host_name, host_email, created_at, pickup) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                pool["id"],
                pool["destination_id"],
                pool["destination_name"],
                pool["lat"],
                pool["lng"],
                pool["when_iso"],
                pool["seats"],
                pool["mode"],
                pool["notes"],
                pool["host_name"],
                pool["host_email"],
                pool["created_at"],
                pool.get("pickup", ""),
            ),
        )
        cur.execute("INSERT OR IGNORE INTO members VALUES (?, ?, ?)", (pool["id"], pool["host_name"], pool["host_email"]))
        con.commit()


def list_future_pools() -> List[Dict[str, Any]]:
    if USE_SUPABASE and SB is not None:
        res = SB.table("pools").select("*").gte("when_iso", now_iso()).execute()
        pools = res.data or []
        ids = [p.get("id") for p in pools]
        if ids:
            mres = SB.table("members").select("pool_id,name,email").in_("pool_id", ids).execute()
            by_pool: Dict[str, List[Dict[str, str]]] = {}
            for m in (mres.data or []):
                by_pool.setdefault(m["pool_id"], []).append({"name": m["name"], "email": m["email"]})
            for p in pools:
                p["members"] = by_pool.get(p.get("id"), [])
        else:
            for p in pools:
                p["members"] = []
        return pools

    # SQLite fallback
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("SELECT * FROM pools")
        rows = cur.fetchall()
        pools: List[Dict[str, Any]] = []
        for r in rows:
            p = {
                "id": r[0],
                "destination_id": r[1],
                "destination_name": r[2],
                "lat": r[3],
                "lng": r[4],
                "when_iso": r[5],
                "seats": r[6],
                "mode": r[7],
                "notes": r[8],
                "host_name": r[9],
                "host_email": r[10],
                "created_at": r[11],
                "pickup": r[12] if len(r) > 12 else "",
            }
            try:
                if datetime.fromisoformat(p["when_iso"]) >= datetime.now():
                    pools.append(p)
            except Exception:
                continue
        for p in pools:
            cur.execute("SELECT name, email FROM members WHERE pool_id = ?", (p["id"],))
            p["members"] = [{"name": n, "email": e} for (n, e) in cur.fetchall()]
        return pools


def get_member_list(pool_id: str) -> List[Dict[str, str]]:
    if USE_SUPABASE and SB is not None:
        try:
            res = SB.table("members").select("name,email").eq("pool_id", pool_id).order("joined_at", desc=False).execute()
            return res.data or []
        except Exception:
            return []
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("SELECT name, email FROM members WHERE pool_id = ?", (pool_id,))
        return [{"name": n, "email": e} for (n, e) in cur.fetchall()]


def get_members_count(pool_id: str) -> int:
    if USE_SUPABASE and SB is not None:
        res = SB.table("members").select("pool_id").eq("pool_id", pool_id).execute()
        return len(res.data or [])
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM members WHERE pool_id = ?", (pool_id,))
        return cur.fetchone()[0]


def is_user_member(pool_id: str, email: str) -> bool:
    if USE_SUPABASE and SB is not None:
        try:
            res = SB.table("members").select("pool_id").eq("pool_id", pool_id).eq("email", email).execute()
            return bool(res.data)
        except Exception:
            return False
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("SELECT 1 FROM members WHERE pool_id = ? AND email = ?", (pool_id, email))
        return cur.fetchone() is not None


def join_pool(pool_id: str, name: str, email: str):
    if USE_SUPABASE and SB is not None:
        # Try atomic RPC if present
        try:
            rpc = SB.rpc("join_pool_atomic", {"p_pool_id": pool_id, "p_name": name, "p_email": email}).execute()
            if isinstance(rpc.data, bool):
                return (True, "Joined") if rpc.data else (False, "Pool is full or ride passed")
        except Exception:
            pass
        # Fallback client-side checks
        chk = SB.table("members").select("pool_id,email").eq("pool_id", pool_id).eq("email", email).execute()
        if (chk.data or []):
            return True, "Already joined"
        pres = SB.table("pools").select("seats,when_iso").eq("id", pool_id).execute()
        if not pres.data:
            return False, "Pool not found"
        seats = pres.data[0]["seats"]
        when_iso = pres.data[0]["when_iso"]
        try:
            if datetime.fromisoformat(when_iso) < datetime.now():
                return False, "Ride time has passed"
        except Exception:
            pass
        cnt = SB.table("members").select("pool_id").eq("pool_id", pool_id).execute()
        if len(cnt.data or []) >= seats:
            return False, "Pool is full"
        SB.table("members").insert({"pool_id": pool_id, "name": name, "email": email}).execute()
        return True, "Joined"

    # SQLite fallback
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("SELECT 1 FROM members WHERE pool_id = ? AND email = ?", (pool_id, email))
        if cur.fetchone():
            return True, "Already joined"
        cur.execute("SELECT seats, when_iso FROM pools WHERE id = ?", (pool_id,))
        row = cur.fetchone()
        if not row:
            return False, "Pool not found"
        seats, when_iso = row
        try:
            if datetime.fromisoformat(when_iso) < datetime.now():
                return False, "Ride time has passed"
        except Exception:
            pass
        cur.execute("SELECT COUNT(*) FROM members WHERE pool_id = ?", (pool_id,))
        count = cur.fetchone()[0]
        if count >= seats:
            return False, "Pool is full"
        cur.execute("INSERT INTO members VALUES (?, ?, ?)", (pool_id, name, email))
        con.commit()
        return True, "Joined"


def leave_pool(pool_id: str, email: str):
    """Remove the user from a pool's members list.
    With Supabase+RLS, prefer a SECURITY DEFINER RPC (leave_pool_if_member).
    Falls back to direct delete for SQLite or if RPC is missing.
    """
    if USE_SUPABASE and SB is not None:
        # Try RPC first (works even when RLS blocks direct deletes)
        try:
            SB.rpc("leave_pool_if_member", {"p_pool_id": pool_id, "p_email": email}).execute()
            return
        except Exception:
            pass
        # Fallback: direct delete (requires an explicit delete policy if you use RLS)
        try:
            SB.table("members").delete().eq("pool_id", pool_id).eq("email", email).execute()
            return
        except Exception:
            return

    # SQLite fallback
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("DELETE FROM members WHERE pool_id = ? AND email = ?", (pool_id, email))
        con.commit()


def delete_pool(pool_id: str, requester_email: str) -> bool:
    """Delete pool if requester is the host.
    Supabase: prefer RPC `delete_pool_if_host` (SECURITY DEFINER) to bypass RLS safely.
    SQLite: direct deletes.
    """
    if USE_SUPABASE and SB is not None:
        # 1) Try RPC first (recommended)
        try:
            rpc = SB.rpc("delete_pool_if_host", {"p_pool_id": pool_id, "p_email": requester_email}).execute()
            if isinstance(rpc.data, bool):
                return rpc.data
            if rpc.data:
                return True
        except Exception:
            pass
        # 2) Fallback pre-check + direct delete (may be blocked by RLS)
        try:
            res = SB.table("pools").select("host_email").eq("id", pool_id).execute()
            if not res.data or res.data[0].get("host_email") != requester_email:
                return False
            dres = SB.table("pools").delete().eq("id", pool_id).execute()
            return bool(getattr(dres, "data", None))
        except Exception:
            return False

    # SQLite fallback
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("SELECT host_email FROM pools WHERE id = ?", (pool_id,))
        row = cur.fetchone()
        if not row or row[0] != requester_email:
            return False
        cur.execute("DELETE FROM members WHERE pool_id = ?", (pool_id,))
        cur.execute("DELETE FROM pools WHERE id = ?", (pool_id,))
        con.commit()
        return True


def cleanup_expired_pools():
    """Remove pools with past departure times and their members.
    Works for both Supabase and SQLite backends.
    """
    if USE_SUPABASE and SB is not None:
        now_s = now_iso()
        try:
            old = SB.table("pools").select("id").lt("when_iso", now_s).execute()
            ids = [p.get("id") for p in (old.data or []) if p.get("id")]
            if ids:
                SB.table("members").delete().in_("pool_id", ids).execute()
                SB.table("pools").delete().in_("id", ids).execute()
        except Exception:
            # Ignore cleanup failures; UI will still function
            pass
        return

    now = datetime.now()
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("SELECT id, when_iso FROM pools")
        for pid, when_iso in cur.fetchall():
            try:
                if datetime.fromisoformat(when_iso) < now:
                    cur.execute("DELETE FROM members WHERE pool_id = ?", (pid,))
                    cur.execute("DELETE FROM pools WHERE id = ?", (pid,))
            except Exception:
                continue
        con.commit()

# -----------------------
# Chat (messages) helpers
# -----------------------

def list_messages(pool_id: str, limit: int = 200) -> List[Dict[str, Any]]:
    if USE_SUPABASE and SB is not None:
        try:
            res = (
                SB.table("messages")
                .select("id,pool_id,sender_email,sender_name,content,created_at")
                .eq("pool_id", pool_id)
                .order("created_at", desc=False)
                .limit(limit)
                .execute()
            )
            return res.data or []
        except Exception:
            return []
    with get_conn() as con:
        cur = con.cursor()
        cur.execute(
            "SELECT id,pool_id,sender_email,sender_name,content,created_at FROM messages WHERE pool_id = ? ORDER BY created_at ASC LIMIT ?",
            (pool_id, limit),
        )
        rows = cur.fetchall()
        return [
            {
                "id": r[0],
                "pool_id": r[1],
                "sender_email": r[2],
                "sender_name": r[3],
                "content": r[4],
                "created_at": r[5],
            }
            for r in rows
        ]


def add_message(pool_id: str, sender_name: str, sender_email: str, content: str):
    ts = now_iso()
    if USE_SUPABASE and SB is not None:
        try:
            SB.table("messages").insert(
                {
                    "pool_id": pool_id,
                    "sender_email": sender_email,
                    "sender_name": sender_name,
                    "content": content,
                    "created_at": ts,
                }
            ).execute()
            return
        except Exception:
            return
    with get_conn() as con:
        cur = con.cursor()
        mid = f"msg_{int(datetime.now().timestamp()*1000)}"
        cur.execute(
            "INSERT INTO messages (id,pool_id,sender_email,sender_name,content,created_at) VALUES (?,?,?,?,?,?)",
            (mid, pool_id, sender_email, sender_name, content, ts),
        )
        con.commit()

# ---------------------------------
# Google OAuth helpers
# ---------------------------------

def get_google_oauth_cfg() -> Optional[Dict[str, str]]:
    try:
        cfg = st.secrets.get("google_oauth", {})
        cid = cfg.get("client_id")
        csec = cfg.get("client_secret")
        redir = cfg.get("redirect_uri", "http://localhost:8501")
        domain = cfg.get("allowed_domain", "hyderabad.bits-pilani.ac.in")
        if cid and csec:
            return {
                "client_id": cid,
                "client_secret": csec,
                "redirect_uri": redir,
                "allowed_domain": domain,
            }
    except Exception:
        pass
    return None


def google_sign_in_ui(cfg: Dict[str, str]):
    if Flow is None:
        st.sidebar.error("Google auth libraries not installed.")
        return

    qp = st.query_params
    code = qp.get("code")
    if isinstance(code, list):
        code = code[0] if code else None
    state_from_cb = qp.get("state")
    if isinstance(state_from_cb, list):
        state_from_cb = state_from_cb[0] if state_from_cb else None

    def make_flow(state: Optional[str] = None) -> Flow:  # type: ignore
        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": cfg["client_id"],
                    "client_secret": cfg["client_secret"],
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            },
            scopes=SCOPES,
            state=state,
        )
        flow.redirect_uri = cfg["redirect_uri"]
        return flow

    # Callback: exchange code for tokens
    if code:
        try:
            flow = make_flow(state=state_from_cb)
            flow.fetch_token(code=code)
            creds = flow.credentials
            idinfo = google_id_token.verify_oauth2_token(  # type: ignore
                creds.id_token, google_requests.Request(), cfg["client_id"]  # type: ignore
            )
            email = (idinfo.get("email") or "").lower()
            name = idinfo.get("name") or email.split("@")[0]
            hd = idinfo.get("hd", "")
            allowed = cfg["allowed_domain"]
            if not email.endswith("@" + allowed) and hd != allowed.split("@")[-1]:
                st.sidebar.error(f"This app is restricted to @{allowed}")
                return
            st.session_state.user = {"name": name, "email": email, "sub": idinfo.get("sub")}
            # Clean URL params
            try:
                if "code" in st.query_params:
                    del st.query_params["code"]
                if "state" in st.query_params:
                    del st.query_params["state"]
            except Exception:
                pass
            st.rerun()
        except Exception as e:
            st.sidebar.error(f"Google sign-in failed: {e}")
            return

    # Show Sign-In button
    try:
        flow = make_flow()
        auth_url, state = flow.authorization_url(
            access_type="offline", include_granted_scopes="true", prompt="consent"
        )
        st.session_state["oauth_state"] = state
        st.sidebar.link_button("Continue with Google", auth_url)
    except Exception as e:
        st.sidebar.error(f"Google auth not ready: {e}")
        st.sidebar.caption("Check .streamlit/secrets.toml and OAuth redirect URI.")

# ---------------------------------
# UI helpers
# ---------------------------------

def hero():
    st.markdown(
        """
        <div style='text-align:center; padding: 24px; background: linear-gradient(90deg,#111,#333); color:white; border-radius:16px;'>
            <h1 style='margin:0; font-size: 30px'>WHERE ARE YOU HEADED TO TODAY??</h1>
            <p style='margin-top:6px; opacity:0.9'>Find or create a ride pool with fellow BITS Hyderabad students.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def email_gate():
    """Sidebar should only contain Google Login. No manual login, no extra UI."""
    google_cfg = get_google_oauth_cfg()
    if google_cfg:
        google_sign_in_ui(google_cfg)
    else:
        st.sidebar.error("Google Sign-In is not configured. Add [google_oauth] in secrets.")


# Create Pool UI (presets only) -----------------------------------------

def create_pool_ui(user: Dict[str, str]):
    with st.expander("➕ Create your own pool", expanded=False):
        dest_name = st.selectbox("Destination", DEST_PICKUP_CHOICES, index=0)
        dest = {"id": _slug(dest_name), "name": dest_name, "lat": 0.0, "lng": 0.0}

        col1, col2 = st.columns([2, 1])
        with col1:
            default_dt = datetime.now() + timedelta(hours=2)
            date_val = st.date_input("Date", value=default_dt.date())
            time_val = st.time_input("Time", value=default_dt.time().replace(second=0, microsecond=0))
            when_dt = datetime.combine(date_val, time_val)
            mode = st.selectbox("Mode", ["Cab", "Auto"])
        with col2:
            pickup_choice = st.selectbox(
                "Pickup point",
                DEST_PICKUP_CHOICES + ["Other (type manually)"],
                index=0,
            )
            if pickup_choice == "Other (type manually)":
                pickup = st.text_input("Pickup (custom)", placeholder="Enter pickup point")
            else:
                pickup = pickup_choice

            seats = st.number_input("Seats", min_value=SEATS_MIN, max_value=SEATS_MAX, value=3)
            notes = st.text_input("Notes (optional)", placeholder="Eg. leaving from Hostel B gate")

        if st.button("Create Pool", use_container_width=True):
            if when_dt < datetime.now():
                st.error("Please pick a future time.")
            elif not pickup.strip():
                st.error("Please select or enter a pickup point.")
            elif not can_host_create(user["email"]):
                st.error(f"You can only create one pool every {COOLDOWN_MINUTES} minutes to prevent spam.")
            else:
                pool = {
                    "id": f"pool_{int(datetime.now().timestamp()*1000)}",
                    "destination_id": dest["id"],
                    "destination_name": dest["name"],
                    "lat": dest["lat"],
                    "lng": dest["lng"],
                    "when_iso": when_dt.isoformat(),
                    "seats": int(seats),
                    "mode": mode,
                    "notes": notes.strip(),
                    "pickup": pickup.strip(),
                    "host_name": user["name"],
                    "host_email": user["email"],
                    "created_at": now_iso(),
                }
                add_pool(pool)
                st.success("Pool created!")
                st.rerun()

# List UI -----------------------------------------------------------------

def pools_list_ui(user: Dict[str, str]):
    cleanup_expired_pools()
    hero()

    # Live updates via auto-refresh (fallback-friendly)
    live = st.checkbox("🔄 Live updates (every 5s)", value=False)
    if live and st_autorefresh is not None:
        st_autorefresh(interval=5000, key="live_refresh")

    # Shared link focus (?pool=...)
    qp = st.query_params
    focus_id: Optional[str] = None
    vals = qp.get("pool")
    if isinstance(vals, list):
        if vals:
            focus_id = vals[0]
    elif isinstance(vals, str):
        focus_id = vals

    pools = list_future_pools()

    # If a pool was just deleted, hide it immediately before rendering
    _prune = st.session_state.pop("_deleted_pool_id", None)
    if _prune:
        pools = [p for p in pools if p.get("id") != _prune]

    # Destination & pickup filters (dropdowns)
    st.subheader("Find pools by destination")
    dest_choice = st.selectbox(
        "Filter by destination",
        ["All destinations"] + DEST_PICKUP_CHOICES,
        index=0,
        key="dest_filter",
    )
    if dest_choice != "All destinations":
        pools = [
            p for p in pools
            if (p.get("destination_name") or "").strip().lower() == dest_choice.strip().lower()
        ]

    pickup_choice = st.selectbox(
        "Optional: filter by pickup point",
        ["Any pickup"] + DEST_PICKUP_CHOICES,
        index=0,
        key="pickup_filter",
    )
    if pickup_choice != "Any pickup":
        pools = [
            p for p in pools
            if (p.get("pickup") or "").strip().lower() == pickup_choice.strip().lower()
        ]

    # Bring shared pool to top
    focus_pool = None
    if focus_id:
        focus_pool = next((p for p in pools if p.get("id") == focus_id), None)
        if focus_pool:
            pools = [focus_pool] + [p for p in pools if p.get("id") != focus_pool.get("id")]

    # Sort by time (chronological)
    pools.sort(key=lambda x: x["when_iso"])

    st.subheader("Available Pools")
    if not pools:
        st.info("No pools yet. Be the first to create one!")
        return

    for p in pools:
        pid = p.get("id")
        cols = st.columns([5, 2, 2, 3])
        with cols[0]:
            title_lines = [f"**{p['destination_name']}**"]
            if focus_id and pid == focus_id:
                title_lines.append(":link: _Linked from share_")
            st.markdown("  /n".join(title_lines))

            dt_str = datetime.fromisoformat(p["when_iso"]).strftime("%d %b %Y, %I:%M %p")
            st.caption(f"{dt_str} • {p['mode']}")
            st.caption(f"Host: {p['host_name']} ({p['host_email']})")
            if p.get("pickup"):
                st.caption(f"Pickup: {p['pickup']}")
            if p.get("notes"):
                st.write(p["notes"])
        with cols[1]:
            member_count = get_members_count(pid)
            st.metric("Members", f"{member_count}/{p['seats']}")
        with cols[2]:
            st.empty()  # reserved column (distance removed in presets build)
        with cols[3]:
            cur_count = get_members_count(pid)
            already = is_user_member(pid, user["email"]) or (p.get("host_email") == user["email"])  # host can always see
            if is_user_member(pid, user["email"]):
                if st.button("Leave", key=f"leave_{pid}"):
                    leave_pool(pid, user["email"])
                    st.success("You left the pool.")
                    st.rerun()
            elif cur_count < p["seats"]:
                if st.button("Join", key=f"join_{pid}"):
                    ok, msg = join_pool(pid, user["name"], user["email"])
                    if ok:
                        st.success("Joined!")
                    else:
                        st.warning(msg)
                    st.rerun()
            else:
                st.button("Full", disabled=True, key=f"full_{pid}")

            if st.button("Share link", key=f"share_{pid}"):
                try:
                    st.query_params["pool"] = pid
                except Exception:
                    pass
                st.info("Link set in your address bar; copy & share.")
                st.text_input("Share this", value=f"?pool={pid}", key=f"link_{pid}")

            if p.get("host_email") == user["email"]:
                if st.button("Delete", key=f"del_{pid}"):
                    ok = delete_pool(pid, user["email"])
                    if ok:
                        st.success("Deleted")
                        st.session_state["_deleted_pool_id"] = pid
                        st.rerun()
                    else:
                        st.warning("You can't delete this pool.")

        # --- Members & Chat (visible to members/host only) ---
        if already:
            with st.expander(f"👥 Members ({get_members_count(pid)}/{p['seats']})", expanded=False):
                mlist = get_member_list(pid)
                if not mlist:
                    st.caption("No members yet.")
                else:
                    for m in mlist:
                        st.write(f"• {m.get('name','')} ({m.get('email','')})")

            with st.expander("💬 Chat (beta)", expanded=False):
                # Faster refresh for chat if live mode on
                if live and st_autorefresh is not None:
                    st_autorefresh(interval=3000, key=f"chat_refresh_{pid}")

                msgs = list_messages(pid)
                if msgs:
                    for msg in msgs[-200:]:
                        who = msg.get("sender_name") or msg.get("sender_email")
                        when = msg.get("created_at", "")
                        st.markdown(f"**{who}**  _{when}_  \n{msg.get('content', '')}")
                else:
                    st.caption("No messages yet. Start the conversation!")

                with st.form(f"chat_send_{pid}", clear_on_submit=True):
                    text = st.text_input("Message", placeholder="Type and press Send…")
                    sent = st.form_submit_button("Send")
                if sent and text.strip():
                    add_message(pid, user["name"], user["email"], text.strip())
                    st.rerun()

# ---------------------------------
# App entry
# ---------------------------------

def main():
    # Inject CSS-only styling (no functional changes)
    inject_css()
    init_db()
    if "user" not in st.session_state:
        st.session_state.user = None

    user = st.session_state.user
    if not user:
        email_gate()
        st.stop()

    create_pool_ui(user)
    pools_list_ui(user)


if __name__ == "__main__":
    main()
