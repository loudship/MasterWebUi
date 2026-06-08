#!/usr/bin/env bash
# harden_mounts.sh
# This script configures /etc/wsl.conf to automate DrvFs Windows drive mounts
# and enforces POSIX file permission mapping so Docker tools can manipulate files.

# Ensure script is run as root or with sudo
if [ "$EUID" -ne 0 ]; then
  echo "Please run as root (e.g., sudo ./harden_mounts.sh)"
  exit 1
fi

WSL_CONF="/etc/wsl.conf"

echo "Configuring DrvFs mounts in $WSL_CONF..."

# Create or update the [automount] section
if grep -q "^\[automount\]" "$WSL_CONF" 2>/dev/null; then
    echo "Existing [automount] section found. Updating options..."
    # If the file already has [automount] options, we can replace them or just warn.
    # To be safe, we will just rebuild the file carefully or append if not present.
    # For a declarative approach, we'll write a clean configuration block.
fi

# We will write the configuration block. If there's an existing file, we append or replace.
# To be robust, let's create a backup and then write the block.
cp "$WSL_CONF" "${WSL_CONF}.bak" 2>/dev/null || true

# We will remove any existing [automount] section and options to avoid duplicates, 
# but for simplicity in a fresh environment, we'll just write it.
# A robust way is to use a temporary file:
cat << 'EOF' > /tmp/wsl_automount_tmp.conf
[automount]
enabled = true
options = "metadata,uid=1000,gid=1000,umask=022"
EOF

# Merge it into wsl.conf (replace automount block if exists)
awk '
/^\[automount\]/ { in_automount=1; next }
/^\[/ && in_automount { in_automount=0 }
!in_automount { print }
' "$WSL_CONF" > /tmp/wsl.conf.new 2>/dev/null || true

cat /tmp/wsl_automount_tmp.conf >> /tmp/wsl.conf.new
mv /tmp/wsl.conf.new "$WSL_CONF"

echo "Permissions hardening applied to $WSL_CONF."
echo "Please restart WSL from Windows using 'wsl --shutdown' for these changes to take effect."
