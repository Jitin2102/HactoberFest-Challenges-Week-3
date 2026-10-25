import argparse, json, re, shutil, sqlite3, sys, time, tomllib, uuid, zipfile
import urllib.error, urllib.request
from pathlib import Path

SKIP_SUFFIXES = {".crdownload", ".part", ".tmp", ".download", ".partial"}
TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".py", ".js", ".ts", ".html", ".log",
                 ".tex", ".yaml", ".yml", ".toml", ".sql", ".xml", ".c", ".cpp", ".java"}

DEFAULTS = {
    "watch_dir": "~/Downloads",
    "dest_dir": "~/Sorted",
    "db_path": "~/.sloth-sorter.db",
    "ollama_host": "http://localhost:11434",
    "model": "qwen2.5:3b",
    "timeout_seconds": 180,
    "snippet_chars": 1200,
    "settle_seconds": 60,
    "date_prefix": True,
    "categories": ["documents", "invoices", "images", "code", "data", "media", "archives", "installers", "misc"],
    "rules": ["Anything that looks like a bill, receipt or invoice goes in invoices."],
}

EXT_FALLBACK = {
    "images": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".heic"},
    "media": {".mp3", ".mp4", ".mkv", ".wav", ".mov", ".flac"},
    "archives": {".zip", ".tar", ".gz", ".7z", ".rar"},
    "installers": {".exe", ".msi", ".dmg", ".pkg", ".deb", ".apk"},
    "code": {".py", ".js", ".ts", ".c", ".cpp", ".java", ".sh"},
    "data": {".csv", ".json", ".xlsx", ".parquet", ".sql"},
    "documents": {".pdf", ".docx", ".doc", ".txt", ".md", ".odt", ".pptx"},
}


def load_config(path):
    cfg = dict(DEFAULTS)
    if path and Path(path).exists():
        with open(path, "rb") as f:
            cfg.update(tomllib.load(f))
    for k in ("watch_dir", "dest_dir", "db_path"):
        cfg[k] = Path(cfg[k]).expanduser()
    if "misc" not in cfg["categories"]:
        cfg["categories"].append("misc")
    return cfg


# ---------- reading files ----------
def snippet(path, n):
    suf = path.suffix.lower()
    try:
        if suf in TEXT_SUFFIXES:
            return path.read_text(errors="ignore")[:n]
        if suf == ".docx":
            with zipfile.ZipFile(path) as z:
                xml = z.read("word/document.xml").decode("utf8", "ignore")
            return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", xml))[:n]
        if suf == ".pdf":
            try:
                from pypdf import PdfReader
            except ImportError:
                return ""
            r = PdfReader(str(path))
            return (r.pages[0].extract_text() or "")[:n] if r.pages else ""
    except Exception:
        return ""
    return ""


# ---------- talking to the local model ----------
def ollama(cfg, path_suffix, payload):
    req = urllib.request.Request(cfg["ollama_host"] + path_suffix,
                                 data=json.dumps(payload).encode() if payload else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=cfg["timeout_seconds"]) as r:
        return json.load(r)


def check_ollama(cfg, model):
    try:
        tags = ollama(cfg, "/api/tags", None)
    except (urllib.error.URLError, OSError):
        sys.exit(f"Can't reach Ollama at {cfg['ollama_host']}. Install it and run `ollama serve`.")
    names = {m["name"] for m in tags.get("models", [])}
    if model not in names and f"{model}:latest" not in names:
        sys.exit(f"Model '{model}' not found locally. Run: ollama pull {model}")


def kebab(s, limit=60):
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:limit].strip("-")


def classify(cfg, model, path):
    """Return (category, new_stem, source). Falls back to extension rules if the model misbehaves."""
    cats = cfg["categories"]
    rules = "\n".join(f"- {r}" for r in cfg["rules"])
    system = (
        "You are a tidy file clerk. Reply with JSON only, shaped exactly like "
        '{"category": "...", "name": "..."}.\n'
        f"category must be one of: {', '.join(cats)}.\n"
        "name is a short descriptive kebab-case name (2-6 words, no extension, no dates).\n"
        f"User rules:\n{rules}"
    )
    user = (f"Filename: {path.name}\nSize: {path.stat().st_size} bytes\n"
            f"Content start:\n{snippet(path, cfg['snippet_chars']) or '(unreadable or binary)'}")
    try:
        out = ollama(cfg, "/api/chat", {
            "model": model, "stream": False, "format": "json", "keep_alive": "10m",
            "options": {"temperature": 0, "num_ctx": 2048},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        })
        data = json.loads(out["message"]["content"])
        cat = str(data.get("category", "")).strip().lower()
        stem = kebab(str(data.get("name", "")))
        if cat in cats and stem:
            return cat, stem, "llm"
        if cat in cats:
            return cat, kebab(path.stem) or "file", "llm"
    except (urllib.error.URLError, OSError, ValueError, KeyError, json.JSONDecodeError):
        pass
    suf = path.suffix.lower()
    for cat, exts in EXT_FALLBACK.items():
        if suf in exts and cat in cats:
            return cat, kebab(path.stem) or "file", "fallback"
    return "misc", kebab(path.stem) or "file", "fallback"


# ---------- planning ----------
def scan(cfg):
    now = time.time()
    for p in sorted(cfg["watch_dir"].iterdir()):
        if (not p.is_file() or p.name.startswith(".") or p.suffix.lower() in SKIP_SUFFIXES
                or now - p.stat().st_mtime < cfg["settle_seconds"]):
            continue
        yield p


def unique(dst, taken):
    cand, i = dst, 2
    while cand.exists() or cand in taken:
        cand = dst.with_name(f"{dst.stem}-{i}{dst.suffix}")
        i += 1
    return cand


def make_plan(cfg, model, files=None, verbose=True):
    plan, taken = [], set()
    files = list(files if files is not None else scan(cfg))
    for i, p in enumerate(files, 1):
        cat, stem, src = classify(cfg, model, p)
        if cfg["date_prefix"]:
            stem = time.strftime("%Y-%m-%d", time.localtime(p.stat().st_mtime)) + "-" + stem
        dst = unique(cfg["dest_dir"] / cat / (stem + p.suffix.lower()), taken)
        taken.add(dst)
        plan.append((p, dst, cat, src))
        if verbose:
            print(f"[{i}/{len(files)}] {p.name}\n    -> {dst.relative_to(cfg['dest_dir'])}  ({src})")
    return plan


# ---------- applying and undoing ----------
def db(cfg):
    con = sqlite3.connect(cfg["db_path"])
    con.execute("""CREATE TABLE IF NOT EXISTS moves(
        id INTEGER PRIMARY KEY, batch TEXT, src TEXT, dst TEXT, ts REAL, undone INTEGER DEFAULT 0)""")
    return con


def apply_plan(cfg, plan):
    if not plan:
        print("Nothing to do. Sloth approves.")
        return
    con, batch = db(cfg), time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    for src, dst, _, _ in plan:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        con.execute("INSERT INTO moves(batch,src,dst,ts) VALUES(?,?,?,?)", (batch, str(src), str(dst), time.time()))
        con.commit()
    print(f"Moved {len(plan)} file(s). Undo with: sloth_sorter.py undo --batch {batch}")


def undo(cfg, batch=None):
    con = db(cfg)
    if batch is None:
        row = con.execute("SELECT batch FROM moves WHERE undone=0 ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return print("Nothing to undo.")
        batch = row[0]
    rows = con.execute("SELECT id,src,dst FROM moves WHERE batch=? AND undone=0 ORDER BY id DESC", (batch,)).fetchall()
    for mid, src, dst in rows:
        src, dst = Path(src), Path(dst)
        if not dst.exists():
            print(f"skip (missing): {dst}")
            continue
        target = unique(src, set()) if src.exists() else src
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(dst), str(target))
        con.execute("UPDATE moves SET undone=1 WHERE id=?", (mid,))
        con.commit()
        print(f"restored {target}")


# ---------- bench ----------
def bench(cfg, models, limit):
    files = list(scan({**cfg, "settle_seconds": 0}))[:limit]
    if not files:
        return print("No files to benchmark in watch_dir.")
    results = {}
    for m in models:
        check_ollama(cfg, m)
        t0, res = time.time(), []
        for p in files:
            res.append(classify(cfg, m, p)[:2])
        results[m] = (time.time() - t0, res)
        print(f"{m}: {len(files)} files in {results[m][0]:.1f}s")
    base = models[0]
    print(f"\n{'model':<22}{'sec/file':>9}{'category agreement vs ' + base:>34}")
    for m in models:
        t, res = results[m]
        agree = sum(a[0] == b[0] for a, b in zip(res, results[base][1]))
        print(f"{m:<22}{t / len(files):>9.1f}{agree:>20}/{len(files)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["plan", "apply", "undo", "watch", "bench"], nargs="?", default="plan")
    ap.add_argument("-c", "--config", default="config.toml")
    ap.add_argument("--model")
    ap.add_argument("--batch")
    ap.add_argument("--models", help="bench: comma-separated model names")
    ap.add_argument("--limit", type=int, default=10, help="bench: max files")
    ap.add_argument("--interval", type=int, default=300, help="watch: seconds between sweeps")
    a = ap.parse_args()
    cfg = load_config(a.config)
    model = a.model or cfg["model"]
    cfg["watch_dir"].mkdir(parents=True, exist_ok=True)

    if a.command == "undo":
        return undo(cfg, a.batch)
    if a.command == "bench":
        return bench(cfg, (a.models or model).split(","), a.limit)
    check_ollama(cfg, model)
    if a.command == "plan":
        make_plan(cfg, model)
        print("\nDry run only. Use `apply` to actually move files.")
    elif a.command == "apply":
        apply_plan(cfg, make_plan(cfg, model))
    elif a.command == "watch":
        print(f"Watching {cfg['watch_dir']} every {a.interval}s with {model}. Ctrl+C to stop.")
        while True:
            apply_plan(cfg, make_plan(cfg, model)) if any(True for _ in scan(cfg)) else None
            time.sleep(a.interval)


if __name__ == "__main__":
    main()
