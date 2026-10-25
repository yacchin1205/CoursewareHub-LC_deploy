"""Build CoursewareHub with the construction notebooks and test its running service."""
import argparse
from contextlib import ExitStack
import json
import os
import secrets
import shlex
from pathlib import Path
from urllib.parse import urlparse
import subprocess
import time
import urllib.request

import papermill as pm
import yaml
from coursewarehub_ui import check_coursewarehub
from guest import Guest
from student_ui import register_student, check_student
from lti_platform import running_platform, check_lti
from registry_fixture import running_registry, running_build_repository
from saml_provider import running_provider, check_saml


def command(args):
    result = subprocess.run(args, text=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            timeout=180)
    print(result.stdout)
    result.check_returncode()
    return result.stdout


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-dir", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--manager-ip", required=True)
    parser.add_argument("--worker-ip", required=True)
    parser.add_argument("--worker-container", required=True)
    parser.add_argument("--manager-container", required=True)
    parser.add_argument("--case", choices=["legacy-2024", "current-registry", "federation", "vm-os"], default="legacy-2024")
    parser.add_argument("--network")
    parser.add_argument("--transport", choices=["docker", "ssh"], default="docker")
    parser.add_argument("--gateway")
    parser.add_argument("--probe-url", default="http://127.0.0.1:18081/")
    args = parser.parse_args()
    with ExitStack() as fixtures:
        run_case(args, fixtures)


def run_case(args, fixtures):
    repo = Path(__file__).resolve().parents[2]
    results = args.result_dir.resolve()
    work = results / "workspace"
    work.mkdir(parents=True, exist_ok=True)
    manager_guest = Guest(args.manager_container, args.manager_ip, args.transport)
    worker_guest = Guest(args.worker_container, args.worker_ip, args.transport)
    if args.transport == "ssh":
        for guest in [manager_guest, worker_guest]:
            assert guest.output(["getenforce"]) == "Enforcing", "VM must start with SELinux enforcing"
    platform_tag = "master" if args.case == "current-registry" else "2024.03"
    gateway = args.gateway or command(["docker", "inspect", "--format",
                       "{{range .NetworkSettings.Networks}}{{.Gateway}}{{end}}",
                       args.manager_container]).strip()
    email = "ciadmin@example.test"
    saml = None
    saml_metadata = None
    if args.case == "federation":
        saml = fixtures.enter_context(running_provider("http://" + gateway + ":18501", "https://coursewarehub.test", email))
        saml_metadata = results / "idp-metadata.xml"
        saml_metadata.write_bytes(saml.metadata())
    group = "ci_nfs"
    master, worker = args.manager_ip, args.worker_ip
    key = str(args.key.resolve())
    hosts = work / "hosts"
    if getattr(args, 'initial_hosts', None):
        hosts.write_text(args.initial_hosts)
    else:
        hosts.write_text(f"[{group}]\n{master} public_ip={master}\n{worker}\n\n"
                         f"[{group}:vars]\nansible_ssh_user=centos\n"
                         f"ansible_ssh_private_key_file={key}\n"
                         "ansible_python_interpreter=/usr/bin/python3\n")
    inventory = work / f"{group}_inventory"

    construction_results = []

    def execute(name, parameters):
        record = {"notebook": name, "parameters": parameters, "status": "running"}
        construction_results.append(record)
        def save_result():
            (results / "construction-results.json").write_text(
                json.dumps(construction_results, ensure_ascii=False, indent=2) + "\n")
        save_result()
        try:
            pm.execute_notebook(str(repo / "coursewarehub" / name), str(results / name),
                                parameters=parameters, cwd=str(work), kernel_name="python3",
                                execution_timeout=600, log_output=True)
        except Exception as error:
            record.update(status="failed", error=str(error))
            raise
        else:
            record["status"] = "passed"
        finally:
            save_result()

    if not getattr(args, 'initial_hosts', None):
        execute("D01_オンプレミス環境の場合のマシン設定.ipynb",
                dict(inventory_path=str(hosts), target_group=group))
    execute("D02_CoursewareHubインベントリの準備.ipynb", dict(
        hosts_file=str(hosts), target_group=group, hubname=group,
        group_vars_dir=str(work / "group_vars"), nfs_server=master,
        master_server=master, master_fqdn="coursewarehub.test",
        servicenet_ips={master: master, worker: worker},
        ansible_ssh_user="centos", ansible_ssh_private_key_file=key,
        spawner_start_timeout=600 if args.case == "current-registry" else 60,
        enable_federation=saml is not None,
        cg_groups=[saml.authorized_group] if saml else [],
        cg_fqdn=urlparse(saml.base_url).netloc if saml else None,
        auth_proxy_image="niicloudoperation/coursewarehub-auth-proxy:" + platform_tag,
        jupyterhub_image="niicloudoperation/coursewarehub-jupyterhub:" + platform_tag))
    execute("D03_NFSの準備.ipynb", dict(
        hosts_file=str(inventory), target_group=group,
        deploy_repo_ref="db991a98e6c321a2643b8ded9d4c2b6e8987459f",
        check_mode_first=args.transport == "ssh", reboot_clients=args.transport == "ssh"))

    def remote(host, cmd):
        return command(["ansible", "-i", str(inventory), host, "-b", "-m", "command", "-a", cmd])

    def inspect_mounts():
        data = command(worker_guest.command(["findmnt", "--json",
                        "--output", "TARGET,SOURCE,FSTYPE", "--target", "/exchange"]))
        mount = json.loads(data)["filesystems"][0]
        assert mount["target"] == "/exchange", mount
        assert mount["fstype"] in ["nfs", "nfs4"], mount
        assert mount["source"] == f"{master}:/exchange", mount
        return mount

    before = inspect_mounts()
    remote(master, "systemctl is-active nfs-server")
    remote(master, "sh -c 'echo persisted-through-container-restart > /exchange/ci-persistence'")
    remote(worker, "grep -qx persisted-through-container-restart /exchange/ci-persistence")
    # A guest restart invalidates a multiplexed SSH session; open a fresh session afterward.
    subprocess.run(["ssh", *shlex.split(os.environ["ANSIBLE_SSH_ARGS"]), "-O", "exit",
                    "centos@" + worker], timeout=15, check=False)
    restart = worker_guest.restart()
    for attempt in range(60):
        if worker_guest.run(["mountpoint", "-q", "/exchange"], check=False).returncode == 0:
            break
        time.sleep(2)
    else:
        raise AssertionError("fstab NFS mount did not recover after guest restart")
    after = inspect_mounts()
    remote(worker, "grep -qx persisted-through-container-restart /exchange/ci-persistence")
    remote(worker, "sh -c 'echo worker-write-ok > /exchange/ci-worker-write'")
    remote(master, "grep -qx worker-write-ok /exchange/ci-worker-write")
    # D04 performs package installation, systemd configuration and Swarm creation.
    # The harness only checks the result and deploys a disposable test workload.
    registry = None
    if args.case == "current-registry":
        registry = fixtures.enter_context(running_registry(args.network, results))
        os.environ["CWH_TEST_REGISTRY_PASSWORD"] = registry.password
    execute("D04_Docker Swarmの準備.ipynb", dict(
        hosts_file=str(inventory), target_group=group + "_nodes",
        docker_packages=["docker-ce-27.5.1", "docker-ce-cli-27.5.1", "containerd.io"],
        docker_opts={"data-root": "/var/lib/docker", "insecure-registries": [registry.host]} if registry else None,
        compose_version="1.29.2" if args.case == "vm-os" else "2.39.4", network_prerequisites_confirmed=True,
        hosts_unsafe_writes=True))
    for attempt in range(30):
        nodes = command(manager_guest.command(["docker", "node", "ls",
                         "--format", "{{.Status}} {{.Availability}}"]))
        if nodes.strip().splitlines().count("Ready Active") == 2:
            break
        time.sleep(2)
    else:
        raise AssertionError("D04 did not produce two Ready/Active Swarm nodes")
    for guest in [manager_guest, worker_guest]:
        command(guest.command(["docker", "pull", "busybox:1.37.0"]))
    stack = yaml.safe_load((repo / "tests/e2e/swarm-workload.yml").read_text())
    stack["services"]["worker-http"]["volumes"] = ["/exchange:/ci-shared"]
    manager_guest.run(["sh", "-c", "cat > /tmp/swarm-workload.yml"], input=yaml.safe_dump(stack))
    command(manager_guest.command(["docker", "stack", "deploy",
             "--resolve-image", "never", "-c", "/tmp/swarm-workload.yml", "smoke"]))
    for attempt in range(90):
        try:
            with urllib.request.urlopen(args.probe_url, timeout=3) as response:
                content = response.read().decode().strip()
            if content == "swarm-worker-shared-volume-ok":
                break
        except (OSError, ValueError):
            pass
        time.sleep(2)
    else:
        raise AssertionError("D04 Swarm cross-node overlay workload failed")
    remote(master, "grep -qx swarm-worker-shared-volume-ok /exchange/probe.txt")
    (results / "nfs-summary.json").write_text(json.dumps({
        "real_nfs_server_and_client_roles": "passed",
        "mount_before_restart": before, "mount_after_restart": after, "restart": restart,
        "cross_node_read_write": "passed", "restart_fstab_remount": "passed",
        "D04_package_install_systemd_compose_swarm": "passed",
        "swarm_overlay_and_nfs_workload": "passed",
    }, indent=2) + "\n")
    print("PASS: D03 real NFS roles, NFS mount, cross-node read/write and guest restart remount")
    print("PASS: D04 Docker installation, systemd configuration, Compose and Swarm creation; overlay/NFS workload")

    command(manager_guest.command(["docker", "stack", "rm", "smoke"]))
    for attempt in range(60):
        services = command(manager_guest.command(["docker", "service", "ls",
                            "--filter", "label=com.docker.stack.namespace=smoke", "-q"]))
        if not services.strip():
            break
        time.sleep(2)
    else:
        raise AssertionError("Probe stack did not terminate before CoursewareHub deployment")
    subprocess.run(["sudo", "tee", "-a", "/etc/hosts"],
                    input=f"\n{master} coursewarehub.test\n", text=True, check=True)
    os.environ["CWH_TEST_DB_PASSWORD"] = secrets.token_hex(24)
    os.environ["CWH_TEST_ADMIN_PASSWORD"] = secrets.token_hex(24)
    student_email = "cistudent@example.test"
    student_password = secrets.token_hex(24)
    build_repository = None
    if registry:
        registry.seed(args.manager_container, args.worker_container, "niicloudoperation/notebook")
        build_repository = fixtures.enter_context(running_build_repository(gateway, registry))
    with running_platform("http://" + gateway + ":18500", "https://coursewarehub.test", email) as platform:
        execute("D05_CoursewareHubの準備.ipynb", dict(
            hosts_file=str(inventory), target_group=group,
            db_password_env="CWH_TEST_DB_PASSWORD", admin_password_env="CWH_TEST_ADMIN_PASSWORD",
            teacher_email=email, intermediate_certfile="",
            cgidp_metadata=str(saml_metadata) if saml_metadata else None,
            generate_self_signed_cert=True, check_mode_first=args.transport == "ssh",
            configure_selinux=args.transport == "ssh",
            legacy_singleuser_profile=registry is None, lti_config=platform.registration,
            registry_host=registry.host if registry else "",
            registry_password_env="CWH_TEST_REGISTRY_PASSWORD" if registry else None,
            registry_insecure=registry is not None,
            singleuser_args=["--NotebookApp.kernel_manager_class=notebook.services.kernels.kernelmanager.AsyncMappingKernelManager"] if registry else [],
            logo_path=str(repo / "coursewarehub/jupyterhub-logo.png"),
            contents_notebook_path=str(repo / "coursewarehub/D06_CoursewareHubコンテンツの配備.ipynb"),
            deploy_repo_ref="db991a98e6c321a2643b8ded9d4c2b6e8987459f",
            resource_schema_url="https://raw.githubusercontent.com/NII-cloud-operation/CoursewareHub-LC_platform/" + ("master" if registry else "d08692cfb77cd427dd1fb197d499a26f9d310003") + "/jupyterhub/resources-schema.json"))
        if registry:
            registry.check_proxy(manager_guest, "https://coursewarehub.test", results)
        username = check_coursewarehub(results, "https://coursewarehub.test", email,
                            os.environ["CWH_TEST_ADMIN_PASSWORD"], manager_guest,
                            worker_guest, build_repository=build_repository)
        check_lti(results / "lti", platform, username)
        if saml:
            check_saml(results / "saml", saml, username, manager_guest)
        register_student(manager_guest, student_email, student_password)
        check_student(results / "student", "https://coursewarehub.test", student_email,
                      student_password, manager_guest, built_image=build_repository is not None)
        execute("O01_CoursewareHubの更新.ipynb", dict(
            hosts_file=str(inventory), target_group=group,
            db_password_env="CWH_TEST_DB_PASSWORD", intermediate_certfile="",
            check_mode_first=False))
        if registry:
            (results / "after-update").mkdir(parents=True, exist_ok=True)
            registry.check_proxy(manager_guest, "https://coursewarehub.test", results / "after-update")
        check_coursewarehub(results / "after-update", "https://coursewarehub.test",
                            email, os.environ["CWH_TEST_ADMIN_PASSWORD"],
                            manager_guest, worker_guest, expected_existing=True,
                            build_repository=build_repository)
        check_student(results / "after-update" / "student", "https://coursewarehub.test", student_email,
                      student_password, manager_guest, expected_existing=True,
                      built_image=build_repository is not None)
        check_lti(results / "after-update" / "lti", platform, username)
        if saml:
            check_saml(results / "after-update" / "saml", saml, username, manager_guest)


if __name__ == "__main__":
    main()
