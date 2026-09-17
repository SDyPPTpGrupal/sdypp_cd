#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# arranque.sh — ENTRYPOINT del contenedor CD (sdypp-cicd)
#
# Configura el entorno seguro SSH, genera ~/.ssh/config desde CASAS,
# establece permisos por equipo, arranca sshd (:2222) y el vigilante,
# y supervisa que ante la caída de cualquiera de los dos, el contenedor caiga.
# ==============================================================================

CICD_BASE="${CICD_BASE:-/cicd}"

echo "==> Iniciando sdypp-cicd (CASA: ${CASA:-casa-tomas})"

# ------------------------------------------------------------------------------
# 1. Validación de variables obligatorias (Sección 3.2)
# ------------------------------------------------------------------------------
if [ -z "${CASAS:-}" ]; then
    echo "ERROR: La variable de entorno obligatoria CASAS no está definida" >&2
    exit 1
fi

if [ -z "${CICD_NODOS_PYTHON:-}" ]; then
    echo "ERROR: La variable de entorno obligatoria CICD_NODOS_PYTHON no está definida" >&2
    exit 1
fi

if [ -z "${CICD_NODOS_JAVA:-}" ]; then
    echo "ERROR: La variable de entorno obligatoria CICD_NODOS_JAVA no está definida" >&2
    exit 1
fi

# ------------------------------------------------------------------------------
# 2. Claves de host persistentes para sshd (:2222)
# ------------------------------------------------------------------------------
mkdir -p "${CICD_BASE}/claves/host"
if [ ! -f "${CICD_BASE}/claves/host/ssh_host_ed25519_key" ]; then
    echo "Generando clave de host Ed25519..."
    ssh-keygen -t ed25519 -f "${CICD_BASE}/claves/host/ssh_host_ed25519_key" -N "" -q
fi

if [ ! -f "${CICD_BASE}/claves/host/ssh_host_rsa_key" ]; then
    echo "Generando clave de host RSA..."
    ssh-keygen -t rsa -b 4096 -f "${CICD_BASE}/claves/host/ssh_host_rsa_key" -N "" -q
fi

chmod 600 "${CICD_BASE}/claves/host"/*_key
chmod 644 "${CICD_BASE}/claves/host"/*_key.pub

# ------------------------------------------------------------------------------
# 3. Configuración de usuarios y claves públicas autorizadas (Sección 3.1)
# ------------------------------------------------------------------------------
# deploy-python
mkdir -p /home/deploy-python/.ssh
if [ -f "${CICD_BASE}/claves/deploy-python.pub" ]; then
    cp "${CICD_BASE}/claves/deploy-python.pub" /home/deploy-python/.ssh/authorized_keys
    chmod 600 /home/deploy-python/.ssh/authorized_keys
fi
chmod 700 /home/deploy-python/.ssh
chown -R deploy-python:deploy-python /home/deploy-python

# deploy-java
mkdir -p /home/deploy-java/.ssh
if [ -f "${CICD_BASE}/claves/deploy-java.pub" ]; then
    cp "${CICD_BASE}/claves/deploy-java.pub" /home/deploy-java/.ssh/authorized_keys
    chmod 600 /home/deploy-java/.ssh/authorized_keys
fi
chmod 700 /home/deploy-java/.ssh
chown -R deploy-java:deploy-java /home/deploy-java

# Permisos de carpetas entrantes (sólo su propio equipo puede escribir)
mkdir -p "${CICD_BASE}/python/entrante" "${CICD_BASE}/python/estado"
mkdir -p "${CICD_BASE}/java/entrante" "${CICD_BASE}/java/estado"
mkdir -p "${CICD_BASE}/logs"

chown -R deploy-python:deploy-python "${CICD_BASE}/python/entrante"
chmod 775 "${CICD_BASE}/python/entrante"

chown -R deploy-java:deploy-java "${CICD_BASE}/java/entrante"
chmod 775 "${CICD_BASE}/java/entrante"

# ------------------------------------------------------------------------------
# 4. Generación de ~/.ssh/config para salida hacia las casas
# ------------------------------------------------------------------------------
mkdir -p /root/.ssh
chmod 700 /root/.ssh

if [ -f "${CICD_BASE}/claves/id_deploy" ]; then
    chmod 600 "${CICD_BASE}/claves/id_deploy"
fi

cat <<EOF > /root/.ssh/config
Host *
    IdentityFile ${CICD_BASE}/claves/id_deploy
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new
    BatchMode yes
    ConnectTimeout 10
EOF

for par in $CASAS; do
    nombre="${par%%=*}"
    usuario_ip="${par#*=}"
    usuario="${usuario_ip%@*}"
    ip="${usuario_ip#*@}"

    cat <<EOF >> /root/.ssh/config

Host $nombre
    HostName $ip
    User $usuario
EOF
done
chmod 600 /root/.ssh/config

# ------------------------------------------------------------------------------
# 5. Configuración de sshd del contenedor
# ------------------------------------------------------------------------------
mkdir -p /etc/ssh/sshd_config.d /run/sshd
cat <<EOF > /etc/ssh/sshd_config.d/cicd.conf
Port 2222
PermitRootLogin no
PasswordAuthentication no
PubkeyAuthentication yes
HostKey ${CICD_BASE}/claves/host/ssh_host_ed25519_key
HostKey ${CICD_BASE}/claves/host/ssh_host_rsa_key
AuthorizedKeysFile .ssh/authorized_keys
EOF

# ------------------------------------------------------------------------------
# 6. Lanzamiento supervisado: sshd y vigilante.sh
# ------------------------------------------------------------------------------
echo "Iniciando sshd en :2222..."
/usr/sbin/sshd -D -e &
PID_SSHD=$!

echo "Iniciando vigilante para ambos pipelines..."
"${CICD_BASE}/bin/vigilante.sh" ambos &
PID_VIGILANTE=$!

# Si se recibe señal de apagado, terminar procesos hijos sin kill -9
trap 'echo "Recibida señal de apagado. Terminando procesos..."; kill -TERM "$PID_SSHD" "$PID_VIGILANTE" 2>/dev/null || true; wait "$PID_SSHD" "$PID_VIGILANTE" 2>/dev/null || true; exit 0' SIGTERM SIGINT

# Regla: si sshd o el vigilante mueren, el contenedor se cae
wait -n "$PID_SSHD" "$PID_VIGILANTE"
CODIGO_SALIDA=$?

echo "Uno de los procesos críticos terminó con código $CODIGO_SALIDA. Apagando contenedor..." >&2
kill -TERM "$PID_SSHD" "$PID_VIGILANTE" 2>/dev/null || true
exit "$CODIGO_SALIDA"
