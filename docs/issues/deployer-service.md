# Deployer service and the autonomy ladder — design review

Status: **PROPOSAL** · Track: **S** (deploy plane, S-17/S-18) · Related:
[agent-trust-boundary.md](agent-trust-boundary.md) (S-17, S-18, Path I, D6, D7, Phase 1.5)

This note reviews the "submit artifact + request deploy" proposal and turns it into a
concrete design for *this* repository. It is deliberately a contract, not code.

The proposal was written against `server-mcp`'s `docs/modernization-plan.md` §3.8 Phase 4.
That document does not exist here; the equivalent material in this repo is S-17 and Phase 1.5
of the trust-boundary track. Read this note as an amendment to Phase 1.5, not a replacement.

---

## 1. What the proposal actually says

Stripped of the framing, it makes five claims:

1. **Move the boundary from "which commands" to "which artifact is approved."** The agent
   never gets a shell or a build; it submits an artifact and a deploy request, and a separate
   deployer decides whether to apply it.
2. **Formalise autonomy as a ladder** (L0 inspect → L1 propose → L2 supervised apply → L3
   bounded autonomous loop), configured server-side per service.
3. **Address deploys by artifact** — a commit SHA or image digest, never a working tree, tag
   or path supplied by the model. Reject dirty worktrees.
4. **Approval is an out-of-band capability** — a short-lived token minted by a human CLI or
   CI, bound to `(service, artifact, expiry)`. The model can request it, not create it.
5. **The loop is only as good as the evidence contract.** Define per-service success signals
   before allowing autonomy, or the agent declares victory on a green-but-broken container.

The honest-limits section is the most important part: Docker is not a VM, "fixed" is the hard
part, and the agent must not build the image the deployer trusts.

---

## 2. Where it agrees with what this repo already decided

| Proposal | Existing decision | Verdict |
|---|---|---|
| Deployer is a separate service, not an MCP tool | S-17, D7, Phase 1.5 | **Already agreed.** Keep the MCP a read/diagnose surface. |
| Agent sends a request, never a secret | S-17, D7 | **Already agreed.** The deployer owns the vault, become password and SSH key. |
| Artifact-addressed, reject dirty worktrees | Phase 1.5 "copy the tree, record its hash" | **Same intent, weaker wording.** Phase 1.5 anchors a tree copy; the proposal anchors a commit. The commit is better. |
| Approval token for promotion | D5/D6, S-12 | **Partially agreed.** D6 says *not* per-call approval for allowlisted tags; a token is only for human-gated tags. |
| Evidence contract | Phase 1.5 health gate | **Agreed, under-specified today.** Phase 1.5 says "health gate"; it does not define per-service success. |
| Bounded loop with budget + rollback | D6, Phase 1.5 deploy budget | **Already agreed.** The proposal's L3 is D6 with a name. |
| Staging target | Not in Phase 1.5 | **New and useful** for container services. |
| Per-service autonomy policy | Implicit in the tag allowlist | **New and useful.** Make it explicit config. |

So roughly 60 % of the proposal is already the plan. The genuinely new pieces are: commit
addressing, the change-request object, the evidence contract, staging, and the explicit
autonomy policy.

---

## 3. Where it does **not** fit this repo — the important part

### 3.1 The artifact here is an Ansible tree, not a container image

The proposal is written for a container build: commit → image → digest → deploy. This repo's
primary task class is **host-level Ansible**: systemd units, `/etc`, mounts, sudoers, cron.
There is no image to build and no digest to promote. The deployable artifact is a **commit of
this repository plus a tag set**.

That changes the security claim. The proposal says the artifact boundary is "safe by
construction." For a container image that is roughly true: the deployer runs a fixed
`docker compose up` against a digest. For an Ansible tree it is **not** true. A role is
Turing-complete root code; any task may write `/etc/sudoers.d`, change `authorized_keys` or
mount over a disk. This is exactly **Path I** in the trust-boundary track.

> **Review conclusion:** commit addressing buys *integrity and forensics* (the deployer runs
> exactly the reviewed commit, and the audit says which one). It does **not** buy *authority
> reduction*. The authority control remains the tag allowlist + snapshot + health gate +
> rollback + budget (D6). Do not let the "safe by construction" framing justify dropping the
> envelope.

### 3.2 "The deployer decides" is not a new authority boundary

The proposal implies the deployer is the thing that decides. In this repo the deployer is
root-by-proxy: it holds the vault and runs Ansible as root. Moving the decision into it does
not reduce what a hostile role can do; it only moves *who* triggers it. The decision that
matters is the **tag allowlist**, and that must stay a static, human-owned file the agent
cannot write.

### 3.3 L2 "supervised apply" is weak for host-level roles

Staging works for a container service: run the compose project on a scratch host, probe it,
promote. It does **not** work for `mounts`, `mergerfs`, `snapraid`, `sshd` or `system`: there
is no disposable copy of the host's `/etc` and disk layout. For those, L2 collapses to
"check mode + human review", which is L1. Be explicit: **L2/L3 are for container-service
tags only.** Host-level tags stay at L0/L1 and human-gated.

### 3.4 The proposal's L3 is D6, not a new level

D6 already says: allowlisted tag, unattended, bounded by snapshot + health gate + rollback +
budget + audit. That *is* L3. The only thing the proposal adds is the name and the
per-service policy file. Adopt the name, keep D6's controls.

---

## 4. Concrete design for this repo

### 4.1 Shape

```
agent sandbox (untrusted)          deployer (trusted, outside sandbox)        catafalco
  no ansible, no vault,   ──────►  owns vault key, become password, SSH key
  no SSH key                       fixed playbook list, tag allowlist
                                   ansible-runner, per-run data dir
  deploy(service, commit,  ◄──────  redacted events, rc, snapshot id
         tags, check)              health gate → auto-rollback
  deploy_status(run_id)    ◄──────
  deploy_events(run_id)    ◄──────
```

First step: a `deploy` service account on the laptop, reachable over HTTP from the sandbox
(same shape as the MCP in Option A). End state: move it to `catafalco` in Phase 2, so no
laptop process owns a server credential.

### 4.2 API

Keep it small and non-extensible. No shell, no argv passthrough, no playbook path, no
`-e`/`--extra-vars`, no connection/inventory overrides, no `--ask-pass`, no
`--vault-password-file`, no `diff=true` from the sandbox.

| Method | Path | Input | Output |
|---|---|---|---|
| `POST` | `/v1/deploys` | `{service, commit, tags[], check, request_id}` | `{run_id}` |
| `GET` | `/v1/deploys/{run_id}` | — | `{state, rc, failed_tasks, snapshot_id, rollback}` |
| `GET` | `/v1/deploys/{run_id}/events?offset=` | — | redacted per-task events |
| `POST` | `/v1/deploys/{run_id}/cancel` | — | cancellation result |
| `POST` | `/v1/deploys/{run_id}/approve` | `{token}` | promotion result (human-gated tags only) |
| `POST` | `/v1/services/{service}/rollback` | `{to_commit}` | rollback result |
| `GET` | `/v1/services` | — | policy: autonomy, tags, evidence, budget |

`commit` is a full 40-char SHA. The deployer fetches it into a read-only per-run directory
and records the tree hash. It never reads the agent's working tree. A dirty or unknown commit
is refused.

### 4.3 The change-request object

The agent fills this in; it is both the approval input and the audit record:

```yaml
request_id: 01J...
service: grafana
commit: 3f2a...            # full SHA
tags: [grafana]
hypothesis: "Dashboard query uses InfluxQL; switch to SQL and ORDER BY time."
expected_evidence:
  - "container grafana healthy for 120s"
  - "GET /api/health returns 200"
  - "no 'invalid function' in grafana logs for 5m"
rollback_plan: "redeploy previous commit; restore snapshot <id>"
```

A human approving a human-gated tag approves *this object*, not a command line.

### 4.4 Evidence contract

Per service, in the policy file. Reuse the checks the MCP already exposes rather than
inventing a second surface:

```yaml
deploy:
  services:
    grafana:
      autonomy: supervised          # inspect | propose | supervised | auto
      tags: [grafana]
      evidence:
        - compose_ps: grafana
        - container_health: grafana
        - http: {url: "http://127.0.0.1:3000/api/health", expect: 200}
        - logs_absent: {service: grafana, pattern: "invalid function", window: 5m}
      budget: {runs_per_hour: 4, max_consecutive_failures: 2, max_wall_clock: 15m}
      rollback: {snapshot: restic, health_gate: true}
    system:
      autonomy: propose
      human_gated: true
```

Rules:

- `autonomy: auto` is refused unless every evidence check is defined and has passed on the
  last known-good deploy.
- The health gate is the **only** definition of success. A green container that fails an
  evidence check is a failure and triggers rollback.
- `human_gated: true` tags (`bootstrap`, `system`, `security`, `sshd`, `mounts`, `mergerfs`,
  `snapraid`, `hd_idle`, anything writing `/etc/sudoers.d`) can never be `auto`.

### 4.5 Approval token

Only for human-gated tags. Minted by a human CLI, not by the agent:

```bash
deployctl approve --service system --commit 3f2a... --ttl 15m
```

The token is signed by a key the deployer holds, bound to `(service, commit, expiry, nonce)`,
single-use, and verified server-side. The agent can *request* approval (the change-request
object); it cannot mint the token. This is the one place the proposal's "out-of-band
capability" is strictly better than D6's after-the-fact notification, and it should be
adopted for the human-gated set.

### 4.6 Staging

For container-service tags only: a scratch compose project (or a disposable VM) where the
agent can iterate freely. Promotion to prod needs the evidence contract to pass on staging
plus, for human-gated tags, a token. This is what makes L2 real. Do not pretend host-level
roles have a staging target.

### 4.7 Autonomy ladder, mapped

| Level | This repo | Control |
|---|---|---|
| L0 inspect | today's MCP read + restart | token, rate limit |
| L1 propose | agent produces a commit + change request; human merges | human review |
| L2 supervised | container-service tags on staging; evidence must pass; token to promote | staging + evidence + token |
| L3 bounded loop | allowlisted container-service tags, `autonomy: auto` | D6 envelope: snapshot, health gate, rollback, budget, audit |

Host-level tags never exceed L1.

---

## 5. How to build it, in order

1. **Deployer skeleton (Phase 1.5).** `deploy` service account, `ansible-runner` as a
   systemd service, commit-addressed per-run read-only tree, tag allowlist, redacted
   structured events, append-only audit, budget, pre-deploy snapshot, health gate,
   auto-rollback. This is already Phase 1.5; the only change is **commit instead of tree
   copy**.
2. **Change-request object + evidence contract.** Add the policy file and make the health
   gate read it. No new authority, just a definition of "fixed".
3. **Approval token** for the human-gated tag set. Replace the blocking prompt with a
   short-lived signed intent.
4. **Staging target** for container services. This unlocks L2.
5. **L3** for services explicitly marked `auto`, with the D6 envelope. Do not enable it
   before step 2 is proven.

Steps 1–2 are the minimum that makes a sandboxed agent useful. Steps 3–5 are refinements.

---

## 6. Honest limits

- **Ansible is root code.** A hostile role compromises the deployer and the host. The
  envelope (allowlist, snapshot, rollback, budget) bounds damage; it does not prevent it.
  This is Path I and Appendix E, accepted deliberately.
- **"Fixed" is the hard part.** Autonomous loops fail on ambiguous success criteria, not on
  deploy mechanics. Invest in the evidence contract before L3.
- **Staging does not exist for host-level roles.** L2 is container-only.
- **The deploy log is a secret channel.** Redaction (S-18) reduces it; a missing `no_log` on
  a role that templates a `.env` is a secret read until the role is fixed.
- **The agent's workspace is the deploy input.** Commit addressing gives forensics, not
  prevention: a writable sandbox can still write what the human commits next (S-19).
- **Docker is not a VM.** If the artifact itself is hostile, the deployer is compromised.
  For truly untrusted code, use a separate host.

---

## 7. Acceptance criteria

- [ ] From inside the sandbox, `ansible-playbook` and `ansible-vault` are absent and the
      vault is unreadable, yet `deploy → deploy_events → fix → deploy` completes unattended.
- [ ] The deployer refuses a dirty tree, a short/tag ref, a playbook path, `-e`, connection
      overrides, `--ask-pass`, `--vault-password-file` and `diff=true`.
- [ ] Every run is anchored to a fetched commit; the tree hash, tag set, `rc`, snapshot id
      and rollback outcome are in the append-only audit.
- [ ] A deliberate failure triggers the health gate and an automatic rollback.
- [ ] A leaking test task returns `***` through `deploy_events`.
- [ ] `autonomy: auto` is refused for a service with an incomplete evidence contract, and
      for every human-gated tag.
- [ ] A human-gated deploy without a valid, unexpired, single-use token is refused.

---

## 8. Bottom line

The proposal is directionally right and mostly already the plan. Adopt from it: **commit
addressing**, the **change-request object**, the **evidence contract**, **staging for
container services**, and the **explicit per-service autonomy policy**. Reject the framing
that the artifact boundary is "safe by construction" for Ansible — it is safe by *envelope*,
and the envelope is D6. Keep the MCP a read/diagnose surface; put the deployer in its own
process with its own credentials; never let the agent hold the vault or mint its own approval.

---

## 9. Addendum — two pipelines, and the "atomic bomb" question

This section answers the harder objection: *Docker is root, Ansible is root, so how can any
boundary hold against a compromised agent that authors a delayed payload?* The answer is that
there is no single boundary. There are **two pipelines with different trust levels**, and the
invariant is not "the artifact is safe" but **"the agent's code never executes on the
production host."**

### 9.1 The split is per task, not per role

Most roles here do both kinds of work. `jellyfin` creates a user, a directory and a systemd
unit (host state) *and* defines a container (declarative state). `grafana` templates a
provisioning file (data) *and* runs a container. So the split is not "jellyfin is Class A,
`mounts` is Class B"; it is per task. Each role should be separable into:

- **provision** — users, dirs, units, firewall, mounts. Host state. Class B.
- **container** — image, compose, config files. Declarative. Class A.

That refactor is the prerequisite for everything below. Without it, a "container" deploy
still runs host-level tasks and inherits Class B's risk.

### 9.2 Class A — container services: prod never runs agent code

Pipeline: **commit → render in a disposable environment → extract the declarative artifact
(compose + configs) → validate against a strict policy → apply by digest.**

The renderer is *not* `ansible-playbook` on `catafalco`. Either the role is rewritten to be
render-only (templates and `defaults`, no `tasks/` side effects), or the whole role runs in a
disposable VM and only the resulting `/srv/<service>/` tree is extracted. Ansible check mode
is **not** a security boundary: a task with `check_mode: false` runs anyway.

The validation policy is finite and checkable, because compose is declarative:

- `image:` by digest, from an allowlist — never `build:`, never a floating tag.
- `volumes:` only under declared paths (`/srv/<service>`, the media roots); no `/`, no
  `/etc`, no `/var/run/docker.sock`, no `volumes_from`.
- no `privileged`, no `cap_add`, no `devices`, no `security_opt` that disables
  seccomp/AppArmor, no `userns_mode: host`.
- no `network_mode: host`, `pid: host`, `ipc: host`, `uts: host`, `cgroup: host`.
- non-root `user:` where the image allows it; resource limits set.

If the rendered artifact violates any rule, the deployer refuses. This is a real boundary
because the output is a finite YAML document, not a program. The agent can write a hostile
*role*; it cannot make the *rendered compose* mount `/` without the policy catching it.

Residual risk, stated honestly: a kernel/container escape, a malicious image you did not
build, and **config-as-code** — a service that executes a script found in its config
directory. The last one is per-service: validate that what you ship is data, or that the
container does not execute it.

### 9.3 Class B — host-level roles: there is no deploy path

The artifact is host state, not a portable document. There is nothing to validate, because
the output *is* the machine. So the agent does not get to apply it:

- The agent produces a commit and a change request. A human reviews the diff and applies it.
  That is L1, and it is the ceiling for `mounts`, `mergerfs`, `snapraid`, `system`, `sshd`,
  `security`, `bootstrap`, and anything writing `/etc/sudoers.d`.
- Controls are: small scoped diffs, human review of the *rendered* change, a pre-deploy
  snapshot, and **detection** — alert on new cron entries, systemd timers, `authorized_keys`,
  `sudoers.d`, `ld.so.preload`, PAM edits, shell rc files and `daemon.json`.
- A disposable VM can *test* a Class B change, but it cannot *promote* it: the VM's `/etc` is
  not the host's. Promotion is still a human applying the commit.

### 9.4 The atomic-bomb question

You cannot prove an executable artifact is safe. Stop trying. Prove the weaker, achievable
invariant instead:

> **Agent-authored code never runs on the production host.**

For Class A that is true by construction: prod only runs `docker compose up` on a validated
file. For Class B it is true because a human applies the commit. Everything else is
mitigation, not prevention.

Consequences worth stating plainly:

- **Budget and rate limits stop loops, not a single malicious deploy.** Do not present them
  as blast-radius control. A logic bomb is one deploy.
- **Snapshot + rollback is recovery, not prevention.** A payload that fires after the
  rollback window (a timer, a cron, a modified binary) survives the rollback. Detection is
  the only control for delayed payloads.
- **Docker ≈ root is correct.** That is why the deployer, not the agent, holds the Docker
  socket, and why the compose policy — not the tag allowlist — is the Class A boundary.
- **The tag allowlist is a Class B control.** It bounds *which* host changes are in scope; it
  does not make them safe. For Class A the equivalent control is the compose policy.

### 9.5 What the deployer therefore is

Not "Ansible with a tag allowlist." Two components behind one API:

| Component | Class | What it does | Autonomy ceiling |
|---|---|---|---|
| artifact applier | A | render → validate → `compose up` by digest | L3 with evidence contract |
| gated runner | B | run a human-approved commit, snapshot, detect | L1 |

The agent gets: propose (both), supervised apply (A only), auto (A only, with a complete
evidence contract). Class B never exceeds propose. That is the boundary you were looking
for, and it is a boundary on *what runs where*, not on *what the model is allowed to say*.
