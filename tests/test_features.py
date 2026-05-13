"""Unit tests for the preamble and output-file features."""
import io
import sys
import os
import pytest
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from rich.console import Console
import run


# ── _ps_run ───────────────────────────────────────────────────────────────────

def _make_channel(exit_code, stdout_bytes=b"", stderr_bytes=b""):
    out_ch = MagicMock()
    out_ch.channel.recv_exit_status.return_value = exit_code
    out_ch.read.return_value = stdout_bytes
    err_ch = MagicMock()
    err_ch.read.return_value = stderr_bytes
    client = MagicMock()
    client.exec_command.return_value = (None, out_ch, err_ch)
    return client


def test_ps_run_success():
    client = _make_channel(0, b"hello world")
    code, out, err = run._ps_run(client, "Write-Output 'hello world'")
    assert code == 0
    assert out == "hello world"
    assert err == ""
    # Verify -EncodedCommand was used
    call_arg = client.exec_command.call_args[0][0]
    assert "-EncodedCommand" in call_arg
    assert "powershell" in call_arg.lower()


def test_ps_run_failure():
    client = _make_channel(1, stderr_bytes=b"Access denied")
    code, out, err = run._ps_run(client, "Do-Something")
    assert code == 1
    assert err == "Access denied"


def test_ps_run_encodes_utf16le():
    import base64
    client = _make_channel(0)
    run._ps_run(client, "Get-Process")
    call_arg = client.exec_command.call_args[0][0]
    # Extract the base64 payload and decode it
    encoded = call_arg.split("-EncodedCommand ")[1].strip()
    decoded = base64.b64decode(encoded).decode("utf-16-le")
    assert decoded == "Get-Process"


# ── preamble in ssh_deliver ───────────────────────────────────────────────────

def _mock_ssh_deliver_deps(monkeypatch, preamble_results=None):
    """Patch everything in ssh_deliver except the preamble path under test."""
    preamble_results = preamble_results or []

    fake_client = MagicMock()

    # ssh_connect returns our fake client
    monkeypatch.setattr(run, "ssh_connect", lambda cfg: fake_client)

    # _ps_run returns successive (exit_code, out, err) tuples
    call_iter = iter(preamble_results)
    monkeypatch.setattr(run, "_ps_run", lambda client, cmd: next(call_iter))

    # Stub out everything after preamble
    monkeypatch.setattr(run, "ssh_terminate_agent", lambda *a: None)
    monkeypatch.setattr(run, "remove_agents_by_name", lambda *a: None)
    monkeypatch.setattr(run, "ssh_start_agent", lambda *a: None)
    monkeypatch.setattr(run, "get_agent_list", lambda *a: [])
    monkeypatch.setattr(run, "wait_for_active_agent",
                        lambda *a, **kw: {"a_id": "abc123", "a_last_tick": 1})

    # Stub exec_command for the alive-check
    chk = MagicMock()
    chk.read.return_value = b"alive"
    fake_client.exec_command.return_value = (None, chk, MagicMock())

    return fake_client


def test_preamble_runs_all_commands(monkeypatch):
    calls = []
    fake_client = _mock_ssh_deliver_deps(
        monkeypatch,
        preamble_results=[(0, "ok1", ""), (0, "ok2", "")],
    )
    monkeypatch.setattr(run, "_ps_run", lambda c, cmd: calls.append(cmd) or (0, "", ""))

    ssh_cfg = {
        "host": "127.0.0.1",
        "username": "user",
        "agent_path": r"C:\ci\agent.exe",
        "preamble": ["New-Item -Force -Path C:\\test", "Set-Location C:\\test"],
    }
    run.ssh_deliver("https://x", {}, ssh_cfg)

    assert calls == ["New-Item -Force -Path C:\\test", "Set-Location C:\\test"]


def test_preamble_string_is_accepted(monkeypatch):
    """A bare string preamble (not a list) should run as a single command."""
    calls = []
    _mock_ssh_deliver_deps(monkeypatch)
    monkeypatch.setattr(run, "_ps_run", lambda c, cmd: calls.append(cmd) or (0, "", ""))

    ssh_cfg = {
        "host": "127.0.0.1",
        "username": "user",
        "agent_path": r"C:\ci\agent.exe",
        "preamble": "New-Item -Force -Path C:\\test",
    }
    run.ssh_deliver("https://x", {}, ssh_cfg)
    assert calls == ["New-Item -Force -Path C:\\test"]


def test_preamble_failure_calls_die(monkeypatch):
    _mock_ssh_deliver_deps(monkeypatch)
    monkeypatch.setattr(run, "_ps_run", lambda c, cmd: (1, "", "Access denied"))

    ssh_cfg = {
        "host": "127.0.0.1",
        "username": "user",
        "agent_path": r"C:\ci\agent.exe",
        "preamble": ["Bad-Command"],
    }
    with pytest.raises(SystemExit):
        run.ssh_deliver("https://x", {}, ssh_cfg)


def test_no_preamble_runs_fine(monkeypatch):
    _mock_ssh_deliver_deps(monkeypatch)
    ps_run_called = []
    monkeypatch.setattr(run, "_ps_run", lambda c, cmd: ps_run_called.append(cmd) or (0, "", ""))

    ssh_cfg = {
        "host": "127.0.0.1",
        "username": "user",
        "agent_path": r"C:\ci\agent.exe",
    }
    run.ssh_deliver("https://x", {}, ssh_cfg)
    assert ps_run_called == []


# ── _render_summary ───────────────────────────────────────────────────────────

def _console_to_str():
    buf = io.StringIO()
    c = Console(file=buf, highlight=False, no_color=True, width=80)
    return c, buf


def test_render_summary_all_passed():
    results = [
        {"task": {"cmdline": "whoami"}, "status": "passed", "result": {"a_text": "user", "a_message": ""}},
        {"task": {"cmdline": "dir"}, "status": "passed", "result": {"a_text": "ok", "a_message": ""}},
    ]
    c, buf = _console_to_str()
    exit_code = run._render_summary(c, results)

    assert exit_code == 0
    out = buf.getvalue()
    assert "2" in out          # total run count
    assert "passed" in out
    assert "All tasks passed" in out
    assert "failed" not in out.lower().replace("passed", "")


def test_render_summary_with_failure():
    results = [
        {"task": {"cmdline": "whoami", "expected": "admin"}, "status": "failed",
         "result": {"a_text": "user", "a_message": ""}},
    ]
    c, buf = _console_to_str()
    exit_code = run._render_summary(c, results)

    assert exit_code == 1
    out = buf.getvalue()
    assert "failed" in out.lower()
    assert "whoami" in out
    assert "admin" in out   # expected value shown
    assert "user" in out    # actual value shown


def test_render_summary_with_xfail():
    results = [
        {"task": {"cmdline": "xyzzy"}, "status": "xfail", "result": None, "err_msg": ""},
        {"task": {"cmdline": "whoami"}, "status": "passed", "result": {"a_text": "user", "a_message": ""}},
    ]
    c, buf = _console_to_str()
    exit_code = run._render_summary(c, results)

    assert exit_code == 0   # xfail does not count as a failure
    out = buf.getvalue()
    assert "xfail" in out


def test_render_summary_dispatch_failed():
    results = [
        {"task": {"cmdline": "bad cmd"}, "status": "dispatch-failed", "result": None, "err_msg": "unknown command"},
    ]
    c, buf = _console_to_str()
    exit_code = run._render_summary(c, results)

    assert exit_code == 1
    out = buf.getvalue()
    assert "dispatch-failed" in out
    assert "bad cmd" in out


def test_render_summary_timed_out():
    results = [
        {"task": {"cmdline": "slow cmd"}, "status": "timed-out", "result": None},
    ]
    c, buf = _console_to_str()
    exit_code = run._render_summary(c, results)

    assert exit_code == 1
    out = buf.getvalue()
    assert "timed out" in out.lower()
    assert "slow cmd" in out


# ── --output flag: stdout is suppressed ──────────────────────────────────────

def test_output_flag_silences_stdout(monkeypatch, tmp_path):
    """With -o, nothing should reach the real stdout console during the run."""
    output_file = tmp_path / "results.txt"

    # Restore the real console before and after (main() mutates the global)
    real_console = run.console

    captured_prints = []

    def fake_main_body():
        # Simulate what main() does: silence console then write summary
        run.console = Console(file=io.StringIO(), highlight=False)
        # Any console.print calls go to the sink
        run.console.print("this should not reach stdout")
        results = [
            {"task": {"cmdline": "whoami"}, "status": "passed",
             "result": {"a_text": "user", "a_message": ""}},
        ]
        with open(output_file, "w") as f:
            fc = Console(file=f, highlight=False, no_color=True)
            return run._render_summary(fc, results)

    try:
        exit_code = fake_main_body()
    finally:
        run.console = real_console

    assert exit_code == 0
    content = output_file.read_text()
    assert "passed" in content
    assert "All tasks passed" in content
    # Progress message did NOT reach the file
    assert "this should not reach stdout" not in content


def test_output_file_success_contains_only_summary(tmp_path):
    output_file = tmp_path / "out.txt"
    results = [
        {"task": {"cmdline": "whoami"}, "status": "passed",
         "result": {"a_text": "ci_runner", "a_message": ""}},
    ]
    with open(output_file, "w") as f:
        fc = Console(file=f, highlight=False, no_color=True)
        run._render_summary(fc, results)

    content = output_file.read_text()
    assert "All tasks passed" in content
    assert "ci_runner" not in content  # agent output not in summary


def test_output_file_failure_contains_detail(tmp_path):
    output_file = tmp_path / "out.txt"
    results = [
        {"task": {"cmdline": "whoami", "expected": "SYSTEM"}, "status": "failed",
         "result": {"a_text": "ci_runner", "a_message": ""}},
    ]
    with open(output_file, "w") as f:
        fc = Console(file=f, highlight=False, no_color=True)
        run._render_summary(fc, results)

    content = output_file.read_text()
    assert "SYSTEM" in content      # expected shown
    assert "ci_runner" in content   # actual shown
    assert "FAILED" in content
