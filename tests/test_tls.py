from types import SimpleNamespace

from kaji_agent import tls


def paths(cafile="", capath=""):
    return lambda: SimpleNamespace(cafile=cafile, capath=capath)


def bundle(tmp_path, name="bundle.crt", text="-----BEGIN CERTIFICATE-----\n"):
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def test_a_host_whose_built_in_path_is_missing_gets_the_first_bundle_that_exists(tmp_path):
    env = {}
    second, third = bundle(tmp_path, "b.crt"), bundle(tmp_path, "c.crt")
    chosen = tls.use_system_certificates(env, paths("/usr/lib/ssl/cert.pem", "/usr/lib/ssl/certs"), (str(tmp_path / "none"), second, third))
    assert chosen == second and env == {"SSL_CERT_FILE": second}


def test_nothing_changes_when_the_built_in_file_or_directory_holds_certificates(tmp_path):
    env = {}
    assert tls.use_system_certificates(env, paths(bundle(tmp_path)), (bundle(tmp_path, "x"),)) is None and env == {}
    (tmp_path / "certs").mkdir()
    (tmp_path / "certs" / "a.pem").write_text("x")
    assert tls.use_system_certificates(env, paths("", str(tmp_path / "certs")), (bundle(tmp_path, "y"),)) is None and env == {}


def test_an_empty_directory_or_file_is_not_a_trust_store(tmp_path):
    (tmp_path / "certs").mkdir()
    env = {}
    found = bundle(tmp_path, "found.crt")
    assert tls.use_system_certificates(env, paths(bundle(tmp_path, "empty.crt", ""), str(tmp_path / "certs")), (found,)) == found


def test_what_the_user_named_is_never_replaced(tmp_path):
    for var in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
        env = {var: "/mine"}
        assert tls.use_system_certificates(env, paths("/nope"), (bundle(tmp_path),)) is None and env == {var: "/mine"}


def test_with_no_bundle_anywhere_nothing_is_set_and_it_says_so(tmp_path, caplog):
    env = {}
    assert tls.use_system_certificates(env, paths("/nope"), (str(tmp_path / "none"),)) is None and env == {}
    assert "no CA certificates found" in caplog.text
