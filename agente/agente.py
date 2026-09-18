#!/usr/bin/env python3
"""Agente de casa: converge hacia el objetivo que publica el CD.

Reemplaza al `ssh docker pull` / `ssh docker run` que hacía el CD. La diferencia
no es sólo quién ejecuta: el agente **no recibe órdenes**, recibe una descripción
de cómo tiene que verse su casa y la compara con lo que hay. Si ya coincide, no
hace nada. Si se reinicia a mitad de un deploy, retoma solo.

Todas las conexiones las inicia el agente. La casa no abre ningún puerto nuevo:
los únicos que escuchan ahí siguen siendo los de las réplicas, que ya tenían que
estar expuestos para el balanceador.

Se ejecuta:  AG_CASA=casa-tomas AG_CD=http://100.101.15.93:8082 python3 agente.py
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# ------------------------------------------------------------------ configuración
CASA = os.environ.get("AG_CASA", "")
EQUIPO = os.environ.get("AG_EQUIPO", "python")
CD = os.environ.get("AG_CD", "http://127.0.0.1:8082").rstrip("/")
TOKEN = os.environ.get("AG_TOKEN", "")
REGISTRY = os.environ.get("AG_REGISTRY", "100.91.228.65:5000")

# En la casa: ahí están el .env y los logs/. El agente no los toca, sólo los monta.
#
# Son dos rutas y no una porque el agente corre en un contenedor pero le habla al
# daemon del host: `--env-file` lo lee el cliente (ruta de adentro, AG_DIR) y el
# `-v` lo resuelve el daemon (ruta del host, AG_DIR_HOST). Si corre suelto en la
# máquina, las dos son la misma y alcanza con AG_DIR.
DIR = os.path.expandvars(os.environ.get("AG_DIR", "$HOME/sdypp"))
DIR_HOST = os.path.expandvars(os.environ.get("AG_DIR_HOST", DIR))
CONTENEDOR = os.environ.get("AG_CONTENEDOR", "sdypp-{color}-app-1")
PUERTO_INTERNO = os.environ.get("AG_PUERTO_INTERNO", "8080")
STOP_TIMEOUT = os.environ.get("AG_STOP_TIMEOUT", "15")

INTENTOS_SALUD = int(os.environ.get("AG_INTENTOS_SALUD", "30"))
ESPERA_SALUD = float(os.environ.get("AG_ESPERA_SALUD", "2"))
ESPERA_REINTENTO = float(os.environ.get("AG_ESPERA_REINTENTO", "5"))
# Un rechazo no se arregla reintentando: alguien tiene que tocar la configuración.
# Se espera más para no llenar la bitácora del CD mientras tanto.
ESPERA_RECHAZO = float(os.environ.get("AG_ESPERA_RECHAZO", "30"))
TIMEOUT_LONGPOLL = float(os.environ.get("AG_TIMEOUT_LONGPOLL", "45"))
DOCKER = os.environ.get("AG_DOCKER", "docker")
BITACORA = os.environ.get("AG_BITACORA", os.path.join(DIR, "logs", "agente.log"))
# Se toca en cada vuelta del loop: el HEALTHCHECK mira su antigüedad, así detecta
# un agente colgado y no sólo uno muerto.
LATIDO = os.environ.get("AG_LATIDO", os.path.join(DIR, "agente.latido"))

COLORES = ("blue", "green")
RE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def ahora():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def latir():
    try:
        os.makedirs(os.path.dirname(LATIDO), exist_ok=True)
        with open(LATIDO, "w", encoding="utf-8") as f:
            f.write(ahora() + "\n")
    except OSError:
        pass


def bitacora(operacion, codigo, detalle):
    """Mismo formato que el resto del sistema: timestamp | quien@casa | op | código | detalle."""
    linea = f"{ahora()} | agente@{CASA} | {operacion} | {codigo} | {detalle}"
    try:
        os.makedirs(os.path.dirname(BITACORA), exist_ok=True)
        with open(BITACORA, "a", encoding="utf-8") as f:
            f.write(linea + "\n")
    except OSError:
        pass
    print(linea, flush=True)


# ------------------------------------------------------------------ docker
def docker(*args, timeout=180):
    return subprocess.run([DOCKER, *args], capture_output=True, text=True, timeout=timeout)


FORMATO_INSPECT = (
    "{{.State.Running}}|"
    "{{if .State.Health}}{{.State.Health.Status}}{{else}}sin-health{{end}}|"
    '{{index .Config.Labels "sdypp.digest"}}|'
    '{{index .Config.Labels "sdypp.puerto"}}|'
    '{{index .Config.Labels "sdypp.generacion"}}'
)


def inspeccionar(nombre):
    """Lo que hay corriendo con ese nombre, o None si no existe.

    El digest y el puerto se leen de etiquetas que puso el propio agente al crear
    el contenedor: `docker inspect` sólo sabe del image id, que no sirve para
    compararlo contra el repo digest del manifiesto.
    """
    salida = docker("inspect", "-f", FORMATO_INSPECT, nombre, timeout=30)
    if salida.returncode != 0:
        return None
    partes = salida.stdout.strip().split("|")
    if len(partes) != 5:
        return None
    return {"corriendo": partes[0] == "true", "salud": partes[1],
            "digest": partes[2], "puerto": partes[3], "generacion": partes[4]}


def sin_tag(imagen):
    """100.91.228.65:5000/sdypp-app-python:v7-abc → 100.91.228.65:5000/sdypp-app-python

    Se parte por el último '/' antes de buscar el ':' del tag, porque el registry
    lleva puerto y si no se le come el host.
    """
    base, barra, ultimo = imagen.rpartition("/")
    return f"{base}{barra}{ultimo.split(':', 1)[0]}"


def bajar(spec):
    """docker pull por digest y se le pone el tag del manifiesto.

    Por digest y no por tag: lo que corre es exactamente lo que se publicó,
    aunque alguien haya reescrito el tag en el registry.
    """
    referencia = f"{sin_tag(spec['imagen'])}@{spec['digest']}"
    salida = docker("pull", referencia, timeout=600)
    if salida.returncode != 0:
        return f"docker pull {referencia}: {salida.stderr.strip()[:300]}"
    salida = docker("tag", referencia, spec["imagen"], timeout=60)
    if salida.returncode != 0:
        return f"docker tag {spec['imagen']}: {salida.stderr.strip()[:300]}"
    return None


def levantar(color, spec, generacion):
    nombre = CONTENEDOR.format(color=color)
    docker("rm", "-f", nombre, timeout=120)  # si no existe, no pasa nada

    orden = ["run", "-d", "--name", nombre, "--restart", "unless-stopped",
             "-p", f"{spec['puerto']}:{PUERTO_INTERNO}",
             "-e", f"HOST_NAME={CASA}-{color}", "-e", f"CASA={CASA}",
             "--stop-timeout", STOP_TIMEOUT,
             "--label", f"sdypp.digest={spec['digest']}",
             "--label", f"sdypp.puerto={spec['puerto']}",
             "--label", f"sdypp.generacion={generacion}",
             "--label", f"sdypp.color={color}"]

    entorno = os.path.join(DIR, ".env")
    if os.path.isfile(entorno):
        orden += ["--env-file", entorno]
    if os.path.isdir(os.path.join(DIR, "logs", color)):
        # La ruta que se monta es la del host: el bind mount lo resuelve el daemon.
        orden += ["-v", f"{os.path.join(DIR_HOST, 'logs', color)}:/app/logs"]

    orden.append(spec["imagen"])
    salida = docker(*orden, timeout=180)
    if salida.returncode != 0:
        return f"docker run {nombre}: {salida.stderr.strip()[:300]}"
    return None


def esperar_sano(color):
    """healthy, o corriendo si la imagen no declara HEALTHCHECK."""
    nombre = CONTENEDOR.format(color=color)
    for _ in range(INTENTOS_SALUD):
        actual = inspeccionar(nombre)
        if actual and actual["corriendo"]:
            if actual["salud"] in ("healthy", "sin-health"):
                return None
            if actual["salud"] == "unhealthy":
                return f"{nombre} quedó unhealthy"
        elif actual and not actual["corriendo"]:
            return f"{nombre} se cayó apenas arrancó"
        time.sleep(ESPERA_SALUD)
    return f"{nombre} no llegó a healthy en {INTENTOS_SALUD} intentos"


# ------------------------------------------------------------------ reconciliación
def validar(spec):
    """El agente no corre cualquier cosa que le manden: aunque el CD estuviera
    comprometido, lo peor que puede pedir es una imagen del registry propio."""
    imagen = spec.get("imagen") or ""
    if not imagen.startswith(f"{REGISTRY}/sdypp-app-{EQUIPO}:"):
        return f"imagen {imagen!r} no es de {REGISTRY}/sdypp-app-{EQUIPO}"
    if not RE_DIGEST.match(spec.get("digest") or ""):
        return f"digest {spec.get('digest')!r} mal formado"
    if not str(spec.get("puerto") or "").isdigit():
        return f"puerto {spec.get('puerto')!r} no es un número"
    return None


def converger(deseado, generacion):
    """Lleva la casa al estado deseado. Idempotente: si ya coincide, no toca nada.

    Devuelve (ok, detalle, versiones), con la versión de cada color que quedó
    arriba. Son las dos y no una sola porque en un deploy conviven: la que servía
    y la que se acaba de levantar. Reportar una sola dejaba en la bitácora del CD
    un `version=5` en medio de un deploy de la 6.

    Un color que no aparece en el objetivo es un color que no debe existir: así se
    aborta un deploy, publicando el objetivo sin él, sin un comando de aborto.
    """
    cambios, versiones = [], {}
    for color in COLORES:
        spec = deseado.get(color)
        nombre = CONTENEDOR.format(color=color)
        actual = inspeccionar(nombre)

        if not spec:
            if actual:
                docker("rm", "-f", nombre, timeout=120)
                cambios.append(f"{color}: bajado")
            continue
        versiones[color] = str(spec.get("version", ""))

        # Adopción. Un color cuyo objetivo viene sin digest es uno que el CD no
        # sabe describir: pasa una sola vez, con el estado que dejó el CD anterior
        # (que guardaba la imagen pero no el digest). Si hay algo corriendo ahí, se
        # lo deja en paz — recrearlo cortaría la réplica que está sirviendo, justo
        # en la migración. En el deploy siguiente ese color ya lleva las etiquetas
        # del agente y entra en el camino normal.
        if not spec.get("digest"):
            if actual and actual["corriendo"]:
                cambios.append(f"{color}: adoptado (el objetivo no trae digest)")
                continue
            return False, f"{color}: el objetivo no trae digest y no hay nada corriendo", versiones

        motivo = validar(spec)
        if motivo:
            return False, f"{color}: objetivo inválido — {motivo}", versiones

        if (actual and actual["corriendo"]
                and actual["digest"] == spec["digest"]
                and actual["puerto"] == str(spec["puerto"])):
            cambios.append(f"{color}: ya coincide")
            continue

        error = bajar(spec)
        if error:
            return False, f"{color}: {error}", versiones
        error = levantar(color, spec, generacion)
        if error:
            return False, f"{color}: {error}", versiones
        cambios.append(f"{color}: levantado en :{spec['puerto']}")

    for color in COLORES:
        if deseado.get(color):
            error = esperar_sano(color)
            if error:
                return False, error, versiones

    return True, " · ".join(cambios), versiones


# ------------------------------------------------------------------ diálogo con el CD
def cabeceras():
    return {"X-Casa-Token": TOKEN} if TOKEN else {}


def pedir_objetivo(desde):
    """GET con long-poll: la respuesta se cuelga hasta que hay una generación
    nueva. Cuando al CD se le acaba la espera contesta novedad=False, así que el
    ritmo lo marca él y acá no hace falta dormir entre vueltas."""
    url = f"{CD}/deploy/{EQUIPO}/objetivo?casa={CASA}&generacion={desde}"
    pedido = urllib.request.Request(url, headers=cabeceras(), method="GET")
    with urllib.request.urlopen(pedido, timeout=TIMEOUT_LONGPOLL) as r:
        respuesta = json.loads(r.read() or b"{}")
    return respuesta if respuesta.get("novedad") else None


def reportar(generacion, ok, detalle, versiones):
    cuerpo = json.dumps({
        "casa": CASA, "generacion": generacion,
        "estado": "listo" if ok else "fallo",
        "versiones": versiones, "detalle": detalle,
    }).encode()
    cabecera = {"Content-Type": "application/json", **cabeceras()}
    pedido = urllib.request.Request(f"{CD}/deploy/{EQUIPO}/reporte", data=cuerpo,
                                    headers=cabecera, method="POST")
    with urllib.request.urlopen(pedido, timeout=15) as r:
        return r.status == 200


def ciclo():
    """Un paso del loop. Devuelve la generación aplicada (o la misma si no hubo nada)."""
    # Arranca en 0 a propósito: el primer GET devuelve el objetivo vigente al toque,
    # se converge (idempotente) y se reporta. Así un agente que se reinició a mitad
    # de un deploy vuelve a la fila sin que nadie lo empuje.
    aplicada = 0
    while True:
        latir()
        try:
            objetivo = pedir_objetivo(aplicada)
        except urllib.error.HTTPError as e:
            # Va antes del URLError, del que hereda: un 403 no es "no llego al CD",
            # es "el CD me contestó que no". Confundirlos manda a revisar la red
            # cuando el problema está en una variable.
            if e.code == 403:
                bitacora("objetivo", "RECHAZADO",
                         f"el CD rechazó el token de {CASA} — que AG_TOKEN coincida con "
                         f"el de esta casa en CICD_TOKENS del CD. No se toca nada")
                time.sleep(ESPERA_RECHAZO)
            else:
                bitacora("objetivo", "SIN-CD", f"{CD}: HTTP {e.code} — no se toca nada")
                time.sleep(ESPERA_REINTENTO)
            continue
        except (urllib.error.URLError, OSError, ValueError) as e:
            bitacora("objetivo", "SIN-CD", f"{CD}: {e} — no se toca nada")
            time.sleep(ESPERA_REINTENTO)
            continue

        if objetivo is None:
            continue  # sin novedad

        generacion = objetivo.get("generacion", 0)
        if generacion <= aplicada:
            continue

        mio = (objetivo.get("casas") or {}).get(CASA)
        if mio is None:
            # El objetivo no habla de esta casa: se toma nota de la generación para
            # no volver a evaluarlo, pero no se reporta ni se toca nada.
            bitacora("objetivo", "AJENO", f"generacion={generacion} no incluye a {CASA}")
            aplicada = generacion
            continue

        bitacora("converger", "INICIO",
                 f"generacion={generacion} colores={' '.join(sorted(mio))}")
        ok, detalle, versiones = converger(mio, generacion)
        aplicada = generacion
        bitacora("converger", "OK" if ok else "FALLO", f"generacion={generacion} | {detalle}")

        try:
            reportar(generacion, ok, detalle, versiones)
            bitacora("reporte", "ENVIADO", f"generacion={generacion} estado={'listo' if ok else 'fallo'}")
        except (urllib.error.URLError, OSError) as e:
            # No se reintenta el reporte: si el CD no lo escuchó, su barrera vence
            # y publica el objetivo de aborto, que este mismo loop va a aplicar.
            bitacora("reporte", "FALLO", f"generacion={generacion}: {e}")


def main():
    if not CASA:
        print("ERROR: falta AG_CASA", file=sys.stderr)
        return 1
    if EQUIPO not in ("python", "java"):
        print(f"ERROR: AG_EQUIPO inválido: {EQUIPO}", file=sys.stderr)
        return 1
    salida = docker("version", "-f", "{{.Server.Version}}", timeout=30)
    if salida.returncode != 0:
        print(f"ERROR: no se puede hablar con Docker: {salida.stderr.strip()}", file=sys.stderr)
        return 1

    bitacora("agente", "ARRIBA",
             f"equipo={EQUIPO} cd={CD} registry={REGISTRY} docker={salida.stdout.strip()} "
             f"contenedores={CONTENEDOR.format(color='<color>')} token={'sí' if TOKEN else 'no'}")
    try:
        ciclo()
    except KeyboardInterrupt:
        bitacora("agente", "ABAJO", "señal recibida")
    return 0


if __name__ == "__main__":
    sys.exit(main())
