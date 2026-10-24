"""Execute repository notebooks against disposable SSH nodes; keep evidence."""
import argparse
import json
import os
from pathlib import Path
import subprocess

import nbformat
import papermill as pm
import yaml


def command(args):
    result = subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE)
    print(result.stdout)
    return result.stdout


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-dir", required=True, type=Path)
    parser.add_argument("--key", required=True, type=Path)
    parser.add_argument("--manager-ip", required=True)
    parser.add_argument("--worker-ip", required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    result_dir = args.result_dir.resolve()
    work = result_dir / "workspace"
    work.mkdir(parents=True, exist_ok=True)
    key = str(args.key.resolve())
    master, worker = args.manager_ip, args.worker_ip
    group = "ci_coursewarehub"
    hosts_file = work / "hosts"
    hosts_file.write_text(
        f"[{group}]\n"
        f"{master} public_ip={master}\n{worker}\n\n"
        f"[{group}:vars]\nansible_ssh_user=centos\n"
        f"ansible_ssh_private_key_file={key}\n"
        "ansible_python_interpreter=/usr/bin/python3\n"
    )
    d01 = repo / "coursewarehub/D01_オンプレミス環境の場合のマシン設定.ipynb"
    d02 = repo / "coursewarehub/D02_CoursewareHubインベントリの準備.ipynb"

    # Fixture preflight exercises SSH, remote Python, sudo and the real Docker daemon.
    command(["ansible", "-i", str(hosts_file), group, "-b", "-m", "command", "-a", "docker info"])
    for source, output, parameters in [
        (d01, "D01-connection.ipynb", dict(inventory_path=str(hosts_file), target_group=group)),
        (d02, "D02-inventory.ipynb", dict(
            hosts_file=str(hosts_file), target_group=group, hubname=group,
            group_vars_dir=str(work / "group_vars"), nfs_server=master,
            master_server=master, master_fqdn="coursewarehub.test",
            servicenet_ips={master: master, worker: worker},
            ansible_ssh_user="centos", ansible_ssh_private_key_file=key,
        )),
    ]:
        pm.execute_notebook(str(source), str(result_dir / output), parameters=parameters,
                            cwd=str(work), kernel_name="python3", execution_timeout=120)

    generated = work / f"{group}_inventory"
    inventory = json.loads(command(["ansible-inventory", "-i", str(generated), "--list"]))
    assert inventory["cwhub_master"]["hosts"] == [master]
    assert inventory["cwhub_nodes"]["hosts"] == [worker]
    assert inventory["cwhub_nfs_server"]["hosts"] == [master]
    for host in [master, worker]:
        values = inventory["_meta"]["hostvars"][host]
        assert values["servicenet_ip"] == host
        assert values["ansible_ssh_user"] == "centos"
        assert values["ansible_ssh_private_key_file"] == key
    group_vars = yaml.safe_load((work / "group_vars" / group).read_text())
    assert group_vars["nfsserver"] == master
    assert group_vars["master_fqdn"] == "coursewarehub.test"
    assert group_vars["nfs_server_role"] == "nfs_server"
    command(["ansible", "-i", str(generated), group, "-m", "ping"])
    command(["ansible", "-i", str(generated), group, "-b", "-m", "command", "-a", "docker info"])

    # Real connection failure, not a mocked exception: a rejected user on a live node.
    bad_inventory = work / "bad-hosts"
    bad_inventory.write_text(
        f"[{group}]\n{master} ansible_user=nonexistent_ci_user "
        f"ansible_ssh_private_key_file={key} ansible_ssh_timeout=5\n")
    try:
        pm.execute_notebook(str(d01), str(result_dir / "D01-expected-failure.ipynb"),
                            parameters=dict(inventory_path=str(bad_inventory), target_group=group),
                            cwd=str(work), kernel_name="python3", execution_timeout=60)
    except pm.PapermillExecutionError:
        notebook = nbformat.read(result_dir / "D01-expected-failure.ipynb", as_version=4)
        errors = [o for c in notebook.cells if c.cell_type == "code"
                  for o in c.get("outputs", []) if o.output_type == "error"]
        assert any(o.ename == "CalledProcessError" for o in errors), errors
    else:
        raise AssertionError("SSH failure was silently accepted by D01")

    (result_dir / "notebook-summary.json").write_text(json.dumps({
        "D01_connection": "passed", "D02_inventory": "passed",
        "generated_inventory_ssh_sudo_docker": "passed",
        "D01_connection_failure_propagation": "passed",
        "not_covered": ["systemd", "NFS", "OS reboot", "D03-D06", "CoursewareHub UI"],
    }, indent=2) + "\n")
    print("PASS: existing D01/D02 notebooks, generated inventory and SSH failure propagation")


if __name__ == "__main__":
    main()
