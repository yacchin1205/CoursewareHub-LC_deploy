#!/usr/bin/env bash
set -Eeuo pipefail
result_dir="${GITHUB_WORKSPACE:-$PWD}/test-results/lc4ri-construction"
mkdir -p "$result_dir"
exec > >(tee "$result_dir/run.log") 2>&1
source tests/lc4ri/guests/qemu-guests.sh
bridge=cwh-e2e-br0
subnet=172.30.77.0/24
gateway=172.30.77.1
export LC4RI_GATEWAY=$gateway
master_ip=172.30.77.10
worker_ip=172.30.77.11
controller=cwh-lc4ri-controller
cleanup() {
  result=$?
  trap - EXIT
  set +e
  docker logs "$controller" > "$result_dir/controller.log" 2>&1
  docker cp "$controller:/home/jovyan/work/.log" "$result_dir/lc-logs"
  docker cp "$controller:/home/jovyan/work/ci_lc4ri_inventory" "$result_dir/"
  docker cp "$controller:/home/jovyan/work/group_vars" "$result_dir/"
  docker rm -f "$controller"
  for host in "$master_ip" "$worker_ip"; do
    timeout 20s ssh -F "$private_dir/ssh_config" "centos@$host" 'sudo journalctl --no-pager -n 400; sudo findmnt' > "$result_dir/$host-state.txt" 2>&1
  done
  timeout 30s ssh -F "$private_dir/ssh_config" "centos@$master_ip" "sudo sh -c 'cd /jupyter/users && find . -type d -name .log -prune -print0 | tar --null -czf - -T -'" > "$result_dir/user-lc-logs.tar.gz" 2> "$result_dir/user-lc-logs-capture.txt"
  for service in auth-proxy jupyterhub postgres registry; do
    timeout 15s ssh -F "$private_dir/ssh_config" "centos@$master_ip" "sudo docker service logs coursewarehub_$service" > "$result_dir/$service.log" 2>&1
  done
  auth_container=$(timeout 15s ssh -F "$private_dir/ssh_config" "centos@$master_ip" 'sudo docker ps -q --filter label=com.docker.swarm.service.name=coursewarehub_auth-proxy')
  if [[ -n "$auth_container" ]]; then
    timeout 15s ssh -F "$private_dir/ssh_config" "centos@$master_ip" "sudo docker exec $auth_container cat /var/www/simplesamlphp/log/simplesamlphp.log" > "$result_dir/simplesamlphp.log" 2>&1
  fi
  docker network rm cwh-lc4ri-moodle
  qemu_host_diagnostics
  docker logs cwh-lc4ri-images > "$result_dir/images-registry.log" 2>&1
  docker logs cwh-lc4ri-dns > "$result_dir/dns.log" 2>&1
  docker rm -f cwh-lc4ri-dns cwh-lc4ri-images
  qemu_cleanup
  sudo chown -R "$(id -u):$(id -g)" "$result_dir"
  exit "$result"
}
trap cleanup EXIT
export CWH_E2E_DNS_SERVER=$gateway
qemu_boot_guests "master:$master_ip:3072:40G" "worker:$worker_ip:4096:40G"
# Nested Docker services must resolve the deployment FQDN through DNS as well.
cat > "$private_dir/Corefile" <<EOF
.:53 {
  bind $gateway
  hosts {
    $master_ip coursewarehub.test
    fallthrough
  }
  forward . 1.1.1.1 8.8.8.8
}
EOF
docker run -d --name cwh-lc4ri-dns --network host \
  -v "$private_dir/Corefile:/Corefile:ro" coredns/coredns:1.12.0 -conf /Corefile
# DNS and certificate files are prerequisites explicitly supplied by the operator.
for host in "$master_ip" "$worker_ip"; do
  ssh -F "$private_dir/ssh_config" "centos@$host" "echo '$master_ip coursewarehub.test' | sudo tee -a /etc/hosts; sudo dnf install -y firewalld python3; sudo systemctl enable --now firewalld"
done
ssh -F "$private_dir/ssh_config" "centos@$master_ip" 'mkdir -p ~/certs; openssl req -x509 -newkey rsa:2048 -nodes -days 2 -keyout ~/certs/coursewarehub.test.key -out ~/certs/coursewarehub.test.cer -subj /CN=coursewarehub.test -addext subjectAltName=DNS:coursewarehub.test,IP:172.30.77.1'
ssh -F "$private_dir/ssh_config" "centos@$master_ip" 'cat ~/certs/coursewarehub.test.cer' > "$private_dir/test-ca.crt"
for host in "$master_ip" "$worker_ip"; do
  cat "$private_dir/test-ca.crt" | ssh -F "$private_dir/ssh_config" "centos@$host" 'sudo tee /etc/pki/ca-trust/source/anchors/coursewarehub-test.crt >/dev/null; sudo update-ca-trust'
done
printf '%s coursewarehub.test\n' "$master_ip" | sudo tee -a /etc/hosts
# The test CA must be trusted before the Hub's TLS clients are imported.
ssh -F "$private_dir/ssh_config" "centos@$master_ip" 'cat ~/certs/coursewarehub.test.key' > "$private_dir/test-server.key"
chmod 0600 "$private_dir/test-server.key"
cp "$private_dir/test-ca.crt" "$result_dir/test-ca.crt"
docker run -d --name cwh-lc4ri-images --network host \
  -e REGISTRY_HTTP_ADDR="$gateway:18504" \
  -e REGISTRY_HTTP_TLS_CERTIFICATE=/test-ca.crt -e REGISTRY_HTTP_TLS_KEY=/test-server.key \
  -v "$private_dir/test-ca.crt:/test-ca.crt:ro" -v "$private_dir/test-server.key:/test-server.key:ro" registry:2.8.3
sudo mkdir -p "/etc/docker/certs.d/$gateway:18504"
sudo cp "$private_dir/test-ca.crt" "/etc/docker/certs.d/$gateway:18504/ca.crt"
hub_base=niicloudoperation/coursewarehub-jupyterhub:master
docker pull "$hub_base"
docker image inspect "$hub_base" --format '{{json .RepoDigests}}' > "$result_dir/hub-base-image.json"
hub_digest=$(docker image inspect "$hub_base" --format '{{index .RepoDigests 0}}')
mkdir "$private_dir/trusted-hub"
cp tests/lc4ri/trusted-hub/Dockerfile "$private_dir/trusted-hub/"
cp "$private_dir/test-ca.crt" "$private_dir/trusted-hub/"
export LC4RI_HUB_IMAGE="$gateway:18504/coursewarehub-jupyterhub:test-ca"
docker build --build-arg BASE_IMAGE="$hub_digest" -t "$LC4RI_HUB_IMAGE" "$private_dir/trusted-hub"
docker push "$LC4RI_HUB_IMAGE"
docker image inspect "$LC4RI_HUB_IMAGE" --format '{{json .RepoDigests}}' > "$result_dir/hub-trusted-image.json"
base_image=$(awk '$1 == "FROM" {print $2; exit}' tests/lc4ri/controller/Dockerfile)
docker pull "$base_image"
docker build -t cwh-lc4ri-controller tests/lc4ri/controller
export LC4RI_TEST_TOKEN
LC4RI_TEST_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(24))')
docker run -d --name "$controller" --network host \
  -e ANSIBLE_PYTHON_INTERPRETER=/usr/bin/python3 -e ANSIBLE_DEPRECATION_WARNINGS=False \
  --entrypoint jupyter cwh-lc4ri-controller notebook \
  --ip=0.0.0.0 --port=18888 --NotebookApp.token="$LC4RI_TEST_TOKEN" \
  --NotebookApp.notebook_dir=/home/jovyan/work --NotebookApp.default_url=/tree \
  --NotebookApp.kernel_manager_class=notebook.services.kernels.kernelmanager.AsyncMappingKernelManager
docker cp "$private_dir/id_ed25519" "$controller:/home/jovyan/.ssh/ansible_id_rsa"
docker cp "$private_dir/known_hosts" "$controller:/home/jovyan/.ssh/known_hosts"
docker exec -u root "$controller" sh -c 'chmod 0600 /home/jovyan/.ssh/*; chown -R 1000:100 /home/jovyan/.ssh'
docker exec -u root "$controller" sh -c 'printf "Host *\n  BatchMode yes\n  StrictHostKeyChecking yes\n  IdentitiesOnly yes\n  IdentityFile ~/.ssh/ansible_id_rsa\n" > /home/jovyan/.ssh/config; chown 1000:100 /home/jovyan/.ssh/config; chmod 0600 /home/jovyan/.ssh/config'
docker image inspect "$base_image" --format '{{json .RepoDigests}}' > "$result_dir/lc4ri-image.json"
docker cp coursewarehub/jupyterhub-logo.png "$controller:/home/jovyan/work/jupyterhub-logo.png"
docker cp coursewarehub/D06_CoursewareHubコンテンツの配備.ipynb "$controller:/home/jovyan/work/"
docker exec -u root "$controller" chown -R 1000:100 /home/jovyan/work
export LC4RI_DB_PASSWORD
LC4RI_DB_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_hex(24))')
python3 tests/lc4ri/notebook_ui.py --result-dir "$result_dir" --master "$master_ip" --worker "$worker_ip" --construction --authentication "${LC4RI_AUTHENTICATION:-local}"
docker exec "$controller" sh -c 'test -n "$(find /home/jovyan/work/.log -type f -name "*.log" -print -quit)"'
