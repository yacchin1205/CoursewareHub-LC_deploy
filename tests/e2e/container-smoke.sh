#!/usr/bin/env bash
set -Eeuo pipefail

result_dir="${GITHUB_WORKSPACE:-$PWD}/test-results/container-smoke"
mkdir -p "$result_dir"
exec > >(tee "$result_dir/run.log") 2>&1

manager=cwh-smoke-manager
worker=cwh-smoke-worker
network=cwh-smoke-network
volume=cwh-smoke-shared
node_image=docker:27.5.1-dind

collect_and_cleanup() {
  result=$?
  trap - EXIT
  set +e
  for node in "$manager" "$worker"; do
    docker logs "$node" > "$result_dir/$node.log" 2>&1
    docker exec "$node" docker info > "$result_dir/$node-info.txt" 2>&1
    docker exec "$node" docker ps -a > "$result_dir/$node-containers.txt" 2>&1
  done
  docker exec "$manager" docker node ls > "$result_dir/nodes.txt" 2>&1
  docker exec "$manager" docker stack ps smoke --no-trunc > "$result_dir/tasks.txt" 2>&1
  docker exec "$manager" docker service logs smoke_gateway > "$result_dir/gateway.log" 2>&1
  docker exec "$manager" docker service logs smoke_worker-http > "$result_dir/worker-http.log" 2>&1
  docker rm -f "$manager" "$worker"
  docker network rm "$network"
  docker volume rm "$volume"
  printf 'Experiment exit status: %s\n' "$result" > "$result_dir/result.txt"
  exit "$result"
}
trap collect_and_cleanup EXIT

docker version
docker network create "$network"
docker volume create "$volume"
for node in "$manager" "$worker"; do
  port_args=()
  if [[ "$node" == "$manager" ]]; then
    port_args=(-p 127.0.0.1:18081:8081)
  fi
  docker run -d --privileged --name "$node" --hostname "$node" \
    --network "$network" -e DOCKER_TLS_CERTDIR= \
    -v "$volume:/ci-shared" "${port_args[@]}" "$node_image"
  ready=false
  for attempt in $(seq 1 60); do
    if docker exec "$node" docker info >/dev/null 2>&1; then
      ready=true
      break
    fi
    sleep 2
  done
  [[ "$ready" == true ]] || { echo "Docker daemon did not start: $node"; exit 1; }
  docker exec "$node" docker pull busybox:1.37.0
done

manager_ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$manager")
worker_ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$worker")
docker exec "$manager" docker swarm init --advertise-addr "$manager_ip" >/dev/null
token=$(docker exec "$manager" docker swarm join-token -q worker)
docker exec "$worker" docker swarm join --token "$token" --advertise-addr "$worker_ip" "$manager_ip:2377"
unset token

ready=false
for attempt in $(seq 1 30); do
  nodes=$(docker exec "$manager" docker node ls --format '{{.Status}} {{.Availability}}')
  if [[ $(printf '%s\n' "$nodes" | grep -c '^Ready Active$') == 2 ]]; then
    ready=true
    break
  fi
  sleep 2
done
[[ "$ready" == true ]] || { echo 'Both Swarm nodes must be Ready/Active'; exit 1; }

docker cp tests/e2e/swarm-smoke.yml "$manager:/tmp/swarm-smoke.yml"
docker exec "$manager" docker stack deploy --resolve-image never -c /tmp/swarm-smoke.yml smoke

ready=false
for attempt in $(seq 1 90); do
  if curl --fail --silent --max-time 3 http://127.0.0.1:18081/ > "$result_dir/response.txt"; then
    if [[ $(cat "$result_dir/response.txt") == swarm-worker-shared-volume-ok ]]; then
      ready=true
      break
    fi
  fi
  sleep 2
done
[[ "$ready" == true ]] || { echo 'Cross-node overlay HTTP probe failed'; exit 1; }

docker exec "$manager" test "$(docker exec "$manager" cat /ci-shared/probe.txt)" = swarm-worker-shared-volume-ok
[[ $(docker exec "$manager" docker ps -q --filter label=com.docker.swarm.service.name=smoke_gateway | wc -l) == 1 ]]
[[ $(docker exec "$worker" docker ps -q --filter label=com.docker.swarm.service.name=smoke_worker-http | wc -l) == 1 ]]
echo 'PASS: two independent Docker daemons, manager/worker placement, overlay HTTP, published port and shared bind mount'
