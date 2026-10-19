import io
import json
import os
import re
import secrets
import sys
import threading
from contextlib import redirect_stdout

import groq
from groq import Groq
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, request, send_from_directory, session

import get_the_system_spec  # models/get_the_system_spec.py

# groq_bot.py lives in <main folder>/models, .env lives in <main folder>
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
load_dotenv(os.path.join(ROOT_DIR, ".env"))


def _load_spec_checker():
    """Import models/spec_checker.py. Also accepts the old file names (spec-checker.py, 'spec checker.py')."""
    try:
        import spec_checker as mod
    except ModuleNotFoundError:
        import importlib.util
        mod = None
        for name in ("spec_checker.py", "spec-checker.py", "spec checker.py"):
            path = os.path.join(BASE_DIR, name)
            if os.path.exists(path):
                spec = importlib.util.spec_from_file_location("spec_checker", path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                break
        if mod is None:
            raise ModuleNotFoundError(f"spec_checker.py not found in {BASE_DIR}")
    if not hasattr(mod, "judge"):
        raise ImportError(f"{mod.__file__} is the OLD chat-only file. Replace it with the new spec_checker.py.")
    return mod


spec_checker = _load_spec_checker()

# .env line:  GROQ_API_KEYS=key1,key2,key3,key4
API_KEYS = [k.strip() for k in os.getenv("GROQ_API_KEYS", "").split(",") if k.strip()]
MODEL = "openai/gpt-oss-120b"

clients = [Groq(api_key=k, timeout=120) for k in API_KEYS]  # web search answers take longer than 60 s sometimes
current = 0  # index of the key that last worked

# main.html is expected in <main folder>/screens -- change this if yours is elsewhere
SCREENS_DIR = os.path.join(ROOT_DIR, "screens")

# static_folder=None: every page is served by a route below, so main.html cannot be opened directly as /main.html
app = Flask(__name__, static_folder=None)
# signs the login cookie; put SECRET_KEY=<long random text> in .env to stay signed in across restarts
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

# sign-up / sign-in / OTP / password-reset routes live in <main folder>/user_data_verification/server.py
sys.path.insert(0, os.path.join(ROOT_DIR, "user_data_verification"))
from server import auth  # noqa: E402  (this also loads storage/db_utils.py and otp_utils.py)

app.register_blueprint(auth)

SYSTEM_PROMPT = """You are GPS (Get PC Specs), a friendly assistant that helps people \
understand software and whether their PC can run it.

Accuracy rules. The person decides whether to download something from your answer, so a wrong \
figure is worse than no figure:
- Before answering about a specific software, AI model or game, look up its official requirements with web \
search. Prefer the official site, documentation or model card over blogs.
- Use only figures you found or can calculate. If something is not published, write "not published" instead \
of guessing. Never invent hardware names (it is GTX 1650, not RTX 1650).
- For local AI models, calculate from the parameter count: weights in GB = billions of parameters x bytes per \
weight (FP16 = 2, Q8 = 1, 4-bit Q4_K_M = about 0.6), then add 1 to 2 GB for the context cache and runtime. \
A 4-bit model is Q4, never Q8. Take the context length from the model card.
- Software often comes in several builds. Make the Minimum column the smallest build that really runs (for \
Ollama models, the default tag, usually 4-bit) and the Recommended column a comfortable one, and say which \
build each column is for.
- If it runs without a dedicated GPU, say so and write "not required" as the GPU minimum.
- If you used web search, put one line "Sources: <site names you actually opened>" after the text and before the tables.

Format every answer like this:
1. Write ALL the explanation first, as short paragraphs and simple bullet lists. \
Cover everything in words: what it is, how it works, tips, and the verdict.
2. Put EVERY table at the very END of the answer, after all the text. \
Never place a table between paragraphs or sections.
3. Before the tables, add one line: "### Tables". Then list the tables one after another, \
each with a short bold title line above it.
4. Use a table only for side-by-side comparisons (for example system requirements). \
Skip tables if the answer does not need them.
5. Keep headings simple (## or ###), no emojis, no horizontal rules, and no closing \
"let me know" offers. Each table must be a proper markdown table with one row per line.
6. The very last line of EVERY answer, after everything else, must be exactly one tag: \
[[SOFTWARE: <name>]] where <name> is the official name of the software the person is asking \
about right now, with the version only if they gave one (for example [[SOFTWARE: Blender 4.2]]). \
If the message is not about one specific software, write [[SOFTWARE: NONE]]. \
Never mention or explain this tag in the text."""

# Only the user/assistant turns are stored here; the system prompt is added per request.
conversation = []
last_software = None  # name of the software the user last chatted about (set from the [[SOFTWARE: ...]] tag)

SOFTWARE_TAG = re.compile(r"\[\[\s*SOFTWARE\s*:(.*?)\]\]", re.I | re.S)


def split_software_tag(reply):
    """Remove the [[SOFTWARE: name]] tag(s) from a reply. Returns (clean_reply, name or None)."""
    name = None
    for m in SOFTWARE_TAG.finditer(reply):
        found = re.sub(r"\s+", " ", m.group(1)).strip()[:80]
        if found and not re.fullmatch(r"none|n/?a|unknown|-", found, re.I):
            name = found
    clean = SOFTWARE_TAG.sub("", reply)
    clean = re.sub(r"\[\[\s*SOFTWARE\b.*$", "", clean, flags=re.I | re.S)  # tag cut off mid-way
    return clean.rstrip(), name


CITE_MARK = re.compile(r"\s*【[^】]*】")  # browser_search leaves markers like 【2†L119-L124】 in the text


def chat(messages, system=SYSTEM_PROMPT, search=False, **opts):
    """Try each key in turn, starting with the last one that worked.
    search=True lets the model look things up (Groq browser_search); if Groq rejects that, answer without it."""
    global current
    if not clients:
        raise RuntimeError(
            "GROQ_API_KEYS is not configured. Add it to the .env file in the main folder."
        )
    last_error = None
    call_opts = dict(opts)
    if search:
        call_opts.update(tools=[{"type": "browser_search"}], extra_body={"reasoning_effort": "low"})
    for offset in range(len(clients)):
        idx = (current + offset) % len(clients)
        try:
            response = clients[idx].chat.completions.create(
                model=MODEL,
                messages=[{"role": "system", "content": system}] + messages,
                **call_opts,
            )
            current = idx
            return CITE_MARK.sub("", response.choices[0].message.content or "")
        except groq.BadRequestError as e:
            if search:  # Groq refused the search tool: answer without it rather than fail
                print(f"[web search rejected: {e}] answering without it")
                return chat(messages, system, search=False, **opts)
            raise  # a bad request fails on every key, so don't rotate
        except Exception as e:
            last_error = e
            print(f"[Key {idx + 1} failed: {e}] Trying next key...")
    raise last_error


@app.after_request
def no_cache(resp):
    # stop the browser from showing an old copy of main.html
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.before_request
def require_verified_user():
    """main.html and the APIs it calls are only for someone who finished sign-up verification or signed in."""
    if session.get("email"):
        return None
    if request.path in ("/main", "/main.html"):
        return redirect("/accounts")
    if request.path in ("/api/chat", "/api/software", "/api/collect_spec", "/api/verdict", "/api/reset"):
        return jsonify({"error": "Sign in first"}), 401
    return None


@app.route("/")
@app.route("/home.html")
def index():
    path = os.path.join(SCREENS_DIR, "home.html")
    if not os.path.exists(path):
        return f"home.html not found at: {path}", 404
    return send_from_directory(SCREENS_DIR, "home.html")


@app.route("/main")
@app.route("/main.html")
def main_page():
    path = os.path.join(SCREENS_DIR, "main.html")
    if not os.path.exists(path):
        return f"main.html not found at: {path}", 404
    return send_from_directory(SCREENS_DIR, "main.html")


@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.get_json(silent=True) or {}
    user_message = (data.get("message") or "").strip()
    if not user_message:
        return jsonify({"error": "Empty message"}), 400

    conversation.append({"role": "user", "content": user_message})
    try:
        reply = chat(conversation, search=True)
    except Exception as e:
        conversation.pop()  # don't keep a failed turn in history
        return jsonify({"error": str(e)}), 500

    global last_software
    reply, software = split_software_tag(reply)
    if software:
        last_software = software  # "NONE" keeps the previous software
    conversation.append({"role": "assistant", "content": reply})
    return jsonify({"reply": reply, "software": last_software})


@app.route("/api/software", methods=["GET"])
def api_software():
    return jsonify({"software": last_software})


# ---------------------------------------------------------------- system spec
_spec_lock = threading.Lock()  # redirect_stdout is process-wide, so one scan at a time


def collect_system_spec():
    """Run get_the_system_spec.py's main() and capture everything it prints."""
    buffer = io.StringIO()
    with _spec_lock, redirect_stdout(buffer):
        get_the_system_spec.main()
    return buffer.getvalue().strip()


def parse_spec_lines(spec_text):
    """Every printed 'Label: value' line -> (label, value). Plain split on the first colon."""
    items = []
    for line in spec_text.splitlines():
        label, sep, value = line.partition(":")
        label, value = label.strip(), value.strip()
        if sep and label:
            items.append((label, value))
    return items


# Lines that are never shown (GPU memory / driver version, installed .NET Framework versions)
HIDDEN_LABELS = re.compile(r"^(?:GPU \d+ (?:AdapterRAM|DriverVersion)|\.NET Framework\b.*)$", re.I)


def _hidden(label):
    return bool(HIDDEN_LABELS.match(label.strip()))


def _int(text):
    m = re.search(r"\d+", text or "")
    return int(m.group()) if m else None


def _tidy(name):
    name = re.sub(r"\((?:R|TM)\)", "", name, flags=re.I)
    name = re.sub(r"\s+with\s+.*$", "", name, flags=re.I)  # "with Radeon Graphics"
    name = re.sub(r"\s*@.*$", "", name)                      # "@ 3.30GHz"
    name = re.sub(r"\b(?:CPU|Processor)\b", "", name)
    return re.sub(r"\s+", " ", name).strip()


def summarize_spec(items):
    """Split the scan into the 5 Step-1 chips (CPU, GPU, RAM, Disk, OS) and
    'details' = every other line, which is shown in Step 2."""
    d = dict(items)
    used = set()  # labels already shown as a chip, so they are not repeated in Step 2

    # CPU
    if d.get("CPU Name"):
        cpu_raw = d["CPU Name"]
        used.add("CPU Name")
    else:
        cpu_raw = d.get("Processor", "N/A")
        used.add("Processor")
    cpu = re.sub(r"^(?:AMD|Intel)\s+", "", _tidy(cpu_raw)) or "N/A"

    # GPU: if there are several (iGPU + dedicated), show the one with the most memory
    gpus = {}
    for label, value in items:
        m = re.fullmatch(r"GPU (\d+) (\w+)", label)
        if m:
            gpus.setdefault(int(m.group(1)), {})[m.group(2)] = value
    gpu = "N/A"
    best = None
    for idx, g in sorted(gpus.items()):
        if not g.get("Name"):
            continue
        # dedicated cards win; AdapterRAM is optional (it is no longer collected or shown)
        discrete = bool(re.search(r"nvidia|geforce|rtx|gtx|quadro|radeon (?:rx|pro)|arc\s*[ab]\d", g["Name"], re.I))
        score = (discrete, _int(g.get("AdapterRAM")) or 0)
        if best is None or score > best[1]:
            best = (idx, score)
    if best:
        gpu = re.sub(r"^(?:NVIDIA\s+)?(?:GeForce\s+)?", "", _tidy(gpus[best[0]]["Name"])) or "N/A"
        used.add(f"GPU {best[0]} Name")

    # RAM
    ram_bytes = _int(d.get("Total RAM"))
    ram = f"{round(ram_bytes / 1024 ** 3)} GB" if ram_bytes else "N/A"
    used.add("Total RAM")

    # Disk: free space on the system drive
    drive = os.environ.get("SystemDrive", "C:")[0].upper()
    disk_label = f"Drive {drive} FreeSpace (bytes)"
    if disk_label not in d:
        disk_label = next((l for l, _ in items if l.endswith("FreeSpace (bytes)")), None)
    free = _int(d.get(disk_label)) if disk_label else None
    disk = f"{int(free / 1024 ** 3)} GB free" if free is not None else "N/A"
    if disk_label:
        used.add(disk_label)

    # OS (Python < 3.10 reports Windows 11 as "10", so decide by build number)
    os_name = d.get("OS Name", "N/A")
    build = _int(d.get("OS Version", "").split(".")[-1])
    if os_name.startswith("Windows") and build is not None and build >= 22000:
        os_name = "Windows 11"
    used.add("OS Name")

    chips = [
        {"label": "CPU", "value": cpu},
        {"label": "GPU", "value": gpu},
        {"label": "RAM", "value": ram},
        {"label": "Disk", "value": disk},
        {"label": "OS", "value": os_name},
    ]
    details = [{"label": l, "value": v or "-"} for l, v in items
               if l not in used and not _hidden(l)]
    return chips, details


# ------------------------------------------------- software spec (Groq) + guard
CHIP_LABELS = ["CPU", "GPU", "RAM", "Disk", "OS"]

SPEC_SYSTEM = """You are a data service. Given a software name and a list of labels, return what that \
software needs, using exactly those labels, so it can be lined up next to a PC scan that uses the same labels.

Output ONLY one JSON object: {"<label>": "<value>", ...} with every label given, spelled exactly as given. \
No markdown, no commentary. Each value is a short plain-text string (max 100 characters).

Where to take the values from, in this order:
1. The requirements already given in the conversation (use the Minimum column; if only estimates or a single \
column are given, use those figures as stated). For software that comes in several builds, use the smallest default build.
2. The software's official system requirements.
3. Otherwise "N/A". Never invent a figure that neither source gives, but do convert and map what is given \
(for example 80 GB -> bytes when the label says bytes, 2.9 GHz -> 2900 for MHz, a named CPU's core count \
when you are certain of it).
How to fill labels:
- CPU / Processor: the required CPU model or class. CPU chip style: "Core i5-8400", no vendor prefix.
- GPU / GPU N Name: the required graphics card. GPU chip style: "GTX 1060", no vendor prefix. If it runs \
without a dedicated GPU, write "Not required".
- RAM / Total RAM / Available RAM: the required memory, as "16 GB" (use bytes only if the label says bytes).
- Disk: free disk space needed, as "40 GB free".
- OS / OS Name: "Windows 10" or "Windows 11" (the lowest supported). OS Version: the lowest supported \
build, as "10.0.19041". If macOS/Linux only, say so.
- System Type / Architecture / Machine: the required architecture, as "AMD64" or "64bit" to match the label.
- Logical cores / Physical cores (any variant): the minimum core count as a number.
- Max Clock Speed (MHz): the minimum CPU clock in MHz as a number.
- Drive ... FreeSpace (bytes): the free space the software needs, in bytes as a number.
- Drive ... Size (bytes), Memory Load, and anything that is only a live reading of a PC: "N/A".
Anything else: the closest software requirement, or "N/A"."""


def fetch_software_spec(name, detail_labels):
    """Ask Groq what `name` needs, keyed by the same labels the PC scan produced.
    The recent chat is included, so the figures match what GPS already told the user."""
    labels = CHIP_LABELS + [l for l in detail_labels if l not in CHIP_LABELS]
    talk = "\n\n".join(f"[{m['role']}]\n{m['content'][:3500]}" for m in list(conversation)[-6:])
    prompt = (f"Software: {name}\nLabels:\n" + "\n".join(f"- {l}" for l in labels)
              + f"\n\nConversation so far:\n{talk or '(none)'}")
    raw = chat([{"role": "user", "content": prompt}], system=SPEC_SYSTEM, temperature=0.1)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Groq did not return JSON")
    data = json.loads(raw[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("Groq did not return a JSON object")
    lower = {re.sub(r"\s+", " ", str(k)).strip().lower(): v for k, v in data.items()}
    clean = {}
    for l in labels:  # only the requested labels survive, values are flattened and capped
        v = data[l] if l in data else lower.get(l.lower(), "N/A")
        v = re.sub(r"\s+", " ", str(v)).strip()[:120]
        clean[l] = v or "N/A"
    if all(v.upper() == "N/A" for v in clean.values()):
        print(f"[groq_bot.py] every value came back N/A for {name!r}. Raw Groq answer:\n{raw[:1500]}")
    chips = [{"label": l, "value": clean[l]} for l in CHIP_LABELS]
    details = [{"label": l, "value": clean[l]} for l in labels if l not in CHIP_LABELS]
    return chips, details


_guard_lock = threading.Lock()
_guard_layer = None


def load_guard():
    """Import meta_llama_layer once (loads Prompt-Guard-86M). Errors are reported to the user, not fatal."""
    global _guard_layer
    with _guard_lock:
        if _guard_layer is None:
            if BASE_DIR not in sys.path:
                sys.path.insert(0, BASE_DIR)
            import meta_llama_layer
            _guard_layer = meta_llama_layer
        return _guard_layer


def _warm_guard():
    try:
        load_guard()
        print("[groq_bot.py] Prompt Guard ready")
    except Exception as e:
        print(f"[groq_bot.py] Prompt Guard not ready yet: {type(e).__name__}: {e}")


def guard_check(text):
    """Run Groq's output through meta_llama_layer.check_prompt as third-party content.
    Prompt Guard only reads ~512 tokens, so the text is checked in chunks."""
    layer = load_guard()
    chunks, cur = [], ""
    for line in text.splitlines():
        if cur and len(cur) + len(line) > 1200:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    worst = None
    for c in chunks:
        r = layer.check_prompt(c, source="third_party")
        if worst is None or (r["blocked"], r["score"]) > (worst["blocked"], worst["score"]):
            worst = r
    return worst


def software_report(details):
    """Software half of 'Take my system data'. Never raises: returns ok/message for the UI."""
    name = last_software
    if not name:
        return {"name": None, "ok": False, "stage": "none",
                "message": "No software yet. Ask GPS about one, then press the button again."}
    try:
        chips, sw_details = fetch_software_spec(name, [d["label"] for d in details])
    except Exception as e:
        return {"name": name, "ok": False, "stage": "groq",
                "message": f"Failed: could not get {name} specs from Groq ({type(e).__name__}: {str(e)[:140]})."}
    text = "\n".join([f"Software: {name}"] + [f"{x['label']}: {x['value']}" for x in chips + sw_details])
    try:
        verdict = guard_check(text)
    except Exception as e:
        return {"name": name, "ok": False, "stage": "guard",
                "message": f"Failed: Prompt Guard could not check the {name} data ({type(e).__name__}: {str(e)[:140]})."}
    print(f"[guard] {name}: {verdict}")
    if verdict["blocked"]:
        return {"name": name, "ok": False, "stage": "guard",
                "message": f"Failed: Prompt Guard blocked the {name} data ({verdict['label']}, {verdict['score']})."}
    return {"name": name, "ok": True, "stage": "done", "chips": chips, "details": sw_details,
            "message": f"Success: {name} specs from Groq passed the Prompt Guard check."}


@app.route("/api/collect_spec", methods=["POST"])
def api_collect_spec():
    try:
        items = parse_spec_lines(collect_system_spec())
        if not items:
            return jsonify({"error": "get_the_system_spec.py printed nothing"}), 500
        chips, details = summarize_spec(items)
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
    return jsonify({"chips": chips, "details": details, "software": software_report(details)})


# ------------------------------------- Step 4: the local spec-checker model (Ollama)
@app.route("/api/verdict", methods=["POST"])
def api_verdict():
    """Grade the rows shown in Step 3 (software needs vs this PC) with the local spec-checker model."""
    data = request.get_json(silent=True) or {}
    rows = data.get("rows")
    if not isinstance(rows, list) or not rows:
        return jsonify({"error": "No rows to judge. Run the scan first."}), 400
    try:
        result = spec_checker.judge(data.get("software") or last_software, rows)
    except spec_checker.OllamaError as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
    print(f"[spec-checker] {result['software']}: run={result['run']} {result['counts']}")
    return jsonify(result)


@app.route("/api/reset", methods=["POST"])
def api_reset():
    global last_software
    conversation.clear()
    last_software = None
    return jsonify({"ok": True})


if __name__ == "__main__":
    print(f"[groq_bot.py] .env path : {os.path.join(ROOT_DIR, '.env')}")
    print(f"[groq_bot.py] Groq keys : {len(API_KEYS)} loaded")
    print(f"[groq_bot.py] serving   : {os.path.join(SCREENS_DIR, 'main.html')}")
    print(f"[groq_bot.py] verdicts  : Ollama model '{spec_checker.MODEL}' at {spec_checker._ollama_url()}")
    threading.Thread(target=_warm_guard, daemon=True).start()  # load Prompt Guard in the background
    # use_reloader=False: the auto-reloader restarts the server (and drops requests) while transformers loads
    app.run(host="127.0.0.1", port=5000, debug=True, use_reloader=False)