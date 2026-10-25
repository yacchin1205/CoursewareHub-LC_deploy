"""Verify the deployed Hub by logging in, running a user kernel, and restarting it."""
import json
from pathlib import Path
import time
from urllib.parse import quote

from playwright.sync_api import expect

from construction import remote



def spawn_user(page, base, username, image=None):
    response=page.goto(base+'/hub/spawn',wait_until='domcontentloaded')
    assert response is not None and response.status<400,page.locator('body').inner_text()
    choices=page.locator('input[name="image"]')
    if choices.count():
        if image is not None:
            page.locator('input[name="image"][value="'+image+'"]').check()
        else:
            assert page.locator('input[name="image"]:checked').count()==1
        page.locator('input[type="submit"], button[type="submit"]').click()
    page.wait_for_url('**/user/'+username+'/**',timeout=600000)
    page.wait_for_function('window.Jupyter && Jupyter.notebook_list',timeout=120000)


def stop_user(page, base, username, fetch, hosts):
    page.goto(base+'/hub/home')
    page.locator('#stop').click()
    for attempt in range(90):
        user=fetch('/hub/api/user')
        if not user.get('server') and '' not in user.get('servers',{}):break
        time.sleep(2)
    else:raise AssertionError('User server did not stop')
    for attempt in range(90):
        if all(not remote(host,"docker ps -q --filter label=com.docker.swarm.service.name=jupyter-"+username) for host in hosts):break
        time.sleep(2)
    else:raise AssertionError('Old user container did not exit')


def check(args, results, browser, *, after_update=False):
    results.mkdir(parents=True, exist_ok=True)
    base = 'https://coursewarehub.test'
    steps = []
    def passed(text):
        steps.append(text)
        print('RUNTIME PASS:', text, flush=True)
        save('running')
    def save(status, error=None):
        (results / 'runtime.json').write_text(json.dumps(dict(status=status, passed=steps, error=error), indent=2))
    context = browser.new_context(ignore_https_errors=True, viewport={'width':1600,'height':1000})
    context.tracing.start(screenshots=True, snapshots=True, sources=True)
    page = context.new_page()
    page.set_default_timeout(60000)
    try:
        if args.saml is not None:
            from saml_provider import launch_saml
            args.saml.mode='valid'
            launch_saml(page,args.saml)
        else:
            page.goto(base + '/php/login.php')
            page.locator('input[name="email"]').fill('ciadmin@example.test')
            page.locator('input[name="password"]').fill(args.admin_password)
            page.locator('input[type="submit"], button[type="submit"]').click()
        page.wait_for_function("location.pathname.startsWith('/hub/') || location.pathname.startsWith('/user/')", timeout=120000)
        page.goto(base + '/hub/home')
        def fetch(path, method='GET', body=None):
            value = page.evaluate('''async ({path,method,body}) => {
                const c=document.cookie.match(/(?:^|; )_xsrf=([^;]*)/);
                const token=(window.jhdata && jhdata.xsrf_token) || (c ? decodeURIComponent(c[1]):'');
                const r=await fetch(path,{method,headers:{'Content-Type':'application/json','X-XSRFToken':token},
                    body:body===null?undefined:JSON.stringify(body)});
                return {status:r.status,text:await r.text()};}''',dict(path=path,method=method,body=body))
            assert 200 <= value['status'] < 300, value
            return json.loads(value['text']) if value['text'] else None
        user=fetch('/hub/api/user')
        assert user['admin'], user
        username=user['name']
        args.teacher_username=username
        prefix='/user/'+quote(username,safe='')+'/'
        passed('administrator logged into the constructed Hub')
        repository=getattr(args,'build_repository',None)
        def spawn():
            spawn_user(page,base,username,repository['image'] if repository else None)
        if repository and not after_update:
            from custom_image import build_environment
            from guest import Guest
            build_environment(page,base,repository,results,Guest('master',args.master,'ssh'))
            passed('Git source was built through the environment-management UI and selected as the default image')
            page.goto(base+'/hub/home')
            current=fetch('/hub/api/user')
            if current.get('server') or '' in current.get('servers',{}):
                stop_user(page,base,username,fetch,(args.master,args.worker))
        spawn()
        passed('user server started and Notebook tree rendered')
        if after_update:
            assert fetch(prefix+'api/contents/lc4ri-persist.txt')['content']=='persistent-user-data'
            passed('user data survived the O01 update')
        notebook={'nbformat':4,'nbformat_minor':5,'metadata':{'kernelspec':{'name':'python3','language':'python','display_name':'Python 3'}},
            'cells':[{'cell_type':'code','id':'runtime-check','metadata':{},'execution_count':None,'outputs':[],'source':''}]}
        fetch(prefix+'api/contents/lc4ri-runtime.ipynb','PUT',{'type':'notebook','format':'json','content':notebook})
        page.goto(base+prefix+'notebooks/lc4ri-runtime.ipynb')
        page.wait_for_function('window.Jupyter && Jupyter.notebook && Jupyter.notebook.kernel && Jupyter.notebook.kernel.is_connected()',timeout=120000)
        uid=int(remote(args.master, 'id -u '+username))
        assert uid > 0
        code=('import os\nfrom pathlib import Path\n'+f'assert os.getuid() == {uid}\n'+
            'Path("lc4ri-persist.txt").write_text("persistent-user-data")\nprint("LC4RI_RESULT", 6 * 7)')
        if repository:
            from custom_image import marker_check
            code=code.replace('print("LC4RI_RESULT", 6 * 7)',marker_check(repository)+'print("LC4RI_RESULT", 6 * 7)')
        page.locator('.cell').first.locator('.CodeMirror').click()
        page.keyboard.insert_text(code)
        page.keyboard.press('Shift+Enter')
        expect(page.locator('.cell').first.locator('.output')).to_contain_text('LC4RI_RESULT 42',timeout=120000)
        page.keyboard.press('Control+s')
        page.wait_for_function('!Jupyter.notebook.dirty')
        saved=fetch(prefix+'api/contents/lc4ri-runtime.ipynb')['content']
        (results/'runtime-working.ipynb').write_text(json.dumps(saved,ensure_ascii=False,indent=1))
        page.screenshot(path=str(results/'executed-cell.png'),full_page=True)
        passed('browser ran Python under the provisioned user UID and saved the Notebook')
        if repository:
            passed('the provisioned user kernel ran inside the repo2docker-built image')
        keys = [host+' '+remote(host,'cat /etc/ssh/ssh_host_ed25519_key.pub') for host in (args.master,args.worker)]
        import shlex
        home='/jupyter/users/'+username
        remote(args.master, "sh -c " + shlex.quote("printf '%s\\n' " + ' '.join(shlex.quote(key) for key in keys) + " > " + home + "/.ssh/known_hosts"))
        remote(args.master,'chown '+username+':'+username+' '+home+'/.ssh/known_hosts')
        # D06 is already placed in the administrator home by D05.
        page.goto(base+prefix+'notebooks/'+quote('D06_CoursewareHubコンテンツの配備.ipynb'))
        page.wait_for_function('window.Jupyter && Jupyter.notebook && Jupyter.notebook.kernel && Jupyter.notebook.kernel.is_connected()',timeout=120000)
        original=page.evaluate('() => Jupyter.notebook.get_cells().map(c=>({type:c.cell_type,text:c.get_text()}))')
        from notebook_ui import source
        canonical=json.loads((Path(__file__).resolve().parents[2]/'coursewarehub/D06_CoursewareHubコンテンツの配備.ipynb').read_text())
        assert original==[dict(type=c['cell_type'],text=source(c)) for c in canonical['cells']], 'Deployed D06 differs from the original source'
        page.locator('#menubar .dropdown-toggle').filter(has_text='File').click()
        with context.expect_page() as copying:
            page.locator('#copy_notebook').click()
        page=copying.value
        page.wait_for_function('window.Jupyter && Jupyter.notebook && Jupyter.notebook.kernel && Jupyter.notebook.kernel.is_connected()',timeout=120000)
        assert page.evaluate('() => Jupyter.notebook.get_cells().map(c=>({type:c.cell_type,text:c.get_text()}))')==original
        from notebook_controls import operations
        d06_records=[]
        controls=operations(page,'D06',canonical,d06_records,timeout=300000)
        for index,cell in enumerate(original):
            if cell['type']!='code' or not cell['text'].strip():continue
            observed=controls.run(index)
            if '-CD' not in cell['text']:
                assert not any('FAILED!' in str(o) or 'UNREACHABLE!' in str(o) for o in observed['outputs']),observed['outputs']
        assert page.evaluate('()=>Jupyter.notebook.get_cells().slice(0,'+str(len(original))+').map(c=>({type:c.cell_type,text:c.get_text()}))')==original
        (results/'D06-operations.json').write_text(json.dumps(d06_records,indent=2)+'\n')
        page.keyboard.press('Control+s')
        page.wait_for_function('!Jupyter.notebook.dirty')
        (results/'D06-working.ipynb').write_text(json.dumps(page.evaluate('()=>Jupyter.notebook.toJSON()'),ensure_ascii=False,indent=1))
        assert fetch(prefix+'api/contents/admin_tools/00_GuidanceForTeacher.ipynb')['type']=='notebook'
        passed('original D06 ran through browser operations and deployed teacher tools')
        stop_user(page,base,username,fetch,(args.master,args.worker))
        spawn()
        assert fetch(prefix+'api/contents/lc4ri-persist.txt')['content']=='persistent-user-data'
        passed('saved user data survived user-server stop and start')
        save('passed')
    except Exception as error:
        save('failed',str(error))
        page.screenshot(path=str(results/'runtime-failure.png'),full_page=True)
        (results/'runtime-failure.html').write_text(page.content())
        try:
            notebook=page.evaluate('()=>Jupyter.notebook.toJSON()')
            (results/'runtime-failure.ipynb').write_text(json.dumps(notebook,ensure_ascii=False,indent=1))
        except Exception:
            pass
        raise
    finally:
        context.tracing.stop(path=str(results/'browser-trace.zip'))
        context.close()
