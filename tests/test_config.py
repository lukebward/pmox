import textwrap

import pytest

from pmox.config import ConfigError, Settings, load_settings


def test_env_and_defaults(tmp_path):
    env = {"PROXMOX_HOST": "h", "PROXMOX_TOKEN_ID": "root@pam!t", "PROXMOX_TOKEN_SECRET": "s"}
    s = load_settings(env=env, config_path=tmp_path / "none.toml")
    assert s.host == "h"
    assert s.port == 8006
    assert s.verify_ssl is False
    assert s.user == "root@pam"
    assert s.token_name == "t"


def test_validate_missing(tmp_path):
    s = load_settings(env={}, config_path=tmp_path / "none.toml")
    with pytest.raises(ConfigError):
        s.validate()


def test_validate_bad_token_id(tmp_path):
    env = {"PROXMOX_HOST": "h", "PROXMOX_TOKEN_ID": "no-bang-here", "PROXMOX_TOKEN_SECRET": "s"}
    s = load_settings(env=env, config_path=tmp_path / "none.toml")
    with pytest.raises(ConfigError):
        s.validate()


def test_malformed_toml_raises_config_error(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text("this is not == toml")
    with pytest.raises(ConfigError, match="Invalid TOML"):
        load_settings(env={}, config_path=bad)


def test_file_then_env_precedence(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        textwrap.dedent(
            """
            host = "filehost"
            port = 9000
            token_id = "root@pam!file"
            token_secret = "filesecret"
            verify_ssl = true
            """
        )
    )
    s = load_settings(env={"PROXMOX_HOST": "envhost"}, config_path=cfg)
    assert s.host == "envhost"  # env overrides file
    assert s.port == 9000  # from file
    assert s.verify_ssl is True  # from file
    assert s.token_secret == "filesecret"


def test_proxmox_table_in_file(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[proxmox]\nhost = "tablehost"\n')
    s = load_settings(env={}, config_path=cfg)
    assert s.host == "tablehost"


def test_verify_ssl_env_parsing(tmp_path):
    env = {
        "PROXMOX_HOST": "h",
        "PROXMOX_TOKEN_ID": "root@pam!t",
        "PROXMOX_TOKEN_SECRET": "s",
        "PROXMOX_VERIFY_SSL": "true",
    }
    s = load_settings(env=env, config_path=tmp_path / "none.toml")
    assert s.verify_ssl is True


def test_overrides_win_and_none_ignored(tmp_path):
    env = {"PROXMOX_HOST": "envhost", "PROXMOX_TOKEN_ID": "root@pam!t", "PROXMOX_TOKEN_SECRET": "s"}
    s = load_settings(
        env=env, config_path=tmp_path / "none.toml", overrides={"host": "clihost", "port": None}
    )
    assert s.host == "clihost"  # explicit override wins
    assert s.port == 8006  # None override is ignored


def test_token_properties_without_bang():
    s = Settings(token_id="root@pam")
    assert s.user == "root@pam"
    assert s.token_name is None


def test_token_properties_without_token_id():
    s = Settings()
    assert s.user is None
    assert s.token_name is None


def test_network_and_defaults_from_file(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text(textwrap.dedent(
        """
        [proxmox]
        host = "h"
        [network]
        cidr = "192.168.0.0/24"
        gateway = "192.168.0.1"
        pool = "192.168.0.200-192.168.0.250"
        nameserver = "1.1.1.1"
        [defaults]
        import_storage = "local"
        ssh_key = "~/.ssh/id_ed25519.pub"
        ciuser = "ubuntu"
        """
    ))
    s = load_settings(env={}, config_path=cfg)
    assert s.host == "h"
    assert s.net_cidr == "192.168.0.0/24"
    assert s.net_gateway == "192.168.0.1"
    assert s.net_pool == "192.168.0.200-192.168.0.250"
    assert s.net_nameserver == "1.1.1.1"
    assert s.default_import_storage == "local"
    assert s.default_ssh_key == "~/.ssh/id_ed25519.pub"
    assert s.default_ciuser == "ubuntu"


def test_network_env_overrides_file(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[network]\ncidr = "10.0.0.0/24"\n')
    s = load_settings(env={"PROXMOX_NET_CIDR": "192.168.5.0/24"}, config_path=cfg)
    assert s.net_cidr == "192.168.5.0/24"


def test_network_unset_defaults_none(tmp_path):
    s = load_settings(env={}, config_path=tmp_path / "none.toml")
    assert s.net_cidr is None
    assert s.default_ciuser is None
