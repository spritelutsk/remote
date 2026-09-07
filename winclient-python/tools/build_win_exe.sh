#!/bin/bash
# Сборка Windows-exe из Linux через Wine.
#
# PyInstaller не умеет кросс-компиляцию: exe собирается только Windows-питоном.
# Скрипт разворачивает такой питон в wine-префиксе и запускает сборку в нём.
# На Windows всё это не нужно — там достаточно:
#     pip install pyinstaller pywebview
#     pyinstaller --noconsole --onefile --name CortenDeskRemote main.py
#
# Требуется: wine (только 64-битный), xvfb, msitools, curl.
#     sudo apt-get install -y --no-install-recommends wine wine64 xvfb msitools
#
# Про MSI. Официальный python-3.12.8-amd64.exe — 32-битный бутстрап WiX, и
# 64-битному wine он не по зубам. Но компоненты установщика лежат на python.org
# отдельными MSI, а msiextract распаковывает их вообще без Windows. Ставить
# wine32 через multiarch не нужно.
#
# Про nuget-пакет python: в нём НЕТ tkinter и tcl, для этого приложения он не
# годится. Именно поэтому здесь MSI, а не он.

set -euo pipefail

PY_VERSION="${PY_VERSION:-3.12.8}"
export WINEPREFIX="${WINEPREFIX:-$HOME/.wine-py}"
export WINEARCH=win64
export WINEDEBUG=-all
export WINEDLLOVERRIDES="mscoree,mshtml="   # без этого wine спросит про mono/gecko и повиснет
export PYTHONUTF8=1
export DISPLAY="${DISPLAY:-:98}"

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
WINPY='C:\Py312\python.exe'

cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

# --- 1. виртуальный дисплей: wine и tkinter без X не стартуют ---------------
if ! xdpyinfo >/dev/null 2>&1; then
  Xvfb "$DISPLAY" -screen 0 1280x800x24 >/dev/null 2>&1 &
  sleep 3
fi

# --- 2. Windows-питон в префиксе -------------------------------------------
if [ ! -f "$WINEPREFIX/drive_c/Py312/python.exe" ]; then
  echo "==> Разворачиваю Windows Python $PY_VERSION"
  wineboot -u >/dev/null 2>&1 || true
  wineserver -w

  mkdir -p "$WORK/msi/out"
  for component in core exe lib tcltk pip dev; do
    curl -sSL -o "$WORK/msi/$component.msi" \
      "https://www.python.org/ftp/python/$PY_VERSION/amd64/$component.msi"
  done

  (cd "$WORK/msi/out" && for m in ../*.msi; do msiextract "$m" >/dev/null; done)

  mkdir -p "$WINEPREFIX/drive_c/Py312"
  cp -r "$WORK/msi/out/." "$WINEPREFIX/drive_c/Py312/"

  wine "$WINPY" -m ensurepip --upgrade >/dev/null 2>&1
fi

# --- 3. зависимости сборки --------------------------------------------------
echo "==> pip install pyinstaller pywebview"
wine "$WINPY" -m pip install -q --no-warn-script-location --disable-pip-version-check \
  pyinstaller pywebview

# --- 4. сборка --------------------------------------------------------------
echo "==> PyInstaller"
cd "$PROJECT_DIR"
wine "$WINPY" -m PyInstaller \
  --noconfirm --clean \
  --noconsole --onefile \
  --name CortenDeskRemote \
  --workpath build-win \
  --distpath dist \
  --specpath build-win \
  main.py

echo
echo "Готово: $PROJECT_DIR/dist/CortenDeskRemote.exe"
file dist/CortenDeskRemote.exe
