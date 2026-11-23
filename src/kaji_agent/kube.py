"""A small Kubernetes client for the cluster the agent runs in: the service account's token and CA, plain HTTPS, nothing else to install."""

import json
import os
import ssl
import urllib.error
import urllib.request

SA = "/var/run/secrets/kubernetes.io/serviceaccount"


class KubeError(OSError):
    def __init__(self, status, text):
        super().__init__(f"Kubernetes answered {status}: {text[:200]}")
        self.status = status


class Kube:
    def __init__(self, host, token, ca=None, namespace="default"):
        self.host, self.token, self.namespace = host.rstrip("/"), token, namespace
        self.ctx = ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()

    @classmethod
    def in_cluster(cls, root=SA, environ=os.environ):
        host = f"https://{environ['KUBERNETES_SERVICE_HOST']}:{environ.get('KUBERNETES_SERVICE_PORT', '443')}"
        read = lambda name: open(os.path.join(root, name)).read().strip()  # noqa: E731
        return cls(
            host, read("token"), os.path.join(root, "ca.crt"), environ.get("KAJI_NAMESPACE") or read("namespace")
        )

    def request(self, method, path, body=None, raw=False):
        """The answer as JSON (text with ``raw``); ``None`` for a 404. Blocking: the agent calls it in a thread."""
        req = urllib.request.Request(
            self.host + path,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=30) as resp:
                data = resp.read().decode()
        except urllib.error.HTTPError as err:
            if err.code == 404:
                return None
            raise KubeError(err.code, err.read().decode(errors="replace")) from None
        return data if raw else json.loads(data or "{}")
