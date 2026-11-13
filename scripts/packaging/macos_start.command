#!/bin/sh
set -eu
READER_PACKAGE=$(CDPATH= cd "$(dirname "$0")" && pwd)
READER_LAUNCHER="$READER_PACKAGE/文献阅读器.app/Contents/MacOS/PaperReader"
if ! "$READER_LAUNCHER" "$@"; then
  printf '\n%s\n' 'The reader could not start. See ~/Library/Application Support/PaperReader/data/reader.log.'
  printf '%s\n' 'Press Return to close this window.'
  read -r READER_DISMISS
  exit 1
fi
