#!/usr/bin/env bash
set -Eeuo pipefail
result_dir="${GITHUB_WORKSPACE:-$PWD}/test-results/nfs-notebook"
mkdir -p "$result_dir"
exec > >(tee "$result_dir/run.log") 2>&1
private_dir=$(mktemp -d)
manager=cwh-nfs-master
worker=cwh-nfs-worker
network=cwh-nfs-network
volume=cwh-nfs-export

cleanup() {
  result=$?
  trap - EXIT
  set +e
  for node in "$manager" "$worker"; do
    docker logs "$node" > "$result_dir/$node.log" 2>&1
    docker exec "$node" journalctl --no-pager > "$result_dir/$node-journal.txt" 2>&1
    docker exec "$node" systemctl --failed --no-pager > "$result_dir/$node-services.txt" 2>&1
    docker exec "$node" findmnt > "$result_dir/$node-mounts.txt" 2>&1
    docker exec "$node" cat /etc/fstab > "$result_dir/$node-fstab.txt" 2>&1
    docker exec "$node" docker info > "$result_dir/$node-docker.txt" 2>&1
  done
  docker exec "$manager" exportfs -v > "$result_dir/exports.txt" 2>&1
  docker exec "$manager" docker node ls > "$result_dir/swarm-nodes.txt" 2>&1
  docker exec "$manager" docker stack ps smoke --no-trunc > "$result_dir/swarm-tasks.txt" 2>&1
  docker exec "$manager" docker service logs smoke_gateway > "$result_dir/swarm-gateway.log" 2>&1
  docker exec "$manager" docker service logs smoke_worker-http > "$result_dir/swarm-worker.log" 2>&1
  docker exec "$manager" docker stack ps coursewarehub --no-trunc > "$result_dir/coursewarehub-tasks.txt" 2>&1
  for service in auth-proxy jupyterhub postgres; do
    docker exec "$manager" docker service logs "coursewarehub_$service" > "$result_dir/coursewarehub-$service.log" 2>&1
  done
  for service_id in $(docker exec "$manager" docker service ls -q 2>/dev/null); do
    timeout 10s docker exec "$manager" docker service ps "$service_id" --no-trunc > "$result_dir/service-$service_id-tasks.txt" 2>&1
    timeout 10s docker exec "$manager" docker service logs "$service_id" > "$result_dir/service-$service_id.log" 2>&1
  done
  # Stop workloads and detach client mounts while the NFS server is still alive.
  timeout 30s docker exec "$worker" systemctl stop docker.service containerd.service
  timeout 15s docker exec "$worker" umount -lf /jupyter /exchange
  timeout 30s docker stop -t 15 "$worker"
  timeout 15s docker rm -f "$worker"
  timeout 30s docker stop -t 15 "$manager"
  timeout 15s docker rm -f "$manager"
  docker network rm "$network"
  docker volume rm "$volume"
  docker volume rm "$manager-docker" "$worker-docker"
  rm -rf "$private_dir"
  printf 'Experiment exit status: %s\n' "$result" > "$result_dir/result.txt"
  exit "$result"
}
trap cleanup EXIT

# Both guests use the runner kernel; a missing NFS module is an actual failure.
sudo modprobe nfsd
sudo modprobe nfs
docker build -t cwh-systemd-node:local tests/e2e/systemd-node
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
EOF
export ANSIBLE_SSH_ARGS="-F $private_dir/ssh_config"
export ANSIBLE_NOCOLOR=1
docker network create "$network"
docker volume create "$volume"
for node in "$manager" "$worker"; do
  volumes=()
  if [[ "$node" == "$manager" ]]; then volumes=(-v "$volume:/exported" -p 127.0.0.1:18081:8081); fi
  docker run -d --privileged --cgroupns=private --name "$node" --hostname "$node" \
    --network "$network" --tmpfs /run --tmpfs /tmp \
    -v "$private_dir/id_ed25519.pub:/ci-authorized-key:ro" \
    -v "$node-docker:/var/lib/docker" \
    "${volumes[@]}" cwh-systemd-node:local
  ready=false
  for attempt in $(seq 1 60); do
    if docker exec "$node" systemctl is-active --quiet sshd; then ready=true; break; fi
    sleep 2
  done
  [[ "$ready" == true ]] || { echo "systemd/sshd did not start: $node"; exit 1; }
  node_ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$node")
  host_key=$(docker exec "$node" cat /etc/ssh/ssh_host_ed25519_key.pub)
  printf '%s %s\n' "$node_ip" "$host_key" >> "$private_dir/known_hosts"
done
manager_ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$manager")
worker_ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$worker")
python tests/e2e/run-nfs-notebook.py --result-dir "$result_dir" \
  --key "$private_dir/id_ed25519" --manager-ip "$manager_ip" --worker-ip "$worker_ip" \
  --worker-container "$worker" --manager-container "$manager"
