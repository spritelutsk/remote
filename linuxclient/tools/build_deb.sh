#!/bin/bash
# Сборка .deb.
#
# Пакет собирается на любой машине с dpkg-deb: внутри только Python и данные,
# ничего компилируемого, поэтому кросс-сборка под arm64 с x86_64 — это просто
# другое значение поля Architecture.
#
#     ./tools/build_deb.sh              # arm64 (по умолчанию)
#     ARCH=amd64 ./tools/build_deb.sh
#     ARCH=all   ./tools/build_deb.sh   # честнее всего: содержимое от арки не зависит

set -euo pipefail

PKG="cortendesk-remote"
VERSION="${VERSION:-1.0.0}"
ARCH="${ARCH:-arm64}"
MAINTAINER="${MAINTAINER:-CortenDesk Remote <admin@spritelutsk.duckdns.org>}"

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$(mktemp -d)"
ROOT="$BUILD/root"
OUT="$PROJECT_DIR/dist"

trap 'rm -rf "$BUILD"' EXIT

mkdir -p "$ROOT/DEBIAN" \
         "$ROOT/usr/bin" \
         "$ROOT/usr/lib/$PKG" \
         "$ROOT/usr/share/applications" \
         "$ROOT/usr/share/icons/hicolor/scalable/apps" \
         "$ROOT/usr/share/doc/$PKG"

# --- код -------------------------------------------------------------------
cp -r "$PROJECT_DIR/cortendesk_remote" "$ROOT/usr/lib/$PKG/"
find "$ROOT/usr/lib/$PKG" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

# --- запускалка ------------------------------------------------------------
cat > "$ROOT/usr/bin/$PKG" <<'LAUNCHER'
#!/bin/sh
# Пакет не ставится в site-packages: приложение не библиотека, и засорять
# общее пространство имён Python ему незачем.
PYTHONPATH="/usr/lib/cortendesk-remote${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
exec python3 -m cortendesk_remote "$@"
LAUNCHER
chmod 0755 "$ROOT/usr/bin/$PKG"

# --- данные ----------------------------------------------------------------
cp "$PROJECT_DIR/packaging/$PKG.desktop" "$ROOT/usr/share/applications/"
cp "$PROJECT_DIR/packaging/$PKG.svg" "$ROOT/usr/share/icons/hicolor/scalable/apps/"
cp "$PROJECT_DIR/packaging/copyright" "$ROOT/usr/share/doc/$PKG/"

# changelog.Debian.gz требует Debian Policy; без него lintian ругается.
printf '%s (%s) unstable; urgency=medium\n\n  * Первый выпуск.\n\n -- %s  %s\n' \
  "$PKG" "$VERSION" "$MAINTAINER" "$(date -R)" \
  | gzip -9n > "$ROOT/usr/share/doc/$PKG/changelog.Debian.gz"

# --- control ---------------------------------------------------------------
INSTALLED_KB="$(du -ks "$ROOT/usr" | cut -f1)"

cat > "$ROOT/DEBIAN/control" <<CONTROL
Package: $PKG
Version: $VERSION
Section: net
Priority: optional
Architecture: $ARCH
Depends: python3 (>= 3.11), python3-gi, gir1.2-gtk-3.0, gir1.2-webkit2-4.1
Recommends: python3-secretstorage
Maintainer: $MAINTAINER
Installed-Size: $INSTALLED_KB
Description: Remote desktop client for a CortenDesk console
 A GTK client for a self-hosted CortenDesk (RustDesk) server. It lists the
 devices your account can reach, with presence, search and folder filters,
 and opens a control session in an embedded WebKit2GTK view.
 .
 The application does not implement the RustDesk protocol itself: screen
 capture, NAT traversal and codecs stay in RustDesk, while this client drives
 the console API and embeds the CortenDesk web client.
CONTROL

# md5sums — по нему dpkg --verify и debsums проверяют целостность файлов.
( cd "$ROOT" && find usr -type f -print0 | sort -z \
    | xargs -0 md5sum > DEBIAN/md5sums )

# --- права -----------------------------------------------------------------
find "$ROOT/usr" -type d -exec chmod 0755 {} +
find "$ROOT/usr" -type f -exec chmod 0644 {} +
chmod 0755 "$ROOT/usr/bin/$PKG"
chmod 0755 "$ROOT/DEBIAN"
# Корень дерева тоже: иначе в архив уезжает umask сборочной машины (0775).
chmod 0755 "$ROOT"
chmod 0644 "$ROOT/DEBIAN/control" "$ROOT/DEBIAN/md5sums"

# --- сборка ----------------------------------------------------------------
mkdir -p "$OUT"
DEB="$OUT/${PKG}_${VERSION}_${ARCH}.deb"
dpkg-deb --root-owner-group --build "$ROOT" "$DEB" >/dev/null

echo "Готово: $DEB"
dpkg-deb --info "$DEB" | sed -n '1,25p'
