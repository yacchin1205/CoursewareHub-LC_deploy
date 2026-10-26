#!/usr/bin/env bash
set -Eeuo pipefail
result_dir="${GITHUB_WORKSPACE:-$PWD}/test-results/lc4ri"
mkdir -p "$result_dir"
exec > >(tee "$result_dir/run.log") 2>&1
private_dir=$(mktemp -d)
network=cwh-lc4ri
controller=cwh-lc4ri-controller
nodes=(cwh-lc4ri-master cwh-lc4ri-worker)
cleanup() {
  result=$?
  trap - EXIT
  set +e
  for node in "$controller" "${nodes[@]}"; do
    docker logs "$node" > "$result_dir/$node.log" 2>&1
  done
  docker cp "$controller:/home/jovyan/work/.log" "$result_dir/lc-logs"
  docker cp "$controller:/home/jovyan/work/ci_lc4ri_inventory" "$result_dir/"
  docker cp "$controller:/home/jovyan/work/group_vars" "$result_dir/"
  docker rm -f "$controller" "${nodes[@]}"
  docker network rm "$network"
  sudo chown -R "$(id -u):$(id -g)" "$result_dir"
  rm -rf "$private_dir"
  exit "$result"
}
trap cleanup EXIT
ssh-keygen -q -t ed25519 -N '' -f "$private_dir/id_ed25519"
docker network create "$network"
docker build -t cwh-lc4ri-ssh tests/lc4ri/ssh-node
base_image=$(awk '$1 == "FROM" {print $2; exit}' tests/lc4ri/controller/Dockerfile)
docker pull "$base_image"
docker build -t cwh-lc4ri-controller tests/lc4ri/controller
controller_image=cwh-lc4ri-controller
controller_network=(--network "$network" -p 127.0.0.1:18888:8888)
notebook_port=8888
controller_environment=()
if [[ "${LC4RI_API_FAILURES:-0}" == 1 ]]; then
  docker build -t cwh-lc4ri-aws tests/lc4ri/aws-controller
  controller_image=cwh-lc4ri-aws
  controller_network=(--network host)
  notebook_port=18888
  controller_environment=(-e AWS_ACCESS_KEY_ID=testing -e AWS_SECRET_ACCESS_KEY=testing
    -e AWS_DEFAULT_REGION=ap-northeast-1 -e AWS_EC2_METADATA_DISABLED=true
    -e AWS_ENDPOINT_URL=http://127.0.0.1:18503 -e AWS_MAX_ATTEMPTS=1)
fi
for node in "${nodes[@]}"; do
  docker run -d --name "$node" --network "$network" \
    -v "$private_dir/id_ed25519.pub:/ci-key.pub:ro" cwh-lc4ri-ssh
  address=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$node")
  ready=false
  for attempt in $(seq 1 30); do
    if docker exec "$node" test -f /etc/ssh/ssh_host_ed25519_key.pub; then ready=true; break; fi
    sleep 1
  done
  [[ "$ready" == true ]] || { echo 'SSH node did not initialize'; exit 1; }
  host_key=$(docker exec "$node" cat /etc/ssh/ssh_host_ed25519_key.pub)
  printf '%s %s\n' "$address" "$host_key" >> "$private_dir/known_hosts"
done
master_ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "${nodes[0]}")
worker_ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "${nodes[1]}")
export LC4RI_TEST_TOKEN
LC4RI_TEST_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(24))')
docker run -d --name "$controller" "${controller_network[@]}" "${controller_environment[@]}" \
  --entrypoint jupyter "$controller_image" notebook \
  --ip=0.0.0.0 --port="$notebook_port" --NotebookApp.token="$LC4RI_TEST_TOKEN" \
  --NotebookApp.notebook_dir=/home/jovyan/work --NotebookApp.default_url=/tree \
  --NotebookApp.kernel_manager_class=notebook.services.kernels.kernelmanager.AsyncMappingKernelManager
docker cp "$private_dir/id_ed25519" "$controller:/home/jovyan/.ssh/ansible_id_rsa"
docker cp "$private_dir/known_hosts" "$controller:/home/jovyan/.ssh/known_hosts"
docker exec -u root "$controller" sh -c 'chmod 0600 /home/jovyan/.ssh/*; chown -R 1000:100 /home/jovyan/.ssh'
docker exec -u root "$controller" sh -c 'printf "Host *\n  BatchMode yes\n  StrictHostKeyChecking yes\n  IdentitiesOnly yes\n  IdentityFile ~/.ssh/ansible_id_rsa\n" > /home/jovyan/.ssh/config; chown 1000:100 /home/jovyan/.ssh/config; chmod 0600 /home/jovyan/.ssh/config'
docker image inspect "$base_image" --format '{{json .RepoDigests}}' > "$result_dir/lc4ri-image.json"
extra_args=()
if [[ "${LC4RI_API_FAILURES:-0}" == 1 ]]; then extra_args+=(--api-failures); fi
if [[ "${LC4RI_INPUT_FAILURES:-0}" == 1 ]]; then extra_args+=(--input-failures); fi
python3 tests/lc4ri/notebook_ui.py --result-dir "$result_dir" --master "$master_ip" --worker "$worker_ip" "${extra_args[@]}"
docker exec "$controller" sh -c 'test -n "$(find /home/jovyan/work/.log -type f -name "*.log" -print -quit)"'
