"""Run D01-D03 with real systemd/NFS, then test a container restart."""
import argparse
import json
import os
import secrets
from pathlib import Path
import subprocess
import time
import urllib.request

import papermill as pm
import yaml
from coursewarehub_ui import check_coursewarehub


def command(args):
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
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
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    results = args.result_dir.resolve()
    work = results / "workspace"
    work.mkdir(parents=True, exist_ok=True)
    group = "ci_nfs"
    master, worker = args.manager_ip, args.worker_ip
    key = str(args.key.resolve())
    hosts = work / "hosts"
    hosts.write_text(f"[{group}]\n{master} public_ip={master}\n{worker}\n\n"
                     f"[{group}:vars]\nansible_ssh_user=centos\n"
                     f"ansible_ssh_private_key_file={key}\n"
                     "ansible_python_interpreter=/usr/bin/python3\n")
    inventory = work / f"{group}_inventory"

    def execute(name, parameters):
        pm.execute_notebook(str(repo / "coursewarehub" / name), str(results / name),
                            parameters=parameters, cwd=str(work), kernel_name="python3",
                            execution_timeout=600, log_output=True)

    execute("D01_オンプレミス環境の場合のマシン設定.ipynb",
            dict(inventory_path=str(hosts), target_group=group))
    execute("D02_CoursewareHubインベントリの準備.ipynb", dict(
        hosts_file=str(hosts), target_group=group, hubname=group,
        group_vars_dir=str(work / "group_vars"), nfs_server=master,
        master_server=master, master_fqdn="coursewarehub.test",
        servicenet_ips={master: master, worker: worker},
        ansible_ssh_user="centos", ansible_ssh_private_key_file=key,
        auth_proxy_image="niicloudoperation/coursewarehub-auth-proxy:2024.03",
        jupyterhub_image="niicloudoperation/coursewarehub-jupyterhub:2024.03"))
    execute("D03_NFSの準備.ipynb", dict(
        hosts_file=str(inventory), target_group=group,
        deploy_repo_ref="db991a98e6c321a2643b8ded9d4c2b6e8987459f",
        check_mode_first=False, reboot_clients=False))

    def remote(host, cmd):
        return command(["ansible", "-i", str(inventory), host, "-b", "-m", "command", "-a", cmd])

    def inspect_mounts():
        data = command(["docker", "exec", args.worker_container, "findmnt", "--json",
                        "--output", "TARGET,SOURCE,FSTYPE", "--target", "/exchange"])
        mount = json.loads(data)["filesystems"][0]
        assert mount["target"] == "/exchange", mount
        assert mount["fstype"] in ["nfs", "nfs4"], mount
        assert mount["source"] == f"{master}:/exchange", mount
        return mount

    before = inspect_mounts()
    remote(master, "systemctl is-active nfs-server")
    remote(master, "sh -c 'echo persisted-through-container-restart > /exchange/ci-persistence'")
    remote(worker, "grep -qx persisted-through-container-restart /exchange/ci-persistence")
    command(["docker", "restart", "--time", "30", args.worker_container])
    for attempt in range(60):
        if subprocess.run(["docker", "exec", args.worker_container,
                           "mountpoint", "-q", "/exchange"]).returncode == 0:
            break
        time.sleep(2)
    else:
        raise AssertionError("fstab NFS mount did not recover after container restart")
    after = inspect_mounts()
    remote(worker, "grep -qx persisted-through-container-restart /exchange/ci-persistence")
    remote(worker, "sh -c 'echo worker-write-ok > /exchange/ci-worker-write'")
    remote(master, "grep -qx worker-write-ok /exchange/ci-worker-write")
    # D04 performs package installation, systemd configuration and Swarm creation.
    # The harness only checks the result and deploys a disposable test workload.
    execute("D04_Docker Swarmの準備.ipynb", dict(
        hosts_file=str(inventory), target_group=group + "_nodes",
        docker_packages=["docker-ce-27.5.1", "docker-ce-cli-27.5.1", "containerd.io"],
        compose_version="2.39.4", network_prerequisites_confirmed=True,
        hosts_unsafe_writes=True))
    for attempt in range(30):
        nodes = command(["docker", "exec", args.manager_container, "docker", "node", "ls",
                         "--format", "{{.Status}} {{.Availability}}"])
        if nodes.strip().splitlines().count("Ready Active") == 2:
            break
        time.sleep(2)
    else:
        raise AssertionError("D04 did not produce two Ready/Active Swarm nodes")
    for node in [args.manager_container, args.worker_container]:
        command(["docker", "exec", node, "docker", "pull", "busybox:1.37.0"])
    stack = yaml.safe_load((repo / "tests/e2e/swarm-smoke.yml").read_text())
    stack["services"]["worker-http"]["volumes"] = ["/exchange:/ci-shared"]
    subprocess.run(["docker", "exec", "-i", args.manager_container, "sh", "-c",
                    "cat > /tmp/swarm-smoke.yml"], input=yaml.safe_dump(stack), text=True, check=True)
    command(["docker", "exec", args.manager_container, "docker", "stack", "deploy",
             "--resolve-image", "never", "-c", "/tmp/swarm-smoke.yml", "smoke"])
    for attempt in range(90):
        try:
            with urllib.request.urlopen("http://127.0.0.1:18081/", timeout=3) as response:
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
        "executed_notebooks": ["on-premise D01", "D02", "D03", "D04"],
        "real_nfs_server_and_client_roles": "passed",
        "mount_before_restart": before, "mount_after_container_restart": after,
        "cross_node_read_write": "passed", "container_restart_fstab_remount": "passed",
        "skipped_notebook_operations": ["server/client check mode", "OS reboot"],
        "D04_package_install_systemd_compose_swarm": "passed",
        "swarm_overlay_and_nfs_workload": "passed",
        "D04_parameters": {"docker_version": "27.5.1", "compose_version": "2.39.4",
                           "hosts_unsafe_writes": True},
        "not_covered": ["OS reboot persistence", "Compose v1", "D05-D06", "CoursewareHub UI"],
    }, indent=2) + "\n")
    print("PASS: D03 real NFS roles, NFS mount, cross-node read/write and container restart remount")
    print("PASS: D04 Docker installation, systemd configuration, Compose and Swarm creation; overlay/NFS workload")

    command(["docker", "exec", args.manager_container, "docker", "stack", "rm", "smoke"])
    for attempt in range(60):
        services = command(["docker", "exec", args.manager_container, "docker", "service", "ls",
                            "--filter", "label=com.docker.stack.namespace=smoke", "-q"])
        if not services.strip():
            break
        time.sleep(2)
    else:
        raise AssertionError("Probe stack did not terminate before CoursewareHub deployment")
    subprocess.run(["sudo", "tee", "-a", "/etc/hosts"],
                    input=f"\n{master} coursewarehub.test\n", text=True, check=True)
    os.environ["CWH_TEST_DB_PASSWORD"] = secrets.token_hex(24)
    os.environ["CWH_TEST_ADMIN_PASSWORD"] = secrets.token_hex(24)
    execute("D05_CoursewareHubの準備.ipynb", dict(
        hosts_file=str(inventory), target_group=group,
        db_password_env="CWH_TEST_DB_PASSWORD", admin_password_env="CWH_TEST_ADMIN_PASSWORD",
        teacher_email="ciadmin@example.test", intermediate_certfile="",
        generate_self_signed_cert=True, check_mode_first=False, configure_selinux=False,
        legacy_singleuser_profile=True,
        logo_path=str(repo / "coursewarehub/jupyterhub-logo.png"),
        contents_notebook_path=str(repo / "coursewarehub/D06_CoursewareHubコンテンツの配備.ipynb"),
        deploy_repo_ref="db991a98e6c321a2643b8ded9d4c2b6e8987459f",
        resource_schema_url="https://raw.githubusercontent.com/NII-cloud-operation/CoursewareHub-LC_platform/d08692cfb77cd427dd1fb197d499a26f9d310003/jupyterhub/resources-schema.json"))
    (results / "construction-summary.json").write_text(json.dumps({
        "D01_D05_execution": "passed", "runtime_verification": "pending",
        "platform_release": "2024.03", "profile": "classic CoursewareUserSpawner",
        "singleuser_image": "niicloudoperation/notebook:latest",
        "skipped": ["OS reboot", "D03/D05 initial check mode", "SELinux configuration"],
        "not_covered": ["repo2docker/registry", "federation", "LTI", "latest master", "O01 update"],
    }, indent=2) + "\n")
    check_coursewarehub(results, "https://coursewarehub.test", "ciadmin@example.test",
                        os.environ["CWH_TEST_ADMIN_PASSWORD"], args.manager_container,
                        args.worker_container)


if __name__ == "__main__":
    main()
