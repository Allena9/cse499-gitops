"""
SRE Copilot — AI-augmented incident response.

Receives Alertmanager webhooks, gives Claude a set of context-gathering tools,
and lets it reason its way to a root-cause diagnosis.
"""

import datetime
import html
import http.server
import json
import logging
import os
import socketserver
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s copilot %(message)s",
)
log = logging.getLogger("copilot")

PROMETHEUS = os.environ.get("PROMETHEUS_URL", "http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090")
LOKI = os.environ.get("LOKI_URL", "http://loki.monitoring.svc.cluster.local:3100")
GITHUB_REPO = os.environ["GITHUB_REPO"]           # e.g. "Allena9/cse499-gitops"
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
MODEL = os.environ.get("MODEL", "claude-sonnet-5")

K8S_API = "https://kubernetes.default.svc"
K8S_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
K8S_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"

_diagnoses = []          # newest first
_diagnoses_lock = threading.Lock()


# ---------------------------------------------------------------- http helper

def _get_json(url, headers=None, context=None, timeout=20):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, context=context, timeout=timeout) as r:
        return json.loads(r.read().decode())


# ------------------------------------------------------------------ the tools

def query_prometheus(query="sum(rate(demo_api_errors_total[2m]))", minutes=15):
    """Range query against Prometheus. Returns downsampled series."""
    end = time.time()
    start = end - (minutes * 60)
    url = f"{PROMETHEUS}/api/v1/query_range?" + urllib.parse.urlencode({
        "query": query, "start": start, "end": end, "step": "30s",
    })
    data = _get_json(url)
    if data.get("status") != "success":
        return f"Prometheus error: {data.get('error', 'unknown')}"

    out = []
    for series in data["data"]["result"][:5]:
        labels = series.get("metric", {})
        name = ", ".join(f"{k}={v}" for k, v in labels.items()) or "(no labels)"
        pts = series.get("values", [])[-20:]
        rendered = " ".join(
            f"{datetime.datetime.utcfromtimestamp(float(t)).strftime('%H:%M:%S')}={float(v):.4g}"
            for t, v in pts
        )
        out.append(f"{name}\n  {rendered}")
    return "\n".join(out) if out else "No data returned for that query."


def query_loki(logql='{namespace="demo", app="demo-api"}', minutes=15, limit=40):
    """LogQL query against Loki. Returns most recent matching lines."""
    end_ns = int(time.time() * 1e9)
    start_ns = end_ns - int(minutes * 60 * 1e9)
    url = f"{LOKI}/loki/api/v1/query_range?" + urllib.parse.urlencode({
        "query": logql, "start": start_ns, "end": end_ns,
        "limit": limit, "direction": "backward",
    })
    data = _get_json(url)
    if data.get("status") != "success":
        return f"Loki error: {data}"

    lines = []
    for stream in data["data"]["result"]:
        for ts, line in stream.get("values", []):
            when = datetime.datetime.utcfromtimestamp(int(ts) / 1e9).strftime("%H:%M:%S")
            lines.append((ts, f"{when} {line.strip()}"))
    lines.sort(key=lambda x: x[0])
    body = "\n".join(l for _, l in lines[-limit:])
    return body or "No log lines matched."


def get_pod_status(namespace="demo"):
    """Pod phases, restart counts, and recent Warning events from the K8s API."""
    with open(K8S_TOKEN_PATH) as f:
        token = f.read().strip()
    ctx = ssl.create_default_context(cafile=K8S_CA_PATH)
    hdrs = {"Authorization": f"Bearer {token}"}

    pods = _get_json(f"{K8S_API}/api/v1/namespaces/{namespace}/pods", hdrs, ctx)
    out = ["PODS:"]
    for p in pods.get("items", []):
        name = p["metadata"]["name"]
        phase = p["status"].get("phase", "?")
        statuses = p["status"].get("containerStatuses") or []
        restarts = sum(c.get("restartCount", 0) for c in statuses)
        ready = all(c.get("ready") for c in statuses) if statuses else False
        image = (p["spec"]["containers"][0] or {}).get("image", "?")
        out.append(f"  {name}  phase={phase} ready={ready} restarts={restarts} image={image}")

    events = _get_json(
        f"{K8S_API}/api/v1/namespaces/{namespace}/events?fieldSelector=type!=Normal", hdrs, ctx
    )
    out.append("RECENT WARNING EVENTS:")
    items = events.get("items", [])[-10:]
    if not items:
        out.append("  (none)")
    for e in items:
        out.append(f"  {e.get('reason')}: {e.get('message', '')[:160]}")
    return "\n".join(out)


def get_recent_commits(limit=3):
    """Recent commits to the GitOps repo, including the actual code diffs."""
    hdrs = {"User-Agent": "sre-copilot", "Accept": "application/vnd.github+json"}
    commits = _get_json(
        f"https://api.github.com/repos/{GITHUB_REPO}/commits?per_page={limit}", hdrs
    )

    out = []
    for c in commits:
        sha = c["sha"]
        msg = c["commit"]["message"].splitlines()[0]
        when = c["commit"]["author"]["date"]
        detail = _get_json(f"https://api.github.com/repos/{GITHUB_REPO}/commits/{sha}", hdrs)

        out.append(f"=== {sha[:8]}  {when}  {msg}")
        for f in detail.get("files", []):
            patch = f.get("patch")
            if patch:
                out.append(f"--- {f['filename']} (+{f['additions']}/-{f['deletions']})")
                out.append(patch[:2000])
    return "\n".join(out) if out else "No commits found."


TOOLS = [
    {
        "name": "query_prometheus",
        "description": (
            "Run a PromQL range query. Use this to see how a metric behaved over time — "
            "error rates, request rates, resource usage. Useful metrics in this cluster include "
            "demo_api_requests_total and demo_api_errors_total (both counters, so wrap in rate())."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "PromQL expression"},
                "minutes": {"type": "integer", "description": "Lookback window in minutes (default 15)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "query_loki",
        "description": (
            "Run a LogQL query against Loki to read application logs. "
            "Streams are labelled by namespace, app, pod, and container. "
            'Example: {namespace="demo", app="demo-api"} |= "ERROR"'
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "logql": {"type": "string", "description": "LogQL expression"},
                "minutes": {"type": "integer", "description": "Lookback window in minutes (default 15)"},
            },
            "required": ["logql"],
        },
    },
    {
        "name": "get_pod_status",
        "description": "Get pod phases, restart counts, images, and recent Warning events for a namespace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Namespace (default 'demo')"},
            },
        },
    },
    {
        "name": "get_recent_commits",
        "description": (
            "Get the most recent commits to the GitOps repository that deploys these workloads, "
            "including full code diffs. ArgoCD deploys directly from this repo, so a recent commit "
            "is a strong candidate cause for a new failure."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "How many commits to fetch (default 3)"},
            },
        },
    },
]

DISPATCH = {
    "query_prometheus": query_prometheus,
    "query_loki": query_loki,
    "get_pod_status": get_pod_status,
    "get_recent_commits": get_recent_commits,
}

SYSTEM = """You are an SRE copilot for a self-hosted Kubernetes platform.

An alert has fired. Investigate it and produce a root-cause diagnosis.

You have tools to query metrics (Prometheus), logs (Loki), live cluster state (Kubernetes API),
and the Git history of the GitOps repo that ArgoCD deploys from. Workloads are deployed straight
from Git, so a recent commit is often the cause of a sudden regression — correlate the time the
failure began against commit timestamps and read the actual diffs.

Investigate before concluding. Call the tools you need; several, if warranted. Do not speculate
about things you could simply check.

When you have enough evidence, respond with a final diagnosis in exactly this format:

ROOT CAUSE: <one or two sentences>
EVIDENCE:
- <specific finding, citing the metric value, log line, or commit SHA it came from>
- <...>
CONFIDENCE: <high|medium|low>
SUGGESTED FIX: <what a human should do; be specific>

You diagnose only. You do not take remediation actions."""


# --------------------------------------------------------------- the LLM loop

def call_claude(messages):
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 2000,
        "system": SYSTEM,
        "tools": TOOLS,
        "messages": messages,
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "content-type": "application/json",
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode())


def diagnose(alert_text):
    messages = [{"role": "user", "content": alert_text}]
    trace = []

    for turn in range(8):
        resp = call_claude(messages)
        messages.append({"role": "assistant", "content": resp["content"]})

        if resp.get("stop_reason") != "tool_use":
            final = "".join(b["text"] for b in resp["content"] if b["type"] == "text")
            return final, trace

        results = []
        for block in resp["content"]:
            if block["type"] != "tool_use":
                continue
            name, args = block["name"], block["input"]
            log.info(f"tool_call name={name} args={json.dumps(args)}")
            try:
                output = DISPATCH[name](**args)
            except Exception as e:
                output = f"Tool error: {type(e).__name__}: {e}"
                log.exception(f"tool_error name={name}")
            trace.append({"tool": name, "args": args, "result": output})
            results.append({
                "type": "tool_result",
                "tool_use_id": block["id"],
                "content": output[:8000],
            })
        messages.append({"role": "user", "content": results})

    return "Investigation did not converge within the turn limit.", trace


# -------------------------------------------------------------------- web tier

def format_alert(payload):
    lines = []
    for a in payload.get("alerts", []):
        if a.get("status") != "firing":
            continue
        labels = a.get("labels", {})
        anns = a.get("annotations", {})
        lines.append(
            f"ALERT: {labels.get('alertname')}\n"
            f"Severity: {labels.get('severity')}\n"
            f"Service: {labels.get('service')}  Namespace: {labels.get('namespace')}\n"
            f"Started at: {a.get('startsAt')}\n"
            f"Summary: {anns.get('summary')}\n"
            f"Description: {anns.get('description')}"
        )
    return "\n\n".join(lines)


def handle_alert(payload):
    alert_text = format_alert(payload)
    if not alert_text:
        log.info("webhook received with no firing alerts; ignoring")
        return

    log.info("alert received, starting investigation")
    started = time.time()
    try:
        diagnosis, trace = diagnose(alert_text)
    except Exception as e:
        log.exception("investigation failed")
        diagnosis, trace = f"Investigation failed: {type(e).__name__}: {e}", []

    elapsed = time.time() - started
    log.info(f"investigation complete in {elapsed:.1f}s, {len(trace)} tool calls")

    with _diagnoses_lock:
        _diagnoses.insert(0, {
            "at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "alert": alert_text,
            "diagnosis": diagnosis,
            "trace": trace,
            "elapsed": f"{elapsed:.1f}s",
        })
        del _diagnoses[20:]


PAGE = """<!doctype html><meta charset=utf-8><title>SRE Copilot</title>
<style>
 body{{background:#0d1117;color:#c9d1d9;font:14px ui-monospace,SFMono-Regular,Menlo,monospace;
      margin:0;padding:2rem;line-height:1.6}}
 h1{{color:#58a6ff;font-size:1.4rem;margin:0 0 .3rem}}
 .sub{{color:#8b949e;margin-bottom:2rem}}
 .card{{border:1px solid #30363d;border-radius:8px;margin-bottom:1.5rem;overflow:hidden}}
 .hd{{background:#161b22;padding:.7rem 1rem;color:#8b949e;
      display:flex;justify-content:space-between;border-bottom:1px solid #30363d}}
 .alert{{padding:1rem;background:#1c1917;color:#f85149;white-space:pre-wrap;
         border-bottom:1px solid #30363d}}
 .diag{{padding:1rem;white-space:pre-wrap}}
 details{{border-top:1px solid #30363d;padding:.7rem 1rem;color:#8b949e}}
 pre{{background:#161b22;padding:.7rem;overflow-x:auto;border-radius:4px;color:#8b949e}}
 .empty{{color:#8b949e;border:1px dashed #30363d;padding:2rem;text-align:center;border-radius:8px}}
</style>
<h1>SRE Copilot</h1>
<div class=sub>AI-augmented incident response &middot; diagnosis only, no autonomous remediation</div>
{body}
"""


def render():
    with _diagnoses_lock:
        items = list(_diagnoses)

    if not items:
        return PAGE.format(body="<div class=empty>No incidents yet. Waiting for alerts.</div>")

    cards = []
    for d in items:
        tools = "".join(
            f"<details><summary>{html.escape(t['tool'])}({html.escape(json.dumps(t['args']))})</summary>"
            f"<pre>{html.escape(t['result'][:3000])}</pre></details>"
            for t in d["trace"]
        )
        cards.append(
            f"<div class=card>"
            f"<div class=hd><span>{html.escape(d['at'])}</span>"
            f"<span>{len(d['trace'])} tool calls &middot; {d['elapsed']}</span></div>"
            f"<div class=alert>{html.escape(d['alert'])}</div>"
            f"<div class=diag>{html.escape(d['diagnosis'])}</div>"
            f"{tools}</div>"
        )
    return PAGE.format(body="".join(cards))


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _respond(self, code, body, ctype="text/plain"):
        payload = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/healthz":
            self._respond(200, b"ok")
        elif self.path.startswith("/debug/context"):
            # Exercise every tool without invoking the LLM. For verifying plumbing.
            parts = []
            for name, fn in DISPATCH.items():
                try:
                    parts.append(f"===== {name} =====\n{fn()}")
                except Exception as e:
                    parts.append(f"===== {name} =====\nFAILED: {type(e).__name__}: {e}")
            self._respond(200, "\n\n".join(parts))
        else:
            self._respond(200, render(), "text/html; charset=utf-8")

    def do_POST(self):
        if self.path != "/alert":
            self._respond(404, b"not found")
            return
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        self._respond(200, b"accepted")
        threading.Thread(target=handle_alert, args=(payload,), daemon=True).start()

    def log_message(self, fmt, *args):
        return


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    # query_prometheus needs a default for /debug/context to call it bare
    log.info(f"sre-copilot starting model={MODEL} repo={GITHUB_REPO}")
    Server(("0.0.0.0", 8080), Handler).serve_forever()
