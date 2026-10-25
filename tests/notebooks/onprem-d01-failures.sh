#!/usr/bin/env bash
set -Eeuo pipefail
result_dir="${GITHUB_WORKSPACE:-$PWD}/test-results/notebook-failures/coursewarehub-onprem-d01"
mkdir -p "$result_dir"
private_dir=$(mktemp -d)
node=cwh-ssh-rejection
cleanup() {
  result=$?
  trap - EXIT
  set +e
  docker logs "$node" > "$result_dir/ssh-node.log" 2>&1
  docker exec "$node" cat /tmp/sshd.log > "$result_dir/sshd.log" 2>&1
  docker rm -f "$node"
  rm -rf "$private_dir"
  exit "$result"
}
trap cleanup EXIT
docker build -t cwh-ssh-node:local tests/e2e/node
ssh-keygen -q -t ed25519 -N '' -f "$private_dir/id_ed25519"
docker run -d --privileged --name "$node" -e DOCKER_TLS_CERTDIR= \
  -v "$private_dir/id_ed25519.pub:/run/ci-authorized-key:ro" cwh-ssh-node:local
ready=false
for attempt in $(seq 1 60); do
  if docker exec "$node" docker info >/dev/null 2>&1; then ready=true; break; fi
  sleep 2
done
[[ "$ready" == true ]] || { echo 'SSH fixture did not start'; exit 1; }
address=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$node")
host_key=$(docker exec "$node" cat /etc/ssh/ssh_host_ed25519_key.pub)
printf '%s %s\n' "$address" "$host_key" > "$private_dir/known_hosts"
export ANSIBLE_SSH_ARGS="-o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$private_dir/known_hosts -o IdentitiesOnly=yes"
# Ensure the fixture is reachable before testing a rejected identity.
ansible all -i "$address," -u centos --private-key "$private_dir/id_ed25519" -m ping
python tests/notebooks/run-failures.py --notebook coursewarehub-onprem-d01 \
  --result-dir "$result_dir" --ssh-host "$address" --ssh-key "$private_dir/id_ed25519"
