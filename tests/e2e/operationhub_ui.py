"""Use the deployed OperationHub and run its AWS construction recipe in a real user kernel."""
import json
from pathlib import Path
import time
from urllib.parse import quote

from playwright.sync_api import sync_playwright, expect


def check_operationhub(results, base_url, username, password, aws_parameters):
    from papermill.parameterize import parameterize_notebook
    from papermill.iorw import load_notebook_node
    import nbformat

    results = Path(results)
    results.mkdir(parents=True, exist_ok=True)
    steps = []

    def passed(step):
        steps.append(step)
        (results / 'runtime-summary.json').write_text(json.dumps({'passed': steps, 'status': 'running'}, indent=2) + '\n')
        print('OPERATIONHUB PASS:', step, flush=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(ignore_https_errors=True)
        context.tracing.start(screenshots=True, snapshots=True, sources=True)
        page = context.new_page()
        try:
            for attempt in range(90):
                try:
                    page.goto(base_url + '/hub/login', wait_until='domcontentloaded')
                    if page.locator('#username_input').is_visible():
                        break
                except Exception:
                    pass
                time.sleep(2)
            else:
                raise AssertionError('The constructed OperationHub login form did not become ready')
            page.locator('#username_input').fill(username)
            page.locator('#password_input').fill(password)
            page.locator('#login_submit').click()
            page.wait_for_url(lambda url: '/hub/login' not in str(url), timeout=60000)
            passed('administrator logged in to the constructed OperationHub')
            page.goto(base_url + '/hub/home')

            def fetch(path, method='GET', body=None):
                result = page.evaluate('''async ({path, method, body}) => {
                    const prefix = path.startsWith('/user/');
                    const cookie = document.cookie.split('; ').find(c => c.startsWith('_xsrf='));
                    const token = !prefix && window.jhdata && window.jhdata.xsrf_token
                        ? window.jhdata.xsrf_token : cookie ? decodeURIComponent(cookie.slice(6)) : '';
                    const response = await window.fetch(path, {method, headers: {
                        'X-XSRFToken': token, 'Content-Type': 'application/json'},
                        ...(body === null ? {} : {body: JSON.stringify(body)})});
                    return {status: response.status, text: await response.text()};
                }''', dict(path=path, method=method, body=body))
                assert 200 <= result['status'] < 300, result
                return json.loads(result['text']) if result['text'] else None

            user = fetch('/hub/api/user')
            assert user['name'] == username and user['admin'], user
            prefix = '/user/' + quote(username, safe='') + '/'
            page.locator('#start').click()
            page.wait_for_url(lambda url: prefix in str(url), timeout=300000)
            page.wait_for_function('window.Jupyter && Jupyter.notebook_list', timeout=120000)
            passed('the real user Notebook server started and rendered its tree')
            name = 'D01_CoursewareHub用VMをAWSに作成.ipynb'
            recipe = fetch(prefix + 'api/contents/' + quote(name))
            assert recipe['type'] == 'notebook', recipe
            input_path = results / 'D01-input-from-operationhub.ipynb'
            input_path.write_text(json.dumps(recipe['content'], ensure_ascii=False, indent=1) + '\n')
            notebook = parameterize_notebook(load_notebook_node(str(input_path)), aws_parameters)
            fetch(prefix + 'api/contents/' + quote(name), 'PUT',
                  {'type': 'notebook', 'format': 'json', 'content': notebook})
            page.goto(base_url + prefix + 'notebooks/' + quote(name))
            page.wait_for_function('window.Jupyter && Jupyter.notebook && Jupyter.notebook.kernel && Jupyter.notebook.kernel.is_connected()')
            page.evaluate('() => Jupyter.notebook.execute_all_cells()')
            page.wait_for_function('''() => {
                const cells = Jupyter.notebook.get_cells().filter(c => c.cell_type === 'code' && c.get_text().trim());
                return cells.some(c => c.output_area && c.output_area.outputs.some(o => o.output_type === 'error')) ||
                    cells.every(c => c.input_prompt_number !== '*' && c.input_prompt_number !== null
                        && c.input_prompt_number !== undefined);
            }''', timeout=900000)
            document = page.evaluate('() => Jupyter.notebook.toJSON()')
            (results / name).write_text(json.dumps(document, ensure_ascii=False, indent=1) + '\n')
            errors = [o for c in document['cells'] for o in c.get('outputs', []) if o['output_type'] == 'error']
            assert not errors, errors
            page.evaluate('() => Jupyter.notebook.save_notebook()')
            passed('the deployed AWS D01 construction Notebook ran in the OperationHub user kernel')
            hosts = fetch(prefix + 'api/contents/hosts')['content']
            assert '[' + aws_parameters['cloudformation_stack_name'] + ']' in hosts, hosts
            probe = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell(
                'import os\nfrom jupyter_core.paths import jupyter_runtime_dir\n'
                'assert os.getuid() > 0\nassert jupyter_runtime_dir() == "/tmp/jupyter-runtime"\n'
                'print("OPHUB_KERNEL", os.getuid(), 6 * 7)')])
            fetch(prefix + 'api/contents/e2e-operationhub.ipynb', 'PUT',
                  {'type': 'notebook', 'format': 'json', 'content': probe})
            page.goto(base_url + prefix + 'notebooks/e2e-operationhub.ipynb')
            page.wait_for_function('window.Jupyter && Jupyter.notebook && Jupyter.notebook.kernel && Jupyter.notebook.kernel.is_connected()')
            page.evaluate('() => Jupyter.notebook.execute_all_cells()')
            expect(page.locator('.output_area')).to_contain_text('OPHUB_KERNEL', timeout=60000)
            expect(page.locator('.output_area')).to_contain_text('42')
            page.evaluate('() => Jupyter.notebook.save_notebook()')
            page.screenshot(path=str(results / 'operationhub-kernel.png'), full_page=True)
            passed('Python ran as a non-root user and saved a Notebook in OperationHub')
            (results / 'runtime-summary.json').write_text(json.dumps({'passed': steps, 'status': 'passed'}, indent=2) + '\n')
            return hosts
        except Exception:
            page.screenshot(path=str(results / 'failure.png'), full_page=True)
            (results / 'failure.html').write_text(page.content())
            raise
        finally:
            context.tracing.stop(path=str(results / 'browser-trace.zip'))
            browser.close()
