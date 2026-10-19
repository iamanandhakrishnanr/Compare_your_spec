"""spec_checker.py -- the local "spec-checker" Ollama model.

Renamed from spec-checker.py: a hyphen cannot be imported, and groq_bot.py now imports this file.

  judge(software, rows)         used by groq_bot.py (/api/verdict). Grades every Step 3 row as
                                pass / partial / fail and decides whether the software will run.
  python spec_checker.py         the old interactive chat (saved to a .txt file on Ctrl+C)
  python spec_checker.py --demo  grades a built-in sample table; handy for testing the prompt
"""
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime

MODEL = os.getenv("SPEC_CHECKER_MODEL", "spec-checker")
NUM_CTX = 8192   # Ollama's default context is only 2-4k tokens and silently cuts the prompt
MAX_ROWS = 60
VERDICTS = ("pass", "partial", "fail")


class OllamaError(RuntimeError):
    """Ollama is down, the model is missing, or it returned something unusable."""


def _ollama_url():
    host = os.getenv("OLLAMA_HOST", "127.0.0.1:11434").strip().rstrip("/").replace("0.0.0.0", "127.0.0.1")
    if not host.startswith(("http://", "https://")):
        host = "http://" + host
    if not re.search(r":\d+$", host):
        host += ":11434"
    return host


# ------------------------------------------------------------------ the prompt
def _compare_prompt(a, b):
    """The ONLY prompt the spec-checker model gets: two parts, 'which is better in performance', one-word answer.
    Built by joining strings (not an f-string) because the template contains literal { and } characters."""
    return (a + " or " + b + " which is better in performance; answer in one word{ "
            + a + " or " + b + " or are they similar [remember only say similar if they are similar]}}")


def _read_comparison(reply, a, b):
    """Turn the model's one-word reply into "a", "b", "similar", or None when it cannot be read."""
    norm = lambda t: re.sub(r"[^a-z0-9]", "", str(t).lower())
    r = norm(re.sub(r"<think>.*?</think>", "", reply or "", flags=re.S))  # reasoning models
    na, nb = norm(a), norm(b)
    if not r:
        return None
    if r.startswith("similar") or na == nb:
        return "similar"
    if r == na:
        return "a"
    if r == nb:
        return "b"
    in_a = bool(na) and (na in r or r in na)
    in_b = bool(nb) and (nb in r or r in nb)
    if in_a != in_b:
        return "a" if in_a else "b"
    return None


# --------------------------------------------------------------- talking to Ollama
def _ollama_chat(user, num_predict, timeout):
    payload = {
        "model": MODEL,
        "stream": False,
        "messages": [{"role": "user", "content": user}],  # no system prompt, no JSON schema: just the comparison line
        "options": {"temperature": 0, "num_ctx": NUM_CTX, "num_predict": num_predict},
    }
    req = urllib.request.Request(
        _ollama_url() + "/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.load(resp)
    except urllib.error.HTTPError as e:   # must come before URLError, which it subclasses
        detail = e.read().decode("utf-8", "replace")[:200]
        raise OllamaError(f"Ollama answered HTTP {e.code} for model '{MODEL}': {detail}")
    except OSError as e:                  # connection refused, timeout, DNS ...
        raise OllamaError(
            f"Could not reach Ollama at {_ollama_url()}. Start it, and check that the "
            f"'{MODEL}' model exists (ollama list). ({e})"
        )
    try:
        return body["message"]["content"]
    except (KeyError, TypeError):
        raise OllamaError("Ollama sent an unexpected reply.")


def _parse_json(raw):
    raw = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S)  # reasoning models
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise OllamaError("spec-checker did not return JSON.")
    try:
        return json.loads(raw[start:end + 1])
    except json.JSONDecodeError as e:
        raise OllamaError(f"spec-checker returned broken JSON ({e}).")


# ------------------------------------------------------------------- clean + merge
def _flat(value, limit):
    text = re.sub(r"\s+", " ", str(value if value is not None else "")).strip().replace("|", "/")
    return text[:limit] or "—"


def _clean_rows(rows):
    if not isinstance(rows, list):
        raise ValueError("rows must be a list")
    return [
        {"label": _flat(r.get("label"), 60), "needs": _flat(r.get("needs"), 120), "has": _flat(r.get("has"), 120)}
        for r in rows[:MAX_ROWS] if isinstance(r, dict)
    ]


_WORDS = {"pass": "pass", "passed": "pass", "ok": "pass", "yes": "pass",
          "partial": "partial", "partially": "partial",
          "fail": "fail", "failed": "fail", "no": "fail"}


def _verdict(value):
    words = re.sub(r"[^a-z]", " ", str(value).lower()).split()
    return _WORDS.get(words[0]) if words else None


_DRIVE = re.compile(r"^drive\s+\w+\s+freespace", re.I)


def _is_disk(label):
    return label.strip().lower() == "disk" or bool(_DRIVE.match(label.strip()))


# ------------------------------------------------ exact checks (a 3B model cannot do sums reliably)
_MEM = {"b": 1, "byte": 1, "bytes": 1, "kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3, "tb": 1024 ** 4}
_FREQ = {"mhz": 1, "ghz": 1000}
_QTY = re.compile(r"^\s*(?:~|≈|>=|≥|at least|min\.?|minimum)?\s*(\d[\d,]*(?:\.\d+)?)\s*([a-z]+)?", re.I)
_NONE_NEEDED = re.compile(r"(—|-|n/?a|none|not required|no requirement|any)\.?", re.I)
_VAGUE_CPU = re.compile(r"\b(modern|any|recent|multi-?core|dual-?core|quad-?core|64-?bit)\b", re.I)
_SPECIFIC_CPU = re.compile(r"\d{3,}|\bi[3579]\b|ryzen|xeon|athlon|pentium|celeron|epyc|threadripper|\bm[123]\b", re.I)
_ARCH_LABELS = {"system type", "architecture", "machine"}


def _quantity(text, label):
    """'8 GB' -> ('mem', bytes); '3301' in an '(MHz)' row -> ('freq', MHz); '6 cores' -> ('num', 6). Else None."""
    m = _QTY.match(text)
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    unit = (m.group(2) or "").lower()
    lab = label.lower()
    if unit in _MEM:
        return "mem", value * _MEM[unit]
    if unit in _FREQ:
        return "freq", value * _FREQ[unit]
    if unit in ("", "cores", "core", "threads", "thread", "free"):
        if "bytes" in lab:
            return "mem", value
        if "mhz" in lab:
            return "freq", value
        return "num", value
    return None


def _fmt(dim, v):
    if dim == "mem":
        return f"{v / 1024 ** 3:.1f} GB" if v >= 1024 ** 3 else f"{v / 1024 ** 2:.0f} MB"
    if dim == "freq":
        return f"{v:.0f} MHz"
    return f"{v:g}"


def _os_family(text):
    t = text.lower()
    return {f for f, pat in (("windows", "windows"), ("macos", r"mac\s?os|macos|os x"), ("linux", "linux|ubuntu")) if re.search(pat, t)}


def _arch(text):
    t = text.lower()
    if re.search(r"arm64|aarch64", t):
        return "arm64"
    if re.search(r"amd64|x86[_-]?64|x64|64[- ]?bit", t):
        return "x64"
    if re.search(r"\bx86\b|i[3-6]86|32[- ]?bit", t):
        return "x86"
    return None


def _rule_check(label, need, has):
    """Exact verdict for rows where the answer is arithmetic or a plain fact.
    Returns (verdict, calc, info) or None to leave the row to the model. info=True: shown, but never blocks."""
    l, n, h = label.strip().lower(), need.strip(), has.strip()
    if _NONE_NEEDED.fullmatch(n):
        return "pass", "nothing required", False
    if _NONE_NEEDED.fullmatch(h):
        return "partial", "PC value unknown", False

    if l == "os version" and re.fullmatch(r"\d+(\.\d+)+", n) and re.fullmatch(r"\d+(\.\d+)+", h):
        nt, ht = tuple(map(int, n.split("."))), tuple(map(int, h.split(".")))
        return ("pass" if ht >= nt else "fail"), f"build {ht[-1]} vs {nt[-1]}", False

    if l in ("os", "os name", "operating system"):
        nf, hf = _os_family(n), _os_family(h)
        if nf and hf:
            if not hf & nf:
                return "fail", f"needs {'/'.join(sorted(nf))}", False
            nn, hn = re.findall(r"\d+", n), re.findall(r"\d+", h)
            if len(nf) == 1 and nn and hn:
                ok = int(hn[0]) >= min(int(x) for x in nn)
                return ("pass" if ok else "fail"), f"{hn[0]} vs {min(int(x) for x in nn)}", False
            return "pass", "same OS family", False

    if l in _ARCH_LABELS:
        na, ha = _arch(n), _arch(h)
        if na and ha:
            if na == ha or (na == "x86" and ha == "x64"):
                return "pass", f"{ha} meets {na}", False
            if na == "x64" and ha == "arm64":
                return "partial", "arm64 runs x64 by emulation", False
            return "fail", f"{ha} vs {na}", False

    if l in ("cpu", "processor") and _VAGUE_CPU.search(n) and not _SPECIFIC_CPU.search(n):
        return "pass", "no specific model needed", False

    qn, qh = _quantity(n, label), _quantity(h, label)
    if qn and qh and qn[0] == qh[0] and qn[1] > 0:
        dim, need_v, has_v = qn[0], qn[1], qh[1]
        calc = f"{_fmt(dim, has_v)} vs {_fmt(dim, need_v)}"
        ratio = has_v / need_v
        if l.startswith("available"):   # a live reading of what is free right now, never a hard fail
            return ("pass" if ratio >= 1 else "partial"), calc + " (live reading)", ratio < 1
        if _is_disk(label) or "free" in n.lower():   # no space, no install
            return ("pass" if ratio >= 1 else "fail"), calc, False
        verdict = "pass" if ratio >= 0.97 else "partial" if ratio >= 0.6 else "fail"
        return verdict, calc, False

    norm = lambda t: re.sub(r"[^a-z0-9]", "", t.lower())
    if norm(n) and norm(n) in norm(h):
        return "pass", "same part", False
    return None


def _is_gpu(label):
    return bool(re.fullmatch(r"gpu(\s*\d+)?(\s*name)?", label.strip(), re.I))


def _demote_spares(results):
    """Several drives or GPUs share ONE need. If one of them meets it, the others cannot block."""
    for same_kind in (_is_disk, _is_gpu):
        group = [r for r in results if same_kind(r["label"])]
        if any(r["verdict"] == "pass" for r in group):
            for r in group:
                if r["verdict"] == "fail":
                    r.update(verdict="partial", info=True, calc=(r["calc"] + " · another one passes")[:80])


def judge(software, rows, timeout=300):
    """Grade every row and decide if the software runs.
    Returns {software, results[{label, needs, has, verdict, calc, src, info}], counts, run, blockers, weak, model}.
    src is "rule" (exact check in code) or "model". info rows are shown but never block."""
    rows = _clean_rows(rows)
    if not rows:
        raise ValueError("There are no rows to judge.")
    name = _flat(software or "this software", 80)

    asked = {}   # (needs, has) -> "a" / "b" / "similar" / None; an identical pair is only asked once
    results = []
    for r in rows:
        row = {**r, "verdict": "partial", "calc": "not judged", "src": "model", "info": False}  # unanswered is never a pass
        rule = _rule_check(r["label"], r["needs"], r["has"])
        if rule:   # exact arithmetic beats the model, so the model is not even asked
            row.update(verdict=rule[0], calc=rule[1], info=rule[2], src="rule")
        else:
            key = (r["needs"], r["has"])
            if key not in asked:
                reply = _ollama_chat(_compare_prompt(r["needs"], r["has"]), 24, timeout)
                asked[key] = _read_comparison(reply, r["needs"], r["has"])
            # "a" = what the software needs is better, "b" = what this PC has is better
            outcome = {"b": ("pass", "PC part is stronger"),
                       "similar": ("pass", "PC part is about equal"),
                       "a": ("fail", "PC part is weaker")}.get(asked[key])
            if outcome:
                row.update(verdict=outcome[0], calc=outcome[1])
        results.append(row)
    _demote_spares(results)

    counts = {k: sum(1 for r in results if r["verdict"] == k) for k in VERDICTS}
    blockers = [r["label"] for r in results if r["verdict"] == "fail"]
    weak = [r["label"] for r in results if r["verdict"] == "partial" and not r["info"]]

    # The model no longer gives a separate "run" answer: any failed row blocks, otherwise it runs.
    run = not blockers
    return {"software": name, "results": results, "counts": counts, "run": run,
            "blockers": blockers, "weak": weak, "model": MODEL}


# ------------------------------------------------------- the old interactive chat
def ask_spec_checker(prompt, timeout=180):
    """Send one prompt to the local 'spec-checker' Ollama model and return its reply."""
    result = subprocess.run(
        ["ollama", "run", MODEL, prompt],
        capture_output=True,
        text=True,
        encoding="utf-8",  # Use UTF-8 instead of the Windows default cp1252
        errors="replace",  # Replace invalid chars instead of crashing
        timeout=timeout,
    )
    return result.stdout.strip()


def chat_with_ollama():
    """Run Ollama chat with conversation logging (Unicode fixed)"""
    log_file = f"spec_checker_chat_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    print(f"📝 Chat will be saved to: {log_file}")
    print("=" * 60)
    print("Type your messages. Press Ctrl+C to exit.\n")
    messages = []
    try:
        while True:
            user_input = input("You: ").strip()
            if not user_input:
                continue
            messages.append(f"You: {user_input}\n")
            response = ask_spec_checker(user_input)
            messages.append(f"spec-checker: {response}\n\n")
            print(f"spec-checker: {response}\n")
    except KeyboardInterrupt:
        print("\n\nSaving conversation...")
        with open(log_file, "w", encoding="utf-8") as f:
            f.writelines(messages)
        print(f"✅ Chat saved to: {log_file}")


def demo():
    rows = [
        {"label": "CPU", "needs": "Core i5-8400", "has": "Ryzen 5 5600H"},
        {"label": "GPU", "needs": "GTX 1060", "has": "Radeon Graphics"},
        {"label": "RAM", "needs": "16 GB", "has": "8 GB"},
        {"label": "Disk", "needs": "40 GB free", "has": "118 GB free"},
        {"label": "OS", "needs": "Windows 10", "has": "Windows 11"},
    ]
    out = judge("Demo Software", rows)
    for r in out["results"]:
        print(f"{r['label']:<10} {r['needs']:<14} {r['has']:<16} {r['verdict']:<8} ({r['calc']})")
    print("Will it run?", "YES" if out["run"] else "NO")


if __name__ == "__main__":
    demo() if "--demo" in sys.argv else chat_with_ollama()