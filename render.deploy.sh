#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$ROOT"

echo "Preparando deploy para o Render (diretório: $ROOT)"

DATA_DIR="$ROOT/data"
mkdir -p "$DATA_DIR"

db_path="${SKYSCANNER_DB_PATH:-$DATA_DIR/flight_tracker_browser.db}"
export SKYSCANNER_DB_PATH="$db_path"
touch "$db_path"

PLAYWRIGHT_DIR="${SKYSCANNER_USER_DATA_DIR:-$DATA_DIR/playwright-profile}"
mkdir -p "$PLAYWRIGHT_DIR"
export SKYSCANNER_USER_DATA_DIR="$PLAYWRIGHT_DIR"

REQ_FILE="requirements.txt"
if [[ ! -f "$REQ_FILE" ]]; then
  echo "Arquivo de dependências $REQ_FILE não encontrado" >&2
  exit 1
fi

echo "Instalando dependências (...)"
pip install -r "$REQ_FILE"

if ! command -v playwright &>/dev/null; then
  echo "Playwright não encontrado. Instalando via python -m playwright..."
  python3 -m playwright install chromium
else
  echo "Playwright já disponível: instalando navegadores..."
  python3 -m playwright install chromium
fi

python3 - <<'PY'
from skyscanner import Database
import os
path = os.getenv('SKYSCANNER_DB_PATH') or 'flight_tracker_browser.db'
Database(path)
print(f"Banco inicializado: {path}")
PY

echo "" 
cat <<'EOF'
Deploy pronto para Render:
  * banco SQLite em: $SKYSCANNER_DB_PATH
  * perfil Playwright em: $SKYSCANNER_USER_DATA_DIR
  * confira render.yaml / Procfile no repositório.
  * suba tudo no Git e conecte ao Render.
EOF
