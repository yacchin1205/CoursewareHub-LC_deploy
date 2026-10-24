#!/usr/bin/env bash
set -Eeuo pipefail
result_dir="${GITHUB_WORKSPACE:-$PWD}/test-results/vm-notebook"
mkdir -p "$result_dir"
exec > >(tee "$result_dir/run.log") 2>&1
private_dir=$(mktemp -d)
bridge=cwh-e2e-br0
subnet=172.30.77.0/24
gateway=172.30.77.1
manager_ip=172.30.77.10
worker_ip=172.30.77.11

remote() {
  local address=$1
  shift
  ssh -F "$private_dir/ssh_config" "centos@$address" "sudo -- $*"
}
cleanup() {
  result=$?
  trap - EXIT
  set +e
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
  for node in worker master; do
    if [[ -f "$private_dir/$node.pid" ]]; then
      guest_pid=$(cat "$private_dir/$node.pid")
      sudo kill "$guest_pid"
      for attempt in $(seq 1 10); do
        sudo kill -0 "$guest_pid" 2>/dev/null || break
        sleep 1
      done
      sudo kill -0 "$guest_pid" 2>/dev/null && sudo kill -KILL "$guest_pid"
    fi
  done
  sudo iptables -D FORWARD -i "$bridge" -j ACCEPT
  sudo iptables -D FORWARD -o "$bridge" -j ACCEPT
  sudo iptables -t nat -D POSTROUTING -s "$subnet" -j MASQUERADE
  sudo ip link del "$bridge"
  sudo ip link del cwh-master
  sudo ip link del cwh-worker
  sudo rm -rf "$private_dir"
  printf 'Experiment exit status: %s\n' "$result" > "$result_dir/result.txt"
  exit "$result"
}
trap cleanup EXIT

ssh-keygen -q -t ed25519 -N '' -f "$private_dir/id_ed25519"
touch "$private_dir/known_hosts"
cat > "$private_dir/ssh_config" <<EOF
Host *
  BatchMode yes
  StrictHostKeyChecking yes
  UserKnownHostsFile $private_dir/known_hosts
  IdentityFile $private_dir/id_ed25519
  IdentitiesOnly yes
  ConnectTimeout 5
  ServerAliveInterval 5
  ServerAliveCountMax 2
  ControlMaster auto
  ControlPersist 60s
  ControlPath $private_dir/ssh-%C
EOF
export ANSIBLE_SSH_ARGS="-F $private_dir/ssh_config"
export ANSIBLE_NOCOLOR=1
export ANSIBLE_PIPELINING=1
image=Rocky-9-GenericCloud-Base-9.8-20260525.0.x86_64.qcow2
curl --fail --location --retry 3 "https://download.rockylinux.org/pub/rocky/9/images/x86_64/$image" -o "$private_dir/base.qcow2"
printf '%s  %s\n' 92c206cc6f790c61583247eefe87890f8828420662c17cacf247cec78ab4eec8 "$private_dir/base.qcow2" | sha256sum -c -
qemu-img info --output=json "$private_dir/base.qcow2" > "$result_dir/base-image.json"
sudo ip link add "$bridge" type bridge
sudo ip address add "$gateway/24" dev "$bridge"
sudo ip link set "$bridge" up
sudo sysctl -w net.ipv4.ip_forward=1
sudo iptables -t nat -A POSTROUTING -s "$subnet" -j MASQUERADE
sudo iptables -I FORWARD -i "$bridge" -j ACCEPT
sudo iptables -I FORWARD -o "$bridge" -j ACCEPT
accel=tcg
cpu=max
if [[ -e /dev/kvm ]]; then accel=kvm; cpu=host; fi
printf 'QEMU acceleration: %s\n' "$accel" | tee "$result_dir/acceleration.txt"
for node in master worker; do
  address=$manager_ip
  mac=52:54:00:77:00:10
  [[ "$node" == worker ]] && address=$worker_ip && mac=52:54:00:77:00:11
  tap="cwh-$node"
  sudo ip tuntap add "$tap" mode tap
  sudo ip link set "$tap" master "$bridge"
  sudo ip link set "$tap" up
  qemu-img create -f qcow2 -F qcow2 -b "$private_dir/base.qcow2" "$private_dir/$node.qcow2" 40G
  cat > "$private_dir/$node-user.yml" <<EOF
#cloud-config
hostname: cwh-vm-$node
users:
  - name: centos
    groups: [wheel]
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    lock_passwd: true
    ssh_authorized_keys:
      - $(cat "$private_dir/id_ed25519.pub")
runcmd:
  - [sh, -c, 'systemctl disable --now firewalld || true']
  - [sh, -c, 'ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256 > /dev/ttyS0']
EOF
  cat > "$private_dir/$node-network.yml" <<EOF
version: 2
ethernets:
  eth0:
    match:
      macaddress: $mac
    set-name: eth0
    addresses: [$address/24]
    routes:
      - to: default
        via: $gateway
    nameservers:
      addresses: [1.1.1.1, 8.8.8.8]
EOF
  printf 'instance-id: cwh-e2e-%s\nlocal-hostname: cwh-vm-%s\n' "$node" "$node" > "$private_dir/$node-meta.yml"
  cloud-localds --network-config="$private_dir/$node-network.yml" "$private_dir/$node-seed.iso" "$private_dir/$node-user.yml" "$private_dir/$node-meta.yml"
  sudo qemu-system-x86_64 -machine "q35,accel=$accel" -cpu "$cpu" -m 2048 -smp 1 \
    -display none -monitor none -serial "file:$result_dir/$node-console.log" \
    -drive "file=$private_dir/$node.qcow2,if=virtio,format=qcow2" \
    -drive "file=$private_dir/$node-seed.iso,if=virtio,format=raw,readonly=on" \
    -netdev "tap,id=net0,ifname=$tap,script=no,downscript=no" \
    -device "virtio-net-pci,netdev=net0,mac=$mac" -daemonize -pidfile "$private_dir/$node.pid"
  sudo chown "$(id -u):$(id -g)" "$result_dir/$node-console.log"
  chmod 0644 "$result_dir/$node-console.log"
done
for node in master worker; do
  address=$manager_ip
  [[ "$node" == worker ]] && address=$worker_ip
  ready=false
  for attempt in $(seq 1 180); do
    if ssh-keyscan -T 2 -t ed25519 "$address" > "$private_dir/scanned-key" 2>/dev/null && [[ -s "$private_dir/scanned-key" ]]; then
      fingerprint=$(ssh-keygen -lf "$private_dir/scanned-key" -E sha256 | awk '{print $2}')
      if rg -Fq "$fingerprint" "$result_dir/$node-console.log"; then
        cat "$private_dir/scanned-key" >> "$private_dir/known_hosts"
        if timeout 90s ssh -F "$private_dir/ssh_config" "centos@$address" 'sudo cloud-init status --wait'; then ready=true; break; fi
      fi
    fi
    sleep 2
  done
  [[ "$ready" == true ]] || { echo "VM cloud-init/SSH did not become ready: $node"; exit 1; }
  remote "$address" getenforce
  remote "$address" cat /proc/sys/kernel/random/boot_id
done
python tests/e2e/run-nfs-notebook.py --result-dir "$result_dir" \
  --key "$private_dir/id_ed25519" --manager-ip "$manager_ip" --worker-ip "$worker_ip" \
  --manager-container cwh-vm-master --worker-container cwh-vm-worker \
  --transport ssh --case vm-os --gateway "$gateway" --probe-url "http://$manager_ip:8081/"
