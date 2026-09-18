#!/usr/bin/env python3
"""Servidor de control del CD: publica el objetivo y espera los reportes.

El CD dejó de ejecutar comandos remotos. Ahora hace dos cosas:

  1. **Publica un objetivo** por equipo — el estado deseado de cada casa, con
     una generación monótona. Los agentes lo leen con un GET que se cuelga
     (long-poll) hasta que hay una generación nueva.
  2. **Espera en una barrera** — los agentes reportan cuando convergieron; el
     CD verifica la versión por su cuenta con grpcurl y recién ahí conmuta el
     balanceador.

Nada de esto abre una conexión hacia una casa: el agente siempre llama al CD.
Lo único que sale del CD son el grpcurl del verify (contra la réplica, que ya
tenía que estar expuesta para el balanceador) y el POST al balanceador, que va
por loopback.

Dos sockets, como el balanceador:
  * `CICD_BIND:CICD_PUERTO_AGENTES` (8082) — lo que ven los agentes del tailnet.
  * `127.0.0.1:CICD_PUERTO_DISPARO` (8083) — lo que usa el vigilante local.

Se ejecuta:  python3 app/control.py
"""
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ------------------------------------------------------------------ configuración
# Precedencia: variable de entorno > default. Nada hardcodeado.
CICD_BASE = os.environ.get("CICD_BASE", "/cicd")
CASA = os.environ.get("CASA", "casa-tomas")
CASAS = os.environ.get("CASAS", "")
PUERTOS_CASA = os.environ.get("PUERTOS_CASA", "")
BALANCEADORES = os.environ.get("BALANCEADORES", "http://127.0.0.1:8081").split()
REGISTRY = os.environ.get("REGISTRY", "100.91.228.65:5000")

BIND = os.environ.get("CICD_BIND", "127.0.0.1")
PUERTO_AGENTES = int(os.environ.get("CICD_PUERTO_AGENTES", "8082"))
PUERTO_DISPARO = int(os.environ.get("CICD_PUERTO_DISPARO", "8083"))

# Cuánto espera la barrera a que reporten todas las casas del equipo, y cuánto
# se cuelga un long-poll antes de contestar "sin novedad".
ESPERA_BARRERA = int(os.environ.get("CICD_ESPERA_BARRERA", "180"))
ESPERA_LONGPOLL = int(os.environ.get("CICD_ESPERA_LONGPOLL", "30"))

# "casa=token casa=token". Vacío = sin autenticación (sólo para pruebas locales:
# en el tailnet cualquier casa podría reportar por otra y hacernos conmutar antes
# de tiempo).
TOKENS = {}
for _par in os.environ.get("CICD_TOKENS", "").split():
    if "=" in _par:
        _n, _t = _par.split("=", 1)
        TOKENS[_n] = _t

BITACORA = os.environ.get("CICD_BITACORA", os.path.join(CICD_BASE, "logs", "cicd.log"))
PROTO = os.environ.get("CICD_PROTO", os.path.join(CICD_BASE, "bin", "contrato.proto"))
GRPCURL = os.environ.get("CICD_GRPCURL", "grpcurl")

EQUIPOS = ("python", "java")
NODOS = {
    "python": os.environ.get("CICD_NODOS_PYTHON", "").split(),
    "java": os.environ.get("CICD_NODOS_JAVA", "").split(),
}

RE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_candado_bitacora = threading.Lock()


def ahora():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def bitacora(operacion, codigo, detalle):
    """Formato común del grupo: timestamp | quien@casa | operación | código | detalle."""
    linea = f"{ahora()} | cicd@{CASA} | {operacion} | {codigo} | {detalle}"
    with _candado_bitacora:
        try:
            os.makedirs(os.path.dirname(BITACORA), exist_ok=True)
            with open(BITACORA, "a", encoding="utf-8") as f:
                f.write(linea + "\n")
        except OSError:
            pass  # una bitácora que no se puede escribir no tumba un deploy
        print(linea, flush=True)


# ------------------------------------------------------------------ casas y colores
def ip_de(casa):
    """La IP de Tailscale de una casa, sacada de CASAS (nombre=usuario@ip)."""
    for par in CASAS.split():
        nombre, _, resto = par.partition("=")
        if nombre == casa:
            return resto.split("@")[-1]
    return ""


def puertos_de(casa):
    """(puerto_blue, puerto_green) según PUERTOS_CASA, con el default 8080/8081."""
    for par in PUERTOS_CASA.split():
        nombre, _, resto = par.partition("=")
        if nombre == casa and ":" in resto:
            azul, verde = resto.split(":", 1)
            return azul, verde
    return "8080", "8081"


def puerto_de(casa, color):
    azul, verde = puertos_de(casa)
    return azul if color == "blue" else verde


def opuesto(color):
    return "blue" if color == "green" else "green"


def ruta_estado(equipo, casa):
    return os.path.join(CICD_BASE, equipo, "estado", f"{casa}.env")


def ruta_generacion(equipo):
    return os.path.join(CICD_BASE, equipo, "estado", "generacion")


def leer_generacion(equipo):
    """La última generación publicada, que sobrevive a un reinicio del CD.

    Sin esto, un CD reiniciado vuelve a empezar en 1 y los agentes —que recuerdan
    haber aplicado la 12— ignorarían todo lo que publique: el sistema quedaría sin
    poder desplegar hasta que alguien se diera cuenta. Va en un archivo propio y no
    se deriva del estado de las casas porque la generación también sube en los
    abortos, que justamente no guardan estado.
    """
    try:
        with open(ruta_generacion(equipo), encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def escribir_generacion(equipo, generacion):
    ruta = ruta_generacion(equipo)
    try:
        os.makedirs(os.path.dirname(ruta), exist_ok=True)
        tmp = ruta + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(f"{generacion}\n")
        os.replace(tmp, ruta)
    except OSError:
        pass


def leer_estado(equipo, casa):
    """El estado de una casa como dict. Vacío si nunca se desplegó ahí."""
    datos = {}
    try:
        with open(ruta_estado(equipo, casa), encoding="utf-8") as f:
            for linea in f:
                linea = linea.strip()
                if linea and not linea.startswith("#") and "=" in linea:
                    clave, _, valor = linea.partition("=")
                    datos[clave.strip()] = valor.strip()
    except OSError:
        pass
    return datos


def escribir_estado(equipo, casa, datos):
    ruta = ruta_estado(equipo, casa)
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    tmp = ruta + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for clave in ("COLOR_ACTIVO", "COLOR_ANTERIOR", "VERSION_ACTIVA", "VERSION_ANTERIOR",
                      "IMAGEN_ACTIVA", "IMAGEN_ANTERIOR", "DIGEST_ACTIVO", "DIGEST_ANTERIOR",
                      "GENERACION"):
            f.write(f"{clave}={datos.get(clave, '')}\n")
    os.replace(tmp, ruta)  # atómico: nadie lee un estado a medio escribir


# ------------------------------------------------------------------ el pipeline
class Pipeline:
    """El estado de un equipo: el objetivo publicado y la barrera en curso.

    Un objeto por equipo. El candado protege el objetivo (lo leen los agentes
    todo el tiempo mientras un deploy lo reescribe); la condición es lo que
    despierta a los long-poll cuando sube la generación.
    """

    def __init__(self, equipo, generacion=0):
        self.equipo = equipo
        self.cambio = threading.Condition()
        # Arranca donde quedó: los agentes recuerdan la generación que aplicaron
        # aunque el CD se haya reiniciado en el medio.
        self.objetivo = {"generacion": generacion, "equipo": equipo, "casas": {}}
        self.deploy = threading.Lock()   # un deploy por equipo, igual que el flock
        # --- barrera
        self.esperadas = set()
        self.reportes = {}
        self.generacion_barrera = 0
        self.completa = threading.Event()

    # -- objetivo -------------------------------------------------------------
    def publicar(self, casas, motivo):
        """Sube la generación y despierta a todos los agentes colgados."""
        with self.cambio:
            generacion = self.objetivo["generacion"] + 1
            self.objetivo = {"generacion": generacion, "equipo": self.equipo,
                             "publicado_en": ahora(), "casas": casas}
            escribir_generacion(self.equipo, generacion)
            self.cambio.notify_all()
        bitacora("objetivo", "PUBLICADO",
                 f"equipo={self.equipo} generacion={generacion} casas={' '.join(sorted(casas))} | {motivo}")
        return generacion

    def leer_objetivo(self, desde, espera):
        """Long-poll: devuelve el objetivo sólo si su generación supera a `desde`.

        Si no hay novedad en `espera` segundos devuelve None y el agente vuelve a
        preguntar. Colgar la respuesta en vez de contestar al toque es lo que hace
        que el deploy sea inmediato sin que los agentes martillen la red.
        """
        limite = time.monotonic() + espera
        with self.cambio:
            while self.objetivo["generacion"] <= desde:
                restante = limite - time.monotonic()
                if restante <= 0:
                    return None
                self.cambio.wait(restante)
            return dict(self.objetivo)

    # -- barrera --------------------------------------------------------------
    def abrir_barrera(self, generacion, casas):
        with self.cambio:
            self.esperadas = set(casas)
            self.reportes = {}
            self.generacion_barrera = generacion
            self.completa.clear()

    def anotar_reporte(self, casa, reporte):
        """Lo llama el handler del POST /reporte, desde el hilo del servidor HTTP.

        Si con este reporte ya están todas las casas esperadas, despierta al hilo
        del deploy, que está bloqueado en `completa.wait()`. Un fallo cierra la
        barrera enseguida: no tiene sentido esperar al resto para abortar.
        """
        with self.cambio:
            if reporte.get("generacion") != self.generacion_barrera:
                return "generacion-vieja"
            if casa not in self.esperadas:
                return "casa-no-esperada"
            self.reportes[casa] = reporte
            if reporte.get("estado") != "listo" or set(self.reportes) >= self.esperadas:
                self.completa.set()
            return "anotado"


PIPELINES = {equipo: Pipeline(equipo, leer_generacion(equipo)) for equipo in EQUIPOS}


# ------------------------------------------------------------------ el deploy
def validar_manifiesto(equipo, m):
    """Las mismas reglas que el vigilante. Se repiten acá a propósito: el
    manifiesto también puede entrar por el socket de disparo."""
    if not isinstance(m, dict):
        return "el manifiesto no es un objeto JSON"
    if m.get("equipo") != equipo:
        return f"equipo del manifiesto ({m.get('equipo')!r}) != pipeline ({equipo!r})"
    imagen = m.get("imagen") or ""
    if not imagen.startswith(f"{REGISTRY}/sdypp-app-{equipo}:"):
        return f"imagen ({imagen!r}) no empieza con {REGISTRY}/sdypp-app-{equipo}:"
    if not RE_DIGEST.match(m.get("digest") or ""):
        return f"digest ({m.get('digest')!r}) no cumple ^sha256:[0-9a-f]{{64}}$"
    try:
        int(m["version"])
    except (KeyError, TypeError, ValueError):
        return f"version ({m.get('version')!r}) no es un entero"
    return None


def color_de(estado, casa, equipo):
    """(activo, nuevo) para una casa. Sin estado previo, el primer color es blue."""
    activo = estado.get("COLOR_ACTIVO") or ""
    return activo, opuesto(activo) if activo else "blue"


def armar_objetivo(equipo, casas, manifiesto):
    """El estado deseado completo: qué imagen va en cada color de cada casa.

    El color activo se describe con la imagen que ya está sirviendo — para que el
    agente lo reconozca y no lo toque — y el color nuevo con la del manifiesto.
    """
    deseado, planes = {}, {}
    for casa in casas:
        estado = leer_estado(equipo, casa)
        activo, nuevo = color_de(estado, casa, equipo)
        colores = {}
        if activo and estado.get("IMAGEN_ACTIVA"):
            colores[activo] = {
                "imagen": estado["IMAGEN_ACTIVA"],
                # Puede venir vacío si el estado lo dejó el CD anterior: el agente
                # lo interpreta como "dejá lo que haya corriendo" y adopta la réplica
                # en vez de recrearla. Ver agente.py, converger().
                "digest": estado.get("DIGEST_ACTIVO", ""),
                "puerto": puerto_de(casa, activo),
                "version": estado.get("VERSION_ACTIVA", ""),
            }
        colores[nuevo] = {
            "imagen": manifiesto["imagen"],
            "digest": manifiesto["digest"],
            "puerto": puerto_de(casa, nuevo),
            "version": str(manifiesto["version"]),
        }
        deseado[casa] = colores
        planes[casa] = {"activo": activo, "nuevo": nuevo, "estado": estado}
    return deseado, planes


def objetivo_sin(equipo, deseado, planes):
    """El mismo objetivo pero sin el color nuevo: así se aborta. Publicarlo hace
    que cada agente baje la réplica que acaba de levantar, sin comandos especiales."""
    recortado = {}
    for casa, colores in deseado.items():
        recortado[casa] = {c: v for c, v in colores.items() if c != planes[casa]["nuevo"]}
    return recortado


def verificar_version(casa, puerto, version_esperada):
    """grpcurl contra la réplica nueva. El CD confirma la versión con sus propios
    ojos: el reporte del agente es una pista, no la prueba."""
    destino = f"{ip_de(casa)}:{puerto}"
    try:
        salida = subprocess.run(
            # -import-path + nombre relativo: grpcurl rechaza un -proto absoluto
            # si no se le dice contra qué raíz resolver los imports.
            [GRPCURL, "-max-time", "5", "-plaintext",
             "-import-path", os.path.dirname(PROTO) or ".",
             "-proto", os.path.basename(PROTO),
             destino, "sdypp.Servicio/Identidad"],
            capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"grpcurl falló contra {destino}: {e}"
    if salida.returncode != 0:
        return False, f"grpcurl {destino} devolvió {salida.returncode}: {salida.stderr.strip()[:200]}"
    try:
        version = str(json.loads(salida.stdout).get("version", ""))
    except (ValueError, AttributeError):
        return False, f"respuesta de {destino} no es JSON: {salida.stdout.strip()[:200]}"
    if version != str(version_esperada):
        return False, f"{destino} dice version={version}, el manifiesto dice {version_esperada}"
    return True, f"{destino} version={version}"


def conmutar(agregar, quitar, equipo):
    """Un solo POST por balanceador con todas las casas. Se manda la forma larga
    ({destino, app}) para que las réplicas Java no figuren como Python en /health."""
    cuerpo = json.dumps({
        "agregar": [{"destino": d, "app": equipo} for d in agregar],
        "quitar": [{"destino": d, "app": equipo} for d in quitar],
    }).encode()
    errores = []
    for bal in BALANCEADORES:
        pedido = urllib.request.Request(f"{bal}/admin/backends", data=cuerpo,
                                        headers={"Content-Type": "application/json"},
                                        method="POST")
        try:
            with urllib.request.urlopen(pedido, timeout=10) as r:
                if r.status != 200:
                    errores.append(f"{bal} → HTTP {r.status}")
        except (urllib.error.URLError, OSError) as e:
            errores.append(f"{bal} → {e}")
    return errores


def desplegar(equipo, manifiesto):
    """El ciclo completo. Devuelve (ok, detalle). Lo llama el vigilante por el
    socket de disparo y se queda esperando el resultado, igual que antes esperaba
    a que terminara deploy.sh."""
    pipeline = PIPELINES[equipo]
    casas = NODOS[equipo]
    version = manifiesto["version"]

    if not casas:
        return False, f"no hay casas configuradas para {equipo} (CICD_NODOS_{equipo.upper()})"

    motivo = validar_manifiesto(equipo, manifiesto)
    if motivo:
        bitacora("deploy", "RECHAZADO", f"equipo={equipo} | {motivo}")
        return False, motivo

    if not pipeline.deploy.acquire(blocking=False):
        return False, f"ya hay un deploy de {equipo} en curso"

    try:
        casas_str = " ".join(casas)
        bitacora("deploy", "INICIO",
                 f"equipo={equipo} version={version} casas={casas_str} | {manifiesto['imagen']}")

        # 1. Objetivo: el estado deseado de cada casa.
        deseado, planes = armar_objetivo(equipo, casas, manifiesto)
        pipeline.abrir_barrera(pipeline.objetivo["generacion"] + 1, casas)
        generacion = pipeline.publicar(deseado, f"deploy version={version}")

        # 2. Barrera: los agentes convergen por su cuenta y reportan.
        completa = pipeline.completa.wait(timeout=ESPERA_BARRERA)
        with pipeline.cambio:
            reportes = dict(pipeline.reportes)

        faltan = [c for c in casas if c not in reportes]
        fallaron = [c for c, r in reportes.items() if r.get("estado") != "listo"]
        if not completa or faltan or fallaron:
            detalle = f"sin reporte: {faltan or '-'} · con fallo: {fallaron or '-'}"
            if faltan:
                detalle += " (si hay líneas 'acceso | RECHAZADO' arriba, es el token)"
            pipeline.publicar(objetivo_sin(equipo, deseado, planes),
                              f"aborta el deploy version={version}")
            bitacora("deploy", "FALLO",
                     f"equipo={equipo} version={version} casas={casas_str} | {detalle} — "
                     "nadie vio la versión rota")
            return False, detalle

        # 3. Verify: el CD comprueba la versión él mismo, contra cada réplica nueva.
        problemas = []
        for casa in casas:
            nuevo = planes[casa]["nuevo"]
            ok, detalle = verificar_version(casa, puerto_de(casa, nuevo), version)
            if not ok:
                problemas.append(detalle)
        if problemas:
            pipeline.publicar(objetivo_sin(equipo, deseado, planes),
                              f"aborta el deploy version={version} (verify)")
            bitacora("deploy", "FALLO",
                     f"equipo={equipo} version={version} casas={casas_str} | verify: {'; '.join(problemas)}")
            return False, "; ".join(problemas)

        # 4. Conmutar: un solo POST con todas las casas.
        agregar, quitar = [], []
        for casa in casas:
            ip = ip_de(casa)
            agregar.append(f"{ip}:{puerto_de(casa, planes[casa]['nuevo'])}")
            if planes[casa]["activo"]:
                quitar.append(f"{ip}:{puerto_de(casa, planes[casa]['activo'])}")
        errores = conmutar(agregar, quitar, equipo)
        if errores:
            bitacora("deploy", "FALLO",
                     f"equipo={equipo} version={version} casas={casas_str} | conmutar: {'; '.join(errores)} — "
                     "las réplicas nuevas quedan arriba y sanas, hay que reintentar conmutar")
            return False, "; ".join(errores)

        # 5. Guardar: recién con el balanceador conmutado el deploy es un hecho.
        for casa in casas:
            plan, estado = planes[casa], planes[casa]["estado"]
            escribir_estado(equipo, casa, {
                "COLOR_ACTIVO": plan["nuevo"],
                "COLOR_ANTERIOR": plan["activo"],
                "VERSION_ACTIVA": str(version),
                "VERSION_ANTERIOR": estado.get("VERSION_ACTIVA", ""),
                "IMAGEN_ACTIVA": manifiesto["imagen"],
                "IMAGEN_ANTERIOR": estado.get("IMAGEN_ACTIVA", ""),
                "DIGEST_ACTIVO": manifiesto["digest"],
                "DIGEST_ANTERIOR": estado.get("DIGEST_ACTIVO", ""),
                "GENERACION": str(generacion),
            })
        bitacora("deploy", "OK",
                 f"equipo={equipo} version={version} casas={casas_str} | generacion={generacion} "
                 "la anterior sigue viva")
        return True, f"generacion={generacion}"
    finally:
        pipeline.deploy.release()


def rollback(equipo):
    """Conmutar al revés. La versión anterior sigue corriendo: no se reconstruye
    ni se vuelve a bajar nada, sólo se cambia quién está en rotación."""
    pipeline = PIPELINES[equipo]
    casas = NODOS[equipo]
    if not pipeline.deploy.acquire(blocking=False):
        return False, f"hay un deploy de {equipo} en curso"
    try:
        agregar, quitar, estados = [], [], {}
        for casa in casas:
            estado = leer_estado(equipo, casa)
            anterior = estado.get("COLOR_ANTERIOR")
            if not anterior:
                return False, f"{casa} no tiene versión anterior a la que volver"
            estados[casa] = estado
            ip = ip_de(casa)
            agregar.append(f"{ip}:{puerto_de(casa, anterior)}")
            quitar.append(f"{ip}:{puerto_de(casa, estado['COLOR_ACTIVO'])}")

        # Antes de tocar nada: que la anterior siga sana en todas las casas.
        for casa in casas:
            estado = estados[casa]
            ok, detalle = verificar_version(casa, puerto_de(casa, estado["COLOR_ANTERIOR"]),
                                            estado.get("VERSION_ANTERIOR", ""))
            if not ok:
                bitacora("rollback", "FALLO", f"equipo={equipo} | {detalle}")
                return False, detalle

        errores = conmutar(agregar, quitar, equipo)
        if errores:
            bitacora("rollback", "FALLO", f"equipo={equipo} | {'; '.join(errores)}")
            return False, "; ".join(errores)

        for casa in casas:
            estado = estados[casa]
            escribir_estado(equipo, casa, {
                "COLOR_ACTIVO": estado["COLOR_ANTERIOR"],
                "COLOR_ANTERIOR": estado["COLOR_ACTIVO"],
                "VERSION_ACTIVA": estado.get("VERSION_ANTERIOR", ""),
                "VERSION_ANTERIOR": estado.get("VERSION_ACTIVA", ""),
                "IMAGEN_ACTIVA": estado.get("IMAGEN_ANTERIOR", ""),
                "IMAGEN_ANTERIOR": estado.get("IMAGEN_ACTIVA", ""),
                "DIGEST_ACTIVO": estado.get("DIGEST_ANTERIOR", ""),
                "DIGEST_ANTERIOR": estado.get("DIGEST_ACTIVO", ""),
                "GENERACION": estado.get("GENERACION", ""),
            })
        bitacora("rollback", "OK", f"equipo={equipo} casas={' '.join(casas)}")
        return True, "conmutado a la versión anterior"
    finally:
        pipeline.deploy.release()


def estado_completo(equipo):
    pipeline = PIPELINES[equipo]
    with pipeline.cambio:
        objetivo = dict(pipeline.objetivo)
        reportes = dict(pipeline.reportes)
    return {
        "equipo": equipo,
        "casas": NODOS[equipo],
        "objetivo": objetivo,
        "ultimosReportes": reportes,
        "estadoPorCasa": {casa: leer_estado(equipo, casa) for casa in NODOS[equipo]},
        "balanceadores": BALANCEADORES,
    }


# ------------------------------------------------------------------ HTTP
class Manejador(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "sdypp-cd/2.0"

    def log_message(self, formato, *args):
        pass  # la bitácora del CD es la del grupo, no la de BaseHTTPRequestHandler

    def responder(self, codigo, cuerpo):
        datos = json.dumps(cuerpo, ensure_ascii=False).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)

    def cuerpo_json(self):
        try:
            largo = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(largo) or b"{}")
        except (ValueError, OSError):
            return None

    def partes(self):
        """/deploy/<equipo>/<accion> → (equipo, accion) o (None, None)."""
        camino = self.path.split("?", 1)[0].strip("/").split("/")
        if len(camino) == 3 and camino[0] == "deploy" and camino[1] in EQUIPOS:
            return camino[1], camino[2]
        return None, None

    def parametro(self, nombre, default=""):
        _, _, cadena = self.path.partition("?")
        for par in cadena.split("&"):
            clave, _, valor = par.partition("=")
            if clave == nombre:
                return valor
        return default


class ManejadorAgentes(Manejador):
    """El socket que ven las casas. Sólo sabe de objetivo, reporte y health."""

    def casa_autorizada(self, casa):
        """Sin tokens configurados no se autentica (pruebas locales). Con tokens,
        cada casa sólo puede hablar por sí misma: si no, una casa podría reportar
        por otra y hacernos conmutar antes de tiempo.

        Es todo o nada: en cuanto CICD_TOKENS trae aunque sea un token, **todas**
        las casas necesitan el suyo. Una casa con token vacío no es una excepción,
        es un agujero — y sería justo la que un tercero elegiría suplantar.
        """
        if not TOKENS:
            return True
        enviado = self.headers.get("X-Casa-Token", "")
        return bool(enviado) and TOKENS.get(casa) == enviado

    def do_GET(self):
        if self.path.split("?")[0] == "/health":
            return self.responder(200, {
                "cd": "sano",
                "casa": CASA,
                # Para que el asistente del agente sepa si tiene que pedir un token.
                "pideToken": bool(TOKENS),
                # Los puertos van acá para que cada casa pueda comprobar que los
                # suyos están libres antes de que un docker run falle en medio de
                # un deploy — y ese deploy aborta para todas, no sólo para ella.
                "equipos": {e: {"generacion": PIPELINES[e].objetivo["generacion"],
                                "casas": NODOS[e],
                                "puertos": {c: {"blue": puerto_de(c, "blue"),
                                                "green": puerto_de(c, "green")}
                                            for c in NODOS[e]}} for e in EQUIPOS},
            })
        equipo, accion = self.partes()
        if accion != "objetivo":
            return self.responder(404, {"error": "no existe"})
        casa = self.parametro("casa")
        if casa and not self.casa_autorizada(casa):
            bitacora("acceso", "RECHAZADO",
                     f"equipo={equipo} casa={casa} ip={self.client_address[0]} "
                     "| GET objetivo con token inválido o ausente")
            return self.responder(403, {"error": "token inválido"})
        try:
            desde = int(self.parametro("generacion", "0"))
        except ValueError:
            return self.responder(400, {"error": "generacion no es un entero"})
        objetivo = PIPELINES[equipo].leer_objetivo(desde, ESPERA_LONGPOLL)
        if objetivo is None:
            # Sin novedad. Se contesta 200 con un flag y no 204: un 204 no lleva
            # cuerpo, y con keep-alive es más fácil equivocarse que ganar algo.
            return self.responder(200, {"novedad": False, "generacion": desde})
        objetivo["novedad"] = True
        return self.responder(200, objetivo)

    def do_POST(self):
        equipo, accion = self.partes()
        if accion != "reporte":
            return self.responder(404, {"error": "no existe"})
        cuerpo = self.cuerpo_json()
        if not isinstance(cuerpo, dict) or not cuerpo.get("casa"):
            return self.responder(400, {"error": "falta 'casa'"})
        casa = cuerpo["casa"]
        if not self.casa_autorizada(casa):
            bitacora("acceso", "RECHAZADO",
                     f"equipo={equipo} casa={casa} ip={self.client_address[0]} "
                     "| POST reporte con token inválido o ausente")
            return self.responder(403, {"error": "token inválido"})
        resultado = PIPELINES[equipo].anotar_reporte(casa, cuerpo)
        versiones = cuerpo.get("versiones") or {}
        resumen = " ".join(f"{c}=v{v}" for c, v in sorted(versiones.items())) or "-"
        bitacora("reporte", resultado.upper(),
                 f"equipo={equipo} casa={casa} generacion={cuerpo.get('generacion')} "
                 f"estado={cuerpo.get('estado')} versiones={resumen} "
                 f"| {cuerpo.get('detalle', '')}")
        return self.responder(200, {"resultado": resultado})


class ManejadorDisparo(Manejador):
    """El socket de loopback: lo usa el vigilante del propio contenedor.

    Se separa en otro socket en vez de filtrar por IP sobre el mismo, igual que
    hace el balanceador con su plano de control: si el filtro se escribe mal, un
    puerto abierto al tailnet dispara deploys.
    """

    def do_POST(self):
        equipo, accion = self.partes()
        if accion == "desplegar":
            manifiesto = self.cuerpo_json()
            if manifiesto is None:
                return self.responder(400, {"error": "cuerpo no es JSON"})
            ok, detalle = desplegar(equipo, manifiesto)
            return self.responder(200 if ok else 409, {"ok": ok, "detalle": detalle})
        if accion == "rollback":
            ok, detalle = rollback(equipo)
            return self.responder(200 if ok else 409, {"ok": ok, "detalle": detalle})
        return self.responder(404, {"error": "no existe"})

    def do_GET(self):
        equipo, accion = self.partes()
        if accion == "estado":
            return self.responder(200, estado_completo(equipo))
        return self.responder(404, {"error": "no existe"})


def main():
    for equipo in EQUIPOS:
        os.makedirs(os.path.join(CICD_BASE, equipo, "estado"), exist_ok=True)

    agentes = ThreadingHTTPServer((BIND, PUERTO_AGENTES), ManejadorAgentes)
    agentes.daemon_threads = True  # un long-poll colgado no puede bloquear al resto
    threading.Thread(target=agentes.serve_forever, daemon=True).start()

    disparo = ThreadingHTTPServer(("127.0.0.1", PUERTO_DISPARO), ManejadorDisparo)
    disparo.daemon_threads = True

    generaciones = " ".join(f"{e}={PIPELINES[e].objetivo['generacion']}" for e in EQUIPOS)
    bitacora("control", "ARRIBA",
             f"generaciones={generaciones} "
             f"agentes={BIND}:{PUERTO_AGENTES} disparo=127.0.0.1:{PUERTO_DISPARO} "
             f"python={' '.join(NODOS['python']) or '-'} java={' '.join(NODOS['java']) or '-'} "
             f"balanceadores={' '.join(BALANCEADORES)} tokens={'sí' if TOKENS else 'no'}")
    try:
        disparo.serve_forever()
    except KeyboardInterrupt:
        bitacora("control", "ABAJO", "señal recibida")


if __name__ == "__main__":
    main()
