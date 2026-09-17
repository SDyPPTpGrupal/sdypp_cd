#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# deploy.sh — Despliegue centralizado, conmutación, rollback y estado (CD)
#
# Interfaz:
#   deploy.sh desplegar --equipo <equipo> --manifiesto <ruta> <casa1> [casa2 ...]
#   deploy.sh conmutar  --equipo <equipo> [--manifiesto <ruta>] <casa1> [casa2 ...]
#   deploy.sh rollback  --equipo <equipo> <casa1> [casa2 ...]
#   deploy.sh estado    --equipo <equipo> <casa1> [casa2 ...]
# ==============================================================================

# Directorio base del CD (/cicd en el contenedor, o relativo para desarrollo/test)
if [ -z "${CICD_BASE:-}" ]; then
    if [ -d "/cicd" ]; then
        CICD_BASE="/cicd"
    else
        CICD_BASE="$(cd "$(dirname "$0")" && pwd)"
    fi
fi

# Variables de entorno con valores por defecto (Sección 3.2)
CASA="${CASA:-casa-tomas}"
CASAS="${CASAS:-}"
PUERTOS_CASA="${PUERTOS_CASA:-}"
BALANCEADORES="${BALANCEADORES:-http://127.0.0.1:8081}"
REGISTRY="${REGISTRY:-100.78.246.64:5000}"
INTENTOS_SALUD="${INTENTOS_SALUD:-30}"
ESPERA_SALUD="${ESPERA_SALUD:-2}"
DIR_REMOTO="${DIR_REMOTO:-\$HOME/sdypp}"
CICD_BITACORA="${CICD_BITACORA:-${CICD_BASE}/logs/cicd.log}"

# Contrato proto para grpcurl (Sección 3.1 y 3.5)
if [ -f "/cicd/bin/contrato.proto" ]; then
    PROTO_PATH="/cicd/bin/contrato.proto"
elif [ -f "${CICD_BASE}/bin/contrato.proto" ]; then
    PROTO_PATH="${CICD_BASE}/bin/contrato.proto"
elif [ -f "${CICD_BASE}/contrato.proto" ]; then
    PROTO_PATH="${CICD_BASE}/contrato.proto"
else
    PROTO_PATH="contrato.proto"
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
# SSH con ConnectTimeout=10 y BatchMode=yes
# ------------------------------------------------------------------------------
ejecutar_ssh() {
    local host_casa="$1"
    local comando="$2"
    ssh -o BatchMode=yes -o ConnectTimeout=10 "$host_casa" "$comando"
}

# ------------------------------------------------------------------------------
# Helpers de resolución de red y colores
# ------------------------------------------------------------------------------
obtener_ip_casa() {
    local nombre_casa="$1"
    for par in $CASAS; do
        if [[ "$par" == "${nombre_casa}="* ]]; then
            local usuario_ip="${par#*=}"
            local ip="${usuario_ip#*@}"
            echo "$ip"
            return 0
        fi
    done

    # Si coincide directamente con una dirección IP
    if [[ "$nombre_casa" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        echo "$nombre_casa"
        return 0
    fi

    echo "ERROR: Casa '${nombre_casa}' no encontrada en la variable CASAS" >&2
    return 1
}

obtener_puertos_casa() {
    local nombre_casa="$1"
    local p_blue="8080"
    local p_green="8081"

    for par in $PUERTOS_CASA; do
        if [[ "$par" == "${nombre_casa}="* ]]; then
            local puertos="${par#*=}"
            p_blue="${puertos%%:*}"
            p_green="${puertos##*:}"
            break
        fi
    done
    echo "$p_blue $p_green"
}

obtener_puerto_color() {
    local nombre_casa="$1"
    local color="$2"
    local p_blue
    local p_green
    read -r p_blue p_green < <(obtener_puertos_casa "$nombre_casa")

    if [ "$color" = "blue" ]; then
        echo "$p_blue"
    elif [ "$color" = "green" ]; then
        echo "$p_green"
    else
        echo "ERROR: Color desconocido '$color'" >&2
        return 1
    fi
}

color_opuesto() {
    local color="$1"
    if [ "$color" = "blue" ]; then
        echo "green"
    elif [ "$color" = "green" ]; then
        echo "blue"
    else
        echo "ERROR: Color desconocido '$color'" >&2
        return 1
    fi
}

# ------------------------------------------------------------------------------
# SUBCOMANDO: desplegar (Sección 3.5)
# ------------------------------------------------------------------------------
comando_desplegar() {
    local equipo="$1"
    local ruta_manifiesto="$2"
    shift 2
    local casas=("$@")

    if [ ${#casas[@]} -eq 0 ]; then
        echo "ERROR: Debe especificar al menos una casa para desplegar" >&2
        exit 1
    fi

    local casas_str="${casas[*]}"

    # 1. CARGAR
    if [ ! -f "$ruta_manifiesto" ]; then
        echo "ERROR: No existe el archivo de manifiesto en '$ruta_manifiesto'" >&2
        exit 1
    fi

    local m_equipo
    local m_imagen
    local m_digest
    local m_version
    m_equipo=$(jq -r '.equipo // empty' "$ruta_manifiesto")
    m_imagen=$(jq -r '.imagen // empty' "$ruta_manifiesto")
    m_digest=$(jq -r '.digest // empty' "$ruta_manifiesto")
    m_version=$(jq -r '.version // empty' "$ruta_manifiesto")

    if [ "$m_equipo" != "$equipo" ]; then
        echo "ERROR: El equipo del manifiesto ('$m_equipo') no coincide con '$equipo'" >&2
        exit 1
    fi

    local imagen_sin_tag="${m_imagen%:*}"
    local dir_estado="${CICD_BASE}/${equipo}/estado"
    mkdir -p "$dir_estado"

    declare -A activo_casa
    declare -A nuevo_casa
    declare -A ip_casa
    declare -A puerto_activo_casa
    declare -A puerto_nuevo_casa

    for casa in "${casas[@]}"; do
        local archivo_estado="${dir_estado}/${casa}.env"
        local color_actual="blue"
        if [ -f "$archivo_estado" ]; then
            # Carga de variables de estado
            local estado_activo
            estado_activo=$(grep '^COLOR_ACTIVO=' "$archivo_estado" | cut -d'=' -f2- || true)
            if [ -n "$estado_activo" ]; then
                color_actual="$estado_activo"
            fi
        fi

        local color_nuevo
        color_nuevo=$(color_opuesto "$color_actual")

        local ip
        ip=$(obtener_ip_casa "$casa")

        local p_activo
        p_activo=$(obtener_puerto_color "$casa" "$color_actual")
        local p_nuevo
        p_nuevo=$(obtener_puerto_color "$casa" "$color_nuevo")

        activo_casa["$casa"]="$color_actual"
        nuevo_casa["$casa"]="$color_nuevo"
        ip_casa["$casa"]="$ip"
        puerto_activo_casa["$casa"]="$p_activo"
        puerto_nuevo_casa["$casa"]="$p_nuevo"
    done

    # --------------------------------------------------------------------------
    # 2. PULL (paralelo)
    # --------------------------------------------------------------------------
    echo "==> Paso 2: PULL en paralelo en las casas (${casas_str})"
    local pids=()
    for casa in "${casas[@]}"; do
        (
            echo "[$casa] Iniciando docker pull de ${imagen_sin_tag}@${m_digest}"
            ejecutar_ssh "$casa" "docker pull '${imagen_sin_tag}@${m_digest}' && docker tag '${imagen_sin_tag}@${m_digest}' '${m_imagen}'"
        ) &
        pids+=($!)
    done

    local fallos_pull=0
    for i in "${!casas[@]}"; do
        local casa="${casas[$i]}"
        local pid="${pids[$i]}"
        if ! wait "$pid"; then
            echo "ERROR: Falló el pull en la casa $casa" >&2
            fallos_pull=$((fallos_pull + 1))
        fi
    done

    if [ "$fallos_pull" -gt 0 ]; then
        echo "ERROR: Abortando despliegue por fallos en docker pull" >&2
        exit 1
    fi

    # --------------------------------------------------------------------------
    # 3. ARRIBA (paralelo)
    # --------------------------------------------------------------------------
    echo "==> Paso 3: Levantando réplicas nuevas en paralelo"
    pids=()
    for casa in "${casas[@]}"; do
        local nuevo="${nuevo_casa[$casa]}"
        local p_nuevo="${puerto_nuevo_casa[$casa]}"
        local nombre_contenedor="sdypp-${nuevo}-app-1"

        (
            echo "[$casa] Levantando $nombre_contenedor en puerto $p_nuevo"
            local cmd="docker rm -f $nombre_contenedor >/dev/null 2>&1 || true; docker run -d --name $nombre_contenedor --restart unless-stopped -p ${p_nuevo}:8080 --env-file ${DIR_REMOTO}/.env -e HOST_NAME=${casa}-${nuevo} -e CASA=${casa} -v ${DIR_REMOTO}/logs/${nuevo}:/app/logs --stop-timeout 15 ${m_imagen}"
            ejecutar_ssh "$casa" "$cmd"
        ) &
        pids+=($!)
    done

    local fallos_arriba=0
    for i in "${!casas[@]}"; do
        local casa="${casas[$i]}"
        local pid="${pids[$i]}"
        if ! wait "$pid"; then
            echo "ERROR: Falló el inicio del contenedor en la casa $casa" >&2
            fallos_arriba=$((fallos_arriba + 1))
        fi
    done

    if [ "$fallos_arriba" -gt 0 ]; then
        echo "ERROR: Abortando despliegue por fallos al arrancar réplicas. Ejecutando limpieza (4b)..." >&2
        for casa in "${casas[@]}"; do
            local nuevo="${nuevo_casa[$casa]}"
            ejecutar_ssh "$casa" "docker rm -f sdypp-${nuevo}-app-1" >/dev/null 2>&1 || true
        done
        registrar_bitacora "deploy" "FALLO" "equipo=${equipo} version=${m_version} casas=${casas_str} | FALLO — nadie vio la versión rota"
        exit 1
    fi

    # --------------------------------------------------------------------------
    # 4. VERIFY (paralelo)
    # --------------------------------------------------------------------------
    echo "==> Paso 4: Verificando salud (docker inspect) e identidad (grpcurl) en paralelo"
    pids=()
    for casa in "${casas[@]}"; do
        local nuevo="${nuevo_casa[$casa]}"
        local ip="${ip_casa[$casa]}"
        local p_nuevo="${puerto_nuevo_casa[$casa]}"
        local nombre_contenedor="sdypp-${nuevo}-app-1"

        (
            local intentos=1
            local verificado=false

            while [ "$intentos" -le "$INTENTOS_SALUD" ]; do
                # Salud vía docker inspect por SSH
                local estado_salud
                estado_salud=$(ejecutar_ssh "$casa" "docker inspect -f '{{.State.Health.Status}}' $nombre_contenedor" 2>/dev/null || echo "desconocido")

                if [ "$estado_salud" = "healthy" ]; then
                    # Verificación de identidad y versión vía grpcurl desde el CD
                    local version_remota
                    version_remota=$(grpcurl -max-time 5 -plaintext -proto "$PROTO_PATH" "${ip}:${p_nuevo}" sdypp.Servicio/Identidad 2>/dev/null | jq -r '.version // empty' 2>/dev/null || echo "")

                    if [ "$version_remota" = "$m_version" ]; then
                        echo "[$casa] Verificación exitosa: healthy y versión $version_remota confirmada"
                        verificado=true
                        break
                    else
                        echo "[$casa] Intento $intentos: healthy pero versión gRPC ('$version_remota') != esperada ('$m_version')"
                    fi
                else
                    echo "[$casa] Intento $intentos: estado de salud='$estado_salud'"
                fi

                sleep "$ESPERA_SALUD"
                intentos=$((intentos + 1))
            done

            if [ "$verificado" = true ]; then
                exit 0
            else
                echo "ERROR: [$casa] No superó la verificación en $INTENTOS_SALUD intentos" >&2
                exit 1
            fi
        ) &
        pids+=($!)
    done

    local fallos_verify=0
    for i in "${!casas[@]}"; do
        local casa="${casas[$i]}"
        local pid="${pids[$i]}"
        if ! wait "$pid"; then
            fallos_verify=$((fallos_verify + 1))
        fi
    done

    # --------------------------------------------------------------------------
    # 4b. ABORTA si falló la verificación en alguna casa
    # --------------------------------------------------------------------------
    if [ "$fallos_verify" -gt 0 ]; then
        echo "ERROR: Verificación fallida en una o más casas. Abortando todo según 4b..." >&2
        for casa in "${casas[@]}"; do
            local nuevo="${nuevo_casa[$casa]}"
            ejecutar_ssh "$casa" "docker rm -f sdypp-${nuevo}-app-1" >/dev/null 2>&1 || true
        done
        registrar_bitacora "deploy" "FALLO" "equipo=${equipo} version=${m_version} casas=${casas_str} | FALLO — nadie vio la versión rota"
        exit 1
    fi

    # --------------------------------------------------------------------------
    # 5. CONMUTAR (un solo POST por balanceador con todas las casas)
    # --------------------------------------------------------------------------
    echo "==> Paso 5: Conmutando balanceadores"
    local json_agregar="[]"
    local json_quitar="[]"

    for casa in "${casas[@]}"; do
        local ip="${ip_casa[$casa]}"
        local destino_nuevo="${ip}:${puerto_nuevo_casa[$casa]}"
        local destino_activo="${ip}:${puerto_activo_casa[$casa]}"

        json_agregar=$(echo "$json_agregar" | jq --arg d "$destino_nuevo" '. + [$d]')
        json_quitar=$(echo "$json_quitar" | jq --arg d "$destino_activo" '. + [$d]')
    done

    local payload
    payload=$(jq -n --argjson a "$json_agregar" --argjson q "$json_quitar" '{"agregar": $a, "quitar": $q}')

    local fallos_bal=0
    for bal in $BALANCEADORES; do
        echo "Conmutando balanceador en $bal..."
        local resp_code
        resp_code=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
            -H "Content-Type: application/json" \
            -d "$payload" \
            "${bal}/admin/backends" || echo "000")

        if [ "$resp_code" != "200" ]; then
            echo "ERROR: El balanceador en $bal respondió código HTTP $resp_code" >&2
            fallos_bal=$((fallos_bal + 1))
        fi
    done

    if [ "$fallos_bal" -gt 0 ]; then
        registrar_bitacora "deploy" "FALLO" "equipo=${equipo} version=${m_version} casas=${casas_str} | FALLO — falló la conmutación en balanceador. Las réplicas nuevas quedan arriba y sanas; reintentar conmutar a mano"
        echo "ADVERTENCIA: Las réplicas nuevas están arriba y sanas, pero falló la conmutación en uno o más balanceadores." >&2
        echo "Debe reintentar la conmutación manualmente con: deploy.sh conmutar --equipo $equipo ${casas_str}" >&2
        exit 1
    fi

    # --------------------------------------------------------------------------
    # 6. GUARDAR estado por casa
    # --------------------------------------------------------------------------
    echo "==> Paso 6: Guardando estado por casa"
    for casa in "${casas[@]}"; do
        local archivo_estado="${dir_estado}/${casa}.env"
        local nuevo="${nuevo_casa[$casa]}"
        local activo="${activo_casa[$casa]}"
        local version_anterior=""
        local imagen_anterior=""

        if [ -f "$archivo_estado" ]; then
            version_anterior=$(grep '^VERSION_ACTIVA=' "$archivo_estado" | cut -d'=' -f2- || true)
            imagen_anterior=$(grep '^IMAGEN_ACTIVA=' "$archivo_estado" | cut -d'=' -f2- || true)
        fi

        cat <<EOF > "$archivo_estado"
COLOR_ACTIVO=${nuevo}
COLOR_ANTERIOR=${activo}
VERSION_ACTIVA=${m_version}
VERSION_ANTERIOR=${version_anterior}
IMAGEN_ACTIVA=${m_imagen}
IMAGEN_ANTERIOR=${imagen_anterior}
EOF
    done

    # --------------------------------------------------------------------------
    # 7. LISTO
    # --------------------------------------------------------------------------
    echo "==> Paso 7: Despliegue completado con éxito"
    registrar_bitacora "deploy" "OK" "equipo=${equipo} version=${m_version} casas=${casas_str} | OK version=${m_version} casas=${casas_str} la anterior sigue viva"
}

# ------------------------------------------------------------------------------
# SUBCOMANDO: conmutar (Paso 5 y 6 manual / reintento)
# ------------------------------------------------------------------------------
comando_conmutar() {
    local equipo="$1"
    local ruta_manifiesto="$2"
    shift 2
    local casas=("$@")

    if [ ${#casas[@]} -eq 0 ]; then
        echo "ERROR: Debe especificar al menos una casa para conmutar" >&2
        exit 1
    fi

    local casas_str="${casas[*]}"
    local dir_estado="${CICD_BASE}/${equipo}/estado"

    local json_agregar="[]"
    local json_quitar="[]"

    declare -A activo_casa
    declare -A nuevo_casa
    declare -A ip_casa
    declare -A puerto_activo_casa
    declare -A puerto_nuevo_casa

    for casa in "${casas[@]}"; do
        local archivo_estado="${dir_estado}/${casa}.env"
        if [ ! -f "$archivo_estado" ]; then
            echo "ERROR: No existe estado previo en $archivo_estado" >&2
            exit 1
        fi

        local c_activo
        c_activo=$(grep '^COLOR_ACTIVO=' "$archivo_estado" | cut -d'=' -f2-)
        local c_nuevo
        c_nuevo=$(color_opuesto "$c_activo")

        local ip
        ip=$(obtener_ip_casa "$casa")
        local p_activo
        p_activo=$(obtener_puerto_color "$casa" "$c_activo")
        local p_nuevo
        p_nuevo=$(obtener_puerto_color "$casa" "$c_nuevo")

        # Verificar que el contenedor a activar esté sano
        local salud
        salud=$(ejecutar_ssh "$casa" "docker inspect -f '{{.State.Health.Status}}' sdypp-${c_nuevo}-app-1" 2>/dev/null || echo "desconocido")
        if [ "$salud" != "healthy" ]; then
            echo "ERROR: La réplica sdypp-${c_nuevo}-app-1 en $casa no está healthy (estado: $salud)" >&2
            exit 1
        fi

        activo_casa["$casa"]="$c_activo"
        nuevo_casa["$casa"]="$c_nuevo"
        ip_casa["$casa"]="$ip"
        puerto_activo_casa["$casa"]="$p_activo"
        puerto_nuevo_casa["$casa"]="$p_nuevo"

        json_agregar=$(echo "$json_agregar" | jq --arg d "${ip}:${p_nuevo}" '. + [$d]')
        json_quitar=$(echo "$json_quitar" | jq --arg d "${ip}:${p_activo}" '. + [$d]')
    done

    local payload
    payload=$(jq -n --argjson a "$json_agregar" --argjson q "$json_quitar" '{"agregar": $a, "quitar": $q}')

    for bal in $BALANCEADORES; do
        echo "Conmutando balanceador en $bal..."
        local resp_code
        resp_code=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
            -H "Content-Type: application/json" \
            -d "$payload" \
            "${bal}/admin/backends" || echo "000")

        if [ "$resp_code" != "200" ]; then
            echo "ERROR: Falló la conmutación en $bal (HTTP $resp_code)" >&2
            exit 1
        fi
    done

    # Actualizar estado
    for casa in "${casas[@]}"; do
        local archivo_estado="${dir_estado}/${casa}.env"
        local nuevo="${nuevo_casa[$casa]}"
        local activo="${activo_casa[$casa]}"

        local v_act
        local v_ant
        local img_act
        local img_ant
        v_act=$(grep '^VERSION_ACTIVA=' "$archivo_estado" | cut -d'=' -f2- || true)
        v_ant=$(grep '^VERSION_ANTERIOR=' "$archivo_estado" | cut -d'=' -f2- || true)
        img_act=$(grep '^IMAGEN_ACTIVA=' "$archivo_estado" | cut -d'=' -f2- || true)
        img_ant=$(grep '^IMAGEN_ANTERIOR=' "$archivo_estado" | cut -d'=' -f2- || true)

        # Si se pasó manifiesto, tomar de ahí la versión e imagen nuevas
        if [ -n "$ruta_manifiesto" ] && [ -f "$ruta_manifiesto" ]; then
            v_ant="$v_act"
            img_ant="$img_act"
            v_act=$(jq -r '.version // empty' "$ruta_manifiesto")
            img_act=$(jq -r '.imagen // empty' "$ruta_manifiesto")
        fi

        cat <<EOF > "$archivo_estado"
COLOR_ACTIVO=${nuevo}
COLOR_ANTERIOR=${activo}
VERSION_ACTIVA=${v_act}
VERSION_ANTERIOR=${v_ant}
IMAGEN_ACTIVA=${img_act}
IMAGEN_ANTERIOR=${img_ant}
EOF
    done

    registrar_bitacora "conmutar" "OK" "equipo=${equipo} casas=${casas_str} | Conmutación manual completada con éxito"
    echo "Conmutación finalizada con éxito."
}

# ------------------------------------------------------------------------------
# SUBCOMANDO: rollback (Sección 3.6)
# ------------------------------------------------------------------------------
comando_rollback() {
    local equipo="$1"
    shift
    local casas=("$@")

    if [ ${#casas[@]} -eq 0 ]; then
        echo "ERROR: Debe especificar al menos una casa para rollback" >&2
        exit 1
    fi

    local casas_str="${casas[*]}"
    local dir_estado="${CICD_BASE}/${equipo}/estado"

    declare -A activo_casa
    declare -A anterior_casa
    declare -A ip_casa
    declare -A puerto_activo_casa
    declare -A puerto_anterior_casa

    # 1. Verificar estado y salud de la versión anterior en todas las casas
    for casa in "${casas[@]}"; do
        local archivo_estado="${dir_estado}/${casa}.env"
        if [ ! -f "$archivo_estado" ]; then
            echo "ERROR: No existe archivo de estado para $casa en $archivo_estado" >&2
            exit 1
        fi

        local c_activo
        local c_anterior
        c_activo=$(grep '^COLOR_ACTIVO=' "$archivo_estado" | cut -d'=' -f2-)
        c_anterior=$(grep '^COLOR_ANTERIOR=' "$archivo_estado" | cut -d'=' -f2-)

        if [ -z "$c_anterior" ]; then
            echo "ERROR: No hay color anterior registrado para la casa $casa. Imposible realizar rollback." >&2
            exit 1
        fi

        local ip
        ip=$(obtener_ip_casa "$casa")
        local p_activo
        p_activo=$(obtener_puerto_color "$casa" "$c_activo")
        local p_anterior
        p_anterior=$(obtener_puerto_color "$casa" "$c_anterior")

        # Comprobar salud de la réplica anterior
        local salud
        salud=$(ejecutar_ssh "$casa" "docker inspect -f '{{.State.Health.Status}}' sdypp-${c_anterior}-app-1" 2>/dev/null || echo "no-existe")

        if [ "$salud" != "healthy" ]; then
            echo "ERROR: Réplica anterior sdypp-${c_anterior}-app-1 en $casa no está healthy (estado: '$salud'). No se realiza el rollback." >&2
            exit 1
        fi

        activo_casa["$casa"]="$c_activo"
        anterior_casa["$casa"]="$c_anterior"
        ip_casa["$casa"]="$ip"
        puerto_activo_casa["$casa"]="$p_activo"
        puerto_anterior_casa["$casa"]="$p_anterior"
    done

    # 2. Comparar GET /admin/backends con el estado local
    local primer_bal
    primer_bal=$(echo "$BALANCEADORES" | awk '{print $1}')
    echo "Consultando balanceador en $primer_bal para comparar estado..."
    local json_backends
    json_backends=$(curl -s "${primer_bal}/admin/backends" || echo "")

    if [ -z "$json_backends" ]; then
        echo "ERROR: No se pudo obtener la lista de backends desde $primer_bal" >&2
        exit 1
    fi

    for casa in "${casas[@]}"; do
        local destino_esperado="${ip_casa[$casa]}:${puerto_activo_casa[$casa]}"
        local encontrado
        encontrado=$(echo "$json_backends" | jq -r --arg d "$destino_esperado" '.backends[]? | select(.destino == $d) | .destino')

        if [ -z "$encontrado" ]; then
            echo "ERROR: El backend activo esperado ($destino_esperado) no está en el balanceador." >&2
            echo "El estado no coincide (posible conmutación manual externa). Se cancela el rollback." >&2
            exit 1
        fi
    done

    # 3. Conmutar balanceador con colores invertidos
    local json_agregar="[]"
    local json_quitar="[]"

    for casa in "${casas[@]}"; do
        local destino_anterior="${ip_casa[$casa]}:${puerto_anterior_casa[$casa]}"
        local destino_activo="${ip_casa[$casa]}:${puerto_activo_casa[$casa]}"

        json_agregar=$(echo "$json_agregar" | jq --arg d "$destino_anterior" '. + [$d]')
        json_quitar=$(echo "$json_quitar" | jq --arg d "$destino_activo" '. + [$d]')
    done

    local payload
    payload=$(jq -n --argjson a "$json_agregar" --argjson q "$json_quitar" '{"agregar": $a, "quitar": $q}')

    for bal in $BALANCEADORES; do
        echo "Conmutando balanceador $bal hacia color anterior..."
        local resp_code
        resp_code=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
            -H "Content-Type: application/json" \
            -d "$payload" \
            "${bal}/admin/backends" || echo "000")

        if [ "$resp_code" != "200" ]; then
            echo "ERROR: Falló la conmutación en $bal durante rollback (HTTP $resp_code)" >&2
            exit 1
        fi
    done

    # 4. Intercambiar activo/anterior en el archivo de estado
    local version_nueva_activa=""
    for casa in "${casas[@]}"; do
        local archivo_estado="${dir_estado}/${casa}.env"
        local act="${activo_casa[$casa]}"
        local ant="${anterior_casa[$casa]}"

        local v_act
        local v_ant
        local img_act
        local img_ant
        v_act=$(grep '^VERSION_ACTIVA=' "$archivo_estado" | cut -d'=' -f2- || true)
        v_ant=$(grep '^VERSION_ANTERIOR=' "$archivo_estado" | cut -d'=' -f2- || true)
        img_act=$(grep '^IMAGEN_ACTIVA=' "$archivo_estado" | cut -d'=' -f2- || true)
        img_ant=$(grep '^IMAGEN_ANTERIOR=' "$archivo_estado" | cut -d'=' -f2- || true)

        version_nueva_activa="$v_ant"

        cat <<EOF > "$archivo_estado"
COLOR_ACTIVO=${ant}
COLOR_ANTERIOR=${act}
VERSION_ACTIVA=${v_ant}
VERSION_ANTERIOR=${v_act}
IMAGEN_ACTIVA=${img_ant}
IMAGEN_ANTERIOR=${img_act}
EOF
    done

    registrar_bitacora "rollback" "OK" "equipo=${equipo} version=${version_nueva_activa} casas=${casas_str} | Rollback exitoso a version=${version_nueva_activa}"
    echo "Rollback completado con éxito."
}

# ------------------------------------------------------------------------------
# SUBCOMANDO: estado (Sección 3.7)
# ------------------------------------------------------------------------------
comando_estado() {
    local equipo="$1"
    shift
    local casas=("$@")

    if [ ${#casas[@]} -eq 0 ]; then
        echo "ERROR: Debe especificar al menos una casa para consultar estado" >&2
        exit 1
    fi

    local dir_estado="${CICD_BASE}/${equipo}/estado"

    echo "=============================================================================="
    echo "ESTADO DE DESPLIEGUE — Equipo: $equipo"
    echo "=============================================================================="

    # Consulta al balanceador
    local primer_bal
    primer_bal=$(echo "$BALANCEADORES" | awk '{print $1}')
    echo -e "\n--- Estado en Balanceador ($primer_bal) ---"
    local json_backends
    json_backends=$(curl -s "${primer_bal}/admin/backends" 2>/dev/null || echo "")
    if [ -n "$json_backends" ]; then
        echo "$json_backends" | jq -r '.backends[]? | "  Destino: \(.destino) | App: \(.app) | Sano: \(.sano) | Fallos: \(.fallos) | Chequeo: \(.ultimoChequeo)"' 2>/dev/null || echo "  Respuesta sin backends"
    else
        echo "  No se pudo contactar al balanceador en $primer_bal"
    fi

    # Consulta por casa
    for casa in "${casas[@]}"; do
        echo -e "\n------------------------------------------------------------------------------"
        echo "Casa: $casa"
        echo "------------------------------------------------------------------------------"

        local archivo_estado="${dir_estado}/${casa}.env"
        if [ -f "$archivo_estado" ]; then
            echo "[Estado Local]"
            sed 's/^/  /' "$archivo_estado"
        else
            echo "[Estado Local] Sin registro previo"
        fi

        echo "[Contenedores en $casa]"
        for color in blue green; do
            local contenedor="sdypp-${color}-app-1"
            local ps_info
            ps_info=$(ejecutar_ssh "$casa" "docker ps -a --filter name=^/${contenedor}$ --format 'Status: {{.Status}} | Ports: {{.Ports}}'" 2>/dev/null || echo "Error de conexión")
            local salud
            salud=$(ejecutar_ssh "$casa" "docker inspect -f '{{.State.Health.Status}}' $contenedor" 2>/dev/null || echo "no-existe")

            echo "  - $contenedor: Health: $salud | $ps_info"
        done
    done
    echo "=============================================================================="
}

# ------------------------------------------------------------------------------
# Punto de entrada y parseo de argumentos
# ------------------------------------------------------------------------------
if [ $# -lt 1 ]; then
    echo "Uso: $0 <desplegar|conmutar|rollback|estado> [opciones] <casas...>" >&2
    exit 1
fi

subcomando="$1"
shift

equipo=""
manifiesto=""
casas=()

while [ $# -gt 0 ]; do
    case "$1" in
        --equipo)
            equipo="$2"
            shift 2
            ;;
        --manifiesto)
            manifiesto="$2"
            shift 2
            ;;
        *)
            casas+=("$1")
            shift
            ;;
    esac
done

if [ -z "$equipo" ]; then
    echo "ERROR: El parámetro --equipo es obligatorio" >&2
    exit 1
fi

case "$subcomando" in
    desplegar)
        if [ -z "$manifiesto" ]; then
            echo "ERROR: El comando desplegar requiere --manifiesto <ruta>" >&2
            exit 1
        fi
        comando_desplegar "$equipo" "$manifiesto" "${casas[@]}"
        ;;
    conmutar)
        comando_conmutar "$equipo" "$manifiesto" "${casas[@]}"
        ;;
    rollback)
        comando_rollback "$equipo" "${casas[@]}"
        ;;
    estado)
        comando_estado "$equipo" "${casas[@]}"
        ;;
    *)
        echo "ERROR: Subcomando desconocido '$subcomando'. Opciones: desplegar, conmutar, rollback, estado" >&2
        exit 1
        ;;
esac
