#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# tests/manifiesto.sh — Pruebas de validación de manifiestos del CD
#
# Verifica el comportamiento del vigilante frente a manifiestos inválidos
# (equipo cruzado, imagen de otro registry, digest mal formado, etc.)
# asegurando que sean rechazados y archivados correctamente según Sección 3.3 y 5.
# ==============================================================================

DIR_SCRIPT="$(cd "$(dirname "$0")" && pwd)"
DIR_REPO="$(cd "$DIR_SCRIPT/.." && pwd)"
VIGILANTE="${DIR_REPO}/bin/vigilante.sh"

# Contador de resultados
TOTAL=0
EXITOS=0
FALLOS=0

# Configuración de entorno de prueba aislado
TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT

export CICD_BASE="$TEST_DIR"
export CICD_BITACORA="$TEST_DIR/logs/cicd.log"
export REGISTRY="100.91.228.65:5000"
export CASA="casa-tomas"
export CICD_NODOS_PYTHON="casa-tomas casa-salvador"
export CICD_NODOS_JAVA="casa-justi1 casa-justi2"

# Servidor de control de mentira: anota cada disparo y contesta {"ok": true}.
# Reemplaza al mock de deploy.sh — el vigilante ya no ejecuta nada, dispara un
# POST por loopback y espera el resultado.
export CICD_PUERTO_DISPARO="${CICD_PUERTO_DISPARO:-18099}"
export CICD_DISPARO="http://127.0.0.1:${CICD_PUERTO_DISPARO}"
REGISTRO_DISPAROS="${CICD_BASE}/disparos.log"

python3 - "$CICD_PUERTO_DISPARO" "$REGISTRO_DISPAROS" <<'PYMOCK' &
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PUERTO, REGISTRO = int(sys.argv[1]), sys.argv[2]


class Mock(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        cuerpo = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        try:  # compactado: el manifiesto viene indentado y una línea por registro
            texto = json.dumps(json.loads(cuerpo), sort_keys=True)
        except ValueError:
            texto = cuerpo.decode("utf-8", "replace").replace("\n", " ")
        with open(REGISTRO, "a", encoding="utf-8") as f:
            f.write(f"{self.path} {texto}\n")
        datos = json.dumps({"ok": True, "detalle": "mock"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)


ThreadingHTTPServer(("127.0.0.1", PUERTO), Mock).serve_forever()
PYMOCK
PID_MOCK=$!
trap 'kill "$PID_MOCK" 2>/dev/null || true' EXIT

for _ in $(seq 1 40); do
    curl -sf -o /dev/null -X POST -d '{}' "${CICD_DISPARO}/arranque" && break
    sleep 0.25
done
rm -f "$REGISTRO_DISPAROS"

# Función auxiliar de aserción
afirmar_resultado() {
    local descripcion="$1"
    local condicion="$2"
    TOTAL=$((TOTAL + 1))

    if eval "$condicion"; then
        echo "  [PASS] $descripcion"
        EXITOS=$((EXITOS + 1))
    else
        echo "  [FAIL] $descripcion"
        FALLOS=$((FALLOS + 1))
    fi
}

reiniciar_entorno() {
    rm -rf "$TEST_DIR/python" "$TEST_DIR/java" "$TEST_DIR/logs" "$REGISTRO_DISPAROS"
    mkdir -p "$TEST_DIR/python/entrante/rechazados" "$TEST_DIR/python/entrante/historial"
    mkdir -p "$TEST_DIR/java/entrante/rechazados" "$TEST_DIR/java/entrante/historial"
    mkdir -p "$TEST_DIR/logs"
}

echo "=============================================================================="
echo "EJECUTANDO BATERÍA DE PRUEBAS DE MANIFIESTO (Sección 5, Punto 1)"
echo "=============================================================================="

# ------------------------------------------------------------------------------
# Test 1: Equipo cruzado (manifiesto con 'java' enviado a pipeline 'python')
# ------------------------------------------------------------------------------
echo -e "\n--- Test 1: Equipo cruzado ---"
reiniciar_entorno

cat <<EOF > "$TEST_DIR/python/entrante/manifiesto.json"
{
  "equipo": "java",
  "imagen": "100.91.228.65:5000/sdypp-app-python:v1-abcdef0",
  "digest": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "version": 1,
  "commit": "abcdef0",
  "publicado_por": "dev",
  "publicado_en": "2026-09-17T10:00:00-03:00"
}
EOF

"$VIGILANTE" procesar python

afirmar_resultado "Manifiesto movido a carpeta rechazados" \
    '[ $(ls -1 "$TEST_DIR/python/entrante/rechazados/"*.json 2>/dev/null | wc -l) -eq 1 ]'

afirmar_resultado "Bitácora registra RECHAZADO por equipo cruzado" \
    'grep -q "deploy | RECHAZADO.*no coincide con pipeline" "$CICD_BITACORA"'

afirmar_resultado "deploy.sh no fue invocado" \
    '[ ! -f "$REGISTRO_DISPAROS" ]'


# ------------------------------------------------------------------------------
# Test 2: Imagen de otro registry
# ------------------------------------------------------------------------------
echo -e "\n--- Test 2: Imagen de otro registry ---"
reiniciar_entorno

cat <<EOF > "$TEST_DIR/python/entrante/manifiesto.json"
{
  "equipo": "python",
  "imagen": "docker.io/usuario/sdypp-app-python:v1-abcdef0",
  "digest": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "version": 1,
  "commit": "abcdef0",
  "publicado_por": "dev",
  "publicado_en": "2026-09-17T10:00:00-03:00"
}
EOF

"$VIGILANTE" procesar python

afirmar_resultado "Manifiesto movido a carpeta rechazados" \
    '[ $(ls -1 "$TEST_DIR/python/entrante/rechazados/"*.json 2>/dev/null | wc -l) -eq 1 ]'

afirmar_resultado "Bitácora registra RECHAZADO por imagen de registry incorrecto" \
    'grep -q "deploy | RECHAZADO.*no comienza con prefijo esperado" "$CICD_BITACORA"'

afirmar_resultado "deploy.sh no fue invocado" \
    '[ ! -f "$REGISTRO_DISPAROS" ]'


# ------------------------------------------------------------------------------
# Test 3: Digest mal formado
# ------------------------------------------------------------------------------
echo -e "\n--- Test 3: Digest mal formado ---"
reiniciar_entorno

cat <<EOF > "$TEST_DIR/python/entrante/manifiesto.json"
{
  "equipo": "python",
  "imagen": "100.91.228.65:5000/sdypp-app-python:v1-abcdef0",
  "digest": "sha256:digest_invalido_demasiado_corto",
  "version": 1,
  "commit": "abcdef0",
  "publicado_por": "dev",
  "publicado_en": "2026-09-17T10:00:00-03:00"
}
EOF

"$VIGILANTE" procesar python

afirmar_resultado "Manifiesto movido a carpeta rechazados" \
    '[ $(ls -1 "$TEST_DIR/python/entrante/rechazados/"*.json 2>/dev/null | wc -l) -eq 1 ]'

afirmar_resultado "Bitácora registra RECHAZADO por digest mal formado" \
    'grep -q "deploy | RECHAZADO.*no cumple con formato" "$CICD_BITACORA"'

afirmar_resultado "deploy.sh no fue invocado" \
    '[ ! -f "$REGISTRO_DISPAROS" ]'


# ------------------------------------------------------------------------------
# Test 4: JSON sintácticamente mal formado
# ------------------------------------------------------------------------------
echo -e "\n--- Test 4: JSON mal formado ---"
reiniciar_entorno

cat <<'EOF' > "$TEST_DIR/python/entrante/manifiesto.json"
{
  "equipo": "python",
  "imagen": "100.91.228.65:5000/sdypp-app-python:v1-abcdef0"
  INVALID_JSON_SIN_COMAS_Y_SIN_CERRAR
EOF

"$VIGILANTE" procesar python

afirmar_resultado "Manifiesto movido a carpeta rechazados" \
    '[ $(ls -1 "$TEST_DIR/python/entrante/rechazados/"*.json 2>/dev/null | wc -l) -eq 1 ]'

afirmar_resultado "Bitácora registra RECHAZADO por JSON mal formado" \
    'grep -q "deploy | RECHAZADO.*JSON mal formado" "$CICD_BITACORA"'

afirmar_resultado "deploy.sh no fue invocado" \
    '[ ! -f "$REGISTRO_DISPAROS" ]'


# ------------------------------------------------------------------------------
# Test 5: Versión no entera
# ------------------------------------------------------------------------------
echo -e "\n--- Test 5: Versión no entera ---"
reiniciar_entorno

cat <<EOF > "$TEST_DIR/python/entrante/manifiesto.json"
{
  "equipo": "python",
  "imagen": "100.91.228.65:5000/sdypp-app-python:v1-abcdef0",
  "digest": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "version": "version-uno",
  "commit": "abcdef0",
  "publicado_por": "dev",
  "publicado_en": "2026-09-17T10:00:00-03:00"
}
EOF

"$VIGILANTE" procesar python

afirmar_resultado "Manifiesto movido a carpeta rechazados" \
    '[ $(ls -1 "$TEST_DIR/python/entrante/rechazados/"*.json 2>/dev/null | wc -l) -eq 1 ]'

afirmar_resultado "Bitácora registra RECHAZADO por versión inválida" \
    'grep -q "deploy | RECHAZADO.*no es un número entero válido" "$CICD_BITACORA"'

afirmar_resultado "deploy.sh no fue invocado" \
    '[ ! -f "$REGISTRO_DISPAROS" ]'


# ------------------------------------------------------------------------------
# Test 6: Manifiesto completamente válido (camino feliz)
# ------------------------------------------------------------------------------
echo -e "\n--- Test 6: Manifiesto válido (éxito) ---"
reiniciar_entorno

cat <<EOF > "$TEST_DIR/python/entrante/manifiesto.json"
{
  "equipo": "python",
  "imagen": "100.91.228.65:5000/sdypp-app-python:v7-a1b2c3d",
  "digest": "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
  "version": 7,
  "commit": "a1b2c3d",
  "publicado_por": "tomas",
  "publicado_en": "2026-09-16T20:00:00-03:00"
}
EOF

"$VIGILANTE" procesar python

afirmar_resultado "Manifiesto procesado y archivado en historial" \
    '[ $(ls -1 "$TEST_DIR/python/entrante/historial/"*-7.json 2>/dev/null | wc -l) -eq 1 ]'

afirmar_resultado "Bitácora registra la recepción del manifiesto" \
    'grep -q "manifiesto | RECIBIDO.*equipo=python version=7" "$CICD_BITACORA"'

afirmar_resultado "Bitácora registra el archivado del manifiesto desplegado" \
    'grep -q "manifiesto | ARCHIVADO.*version=7.*desplegado" "$CICD_BITACORA"'

afirmar_resultado "El disparo llegó al pipeline correcto, con el manifiesto entero" \
    'grep -q "^/deploy/python/desplegar .*version.: 7" "$REGISTRO_DISPAROS"' 


# ------------------------------------------------------------------------------
# Resumen
# ------------------------------------------------------------------------------
echo -e "\n=============================================================================="
echo "RESUMEN DE PRUEBAS: Total: $TOTAL | Exitosos: $EXITOS | Fallidos: $FALLOS"
echo "=============================================================================="

if [ "$FALLOS" -gt 0 ]; then
    echo "ERROR: Fallaron $FALLOS pruebas." >&2
    exit 1
fi

echo "¡Todas las pruebas pasaron correctamente!"
exit 0
