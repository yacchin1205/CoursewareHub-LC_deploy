"""Authenticated local Docker registry and a local Git repository for real builds."""
from contextlib import contextmanager
import json
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import base64


def run(argv, **kwargs):
    result = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            timeout=600, **kwargs)
    if result.returncode:
        raise RuntimeError(result.stdout)
    return result.stdout.strip()


class RegistryFixture:
    username = "cwh"
    initial_image = "niicloudoperation/jupyterhub-singleuser:latest"

    def __init__(self, host, password):
        self.host, self.password = host, password

    def request(self, path, password=None):
        auth = base64.b64encode((self.username + ":" + (self.password if password is None else password)).encode()).decode()
        request = urllib.request.Request("http://" + self.host + "/v2/" + path,
            headers={"Authorization": "Basic " + auth,
                     "Accept": "application/vnd.docker.distribution.manifest.v2+json"})
        return urllib.request.urlopen(request, timeout=10)

    def seed(self, manager, worker, source_image):
        run(["docker", "exec", manager, "docker", "pull", source_image])
        image = self.host + "/" + self.initial_image
        run(["docker", "exec", manager, "docker", "tag", source_image, image])
        for node in [manager, worker]:
            run(["docker", "exec", "-i", node, "docker", "login", "--username", self.username,
                 "--password-stdin", self.host], input=self.password + "\n")
        run(["docker", "exec", manager, "docker", "push", image])
        with self.request("niicloudoperation/jupyterhub-singleuser/manifests/latest") as response:
            assert response.status == 200
        # The builder runs on the manager and uses this real Docker login configuration.
        run(["docker", "exec", manager, "docker", "pull", "quay.io/jupyterhub/repo2docker:main"])


@contextmanager
def running_registry(network, results):
    name = "cwh-e2e-registry"
    results = Path(results)
    with tempfile.TemporaryDirectory(prefix="cwh-registry-") as directory:
        password = secrets.token_hex(24)
        run(["docker", "pull", "httpd:2.4-alpine"])
        auth = run(["docker", "run", "--rm", "--entrypoint", "htpasswd", "httpd:2.4-alpine",
                    "-Bbn", "cwh", password])
        auth_path = Path(directory) / "htpasswd"
        auth_path.write_text(auth + "\n")
        auth_path.chmod(0o644)
        run(["docker", "run", "-d", "--name", name, "--network", network,
             "-v", str(auth_path) + ":/auth/htpasswd:ro",
             "-e", "REGISTRY_AUTH=htpasswd", "-e", "REGISTRY_AUTH_HTPASSWD_REALM=CoursewareHub E2E",
             "-e", "REGISTRY_AUTH_HTPASSWD_PATH=/auth/htpasswd",
             "-e", "REGISTRY_STORAGE_DELETE_ENABLED=true", "registry:2.8.3"])
        try:
            ip = run(["docker", "inspect", "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", name])
            fixture = RegistryFixture(ip + ":5000", password)
            for attempt in range(60):
                try:
                    with fixture.request("") as response:
                        assert response.status == 200
                    break
                except (OSError, urllib.error.URLError):
                    time.sleep(1)
            else:
                raise AssertionError("Authenticated local registry did not start")
            try:
                fixture.request("", password="wrong-password")
            except urllib.error.HTTPError as error:
                assert error.code == 401, error.code
            else:
                raise AssertionError("Registry accepted an invalid password")
            (results / "registry-results.json").write_text(json.dumps({
                "authenticated_v2_endpoint": "passed", "wrong_password_rejected": "passed",
            }, indent=2) + "\n")
            yield fixture
        finally:
            log = subprocess.run(["docker", "logs", name], text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15)
            (results / "registry.log").write_text(log.stdout)
            subprocess.run(["docker", "rm", "-f", name], timeout=30, check=False)


@contextmanager
def running_build_repository(gateway, registry):
    with tempfile.TemporaryDirectory(prefix="cwh-build-repo-") as directory:
        root = Path(directory)
        work = root / "work"
        run(["git", "init", "-b", "main", str(work)])
        (work / "Dockerfile").write_text(
            "FROM " + registry.host + "/" + registry.initial_image + "\n"
            "USER root\nRUN printf 'built-by-repo2docker\\n' > /etc/cwh-e2e-built\nUSER jovyan\n")
        run(["git", "-C", str(work), "add", "Dockerfile"])
        run(["git", "-C", str(work), "-c", "user.name=CoursewareHub E2E",
             "-c", "user.email=e2e@example.test", "commit", "-m", "Build runtime probe"])
        ref = run(["git", "-C", str(work), "rev-parse", "HEAD"])
        run(["git", "clone", "--bare", str(work), str(root / "course.git")])
        daemon = subprocess.Popen(["git", "daemon", "--reuseaddr", "--export-all",
            "--base-path=" + directory, "--listen=0.0.0.0", "--port=19418"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            url = "git://" + gateway + ":19418/course.git"
            for attempt in range(30):
                if daemon.poll() is not None:
                    raise RuntimeError(daemon.stderr.read().decode())
                result = subprocess.run(["git", "ls-remote", url, "HEAD"], text=True,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
                if result.returncode == 0:
                    assert result.stdout.split()[0] == ref, result.stdout
                    break
                time.sleep(1)
            else:
                raise AssertionError("Local Git build source did not start")
            yield {"url": url, "ref": ref, "name": "e2e-built", "image": registry.host + "/e2e-built:" + ref[:7]}
        finally:
            daemon.terminate()
            daemon.wait(timeout=10)


def build_environment(page, base_url, repository, results, manager):
    results = Path(results)
    page.goto(base_url + "/services/environments/")
    page.locator("#add-environment").click()
    dialog = page.locator("#create-environment-dialog")
    dialog.locator(".repo-input").fill(repository["url"])
    dialog.locator(".ref-input").fill(repository["ref"])
    dialog.locator(".name-input").fill(repository["name"])
    with page.expect_response(lambda response:
            "/services/environments/api/environments" in response.url and
            response.request.method == "POST", timeout=60000) as request:
        dialog.locator(".save-button").click()
    response = request.value
    assert response.status == 200, response.text()
    image_name = repository["image"].split("/", 1)[1]
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        page.goto(base_url + "/services/environments/")
        row = page.locator('tr.image-row[data-image="' + image_name + '"]')
        if row.locator(".status-col .fa-check-square").is_visible():
            page.screenshot(path=str(results / "repo2docker-built.png"), full_page=True)
            (results / "repo2docker-results.json").write_text(json.dumps({
                "repository": repository, "build_and_registry_publication": "passed",
            }, indent=2) + "\n")
            return
        containers = run(["docker", "exec", manager, "docker", "ps", "-aq", "--filter",
                          "label=repo2docker.build=" + image_name]).splitlines()
        for container in containers:
            log = run(["docker", "exec", manager, "docker", "logs", container])
            (results / "repo2docker-build.log").write_text(log)
        time.sleep(2)
    raise AssertionError("repo2docker image did not appear as built: " + image_name)
