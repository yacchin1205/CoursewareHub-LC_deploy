"""Run D01-D03 with real systemd/NFS, then test a container restart."""
import argparse
import json
from pathlib import Path
import subprocess
import time

import papermill as pm


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
                            execution_timeout=600)

    execute("D01_オンプレミス環境の場合のマシン設定.ipynb",
            dict(inventory_path=str(hosts), target_group=group))
    execute("D02_CoursewareHubインベントリの準備.ipynb", dict(
        hosts_file=str(hosts), target_group=group, hubname=group,
        group_vars_dir=str(work / "group_vars"), nfs_server=master,
        master_server=master, master_fqdn="coursewarehub.test",
        servicenet_ips={master: master, worker: worker},
        ansible_ssh_user="centos", ansible_ssh_private_key_file=key))
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
    (results / "nfs-summary.json").write_text(json.dumps({
        "executed_notebooks": ["on-premise D01", "D02", "D03"],
        "real_nfs_server_and_client_roles": "passed",
        "mount_before_restart": before, "mount_after_container_restart": after,
        "cross_node_read_write": "passed", "container_restart_fstab_remount": "passed",
        "skipped_notebook_operations": ["server/client check mode", "OS reboot"],
        "not_covered": ["OS reboot persistence", "D04-D06", "CoursewareHub UI"],
    }, indent=2) + "\n")
    print("PASS: D03 real NFS roles, NFS mount, cross-node read/write and container restart remount")


if __name__ == "__main__":
    main()
