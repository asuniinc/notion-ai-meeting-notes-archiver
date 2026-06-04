#!/bin/zsh
set -euo pipefail

cd "${0:A:h}"

if [[ -x /usr/bin/python3 ]]; then
  PYTHON="/usr/bin/python3"
else
  PYTHON="$(command -v python3 || true)"
fi

if [[ -z "${PYTHON:-}" ]]; then
  echo "python3 が見つかりません。macOSのCommand Line Toolsをインストールしてください。"
  echo
  echo "Enterキーを押すと閉じます。"
  read -r _
  exit 1
fi

"$PYTHON" notion_ai_meeting_notes_archiver.py setup

echo
echo "Enterキーを押すと閉じます。"
read -r _
