#!/bin/zsh
# Сборка QA-пакета таймшита: PyInstaller one-dir + INSTALL.md + .command → zip.
# Запускать на macOS arm64 из чистой рабочей копии; артефакт — dist/lpdev-qa-<arch>-<commit>.zip
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
cd "$PROJECT_DIR"

[ "$(uname)" = "Darwin" ] || { echo "Сборка QA-пакета — только на macOS"; exit 1; }
ARCH="$(uname -m)"
[ "$ARCH" = "arm64" ] || { echo "Поддержана сборка для arm64 (Homebrew python); текущая архитектура: $ARCH"; exit 1; }
[ -x /opt/homebrew/bin/python3.12 ] || { echo "Нужен python3.12 из Homebrew: /opt/homebrew/bin/python3.12"; exit 1; }
[ -z "$(git status --porcelain)" ] || {
  echo "Рабочая копия не чистая — собирайте из закоммиченного состояния (provenance: commit + sha256 в посте к сборке)"
  git status --short
  exit 1
}

COMMIT="$(git rev-parse HEAD)"
SHORT="$(git rev-parse --short HEAD)"
BUILD_DIR="$PROJECT_DIR/build-qa"
STAGE_DIR="$PROJECT_DIR/dist-qa"
OUT_DIR="$PROJECT_DIR/dist"
rm -rf "$BUILD_DIR" "$STAGE_DIR"
mkdir -p "$BUILD_DIR" "$STAGE_DIR" "$OUT_DIR"

/opt/homebrew/bin/python3.12 -m venv "$BUILD_DIR/venv"
"$BUILD_DIR/venv/bin/pip" install -q -e . "pyinstaller==6.22.3"
"$BUILD_DIR/venv/bin/pyinstaller" --noconfirm --clean \
  --workpath "$BUILD_DIR/work" --specpath "$BUILD_DIR/work" --distpath "$STAGE_DIR" \
  --name lpdev-qa \
  --add-data "$PROJECT_DIR/src/lpdev/web/templates:lpdev/web/templates" \
  --add-data "$PROJECT_DIR/src/lpdev/web/static:lpdev/web/static" \
  --add-data "$PROJECT_DIR/src/lpdev/calendar:lpdev/calendar" \
  --hidden-import uvicorn.logging \
  --hidden-import uvicorn.loops \
  --hidden-import uvicorn.loops.auto \
  --hidden-import uvicorn.loops.asyncio \
  --hidden-import uvicorn.protocols \
  --hidden-import uvicorn.protocols.http \
  --hidden-import uvicorn.protocols.http.auto \
  --hidden-import uvicorn.protocols.http.h11_impl \
  --hidden-import uvicorn.protocols.websockets \
  --hidden-import uvicorn.protocols.websockets.auto \
  --hidden-import uvicorn.lifespan \
  --hidden-import uvicorn.lifespan.on \
  --hidden-import uvicorn.lifespan.off \
  --hidden-import anyio._backends._asyncio \
  "$PROJECT_DIR/src/lpdev/qa_main.py"

codesign --force --sign - "$STAGE_DIR/lpdev-qa/lpdev-qa"

cp deploy/qa/INSTALL.md "$STAGE_DIR/"
cp "deploy/qa/Lemanapro Timesheet.command" "$STAGE_DIR/"
chmod +x "$STAGE_DIR/Lemanapro Timesheet.command"

ZIP_NAME="lpdev-qa-${ARCH}-${SHORT}.zip"
(cd "$STAGE_DIR" && rm -f "$ZIP_NAME" && zip -qry "$ZIP_NAME" lpdev-qa INSTALL.md "Lemanapro Timesheet.command")
mv "$STAGE_DIR/$ZIP_NAME" "$OUT_DIR/$ZIP_NAME"

echo
echo "Готово: dist/$ZIP_NAME"
echo "commit: $COMMIT"
echo "sha256: $(shasum -a 256 "$OUT_DIR/$ZIP_NAME" | awk '{print $1}')"
