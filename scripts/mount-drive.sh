#!/usr/bin/env bash
#
# mount-drive.sh -- format (optional) and auto-mount a data drive by UUID.
#
# Usage: sudo ./scripts/mount-drive.sh <device> <mountpoint> [--format]
#   <device>      block device, e.g. /dev/sdb
#   <mountpoint>  where to mount it, e.g. /mnt/data
#   --format      DESTRUCTIVE: GPT-label the disk, make one ext4 partition
#                 spanning it, and mkfs.ext4. Omit to mount an existing fs.
#
# Safety: with --format you must re-type the device name to confirm before
# anything destructive happens.

set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "This script must run as root (use sudo)." >&2
    exit 1
fi

if [[ $# -lt 2 ]]; then
    echo "Usage: sudo $0 <device> <mountpoint> [--format]" >&2
    exit 1
fi

DEVICE="$1"
MOUNTPOINT="$2"
MODE="mount-only"
if [[ "${3:-}" == "--format" ]]; then
    MODE="format"
fi

if [[ ! -b "${DEVICE}" ]]; then
    echo "Error: ${DEVICE} is not a block device." >&2
    exit 1
fi

echo "Device:     ${DEVICE}"
echo "Mountpoint: ${MOUNTPOINT}"
echo "Mode:       ${MODE}"
echo ""

# ---------------------------------------------------------------------------
# Format path (destructive) -- requires re-typing the device name.
# ---------------------------------------------------------------------------
if [[ "${MODE}" == "format" ]]; then
    echo "WARNING: this will ERASE ALL DATA on ${DEVICE}."
    read -r -p "Re-type the device name (${DEVICE}) to confirm: " confirm
    if [[ "${confirm}" != "${DEVICE}" ]]; then
        echo "Confirmation did not match; aborting." >&2
        exit 1
    fi

    echo "==> Creating GPT label and one ext4 partition spanning the disk..."
    parted -s "${DEVICE}" mklabel gpt
    parted -s "${DEVICE}" mkpart primary ext4 0% 100%
    # Let the kernel register the new partition table.
    partprobe "${DEVICE}" || udevadm settle || sleep 2

    # Detect the partition name: nvme0n1 -> nvme0n1p1, sdb -> sdb1.
    if [[ "${DEVICE}" == *nvme* || "${DEVICE}" == *mmcblk* ]]; then
        PARTITION="${DEVICE}p1"
    else
        PARTITION="${DEVICE}1"
    fi

    echo "==> Formatting ${PARTITION} as ext4..."
    mkfs.ext4 -F "${PARTITION}"
else
    # Mount-only: if the whole device has no partition table, treat the device
    # itself as the filesystem; otherwise use its first partition.
    if [[ "${DEVICE}" == *nvme* || "${DEVICE}" == *mmcblk* ]]; then
        CANDIDATE="${DEVICE}p1"
    else
        CANDIDATE="${DEVICE}1"
    fi
    if [[ -b "${CANDIDATE}" ]]; then
        PARTITION="${CANDIDATE}"
    else
        PARTITION="${DEVICE}"
    fi
fi

# ---------------------------------------------------------------------------
# Mountpoint + fstab (by UUID, with nofail).
# ---------------------------------------------------------------------------
mkdir -p "${MOUNTPOINT}"

UUID="$(blkid -s UUID -o value "${PARTITION}")"
if [[ -z "${UUID}" ]]; then
    echo "Error: could not read UUID of ${PARTITION}." >&2
    exit 1
fi
echo "==> ${PARTITION} UUID=${UUID}"

# nofail so a missing data drive never blocks boot.
FSTAB_LINE="UUID=${UUID} ${MOUNTPOINT} ext4 defaults,nofail 0 2"
if grep -q "UUID=${UUID}" /etc/fstab; then
    echo "    UUID already in /etc/fstab; not adding again."
else
    echo "${FSTAB_LINE}" >> /etc/fstab
    echo "    added to /etc/fstab: ${FSTAB_LINE}"
fi

echo "==> Mounting..."
mount "${MOUNTPOINT}"

echo ""
df -h "${MOUNTPOINT}"
