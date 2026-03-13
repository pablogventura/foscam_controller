#!/usr/bin/env bash
#
# Publica foscam-controller en PyPI (o en Test PyPI si pasas --test).
#
# Requisitos:
#   pip install build twine
#
# Configuración (PyPI):
#   TWINE_USERNAME=__token__
#   TWINE_PASSWORD=pypi-xxxxxxxx  (token de https://pypi.org/manage/account/token/)
#
# Para Test PyPI (--test):
#   TWINE_USERNAME=__token__
#   TWINE_PASSWORD=pypi-xxxxxxxx  (token de https://test.pypi.org/manage/account/token/)
#
# Uso:
#   ./scripts/publish_to_pypi.sh         # publica en PyPI
#   ./scripts/publish_to_pypi.sh --test  # publica en Test PyPI

set -e
cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"

USE_TEST=false
for arg in "$@"; do
  if [ "$arg" = "--test" ]; then
    USE_TEST=true
    break
  fi
done

echo "=== Foscam Controller - Publicar en PyPI ==="
if [ "$USE_TEST" = true ]; then
  echo "Destino: Test PyPI (https://test.pypi.org/)"
  TWINE_REPOSITORY="${TWINE_REPOSITORY:-testpypi}"
else
  echo "Destino: PyPI (https://pypi.org/)"
  TWINE_REPOSITORY="${TWINE_REPOSITORY:-pypi}"
fi

# Comprobar que existen build y twine
if ! python3 -c "import build" 2>/dev/null; then
  echo "Instalando build..."
  pip install build
fi
if ! python3 -c "import twine" 2>/dev/null; then
  echo "Instalando twine..."
  pip install twine
fi

# Limpiar y construir
echo ""
echo ">>> Limpiando dist/ anterior..."
rm -rf dist/
echo ">>> Construyendo el paquete (wheel + sdist)..."
python3 -m build

# Verificar que los artefactos son válidos
echo ""
echo ">>> Comprobando artefactos con twine check..."
python3 -m twine check dist/*

# Subir
echo ""
if [ "$USE_TEST" = true ]; then
  echo ">>> Subiendo a Test PyPI..."
  python3 -m twine upload --repository testpypi dist/*
else
  echo ">>> Subiendo a PyPI..."
  python3 -m twine upload dist/*
fi

echo ""
echo "Listo. Para instalar desde PyPI:"
if [ "$USE_TEST" = true ]; then
  echo "  pip install --index-url https://test.pypi.org/simple/ foscam-controller"
else
  echo "  pip install foscam-controller"
  echo "  pipx install foscam-controller"
fi
