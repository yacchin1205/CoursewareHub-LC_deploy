#!/usr/bin/env bash
set -Eeuo pipefail
source tests/e2e/guest-image.env
image_dir=$1
mkdir -p "$image_dir"
image_path="$image_dir/$CWH_E2E_GUEST_IMAGE"
if [[ ! -f "$image_path" ]]; then
  curl --fail --location --retry 3 "$CWH_E2E_GUEST_IMAGE_BASE_URL/$CWH_E2E_GUEST_IMAGE" -o "$image_path.download"
  printf '%s  %s\n' "$CWH_E2E_GUEST_IMAGE_SHA256" "$image_path.download" | sha256sum -c -
  mv "$image_path.download" "$image_path"
fi
printf '%s  %s\n' "$CWH_E2E_GUEST_IMAGE_SHA256" "$image_path" | sha256sum -c -
