# Called with the CoursewareHub master first, followed by its workers.
prepare_coursewarehub_prerequisites() {
  local master_ip=$1 host hub_base hub_digest
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
  run_with_timeout 10m docker run -d --name cwh-lc4ri-dns --network host \
    -v "$private_dir/Corefile:/Corefile:ro" coredns/coredns:1.12.0 -conf /Corefile
  # DNS and certificate files are prerequisites explicitly supplied by the operator.
  for host in "$@"; do
    run_with_timeout 10m ssh -F "$private_dir/ssh_config" "centos@$host" "echo '$master_ip coursewarehub.test' | sudo tee -a /etc/hosts; sudo dnf install -y firewalld python3; sudo systemctl enable --now firewalld"
  done
  run_with_timeout 60s ssh -F "$private_dir/ssh_config" "centos@$master_ip" "mkdir -p ~/certs; openssl req -x509 -newkey rsa:2048 -nodes -days 2 -keyout ~/certs/coursewarehub.test.key -out ~/certs/coursewarehub.test.cer -subj /CN=coursewarehub.test -addext subjectAltName=DNS:coursewarehub.test,IP:$gateway"
  run_with_timeout 60s ssh -F "$private_dir/ssh_config" "centos@$master_ip" 'cat ~/certs/coursewarehub.test.cer' > "$private_dir/test-ca.crt"
  for host in "$@"; do
    cat "$private_dir/test-ca.crt" | run_with_timeout 60s ssh -F "$private_dir/ssh_config" "centos@$host" 'sudo tee /etc/pki/ca-trust/source/anchors/coursewarehub-test.crt >/dev/null; sudo update-ca-trust'
  done
  printf '%s coursewarehub.test\n' "$master_ip" | sudo tee -a /etc/hosts
  run_with_timeout 60s ssh -F "$private_dir/ssh_config" "centos@$master_ip" 'cat ~/certs/coursewarehub.test.key' > "$private_dir/test-server.key"
  chmod 0600 "$private_dir/test-server.key"
  cp "$private_dir/test-ca.crt" "$result_dir/test-ca.crt"
  run_with_timeout 10m docker run -d --name cwh-lc4ri-images --network host \
    -e REGISTRY_HTTP_ADDR="$gateway:18504" \
    -e REGISTRY_HTTP_TLS_CERTIFICATE=/test-ca.crt -e REGISTRY_HTTP_TLS_KEY=/test-server.key \
    -v "$private_dir/test-ca.crt:/test-ca.crt:ro" -v "$private_dir/test-server.key:/test-server.key:ro" registry:2.8.3
  sudo mkdir -p "/etc/docker/certs.d/$gateway:18504"
  sudo cp "$private_dir/test-ca.crt" "/etc/docker/certs.d/$gateway:18504/ca.crt"
  hub_base=niicloudoperation/coursewarehub-jupyterhub:master
  run_with_timeout 15m docker pull "$hub_base"
  run_with_timeout 60s docker image inspect "$hub_base" --format '{{json .RepoDigests}}' > "$result_dir/hub-base-image.json"
  hub_digest=$(run_with_timeout 60s docker image inspect "$hub_base" --format '{{index .RepoDigests 0}}')
  # Keep the Hub image's public roots alongside the CA for the fixture registry.
  run_with_timeout 60s docker run --rm --entrypoint cat "$hub_digest" /etc/ssl/certs/ca-certificates.crt > "$private_dir/hub-ca-bundle.pem"
  cat "$private_dir/test-ca.crt" >> "$private_dir/hub-ca-bundle.pem"
  run_with_timeout 60s ssh -F "$private_dir/ssh_config" "centos@$master_ip" 'install -m 0644 /dev/stdin ~/certs/hub-ca-bundle.pem' < "$private_dir/hub-ca-bundle.pem"
  export LC4RI_HUB_IMAGE="$gateway:18504/coursewarehub-jupyterhub:platform"
  docker tag "$hub_digest" "$LC4RI_HUB_IMAGE"
  run_with_timeout 10m docker push "$LC4RI_HUB_IMAGE"
  run_with_timeout 60s docker image inspect "$LC4RI_HUB_IMAGE" --format '{{json .RepoDigests}}' > "$result_dir/hub-image.json"
}
