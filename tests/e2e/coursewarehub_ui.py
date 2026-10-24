"""Runtime acceptance tests, separate from the deployment notebooks."""
import json
from pathlib import Path
import subprocess
import time
from urllib.parse import quote

from playwright.sync_api import sync_playwright, expect
from registry_fixture import build_environment


def check_coursewarehub(results, base_url, email, password, manager, worker, *, expected_existing=False, build_repository=None):
    results = Path(results)
    results.mkdir(parents=True, exist_ok=True)
    steps = []
    def passed(name):
        steps.append(name)
        (results / "runtime-summary.json").write_text(json.dumps({
            "passed": steps, "status": "running",
        }, indent=2) + "\n")
        print("RUNTIME PASS:", name)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(ignore_https_errors=True,
                                      record_video_dir=str(results / "video"))
        page = context.new_page()
        page.set_default_timeout(60000)
        context.tracing.start(screenshots=True, snapshots=True, sources=True)
        try:
            for attempt in range(90):
                try:
                    page.goto(base_url + "/php/login.php", wait_until="domcontentloaded")
                    if page.locator('input[name="email"]').is_visible():
                        break
                except Exception:
                    pass
                time.sleep(2)
            else:
                raise AssertionError("CoursewareHub login form did not become available")
            passed("login form served by deployed CoursewareHub")
            page.locator('input[name="email"]').fill(email)
            page.locator('input[name="password"]').fill(password)
            page.locator('input[type="submit"], button[type="submit"]').click()
            page.wait_for_function("location.pathname.startsWith('/hub/') || location.pathname.startsWith('/user/')", timeout=120000)
            user_response = page.evaluate("""async () => {
                const token = document.cookie.match(/(?:^|; )_xsrf=([^;]*)/);
                const response = await fetch('/hub/api/user', {headers: {
                    'X-XSRFToken': token ? decodeURIComponent(token[1]) : ''
                }});
                return {status: response.status, text: await response.text()};
            }""")
            assert user_response["status"] == 200, user_response
            user = json.loads(user_response["text"])
            assert user.get("admin") is True, user
            username = user["name"]
            passed("local administrator logged in through browser")

            if build_repository and not expected_existing:
                build_environment(page, base_url, build_repository, results, manager)
                passed("repo2docker built an image from local Git and published it to the authenticated registry")

            def spawn():
                response = page.goto(base_url + "/hub/spawn", wait_until="domcontentloaded")
                if response is not None:
                    assert response.status < 400, page.locator("body").inner_text()
                if build_repository and page.locator('input[name="image"]').count():
                    page.locator('input[name="image"][value="' + build_repository["image"] + '"]').check()
                    page.locator('input[type="submit"], button[type="submit"]').click()
                page.wait_for_url("**/user/" + username + "/**", timeout=300000)

            spawn()
            page.wait_for_function("window.Jupyter && Jupyter.notebook_list", timeout=120000)
            page.screenshot(path=str(results / "user-server.png"), full_page=True)
            passed("user Notebook server started and tree UI rendered")

            def user_fetch(path, method="GET", body=None):
                result = page.evaluate("""async ({path, method, body}) => {
                    const token = document.cookie.match(/(?:^|; )_xsrf=([^;]*)/);
                    const response = await fetch(path, {
                        method, headers: {'Content-Type': 'application/json',
                        'X-XSRFToken': token ? decodeURIComponent(token[1]) : ''},
                        body: body === null ? undefined : JSON.stringify(body)
                    });
                    return {status: response.status, text: await response.text()};
                }""", dict(path=path, method=method, body=body))
                assert 200 <= result["status"] < 300, result
                return json.loads(result["text"])

            user_prefix = "/user/" + username + "/"
            if expected_existing:
                persisted = user_fetch(user_prefix + "api/contents/e2e-persist.txt")
                assert persisted["content"] == "persistent-user-data", persisted
                saved = user_fetch(user_prefix + "api/contents/e2e-runtime.ipynb")
                assert "E2E_RESULT" in str(saved["content"]["cells"][0]["source"]), saved
                passed("user file and saved Notebook survived the O01 stack update")
            notebook = {"nbformat": 4, "nbformat_minor": 5, "metadata": {
                "kernelspec": {"name": "python3", "language": "python", "display_name": "Python 3"}},
                "cells": [{"cell_type": "code", "id": "runtime-check", "metadata": {},
                           "execution_count": None, "outputs": [], "source": ""}]}
            user_fetch(user_prefix + "api/contents/e2e-runtime.ipynb", "PUT",
                       {"type": "notebook", "format": "json", "content": notebook})
            page.goto(base_url + user_prefix + "notebooks/e2e-runtime.ipynb")
            page.wait_for_function("window.Jupyter && Jupyter.notebook && Jupyter.notebook.kernel && Jupyter.notebook.kernel.is_connected()")
            expected_uid = int(subprocess.check_output(
                ["docker", "exec", manager, "id", "-u", username], text=True).strip())
            assert expected_uid > 0
            source = ('import os\nfrom pathlib import Path\n'
                      f'assert os.getuid() == {expected_uid}, os.getuid()\n'
                      'Path("e2e-persist.txt").write_text("persistent-user-data")\n'
                      'print("E2E_RESULT", 6 * 7)')
            if build_repository:
                source += '\nassert Path("/etc/cwh-e2e-built").read_text().strip() == "built-by-repo2docker"\nprint("E2E_BUILT_IMAGE")'
            page.evaluate("source => { const c = Jupyter.notebook.get_cell(0); c.set_text(source); c.execute(); }", source)
            expect(page.locator(".output_area")).to_contain_text("E2E_RESULT 42", timeout=120000)
            if build_repository:
                expect(page.locator(".output_area")).to_contain_text("E2E_BUILT_IMAGE", timeout=120000)
                passed("the user kernel ran inside the repo2docker-built image")
            page.evaluate("() => Jupyter.notebook.save_notebook()")
            saved = user_fetch(user_prefix + "api/contents/e2e-runtime.ipynb")
            assert "E2E_RESULT" in str(saved["content"]["cells"][0]["source"]), saved
            page.screenshot(path=str(results / "executed-cell.png"), full_page=True)
            passed("browser executed Python as the provisioned non-root UID and saved the Notebook")

            # Pin the fixture host identities for the administrator's real SSH client.
            known_hosts = []
            for node in [manager, worker]:
                ip = subprocess.check_output(["docker", "inspect", "-f",
                    "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", node], text=True).strip()
                key = subprocess.check_output(["docker", "exec", node, "cat",
                    "/etc/ssh/ssh_host_ed25519_key.pub"], text=True).strip()
                known_hosts.append(ip + " " + key)
            home = "/jupyter/users/" + username
            subprocess.run(["docker", "exec", "-i", manager, "tee", home + "/.ssh/known_hosts"],
                           input="\n".join(known_hosts) + "\n", text=True, check=True,
                           stdout=subprocess.DEVNULL)
            subprocess.run(["docker", "exec", manager, "chown", username + ":" + username,
                            home + "/.ssh/known_hosts"], check=True)

            d06_name = "D06_CoursewareHubコンテンツの配備.ipynb"
            page.goto(base_url + user_prefix + "notebooks/" + quote(d06_name))
            page.wait_for_function("window.Jupyter && Jupyter.notebook && Jupyter.notebook.kernel && Jupyter.notebook.kernel.is_connected()")
            page.evaluate("""() => {
                const cell = Jupyter.notebook.get_cell(1);
                cell.set_text(cell.get_text().replace('deploy_repo_ref = None',
                    "deploy_repo_ref = '3b586614affb304a65f337226aaf96138dcff4c8'"));
                Jupyter.notebook.execute_all_cells();
            }""")
            page.wait_for_function("""() => Jupyter.notebook.get_cells()
                .filter(c => c.cell_type === 'code' && c.get_text().trim())
                .every(c => c.input_prompt_number !== '*' && c.input_prompt_number !== null
                    && c.input_prompt_number !== undefined)""", timeout=300000)
            errors = page.evaluate("""() => Jupyter.notebook.get_cells()
                .filter(c => c.cell_type === 'code')
                .flatMap(c => (c.output_area ? c.output_area.outputs : []))
                .filter(o => o.output_type === 'error')""")
            assert not errors, errors
            page.evaluate("() => Jupyter.notebook.save_notebook()")
            d06 = user_fetch(user_prefix + "api/contents/" + quote(d06_name))
            (results / "D06-executed-in-user-server.ipynb").write_text(
                json.dumps(d06["content"], ensure_ascii=False, indent=1) + "\n")
            page.screenshot(path=str(results / "d06-completed.png"), full_page=True)
            teacher_tool = user_fetch(user_prefix + "api/contents/admin_tools/00_GuidanceForTeacher.ipynb")
            assert teacher_tool["type"] == "notebook", teacher_tool
            passed("D06 executed in the user Notebook server and deployed teacher tools are accessible")

            page.goto(base_url + "/hub/home")
            page.locator("#stop").click()
            expect(page.locator("#start")).to_be_visible(timeout=120000)
            spawn()
            page.wait_for_function("window.Jupyter && Jupyter.notebook_list", timeout=120000)
            persisted = user_fetch(user_prefix + "api/contents/e2e-persist.txt")
            assert persisted["content"] == "persistent-user-data", persisted
            saved = user_fetch(user_prefix + "api/contents/e2e-runtime.ipynb")
            assert "E2E_RESULT" in str(saved["content"]["cells"][0]["source"])
            passed("saved user file and Notebook survived server stop/start")
            (results / "runtime-summary.json").write_text(json.dumps({
                "status": "passed", "passed": steps,
            }, indent=2) + "\n")
        except Exception as error:
            try:
                current_notebook = page.evaluate("() => window.Jupyter && Jupyter.notebook ? Jupyter.notebook.toJSON() : null")
                if current_notebook:
                    (results / "runtime-notebook-at-failure.ipynb").write_text(
                        json.dumps(current_notebook, ensure_ascii=False, indent=1) + "\n")
            except Exception:
                pass
            page.screenshot(path=str(results / "runtime-failure.png"), full_page=True)
            (results / "runtime-failure.html").write_text(page.content())
            (results / "runtime-summary.json").write_text(json.dumps({
                "status": "failed", "passed": steps, "error": str(error),
            }, indent=2) + "\n")
            raise
        finally:
            context.tracing.stop(path=str(results / "runtime-trace.zip"))
            context.close()
            browser.close()

    return username
