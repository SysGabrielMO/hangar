#!/bin/bash
# Instala el ayudante LXC y la regla de sudo para que Hangar pueda gestionar
# los contenedores de /var/lib/lxc sin ejecutar toda la app como root.
#
#   sudo ./scripts/install-lxc-helper.sh
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Ejecuta con sudo"; exit 1; }
user=${SUDO_USER:?Ejecuta con sudo desde tu usuario normal}
here=$(dirname "$(realpath "$0")")

install -o root -g root -m 0755 "$here/hangar-lxc-helper" /usr/local/sbin/hangar-lxc-helper

rule="$user ALL=(root) NOPASSWD: /usr/local/sbin/hangar-lxc-helper"
echo "$rule" > /etc/sudoers.d/hangar
chmod 0440 /etc/sudoers.d/hangar
visudo -cf /etc/sudoers.d/hangar

echo "Listo: $user puede ejecutar 'sudo -n /usr/local/sbin/hangar-lxc-helper'"
