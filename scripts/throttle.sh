#!/bin/sh
# One tbf on the compose bridge caps the whole stack, not 40mbit per container.
set -eu
RATE="${RATE:-40mbit}"
BRIDGE="${BRIDGE:-br-debrid}"

apk add --no-cache iproute2 >/dev/null 2>&1

i=0
while [ "$i" -lt 30 ]; do
    ip link show "$BRIDGE" >/dev/null 2>&1 && break
    i=$((i + 1))
    sleep 1
done

if ! ip link show "$BRIDGE" >/dev/null 2>&1; then
    echo "bridge $BRIDGE not found - is the compose network up?" >&2
    exit 1
fi

tc qdisc replace dev "$BRIDGE" root tbf rate "$RATE" burst 2mbit latency 400ms
tc -s qdisc show dev "$BRIDGE"
echo "stack capped at $RATE on $BRIDGE"
