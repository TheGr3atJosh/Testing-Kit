# Testing-Kit

Automated task runner for [Adaptix C2](https://github.com/Adaptix-Framework/AdaptixC2). Logs in to the server, optionally spins up a listener and generates an agent, delivers the agent to a target via SSH if needed, then dispatches a list of commands, waits for each to complete, and checks output against expected substrings.

## Requirements

- [uv](https://docs.astral.sh/uv/)
- Adaptix server accessible over HTTPS

## Install

```sh
uv tool install git+https://github.com/TheGr3atJosh/Testing-Kit
```

Run with:

```sh
adaptix-testing -c config.yaml -t tasks.yaml
```

Both flags default to `config.yaml` / `tasks.yaml` in the current directory if omitted.

---

## config.yaml

### Server + operator

```yaml
server:
  url: https://127.0.0.1:4321
  endpoint: /endpoint       # from profile.yaml → server.endpoint

operator:
  name: operator
  password: changeme
```

### Pin a specific agent (optional)

```yaml
agent:
  id: 12abcdef
```

Omit to use the first available agent on the server.

---

### Setup — automatic listener + agent (optional)

Creates a listener and generates an agent binary before running tasks.  
Two modes: **profile-based** (reads saved profiles from `~/.adaptix/storage-v1.db`) or **inline** (config embedded directly in the YAML).

#### Profile-based

```yaml
setup:
  project: myproject            # project name in the Adaptix DB
  listener_profile: my_listener # name in ListenerProfiles  (omit → first in project)
  agent_profile: my_agent       # name in AgentProfiles      (omit → auto-matches listener)
  agent_output: ./agent.exe     # where to save the payload  (default: ./generated_agent)
```

- If `listener_profile` is omitted, the first listener profile in the project is used.
- If `agent_profile` is omitted, the first agent profile whose `listener` field matches the created listener is used; falls back to first overall.
- If the listener already exists on the server it is skipped — safe to re-run without rebuilding.

#### Inline

No DB required — config fields mirror the Adaptix profile format exactly.

```yaml
setup:
  listener:
    name: my_listener
    type: KharonHTTP
    config:
      host_bind: "0.0.0.0"
      port_bind: 443
      callback_addresses:
        - "10.0.0.1:443"
  agent:
    agent: kharon
    listener: my_listener
    listener_type: KharonHTTP
    config:
      arch: x64
      format: Bin
      sleep: 5s
      jitter: 10
  agent_output: ./agent.bin
```

The `config` field can be a YAML mapping (as above) or a raw JSON string — both are accepted.

You can also mix the two modes, e.g. listener from DB and agent inline.

---

### SSH delivery (optional)

Uploads and starts the agent on a Windows target before running tasks.  
Requires SSH `authorized_keys` configured on the target (`ssh-copy-id <user>@<host>`).

```yaml
ssh:
  host: 192.168.1.100
  username: administrator
  # key_path: ~/.ssh/id_rsa            # optional — uses ssh-agent if omitted
  source_path: ./agent.exe             # optional — upload via SCP before starting
  agent_path: C:\Users\administrator\agent.exe
  # terminate: true                    # kill agent + remove from server when done
```

When `terminate: true`, the agent process is killed via `taskkill` and its record removed from the server at the end of the run.

Combine with `setup.agent_output` + `ssh.source_path` pointing to the same path to generate and immediately deliver an agent in one run.

---

## tasks.yaml

```yaml
tasks:
  - cmdline: "whoami"

  - cmdline: "fs ls C:\\Windows"
    expected: "explorer.exe"

  - cmdline: "process kill 9999"
    allowed_to_fail: true
```

| Field | Description |
|---|---|
| `cmdline` | Command dispatched via `/agent/command/raw` (server-side AxScript engine) |
| `expected` | Case-insensitive substring that must appear in output. Omit to only verify the command completed without error. |
| `allowed_to_fail` | If `true`, failure / timeout / dispatch rejection counts as `xfail` and does not fail the run. |

> **Note:** Only commands supported by the server-side AxScript engine work here. Client-side hook commands are not available. Some commands complete successfully but return no text output via the task list API (e.g. `fs pwd` on Kharon) — omit `expected` for those.

---

## Use cases

**Regression testing** — run a fixed task suite after updating an agent or BOF and verify nothing broke:

```yaml
tasks:
  - cmdline: "whoami"
    expected: "nt authority\\system"
  - cmdline: "fs ls C:\\Windows\\System32"
    expected: "ntdll.dll"
  - cmdline: "process list"
    expected: "lsass.exe"
```

**Full end-to-end run from zero** — spin up a listener, generate and deliver an agent, then run tasks, all in one command:

```yaml
setup:
  project: myproject
  listener_profile: my_listener
  agent_profile: my_agent
  agent_output: ./agent.exe

ssh:
  host: 192.168.1.100
  username: administrator
  source_path: ./agent.exe
  agent_path: C:\Users\administrator\agent.exe
  terminate: true
```

**Testing a new BOF** — mark surrounding tasks `allowed_to_fail` to isolate what you're actually testing:

```yaml
tasks:
  - cmdline: "bof /tmp/my_new.o arg1 arg2"
    expected: "success"

  - cmdline: "bof /tmp/cleanup.o"
    allowed_to_fail: true
```

**CI integration** — exits `0` on all-pass, `1` on any failure, compatible with standard CI pipelines.
