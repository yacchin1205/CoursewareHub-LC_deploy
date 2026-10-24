"""Construct OperationHub and CoursewareHub using the AWS notebooks and real CI guests."""
import argparse
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
from types import SimpleNamespace

import papermill as pm

from aws_guest_provider import GuestBinding, running_aws_provider
from guest import Guest
from operationhub_ui import check_operationhub


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--result-dir', type=Path, required=True)
    parser.add_argument('--private-dir', type=Path, required=True)
    parser.add_argument('--gateway', required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    results = args.result_dir.resolve()
    work = results / 'workspace'
    work.mkdir(parents=True, exist_ok=True)
    private = args.private_dir.resolve()
    bindings = [GuestBinding(role, Guest('cwh-vm-' + role, address, 'ssh'), results / (role + '-console.log'))
                for role, address in [('operationhub', '10.1.1.9'), ('master', '10.1.1.10'), ('worker1', '10.1.1.11')]]
    os.environ.update(AWS_ACCESS_KEY_ID='testing', AWS_SECRET_ACCESS_KEY='testing',
                      AWS_DEFAULT_REGION='ap-northeast-1', AWS_EC2_METADATA_DISABLED='true')
    aws_dir = private / 'aws'
    aws_dir.mkdir(mode=0o700)
    (aws_dir / 'config').write_text('[default]\nregion = ap-northeast-1\noutput = json\n')
    (aws_dir / 'credentials').write_text('[default]\naws_access_key_id = testing\naws_secret_access_key = testing\n')
    for file in aws_dir.iterdir():
        file.chmod(0o600)
    os.environ['AWS_CONFIG_FILE'] = str(aws_dir / 'config')
    os.environ['AWS_SHARED_CREDENTIALS_FILE'] = str(aws_dir / 'credentials')
    os.environ['CWH_OPERATIONHUB_PASSWORD'] = secrets.token_hex(24)
    cert, key = private / 'ophub.cer', private / 'ophub.key'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2',
                    '-subj', '/CN=operationhub.test', '-addext', 'subjectAltName=DNS:operationhub.test',
                    '-keyout', str(key), '-out', str(cert)], check=True,
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    key.chmod(0o600)
    with ExitStack() as fixtures:
        provider = fixtures.enter_context(running_aws_provider(bindings, results / 'aws-api', args.gateway))
        parameters = dict(
            cloudformation_stack_name='ci_operationhub', resource_prefix='ci-operationhub',
            image_id=provider['image_id'], aws_endpoint_url=provider['endpoint'],
            construction_parameters_confirmed=True, admin_credentials_confirmed=True,
            ssh_private_key_path=str(private / 'operationhub-key'), known_hosts_path=str(private / 'known_hosts'),
            hosts_inventory_path=str(work / 'hosts'), update_hosts_inventory=True, boot_wait_seconds=0,
            notebook_runtime_path=str(repo / 'coursewarehub/notebook_runtime.py'),
            controller_public_ip=args.gateway, ssh_ingress_cidr='10.1.1.0/24', https_ingress_cidr='10.1.1.0/24',
            server_name='operationhub.test', cert_file=str(cert), key_file=str(key),
            admin_username='ohadmin', admin_password_env='CWH_OPERATIONHUB_PASSWORD',
            operationhub_repo_ref='de0c64a2981d04309fb41f4ca496b7ed9caf6fbf',
            singleuser_dockerfile=str(repo / 'image/Dockerfile'), courseware_dir=str(repo / 'coursewarehub'),
            aws_config_dir=str(aws_dir))
        name = 'D01_OperationHubをAWSに構築.ipynb'
        record = {'notebook': name, 'parameters': parameters, 'status': 'running'}
        try:
            pm.execute_notebook(str(repo / name), str(results / name), parameters=parameters,
                                cwd=str(work), kernel_name='python3', execution_timeout=1200, log_output=True)
        except Exception as error:
            record.update(status='failed', error=str(error))
            raise
        else:
            record['status'] = 'passed'
        finally:
            (results / 'operationhub-construction.json').write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
        subprocess.run(['sudo', 'tee', '-a', '/etc/hosts'], input='\n10.1.1.9 operationhub.test\n', text=True, check=True)
        hosts = check_operationhub(results / 'operationhub', 'https://operationhub.test', 'ohadmin',
            os.environ['CWH_OPERATIONHUB_PASSWORD'], dict(
                cloudformation_stack_name='ci_nfs', resource_prefix='ci-coursewarehub',
                image_id=provider['image_id'], aws_endpoint_url=provider['endpoint'],
                slave_instances=1, construction_parameters_confirmed=True, update_hosts_inventory=True,
                controller_public_ip=args.gateway, allow_cluster_internal_traffic=True, boot_wait_seconds=0))
        assert all(binding.instance_id for binding in bindings), [(b.role, b.instance_id) for b in bindings]
        # The controller uses its separately pinned fixture SSH identity for acceptance checks.
        hosts = re.sub(r'ansible_ssh_private_key_file=\S+',
                       'ansible_ssh_private_key_file=' + str(private / 'id_ed25519'), hosts)
        (results / 'aws-generated-hosts').write_text(hosts)
        spec = importlib.util.spec_from_file_location('coursewarehub_construction', repo / 'tests/e2e/run-nfs-notebook.py')
        driver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(driver)
        driver.run_case(SimpleNamespace(result_dir=results / 'coursewarehub', key=private / 'id_ed25519',
            manager_ip='10.1.1.10', worker_ip='10.1.1.11', manager_container='cwh-vm-master',
            worker_container='cwh-vm-worker1', case='vm-os', network=None, transport='ssh',
            gateway=args.gateway, probe_url='http://10.1.1.10:8081/', initial_hosts=hosts), fixtures)


if __name__ == '__main__':
    main()
