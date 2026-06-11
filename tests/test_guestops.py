"""Tests for guestops: SSH edges and the agent-install orchestration.

All subprocess/socket edges are module-level functions, monkeypatched here —
no real SSH or sockets.
"""

import subprocess
from types import SimpleNamespace

import pytest

from pmox import guestops


# ---------------------------------------------------------------- naming --


def test_image_tag_sanitizes():
    assert guestops.image_tag("ubuntu-24.04") == "img-ubuntu-24.04"
    assert guestops.image_tag("Ubuntu-24.04") == "img-ubuntu-24.04"
    assert guestops.image_tag("https://x.test/noble.img") == "img-https-x.test-noble.img"


def test_template_name_is_a_valid_guest_name():
    from pmox.provision import validate_guest_name

    name = guestops.agent_template_name("ubuntu-24.04")
    assert name == "agent-ubuntu-24-04"
    assert validate_guest_name(name) == name


def test_template_name_truncates_long_images():
    name = guestops.agent_template_name("x" * 300)
    assert len(name) <= 63


def test_agent_tags_joins_both_markers():
    tags = guestops.agent_tags("ubuntu-24.04")
    assert tags == "pmox-agent;img-ubuntu-24.04"


# ------------------------------------------------------------- ssh argv --


def test_ssh_command_shape_posix():
    argv = guestops.ssh_command("ubuntu", "10.0.0.5", "/k/id_ed25519", "echo hi", platform="linux")
    assert argv[0] == "ssh"
    assert "BatchMode=yes" in " ".join(argv)
    assert "UserKnownHostsFile=/dev/null" in " ".join(argv)
    assert "StrictHostKeyChecking=no" in " ".join(argv)
    assert "ubuntu@10.0.0.5" in argv
    assert argv[-1] == "echo hi"
    assert "/k/id_ed25519" in argv


def test_ssh_command_uses_nul_on_windows():
    argv = guestops.ssh_command("u", "h", "k", "c", platform="win32")
    assert "UserKnownHostsFile=NUL" in " ".join(argv)


def test_ssh_command_defaults_to_running_platform():
    import sys

    argv = guestops.ssh_command("u", "h", "k", "c")
    expected = "NUL" if sys.platform == "win32" else "/dev/null"
    assert f"UserKnownHostsFile={expected}" in " ".join(argv)


def test_private_key_path_strips_pub_suffix(tmp_path):
    pub = tmp_path / "id_ed25519.pub"
    priv = tmp_path / "id_ed25519"
    pub.write_text("ssh-ed25519 AAA pmox")
    priv.write_text("key")
    assert guestops.private_key_path(str(pub)) == str(priv)


def test_private_key_path_missing_private_errors(tmp_path):
    pub = tmp_path / "id_ed25519.pub"
    pub.write_text("ssh-ed25519 AAA pmox")
    with pytest.raises(FileNotFoundError) as exc:
        guestops.private_key_path(str(pub))
    assert "private key" in str(exc.value)


def test_private_key_path_rejects_non_pub_input(tmp_path):
    with pytest.raises(FileNotFoundError):
        guestops.private_key_path(str(tmp_path / "not_a_pub_key"))


# ---------------------------------------------------------- wait_for_port --


def test_wait_for_port_returns_once_connectable(monkeypatch):
    attempts = []

    def fake_connect(addr, timeout):
        attempts.append(addr)
        if len(attempts) < 3:
            raise OSError("refused")
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(guestops.socket, "create_connection", fake_connect)
    monkeypatch.setattr(guestops.time, "sleep", lambda s: None)
    guestops.wait_for_port("10.0.0.5", 22, timeout=60)
    assert len(attempts) == 3
    assert attempts[0] == ("10.0.0.5", 22)


def test_wait_for_port_times_out(monkeypatch):
    monkeypatch.setattr(guestops.socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(OSError("no")))
    ticks = iter([0, 1, 2, 100, 101])
    monkeypatch.setattr(guestops.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(guestops.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError) as exc:
        guestops.wait_for_port("10.0.0.5", 22, timeout=50)
    assert "SSH" in str(exc.value) or "port" in str(exc.value)


# ----------------------------------------------------------- run_ssh edge --


def test_run_ssh_invokes_subprocess(monkeypatch):
    captured = {}

    def fake_run(argv, capture_output, text, timeout):
        captured["argv"] = argv
        captured["timeout"] = timeout
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(guestops.subprocess, "run", fake_run)
    proc = guestops.run_ssh("ubuntu", "10.0.0.5", "/k/key", "echo hi", timeout=30)
    assert proc.returncode == 0
    assert captured["argv"][0] == "ssh"
    assert captured["timeout"] == 30


def test_run_ssh_missing_ssh_binary(monkeypatch):
    def fake_run(*a, **k):
        raise FileNotFoundError("ssh")

    monkeypatch.setattr(guestops.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError) as exc:
        guestops.run_ssh("u", "h", "k", "c")
    assert "OpenSSH" in str(exc.value)


# ------------------------------------------------------- ipv6 link-local --


def test_eui64_link_local_matches_real_guests():
    # real pair observed on the cluster: adguard CT bc:24:11:26:1d:b3
    assert guestops.eui64_link_local("bc:24:11:26:1d:b3") == "fe80::be24:11ff:fe26:1db3"
    assert guestops.eui64_link_local("BC:24:11:0D:85:4E") == "fe80::be24:11ff:fe0d:854e"


def test_link_local_candidates_windows_uses_ifindex(monkeypatch):
    monkeypatch.setattr(guestops.socket, "if_nameindex", lambda: [(1, "lo"), (22, "eth")])
    out = guestops.link_local_candidates("bc:24:11:26:1d:b3", platform="win32")
    assert out == ["fe80::be24:11ff:fe26:1db3%1", "fe80::be24:11ff:fe26:1db3%22"]


def test_link_local_candidates_posix_uses_names(monkeypatch):
    monkeypatch.setattr(guestops.socket, "if_nameindex", lambda: [(2, "eth0")])
    out = guestops.link_local_candidates("bc:24:11:26:1d:b3", platform="linux")
    assert out == ["fe80::be24:11ff:fe26:1db3%eth0"]


def test_link_local_candidates_survive_enumeration_failure(monkeypatch):
    def boom():
        raise OSError("no interfaces")

    monkeypatch.setattr(guestops.socket, "if_nameindex", boom)
    assert guestops.link_local_candidates("bc:24:11:26:1d:b3") == []


def test_wait_for_any_port_returns_first_connectable(monkeypatch):
    def fake_connect(addr, timeout):
        if addr[0] == "b":
            return SimpleNamespace(close=lambda: None)
        raise OSError("refused")

    monkeypatch.setattr(guestops.socket, "create_connection", fake_connect)
    monkeypatch.setattr(guestops.time, "sleep", lambda s: None)
    assert guestops.wait_for_any_port(["a", "b"], 22, timeout=60) == "b"


def test_wait_for_any_port_times_out_listing_candidates(monkeypatch):
    monkeypatch.setattr(guestops.socket, "create_connection",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no")))
    ticks = iter([float(i) for i in range(0, 500, 10)])
    monkeypatch.setattr(guestops.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(guestops.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError) as exc:
        guestops.wait_for_any_port(["a", "b"], 22, timeout=50)
    assert "a" in str(exc.value) and "b" in str(exc.value)


# ------------------------------------------------------ establish_agent_ssh --


def test_establish_tries_winner_first(monkeypatch):
    monkeypatch.setattr(guestops, "wait_for_any_port", lambda c, p, timeout: "fe80::1%2")
    installed = []
    monkeypatch.setattr(guestops, "install_agent",
                        lambda user, host, key, retry_seconds=120: installed.append(host))
    winner = guestops.establish_agent_ssh("ubuntu", ["10.0.0.5", "fe80::1%2"], "/k")
    assert winner == "fe80::1%2"
    assert installed == ["fe80::1%2"]


def test_establish_falls_through_to_next_candidate(monkeypatch):
    monkeypatch.setattr(guestops, "wait_for_any_port", lambda c, p, timeout: "fe80::1%2")
    monkeypatch.setattr(guestops, "wait_for_port", lambda host, port, timeout: None)

    def fake_install(user, host, key, retry_seconds=120):
        if host == "fe80::1%2":
            raise RuntimeError("denied")

    monkeypatch.setattr(guestops, "install_agent", fake_install)
    winner = guestops.establish_agent_ssh("ubuntu", ["fe80::1%2", "10.0.0.5"], "/k")
    assert winner == "10.0.0.5"


def test_establish_aggregates_all_failures(monkeypatch):
    monkeypatch.setattr(guestops, "wait_for_any_port", lambda c, p, timeout: "a")
    monkeypatch.setattr(guestops, "wait_for_port", lambda host, port, timeout: None)
    monkeypatch.setattr(guestops, "install_agent",
                        lambda user, host, key, retry_seconds=120: (_ for _ in ()).throw(RuntimeError(f"no {host}")))
    with pytest.raises(RuntimeError) as exc:
        guestops.establish_agent_ssh("u", ["a", "b"], "/k")
    assert "no a" in str(exc.value) and "no b" in str(exc.value)


def test_establish_skips_candidate_whose_port_never_opens(monkeypatch):
    monkeypatch.setattr(guestops, "wait_for_any_port", lambda c, p, timeout: "a")

    def fake_port(host, port, timeout):
        raise RuntimeError("closed")

    monkeypatch.setattr(guestops, "wait_for_port", fake_port)
    monkeypatch.setattr(guestops, "install_agent",
                        lambda user, host, key, retry_seconds=120: (_ for _ in ()).throw(RuntimeError("denied")))
    with pytest.raises(RuntimeError) as exc:
        guestops.establish_agent_ssh("u", ["a", "b"], "/k")
    assert "denied" in str(exc.value) and "closed" in str(exc.value)


# -------------------------------------------------------- agent script --


def test_setup_script_covers_the_critical_steps():
    s = guestops.AGENT_SETUP_SCRIPT
    assert "cloud-init status --wait" in s
    assert "qemu-guest-agent" in s
    assert "systemctl enable --now qemu-guest-agent" in s
    assert "cloud-init clean --logs" in s
    assert "truncate -s 0 /etc/machine-id" in s
    assert "ssh_host_" in s
    # stage markers make failures attributable
    assert s.count("===pmox:") >= 4
    # apt path retries the dpkg lock; dnf is the fallback
    assert "apt-get" in s and "dnf" in s


def test_install_agent_runs_script_and_succeeds(monkeypatch):
    calls = []

    def fake_run_ssh(user, host, key, cmd, timeout=900):
        calls.append((user, host, key, cmd))
        return subprocess.CompletedProcess([], 0, stdout="===pmox:done===", stderr="")

    monkeypatch.setattr(guestops, "run_ssh", fake_run_ssh)
    guestops.install_agent("ubuntu", "10.0.0.5", "/k/key")
    assert len(calls) == 1
    user, host, key, cmd = calls[0]
    assert (user, host, key) == ("ubuntu", "10.0.0.5", "/k/key")
    assert "qemu-guest-agent" in cmd


def test_install_agent_failure_reports_last_stage(monkeypatch):
    def fake_run_ssh(user, host, key, cmd, timeout=900):
        return subprocess.CompletedProcess(
            [], 100, stdout="===pmox:cloud-init===\n===pmox:install===\nE: lock", stderr="apt died"
        )

    monkeypatch.setattr(guestops, "run_ssh", fake_run_ssh)
    with pytest.raises(RuntimeError) as exc:
        guestops.install_agent("ubuntu", "10.0.0.5", "/k/key")
    msg = str(exc.value)
    assert "install" in msg          # the last stage marker reached
    assert "apt died" in msg         # stderr tail is surfaced


def test_install_agent_retries_ssh_255_until_auth_works(monkeypatch):
    """Ubuntu socket-activates sshd, so the port opens before cloud-init has
    written authorized_keys — early attempts fail with exit 255 and must retry."""
    procs = iter([
        subprocess.CompletedProcess([], 255, stdout="", stderr="Permission denied (publickey)."),
        subprocess.CompletedProcess([], 255, stdout="", stderr="Connection reset"),
        subprocess.CompletedProcess([], 0, stdout="===pmox:done===", stderr=""),
    ])
    calls = []

    def fake_run_ssh(user, host, key, cmd, timeout=900):
        calls.append(1)
        return next(procs)

    monkeypatch.setattr(guestops, "run_ssh", fake_run_ssh)
    monkeypatch.setattr(guestops.time, "sleep", lambda s: None)
    guestops.install_agent("u", "h", "k")
    assert len(calls) == 3


def test_install_agent_gives_up_after_retry_window(monkeypatch):
    def fake_run_ssh(user, host, key, cmd, timeout=900):
        return subprocess.CompletedProcess([], 255, stdout="", stderr="Connection refused")

    monkeypatch.setattr(guestops, "run_ssh", fake_run_ssh)
    monkeypatch.setattr(guestops.time, "sleep", lambda s: None)
    ticks = iter([float(i) for i in range(0, 2000, 50)])
    monkeypatch.setattr(guestops.time, "monotonic", lambda: next(ticks))
    with pytest.raises(RuntimeError) as exc:
        guestops.install_agent("u", "h", "k", retry_seconds=120)
    assert "Connection refused" in str(exc.value)


def test_install_agent_remote_script_failure_does_not_retry(monkeypatch):
    calls = []

    def fake_run_ssh(user, host, key, cmd, timeout=900):
        calls.append(1)
        return subprocess.CompletedProcess([], 1, stdout="===pmox:install===", stderr="boom")

    monkeypatch.setattr(guestops, "run_ssh", fake_run_ssh)
    with pytest.raises(RuntimeError):
        guestops.install_agent("u", "h", "k")
    assert len(calls) == 1
