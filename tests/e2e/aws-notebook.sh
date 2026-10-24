#!/usr/bin/env bash
set -Eeuo pipefail
result_dir="${GITHUB_WORKSPACE:-$PWD}/test-results/aws-notebook"
mkdir -p "$result_dir"
exec > >(tee "$result_dir/run.log") 2>&1
source tests/e2e/qemu-guests.sh
bridge=cwh-aws-br0
subnet=10.1.1.0/24
gateway=10.1.1.1
cleanup() {
  result=$?
  trap - EXIT
  set +e
  qemu_host_diagnostics
  for index in "${!qemu_names[@]}"; do
    node=${qemu_names[$index]}
    address=${qemu_addresses[$index]}
    timeout 20s ssh -F "$private_dir/ssh_config" "centos@$address" 'sudo journalctl --no-pager' > "$result_dir/$node-journal.txt" 2>&1
    timeout 15s ssh -F "$private_dir/ssh_config" "centos@$address" 'sudo findmnt; sudo getenforce; cat /proc/sys/kernel/random/boot_id' > "$result_dir/$node-state.txt" 2>&1
  done
  timeout 20s ssh -F "$private_dir/ssh_config" centos@10.1.1.9 'cd /opt/ophub && sudo docker compose logs' > "$result_dir/operationhub-services.log" 2>&1
  for service in jupyterhub auth-proxy postgres; do
    timeout 15s ssh -F "$private_dir/ssh_config" centos@10.1.1.10 "sudo docker service logs coursewarehub_$service" > "$result_dir/coursewarehub-$service.log" 2>&1
  done
  timeout 20s ssh -F "$private_dir/ssh_config" centos@10.1.1.11 'sudo systemctl stop docker containerd; sudo umount -lf /jupyter /exchange'
  qemu_cleanup
  printf 'Experiment exit status: %s\n' "$result" > "$result_dir/result.txt"
  exit "$result"
}
trap cleanup EXIT
qemu_boot_guests 'operationhub:10.1.1.9:4096:50G' 'master:10.1.1.10:2048:100G' 'worker1:10.1.1.11:4096:50G'
python tests/e2e/run-aws-notebooks.py --result-dir "$result_dir" --private-dir "$private_dir" --gateway "$gateway"
