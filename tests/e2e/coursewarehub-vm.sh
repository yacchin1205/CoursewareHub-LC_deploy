#!/usr/bin/env bash
set -Eeuo pipefail
result_dir="${GITHUB_WORKSPACE:-$PWD}/test-results/coursewarehub-vm"
mkdir -p "$result_dir"
exec > >(tee "$result_dir/run.log") 2>&1
source tests/e2e/qemu-guests.sh
bridge=cwh-e2e-br0
subnet=172.30.77.0/24
gateway=172.30.77.1
manager_ip=172.30.77.10
worker_ip=172.30.77.11

cleanup() {
  result=$?
  trap - EXIT
  set +e
  qemu_host_diagnostics
  for node in master worker; do
    address=$manager_ip
    [[ "$node" == worker ]] && address=$worker_ip
    timeout 20s ssh -F "$private_dir/ssh_config" "centos@$address" 'sudo journalctl --no-pager' > "$result_dir/$node-journal.txt" 2>&1
    timeout 15s ssh -F "$private_dir/ssh_config" "centos@$address" 'sudo findmnt; sudo getenforce; cat /proc/sys/kernel/random/boot_id' > "$result_dir/$node-state.txt" 2>&1
  done
  for service in auth-proxy jupyterhub postgres; do
    timeout 15s ssh -F "$private_dir/ssh_config" "centos@$manager_ip" "sudo docker service logs coursewarehub_$service" > "$result_dir/coursewarehub-$service.log" 2>&1
  done
  timeout 20s ssh -F "$private_dir/ssh_config" "centos@$worker_ip" 'sudo systemctl stop docker containerd; sudo umount -lf /jupyter /exchange'
  qemu_cleanup
  printf 'Test exit status: %s\n' "$result" > "$result_dir/result.txt"
  exit "$result"
}
trap cleanup EXIT

qemu_boot_guests "master:$manager_ip:2048:40G" "worker:$worker_ip:4096:40G"
python tests/e2e/run-coursewarehub.py --result-dir "$result_dir" \
  --key "$private_dir/id_ed25519" --manager-ip "$manager_ip" --worker-ip "$worker_ip" \
  --manager-container cwh-vm-master --worker-container cwh-vm-worker \
  --transport ssh --case vm-os --gateway "$gateway" --probe-url "http://$manager_ip:8081/"
