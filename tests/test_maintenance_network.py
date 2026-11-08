"""Updater network policy: official trust, bounded fallback, and secret handling."""

import subprocess
import time

import pytest

SHA = "a" * 40
OFFICIAL = "https://github.com/Eason4869/Ooptra.git"


def network():
    from tests.support.maintenance_harness import maintenance_network
    return maintenance_network


@pytest.fixture(autouse=True)
def isolated_child_environment(monkeypatch):
    # A failed subprocess-boundary assertion must never print workstation secrets.
    monkeypatch.setattr(network().os, "environ", {})


@pytest.mark.parametrize("field,value", [
    ("update_proxy", "http://host:7890\n--upload-pack=oops"),
    ("update_proxy", "http://host:99999"),
    ("update_proxy", "file:///tmp/proxy"),
    ("update_proxy", "http://host:7890/path?token=hidden"),
    ("update_proxy", "http://user:password@host:7890#secret"),
    ("update_mirror", "file:///tmp/repo"),
    ("update_mirror", "https://user:password@mirror.example/repo.git"),
    ("update_mirror", "https://mirror.example/repo.git?token=hidden"),
    ("update_mirror", "https://mirror.example/repo.git\r"),
    ("update_mirror", 123),
])
def test_invalid_settings_are_rejected_without_echoing_secret(field, value):
    with pytest.raises(ValueError) as caught:
        network().validate_network_field(field, value)
    assert "password" not in str(caught.value)
    assert "hidden" not in str(caught.value)


def test_valid_settings_preserve_encoded_proxy_credentials():
    module = network()
    value = "http://user:p%40ss@127.0.0.1:7890"
    assert module.validate_network_field("update_proxy", value) == value
    assert module.validate_network_field("update_proxy", "direct") == "direct"
    assert module.validate_network_field("update_mirror", "https://mirror.example/repo.git") == "https://mirror.example/repo.git"


def test_official_lookup_uses_no_mirror_and_noninteractive_verified_tls(tmp_path, monkeypatch):
    module = network()
    monkeypatch.setenv("GIT_SSL_NO_VERIFY", "true")
    monkeypatch.setenv("GIT_CONFIG", str(tmp_path / "untrusted.gitconfig"))
    settings = module.NetworkSettings("http://user:secret@proxy.example:7890", "https://mirror.example/repo.git")

    def run(root, args, timeout, env=None):
        assert root != tmp_path  # isolate official lookup from url.insteadOf
        assert args == ["git", "ls-remote", OFFICIAL, "refs/heads/dev"]
        assert "secret" not in " ".join(args)
        assert "GIT_SSL_NO_VERIFY" not in env
        assert "GIT_CONFIG" not in env
        pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))}
        assert pairs["http.proxy"] == "http://user:secret@proxy.example:7890"
        assert pairs["http.sslVerify"] == "true"
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_CEILING_DIRECTORIES"] == str(root.parent)
        assert 0 < timeout <= 15
        return SHA + "\trefs/heads/dev"

    assert module.lookup_official_sha(tmp_path, "dev", time.monotonic() + 60, run, settings=settings) == SHA


def test_official_lookup_switches_to_direct_then_official_api(tmp_path, monkeypatch):
    module = network()
    settings = module.NetworkSettings("http://proxy.example:7890", "https://mirror.example/repo.git")
    attempts = []

    def run(root, args, timeout, env=None):
        attempts.append(env["GIT_CONFIG_VALUE_0"])
        raise subprocess.TimeoutExpired(args, timeout)

    def api(channel, timeout, proxy):
        assert channel == "beta"
        assert timeout <= 15
        assert proxy == "http://proxy.example:7890"
        return SHA

    monkeypatch.setattr(module, "api_lookup", api)
    assert module.lookup_official_sha(tmp_path, "beta", time.monotonic() + 60, run, settings=settings) == SHA
    assert attempts == ["http://proxy.example:7890", ""]


def test_official_branch_row_must_match_requested_channel(tmp_path, monkeypatch):
    module = network()
    monkeypatch.setattr(module, "api_lookup", lambda *args: "b" * 40)
    sha = module.lookup_official_sha(tmp_path, "beta", time.monotonic() + 60,
                                    lambda *args, **kwargs: SHA + "\trefs/heads/dev",
                                    settings=module.NetworkSettings("direct"))
    assert sha == "b" * 40


def test_official_failure_never_consults_mirror_or_exposes_credentials(tmp_path, monkeypatch):
    module = network()
    monkeypatch.setattr(module, "api_lookup", lambda *args: (_ for _ in ()).throw(ValueError("secret")))

    def run(root, args, timeout, env=None):
        assert "mirror" not in " ".join(args)
        raise ValueError("http://user:secret@proxy.example:7890")

    with pytest.raises(ValueError, match="官方") as caught:
        module.lookup_official_sha(tmp_path, "dev", time.monotonic() + 60, run,
                                  settings=module.NetworkSettings("http://user:secret@proxy.example:7890", "https://mirror.example/repo.git"))
    assert "secret" not in str(caught.value)


def test_fallback_attempts_share_deadline(tmp_path, monkeypatch):
    module = network()
    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    attempts = []

    def run(root, args, timeout, env=None):
        attempts.append(timeout)
        clock[0] += timeout
        raise subprocess.TimeoutExpired(args, timeout)

    def api(channel, timeout, proxy):
        attempts.append(timeout)
        clock[0] += timeout
        raise ValueError("unreachable")

    monkeypatch.setattr(module, "api_lookup", api)
    with pytest.raises(ValueError):
        module.lookup_official_sha(tmp_path, "dev", 112.0, run, settings=module.NetworkSettings("http://proxy.example:7890"))
    assert sum(attempts) <= 12.001
    assert len(attempts) >= 3


def test_fetch_falls_back_to_configured_mirror_and_verifies_pinned_sha(tmp_path):
    module = network()
    attempted_urls = []
    events = []

    def run(root, args, timeout, env=None):
        if args[1] == "fetch":
            attempted_urls.append(args[-2])
            if args[-2] == OFFICIAL:
                raise ValueError("unreachable")
            return ""
        assert args == ["git", "rev-parse", "FETCH_HEAD"]
        return SHA

    result = module.fetch_verified(tmp_path, "dev", SHA, time.monotonic() + 120, run,
                                   settings=module.NetworkSettings("direct", "https://mirror.example/repo.git"), progress=events.append)
    assert attempted_urls == [OFFICIAL, "https://mirror.example/repo.git"]
    assert result == "配置镜像（已核对官方提交）"
    assert events and all("mirror.example" not in event for event in events)


def test_mirror_cannot_substitute_a_different_commit(tmp_path):
    module = network()

    def run(root, args, timeout, env=None):
        if args[1] == "fetch":
            if args[-2] == OFFICIAL:
                raise ValueError("unreachable")
            return ""
        return "b" * 40

    with pytest.raises(ValueError, match="不一致"):
        module.fetch_verified(tmp_path, "dev", SHA, time.monotonic() + 120, run,
                              settings=module.NetworkSettings("direct", "https://mirror.example/repo.git"))


def test_fetch_rejects_invalid_pinned_sha_before_network(tmp_path):
    module = network()
    with pytest.raises(ValueError):
        module.fetch_verified(tmp_path, "dev", "malformed", time.monotonic() + 120,
                              lambda *args, **kwargs: pytest.fail("must reject before fetch"))


def test_config_api_validates_and_redacts_network_fields(tmp_path, monkeypatch):
    import config
    from webui import config_editor as editor

    path = tmp_path / "config.py"
    path.write_text('WEBUI_CONFIG = {"host": "127.0.0.1", "port": 3090, "token": ""}\n', encoding="utf-8")
    monkeypatch.setattr(editor, "CONFIG_PATH", str(path))
    monkeypatch.setattr(config, "WEBUI_CONFIG", dict(config.WEBUI_CONFIG))
    result = editor.apply_updates({"webui": {"update_proxy": "http://user:secret@proxy.example:7890", "update_mirror": "https://mirror.example/repo.git"}})
    assert result["restart_required"] is False
    payload = editor.schema_payload()["webui"]["fields"]
    assert payload["update_proxy"]["value"] is None
    assert payload["update_proxy"]["is_set"] is True
    assert payload["update_mirror"]["value"] == "https://mirror.example/repo.git"
    assert network().load_network_settings().proxy == "http://user:secret@proxy.example:7890"
    before = path.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        editor.apply_updates({"webui": {"update_mirror": "file:///tmp/repo"}})
    assert path.read_text(encoding="utf-8") == before


def test_api_fallback_uses_only_official_verified_https(monkeypatch):
    module = network()

    class Response:
        status_code = 200

        def iter_content(self, chunk_size):
            yield (SHA + "\n").encode("ascii")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class Session:
        trust_env = True

        def __init__(self):
            self.proxies = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, **kwargs):
            assert url == "https://api.github.com/repos/Eason4869/Ooptra/commits/heads/dev"
            assert kwargs["verify"] is True
            assert kwargs["allow_redirects"] is False
            assert self.trust_env is False
            assert self.proxies == {"http": "http://proxy.example:7890", "https": "http://proxy.example:7890"}
            return Response()

    monkeypatch.setattr(module.requests, "Session", Session)
    assert module.lookup_official_sha(
        None, "dev", time.monotonic() + 60,
        lambda *a, **kw: (_ for _ in ()).throw(ValueError("offline")),
        settings=module.NetworkSettings("http://proxy.example:7890"),
    ) == SHA


def test_socks_git_proxy_falls_back_to_direct_api_without_optional_dependency(tmp_path, monkeypatch):
    module = network()
    monkeypatch.setattr(module, "api_lookup", lambda channel, timeout, proxy: SHA if proxy == "direct" else pytest.fail("SOCKS must not require requests optional extras"))
    assert module.lookup_official_sha(tmp_path, "dev", time.monotonic() + 60,
                                     lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("offline")),
                                     settings=module.NetworkSettings("socks5h://127.0.0.1:7890")) == SHA


def test_fetch_deadline_never_resets_for_mirror(tmp_path, monkeypatch):
    module = network()
    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    attempts = []

    def run(root, args, timeout, env=None):
        attempts.append(timeout)
        clock[0] += timeout
        raise subprocess.TimeoutExpired(args, timeout)

    with pytest.raises(ValueError):
        module.fetch_verified(tmp_path, "dev", SHA, 112.0, run,
                              settings=module.NetworkSettings("http://proxy.example:7890", "https://mirror.example/repo.git"))
    assert len(attempts) == 4
    assert sum(attempts) <= 12.001


def test_explicit_fetch_proxy_overrides_url_specific_git_settings(tmp_path):
    module = network()

    def run(root, args, timeout, env=None):
        if args[1] == "fetch":
            pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))}
            assert pairs[f"http.{OFFICIAL}.proxy"] == ""
            assert pairs[f"http.{OFFICIAL}.sslVerify"] == "true"
            return ""
        return SHA

    assert module.fetch_verified(tmp_path, "dev", SHA, time.monotonic() + 120, run,
                                 settings=module.NetworkSettings("direct")) == "官方 GitHub"
