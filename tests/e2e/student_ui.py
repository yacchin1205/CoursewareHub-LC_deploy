"""Runtime checks for a real non-administrator account."""
import json
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright, expect


def register_student(manager, email, password):
    tasks = manager.output(['docker', 'service', 'ps', 'coursewarehub_auth-proxy',
                            '--filter', 'desired-state=running', '-q']).splitlines()
    assert len(tasks) == 1, tasks
    task = json.loads(manager.output(['docker', 'inspect', tasks[0]]))[0]
    container = task['Status']['ContainerStatus']['ContainerID']
    created = manager.run(['docker', 'exec', container, 'php', '/var/www/bin/add-local-user.php', email], check=False)
    assert created.returncode == 0, 'Student account creation failed'
    changed = manager.run(['docker', 'exec', container, 'php', '/var/www/bin/change-local-user-password.php', email, password], check=False)
    assert changed.returncode == 0, 'Student password setup failed'


def check_student(results, base_url, email, password, manager, *, expected_existing=False, built_image=False):
    results = Path(results)
    results.mkdir(parents=True, exist_ok=True)
    checks = []
    def passed(name):
        checks.append(name)
        print('STUDENT PASS:', name, flush=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        page.set_default_timeout(60000)
        context.tracing.start(screenshots=True, snapshots=True, sources=True)
        error = None
        try:
            page.goto(base_url + '/php/login.php')
            page.locator('input[name="email"]').fill(email)
            page.locator('input[name="password"]').fill(password)
            page.locator('input[type="submit"], button[type="submit"]').click()
            page.wait_for_function("location.pathname.startsWith('/hub/') || location.pathname.startsWith('/user/')", timeout=120000)

            def request(path, method='GET', body=None):
                return page.evaluate("""async ({path, method, body}) => {
                    const cookie = document.cookie.match(/(?:^|; )_xsrf=([^;]*)/);
                    const token = (window.jhdata && window.jhdata.xsrf_token) || (cookie ? decodeURIComponent(cookie[1]) : '');
                    const r = await fetch(path, {method, headers: {
                        'Content-Type': 'application/json',
                        'X-XSRFToken': token},
                        body: body === null ? undefined : JSON.stringify(body)});
                    return {status: r.status, text: await r.text()};
                }""", dict(path=path, method=method, body=body))

            page.goto(base_url + '/hub/home')
            result = request('/hub/api/user')
            assert result['status'] == 200, result
            user = json.loads(result['text'])
            assert user['admin'] is False, user
            username = user['name']
            assert request('/hub/admin')['status'] == 403
            passed('student logged in without administrator rights; admin page access denied')
            page.goto(base_url + '/hub/spawn')
            page.wait_for_url('**/user/' + username + '/**', timeout=300000)
            page.wait_for_function('window.Jupyter && Jupyter.notebook_list', timeout=120000)
            prefix = '/user/' + username + '/'
            tools = request(prefix + 'api/contents/tools/00_GuidanceForStudent.ipynb')
            assert tools['status'] == 200, tools
            assert json.loads(tools['text'])['type'] == 'notebook'
            teacher_tools = request(prefix + 'api/contents/admin_tools/00_GuidanceForTeacher.ipynb')
            assert teacher_tools['status'] == 404, teacher_tools
            passed('deployed student tools are accessible; administrator tools are not mounted')
            if expected_existing:
                data = request(prefix + 'api/contents/student-persist.txt')
                assert data['status'] == 200 and json.loads(data['text'])['content'] == 'student-data', data
                passed('student data and login survived the O01 update')
            notebook = {'nbformat': 4, 'nbformat_minor': 5, 'metadata': {
                'kernelspec': {'name': 'python3', 'language': 'python', 'display_name': 'Python 3'}},
                'cells': [{'cell_type': 'code', 'id': 'student-runtime', 'metadata': {},
                           'execution_count': None, 'outputs': [], 'source': ''}]}
            result = request(prefix + 'api/contents/student-runtime.ipynb', 'PUT',
                             {'type': 'notebook', 'format': 'json', 'content': notebook})
            assert result['status'] in [200, 201], result
            page.goto(base_url + prefix + 'notebooks/' + quote('student-runtime.ipynb'))
            page.wait_for_function('window.Jupyter && Jupyter.notebook && Jupyter.notebook.kernel && Jupyter.notebook.kernel.is_connected()')
            uid = int(manager.output(['id', '-u', username]))
            assert uid > 0
            source = ('import os, subprocess\nfrom pathlib import Path\n'
                      f'assert os.getuid() == {uid}, os.getuid()\n'
                      'assert subprocess.run(["sudo", "-n", "true"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT).returncode != 0\n'
                      'Path("student-persist.txt").write_text("student-data")\n')
            if built_image:
                source += 'assert Path("/etc/cwh-e2e-built").read_text().strip() == "built-by-repo2docker"\n'
            source += 'print("STUDENT_E2E_RESULT", 6 * 7)'
            page.evaluate('source => { const c = Jupyter.notebook.get_cell(0); c.set_text(source); c.execute(); }', source)
            expect(page.locator('.output_area')).to_contain_text('STUDENT_E2E_RESULT 42', timeout=120000)
            page.evaluate('() => Jupyter.notebook.save_notebook()')
            saved = request(prefix + 'api/contents/student-runtime.ipynb')
            assert saved['status'] == 200, saved
            (results / 'student-executed.ipynb').write_text(json.dumps(json.loads(saved['text'])['content'], indent=1) + '\n')
            page.screenshot(path=str(results / 'student-executed.png'), full_page=True)
            passed('student executed and saved a Notebook as the correct UID without passwordless sudo')
        except Exception as exception:
            error = str(exception)
            page.screenshot(path=str(results / 'student-failure.png'), full_page=True)
            (results / 'student-failure.html').write_text(page.content())
            raise
        finally:
            (results / 'student-results.json').write_text(json.dumps({
                'status': 'failed' if error else 'passed', 'passed': checks, 'error': error}, indent=2) + '\n')
            context.tracing.stop(path=str(results / 'student-trace.zip'))
            context.close()
            browser.close()
