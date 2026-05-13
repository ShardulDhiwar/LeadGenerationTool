"""
server.py — LeadGenerationTool Dashboard Backend
─────────────────────────────────────────────────
Serves the dashboard UI and provides all API endpoints.

Usage:
    pip install flask
    python server.py

Then open: http://localhost:5000
"""

import os
import sys
import json
import uuid
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from flask import Flask, jsonify, request, send_file, abort

# ── Fix Windows emoji/Unicode encoding ────────────────────────
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    os.environ["PYTHONIOENCODING"] = "utf-8"

# ── Paths ─────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
OUTPUTS_DIR = BASE_DIR / "outputs"
MERGED_DIR  = BASE_DIR / "merged"     # ← new: separate folder for merged files
DASHBOARD   = BASE_DIR / "dashboard.html"

# Allowed base directories for download/preview — both outputs and merged
ALLOWED_DIRS = [OUTPUTS_DIR, MERGED_DIR]

sys.path.insert(0, str(BASE_DIR))
try:
    from config.specialists import SPECIALISTS
    from config.cities      import CITIES
    from config.areas       import AREAS
except ImportError:
    SPECIALISTS = ["dentist", "cardiologist", "dermatologist", "orthopedic doctor",
                   "gynecologist", "neurologist", "general physician",
                   "physiotherapist", "nephrologist", "endocrinologist"]
    CITIES = ["Pune", "Mumbai", "Bangalore", "Hyderabad", "Delhi"]
    AREAS  = ["Baner", "Wakad", "Kothrud", "Viman Nagar", "Hadapsar",
              "Aundh", "Shivajinagar", "Katraj", "Hinjewadi", "Koregaon Park", "Chikhali", ""]

# ── In-memory job store ───────────────────────────────────────
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()

app = Flask(__name__)


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _is_allowed_path(p: Path) -> bool:
    """Return True if the resolved path is inside outputs/ or merged/."""
    resolved = p.resolve()
    return any(
        str(resolved).startswith(str(d.resolve()))
        for d in ALLOWED_DIRS
    )


def _read_folder(base_dir: Path, folder_type: str) -> list[dict]:
    """
    Scan a directory (outputs/ or merged/) and return folder metadata
    in the same shape the dashboard expects.
    `folder_type` is attached so the dashboard can style them differently.
    """
    base_dir.mkdir(exist_ok=True)
    folders = []

    for entry in sorted(base_dir.iterdir(), reverse=True):
        if not entry.is_dir() or entry.name.startswith("."):
            continue

        files         = []
        total_records = 0

        for f in sorted(entry.iterdir()):
            if f.suffix not in (".json", ".csv"):
                continue

            size    = f.stat().st_size
            records = None

            if f.suffix == ".json":
                try:
                    data    = json.loads(f.read_text(encoding="utf-8"))
                    records = len(data) if isinstance(data, list) else None
                    total_records += records or 0
                except Exception:
                    pass

            files.append({
                "name":    f.name,
                "path":    str(f),
                "type":    f.suffix.lstrip("."),
                "size":    size,
                "records": records,
            })

        folders.append({
            "folder":        entry.name,
            "path":          str(entry),
            "folder_type":   folder_type,   # "outputs" | "merged"
            "file_count":    len(files),
            "total_records": total_records,
            "files":         files,
        })

    return folders


# ─────────────────────────────────────────────────────────────
# Serve dashboard
# ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if not DASHBOARD.exists():
        abort(404, "dashboard.html not found — place it next to server.py")
    return send_file(DASHBOARD)


# ─────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────

@app.route("/api/config")
def api_config():
    return jsonify({
        "specialists": SPECIALISTS,
        "cities":      CITIES,
        "areas":       [a for a in AREAS if a],   # drop blank entry
        "sources":     ["google_maps", "practo"],  # justdial excluded intentionally
    })


# ─────────────────────────────────────────────────────────────
# Start a scrape job
# ─────────────────────────────────────────────────────────────

@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    body = request.get_json(force=True)

    mode         = body.get("mode", "single")
    source       = body.get("source", "google_maps")
    specialty    = body.get("specialty", "dentist")
    city         = body.get("city", "Pune")
    area         = body.get("area", "")
    max_listings = int(body.get("max_listings", 20))
    pause        = int(body.get("pause", 5))

    # Safety: only allow known sources
    allowed_sources = {"google_maps", "practo", "both"}
    if source not in allowed_sources:
        source = "google_maps"

    cmd = [sys.executable, str(BASE_DIR / "main.py"), "--source", source]

    if mode == "full_run":
        cmd += ["--full-run"]
    elif mode == "all_areas":
        cmd += ["--all-specialists", "--all-areas", "--city", city]
    elif mode == "all_specialists":
        cmd += ["--all-specialists", "--city", city]
        if area:
            cmd += ["--area", area]
    else:  # single
        cmd += ["--specialty", specialty, "--city", city]
        if area:
            cmd += ["--area", area]

    cmd += ["--max", str(max_listings), "--pause", str(pause)]

    job_id = str(uuid.uuid4())[:8]
    job = {
        "job_id":     job_id,
        "status":     "running",
        "command":    " ".join(cmd),
        "started_at": datetime.utcnow().isoformat(),
        "log":        [],
    }

    with JOBS_LOCK:
        JOBS[job_id] = job

    t = threading.Thread(target=_run_job, args=(job_id, cmd), daemon=True)
    t.start()

    return jsonify({"job_id": job_id})


def _run_job(job_id: str, cmd: list[str]):
    try:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"

        proc = subprocess.Popen(
            cmd,
            cwd=str(BASE_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )

        for line in proc.stdout:
            line = line.rstrip()
            with JOBS_LOCK:
                JOBS[job_id]["log"].append(line)

        proc.wait()

        with JOBS_LOCK:
            JOBS[job_id]["status"] = "done" if proc.returncode == 0 else "error"

    except Exception as e:
        with JOBS_LOCK:
            JOBS[job_id]["log"].append(f"ERROR: {e}")
            JOBS[job_id]["status"] = "error"


# ─────────────────────────────────────────────────────────────
# Job status
# ─────────────────────────────────────────────────────────────

@app.route("/api/jobs")
def api_jobs():
    with JOBS_LOCK:
        jobs = sorted(JOBS.values(), key=lambda j: j["started_at"], reverse=True)
        return jsonify([
            {k: v for k, v in j.items() if k != "log"}
            for j in jobs
        ])


@app.route("/api/job/<job_id>")
def api_job(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        abort(404)
    return jsonify(job)


# ─────────────────────────────────────────────────────────────
# File browser — raw outputs/ only
# ─────────────────────────────────────────────────────────────

@app.route("/api/files")
def api_files():
    folders = _read_folder(OUTPUTS_DIR, "outputs")
    return jsonify(folders)


# ─────────────────────────────────────────────────────────────
# Merged file browser — merged/ only
# ─────────────────────────────────────────────────────────────

@app.route("/api/merged")
def api_merged():
    folders = _read_folder(MERGED_DIR, "merged")
    return jsonify(folders)


# ─────────────────────────────────────────────────────────────
# Preview (first 20 records) — works for outputs/ AND merged/
# ─────────────────────────────────────────────────────────────

@app.route("/api/preview")
def api_preview():
    path = request.args.get("path", "")
    p    = Path(path)

    if not p.exists() or p.suffix != ".json":
        abort(404)
    if not _is_allowed_path(p):
        abort(403)

    data    = json.loads(p.read_text(encoding="utf-8"))
    records = data if isinstance(data, list) else []

    return jsonify({"total": len(records), "records": records[:20]})


# ─────────────────────────────────────────────────────────────
# Download — works for outputs/ AND merged/
# ─────────────────────────────────────────────────────────────

@app.route("/api/download")
def api_download():
    path = request.args.get("path", "")
    p    = Path(path)

    if not p.exists():
        abort(404)
    if not _is_allowed_path(p):
        abort(403)

    return send_file(p, as_attachment=True)


# ─────────────────────────────────────────────────────────────
# Run
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Ensure both directories exist on startup
    OUTPUTS_DIR.mkdir(exist_ok=True)
    MERGED_DIR.mkdir(exist_ok=True)

    print("─" * 50)
    print("  LeadGen Dashboard")
    print("  http://localhost:5000")
    print("─" * 50)
    app.run(debug=False, port=5000, threaded=True)