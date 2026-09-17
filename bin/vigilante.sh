#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# vigilante.sh — Monitoreo de manifiestos y disparo de despliegues (CD)
#
# Escucha eventos moved_to sobre manifiesto.json en /cicd/<equipo>/entrante/,
# valida el manifiesto, asegura exclusión mutua por equipo mediante flock,
# y ejecuta deploy.sh desplegar.
# ==============================================================================

# Directorio base del CD (/cicd en el contenedor, o relativo para desarrollo/test)
if [ -z "${CICD_BASE:-}" ]; then
    if [ -d "/cicd" ]; then
        CICD_BASE="/cicd"
    else
        CICD_BASE="$(cd "$(dirname "$0")/.." && pwd)"
    fi
fi

# Variables de configuración con valores por defecto según especificación
CASA="${CASA:-casa-tomas}"
REGISTRY="${REGISTRY:-100.78.246.64:5000}"
CICD_BITACORA="${CICD_BITACORA:-${CICD_BASE}/logs/cicd.log}"
CICD_NODOS_PYTHON="${CICD_NODOS_PYTHON:-}"
CICD_NODOS_JAVA="${CICD_NODOS_JAVA:-}"

# Ubicación de deploy.sh
if [ -n "${CICD_DEPLOY:-}" ]; then
    DEPLOY_BIN="$CICD_DEPLOY"
elif [ -f "${CICD_BASE}/bin/deploy.sh" ]; then
    DEPLOY_BIN="${CICD_BASE}/bin/deploy.sh"
elif [ -f "${CICD_BASE}/deploy.sh" ]; then
    DEPLOY_BIN="${CICD_BASE}/deploy.sh"
else
    DEPLOY_BIN="deploy.sh"
fi

# ------------------------------------------------------------------------------
# registrar_bitacora: escribe en cicd.log con el formato estándar del TP
# Formato: timestamp ISO | quien@casa | operación | código | detalle
# ------------------------------------------------------------------------------
registrar_bitacora() {
    local operacion="$1"
    local codigo="$2"
    local detalle="$3"

    local timestamp
    timestamp=$(date --iso-8601=seconds 2>/dev/null || date +"%Y-%m-%dT%H:%M:%S%z")
    local entrada="${timestamp} | cicd@${CASA} | ${operacion} | ${codigo} | ${detalle}"

    mkdir -p "$(dirname "$CICD_BITACORA")"
    echo "$entrada" >> "$CICD_BITACORA"
    echo "$entrada"
}

# ------------------------------------------------------------------------------
# procesar_manifiesto: valida y ejecuta el ciclo de despliegue bajo cerrojo
# ------------------------------------------------------------------------------
procesar_manifiesto() {
    local equipo="$1"
    local dir_entrante="${CICD_BASE}/${equipo}/entrante"
    local archivo_manifiesto="${dir_entrante}/manifiesto.json"
    local archivo_procesando="${dir_entrante}/procesando.json"
    local cerrojo="/tmp/cicd-${equipo}.lock"

    # Exclusión mutua por equipo
    (
        flock -x 200

        # Si el archivo no existe (por ejemplo, ya fue procesado), salir
        if [ ! -f "$archivo_manifiesto" ]; then
            exit 0
        fi

        local timestamp_archivo
        timestamp_archivo=$(date +"%Y%m%d_%H%M%S")

        # Obtener nodos asignados al equipo
        local nodos=""
        if [ "$equipo" = "python" ]; then
            nodos="${CICD_NODOS_PYTHON}"
        elif [ "$equipo" = "java" ]; then
            nodos="${CICD_NODOS_JAVA}"
        fi

        # ----------------------------------------------------------------------
        # 1. Validación del manifiesto
        # ----------------------------------------------------------------------
        local motivo_rechazo=""
        local m_equipo=""
        local m_imagen=""
        local m_digest=""
        local m_version=""

        if ! jq empty "$archivo_manifiesto" 2>/dev/null; then
            motivo_rechazo="JSON mal formado"
        else
            m_equipo=$(jq -r '.equipo // empty' "$archivo_manifiesto" 2>/dev/null)
            m_imagen=$(jq -r '.imagen // empty' "$archivo_manifiesto" 2>/dev/null)
            m_digest=$(jq -r '.digest // empty' "$archivo_manifiesto" 2>/dev/null)
            m_version=$(jq -r '.version // empty' "$archivo_manifiesto" 2>/dev/null)

            if [ "$m_equipo" != "$equipo" ]; then
                motivo_rechazo="Equipo en manifiesto ('${m_equipo}') no coincide con pipeline ('${equipo}')"
            elif [[ "$m_imagen" != "${REGISTRY}/sdypp-app-${equipo}:"* ]]; then
                motivo_rechazo="Imagen ('${m_imagen}') no comienza con prefijo esperado '${REGISTRY}/sdypp-app-${equipo}:'"
            elif ! echo "$m_digest" | grep -Eq '^sha256:[0-9a-f]{64}$'; then
                motivo_rechazo="Digest ('${m_digest}') no cumple con formato ^sha256:[0-9a-f]{64}$"
            elif ! echo "$m_version" | grep -Eq '^[0-9]+$'; then
                motivo_rechazo="Versión ('${m_version}') no es un número entero válido"
            elif [ -z "$nodos" ]; then
                motivo_rechazo="No hay nodos configurados para el equipo ${equipo} (CICD_NODOS_${equipo^^} vacía)"
            fi
        fi

        # Si falló la validación: registrar, mover a rechazados y finalizar
        if [ -n "$motivo_rechazo" ]; then
            mkdir -p "${dir_entrante}/rechazados"
            local destino_rechazado="${dir_entrante}/rechazados/${timestamp_archivo}.json"
            mv "$archivo_manifiesto" "$destino_rechazado"

            local detalle_log="equipo=${equipo} version=${m_version:-desconocida} casas=${nodos:-ninguna} | ${motivo_rechazo}"
            registrar_bitacora "deploy" "RECHAZADO" "$detalle_log"
            exit 0
        fi

        # ----------------------------------------------------------------------
        # 2. Mover a procesando.json para evitar que otro scp lo sobreescriba
        # ----------------------------------------------------------------------
        mv "$archivo_manifiesto" "$archivo_procesando"

        # ----------------------------------------------------------------------
        # 3. Ejecución de deploy.sh desplegar
        # ----------------------------------------------------------------------
        registrar_bitacora "deploy" "INICIO" "equipo=${equipo} version=${m_version} casas=${nodos} | Iniciando despliegue de ${m_imagen}"

        local codigo_despliegue=0
        # shellcheck disable=SC2086
        "$DEPLOY_BIN" desplegar --equipo "$equipo" --manifiesto "$archivo_procesando" $nodos || codigo_despliegue=$?

        # ----------------------------------------------------------------------
        # 4. Resultado en bitácora y archivado en historial
        # ----------------------------------------------------------------------
        if [ "$codigo_despliegue" -eq 0 ]; then
            registrar_bitacora "deploy" "OK" "equipo=${equipo} version=${m_version} casas=${nodos} | version=${m_version} casas=${nodos} la anterior sigue viva"
        else
            registrar_bitacora "deploy" "FALLO" "equipo=${equipo} version=${m_version} casas=${nodos} | FALLO — las versiones viejas siguen sirviendo"
        fi

        mkdir -p "${dir_entrante}/historial"
        local destino_historial="${dir_entrante}/historial/${timestamp_archivo}-${m_version}.json"
        if [ -f "$archivo_procesando" ]; then
            mv "$archivo_procesando" "$destino_historial"
        fi

    ) 200>"$cerrojo"
}

# ------------------------------------------------------------------------------
# vigilar_equipo: bucle de espera inotifywait para un equipo específico
# ------------------------------------------------------------------------------
vigilar_equipo() {
    local equipo="$1"
    local dir_entrante="${CICD_BASE}/${equipo}/entrante"

    mkdir -p "${dir_entrante}/rechazados" "${dir_entrante}/historial"

    # Procesar si ya existía un manifiesto previo al inicio
    if [ -f "${dir_entrante}/manifiesto.json" ]; then
        procesar_manifiesto "$equipo"
    fi

    # inotifywait monitorea moved_to (scp atómico con rename)
    inotifywait -m -e moved_to --format "%f" "$dir_entrante" | while read -r archivo; do
        if [ "$archivo" = "manifiesto.json" ]; then
            procesar_manifiesto "$equipo"
        fi
    done
}

# ------------------------------------------------------------------------------
# Punto de entrada: permite monitorear un solo equipo o ambos
# ------------------------------------------------------------------------------
modo="${1:-ambos}"

case "$modo" in
    python)
        vigilar_equipo "python"
        ;;
    java)
        vigilar_equipo "java"
        ;;
    procesar)
        procesar_manifiesto "${2:-python}"
        ;;
    ambos|todos)
        # Lanza ambos vigilantes como procesos hijos separados para supervisión
        "$0" python &
        pid_python=$!

        "$0" java &
        pid_java=$!

        # Manejo de señales para apagado ordenado
        trap 'kill -TERM "$pid_python" "$pid_java" 2>/dev/null || true; exit 0' SIGTERM SIGINT

        # Si cualquiera de los dos vigilantes muere, el proceso principal cae
        # asegurando la regla: "si sshd o el vigilante mueren, el contenedor se cae"
        wait -n "$pid_python" "$pid_java"
        codigo_retorno=$?

        kill -TERM "$pid_python" "$pid_java" 2>/dev/null || true
        exit "$codigo_retorno"
        ;;
    *)
        echo "Uso: $0 [python|java|ambos]" >&2
        exit 1
        ;;
esac
