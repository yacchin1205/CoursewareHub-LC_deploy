"""Operate unmodified D01/D02 originals through the LC4RI Notebook UI."""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import quote, unquote, urlparse
import urllib.request

import yaml
from playwright.sync_api import sync_playwright, expect

BASE = 'http://127.0.0.1:18888'
SOURCES = {
    'D01': 'coursewarehub/D01_オンプレミス環境の場合のマシン設定.ipynb',
    'D02': 'coursewarehub/D02_CoursewareHubインベントリの準備.ipynb',
}


def source(cell):
    value = cell['source']
    return ''.join(value) if isinstance(value, list) else value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--result-dir', type=Path, required=True)
    parser.add_argument('--master', required=True)
    parser.add_argument('--worker', required=True)
    parser.add_argument('--construction', action='store_true')
    parser.add_argument('--operationhub', action='store_true')
    parser.add_argument('--input-failures', action='store_true')
    parser.add_argument('--api-failures', action='store_true')
    parser.add_argument('--authentication', choices=('local','federation'), default='local')
    args = parser.parse_args()
    if args.api_failures:
        SOURCES.clear()
        SOURCES['OperationHub'] = 'D01_OperationHubをAWSに構築.ipynb'
        SOURCES['CoursewareHub-AWS-D01'] = 'coursewarehub/D01_CoursewareHub用VMをAWSに作成.ipynb'
    elif args.operationhub:
        args.construction = True
        SOURCES.clear()
        SOURCES['OperationHub'] = 'D01_OperationHubをAWSに構築.ipynb'
    elif args.input_failures:
        for prefix in ('D03', 'D04', 'D05', 'D06', 'O01'):
            SOURCES[prefix] = str(next(Path('coursewarehub').glob(prefix + '*.ipynb')))
    elif args.construction:
        for prefix in ('D03', 'D04', 'D05', 'O01'):
            SOURCES[prefix] = str(next(Path('coursewarehub').glob(prefix + '*.ipynb')))
    repo = Path(__file__).resolve().parents[2]
    results = args.result_dir.resolve()
    results.mkdir(parents=True, exist_ok=True)
    token = os.environ['LC4RI_TEST_TOKEN']
    records, steps = [], []
    args.fixtures = ExitStack()
    args.saml = None
    if args.construction and not args.operationhub and args.authentication == 'federation':
        from saml_provider import running_provider
        args.saml = args.fixtures.enter_context(running_provider('http://' + os.environ['LC4RI_GATEWAY'] + ':18501', 'https://coursewarehub.test', 'ciadmin@example.test'))
    if args.api_failures:
        from moto.server import ThreadedMotoServer
        args.api_server = ThreadedMotoServer(ip_address='127.0.0.1', port=18503, verbose=False)
        args.api_server.start()
        args.fixtures.callback(args.api_server.stop)
    if args.operationhub:
        from operationhub import setup
        setup(args, results)
    originals = {key: (repo / path).read_bytes() for key, path in SOURCES.items()}
    hashes = {key: hashlib.sha256(value).hexdigest() for key, value in originals.items()}
    (results / 'source-hashes.json').write_text(json.dumps(hashes, indent=2) + '\n')

    def api(path, method='GET', body=None):
        request = urllib.request.Request(BASE + '/api/contents/' + quote(path, safe='/'), method=method,
            headers={'Authorization': 'token ' + token, 'Content-Type': 'application/json'},
            data=None if body is None else json.dumps(body).encode())
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    for attempt in range(90):
        try:
            api('')
            break
        except Exception:
            time.sleep(1)
    else:
        raise AssertionError('LC4RI Notebook server did not become ready')

    if args.saml is not None:
        metadata=args.saml.metadata().decode()
        api('ci-idp-metadata.xml','PUT',dict(type='file',format='text',content=metadata))
        (results/'identity-provider-metadata.xml').write_text(metadata)

    inventory = f'[ci_lc4ri]\n{args.master} public_ip={args.master}\n{args.worker}\n\n[ci_lc4ri:vars]\nansible_user=centos\nansible_ssh_private_key_file=~/.ssh/ansible_id_rsa\n'
    if not args.operationhub and not args.api_failures:
        api('hosts', 'PUT', dict(type='file', format='text', content=inventory))
        if args.input_failures:
            api('hosts-bad-user', 'PUT', dict(type='file', format='text', content=inventory.replace('ansible_user=centos', 'ansible_user=ci-invalid-user')))
    templates = {}
    for key, data in originals.items():
        templates[key] = json.loads(data)
        api(key + '-template.ipynb', 'PUT', dict(type='notebook', format='json', content=templates[key]))

    def passed(name):
        steps.append(name)
        print('LC4RI PASS:', name, flush=True)
        summary('running')

    def summary(status, error=None):
        (results / 'results.json').write_text(json.dumps(dict(status=status, passed=steps,
            operations=records, error=error), ensure_ascii=False, indent=2) + '\n')

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(viewport=dict(width=1600, height=1000))
        context.tracing.start(screenshots=True, snapshots=True, sources=True)
        page = None
        document = None
        key = None
        try:
            for key in SOURCES:
                original = templates[key]
                template_page = context.new_page()
                page = template_page
                template_page.goto(BASE + '/notebooks/' + key + '-template.ipynb?token=' + token)
                template_page.wait_for_function('window.Jupyter && Jupyter.notebook && Jupyter.notebook.kernel && Jupyter.notebook.kernel.is_connected()', timeout=120000)
                template_page.locator('#menubar .dropdown-toggle').filter(has_text=re.compile(r'^File$')).click()
                with context.expect_page() as copied:
                    template_page.locator('#copy_notebook').click()
                page = copied.value
                page.set_default_timeout(60000)
                page.wait_for_function('window.Jupyter && Jupyter.notebook && Jupyter.notebook.kernel && Jupyter.notebook.kernel.is_connected()', timeout=120000)
                expect(page.locator('button:has(.fa-unfreeze)')).to_be_visible()
                document = unquote(urlparse(page.url).path.split('/notebooks/', 1)[1])
                assert document != key + '-template.ipynb', document
                texts = page.evaluate('() => Jupyter.notebook.get_cells().map(c => c.get_text())')
                assert texts == [source(c) for c in original['cells']], 'Make a Copy changed the original cell sources'
                template_page.close()
                passed(key + ': created a working copy with Make a Copy; cell sources match the original')
                from notebook_controls import operations
                controls = operations(page, key, original, records, timeout=900000 if args.construction else 120000)
                find, state, select = controls.find, controls.state, controls.select
                unfreeze, edit, run = controls.unfreeze, controls.edit, controls.run
                edits = controls.edits

                if args.api_failures:
                    from api_failures import operate
                    operate(key, original, run, page, records, results, args.api_server)
                    passed(key + ': unavailable AWS API failed; restoring it and rerunning recovered')
                elif args.input_failures:
                    from input_failures import operate
                    operate(key, original, run, edit, page, results)
                    passed(key + ': rejected invalid SSH user; correction through the UI restored connectivity')
                elif key == 'D01':
                    index = find("target_group = 'cwhtest0001'")
                    edit(index, source(original['cells'][index]).replace("'cwhtest0001'", "'ci_lc4ri'"), 'Choose the fixture inventory group')
                    result = run(index)
                    output = str(result['outputs'])
                    assert args.master in output and args.worker in output and output.count('SUCCESS') >= 2, output
                    passed('D01: real SSH/Ansible connectivity succeeded for both selected hosts; executed cell froze')
                elif key == 'D02':
                    group_index = find("target_group = 'cwhtest0001'")
                    setting = find("nfs_server = '10.1.1.162'")
                    check = find('assert nfs_server in hosts')
                    workers = find('worker_servers = list')
                    for index, cell in enumerate(original['cells']):
                        if index >= setting:
                            break
                        if cell['cell_type'] != 'code' or not source(cell).strip():
                            continue
                        if index == group_index:
                            edit(index, source(cell).replace("'cwhtest0001'", "'ci_lc4ri'"), 'Choose the fixture inventory group')
                        run(index)
                    text = source(original['cells'][setting])
                    corrected = text.replace("nfs_server = '10.1.1.162'", 'nfs_server = ' + repr(args.master)).replace("master_server = '10.1.1.162'", 'master_server = ' + repr(args.master))
                    if args.construction:
                        edit(setting, corrected, 'Select the NFS and master hosts from the deployment inventory')
                        run(setting)
                        run(check)
                        passed('D02: selected deployment hosts and passed the original checks')
                    else:
                        bad = text.replace("nfs_server = '10.1.1.162'", "nfs_server = '192.0.2.254'").replace("master_server = '10.1.1.162'", 'master_server = ' + repr(args.master))
                        edit(setting, bad, 'Select an NFS host outside the inventory to exercise the documented recovery procedure')
                        frozen_setting = run(setting)
                        assert frozen_setting['frozen']
                        blocked_before = frozen_setting['message']
                        select(setting)
                        page.keyboard.press('Shift+Enter')
                        assert state(setting)['message'] == blocked_before, 'Frozen input cell was re-executed'
                        downstream = state(workers)['message']
                        failure = run(check, 'AssertionError')
                        assert 'nfs_server' in str(failure['outputs'])
                        assert state(workers)['message'] == downstream, 'The next operation executed after the failed check'
                        page.screenshot(path=str(results / 'D02-invalid-setting.png'), full_page=True)
                        passed('D02: frozen input cannot re-execute; invalid NFS selection fails at the original check')
                        edit(setting, corrected, 'Unfreeze and correct the setting as instructed by the original notebook')
                        run(setting)
                        run(check)
                        passed('D02: unfreeze, correct and rerun recovered the failed check')
                    for index, cell in enumerate(original['cells']):
                        if index <= check or cell['cell_type'] != 'code' or not source(cell).strip():
                            continue
                        if "hubname = 'cwhtest0001'" in source(cell):
                            edit(index, source(cell).replace("'cwhtest0001'", "'ci_lc4ri'"), 'Set the deployment identifier')
                        if args.saml is not None:
                            from saml_provider import configure_d02
                            configured=configure_d02(source(cell),args.saml)
                            if configured is not None:
                                edit(index,configured,'Enter the fixture IdP metadata and authorized federation group')
                        if args.construction and "group_vars['jupyterhub_image'] = " in source(cell):
                            edit(index, re.sub(r"group_vars\['jupyterhub_image'\] = .+", "group_vars['jupyterhub_image'] = " + repr(os.environ['LC4RI_HUB_IMAGE']), source(cell)), 'Select the original Hub image with the fixture CA added to its trust store')
                        if "group_vars['master_fqdn'] = 'example.com'" in source(cell):
                            edit(index, source(cell).replace("'example.com'", "'coursewarehub.test'"), 'Set the service hostname')
                        run(index)
                    passed('D02: completed the remaining original procedure and generated inventory/group variables')

                elif args.operationhub:
                    from operationhub import operate
                    operate(original, args, run, edit, select, state, page, records, results)
                    passed('OperationHub: completed the original AWS construction procedure')
                else:
                    from construction import operate
                    operate(key, original, args, run, edit, select, state, page, records, results)
                    passed(key + ': completed original procedure through LC4RI UI')

                page.keyboard.press('Control+s')
                page.wait_for_function('!Jupyter.notebook.dirty')
                saved = api(document)['content']
                count = len(original['cells'])
                assert len(saved['cells']) in (count, count + 1), 'Unexpected cells added to working copy'
                if len(saved['cells']) > count:
                    extra = saved['cells'][-1]
                    assert extra['cell_type'] == 'code' and source(extra) == '' and not extra.get('outputs')
                    records.append(dict(notebook=key, action='Shift+Enter appended an empty cell at the end'))
                for index, cell in enumerate(saved['cells'][:count]):
                    assert source(cell) == edits.get(index, source(original['cells'][index])), (key, index)
                (results / (key + '-working.ipynb')).write_text(json.dumps(saved, ensure_ascii=False, indent=1) + '\n')
                page.screenshot(path=str(results / (key + '-completed.png')), full_page=True)
                page.close()
                page = None
                if args.construction and key == 'D05':
                    from construction import check_runtime
                    check_runtime(args, results / 'before-update', browser)
                    passed('Constructed CoursewareHub: browser login, user kernel, D06 and persistence')

            if args.api_failures:
                passed('Both AWS construction notebooks: API connection failure and recovery')
            elif args.input_failures:
                passed('All on-premise construction notebooks: invalid SSH setting and recovery')
            elif args.operationhub:
                from operationhub import check
                check(args, results, browser)
                passed('Constructed OperationHub: login, original construction notebooks and user kernel')
            else:
                generated = api('ci_lc4ri_inventory')['content']
                assert args.master in generated and args.worker in generated, generated
                group_vars = yaml.safe_load(api('group_vars/ci_lc4ri')['content'])
                assert group_vars['nfsserver'] == args.master, group_vars
                assert group_vars['master_fqdn'] == 'coursewarehub.test', group_vars
                assert group_vars['jupyterhub_singleuser_app'] == 'jupyter_server.serverapp.ServerApp'
                assert group_vars['jupyterhub_singleuser_default_url'] == '/tree'
                assert group_vars['spawner_start_timeout'] == 300
                if args.construction:
                    assert group_vars['jupyterhub_image'] == os.environ['LC4RI_HUB_IMAGE']
                inventory_json = json.loads(subprocess.check_output(['docker', 'exec', 'cwh-lc4ri-controller',
                    'ansible-inventory', '-i', '/home/jovyan/work/ci_lc4ri_inventory', '--list'], text=True))
                assert inventory_json['cwhub_master']['hosts'] == [args.master], inventory_json
                assert inventory_json['cwhub_nodes']['hosts'] == [args.worker], inventory_json
                assert inventory_json['cwhub_nfs_server']['hosts'] == [args.master], inventory_json
                connected = subprocess.check_output(['docker', 'exec', 'cwh-lc4ri-controller', 'ansible',
                    '-i', '/home/jovyan/work/ci_lc4ri_inventory', 'ci_lc4ri', '-m', 'ping'], text=True)
                assert connected.count('SUCCESS') == 2, connected
                (results / 'generated-inventory.json').write_text(json.dumps(inventory_json, indent=2) + '\n')
                passed('Generated inventory has the requested roles and supports real SSH; upstream server defaults are preserved')
                if args.construction:
                    from construction import check_runtime
                    check_runtime(args, results / 'after-update', browser, after_update=True)
                    passed('CoursewareHub construction and runtime acceptance')
            for key, data in originals.items():
                assert (repo / SOURCES[key]).read_bytes() == data, 'Tracked Notebook original changed'
            summary('passed')
        except Exception as error:
            if page is not None:
                page.screenshot(path=str(results / 'failure.png'), full_page=True)
                (results / 'failure.html').write_text(page.content())
                try:
                    saved = page.evaluate('() => Jupyter.notebook.toJSON()')
                    (results / (str(key) + '-failure.ipynb')).write_text(json.dumps(saved, ensure_ascii=False, indent=1) + '\n')
                except Exception:
                    pass
            summary('failed', str(error))
            raise
        finally:
            context.tracing.stop(path=str(results / 'browser-trace.zip'))
            browser.close()
            args.fixtures.close()


if __name__ == '__main__':
    main()
