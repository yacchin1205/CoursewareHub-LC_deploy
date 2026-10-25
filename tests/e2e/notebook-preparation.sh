#!/usr/bin/env bash
set -Eeuo pipefail

result_dir="${GITHUB_WORKSPACE:-$PWD}/test-results/notebook-preparation"
mkdir -p "$result_dir"
exec > >(tee "$result_dir/run.log") 2>&1

manager=cwh-preparation-manager
worker=cwh-preparation-worker
network=cwh-preparation-network
node_image=cwh-preparation-node:local
private_dir=$(mktemp -d)

collect_and_cleanup() {
  result=$?
  trap - EXIT
  set +e
  for node in "$manager" "$worker"; do
    docker logs "$node" > "$result_dir/$node.log" 2>&1
    docker exec "$node" docker info > "$result_dir/$node-info.txt" 2>&1
    docker exec "$node" docker ps -a > "$result_dir/$node-containers.txt" 2>&1
    docker exec "$node" cat /tmp/sshd.log > "$result_dir/$node-sshd.log" 2>&1
  done
  docker rm -f "$manager" "$worker"
  docker network rm "$network"
  rm -rf "$private_dir"
  printf 'Test exit status: %s\n' "$result" > "$result_dir/result.txt"
  exit "$result"
}
trap collect_and_cleanup EXIT

docker version
docker build -t "$node_image" tests/e2e/node
ssh-keygen -q -t ed25519 -N '' -f "$private_dir/id_ed25519"
touch "$private_dir/known_hosts"
cat > "$private_dir/ssh_config" <<EOF
Host *
  BatchMode yes
  StrictHostKeyChecking yes
  UserKnownHostsFile $private_dir/known_hosts
  IdentityFile $private_dir/id_ed25519
  IdentitiesOnly yes
EOF
export ANSIBLE_SSH_ARGS="-F $private_dir/ssh_config"
export ANSIBLE_NOCOLOR=1
docker network create "$network"
for node in "$manager" "$worker"; do
  docker run -d --privileged --name "$node" --hostname "$node" \
    --network "$network" -e DOCKER_TLS_CERTDIR= \
    -v "$private_dir/id_ed25519.pub:/run/ci-authorized-key:ro" \
    "$node_image"
  ready=false
  for attempt in $(seq 1 60); do
    if docker exec "$node" docker info >/dev/null 2>&1; then
      ready=true
      break
    fi
    sleep 2
  done
  [[ "$ready" == true ]] || { echo "Docker daemon did not start: $node"; exit 1; }
done

manager_ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$manager")
worker_ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$worker")
for node in "$manager" "$worker"; do
  node_ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$node")
  host_key=$(docker exec "$node" cat /etc/ssh/ssh_host_ed25519_key.pub)
  printf '%s %s\n' "$node_ip" "$host_key" >> "$private_dir/known_hosts"
done
python tests/e2e/run-notebook-preparation.py --result-dir "$result_dir" \
  --key "$private_dir/id_ed25519" --manager-ip "$manager_ip" --worker-ip "$worker_ip"

echo 'PASS: D01/D02 SSH validation, inventory generation and connection failure propagation'
