#!/bin/sh
# The container's first-boot setup (Dockerfile), then whatever CMD says.
#
# A container binds every interface (HOST=0.0.0.0), and a non-loopback bind
# refuses to boot without an owner token (SPEC §11.1). So the first boot does
# what install.sh and `yurios pair` would have: start .env from .env.example,
# generate the token into it, and print the link that signs a browser in. Every
# later boot finds both in the volume and goes straight to CMD.
set -eu

ROOT="${YURIOS_ROOT:-/yurios}"
cd "$ROOT"

# Upsert KEY=value in the .env being built.
set_default() {
    if grep -q "^$1=" .env.new; then
        sed -i "s#^$1=.*#$1=$2#" .env.new
    else
        printf '%s=%s\n' "$1" "$2" >> .env.new
    fi
}

if [ ! -f .env ]; then
    # Built aside and moved into place, so a boot that dies half way leaves no
    # .env at all and the next one starts over, rather than one that skips this.
    install -m 600 /opt/yurios/.env.example .env.new   # it is about to hold the token
    # localhost is this container, so the defaults would never find a model
    # server on the host. Point them at it instead (compose maps the name) —
    # before the first boot, which seeds the default connection profile from
    # these. Only on creation: after that the values are yours.
    set_default LMSTUDIO_BASE_URL http://host.docker.internal:1234/v1
    set_default OLLAMA_BASE_URL http://host.docker.internal:11434
    mv .env.new .env
    echo "Created $ROOT/.env from .env.example (LM Studio and Ollama: host.docker.internal)"
fi

if ! grep -Eq '^OWNER_TOKEN=.+' .env && [ -z "${OWNER_TOKEN:-}" ]; then
    # She is not running yet, so `pair` writes the token to .env and prints the
    # link. The link names the address the published port is reached on, which
    # nothing inside the container can know: YURIOS_URL says it.
    yurios pair --no-color --url "${YURIOS_URL:-http://localhost:8768}"
    echo
    echo "Open that link once in each browser you use. Print it again with:"
    echo "  docker compose exec yurios yurios pair --url ${YURIOS_URL:-http://localhost:8768}"
    echo
fi

exec "$@"
