"""Run unmodified construction notebooks with representative invalid inputs and command failures."""
import argparse
import json
import os
from pathlib import Path
import tempfile
import traceback
from unittest.mock import patch

import nbformat
import papermill as pm


NOTEBOOKS = {
    'operationhub-d01': 'D01_OperationHubをAWSに構築.ipynb',
    'coursewarehub-aws-d01': 'coursewarehub/D01_CoursewareHub用VMをAWSに作成.ipynb',
    'coursewarehub-onprem-d01': 'coursewarehub/D01_オンプレミス環境の場合のマシン設定.ipynb',
    'coursewarehub-d02': 'coursewarehub/D02_CoursewareHubインベントリの準備.ipynb',
    'coursewarehub-d03': 'coursewarehub/D03_NFSの準備.ipynb',
    'coursewarehub-d04': 'coursewarehub/D04_Docker Swarmの準備.ipynb',
    'coursewarehub-d05': 'coursewarehub/D05_CoursewareHubの準備.ipynb',
    'coursewarehub-d06': 'coursewarehub/D06_CoursewareHubコンテンツの配備.ipynb',
    'coursewarehub-o01': 'coursewarehub/O01_CoursewareHubの更新.ipynb',
}


def cases(notebook):
    if notebook == 'operationhub-d01':
        return [('unconfirmed-construction', {}, 'AssertionError', 'assert construction_parameters_confirmed', ''),
                ('aws-command-failure', {}, 'RuntimeError', "aws('ec2 describe-vpcs')", 'INJECTED_COMMAND_FAILURE')]
    if notebook == 'coursewarehub-aws-d01':
        return [('missing-operationhub-stack', {'operationhub_stack_path': 'missing-stack.json'}, 'AssertionError', 'assert Path(os.path.expanduser(operationhub_stack_path)).is_file()', ''),
                ('unconfirmed-construction', {}, 'AssertionError', 'assert construction_parameters_confirmed', ''),
                ('aws-command-failure', {}, 'RuntimeError', "aws('ec2 describe-vpcs')", 'INJECTED_COMMAND_FAILURE')]
    if notebook == 'coursewarehub-onprem-d01':
        return [('missing-inventory', {'inventory_path': 'missing-inventory'}, 'AssertionError', 'assert os.path.isfile(inventory_path)', ''),
                ('empty-target-group', {'target_group': 'empty'}, 'AssertionError', 'assert inventory_hosts(target_group)', ''),
                ('ssh-rejection', {}, 'CalledProcessError', "subprocess.run(['ansible'", '')]
    if notebook == 'coursewarehub-d02':
        return [('missing-master', {'master_server': '192.0.2.99'}, 'AssertionError', 'assert master_server in hosts', 'master_server'),
                ('missing-nfs-server', {'nfs_server': '192.0.2.99'}, 'AssertionError', 'assert nfs_server in hosts', 'nfs_server'),
                ('missing-public-ip', {'fixture_missing_public_ip': True}, 'AssertionError', "get('public_ip')", 'public_ip'),
                ('unknown-service-host', {'servicenet_ips': {'192.0.2.99': '192.0.2.99'}}, 'AssertionError', 'for ip in servicenet_ips.keys()', ''),
                ('connection-command-failure', {}, 'CalledProcessError', "subprocess.run(['ansible'", '')]
    if notebook == 'coursewarehub-d03':
        return [('missing-target-group', {'target_group': 'empty'}, 'AssertionError', 'assert inventory.get(target_group)', 'Missing inventory group'),
                ('multiple-nfs-servers', {'fixture_multiple_servers': True}, 'AssertionError', 'assert len(servers) == 1', 'exactly one'),
                ('connection-command-failure', {}, 'CalledProcessError', "ansible(['-m', 'ping'], target_group)", '')]
    if notebook == 'coursewarehub-d04':
        return [('empty-target-group', {'target_group': 'empty'}, 'AssertionError', 'assert inventory_hosts(target_group)', 'Empty inventory group'),
                ('connection-command-failure', {}, 'CalledProcessError', "ansible(['-m', 'ping'], target_group)", '')]
    if notebook in {'coursewarehub-d05', 'coursewarehub-o01'}:
        return [('multiple-hub-hosts', {'fixture_multiple_servers': True}, 'AssertionError', 'assert len(hub_hosts) == 1', ''),
                ('connection-command-failure', {}, 'CalledProcessError', "checked_system('ansible", '')]
    if notebook == 'coursewarehub-d06':
        return [('missing-inventory', {'inventory_path': 'missing-inventory'}, 'AssertionError', 'assert os.path.isfile(inventory_path)', ''),
                ('connection-command-failure', {}, 'CalledProcessError', "run(['ansible'", '')]
    raise ValueError(notebook)


def run_case(repo, notebook, case, results, ssh_host, ssh_key):
    case_id, overrides, error_type, source_marker, message = case
    directory = results / case_id
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='notebook-failure-') as temp:
        work = Path(temp)
        overrides = dict(overrides)
        multiple = overrides.pop('fixture_multiple_servers', False)
        public_ip = '' if overrides.pop('fixture_missing_public_ip', False) else ' public_ip=192.0.2.10'
        inventory = work / 'hosts'
        inventory.write_text('[failure_test:children]\ncwhub_master\ncwhub_nodes\ncwhub_nfs_server\n'
            '[cwhub_master]\n192.0.2.10' + public_ip + '\n' + ('192.0.2.11\n' if multiple else '') +
            '[cwhub_nodes]\n192.0.2.11\n[cwhub_nfs_server]\n192.0.2.10\n' +
            ('192.0.2.11\n' if multiple else '') +
            '[failure_test:vars]\nansible_user=centos\nservicenet_ip=192.0.2.10\nnfsnodes_group=cwhub_nodes\nnfs_server_role=nfs_server\n')
        stack = work / 'operationhub-stack.json'
        stack.write_text(json.dumps([{'OutputKey': name, 'OutputValue': 'fixture'}
            for name in ['OpHubVPC', 'OpHubSubnet', 'OpHubSecurityGroup']]))
        parameters = dict(hosts_file=str(inventory), inventory_path=str(inventory),
            target_group='failure_test', group_vars_dir=str(work / 'group_vars'), hubname='failure_test',
            nfs_server='192.0.2.10', master_server='192.0.2.10',
            notebook_runtime_path=str(repo / 'coursewarehub/notebook_runtime.py'),
            operationhub_stack_path=str(stack))
        # Only inject parameters declared by the original notebook.
        source = nbformat.read(repo / NOTEBOOKS[notebook], as_version=4)
        declarations = '\n'.join(c.source for c in source.cells if 'parameters' in c.metadata.get('tags', []))
        parameters = {k: v for k, v in parameters.items() if k + ' =' in declarations}
        parameters.update(overrides)
        commands = directory / 'commands.jsonl'
        commands.write_text('')
        bin_dir = work / 'bin'
        bin_dir.mkdir()
        mode = 'command-failure' if case_id.endswith('command-failure') else 'input-validation'
        if case_id == 'ssh-rejection':
            assert ssh_host and ssh_key, 'A live SSH fixture is required'
            inventory.write_text('[failure_test]\n' + ssh_host +
                ' ansible_user=nonexistent_ci_user ansible_ssh_private_key_file=' + str(ssh_key) +
                ' ansible_ssh_timeout=5\n')
        for command in ['aws', 'ansible']:
            if case_id == 'ssh-rejection' and command == 'ansible':
                continue
            (bin_dir / command).symlink_to(repo / 'tests/notebooks/failing-command.py')
        env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ['PATH'],
                   NOTEBOOK_FAILURE_MODE=mode, NOTEBOOK_COMMAND_LOG=str(commands))
        output = directory / Path(NOTEBOOKS[notebook]).name
        try:
            with patch.dict(os.environ, env):
                pm.execute_notebook(str(repo / NOTEBOOKS[notebook]), str(output), parameters=parameters,
                    cwd=str(work), kernel_name='python3', execution_timeout=60)
        except pm.PapermillExecutionError:
            executed = nbformat.read(output, as_version=4)
            failures = [(i, c, o) for i, c in enumerate(executed.cells) for o in c.get('outputs', [])
                        if o.output_type == 'error']
            assert len(failures) == 1, failures
            index, cell, error = failures[0]
            assert error.ename == error_type, (error.ename, error.evalue)
            assert source_marker in cell.source, cell.source
            assert message in error.evalue, error.evalue
            assert all(c.get('execution_count') is None for c in executed.cells[index + 1:]
                       if c.cell_type == 'code'), 'Notebook continued executing after failure'
            if mode == 'command-failure':
                assert 'INJECTED_COMMAND_FAILURE' in output.read_text(), 'The intended failure was not reached'
            calls = [json.loads(line) for line in commands.read_text().splitlines()]
            if notebook in {'operationhub-d01', 'coursewarehub-aws-d01'}:
                assert not any(call[0] == 'aws' and any(a.startswith(('create-', 'import-', 'authorize-')) for a in call[1:]) for call in calls), calls
            if notebook == 'coursewarehub-d02':
                assert not (work / 'failure_test_inventory').exists(), 'Invalid input produced a usable-looking inventory'
        else:
            raise AssertionError('Notebook incorrectly accepted the failure case: ' + case_id)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--notebook', choices=NOTEBOOKS, required=True)
    parser.add_argument('--result-dir', type=Path, required=True)
    parser.add_argument('--ssh-host')
    parser.add_argument('--ssh-key', type=Path)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    results = args.result_dir.resolve()
    results.mkdir(parents=True, exist_ok=True)
    records = []
    failed = False
    for case in cases(args.notebook):
        record = {'case': case[0], 'status': 'running'}
        records.append(record)
        try:
            run_case(repo, args.notebook, case, results, args.ssh_host, args.ssh_key)
        except Exception:
            record.update(status='failed', error=traceback.format_exc())
            print(record['error'], flush=True)
            failed = True
        else:
            record['status'] = 'passed'
            print('NOTEBOOK FAILURE TEST PASS:', args.notebook, case[0], flush=True)
        finally:
            (results / 'results.json').write_text(json.dumps({'notebook': NOTEBOOKS[args.notebook],
                'cases': records}, ensure_ascii=False, indent=2) + '\n')
    if failed:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
