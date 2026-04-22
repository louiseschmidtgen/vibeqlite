"""
cli/vibeqlite.py — vibeqlite REPL

Phase 10: readline-based REPL with pretty-printed output.
"""
from __future__ import annotations

import json
import os
from typing import Any

import httpx

try:
    import readline  # noqa: F401 — enables arrow keys / history on supported platforms
except ImportError:
    pass

_DEFAULT_URL = os.environ.get("VIBEQLITE_URL", "http://localhost:8001")
_DEFAULT_CONSISTENCY = os.environ.get("VIBEQLITE_CONSISTENCY", "yolo")


def _print_table(rows: list[dict], headers: list[str] | None = None) -> None:
    if not rows:
        print("(empty)")
        return
    cols = headers or list(rows[0].keys())
    widths = {c: max(len(c), max(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    sep = "+-" + "-+-".join("-" * widths[c] for c in cols) + "-+"
    print(sep)
    print("| " + " | ".join(c.ljust(widths[c]) for c in cols) + " |")
    print(sep)
    for r in rows:
        print("| " + " | ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols) + " |")
    print(sep)


def _print_result(data: dict) -> None:
    rows = data.get("rows")
    headers = data.get("headers")
    confidence = data.get("confidence")
    affected_rows = data.get("affected_rows")
    vibe_error = data.get("vibe_error")
    node_id = data.get("node_id", "?")
    vc = data.get("vector_clock", {})
    compaction_count = data.get("compaction_count", 0)

    if vibe_error:
        if isinstance(vibe_error, dict):
            print(f"\n\u26a1 VIBE_ERROR: {vibe_error.get('type', 'VIBE_ERROR')}")
            for k, v in vibe_error.items():
                if k != "type":
                    print(f"   {k}: {v}")
        else:
            print(f"\n\u26a1 VIBE_ERROR: {vibe_error}")

    if confidence == "low":
        print("\u26a0  X-Vibe-Confidence: low \u2014 verify with other nodes")

    if rows is not None:
        _print_table(rows, headers)
    elif affected_rows is not None:
        print(f"({affected_rows} row{'s' if affected_rows != 1 else ''} affected)")
    elif not vibe_error:
        print("OK")

    vc_str = json.dumps(vc, sort_keys=True) if vc else "{}"
    footer = f"  node={node_id}  clock={vc_str}  compactions={compaction_count}"
    if confidence:
        footer += f"  confidence={confidence}"
    print(f"\033[2m{footer}\033[0m")


def query(sql: str, url: str = _DEFAULT_URL, consistency: str = _DEFAULT_CONSISTENCY) -> dict:
    """Send a query to a vibeqlite node and return the response dict."""
    with httpx.Client(timeout=120.0) as client:
        resp = client.post(f"{url}/query", json={"sql": sql, "consistency": consistency})
        resp.raise_for_status()
        return resp.json()


def main() -> None:
    url = _DEFAULT_URL
    consistency = _DEFAULT_CONSISTENCY
    print(f"vibeqlite \u2014 connecting to {url}  (consistency: {consistency})")
    print("Type SQL or commands. 'quit' or Ctrl-D to exit.")
    print("  \\connect <url>       switch node")
    print("  \\consistency <mode>  yolo | vibe_check\n")

    while True:
        try:
            line = input("vql> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line.lower() in ("quit", "exit", "\\q"):
            break
        if line.lower().startswith("\\connect "):
            url = line.split(None, 1)[1].strip().rstrip("/")
            print(f"Connecting to {url}")
            continue
        if line.lower().startswith("\\consistency "):
            consistency = line.split(None, 1)[1].strip()
            print(f"Consistency: {consistency}")
            continue
        try:
            data = query(line, url=url, consistency=consistency)
            _print_result(data)
        except httpx.ConnectError:
            print(f"Error: cannot connect to {url}")
        except httpx.HTTPStatusError as exc:
            print(f"Error {exc.response.status_code}: {exc.response.text[:200]}")
        except Exception as exc:
            print(f"Error: {exc}")


if __name__ == "__main__":
    main()
