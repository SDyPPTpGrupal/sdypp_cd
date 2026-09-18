#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# tests/e2e.sh — el CD con agentes, de punta a punta, en una sola máquina.
#
# Simula dos casas con dos agentes contra un registry y un balanceador locales.
# No usa SSH contra ninguna casa: ése es justamente el punto de la prueba.
#
#   ./tests/e2e.sh            corre todo y limpia al final
#   ./tests/e2e.sh --dejar    corre todo y deja el sistema andando para mirarlo
#   ./tests/e2e.sh --limpiar  sólo borra lo que haya quedado de una corrida previa
# ==============================================================================

RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
RUTA_BALANCEADOR="${RUTA_BALANCEADOR:-$(cd "$RAIZ/../sdypp_balanceador" 2>/dev/null && pwd || true)}"
TRABAJO="${TRABAJO:-/tmp/sdypp-e2e}"

REGISTRY="localhost:5000"
IMAGEN="${REGISTRY}/sdypp-app-python"
RED="sdypp-e2e"

# Puertos altos para no chocar con nada que el equipo tenga levantado.
BA_PUERTO=18080; BA_ADMIN=18081
CD_AGENTES=18082; CD_DISPARO=18083; CD_SSH=12222
A_BLUE=18090; A_GREEN=18091
B_BLUE=18092; B_GREEN=18093

verde() { printf '\033[32m%s\033[0m\n' "$*"; }
rojo()  { printf '\033[31m%s\033[0m\n' "$*"; }
paso()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

FALLAS=0
comprobar() {  # comprobar "descripción" "esperado" "obtenido"
    if [ "$2" = "$3" ]; then
        verde "  ✓ $1"
    else
        rojo  "  ✗ $1 — esperaba '$2', obtuvo '$3'"
        FALLAS=$((FALLAS + 1))
    fi
}

limpiar() {
    docker rm -f sdypp-e2e-cd sdypp-e2e-ba sdypp-e2e-registry \
                 sdypp-e2e-agente-a sdypp-e2e-agente-b \
                 e2e-casa-a-blue e2e-casa-a-green \
                 e2e-casa-b-blue e2e-casa-b-green >/dev/null 2>&1 || true
    docker network rm "$RED" >/dev/null 2>&1 || true
    # El CD hace chown de entrante/ a deploy-python, así que en el volumen quedan
    # archivos de un uid que no es el nuestro: se borran desde un contenedor.
    if [ -d "$TRABAJO" ] && ! rm -rf "$TRABAJO" 2>/dev/null; then
        docker run --rm -v "$(dirname "$TRABAJO"):/padre" debian:13-slim \
            rm -rf "/padre/$(basename "$TRABAJO")" >/dev/null 2>&1 || true
    fi
}

if [ "${1:-}" = "--limpiar" ]; then limpiar; verde "limpio"; exit 0; fi

# ------------------------------------------------------------------ preparación
paso "0 · Limpieza previa y directorios de trabajo"
limpiar
mkdir -p "$TRABAJO"/cd/{claves/host,python/entrante/{rechazados,historial},python/estado,java/entrante/{rechazados,historial},java/estado,logs}
mkdir -p "$TRABAJO"/casa-a/logs/{blue,green} "$TRABAJO"/casa-b/logs/{blue,green}
echo "TP_EJEMPLO=1" > "$TRABAJO"/casa-a/.env
echo "TP_EJEMPLO=1" > "$TRABAJO"/casa-b/.env
echo "trabajo en $TRABAJO"

paso "1 · Registry local"
docker run -d --name sdypp-e2e-registry -p 5000:5000 registry:2 >/dev/null
for _ in $(seq 1 30); do curl -sf "http://${REGISTRY}/v2/" >/dev/null && break; sleep 0.5; done
verde "  registry arriba en ${REGISTRY}"

paso "2 · Imágenes de la réplica de prueba (v5, v6 y una que miente la versión)"
cp "$RAIZ/bin/contrato.proto" "$RAIZ/tests/e2e/app/contrato.proto"
construir() {  # construir <tag> <VERSION horneada>
    docker build -q 2>/dev/null --build-arg "VERSION=$2" -t "${IMAGEN}:$1" "$RAIZ/tests/e2e/app" >/dev/null
    docker push -q "${IMAGEN}:$1" >/dev/null
    docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "${IMAGEN}:$1" \
        | grep "^${IMAGEN}@" | head -1 | cut -d@ -f2
}
DIGEST_V5=$(construir v5-aaaaaaa 5)
DIGEST_V6=$(construir v6-bbbbbbb 6)
# El manifiesto va a decir version 7, pero la imagen responde 99: así se prueba
# que el CD verifica por su cuenta y no le cree al reporte del agente.
DIGEST_V7=$(construir v7-ccccccc 99)
rm -f "$RAIZ/tests/e2e/app/contrato.proto"
echo "  v5 $DIGEST_V5"
echo "  v6 $DIGEST_V6"
echo "  v7 $DIGEST_V7 (la imagen dice 99)"

paso "3 · Balanceador"
if [ -z "$RUTA_BALANCEADOR" ] || [ ! -f "$RUTA_BALANCEADOR/app/balanceador.py" ]; then
    rojo "No encuentro el repo del balanceador. Poné RUTA_BALANCEADOR=/ruta/a/sdypp_balanceador"
    exit 1
fi
docker build -q 2>/dev/null -t sdypp-ba:e2e "$RUTA_BALANCEADOR" >/dev/null
docker run -d --name sdypp-e2e-ba --network host \
    -e BA_PUERTO=$BA_PUERTO -e BA_PUERTO_ADMIN=$BA_ADMIN -e BA_ADMIN_BIND=127.0.0.1 \
    -e BA_BACKENDS= -e BA_CASA=casa-e2e \
    sdypp-ba:e2e >/dev/null
for _ in $(seq 1 30); do curl -sf "http://127.0.0.1:${BA_ADMIN}/admin/backends" >/dev/null && break; sleep 0.5; done
verde "  balanceador arriba · público :$BA_PUERTO · control 127.0.0.1:$BA_ADMIN"

paso "4 · CD (sin una sola clave de SSH hacia las casas)"
docker build -q 2>/dev/null -t sdypp-cd:e2e "$RAIZ" >/dev/null
docker run -d --name sdypp-e2e-cd --network host \
    -e CASA=casa-e2e \
    -e "CASAS=casa-a=nadie@127.0.0.1 casa-b=nadie@127.0.0.1" \
    -e "CICD_NODOS_PYTHON=casa-a casa-b" \
    -e "CICD_NODOS_JAVA=casa-j" \
    -e "PUERTOS_CASA=casa-a=${A_BLUE}:${A_GREEN} casa-b=${B_BLUE}:${B_GREEN}" \
    -e "BALANCEADORES=http://127.0.0.1:${BA_ADMIN}" \
    -e "REGISTRY=${REGISTRY}" \
    -e CICD_BIND=127.0.0.1 -e CICD_PUERTO_SSH=$CD_SSH \
    -e CICD_PUERTO_AGENTES=$CD_AGENTES -e CICD_PUERTO_DISPARO=$CD_DISPARO \
    -e CICD_ESPERA_BARRERA=90 -e CICD_ESPERA_LONGPOLL=10 \
    -v "$TRABAJO/cd/claves:/cicd/claves" \
    -v "$TRABAJO/cd/python:/cicd/python" \
    -v "$TRABAJO/cd/java:/cicd/java" \
    -v "$TRABAJO/cd/logs:/cicd/logs" \
    sdypp-cd:e2e >/dev/null
for _ in $(seq 1 40); do curl -sf "http://127.0.0.1:${CD_AGENTES}/health" >/dev/null && break; sleep 0.5; done
if ! curl -sf "http://127.0.0.1:${CD_AGENTES}/health" >/dev/null; then
    rojo "El CD no levantó. Últimas líneas:"
    docker logs sdypp-e2e-cd 2>&1 | tail -15 | sed 's/^/    /'
    exit 1
fi
comprobar "el CD responde /health" "sano" \
          "$(curl -s "http://127.0.0.1:${CD_AGENTES}/health" | jq -r .cd)"

paso "5 · Dos agentes, uno por casa"
docker build -q 2>/dev/null -t sdypp-agente:e2e "$RAIZ/agente" >/dev/null
GID_DOCKER=$(stat -c '%g' /var/run/docker.sock)
levantar_agente() {  # levantar_agente <casa> <sufijo>
    docker run -d --name "sdypp-e2e-agente-$2" --network host \
        --group-add "$GID_DOCKER" \
        -v /var/run/docker.sock:/var/run/docker.sock \
        -v "$TRABAJO/$1:/casa" \
        -e "AG_CASA=$1" -e AG_EQUIPO=python \
        -e "AG_CD=http://127.0.0.1:${CD_AGENTES}" \
        -e "AG_REGISTRY=${REGISTRY}" \
        -e AG_DIR=/casa -e "AG_DIR_HOST=${TRABAJO}/$1" \
        -e "AG_CONTENEDOR=e2e-$1-{color}" \
        -e AG_ESPERA_SALUD=1 -e AG_INTENTOS_SALUD=40 \
        sdypp-agente:e2e >/dev/null
}
levantar_agente casa-a a
levantar_agente casa-b b
sleep 3
comprobar "el agente de casa-a arrancó" "true" \
          "$(docker inspect -f '{{.State.Running}}' sdypp-e2e-agente-a)"
comprobar "el agente de casa-b arrancó" "true" \
          "$(docker inspect -f '{{.State.Running}}' sdypp-e2e-agente-b)"

# ------------------------------------------------------------------ utilidades
MARCA=0
marcar() { MARCA=$(wc -l < "$TRABAJO/cd/logs/cicd.log" 2>/dev/null || echo 0); }

publicar() {  # publicar <version> <tag> <digest> — deja el manifiesto como lo haría publicar.sh
    marcar
    cat > "$TRABAJO/cd/python/entrante/manifiesto.json.tmp" <<JSON
{
  "equipo": "python",
  "imagen": "${IMAGEN}:$2",
  "digest": "$3",
  "version": $1,
  "commit": "$(echo "$2" | cut -d- -f2)",
  "publicado_por": "e2e",
  "publicado_en": "$(date -Iseconds)"
}
JSON
    mv "$TRABAJO/cd/python/entrante/manifiesto.json.tmp" "$TRABAJO/cd/python/entrante/manifiesto.json"
}

esperar_deploy() {  # esperar_deploy <OK|FALLO> <segundos> — sólo mira lo escrito desde la marca
    local esperado="$1" limite=$((SECONDS + $2))
    while [ $SECONDS -lt $limite ]; do
        if tail -n "+$((MARCA + 1))" "$TRABAJO/cd/logs/cicd.log" 2>/dev/null \
             | grep -q "| deploy | ${esperado} |"; then
            return 0
        fi
        sleep 1
    done
    return 1
}

backends() { curl -s "http://127.0.0.1:${BA_ADMIN}/admin/backends" | jq -r '.backends[].destino' | sort | tr '\n' ' '; }
version_en() { docker exec "$1" python3 -c "import os;print(os.environ['VERSION'])" 2>/dev/null || echo "-"; }

# ------------------------------------------------------------------ los casos
paso "6 · Primer deploy (v5): no había nada, arranca en blue"
publicar 5 v5-aaaaaaa "$DIGEST_V5"
if esperar_deploy OK 120; then verde "  el CD reportó OK"; else rojo "  el deploy no terminó"; FALLAS=$((FALLAS+1)); fi
comprobar "el balanceador tiene las dos blue" "127.0.0.1:${A_BLUE} 127.0.0.1:${B_BLUE} " "$(backends)"
comprobar "casa-a corre la v5" "5" "$(version_en e2e-casa-a-blue)"
comprobar "el estado guardado dice blue" "blue" \
          "$(grep ^COLOR_ACTIVO= "$TRABAJO/cd/python/estado/casa-a.env" | cut -d= -f2)"

paso "7 · Segundo deploy (v6): blue→green sin que la anterior se caiga"
publicar 6 v6-bbbbbbb "$DIGEST_V6"
if esperar_deploy OK 120; then verde "  el CD reportó OK"; else rojo "  el deploy no terminó"; FALLAS=$((FALLAS+1)); fi
comprobar "el balanceador conmutó a las green" "127.0.0.1:${A_GREEN} 127.0.0.1:${B_GREEN} " "$(backends)"
comprobar "casa-a corre la v6 en green" "6" "$(version_en e2e-casa-a-green)"
comprobar "la v5 sigue viva para el rollback" "true" \
          "$(docker inspect -f '{{.State.Running}}' e2e-casa-a-blue)"

paso "8 · Deploy mentiroso: el manifiesto dice 7, la imagen responde 99"
publicar 7 v7-ccccccc "$DIGEST_V7"
if esperar_deploy FALLO 120; then verde "  el CD lo rechazó"; else rojo "  no detectó la versión rota"; FALLAS=$((FALLAS+1)); fi
comprobar "el balanceador NO conmutó" "127.0.0.1:${A_GREEN} 127.0.0.1:${B_GREEN} " "$(backends)"
sleep 3
comprobar "el agente bajó la réplica rota" "" \
          "$(docker ps -q -f name=e2e-casa-a-blue -f status=running | tr -d '\n')"
comprobar "la v6 sigue sirviendo" "6" "$(version_en e2e-casa-a-green)"

paso "9 · Idempotencia: se vuelve a publicar la v6 que ya está corriendo"
publicar 6 v6-bbbbbbb "$DIGEST_V6"
if esperar_deploy OK 120; then verde "  el CD reportó OK"; else rojo "  el deploy no terminó"; FALLAS=$((FALLAS+1)); fi
comprobar "el agente no tocó la green que ya coincidía" "ya coincide" \
          "$(docker logs sdypp-e2e-agente-a 2>&1 | grep -o 'green: ya coincide' | tail -1 | sed 's/green: //')"

paso "10 · Migración: una réplica sin etiquetas no se recrea"
# Se simula lo que deja el CD anterior: un contenedor levantado a mano y un estado
# con IMAGEN_ACTIVA pero sin DIGEST_ACTIVO.
docker rm -f e2e-casa-a-blue >/dev/null 2>&1 || true
docker run -d --name e2e-casa-a-blue -p ${A_BLUE}:8080 \
    -e HOST_NAME=casa-a-blue "${IMAGEN}:v5-aaaaaaa" >/dev/null
ID_ANTES=$(docker inspect -f '{{.Id}}' e2e-casa-a-blue)
sed -i 's/^DIGEST_ACTIVO=.*/DIGEST_ACTIVO=/' "$TRABAJO/cd/python/estado/casa-a.env"
sed -i 's/^COLOR_ACTIVO=.*/COLOR_ACTIVO=blue/;s/^VERSION_ACTIVA=.*/VERSION_ACTIVA=5/' "$TRABAJO/cd/python/estado/casa-a.env"
sleep 2
publicar 6 v6-bbbbbbb "$DIGEST_V6"
if esperar_deploy OK 120; then verde "  el CD reportó OK"; else rojo "  el deploy no terminó"; FALLAS=$((FALLAS+1)); fi
comprobar "la réplica sin etiquetas sobrevivió" "$ID_ANTES" \
          "$(docker inspect -f '{{.Id}}' e2e-casa-a-blue 2>/dev/null)"
comprobar "el agente la adoptó en vez de recrearla" "adoptado" \
          "$(docker logs sdypp-e2e-agente-a 2>&1 | grep -o 'blue: adoptado' | tail -1 | sed 's/blue: //')"

paso "11 · El CD se reinicia: la generación sobrevive"
GEN_ANTES=$(docker exec sdypp-e2e-cd curl -s "http://127.0.0.1:${CD_DISPARO}/deploy/python/estado" | jq -r .objetivo.generacion)
docker restart sdypp-e2e-cd >/dev/null
for _ in $(seq 1 40); do curl -sf "http://127.0.0.1:${CD_AGENTES}/health" >/dev/null && break; sleep 0.5; done
comprobar "retoma la generación donde quedó" "$GEN_ANTES" \
          "$(docker exec sdypp-e2e-cd curl -s "http://127.0.0.1:${CD_DISPARO}/deploy/python/estado" | jq -r .objetivo.generacion)"

# Y lo que importa de verdad: que después del reinicio se pueda seguir desplegando.
publicar 5 v5-aaaaaaa "$DIGEST_V5"
if esperar_deploy OK 120; then verde "  despliega igual después del reinicio"; else rojo "  no pudo desplegar tras reiniciar"; FALLAS=$((FALLAS+1)); fi
# El color en el que cae depende de lo que haya pasado antes: se lo pregunta al CD
# en vez de suponerlo.
COLOR_ACTIVO=$(docker exec sdypp-e2e-cd curl -s "http://127.0.0.1:${CD_DISPARO}/deploy/python/estado" \
    | jq -r '.estadoPorCasa["casa-a"].COLOR_ACTIVO')
comprobar "volvió a la v5, en el color que corresponda ($COLOR_ACTIVO)" "5" \
          "$(version_en "e2e-casa-a-${COLOR_ACTIVO}")"

paso "12 · Resumen"
echo
docker exec sdypp-e2e-cd curl -s "http://127.0.0.1:${CD_DISPARO}/deploy/python/estado" \
    | jq '{generacion: .objetivo.generacion, reportes: .ultimosReportes | keys, estado: .estadoPorCasa | map_values(.COLOR_ACTIVO + " v" + .VERSION_ACTIVA)}'
echo
echo "bitácora del CD:"
grep -E "\| (deploy|objetivo|reporte) \|" "$TRABAJO/cd/logs/cicd.log" | tail -12 | sed 's/^/  /'

echo
if [ "$FALLAS" -eq 0 ]; then verde "TODO OK"; else rojo "$FALLAS comprobaciones fallaron"; fi

if [ "${1:-}" = "--dejar" ]; then
    echo
    echo "Queda andando. Para mirarlo:"
    echo "  curl -s http://127.0.0.1:${CD_AGENTES}/health | jq"
    echo "  curl -s http://127.0.0.1:${BA_PUERTO}/health | jq"
    echo "  docker logs -f sdypp-e2e-agente-a"
    echo "  tail -f $TRABAJO/cd/logs/cicd.log"
    echo "  $0 --limpiar    cuando termines"
else
    paso "13 · Limpieza"
    limpiar
    verde "  limpio"
fi

exit $((FALLAS > 0))
