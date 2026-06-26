#!/usr/bin/env python3
"""Bridge between highlights.py --smart-rank and the Cline CLI.

highlights.py's --llm-command mode pipes the judge prompt to this script's STDIN.
We forward it to the Cline CLI (default model: DeepSeek V4 Flash, which is free via
Cline, hosted, and doesn't touch your GPU) and print the model's final reply (the
JSON score array) to STDOUT, which highlights.py then parses.

cline takes the prompt as an ARGUMENT (not stdin), and on Windows a multi-line prompt
full of quotes/`%`/`&` would be mangled by cmd.exe — so we invoke cline's Node entry
directly (node passes argv verbatim, no shell re-parsing).

Usage from highlights.py:
    --smart-rank --llm-command "python cline_judge.py"
Optional env: CLINE_JUDGE_MODEL (default deepseek/deepseek-v4-flash),
              CLINE_JUDGE_TIMEOUT (seconds, default 180).
"""
import json
import os
import shutil
import subprocess
import sys

MODEL = os.environ.get("CLINE_JUDGE_MODEL", "deepseek/deepseek-v4-flash")
TIMEOUT = float(os.environ.get("CLINE_JUDGE_TIMEOUT", "180"))
SYSTEM = ("You are a strict JSON-only rater. Output only the requested JSON array. "
          "Never use tools, never ask questions, never add prose.")


def find_cline_js() -> str | None:
    """Locate cline's Node entry (the npm shim runs: node <prefix>/node_modules/cline/bin/cline)."""
    candidates = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(os.path.join(appdata, "npm", "node_modules", "cline", "bin", "cline"))
    npm_prefix = os.environ.get("NPM_PREFIX")
    if npm_prefix:
        candidates.append(os.path.join(npm_prefix, "node_modules", "cline", "bin", "cline"))
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def run_cline(prompt: str) -> str:
    js = find_cline_js()
    common = ["--json", "--thinking", "none", "--auto-approve", "true",
              "-P", "cline", "-m", MODEL, "-s", SYSTEM, prompt]
    if js and shutil.which("node"):
        cmd = ["node", js] + common          # cleanest: no shell, argv passed verbatim
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
    else:                                    # fallback: the cline launcher on PATH
        proc = subprocess.run(["cline"] + common, capture_output=True, text=True,
                              timeout=TIMEOUT, shell=False)
    return proc.stdout


def extract_final_text(stdout: str) -> str:
    """Pull the model's final assistant text out of cline's --json event stream."""
    final = ""
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") == "run_result" and isinstance(obj.get("text"), str):
            final = obj["text"]
        ev = obj.get("event") or {}
        if ev.get("type") == "done" and isinstance(ev.get("text"), str):
            final = ev["text"]
    return final


def main() -> None:
    prompt = sys.stdin.read()
    try:
        out = run_cline(prompt)
    except Exception as e:
        sys.stderr.write(f"cline_judge error: {e}\n")
        sys.exit(1)
    sys.stdout.write(extract_final_text(out))


if __name__ == "__main__":
    main()
