"""Where the agent finds the CA certificates it checks the gateway's certificate against.

The compiled agent is built on Debian, and its OpenSSL looks where Debian keeps them. On another distribution (Gentoo: ``/etc/ssl/certs/ca-certificates.crt``) that
path does not exist and every connection fails with ``CERTIFICATE_VERIFY_FAILED``. So when the paths the agent was built with hold nothing, and the user did not
name a bundle (``SSL_CERT_FILE`` or ``SSL_CERT_DIR``), the first bundle that exists in the usual places of the other distributions is used. Nothing is turned off:
a host with no bundle at all still fails to connect, and says why."""

import logging
import os
import ssl

log = logging.getLogger("kaji_agent")

BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",  # Debian, Ubuntu, Gentoo, Alpine, Arch
    "/etc/pki/tls/certs/ca-bundle.crt",  # Fedora, RHEL
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",  # RHEL's own copy
    "/etc/ssl/ca-bundle.pem",  # openSUSE
    "/etc/ssl/cert.pem",  # Alpine, macOS, the BSDs
    "/usr/local/share/certs/ca-root-nss.crt",  # FreeBSD
)


def _holds_certificates(path):
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0 if path else False
    except OSError:
        return False


def _has_directory(path):
    try:
        return bool(path) and os.path.isdir(path) and any(os.scandir(path))
    except OSError:
        return False


def use_system_certificates(environ=os.environ, paths=ssl.get_default_verify_paths, bundles=BUNDLES):
    """Point ``SSL_CERT_FILE`` at the host's bundle if the built-in paths hold none. Returns the file it chose, or ``None`` (nothing needed, or nothing found)."""
    if environ.get("SSL_CERT_FILE") or environ.get("SSL_CERT_DIR"):
        return None  # the user said where
    default = paths()
    if _holds_certificates(default.cafile) or _has_directory(default.capath):
        return None
    for bundle in bundles:
        if _holds_certificates(bundle):
            environ["SSL_CERT_FILE"] = bundle
            log.info("no CA certificates at %s: using %s", default.cafile or default.capath or "the built-in path", bundle)
            return bundle
    log.warning("no CA certificates found (looked at %s and %s): the gateway's certificate cannot be checked; set SSL_CERT_FILE", default.cafile, ", ".join(bundles))
    return None
