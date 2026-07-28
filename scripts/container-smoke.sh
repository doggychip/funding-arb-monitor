#!/bin/sh
set -eu

image_name="${IMAGE_NAME:-funding-arb-monitor:smoke}"
container_name="funding-arb-smoke-$$"
volume_name="funding-arb-smoke-data-$$"

cleanup() {
  docker rm -f "$container_name" >/dev/null 2>&1 || true
  docker volume rm "$volume_name" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

docker build -t "$image_name" .
test "$(docker run --rm "$image_name" id -u)" != "0"
docker volume create "$volume_name" >/dev/null
docker run -d \
  --name "$container_name" \
  -p 127.0.0.1::8080 \
  -v "$volume_name:/data" \
  -e FUNDING_ARB_SCHEDULER=0 \
  "$image_name" >/dev/null

host_port="$(docker port "$container_name" 8080/tcp | sed 's/.*://')"
attempt=0
until curl -fsS "http://127.0.0.1:${host_port}/healthz" >/dev/null; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 30 ]; then
    docker logs "$container_name"
    exit 1
  fi
  sleep 1
done

curl -fsS -D - -o /dev/null "http://127.0.0.1:${host_port}/" | grep -qi '^x-content-type-options: nosniff'
curl -fsS -D - -o /dev/null "http://127.0.0.1:${host_port}/api/status" | grep -qi '^cache-control: no-store'
docker exec "$container_name" funding-arb-monitor maintenance check | grep -qx 'integrity=ok'
backup_output="$(
  docker exec \
    -e FUNDING_ARB_R2_ACCOUNT_ID= \
    -e FUNDING_ARB_R2_ACCESS_KEY_ID= \
    -e FUNDING_ARB_R2_SECRET_ACCESS_KEY= \
    -e FUNDING_ARB_R2_BUCKET= \
    "$container_name" funding-arb-monitor maintenance backup
)"
backup_path="${backup_output#backup=}"
docker exec "$container_name" funding-arb-monitor --db "$backup_path" maintenance check | grep -qx 'integrity=ok'
