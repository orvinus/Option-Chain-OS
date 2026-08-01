#!/usr/bin/env bash
# Idempotent VPS hardening for the OI analytics stack. Safe to re-run.
#
# DESIGN NOTE — this script deliberately does NOT persist firewall rules.
# If something here locks you out, a reboot restores the previous state.
# Once you have confirmed a SECOND ssh session still works, run:
#     netfilter-persistent save
#
# Usage:  sudo bash scripts/harden-vps.sh
set -euo pipefail

log()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[0;32mok\033[0m   %s\n' "$*"; }
warn() { printf '    \033[0;33mwarn\033[0m %s\n' "$*"; }

[[ $EUID -eq 0 ]] || { echo "Run as root (sudo)."; exit 1; }

PUBLIC_IF="$(ip route get 8.8.8.8 2>/dev/null | grep -oP 'dev \K\S+' || true)"
[[ -n "$PUBLIC_IF" ]] || { echo "Could not detect the public interface."; exit 1; }
log "Public interface: $PUBLIC_IF"

# --- 1. Keep Docker-published ports off the internet ------------------------
# Docker BYPASSES ufw/INPUT: publishing a port writes DNAT rules that skip the
# INPUT chain entirely, so only DOCKER-USER (in FORWARD) can filter them.
# This re-asserts the rules; the real fix is binding to 127.0.0.1 in compose.
log "Blocking Docker-published DB/API ports on $PUBLIC_IF"
for port in 5432 8000; do
  if iptables -C DOCKER-USER -i "$PUBLIC_IF" -p tcp --dport "$port" -j DROP 2>/dev/null; then
    ok "DOCKER-USER drop for $port already present"
  else
    iptables -I DOCKER-USER -i "$PUBLIC_IF" -p tcp --dport "$port" -j DROP
    ok "added DOCKER-USER drop for $port"
  fi
done

# --- 2. Host firewall (ufw is removed by iptables-persistent) ---------------
# Allow rules are added BEFORE the default policy changes, so an interrupted
# run can never leave SSH unreachable.
log "Host INPUT rules"
add_input() {
  if iptables -C INPUT "$@" 2>/dev/null; then ok "INPUT $* (already)"; else
    iptables -A INPUT "$@"; ok "INPUT $*"; fi
}
add_input -i lo -j ACCEPT
add_input -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
add_input -p tcp --dport 22 -j ACCEPT
add_input -p tcp --dport 80 -j ACCEPT
add_input -p tcp --dport 443 -j ACCEPT
add_input -p icmp -j ACCEPT

if [[ "$(iptables -S INPUT | head -1)" == "-P INPUT DROP" ]]; then
  ok "INPUT policy already DROP"
else
  iptables -P INPUT DROP
  ok "INPUT policy set to DROP"
fi

# --- 3. Brute-force protection + automatic security updates ----------------
log "fail2ban + unattended-upgrades"
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq fail2ban unattended-upgrades >/dev/null
systemctl enable --now fail2ban >/dev/null 2>&1 || true
ok "fail2ban active ($(systemctl is-active fail2ban 2>/dev/null || echo unknown))"
cat >/etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
ok "unattended security upgrades enabled"

# --- 4. SSH hardening — ONLY when a key is already installed ----------------
# Disabling password auth without a working key is the classic way to lock
# yourself out of a remote box, so this is skipped unless a key exists.
log "SSH hardening"
KEYS=0
for f in /root/.ssh/authorized_keys /home/*/.ssh/authorized_keys; do
  [[ -s "$f" ]] && KEYS=$((KEYS + $(grep -cvE '^\s*(#|$)' "$f" || true)))
done
if [[ "$KEYS" -gt 0 ]]; then
  cp -n /etc/ssh/sshd_config /etc/ssh/sshd_config.bak.$(date +%F) 2>/dev/null || true
  sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
  sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/'  /etc/ssh/sshd_config
  if sshd -t; then
    systemctl restart ssh 2>/dev/null || systemctl restart sshd
    ok "password login disabled ($KEYS key(s) found); backup at /etc/ssh/sshd_config.bak.*"
  else
    warn "sshd config test FAILED — reverted nothing, SSH untouched. Check /etc/ssh/sshd_config"
  fi
else
  warn "no authorized_keys found — SKIPPING ssh hardening (would lock you out)."
  warn "run 'ssh-copy-id root@<ip>' from your PC first, then re-run this script."
fi

# --- 5. Secrets file permissions -------------------------------------------
log "Secrets"
for env in /root/nifty-oi/.env /opt/nifty-oi/.env; do
  [[ -f "$env" ]] && { chmod 600 "$env"; ok "chmod 600 $env"; }
done

cat <<'EOF'

────────────────────────────────────────────────────────────────────
NEXT — do this in THIS order:

  1. Open a NEW terminal and ssh in again. Do not close this session.
  2. If that worked:   netfilter-persistent save
     If it did NOT:    reboot from the provider console (nothing was saved).

Verify from your OWN machine (both must fail):
     nc -zv <your-host> 5432
     nc -zv <your-host> 8000
And the dashboard must still load over https.
────────────────────────────────────────────────────────────────────
EOF
