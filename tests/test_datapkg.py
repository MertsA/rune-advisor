"""Data package: build, sign, install, resolve, refuse, roll back, update (rune_advisor.datapkg)."""

import hashlib
import io
import json
import os
import tarfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rune_advisor import config, datapkg


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A scratch data home, a throwaway signing key trusted by the app, and a small data tree to package."""
    priv, pub = datapkg.generate_keypair()
    monkeypatch.setattr(config, "DATA_HOME", tmp_path / "home")
    monkeypatch.setattr(datapkg, "PUBLIC_KEYS", [pub])
    datapkg.reset()
    tree = tmp_path / "tree"
    (tree / "game_configs").mkdir(parents=True)
    (tree / "boosts.json").write_text('{"v": 1}')
    (tree / "game_configs" / "x.json.gz").write_bytes(b"gz")
    yield type("Env", (), {"priv": priv, "pub": pub, "tree": tree, "out": tmp_path / "out", "tmp": tmp_path})
    datapkg.reset()


def build(env, version="2026.11.01", **kw):
    return datapkg.build(env.out, version, kw.pop("priv", env.priv), "notice", root=env.tree, **kw)


def craft(env, members, manifest=None, version="2026.11.09", sign_with=None):
    """A signed archive with exactly these (name, bytes | TarInfo-maker) members, for hostile cases."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tar:
        for name, blob in members.items():
            info = tarfile.TarInfo(name)
            if isinstance(blob, tuple):  # (type, linkname)
                info.type, info.linkname = blob
                tar.addfile(info)
            else:
                info.size = len(blob)
                tar.addfile(info, io.BytesIO(blob))
    data = buf.getvalue()
    f = env.out / f"crafted-{version}.tar.xz"
    env.out.mkdir(parents=True, exist_ok=True)
    f.write_bytes(data)
    f.with_name(f.name + ".sig").write_text(datapkg.sign(data, sign_with or env.priv))
    return f


def manifest_for(files: dict[str, bytes], fmt=datapkg.DATA_FORMAT, version="2026.11.09") -> bytes:
    return json.dumps({"format": fmt, "version": version, "built": "x", "files": {
        n: {"sha256": hashlib.sha256(b).hexdigest(), "size": len(b)} for n, b in files.items()}}).encode()


def test_build_apply_resolves_from_package(env):
    archive, sig = build(env)
    assert datapkg.active().source == "bundled"
    assert datapkg.install_file(archive) == "2026.11.01"
    datapkg.reset()
    assert datapkg.active().source == "package"
    assert datapkg.path("boosts.json").parent == datapkg.package_dir("2026.11.01")
    assert datapkg.load_json("boosts.json") == {"v": 1}
    assert datapkg.path("game_configs") == datapkg.package_dir("2026.11.01") / "game_configs"  # packaged too
    # a file the package lacks still comes from the bundled data; the manifest and notice are in the package
    assert datapkg.path("urns.json") == datapkg.BUNDLED / "urns.json"
    assert (datapkg.package_dir("2026.11.01") / "NOTICE").read_text() == "notice"
    manifest = json.loads((datapkg.package_dir("2026.11.01") / "manifest.json").read_text())
    assert manifest["format"] == datapkg.DATA_FORMAT and manifest["files"]["boosts.json"]["size"] == 8


def test_no_package_is_bundled(env):
    assert datapkg.path("urns.json") == datapkg.BUNDLED / "urns.json"
    assert datapkg.load_json("urns.json")
    with pytest.raises(ValueError):
        datapkg.path("../x")


def test_wrong_format_refused(env):
    archive, _ = build(env, fmt=datapkg.DATA_FORMAT + 1)
    with pytest.raises(datapkg.PackageError, match="format"):
        datapkg.install_file(archive)
    assert datapkg.read_state()["current"] is None


def test_bad_signature_refused(env):
    archive, sig = build(env)
    other_priv, _ = datapkg.generate_keypair()
    sig.write_text(datapkg.sign(archive.read_bytes(), other_priv))
    with pytest.raises(datapkg.PackageError, match="signature"):
        datapkg.install_file(archive)
    sig.unlink()
    with pytest.raises(datapkg.PackageError, match="no signature"):
        datapkg.install_file(archive)
    assert not (config.DATA_HOME / "packages").exists() or not list((config.DATA_HOME / "packages").iterdir())


def test_no_trusted_keys_refuses_everything(env, monkeypatch):
    archive, _ = build(env)
    monkeypatch.setattr(datapkg, "PUBLIC_KEYS", [])
    with pytest.raises(datapkg.PackageError, match="no trusted keys"):
        datapkg.install_file(archive)


def test_tampered_archive_refused_before_unpacking(env, monkeypatch):
    archive, _ = build(env)
    data = bytearray(archive.read_bytes())
    data[len(data) // 2] ^= 0xFF
    archive.write_bytes(bytes(data))
    monkeypatch.setattr(datapkg, "unpack", lambda *a, **k: pytest.fail("unpacked an unverified archive"))
    with pytest.raises(datapkg.PackageError, match="signature"):
        datapkg.install_file(archive)


def test_signed_but_file_differs_from_manifest_refused(env):
    files = {"boosts.json": b'{"v": 2}'}
    man = json.loads(manifest_for(files))
    man["files"]["boosts.json"]["sha256"] = "0" * 64
    f = craft(env, {"manifest.json": json.dumps(man).encode(), "boosts.json": files["boosts.json"]})
    with pytest.raises(datapkg.PackageError, match="manifest"):
        datapkg.install_file(f)
    # and a file the manifest does not list
    f = craft(env, {"manifest.json": manifest_for({}), "evil.json": b"{}"})
    with pytest.raises(datapkg.PackageError, match="not in the manifest"):
        datapkg.install_file(f)


@pytest.mark.parametrize("name", ["../escape.json", "/abs.json", "a/../../b.json", "a\\b.json"])
def test_traversal_member_refused(env, name):
    blob = b"{}"
    f = craft(env, {"manifest.json": manifest_for({name: blob}), name: blob})
    with pytest.raises(datapkg.PackageError, match="unsafe"):
        datapkg.install_file(f)
    assert not (env.tmp / "escape.json").exists()


def test_link_member_refused(env):
    f = craft(env, {"manifest.json": manifest_for({}), "boosts.json": (tarfile.SYMTYPE, "/etc/passwd")})
    with pytest.raises(datapkg.PackageError, match="not a regular file"):
        datapkg.install_file(f)


def test_oversized_member_refused(env, monkeypatch):
    monkeypatch.setattr(datapkg, "MAX_MEMBER_BYTES", 4)
    files = {"boosts.json": b'{"v": 2}'}
    f = craft(env, {"manifest.json": manifest_for(files), "boosts.json": files["boosts.json"]})
    with pytest.raises(datapkg.PackageError, match="too large"):
        datapkg.install_file(f)


def test_rollback_and_keep_one_previous(env):
    for v in ("2026.11.01", "2026.11.02", "2026.11.03"):
        (env.tree / "boosts.json").write_text(json.dumps({"v": v}))
        datapkg.install_file(build(env, v)[0])
    st = datapkg.read_state()
    assert st == {"current": "2026.11.03", "previous": "2026.11.02"}
    assert not datapkg.package_dir("2026.11.01").exists()  # one previous only
    assert datapkg.rollback() == "2026.11.02"
    datapkg.reset()
    assert datapkg.load_json("boosts.json") == {"v": "2026.11.02"}
    assert datapkg.rollback() == "bundled"  # nothing before it
    datapkg.reset()
    assert datapkg.active().source == "bundled"
    with pytest.raises(datapkg.PackageError):
        datapkg.rollback()


def test_older_package_refused_unless_forced(env):
    datapkg.install_file(build(env, "2026.11.05")[0])
    old, _ = build(env, "2026.11.01")
    with pytest.raises(datapkg.PackageError, match="older"):
        datapkg.install_file(old)
    assert datapkg.install_file(old, allow_older=True) == "2026.11.01"


def test_damaged_current_falls_back_to_previous_then_bundled(env):
    (env.tree / "boosts.json").write_text('{"v": "old"}')
    datapkg.install_file(build(env, "2026.11.01")[0])
    (env.tree / "boosts.json").write_text('{"v": "new!"}')
    datapkg.install_file(build(env, "2026.11.02")[0])
    (datapkg.package_dir("2026.11.02") / "boosts.json").write_text("x")  # wrong size
    datapkg.reset()
    a = datapkg.active()
    assert (a.source, a.version) == ("previous", "2026.11.01") and a.notes
    assert datapkg.load_json("boosts.json") == {"v": "old"}
    (datapkg.package_dir("2026.11.01") / "manifest.json").write_text("not json")
    datapkg.reset()
    assert datapkg.active().source == "bundled"


def test_unparsable_packaged_file_falls_back_to_bundled_copy(env):
    tree = env.tree
    (tree / "urns.json").write_text("{not json}")
    datapkg.install_file(build(env, "2026.11.01")[0])
    datapkg.reset()
    assert datapkg.path("urns.json").parent == datapkg.package_dir("2026.11.01")
    assert datapkg.load_json("urns.json") == json.loads((datapkg.BUNDLED / "urns.json").read_text())


def test_active_package_is_frozen_for_the_process(env):
    datapkg.active()  # bundled, resolved
    datapkg.install_file(build(env)[0])
    assert datapkg.active().source == "bundled"  # takes effect on next start
    datapkg.reset()
    assert datapkg.active().source == "package"


class FakeServer:
    def __init__(self, files):
        self.files, self.calls = files, []

    def __call__(self, url, limit):
        self.calls.append(url)
        return self.files[url]


def serve(env, version):
    archive, sig = build(env, version)
    base = "https://example.test/rs"
    index = {"packages": {str(datapkg.DATA_FORMAT): {"version": version, "file": archive.name}}}
    return base, FakeServer({
        f"{base}/data/index.json": json.dumps(index).encode(),
        f"{base}/data/{archive.name}": archive.read_bytes(),
        f"{base}/data/{archive.name}.sig": sig.read_bytes(),
    })


def test_update_from_local_fake_index(env):
    base, server = serve(env, "2026.11.07")
    offer = datapkg.check(base, server)
    assert offer.version == "2026.11.07" and offer.newer
    assert datapkg.update(base, server) == "2026.11.07"
    assert datapkg.read_state()["current"] == "2026.11.07"
    assert datapkg.update(base, server) is None  # already current: no download
    assert not datapkg.check(base, server).newer


def test_update_refuses_bad_signature_and_keeps_data(env):
    base, server = serve(env, "2026.11.07")
    other, _ = datapkg.generate_keypair()
    sig_url = next(u for u in server.files if u.endswith(".sig"))
    server.files[sig_url] = datapkg.sign(b"other", other).encode()
    with pytest.raises(datapkg.PackageError):
        datapkg.update(base, server)
    assert datapkg.read_state()["current"] is None


def test_index_with_odd_file_name_refused(env):
    base, server = serve(env, "2026.11.07")
    server.files[f"{base}/data/index.json"] = json.dumps(
        {"packages": {str(datapkg.DATA_FORMAT): {"version": "2026.11.07", "file": "../../evil"}}}).encode()
    with pytest.raises(datapkg.PackageError, match="unexpected file"):
        datapkg.check(base, server)


def test_no_url_or_updates_off_never_calls_out(env):
    def boom(*a, **k):
        raise AssertionError("called out")

    assert datapkg.check(None, boom) is None
    assert datapkg.check_on_launch(config.Settings(), boom) is None  # no url
    s = config.Settings(data_url="https://example.test", check_updates=False)
    assert datapkg.check_on_launch(s, boom) is None
    s.check_updates = True  # a failing check never raises
    assert datapkg.check_on_launch(s, lambda *a: (_ for _ in ()).throw(OSError("down"))) is None


def test_settings_and_env(tmp_path, monkeypatch):
    toml = tmp_path / "rune_advisor.toml"
    toml.write_text('[data]\nurl = "https://x.test"\ncheck_updates = false\n')
    monkeypatch.delenv("RUNE_ADVISOR_DATA_URL", raising=False)
    s = config.load_settings(toml)
    assert s.data_url == "https://x.test" and s.check_updates is False
    monkeypatch.setenv("RUNE_ADVISOR_DATA_URL", "https://env.test")
    assert config.load_settings(toml).data_url == "https://env.test"


def test_cli_data_commands(env, monkeypatch):
    from rune_advisor.cli import app
    monkeypatch.delenv("RUNE_ADVISOR_DATA_URL", raising=False)
    monkeypatch.setenv("RUNE_ADVISOR_CONFIG", str(env.tmp / "none.toml"))
    run = CliRunner()
    archive, sig = build(env)
    r = run.invoke(app, ["data", "status"])
    assert r.exit_code == 0 and "bundled" in r.output
    r = run.invoke(app, ["data", "check"])
    assert r.exit_code == 0 and "not calling out" in " ".join(r.output.split())
    r = run.invoke(app, ["data", "apply", str(archive)])
    assert r.exit_code == 0 and "2026.11.01" in r.output
    sig.write_text(datapkg.sign(b"nope", env.priv))
    r = run.invoke(app, ["data", "apply", str(archive), "--force"])
    assert r.exit_code == 1 and "refused" in r.output
    r = run.invoke(app, ["data", "rollback"])
    assert r.exit_code == 0 and "bundled" in r.output


def test_build_script_keygen_and_package(env, monkeypatch, capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location("bdp", config.Path(__file__).parent.parent / "scripts" / "build_data_package.py")
    bdp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bdp)
    bdp.keygen(env.tmp / "keys")
    priv = (env.tmp / "keys" / "rune-advisor-data.key").read_text().strip()
    pub = (env.tmp / "keys" / "rune-advisor-data.pub").read_text().strip()
    assert oct((env.tmp / "keys" / "rune-advisor-data.key").stat().st_mode & 0o777) == "0o600"
    monkeypatch.setattr(datapkg, "PUBLIC_KEYS", [pub])
    archive, sig = datapkg.build(env.out, "2026.11.12", priv, bdp.NOTICE)  # the real bundled tree
    assert "CC BY-NC-SA" in bdp.NOTICE and "runescape.wiki" in bdp.NOTICE
    names = tarfile.open(archive).getnames()
    assert "manifest.json" in names and "NOTICE" in names and "boosts.json" in names
    assert "game_configs/quests.json.gz" in names and "game_configs/achievements.json.gz" in names
    assert "Jagex" in bdp.NOTICE and "game_configs/" in bdp.NOTICE
    assert not any(n.endswith(".py") or "__pycache__" in n for n in names)
    assert datapkg.install_file(archive) == "2026.11.12"


def test_package_not_newer_than_bundled_data_is_ignored(env, monkeypatch):
    """After an app upgrade the bundled data can be newer than an installed package: the package must not win."""
    datapkg.install_file(build(env, "2026.11.01")[0])
    monkeypatch.setattr(datapkg, "bundled_version", lambda: "2026.11.05")
    datapkg.reset()
    assert datapkg.active().source == "bundled"
    monkeypatch.setattr(datapkg, "bundled_version", lambda: "2026.10.01")
    datapkg.reset()
    assert datapkg.active().source == "package"


def test_user_folders_for_an_installed_app(tmp_path, monkeypatch):
    """A checkout reads settings from the working directory; an installed app from the user's settings folder,
    unless the file is in the working directory."""
    monkeypatch.chdir(tmp_path)
    assert config.CHECKOUT is not None and config.user_file("rune_advisor.toml") == Path("rune_advisor.toml")
    monkeypatch.setattr(config, "CHECKOUT", None)
    assert config.user_file("overrides.toml") == config.CONFIG_DIR / "overrides.toml"
    (tmp_path / "overrides.toml").write_text("")
    assert config.user_file("overrides.toml") == Path("overrides.toml")
    if os.name != "nt":
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "st"))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        assert config._user_dir("state") == tmp_path / "st" / "rune-advisor"
        assert config._user_dir("config") == Path.home() / ".config" / "rune-advisor"
