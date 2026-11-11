#!/bin/sh
set -eu
READER_CONTENTS=$(CDPATH= cd "$(dirname "$0")/.." && pwd)
READER_RESOURCES="$READER_CONTENTS/Resources"
case "$(uname -m)" in
  arm64|aarch64) READER_ARCH=arm64 ;;
  x86_64) READER_ARCH=x86_64 ;;
  *) printf '%s\n' 'This Mac architecture is not supported.' >&2; exit 1 ;;
esac
export READER_SHARED_BUILD=1
export PYTHONDONTWRITEBYTECODE=1
export PYTHONNOUSERSITE=1
unset PYTHONHOME PYTHONPATH
cd "$READER_RESOURCES"
exec "$READER_RESOURCES/runtime/$READER_ARCH/python/bin/python3" -B "$READER_RESOURCES/Launch-Reader.pyw" "$@"
