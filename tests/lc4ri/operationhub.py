"""Operate the original AWS OperationHub construction procedure through LC4RI."""
import json
import os
import re
import secrets
import subprocess
import time
from urllib.parse import quote

from notebook_ui import BASE, source
from browser_evidence import recorded_context, finish_recording


def setup(args, results):
    from aws_guest_provider import GuestBinding, running_aws_provider
    from guest import Guest
    binding=GuestBinding('operationhub', Guest('operationhub',args.master,'ssh'), results/'operationhub-console.log')
    bindings=[binding]+[GuestBinding(role,Guest(role,address,'ssh'),results/(role+'-console.log'))
        for role,address in [('master','10.1.1.10'),('worker1','10.1.1.11'),('worker2','10.1.1.12')]]
    args.provider=args.fixtures.enter_context(running_aws_provider(bindings, results/'aws-api', os.environ['LC4RI_AWS_HOST'],port=18503))
    args.operation_password=secrets.token_hex(24)


def operate(original,args,run,edit,select,state,page,records,results):
    import boto3
    ec2=boto3.client('ec2',region_name='ap-northeast-1',aws_access_key_id='testing',aws_secret_access_key='testing',endpoint_url=args.provider['endpoint'])
    group='ci-operationhub'
    for index,cell in enumerate(original['cells']):
        text=source(cell)
        if cell['cell_type']!='code' or not text.strip():continue
        if "cloudformation_stack_name = 'ophubtest0001'" in text:
            edit(index,text.replace('ophubtest0001',group),'Set the test CloudFormation stack name')
        if "resource_prefix = 'ophubtest'" in text:
            edit(index,text.replace('ophubtest','ci-operationhub'),'Set the test resource prefix')
        if re.search(r'^image_id = ',text,re.M):
            edit(index,re.sub(r'^image_id = .+$','image_id = '+repr(args.provider['image_id']),text,flags=re.M),'Select the AMI registered by the CI guest provider')
        if text.strip().endswith('assert False'):
            select(index)
            page.locator('button:has(.fa-freeze)').click()
            assert state(index)['frozen']
            records.append(dict(notebook='OperationHub',cell=index,action='confirm inputs and Freeze via toolbar'))
            continue
        # The procedure asks the operator to add rules using the AWS console.
        # Use the emulated EC2 API for that external action; the next original cell verifies it.
        if 'sg_result = !aws ec2 describe-security-groups' in text:
            groups=ec2.describe_security_groups(Filters=[{'Name':'tag:aws:cloudformation:stack-name','Values':[group]}])['SecurityGroups']
            if not groups:
                groups=[g for g in ec2.describe_security_groups()['SecurityGroups'] if g.get('GroupName','')!='default']
            assert len(groups)==1, groups
            port=443 if 'HTTPS' in text else 22
            ec2.authorize_security_group_ingress(GroupId=groups[0]['GroupId'],IpPermissions=[dict(IpProtocol='tcp',FromPort=port,ToPort=port,IpRanges=[{'CidrIp':os.environ['LC4RI_AWS_HOST']+'/32'}])])
            records.append(dict(notebook='OperationHub',action='add operator-required EC2 ingress rule',port=port))
        if 'hosts-backup ./hosts' in text:
            expected=f'[{group}]\n{args.master} ansible_ssh_user=centos ansible_ssh_private_key_file=~/.ssh/ansible_id_rsa\n'
            editor=page.context.new_page()
            editor.goto(BASE+'/edit/hosts')
            editor.locator('.CodeMirror').click()
            editor.keyboard.press('Control+A')
            editor.keyboard.insert_text(expected)
            assert editor.locator('.CodeMirror').evaluate('e=>e.CodeMirror.getValue()')==expected
            editor.keyboard.press('Control+s')
            import urllib.request
            for attempt in range(100):
                request=urllib.request.Request(BASE+'/api/contents/hosts',headers={'Authorization':'token '+os.environ['LC4RI_TEST_TOKEN']})
                with urllib.request.urlopen(request) as response:
                    if json.load(response)['content']==expected:break
                editor.wait_for_timeout(100)
            else:raise AssertionError('Edited hosts file was not saved')
            editor.close()
            records.append(dict(notebook='OperationHub',action='edit hosts through the file editor',after=expected))
        inputs=(args.operation_password,) if 'admin_password = getpass.getpass()' in text else ()
        observed=run(index,inputs=inputs)
        output=json.dumps(observed['outputs'],ensure_ascii=False)
        if re.search(r'FAILED!|UNREACHABLE!|failed=[1-9]',output):
            raise AssertionError(f'OperationHub cell {index}: operation failed: {output[-3000:]}')



def provision_coursewarehub(args, results, page, fetch, base, prefix, original):
    """Follow the deployed AWS D01 on the OperationHub that the original built."""
    from notebook_controls import operations
    records=[]
    copied=None
    path=None
    try:
        page.goto(base+prefix+'notebooks/'+quote('D01_CoursewareHub用VMをAWSに作成.ipynb'))
        page.wait_for_function('window.Jupyter && Jupyter.notebook && Jupyter.notebook.kernel && Jupyter.notebook.kernel.is_connected()',timeout=120000)
        page.locator('#menubar .dropdown-toggle').filter(has_text=re.compile(r'^File$')).click()
        with page.context.expect_page() as opened:
            page.locator('#copy_notebook').click()
        copied=opened.value
        copied.set_default_timeout(60000)
        copied.wait_for_function('window.Jupyter && Jupyter.notebook && Jupyter.notebook.kernel && Jupyter.notebook.kernel.is_connected()',timeout=120000)
        from urllib.parse import unquote
        path=unquote(copied.url.split('/notebooks/',1)[1].split('?',1)[0])
        assert path!='D01_CoursewareHub用VMをAWSに作成.ipynb'
        assert copied.evaluate('()=>Jupyter.notebook.get_cells().map(c=>c.get_text())')==[source(c) for c in original['cells']]
        controls=operations(copied,'CoursewareHub-AWS-D01',original,records,timeout=900000)
        import boto3
        ec2=boto3.client('ec2',region_name='ap-northeast-1',aws_access_key_id='testing',aws_secret_access_key='testing',endpoint_url=args.provider['endpoint'])
        group='ci-coursewarehub'
        inventory=None
        for index,cell in enumerate(original['cells']):
            text=source(cell)
            if cell['cell_type']!='code' or not text.strip():continue
            if "cloudformation_stack_name = 'cwhtest0001'" in text:
                controls.edit(index,text.replace('cwhtest0001',group),'Set the CoursewareHub stack name')
            if "resource_prefix = 'cwhtest0001'" in text:
                controls.edit(index,text.replace('cwhtest0001',group),'Set the CoursewareHub resource prefix')
            if re.search(r'^image_id = ',text,re.M):
                controls.edit(index,re.sub(r'^image_id = .+$','image_id = '+repr(args.provider['image_id']),text,flags=re.M),'Select the registered guest AMI')
            if text.strip().endswith('assert False'):
                controls.select(index)
                copied.locator('button:has(.fa-freeze)').click()
                assert controls.state(index)['frozen']
                records.append(dict(notebook='CoursewareHub-AWS-D01',cell=index,action='confirm inputs and Freeze via toolbar'))
                continue
            if 'sg_result = !aws ec2 describe-security-groups' in text or '全てのトラフィックを自身のセキュリティグループ' in text:
                groups=ec2.describe_security_groups(Filters=[{'Name':'tag:aws:cloudformation:stack-name','Values':[group]}])['SecurityGroups']
                assert len(groups)==1,groups
                sg=groups[0]['GroupId']
                if 'sg_result' in text:
                    permissions=[dict(IpProtocol='tcp',FromPort=22,ToPort=22,IpRanges=[{'CidrIp':args.master+'/32'}])]
                else:
                    permissions=[dict(IpProtocol='-1',UserIdGroupPairs=[{'GroupId':sg}])]
                ec2.authorize_security_group_ingress(GroupId=sg,IpPermissions=permissions)
                records.append(dict(notebook='CoursewareHub-AWS-D01',action='add operator-required EC2 ingress rule',permissions=permissions))
            if 'hosts-backup ./hosts' in text:
                assert inventory is not None
                previous=fetch(prefix+'api/contents/hosts')['content']
                expected=previous.rstrip()+'\n\n'+inventory
                editor=page.context.new_page()
                editor.goto(base+prefix+'edit/hosts')
                editor.locator('.CodeMirror').click()
                editor.keyboard.press('Control+A')
                editor.keyboard.insert_text(expected)
                assert editor.locator('.CodeMirror').evaluate('e=>e.CodeMirror.getValue()')==expected
                editor.keyboard.press('Control+s')
                for attempt in range(100):
                    if fetch(prefix+'api/contents/hosts')['content']==expected:break
                    editor.wait_for_timeout(100)
                else:raise AssertionError('Edited OperationHub inventory was not saved')
                editor.close()
                records.append(dict(notebook='CoursewareHub-AWS-D01',action='copy generated inventory through the file editor',after=expected))
            observed=controls.run(index)
            output=json.dumps(observed['outputs'],ensure_ascii=False)
            assert not re.search(r'FAILED!|UNREACHABLE!|failed=[1-9]',output),(index,output[-3000:])
            if "print('[{}]'.format(cloudformation_stack_name))" in text:
                inventory=''.join(o.get('text','') for o in observed['outputs'] if o['output_type']=='stream')
                inventory='\n'.join(inventory.splitlines())+'\n'
                assert '['+group+']' in inventory and all(address in inventory for address in ('10.1.1.10','10.1.1.11','10.1.1.12')),inventory
            if '!ansible -m ping' in text:
                assert output.count('SUCCESS')==3,(index,output)
        copied.keyboard.press('Control+s')
        copied.wait_for_function('!Jupyter.notebook.dirty')
        saved=fetch(prefix+'api/contents/'+quote(path))['content']
        count=len(original['cells'])
        assert len(saved['cells']) in (count,count+1)
        if len(saved['cells'])>count:
            extra=saved['cells'][-1]
            assert extra['cell_type']=='code' and source(extra)=='' and not extra.get('outputs')
        for index,cell in enumerate(saved['cells'][:count]):
            assert source(cell)==controls.edits.get(index,source(original['cells'][index])),index
        (results/'operationhub-aws-working.ipynb').write_text(json.dumps(saved,ensure_ascii=False,indent=1))
        (results/'operationhub-aws-hosts').write_text(fetch(prefix+'api/contents/hosts')['content'])
        deployed=fetch(prefix+'api/contents/'+quote('D01_CoursewareHub用VMをAWSに作成.ipynb'))['content']
        assert [source(c) for c in deployed['cells']]==[source(c) for c in original['cells']]
        copied.screenshot(path=str(results/'operationhub-aws-completed.png'),full_page=True)
        print('LC4RI PASS: constructed OperationHub executed original AWS D01; three allocated guests passed real SSH/Ansible',flush=True)
    except Exception:
        if copied is not None:
            copied.screenshot(path=str(results/'operationhub-aws-failure.png'),full_page=True)
            saved=copied.evaluate('()=>Jupyter.notebook.toJSON()')
            (results/'operationhub-aws-failure.ipynb').write_text(json.dumps(saved,ensure_ascii=False,indent=1))
        raise
    finally:
        (results/'operationhub-aws-operations.json').write_text(json.dumps(records,ensure_ascii=False,indent=2))
        if copied is not None:copied.close()


def check(args, results, browser):
    from playwright.sync_api import expect
    videos=results/'videos'/'operationhub-runtime'
    context=recorded_context(browser, videos)
    context.tracing.start(screenshots=True,snapshots=True,sources=True)
    page=context.new_page()
    base='https://'+args.master
    try:
        page.goto(base+'/hub/login')
        page.locator('#username_input').fill('ohadmin')
        page.locator('#password_input').fill(args.operation_password)
        page.locator('#login_submit').click()
        page.wait_for_url(lambda url:'/hub/login' not in str(url),timeout=60000)
        page.goto(base+'/hub/home')
        page.locator('#start').click()
        page.wait_for_url('**/user/ohadmin/**',timeout=600000)
        page.wait_for_function('window.Jupyter && Jupyter.notebook_list',timeout=120000)
        page.screenshot(path=str(results/'operationhub-tree.png'),full_page=True)
        expect(page.get_by_text('D01_CoursewareHub用VMをAWSに作成.ipynb',exact=True)).to_be_visible()
        def fetch(path,method='GET',body=None):
            value=page.evaluate("""async ({path,method,body})=>{
                const c=document.cookie.match(/(?:^|; )_xsrf=([^;]*)/);
                const r=await fetch(path,{method,headers:{'Content-Type':'application/json','X-XSRFToken':c?decodeURIComponent(c[1]):''},body:body===null?undefined:JSON.stringify(body)});
                return {status:r.status,text:await r.text()};}""",dict(path=path,method=method,body=body))
            assert 200<=value['status']<300,value
            return json.loads(value['text']) if value['text'] else None
        prefix='/user/ohadmin/'
        recipe=fetch(prefix+'api/contents/'+quote('D01_CoursewareHub用VMをAWSに作成.ipynb'))['content']
        original=json.loads(__import__('pathlib').Path('coursewarehub/D01_CoursewareHub用VMをAWSに作成.ipynb').read_text())
        assert [source(c) for c in recipe['cells']]==[source(c) for c in original['cells']]
        notebook={'nbformat':4,'nbformat_minor':5,'metadata':{'kernelspec':{'name':'python3','display_name':'Python 3','language':'python'}},
            'cells':[{'cell_type':'code','id':'runtime-check','metadata':{},'execution_count':None,'outputs':[],'source':''}]}
        fetch(prefix+'api/contents/lc4ri-operationhub.ipynb','PUT',{'type':'notebook','format':'json','content':notebook})
        page.goto(base+prefix+'notebooks/lc4ri-operationhub.ipynb')
        page.wait_for_function('window.Jupyter && Jupyter.notebook && Jupyter.notebook.kernel && Jupyter.notebook.kernel.is_connected()',timeout=120000)
        page.locator('.cell').first.locator('.CodeMirror').click()
        page.keyboard.insert_text('import os\nassert os.getuid() > 0\nprint("OPERATIONHUB_RESULT", 6 * 7)')
        page.keyboard.press('Shift+Enter')
        expect(page.locator('.cell').first.locator('.output')).to_contain_text('OPERATIONHUB_RESULT 42',timeout=120000)
        page.keyboard.press('Control+s')
        page.wait_for_function('!Jupyter.notebook.dirty')
        (results/'operationhub-kernel.ipynb').write_text(json.dumps(fetch(prefix+'api/contents/lc4ri-operationhub.ipynb')['content'],ensure_ascii=False,indent=1))
        provision_coursewarehub(args,results,page,fetch,base,prefix,original)
        (results/'operationhub-runtime.json').write_text(json.dumps({'login':'passed','user_server':'passed','construction_notebooks_deployed':'passed','user_kernel':'passed','aws_guest_provisioning_and_real_ssh':'passed'}))
    except Exception:
        page.screenshot(path=str(results/'operationhub-runtime-failure.png'),full_page=True)
        raise
    finally:
        finish_recording(context, results/'operationhub-runtime-trace.zip', videos)
