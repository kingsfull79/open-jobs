#!/usr/bin/env python3
"""
app.py - a Streamlit GUI for the hull -> learn -> rank pipeline.

This is a thin orchestration layer: it shells out to the same scripts you'd run by hand
(hull.py, langsort.py, btrank.py), streams their live progress into the browser, and reads
their JSON outputs back into sortable tables. No logic is duplicated -- the scripts stay
authoritative, so anything you tweak there shows up here.

    pip install streamlit pandas pyarrow numpy
    streamlit run app.py

You still need a local copy of the dataset (python3 download.py) and, for steps 2-3, an
OPENAI_API_KEY (paste it in the sidebar).
"""
import io, json, os, re, subprocess, sys, zipfile
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent
PY = sys.executable or "python3"

st.set_page_config(page_title="open-jobs · hull → learn → rank", layout="wide")


# ── helpers ───────────────────────────────────────────────────────────────────────────────

def run_stream(cmd, env=None, cwd=ROOT):
    """Run a command, stream its combined stdout/stderr live into the page (handling the
    scripts' \\r progress lines), and return (returncode, full_log_text)."""
    env = {**os.environ, **(env or {})}
    status = st.empty()                       # one-line live status (the \r progress readout)
    log_box = st.expander("Show full log", expanded=False)
    log_area = log_box.empty()
    lines, buf = [], ""
    proc = subprocess.Popen(cmd, cwd=str(cwd), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    fd = proc.stdout.fileno()
    while True:
        chunk = os.read(fd, 4096)             # blocks until >=1 byte or EOF; good enough for streaming
        if not chunk:
            break
        buf += chunk.decode("utf-8", "replace")
        parts = re.split(r"[\r\n]", buf)
        buf = parts[-1]                        # keep the trailing partial line
        for p in parts[:-1]:
            if p.strip():
                lines.append(p)
        tail = (buf.strip() or (lines[-1] if lines else ""))[:300]
        if tail:
            status.text(tail)
        log_area.code("\n".join(lines[-200:]) or "…")
    proc.wait()
    if buf.strip():
        lines.append(buf.strip())
    log_area.code("\n".join(lines[-200:]) or "…")
    full = "\n".join(lines)
    (status.success if proc.returncode == 0 else status.error)(
        f"{'done' if proc.returncode == 0 else 'exited ' + str(proc.returncode)} · {cmd[1] if len(cmd) > 1 else ''}")
    return proc.returncode, full


def read_resume_upload(up):
    """Plain text for .txt/.md; for .docx, pull the paragraph text out of the zip (no extra
    dependency — a .docx is just a zip with word/document.xml)."""
    raw = up.read()
    if up.name.lower().endswith(".docx"):
        try:
            xml = zipfile.ZipFile(io.BytesIO(raw)).read("word/document.xml").decode("utf-8", "replace")
            xml = xml.replace("</w:p>", "\n").replace("<w:tab/>", "\t")
            text = re.sub(r"<[^>]+>", "", xml)
            for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
                text = text.replace(a, b)
            return re.sub(r"\n{3,}", "\n\n", text).strip()
        except Exception as e:
            st.error(f"Could not read .docx: {e}")
            return ""
    return raw.decode("utf-8", "replace")


def load_json(path):
    p = Path(path)
    return json.load(open(p)) if p.exists() else None


def count_lines(path):
    p = Path(path)
    return sum(1 for _ in open(p)) if p.exists() else 0


def build_report_html(records):
    """Self-contained report.html (the AGENTS.md §5 template), so the shortlist travels
    as one file the person can double-click open."""
    cols = ["company", "title", "level", "salary_min_k", "salary_max_k", "remote_scope",
            "country_code", "score", "url", "role_summary", "model_score", "comparisons"]
    rows = []
    for r in records:
        row = {c: r.get(c) for c in cols}
        if row.get("score") is None:           # btrank emits model_score / fused_strength, not "score"
            row["score"] = r.get("model_score") or r.get("fused_strength") or r.get("bt_strength") or 0
        rows.append(row)
    data = json.dumps(rows)
    doc = """<!doctype html><meta charset=utf-8><title>Job matches</title>
<style>
 body{font:14px/1.5 system-ui;margin:2rem;max-width:1100px}
 input{padding:.5rem;width:100%;margin-bottom:1rem;font-size:1rem}
 table{border-collapse:collapse;width:100%} th,td{padding:.4rem .6rem;border-bottom:1px solid #ddd;text-align:left}
 th{cursor:pointer;background:#fafafa;position:sticky;top:0} tr:hover{background:#f6f9ff}
 a{color:#1558d6;text-decoration:none} .sub{color:#666;font-size:12px}
</style>
<input id=q placeholder="filter by company, title, summary...">
<table id=t><thead><tr>
 <th>Company</th><th>Title</th><th>Level</th><th>Salary (k)</th><th>Remote</th><th>Loc</th>
 <th data-sort=num>Match</th><th>Apply</th></tr></thead><tbody></tbody></table>
<script>
const D=__DATA__; const tb=document.querySelector('#t tbody'); let asc=false;
function sal(r){return r.salary_min_k>0?`${r.salary_min_k}-${r.salary_max_k>0?r.salary_max_k:'?'}`:''}
function draw(rows){tb.innerHTML=rows.map(r=>`<tr>
 <td>${r.company||''}</td>
 <td>${r.title||''}<div class=sub>${(r.role_summary||'').slice(0,120)}</div></td>
 <td>${r.level||''}</td><td>${sal(r)}</td><td>${r.remote_scope||''}</td><td>${r.country_code||''}</td>
 <td>${(r.score||0).toFixed(3)}</td>
 <td>${r.url?`<a href="${r.url}" target=_blank>apply</a>`:''}</td></tr>`).join('')}
draw(D);
q.oninput=e=>{const s=e.target.value.toLowerCase();
 draw(D.filter(r=>JSON.stringify(r).toLowerCase().includes(s)))};
document.querySelectorAll('th').forEach((th,i)=>th.onclick=()=>{asc=!asc;
 const k=['company','title','level','salary_min_k','remote_scope','country_code','score'][i];
 if(!k)return; draw([...D].sort((a,b)=>(a[k]>b[k]?1:-1)*(asc?1:-1)))});
</script>"""
    return doc.replace("__DATA__", data)


# ── sidebar: shared config ────────────────────────────────────────────────────────────────

st.sidebar.title("open-jobs")
st.sidebar.caption("hull → learn → rank")

workdir = Path(st.sidebar.text_input("Working directory", value=str(ROOT),
                                     help="where hull.json / decisions / ranked.json are written"))
workdir.mkdir(parents=True, exist_ok=True)

parquet = st.sidebar.text_input("Dataset (.parquet)", value=str(ROOT / "open-jobs.parquet"),
                                help="local path to the dataset; run `python3 download.py` to fetch it")
parquet_ok = Path(parquet).exists()
if parquet_ok:
    size_gb = Path(parquet).stat().st_size / 1e9
    st.sidebar.success(f"dataset found · {size_gb:.1f} GB")
else:
    st.sidebar.error("dataset not found — see the **Dataset** tab")

api_key = st.sidebar.text_input("OPENAI_API_KEY", type="password",
                                value=os.environ.get("OPENAI_API_KEY", ""),
                                help="needed for step 2 (langsort)")
env = {"OPENAI_API_KEY": api_key} if api_key else {}

HULL = str(workdir / "hull.json")
DECISIONS = str(workdir / "langsort_decisions.jsonl")
RANKED = str(workdir / "ranked.json")

st.title("Find the roles worth applying to")
st.caption("A GUI over the same scripts you'd run by hand. Each tab is one stage of the pipeline.")

tab_data, tab_hull, tab_learn, tab_rank = st.tabs(
    ["① Dataset", "② Hull", "③ Learn", "④ Rank →"])


# ── tab: dataset ──────────────────────────────────────────────────────────────────────────

with tab_data:
    st.subheader("The dataset")
    st.markdown(
        "~967K currently-open roles from 16 ATSs, one Parquet file (~21 GB), refreshed daily. "
        "Everything downstream reads from it. Point the sidebar at a local copy.")
    if parquet_ok:
        st.success(f"Using `{parquet}`")
    else:
        st.warning("No local dataset yet. Download it (resumable, ~21 GB):")
        st.code("python3 download.py -o open-jobs.parquet", language="bash")
        st.markdown(
            "The download is large and long-running, so it's best run in a terminal rather than "
            "from this app. Once it's on disk, set its path in the sidebar.")


# ── tab: hull (step 1) ────────────────────────────────────────────────────────────────────

with tab_hull:
    st.subheader("Step 1 — the convex hull")
    st.caption("Filter on **hard eligibility + broad recall only** — never on soft fit. "
               "The hull should be the smallest set that still contains every relevant role.")

    FUNCTIONS = ["", "engineering", "data", "design", "product", "sales", "marketing", "ops",
                 "security", "finance", "hr", "legal", "research", "support", "healthcare",
                 "education", "skilled-trade", "other"]
    c1, c2, c3 = st.columns(3)
    with c1:
        function = st.selectbox("Function", FUNCTIONS, index=1)
        country = st.text_input("Country (ISO-2)", value="US",
                                help="US also matches us-only / us-canada remote scope")
    with c2:
        level = st.text_input("Levels (comma)", value="Senior,Staff",
                              help="e.g. Senior,Staff,Principal")
        min_comp = st.number_input("Min comp on salary_max_k (k, 0 = off)", min_value=0, value=0,
                                   help="keeps unknown (-1) rows; unknown ≠ low")
    with c3:
        remote = st.checkbox("Require remote", value=True)
        require_visa = st.checkbox("Require visa sponsorship", value=False)
        include_staffing = st.checkbox("Include staffing/agency", value=False)
        include_management = st.checkbox("Include people-management", value=False)

    title_terms = st.text_input(
        "Title terms (comma) — matches title OR alt_titles, generous recall",
        value="software engineer,backend,platform,distributed systems")

    if st.button("Build hull", type="primary", disabled=not parquet_ok):
        cmd = [PY, str(ROOT / "hull.py"), "--in", parquet, "--out", HULL]
        if function:           cmd += ["--function", function]
        if level.strip():      cmd += ["--level", level.strip()]
        if country.strip():    cmd += ["--country", country.strip()]
        if title_terms.strip():cmd += ["--title", title_terms.strip()]
        if min_comp:           cmd += ["--min-comp", str(min_comp)]
        if remote:             cmd += ["--remote"]
        if require_visa:       cmd += ["--require-visa"]
        if include_staffing:   cmd += ["--include-staffing"]
        if include_management: cmd += ["--include-management"]
        with st.spinner("streaming the parquet (structured fields only)…"):
            rc, _ = run_stream(cmd, env)

    hull = load_json(HULL)
    if hull is not None:
        st.metric("Roles in hull", f"{len(hull):,}")
        if hull:
            df = pd.DataFrame(hull)
            show = [c for c in ["company", "title", "level", "remote_scope", "country_code",
                                "salary_min_k", "salary_max_k", "url"] if c in df.columns]
            st.dataframe(df[show], width="stretch", height=380)
        else:
            st.error("Empty hull — loosen a filter. The hull must CONTAIN every relevant role.")


# ── tab: learn (step 2) ───────────────────────────────────────────────────────────────────

with tab_learn:
    st.subheader("Step 2 — gather the LLM's pairwise judgments")
    st.caption("“Which of these two fits the resume better?” — better calibrated than 0–100 scoring. "
               "Every decision is appended to a log that replays on restart.")

    hull_ready = Path(HULL).exists()
    if not hull_ready:
        st.info("Build a hull first (step 2).")

    st.markdown("**Resume**")
    rc1, rc2 = st.columns([2, 1])
    with rc2:
        use_sample = st.checkbox("Use egd-resume.txt", value=False)
        up = st.file_uploader("…or upload", type=["txt", "md", "docx"])
    with rc1:
        default_resume = ""
        if use_sample and (ROOT / "egd-resume.txt").exists():
            default_resume = (ROOT / "egd-resume.txt").read_text(encoding="utf-8", errors="replace")
        elif up is not None:
            default_resume = read_resume_upload(up)
        resume_text = st.text_area("Resume text", value=default_resume, height=180,
                                   placeholder="Paste the resume here…")

    o1, o2, o3 = st.columns(3)
    with o1:
        mode = st.selectbox("Mode", ["sample", "sort"],
                            help="sample = parallel comparisons for learning; sort = exact total order")
        model = st.text_input("Model", value="gpt-5.4-nano-2026-03-17")
    with o2:
        per_item = st.number_input("Comparisons per role", min_value=1, value=12)
        workers = st.number_input("Workers", min_value=1, value=64)
    with o3:
        max_cmp = st.number_input("Max comparisons (0 = off)", min_value=0, value=0)
        no_gate = st.checkbox("Disable incomparability gate", value=False)

    can_learn = hull_ready and bool(api_key) and bool(resume_text.strip()) and parquet_ok
    if not api_key:
        st.warning("Set OPENAI_API_KEY in the sidebar to gather decisions.")
    if st.button("Gather decisions", type="primary", disabled=not can_learn):
        resume_path = workdir / "_resume.txt"
        resume_path.write_text(resume_text, encoding="utf-8")
        cmd = [PY, str(ROOT / "langsort.py"), "--resume", str(resume_path),
               "--candidates", HULL, "--parquet", parquet, "--log", DECISIONS,
               "--mode", mode, "--per-item", str(per_item), "--workers", str(workers),
               "--model", model]
        if max_cmp:  cmd += ["--max-comparisons", str(max_cmp)]
        if no_gate:  cmd += ["--no-gate"]
        st.info("This calls the LLM many times in parallel — it can take a while. Progress streams below.")
        with st.spinner("comparing roles…"):
            run_stream(cmd, env)

    n_dec = count_lines(DECISIONS)
    if n_dec:
        st.metric("Decisions gathered", f"{n_dec:,}")
        st.caption(f"Appended to `{DECISIONS}` — re-running resumes without repeating work.")


# ── tab: rank (step 3) ────────────────────────────────────────────────────────────────────

with tab_rank:
    st.subheader("Step 3 — aggregate into one ranking")
    st.caption("Decisions are GOLD (a partial order, topologically sorted); a distilled model only "
               "disambiguates incomparable / never-compared roles.")

    have_dec = Path(DECISIONS).exists()
    have_hull = Path(HULL).exists()
    if not (have_dec and have_hull):
        st.info("Need a hull (step 2) and decisions (step 3) first.")

    r1, r2 = st.columns(2)
    with r1:
        method = st.selectbox("Method", ["gold", "fuse", "bt"],
                              help="gold = decisions as gold partial order (default); "
                                   "fuse = softer blend; bt = plain Bradley-Terry (no embeddings)")
    with r2:
        distill = st.checkbox("Also save a corpus-wide model (taste.npz)", value=False)

    needs_parquet = method in ("gold", "fuse") or distill
    can_rank = have_dec and have_hull and (parquet_ok or not needs_parquet)
    if needs_parquet and not parquet_ok:
        st.warning(f"Method `{method}` needs the dataset (for embeddings). Set its path in the sidebar.")

    if st.button("Rank", type="primary", disabled=not can_rank):
        cmd = [PY, str(ROOT / "btrank.py"), "--candidates", HULL, "--decisions", DECISIONS,
               "--parquet", parquet, "--out", RANKED, "--method", method]
        if distill:
            cmd += ["--distill-out", str(workdir / "taste.npz")]
        with st.spinner("aggregating…"):
            run_stream(cmd, env)

    ranked = load_json(RANKED)
    if ranked:
        st.metric("Roles ranked", f"{len(ranked):,}")
        df = pd.DataFrame(ranked)
        score_col = next((c for c in ["model_score", "fused_strength", "bt_strength"] if c in df.columns), None)
        show = [c for c in ["rank", "company", "title", score_col, "comparisons", "compared", "url"]
                if c and c in df.columns]
        st.dataframe(
            df[show], width="stretch", height=460,
            column_config={"url": st.column_config.LinkColumn("apply", display_text="apply")}
                if "url" in show else None)

        html = build_report_html(ranked)
        st.download_button("⬇ Download report.html", data=html, file_name="report.html",
                           mime="text/html",
                           help="a self-contained, searchable, sortable page — open it in any browser")
        st.download_button("⬇ Download ranked.json", data=json.dumps(ranked, indent=1),
                           file_name="ranked.json", mime="application/json")
