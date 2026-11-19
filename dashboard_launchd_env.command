#!/usr/bin/env bash
# Hydrate runtime-only Together configuration from the user launchd session.
# Credentials are not written to a plist, application log, or child command line.
set -euo pipefail

launchctl_bin="/bin/launchctl"
allowed_hosts_max_bytes=8192

load_launchd_env() {
  local name="$1"
  local value

  # An explicitly supplied process environment wins over the launchd
  # fallback. This keeps one-off checks from accidentally using a stale
  # launchd value while still hydrating the dashboard job, whose plist does
  # not contain provider credentials.
  if [[ "${!name+x}" == "x" ]]; then
    return 0
  fi

  value="$("$launchctl_bin" getenv "$name" 2>/dev/null || true)"
  if [[ -n "$value" ]]; then
    export "$name=$value"
  fi
}

load_launchd_env TOGETHER_API_KEY
load_launchd_env TOGETHER_API_BASE_URL
load_launchd_env TOGETHER_SEO_MODEL
load_launchd_env TOGETHER_MODEL
load_launchd_env TOGETHER_REASONING_EFFORT

invalid_allowed_hosts() {
  printf 'dashboard launchd environment: invalid allowed-hosts configuration\n' >&2
  return 1
}

valid_allowed_host() {
  local host="$1"
  local label remainder octet
  local -a octets

  [[ -n "$host" && ${#host} -le 253 ]] || return 1
  [[ "$host" =~ ^[A-Za-z0-9.-]+$ ]] || return 1

  # Four numeric labels identify an IPv4 address and must have valid octets.
  if [[ "$host" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    local IFS=.
    read -r -a octets <<< "$host"
    [[ ${#octets[@]} -eq 4 ]] || return 1
    for octet in "${octets[@]}"; do
      [[ "$octet" =~ ^[0-9]{1,3}$ ]] || return 1
      (( 10#$octet <= 255 )) || return 1
    done
    return 0
  fi

  remainder="$host"
  while [[ "$remainder" == *.* ]]; do
    label="${remainder%%.*}"
    [[ ${#label} -le 63 && "$label" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$ ]] || return 1
    remainder="${remainder#*.}"
  done
  [[ ${#remainder} -le 63 && "$remainder" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$ ]]
}

valid_allowed_hosts_csv() {
  local value="$1"
  local remainder entry

  # Count and match bytes in the ASCII range so locale settings cannot widen
  # the accepted hostname character set.
  local LC_ALL=C
  [[ -n "$value" && ${#value} -le "$allowed_hosts_max_bytes" ]] || return 1
  [[ "$value" =~ ^[A-Za-z0-9.,-]+$ ]] || return 1

  remainder="$value"
  while [[ "$remainder" == *,* ]]; do
    entry="${remainder%%,*}"
    [[ -n "$entry" ]] || return 1
    valid_allowed_host "$entry" || return 1
    remainder="${remainder#*,}"
  done
  [[ -n "$remainder" ]] && valid_allowed_host "$remainder"
}

load_allowed_hosts() {
  local hosts_file file_bytes allowed_hosts

  # A process-level setting always takes precedence, including an explicit
  # empty value that intentionally leaves the dashboard loopback-only.
  if [[ "${ETSY_DASHBOARD_ALLOWED_HOSTS+x}" == "x" ]]; then
    if [[ -n "$ETSY_DASHBOARD_ALLOWED_HOSTS" ]]; then
      valid_allowed_hosts_csv "$ETSY_DASHBOARD_ALLOWED_HOSTS" || invalid_allowed_hosts
    fi
    return 0
  fi

  hosts_file="${ETSY_DASHBOARD_PUBLIC_HOSTS_FILE:-${HOME:-}/.config/etsy-dashboard/public-hosts}"
  if [[ -L "$hosts_file" || -e "$hosts_file" ]]; then
    [[ ! -L "$hosts_file" && -f "$hosts_file" ]] || invalid_allowed_hosts

    file_bytes="$(LC_ALL=C wc -c < "$hosts_file" 2>/dev/null)" || invalid_allowed_hosts
    file_bytes="${file_bytes//[[:space:]]/}"
    [[ "$file_bytes" =~ ^[0-9]+$ && "$file_bytes" -le "$allowed_hosts_max_bytes" ]] || invalid_allowed_hosts

    allowed_hosts=''
    IFS= read -r -d '' allowed_hosts < "$hosts_file" 2>/dev/null || true
    [[ ${#allowed_hosts} -eq "$file_bytes" ]] || invalid_allowed_hosts
    valid_allowed_hosts_csv "$allowed_hosts" || invalid_allowed_hosts
    export ETSY_DASHBOARD_ALLOWED_HOSTS="$allowed_hosts"
  fi
}

load_allowed_hosts

exec "$@"
