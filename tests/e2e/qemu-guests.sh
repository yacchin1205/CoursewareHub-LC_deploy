#!/usr/bin/env bash
# The caller sets result_dir, bridge, subnet and gateway, and owns the EXIT trap.
declare -a qemu_names=() qemu_addresses=() qemu_taps=()
private_dir=''

qemu_boot_guests() {
  private_dir=$(mktemp -d)
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
  local image=Rocky-9-GenericCloud-Base-9.8-20260525.0.x86_64.qcow2
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
  local accel=tcg cpu=max
  if [[ -e /dev/kvm ]]; then accel=kvm; cpu=host; fi
  printf 'QEMU acceleration: %s\n' "$accel" | tee "$result_dir/acceleration.txt"
  local spec node address memory disk mac tap
  for spec in "$@"; do
    IFS=: read -r node address memory disk <<< "$spec"
    [[ "$node" =~ ^[a-z][a-z0-9-]*$ ]] || { echo "Invalid guest name"; return 1; }
    mac=$(printf '52:54:00:77:00:%02x' "$((16 + ${#qemu_names[@]}))")
    tap="cwh-$node"
    qemu_names+=("$node")
    qemu_addresses+=("$address")
    qemu_taps+=("$tap")
    sudo ip tuntap add "$tap" mode tap
    sudo ip link set "$tap" master "$bridge"
    sudo ip link set "$tap" up
    qemu-img create -f qcow2 -F qcow2 -b "$private_dir/base.qcow2" "$private_dir/$node.qcow2" "$disk"
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
      - to: 0.0.0.0/0
        via: $gateway
    nameservers:
      addresses: [1.1.1.1, 8.8.8.8]
EOF
    printf 'instance-id: cwh-e2e-%s\nlocal-hostname: cwh-vm-%s\n' "$node" "$node" > "$private_dir/$node-meta.yml"
    cloud-localds --network-config="$private_dir/$node-network.yml" "$private_dir/$node-seed.iso" "$private_dir/$node-user.yml" "$private_dir/$node-meta.yml"
    sudo qemu-system-x86_64 -machine "q35,accel=$accel" -cpu "$cpu" -m "$memory" -smp 1 \
      -display none -monitor none -serial "file:$result_dir/$node-console.log" \
      -drive "file=$private_dir/$node.qcow2,if=virtio,format=qcow2" \
      -drive "file=$private_dir/$node-seed.iso,if=virtio,format=raw,readonly=on" \
      -netdev "tap,id=net0,ifname=$tap,script=no,downscript=no" \
      -device "virtio-net-pci,netdev=net0,mac=$mac" -daemonize -pidfile "$private_dir/$node.pid"
    sudo chown "$(id -u):$(id -g)" "$result_dir/$node-console.log"
    chmod 0644 "$result_dir/$node-console.log"
  done
  for index in "${!qemu_names[@]}"; do
    node=${qemu_names[$index]}
    address=${qemu_addresses[$index]}
    local ready=false fingerprint init_status
    for attempt in $(seq 1 180); do
      if grep -Fq 'failed stage init-local' "$result_dir/$node-console.log"; then
        echo "VM cloud-init local stage failed: $node"
        return 1
      fi
      if ssh-keyscan -T 2 -t ed25519 "$address" > "$private_dir/scanned-key" 2>/dev/null && [[ -s "$private_dir/scanned-key" ]]; then
        fingerprint=$(ssh-keygen -lf "$private_dir/scanned-key" -E sha256 | awk '{print $2}')
        if grep -Fq -- "$fingerprint" "$result_dir/$node-console.log"; then
          cat "$private_dir/scanned-key" >> "$private_dir/known_hosts"
          if timeout 90s ssh -F "$private_dir/ssh_config" "centos@$address" 'sudo cloud-init status --wait'; then
            ready=true
            break
          else
            init_status=$?
            if [[ "$init_status" != 255 && "$init_status" != 124 ]]; then
              ssh -F "$private_dir/ssh_config" "centos@$address" 'sudo cloud-init status --long' > "$result_dir/$node-cloud-init.txt" 2>&1 || true
              return 1
            fi
          fi
        fi
      fi
      if (( attempt % 15 == 0 )); then printf 'Waiting for verified VM SSH: %s (%s/180)\n' "$node" "$attempt"; fi
      sleep 2
    done
    [[ "$ready" == true ]] || { echo "VM cloud-init/SSH did not become ready: $node"; return 1; }
    ssh -F "$private_dir/ssh_config" "centos@$address" 'sudo getenforce; cat /proc/sys/kernel/random/boot_id'
  done
}

qemu_cleanup() {
  local index node guest_pid
  for ((index=${#qemu_names[@]}-1; index>=0; index--)); do
    node=${qemu_names[$index]}
    if [[ -f "$private_dir/$node.pid" ]]; then
      guest_pid=$(sudo cat "$private_dir/$node.pid")
      if [[ "$guest_pid" =~ ^[0-9]+$ ]]; then
        sudo kill "$guest_pid"
        for attempt in $(seq 1 10); do
          sudo kill -0 "$guest_pid" 2>/dev/null || break
          sleep 1
        done
        sudo kill -0 "$guest_pid" 2>/dev/null && sudo kill -KILL "$guest_pid"
      fi
    fi
  done
  sudo iptables -D FORWARD -i "$bridge" -j ACCEPT
  sudo iptables -D FORWARD -o "$bridge" -j ACCEPT
  sudo iptables -t nat -D POSTROUTING -s "$subnet" -j MASQUERADE
  sudo ip link del "$bridge"
  for tap in "${qemu_taps[@]}"; do sudo ip link del "$tap"; done
  if [[ -n "$private_dir" ]]; then sudo rm -rf "$private_dir"; fi
}
