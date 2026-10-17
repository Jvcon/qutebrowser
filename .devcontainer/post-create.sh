#!/usr/bin/env bash
# post-create setup for the qutebrowser devcontainer.
#
# Runs as the container user ("vscode"). Installs system packages via sudo
# (available in the official base image), bootstraps the tox venv, and
# installs opencode + its plugins. Failure of the opencode tooling steps is
# non-fatal so container creation still succeeds.

set -euo pipefail

echo "==> Installing system packages (PyQt6/QtWebEngine, docs tooling)"
sudo apt-get update
# python3-pyqt6 / python3-pyqt6.qtwebengine: system-wide PyQt, required because
#   tox links the system PyQt into its venvs via scripts/link_pyqt.py.
#   (Ubuntu 24.04 uses the dotted name; the hyphenated one doesn't exist yet.)
# python3-pip / python3-venv: Ubuntu 24.04 ships neither pip nor ensurepip
#   in the base python3 (PEP 668); both are needed for the tox bootstrap.
# libegl1: needed by pylint/vulture/docs tox envs importing PyQt (see CI).
# asciidoc: tox docs env.
# dbus-run-session: how CI wraps tox runs.
sudo apt-get install -y --no-install-recommends \
    python3-pyqt6 \
    python3-pyqt6.qtwebengine \
    python3-pip \
    python3-venv \
    libegl1 \
    asciidoc \
    dbus \
    dbus-x11

# Sanity check, as the CI image does.
python3 -c "from PyQt6 import QtWebEngineCore, QtWebEngineWidgets"

echo "==> Bootstrapping tox"
python3 -m pip install --user --break-system-packages -U pip
python3 -m pip install --user --break-system-packages \
    -r misc/requirements/requirements-tox.txt

echo "==> Creating .venv with pip-installed PyQt6"
# Use pip PyQt6 only (matching tox's pyqt* envs): never mix it with
# link_pyqt.py, which would symlink the root-owned system PyQt6 into the
# venv and make the subsequent pip install of PyQt6-Qt6 fail with EACCES.
# The system PyQt6 installed via apt above is still needed for tox envs
# without the pyqt factor (they run link_pyqt.py --tox).
python3 -m venv .venv
# Drop a stale link_pyqt.py symlink from a previous run (the .venv survives
# container rebuilds); pip would otherwise write through it into the
# root-owned system dist-packages and fail with EACCES.
find .venv -maxdepth 3 -type l \( -name 'PyQt6' -o -name 'sip' -o -name 'sipconfig.py' \) -delete
./.venv/bin/pip install -U pip
./.venv/bin/pip install \
    -r requirements.txt \
    -r misc/requirements/requirements-pyqt.txt
./.venv/bin/python -c "from PyQt6 import QtWebEngineCore, QtWebEngineWidgets"

echo "==> Upgrading npm to latest"
# Node ships a bundled npm (10.x on Node 22); bump to the current major.
# The nvm install dir is group-writable by the nvm group (vscode is a member),
# so this needs no sudo.
npm install -g npm@latest

echo "==> Installing opencode tooling (non-fatal)"
set +e
curl -fsSL https://opencode.ai/install | bash
npx -y oh-my-opencode-slim@latest install
npx -y skills@latest add mattpocock/skills
set -e

echo "==> post-create done"
