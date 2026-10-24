"""Local LTI 1.3 platform using real RS256 signatures and a JWKS endpoint."""
import base64
from contextlib import contextmanager
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlencode, urlparse


CLAIM = "https://purl.imsglobal.org/spec/lti/claim/"


def b64url(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


class LTIPlatform:
    client_id = "coursewarehub-e2e"
    deployment_id = "e2e-deployment"

    def __init__(self, issuer, tool_url, key, email):
        self.issuer = issuer.rstrip("/")
        self.tool_url = tool_url.rstrip("/")
        self.key = Path(key)
        self.email = email
        self.requests = []
        modulus = subprocess.check_output([
            "openssl", "rsa", "-in", str(key), "-noout", "-modulus"], text=True)
        modulus = bytes.fromhex(modulus.strip().split("=", 1)[1])
        self.jwks = {"keys": [{"kty": "RSA", "use": "sig", "alg": "RS256",
                               "kid": "e2e-platform", "n": b64url(modulus),
                               "e": b64url((65537).to_bytes(3, "big"))}]}

    @property
    def registration(self):
        return {self.issuer: {
            "client_id": self.client_id,
            "auth_login_url": self.issuer + "/authorize",
            "auth_token_url": self.issuer + "/token",
            "key_set_url": self.issuer + "/jwks",
            "private_key_file": "/private.key",
            "deployment": [self.deployment_id],
        }}

    def login_url(self, mode="valid"):
        return self.tool_url + "/php/lti/login.php?" + urlencode({
            "iss": self.issuer, "login_hint": mode,
            "target_link_uri": self.tool_url + "/php/lti/service.php",
            "client_id": self.client_id,
        })

    def token(self, nonce, mode):
        now = int(time.time())
        claims = {
            "iss": self.issuer, "aud": self.client_id, "sub": "e2e-instructor",
            "iat": now, "exp": now + 300, "nonce": nonce,
            "email": self.email,
            CLAIM + "message_type": "LtiResourceLinkRequest",
            CLAIM + "version": "1.3.0",
            CLAIM + "deployment_id": self.deployment_id,
            CLAIM + "target_link_uri": self.tool_url + "/php/lti/service.php",
            CLAIM + "resource_link": {"id": "e2e-resource"},
            CLAIM + "roles": ["http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor"],
        }
        if mode == "wrong-audience":
            claims["aud"] = "unregistered-client"
        elif mode == "wrong-deployment":
            claims[CLAIM + "deployment_id"] = "unregistered-deployment"
        elif mode == "expired":
            claims.update(iat=now - 600, exp=now - 300)
        header = {"alg": "RS256", "typ": "JWT", "kid": "e2e-platform"}
        unsigned = ".".join(b64url(json.dumps(part, separators=(",", ":")).encode())
                            for part in [header, claims])
        signature = subprocess.check_output([
            "openssl", "dgst", "-sha256", "-sign", str(self.key)], input=unsigned.encode())
        if mode == "bad-signature":
            signature = bytes([signature[0] ^ 1]) + signature[1:]
        return unsigned + "." + b64url(signature)

    def handler(self):
        platform = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                request = urlparse(self.path)
                if request.path == "/jwks":
                    self.respond(200, json.dumps(platform.jwks), "application/json")
                    platform.requests.append({"endpoint": "jwks"})
                    return
                if request.path != "/authorize":
                    self.respond(404, "Unknown platform endpoint")
                    return
                query = {key: values[0] for key, values in parse_qs(request.query).items()}
                expected = {"client_id": platform.client_id,
                            "redirect_uri": platform.tool_url + "/php/lti/service.php",
                            "response_type": "id_token", "response_mode": "form_post"}
                if any(query.get(key) != value for key, value in expected.items()):
                    self.respond(400, "Invalid OIDC request")
                    return
                mode = query.get("login_hint", "valid")
                if mode not in {"valid", "bad-signature", "wrong-audience", "wrong-deployment", "expired"}:
                    self.respond(400, "Unknown test case")
                    return
                if not query.get("nonce") or not query.get("state"):
                    self.respond(400, "Missing OIDC nonce or state")
                    return
                platform.requests.append({"endpoint": "authorize", "mode": mode})
                fields = {"state": query["state"], "id_token": platform.token(query["nonce"], mode)}
                inputs = "".join('<input type="hidden" name="' + escape(key, quote=True) +
                                 '" value="' + escape(value, quote=True) + '">'
                                 for key, value in fields.items())
                self.respond(200, '<!doctype html><form method="post" action="' +
                             escape(query["redirect_uri"], quote=True) + '">' + inputs +
                             '</form><script>document.forms[0].submit()</script>', "text/html")

            def respond(self, status, body, content_type="text/plain"):
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type + "; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format, *args):
                pass

        return Handler


@contextmanager
def running_platform(issuer, tool_url, email):
    with tempfile.TemporaryDirectory(prefix="cwh-lti-") as directory:
        key = Path(directory) / "platform.key"
        subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt",
                        "rsa_keygen_bits:2048", "-out", str(key)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        key.chmod(0o600)
        platform = LTIPlatform(issuer, tool_url, key, email)
        server = ThreadingHTTPServer(("0.0.0.0", urlparse(issuer).port), platform.handler())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield platform
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def check_lti(results, platform, expected_username):
    """Exercise the deployed tool's OIDC, JWT validation, and Hub login."""
    from playwright.sync_api import sync_playwright

    results = Path(results)
    results.mkdir(parents=True, exist_ok=True)
    checks = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            for mode in ["bad-signature", "wrong-audience", "wrong-deployment", "expired", "valid"]:
                context = browser.new_context(ignore_https_errors=True)
                page = context.new_page()
                context.tracing.start(screenshots=True, snapshots=True, sources=True)
                record = {"case": mode, "status": "running"}
                checks.append(record)
                try:
                    with page.expect_response(lambda response:
                            response.url == platform.tool_url + "/php/lti/service.php" and
                            response.request.method == "POST", timeout=60000) as launch:
                        page.goto(platform.login_url(mode))
                    response = launch.value
                    record["launch_http_status"] = response.status
                    if mode == "valid":
                        assert response.status < 400, response.text()
                        page.wait_for_function("location.pathname.startsWith('/hub/') || location.pathname.startsWith('/user/')", timeout=120000)
                        user = page.evaluate("""async () => {
                            const token = document.cookie.match(/(?:^|; )_xsrf=([^;]*)/);
                            const response = await fetch('/hub/api/user', {headers: {
                                'X-XSRFToken': token ? decodeURIComponent(token[1]) : ''}});
                            return {status: response.status, text: await response.text()};
                        }""")
                        assert user["status"] == 200, user
                        account = json.loads(user["text"])
                        assert account["name"] == expected_username and account["admin"] is True, account
                    else:
                        assert response.status == 403, response.status
                    assert {"endpoint": "authorize", "mode": mode} in platform.requests
                    page.screenshot(path=str(results / (mode + ".png")), full_page=True)
                    record["status"] = "passed"
                    print("LTI PASS:", mode, flush=True)
                except Exception as error:
                    record.update(status="failed", error=str(error))
                    page.screenshot(path=str(results / (mode + "-failure.png")), full_page=True)
                    (results / (mode + "-failure.html")).write_text(page.content())
                    raise
                finally:
                    (results / "lti-results.json").write_text(json.dumps(checks, indent=2) + "\n")
                    context.tracing.stop(path=str(results / (mode + "-trace.zip")))
                    context.close()
            assert {"endpoint": "jwks"} in platform.requests, platform.requests
        finally:
            (results / "platform-requests.json").write_text(json.dumps(platform.requests, indent=2) + "\n")
            browser.close()

