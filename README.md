# Self-Hosted Kubernetes Platform with AI-Augmented Incident Response

CSE 499 Senior Project · BYU-Idaho · Spring 2026

A three-node, high-availability Kubernetes platform running on owned bare metal,
with a full metrics-and-logs observability stack and a custom **SRE copilot** —
an LLM agent that receives Alertmanager webhooks, investigates using tool calls
against live cluster telemetry and Git history, and produces a root-cause
diagnosis with cited evidence.

Commercial AIOps tooling (Datadog Watchdog, PagerDuty AIOps, New Relic AI) solves
adjacent problems but is closed-source and priced for enterprises. Open-source
observability stacks exist but include no AI-augmented incident response layer.
The novel contribution here is that layer, built on an open foundation.

---

## Architecture

```
                  UniFi · dedicated Cluster VLAN
                               |
        +--------------+-------+-------+--------------+
        |              |                              |
   k3s-node1       k3s-node2                     k3s-node3
        +--------------+--------------+---------------+
                               |
                  kube-vip floating control-plane VIP
                               |
  +----------------------------+-----------------------------------+
  |  Platform    ArgoCD (GitOps) · MetalLB (L2) · Longhorn          |
  +----------------------------------------------------------------+
  |  Observability   Prometheus · Alertmanager · Loki · Alloy       |
  |                  · Grafana                                      |
  +----------------------------------------------------------------+
  |  Application     demo-api · loadgen · sre-copilot               |
  +----------------------------------------------------------------+

  Incident path:
    fault -> 500s -> Prometheus rule -> Alertmanager -> webhook
       -> copilot tool-use loop -> LLM diagnosis -> operator
```

## Hardware

| | |
|---|---|
| Nodes | 3x Lenovo ThinkCentre M720q (i5-8400T, 8GB DDR4, 256GB SSD) |
| Addressing | Static addresses on a private VLAN; floating control-plane VIP |
| LoadBalancer | MetalLB pool on the same private VLAN |
| OS | Ubuntu Server 24.04 LTS |
| Network | UniFi, dedicated Cluster VLAN with Zone-Based Firewall |

## Stack

| Component | Version | Role |
|---|---|---|
| k3s | v1.35.4+k3s1 | Kubernetes, embedded etcd, Flannel CNI |
| kube-vip | - | Floating control-plane VIP, leader election |
| MetalLB | v0.16.1 | Bare-metal L2 load balancing |
| Longhorn | v1.12.0 | Replicated block storage |
| ArgoCD | - | GitOps, app-of-apps pattern |
| kube-prometheus-stack | 87.15.1 | Prometheus, Alertmanager, Grafana |
| Loki | 17.2.0 (grafana-community) | Log aggregation, SingleBinary mode |
| Grafana Alloy | 1.6.0 | Log shipping (Promtail replacement) |
| SRE copilot | - | Python 3.12 stdlib only, Anthropic API |

ServiceLB and Traefik are disabled in k3s in favour of MetalLB.

## Repository layout

```
bootstrap/root-app.yaml          app-of-apps root Application
apps/                            one ArgoCD Application per component
manifests/
  demo-api/                      sample workload (code shipped as ConfigMap)
  observability/                 ServiceMonitor + PrometheusRule
  sre-copilot/                   copilot Deployment, Service, RBAC, source
faults/                          fault-injection scenario variants
scripts/faultctl.sh              fault-injection harness
```

Application code for `demo-api` and `sre-copilot` ships as a Kustomize
`configMapGenerator`. The generated ConfigMap name carries a content hash, so
editing the source triggers an automatic pod rollout through ArgoCD with no
container registry in the loop. That mechanism is what makes fault injection a
single Git push.

## The SRE copilot

`manifests/sre-copilot/copilot.py` — pure standard library, no dependencies.

On an Alertmanager webhook it runs a tool-use loop against the Anthropic API
with four tools:

| Tool | Source |
|---|---|
| `query_prometheus` | PromQL range queries |
| `query_loki` | LogQL queries |
| `get_pod_status` | Kubernetes API — phases, restarts, Warning events |
| `get_recent_commits` | GitHub API — commits **including full diffs** |

The model chooses which tools to call and in what order; nothing is pre-fetched.
In practice it issues metrics, pod-status and commit queries in parallel, then a
targeted log query two seconds later — forming a hypothesis, then seeking
confirmation.

**Diagnosis only.** The copilot never remediates. That is enforced structurally
rather than behaviourally: its ServiceAccount has `get`/`list` on `pods` and
`events` and nothing else (`manifests/sre-copilot/rbac.yaml`). Even a
hallucinated intent to act has no permission to.

The Anthropic API key lives in a manually created Kubernetes Secret and is never
committed:

```
kubectl -n sre-copilot create secret generic anthropic --from-literal=api-key=YOUR_KEY
```

### Sample output

For an injected `KeyError` fault, the copilot pulled the commit diff, observed
that `random.choice` offered three pricing tiers while the lookup dict defined
two, computed the resulting one-in-three failure probability, and matched it
against the 32.77% error rate Prometheus independently measured — correlating
two independent sources rather than paraphrasing a traceback.

## Fault injection

```
./scripts/faultctl.sh list
./scripts/faultctl.sh inject broken-commit
./scripts/faultctl.sh heal
./scripts/faultctl.sh status
```

| Scenario | Fault | Alert |
|---|---|---|
| `broken-commit` | Unhandled `KeyError` in `/work`; pods stay up emitting 500s | `DemoApiHighErrorRate` |
| `crashloop` | Missing module at import; pods crash and restart | `DemoApiCrashLooping` |
| `latency` | 3s delay in the request path; no errors | `DemoApiHighLatency` |

`inject` commits the variant, pushes, forces an ArgoCD refresh, waits for
rollout, and polls Alertmanager until the alert fires — printing a timeline:

```
   1s  pushed a broken commit to origin
  18s  ArgoCD reported Synced + Healthy at that revision
  18s  new pods running with faulty code
 121s  DemoApiHighErrorRate FIRING — webhook delivered to sre-copilot
```

`demo-api` deliberately has **no liveness probe**, so faulty pods keep serving
and emitting logs rather than crashlooping — a harder and more realistic
diagnostic problem than a dead service.

## Requirements to evidence

| Requirement | Where |
|---|---|
| API reachable with one node off | 2-of-3 etcd quorum; verified by node power-off |
| Control plane behind a VIP | kube-vip; `kubectl config view --minify` |
| VIP moves on node failure | `kube-system` lease `holderIdentity` changes |
| Deploy from Git via ArgoCD | `bootstrap/root-app.yaml`, `apps/` |
| Reconcile and report drift | out-of-band `kubectl scale` reverted; ArgoCD diff view |
| MetalLB pool IPs | `kubectl get svc -A` shows LoadBalancer addresses |
| Replicated Longhorn volumes | Longhorn UI, replicas across nodes |
| Data intact after node loss | marker file written before kill, read after |
| Prometheus metrics | `manifests/observability/servicemonitor-demo-api.yaml` |
| Grafana dashboards | Grafana on a MetalLB address |
| Loki logs queryable | Alloy to Loki; Grafana Explore |
| Alertmanager fires on faults | `manifests/observability/prometheusrule-demo-api.yaml` |
| Alerts delivered by webhook | Alertmanager receiver to copilot Service |
| Correlated context gathered | four tool calls per investigation |
| LLM root-cause diagnosis | copilot UI |
| Diagnosis presented with evidence | structured EVIDENCE section, cites SHAs and metrics |

**Stretch:** scripted fault injection and synthetic load are implemented.
Distributed tracing and confirmation-gated remediation were cut in the order the
project proposal specified; a local-model (Ollama) path was not attempted, as
8GB nodes leave no headroom. No must-have requirement was affected.

## Notes from the build

- **UniFi Zone-Based Firewall blocks intra-zone traffic by default**, which
  breaks etcd Raft consensus. Return traffic also needs an explicit
  established/related allow rule ordered *above* the block.
- **Longhorn prerequisites are non-negotiable.** `multipathd` must be disabled
  and masked on every node before install, alongside `open-iscsi`, `nfs-common`,
  and persistent kernel modules. Use `longhornctl` for preflight.
- **k3s runs the controller-manager, scheduler, proxy and etcd in-process**, so
  those kube-prometheus-stack scrape targets must be disabled. ServerSideApply is
  required to avoid CRD annotation size limits.
- **Promtail is EOL** — use Grafana Alloy.
- **The `grafana/loki` chart is GEL-only** — use `grafana-community/loki` for
  open-source SingleBinary mode.
- **After installing Longhorn**, remove the default-StorageClass annotation from
  `local-path` or provisioning becomes ambiguous.
- **kube-vip leader election** doesn't settle until all nodes have been rebooted
  after initial deployment.
- **`set -euo pipefail` bit three times** in `faultctl.sh`, each on a different
  path: a filtered `grep` returning nothing propagated failure through
  `pipefail`; a `[[ test ]] && { }` as a loop's last statement aborted on false;
  and an undeclared variable tripped `set -u`. Exit codes are part of a script's
  interface, and the empty case is a case.

## Security scope

Single-operator platform, no public-internet exposure, no end-user accounts.
Grafana, Prometheus and Alertmanager are exposed unauthenticated on an isolated
private VLAN — acceptable for this scope, not for production. The only data
leaving the network is the copilot's request to the Anthropic API over TLS, which
may include short metric and log excerpts. Prompt-size limits and a monthly spend
cap bound both cost and egress volume.

## References

Kubernetes · k3s · kube-vip · MetalLB · Longhorn · ArgoCD ·
kube-prometheus-stack · Prometheus · Grafana · Loki · Anthropic API docs · Raft
