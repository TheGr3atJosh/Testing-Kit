#!/usr/bin/env python3
import argparse
import base64
import json
import os
import re
import sqlite3
import sys
import time
import urllib3
import yaml
import requests
import paramiko

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

POLL_INTERVAL = 2  # seconds between task list polls
POLL_TIMEOUT = 60  # seconds before declaring a task timed-out

console = Console(highlight=False)


def die(msg: str) -> None:
    console.print(Panel(f"[bold]{escape(str(msg))}[/bold]", title="[bold red] Error [/bold red]", border_style="red"))
    sys.exit(1)


# ── Adaptix profile DB ────────────────────────────────────────────────────────

_ADAPTIX_DB = os.path.expanduser("~/.adaptix/storage-v1.db")


def _load_profile(table, project, name=None):
    if not os.path.exists(_ADAPTIX_DB):
        die(f"Adaptix database not found: {_ADAPTIX_DB}")
    con = sqlite3.connect(_ADAPTIX_DB)
    try:
        if name:
            row = con.execute(
                f"SELECT data FROM {table} WHERE project=? AND name=?", (project, name)
            ).fetchone()
        else:
            row = con.execute(
                f"SELECT data FROM {table} WHERE project=? LIMIT 1", (project,)
            ).fetchone()
    finally:
        con.close()
    if row is None:
        target = f"'{name}'" if name else "any profile"
        die(f"{table}: {target} not found in project '{project}'")
    return json.loads(row[0])


def _auto_agent_profile(project, listener_name):
    """First agent profile whose listener field matches listener_name, else first overall."""
    if not os.path.exists(_ADAPTIX_DB):
        die(f"Adaptix database not found: {_ADAPTIX_DB}")
    con = sqlite3.connect(_ADAPTIX_DB)
    try:
        rows = con.execute(
            "SELECT data FROM AgentProfiles WHERE project=?", (project,)
        ).fetchall()
    finally:
        con.close()
    if not rows:
        die(f"AgentProfiles: no profiles found in project '{project}'")
    profiles = [json.loads(r[0]) for r in rows]
    match = next((p for p in profiles if p.get("listener") == listener_name), None)
    if match:
        return match
    console.print(f"[dim]No agent profile matching listener '{escape(listener_name)}' — using first available[/dim]")
    return profiles[0]


def _inline_config(val):
    """Serialize config value to JSON string if it's a dict, otherwise return as-is."""
    if isinstance(val, dict):
        return json.dumps(val)
    return val or "{}"


def _resolve_listener_profile(setup_cfg, project):
    """Resolve listener profile from inline config or DB."""
    inline = setup_cfg.get("listener")
    if inline:
        return {
            "name": inline["name"],
            "type": inline["type"],
            "config": _inline_config(inline.get("config")),
        }
    return _load_profile("ListenerProfiles", project, setup_cfg.get("listener_profile"))


def _resolve_agent_profile(setup_cfg, project, listener_name):
    """Resolve agent profile from inline config, named DB profile, or auto-match."""
    inline = setup_cfg.get("agent")
    if inline:
        return {
            "agent": inline["agent"],
            "listener": inline["listener"],
            "listener_type": inline.get("listener_type", ""),
            "config": _inline_config(inline.get("config")),
        }
    profile_name = setup_cfg.get("agent_profile")
    if profile_name:
        return _load_profile("AgentProfiles", project, profile_name)
    return _auto_agent_profile(project, listener_name)


# ── Listener / agent setup ────────────────────────────────────────────────────

def _create_listener_from_profile(base_url, headers, profile):
    name = profile["name"]
    console.print(f"[dim]Creating listener[/dim] [cyan]{escape(name)}[/cyan] [dim]...[/dim]")
    resp = requests.post(
        f"{base_url}/listener/create",
        json={"name": name, "type": profile["type"], "config": profile["config"]},
        headers=headers,
        verify=False,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        msg = data.get("message", "")
        if "already exists" in msg.lower():
            console.print(f"[yellow]⚠[/yellow]  Listener '[cyan]{escape(name)}[/cyan]' already exists — skipping")
        else:
            die(f"Failed to create listener: {msg}")
    else:
        console.print(f"[green]✓[/green]  Listener '[cyan]{escape(name)}[/cyan]' created")


def _generate_agent_from_profile(base_url, headers, profile, output_path):
    output = os.path.expanduser(output_path)
    console.print(
        f"[dim]Generating agent[/dim] [cyan]{escape(profile.get('agent', '?'))}[/cyan] "
        f"[dim]→[/dim] [white]{escape(output)}[/white] [dim]...[/dim]"
    )
    resp = requests.post(
        f"{base_url}/agent/generate",
        json={
            "agent": profile["agent"],
            "listener_name": [profile["listener"]],
            "config": profile["config"],
        },
        headers=headers,
        verify=False,
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        die(f"Failed to generate agent: {data.get('message', '')}")

    name_b64, content_b64 = data["message"].split(":", 1)
    filename = base64.b64decode(name_b64).decode()
    payload = base64.b64decode(content_b64)

    with open(output, "wb") as f:
        f.write(payload)
    console.print(
        f"[green]✓[/green]  Agent generated "
        f"[dim]({len(payload):,} bytes)[/dim] [dim]→[/dim] "
        f"[white]{escape(output)}[/white] [dim][[{escape(filename)}]][/dim]"
    )


# ── Core helpers ──────────────────────────────────────────────────────────────

def load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)


def build_base_url(cfg):
    url = cfg["server"]["url"].rstrip("/")
    endpoint = cfg["server"].get("endpoint", "/").strip("/")
    return f"{url}/{endpoint}" if endpoint else url


def login(base_url, operator):
    resp = requests.post(
        f"{base_url}/login",
        json={"username": operator["name"], "password": operator["password"], "version": "1.0"},
        verify=False,
        timeout=15,
    )
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        die("Login failed: no access_token in response")
    return token


def dispatch(base_url, headers, agent_id, cmdline):
    resp = requests.post(
        f"{base_url}/agent/command/raw",
        json={"id": agent_id, "cmdline": cmdline},
        headers=headers,
        verify=False,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("ok", False), data.get("message", "")


def get_agent_list(base_url, headers):
    resp = requests.get(
        f"{base_url}/agent/list",
        headers=headers,
        verify=False,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json() or []


def resolve_agent(base_url, headers, cfg_agent_id):
    agents = get_agent_list(base_url, headers)
    if not agents:
        die("No agents available on the server.")

    if cfg_agent_id:
        agent = next((a for a in agents if a.get("a_id") == cfg_agent_id), None)
        if agent is None:
            die(f"Agent '{cfg_agent_id}' not found. Available: {[a.get('a_id') for a in agents]}")
    else:
        agent = agents[0]
        console.print(f"[dim]No agent.id in config — using first available: {agent.get('a_id')}[/dim]")

    os_map = {1: "Windows", 2: "Linux", 3: "MacOS"}
    os_str = os_map.get(agent.get("a_os", 0), "Unknown")
    elevated = "  [yellow bold]⚡ elevated[/yellow bold]" if agent.get("a_elevated") else ""
    console.print(
        f"[dim]Agent[/dim]  [cyan]{agent.get('a_id', '?')}[/cyan]  "
        f"[white]{escape(agent.get('a_computer', '?'))}[/white]"
        f"[dim]\\\\[/dim][white]{escape(agent.get('a_username', '?'))}[/white]  "
        f"[dim]{os_str}  {escape(agent.get('a_process', '?'))}:{agent.get('a_pid', '?')}[/dim]"
        f"{elevated}"
    )
    return agent.get("a_id")


# ── SSH delivery ──────────────────────────────────────────────────────────────

def ssh_connect(ssh_cfg):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs = {
        "hostname": ssh_cfg["host"],
        "username": ssh_cfg["username"],
        "look_for_keys": True,
        "allow_agent": True,
    }
    if "key_path" in ssh_cfg:
        kwargs["key_filename"] = os.path.expanduser(ssh_cfg["key_path"])
    client.connect(**kwargs)
    return client


def _exe_name(agent_path):
    return agent_path.replace("\\", "/").split("/")[-1]


_WMIC_ERRORS = {
    2: ("Access denied", "the SSH user lacks permission to create processes"),
    3: ("Insufficient privilege", "try running as an elevated user"),
    4: ("Initialization failure", "WMI service may not be running on the target"),
    8: ("Unknown failure", "no additional detail from WMI"),
    9: ("Path not found", "agent_path does not exist on the target — check config.yaml"),
    10: ("Invalid parameter", "the path string contains unsupported characters"),
    21: ("Invalid parameter", "malformed command line passed to WMI"),
}


def ssh_start_agent(client, agent_path):
    _, stdout, stderr = client.exec_command(f'wmic process call create "{agent_path}"')
    out = stdout.read().decode()
    err = stderr.read().decode().strip()

    if not out and not err:
        die("Failed to start agent: no response from WMIC (is WMI available on the target?)")
    if "ReturnValue = 0" in out:
        return

    m = re.search(r"ReturnValue = (\d+)", out)
    if m:
        code = int(m.group(1))
        title, hint = _WMIC_ERRORS.get(code, ("Unknown error", f"WMIC return code {code}"))
        die(f"Failed to start agent: {title} (code {code}) — {hint}")
    elif err:
        die(f"Failed to start agent: WMIC error — {err}")
    else:
        die("Failed to start agent: unexpected WMIC response (no ReturnValue found)")


def ssh_terminate_agent(client, agent_path):
    client.exec_command(f"taskkill /F /IM {_exe_name(agent_path)}")


def remove_agents_by_name(base_url, headers, exe_name):
    ids = [
        a["a_id"]
        for a in get_agent_list(base_url, headers)
        if a.get("a_process", "").lower() == exe_name.lower()
    ]
    if ids:
        requests.post(
            f"{base_url}/agent/remove",
            json={"agent_id_array": ids},
            headers=headers,
            verify=False,
            timeout=15,
        )


def wait_for_active_agent(base_url, headers, known_ticks, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for agent in get_agent_list(base_url, headers):
            aid = agent.get("a_id")
            tick = agent.get("a_last_tick", 0)
            if aid not in known_ticks or tick > known_ticks[aid]:
                return agent
        time.sleep(POLL_INTERVAL)
    return None


def ssh_deliver(base_url, headers, ssh_cfg):
    host = ssh_cfg["host"]
    agent_path = ssh_cfg["agent_path"]

    console.print(f"[dim]SSH →[/dim] [cyan]{escape(host)}[/cyan]  [dim]{escape(agent_path)}[/dim]")
    client = ssh_connect(ssh_cfg)
    console.print("[green]✓[/green]  SSH connected")

    ssh_terminate_agent(client, agent_path)
    time.sleep(1)
    remove_agents_by_name(base_url, headers, _exe_name(agent_path))

    if "source_path" in ssh_cfg:
        source = os.path.expanduser(ssh_cfg["source_path"])
        console.print(
            f"  [dim]uploading[/dim] [white]{escape(source)}[/white] "
            f"[dim]→[/dim] [white]{escape(agent_path)}[/white] [dim]...[/dim]"
        )
        sftp = client.open_sftp()
        sftp.put(source, agent_path)
        sftp.close()
        console.print("  [green]✓[/green]  Uploaded")

    known_ticks = {
        a["a_id"]: a.get("a_last_tick", 0) for a in get_agent_list(base_url, headers)
    }
    ssh_start_agent(client, agent_path)
    console.print("[dim]Agent process started — waiting for check-in ...[/dim]")

    agent = wait_for_active_agent(base_url, headers, known_ticks)
    if agent is None:
        client.close()
        die("Agent did not check in within 60s.")

    console.print(f"[green]✓[/green]  Agent checked in  [dim]({agent.get('a_id')})[/dim]")
    return client, agent.get("a_id")


# ── Task polling ──────────────────────────────────────────────────────────────

def get_task_list(base_url, headers, agent_id):
    resp = requests.get(
        f"{base_url}/agent/task/list",
        params={"agent_id": agent_id, "limit": 1000},
        headers=headers,
        verify=False,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json() or []


def poll_for_result(base_url, headers, agent_id, cmdline, known_ids):
    deadline = time.time() + POLL_TIMEOUT
    seen_ids = set(known_ids)
    chunks = []

    while time.time() < deadline:
        new_this_poll = False
        for task in get_task_list(base_url, headers, agent_id) or []:
            tid = task.get("a_task_id")
            if (
                tid not in seen_ids
                and task.get("a_cmdline") == cmdline
                and task.get("a_completed")
            ):
                chunks.append(task)
                seen_ids.add(tid)
                new_this_poll = True

        if chunks and not new_this_poll:
            break

        time.sleep(POLL_INTERVAL)

    if not chunks:
        return None
    if len(chunks) == 1:
        return chunks[0]

    merged = dict(chunks[0])
    merged["a_text"] = "".join(chunk.get("a_text", "") for chunk in chunks)
    merged["a_message"] = "".join(chunk.get("a_message", "") for chunk in chunks)
    return merged


def check_output(task_result, expected):
    actual = task_result.get("a_text", "") + task_result.get("a_message", "")
    return expected.lower() in actual.lower()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Adaptix automated task runner")
    parser.add_argument("-c", "-config", "--config", default="config.yaml",
                        help="Path to config YAML (default: config.yaml)")
    parser.add_argument("-t", "-tasks", "--tasks", default="tasks.yaml",
                        help="Path to tasks YAML (default: tasks.yaml)")
    args = parser.parse_args()

    try:
        cfg = load_yaml(args.config)
        tasks = load_yaml(args.tasks)["tasks"]
    except FileNotFoundError as e:
        die(f"Config file not found: {e.filename}")
    except KeyError:
        die("tasks.yaml must have a top-level 'tasks' key")

    base_url = build_base_url(cfg)
    operator = cfg["operator"]

    console.print(
        f"[dim]Logging in to[/dim] [cyan]{escape(base_url)}[/cyan] "
        f"[dim]as[/dim] [cyan]{escape(operator['name'])}[/cyan] [dim]...[/dim]"
    )
    try:
        token = login(base_url, operator)
    except requests.exceptions.ConnectionError:
        die(f"Connection refused — is the Adaptix server running at {base_url}?")
    except requests.exceptions.SSLError:
        die("SSL error connecting to server.")
    except requests.exceptions.Timeout:
        die("Login timed out.")
    except requests.exceptions.HTTPError as e:
        die(f"Login failed: {e}")
    headers = {"Authorization": f"Bearer {token}"}
    console.print("[green]✓[/green]  Logged in")

    setup_cfg = cfg.get("setup")
    if setup_cfg:
        project = setup_cfg.get("project", "")
        try:
            listener_profile = _resolve_listener_profile(setup_cfg, project)
            _create_listener_from_profile(base_url, headers, listener_profile)

            output_path = setup_cfg.get("agent_output", "./generated_agent")
            agent_profile = _resolve_agent_profile(setup_cfg, project, listener_profile["name"])
            _generate_agent_from_profile(base_url, headers, agent_profile, output_path)
        except requests.exceptions.RequestException as e:
            die(f"Setup failed: {e}")

    ssh_client = None
    ssh_cfg = cfg.get("ssh")

    if ssh_cfg:
        try:
            ssh_client, agent_id = ssh_deliver(base_url, headers, ssh_cfg)
        except Exception as e:
            die(f"SSH delivery failed: {e}")
    else:
        cfg_agent_id = cfg.get("agent", {}).get("id") or None
        try:
            agent_id = resolve_agent(base_url, headers, cfg_agent_id)
        except requests.exceptions.RequestException as e:
            die(f"Failed to fetch agent list: {e}")

    console.clear()

    results = []
    n = len(tasks)

    try:
        for i, task in enumerate(tasks, 1):
            cmdline = task["cmdline"]
            expected = task.get("expected", "")
            allowed_fail = task.get("allowed_to_fail", False)

            console.print(f"[bold]\\[{i}/{n}][/bold] [white]{escape(cmdline)}[/white]")

            try:
                known_ids = {t.get("a_task_id") for t in get_task_list(base_url, headers, agent_id)}
                ok, err_msg = dispatch(base_url, headers, agent_id, cmdline)
            except requests.exceptions.RequestException as e:
                console.print(f"  [red]✗ REQUEST ERROR[/red]  {escape(str(e))}\n")
                results.append({"task": task, "status": "dispatch-failed", "result": None, "err_msg": ""})
                continue

            if not ok:
                suffix = f"  [dim]{escape(err_msg)}[/dim]" if err_msg else ""
                if allowed_fail:
                    console.print(f"  [yellow]⚠ XFAIL[/yellow] [dim](dispatch failed)[/dim]{suffix}\n")
                    results.append({"task": task, "status": "xfail", "result": None, "err_msg": err_msg})
                else:
                    console.print(f"  [red]✗ DISPATCH FAILED[/red]{suffix}\n")
                    results.append({"task": task, "status": "dispatch-failed", "result": None, "err_msg": err_msg})
                continue

            console.print(f"  [dim]waiting (timeout {POLL_TIMEOUT}s) ...[/dim]")
            try:
                result = poll_for_result(base_url, headers, agent_id, cmdline, known_ids)
            except requests.exceptions.RequestException as e:
                console.print(f"  [red]✗ REQUEST ERROR[/red]  {escape(str(e))}\n")
                results.append({"task": task, "status": "timed-out", "result": None})
                continue

            if result is None:
                if allowed_fail:
                    console.print(f"  [yellow]⚠ XFAIL[/yellow] [dim](timed out)[/dim]\n")
                    results.append({"task": task, "status": "xfail", "result": None})
                else:
                    console.print(f"  [yellow]⏱ TIMED OUT[/yellow]\n")
                    results.append({"task": task, "status": "timed-out", "result": None})
                continue

            if result.get("a_msg_type") == 6:
                passed = False
            else:
                passed = check_output(result, expected) if expected else True

            if passed:
                status, label = "passed", "[green]✓ PASS[/green]"
            elif allowed_fail:
                status, label = "xfail", "[yellow]⚠ XFAIL[/yellow]"
            else:
                status, label = "failed", "[red]✗ FAIL[/red]"

            console.print(f"  {label}\n")
            results.append({"task": task, "status": status, "result": result})

    finally:
        if ssh_client:
            if ssh_cfg.get("terminate", False):
                exe = _exe_name(ssh_cfg["agent_path"])
                console.print(f"[dim]Terminating agent ({escape(exe)}) ...[/dim]")
                ssh_terminate_agent(ssh_client, ssh_cfg["agent_path"])
                remove_agents_by_name(base_url, headers, exe)
                console.print("[green]✓[/green]  Agent terminated and removed")
            ssh_client.close()

    # ── Summary ───────────────────────────────────────────────────────────────
    n_passed   = sum(1 for r in results if r["status"] == "passed")
    n_failed   = sum(1 for r in results if r["status"] == "failed")
    n_dispatch = sum(1 for r in results if r["status"] == "dispatch-failed")
    n_timeout  = sum(1 for r in results if r["status"] == "timed-out")
    n_xfail    = sum(1 for r in results if r["status"] == "xfail")
    n_bad = n_failed + n_dispatch + n_timeout

    console.print(Rule(style="dim"))

    table = Table(box=None, show_header=False, padding=(0, 2), collapse_padding=True)
    table.add_column(justify="right")
    table.add_column()
    table.add_row(f"[bold]{len(results)}[/bold]", "run")
    table.add_row(f"[green]{n_passed}[/green]", "passed")
    if n_failed:    table.add_row(f"[red]{n_failed}[/red]", "failed")
    if n_dispatch:  table.add_row(f"[red]{n_dispatch}[/red]", "dispatch-failed")
    if n_timeout:   table.add_row(f"[yellow]{n_timeout}[/yellow]", "timed out")
    if n_xfail:     table.add_row(f"[yellow]{n_xfail}[/yellow]", "xfail")
    console.print(table)

    if n_bad == 0:
        console.print("\n[bold green]All tasks passed.[/bold green]")
        return 0

    # ── Failed detail ─────────────────────────────────────────────────────────
    for r in [r for r in results if r["status"] == "failed"]:
        res = r["result"] or {}
        actual = res.get("a_text", "") + res.get("a_message", "")
        exp = r["task"].get("expected", "")

        body = Text()
        body.append(r["task"]["cmdline"] + "\n\n", style="bold white")
        body.append("Expected\n", style="dim")
        body.append("─" * 48 + "\n", style="dim")
        for line in exp.strip().splitlines():
            body.append(line + "\n", style="yellow")
        body.append(f"\nActual  ({len(actual):,} chars)\n", style="dim")
        body.append("─" * 48 + "\n", style="dim")
        for line in actual.splitlines():
            body.append(line + "\n")
        console.print(Panel(body, title="[bold red] FAILED [/bold red]", border_style="red"))

    # ── Dispatch failures ─────────────────────────────────────────────────────
    dispatched = [r for r in results if r["status"] == "dispatch-failed"]
    if dispatched:
        body = Text()
        for r in dispatched:
            body.append(r["task"]["cmdline"], style="white")
            if r.get("err_msg"):
                body.append(f"  {r['err_msg']}", style="dim")
            body.append("\n")
        console.print(Panel(body, title="[bold red] DISPATCH FAILED [/bold red]", border_style="red"))

    # ── Timeouts ──────────────────────────────────────────────────────────────
    timeouts = [r for r in results if r["status"] == "timed-out"]
    if timeouts:
        body = Text()
        for r in timeouts:
            body.append(r["task"]["cmdline"] + "\n", style="white")
        console.print(Panel(
            body,
            title=f"[bold yellow] TIMED OUT [/bold yellow][dim] (>{POLL_TIMEOUT}s)[/dim]",
            border_style="yellow",
        ))

    return 1


if __name__ == "__main__":
    sys.exit(main())
