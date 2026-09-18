#!/usr/bin/env python3
"""Asistente de configuración del agente de casa.

Arma la configuración, la comprueba contra el sistema de verdad y deja el agente
andando. Existe porque el `docker run` del agente tiene diez variables y una sola
mal puesta —`AG_CASA` copiado de otra casa, por ejemplo— se manifiesta como un
deploy que aborta culpando a la máquina equivocada.

    python3 configurar.py              pregunta, comprueba y ofrece levantar
    python3 configurar.py --mostrar    muestra la configuración guardada
    python3 configurar.py --levantar   levanta con lo guardado, sin preguntar
    python3 configurar.py --probar     sólo corre las comprobaciones

Sólo biblioteca estándar. No pide sudo: si falta algo que lo necesita, lo dice y
te da el comando.
"""
import getpass
import json
import os
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

CARPETA_POR_DEFECTO = os.path.join(os.path.expanduser("~"), "sdypp")
ARCHIVO_AGENTE = "agente.env"      # lo lee el --env-file del docker run
ARCHIVO_APP = ".env"               # el de la réplica: TP_REDIS_URL
REGISTRY_POR_DEFECTO = "100.91.228.65:5000"
PUERTO_CD = 8082
SOCKET_DOCKER = "/var/run/docker.sock"

COLOR = sys.stdout.isatty() and os.environ.get("TERM") not in (None, "dumb")


def pintar(texto, codigo):
    return f"\033[{codigo}m{texto}\033[0m" if COLOR else texto


def titulo(t):
    print("\n" + pintar(f"=== {t} ===", "1"))


def ok(t):
    print(pintar(f"  ✓ {t}", "32"))


def mal(t):
    print(pintar(f"  ✗ {t}", "31"))


def aviso(t):
    print(pintar(f"  ! {t}", "33"))


def nota(t):
    print(pintar(f"  {t}", "90"))


# ------------------------------------------------------------------ detección
def correr(*orden, timeout=10):
    try:
        r = subprocess.run(orden, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def cd_pide_token(url):
    """¿Este CD autentica a las casas? None si no se lo pudo preguntar."""
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=8) as r:
            return bool(json.loads(r.read()).get("pideToken"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def ip_tailscale():
    """La IP de esta máquina en el tailnet, para sugerir un nombre de casa."""
    salida = correr("tailscale", "ip", "-4")
    if salida:
        return salida.splitlines()[0].strip()
    for linea in correr("ip", "-4", "-o", "addr", "show", "tailscale0").split("\n"):
        hallado = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", linea)
        if hallado:
            return hallado.group(1)
    return ""


def grupo_socket_docker():
    try:
        return str(os.stat(SOCKET_DOCKER).st_gid)
    except OSError:
        return ""


def version_docker():
    return correr("docker", "version", "-f", "{{.Server.Version}}")


def registry_inseguro_configurado(registry):
    """¿El daemon acepta este registry sin TLS? Sin esto el `docker pull` del
    agente falla con un error de certificado que no dice nada útil."""
    host = registry.partition(":")[0]
    if host in ("localhost", "127.0.0.1", "::1"):
        return True  # Docker ya los trata como inseguros, no hace falta declararlos
    try:
        with open("/etc/docker/daemon.json", encoding="utf-8") as f:
            return registry in (json.load(f).get("insecure-registries") or [])
    except (OSError, ValueError):
        return False


# ------------------------------------------------------------------ preguntas
def preguntar(etiqueta, ayuda=None, default="", obligatorio=True, secreto=False,
              validar=None):
    """Pregunta hasta que la respuesta sirva. Enter acepta el default."""
    while True:
        print()
        print(pintar(etiqueta, "1"))
        if ayuda:
            nota(ayuda)
        if default:
            nota(f"[{'·' * 12 if secreto else default}]  ⏎ para aceptar")
        try:
            leido = (getpass.getpass("  > ") if secreto else input("  > ")).strip()
        except (EOFError, KeyboardInterrupt):
            print("\ncancelado")
            sys.exit(1)
        valor = leido or default
        if not valor and obligatorio:
            mal("hace falta un valor")
            continue
        if validar:
            problema = validar(valor)
            if problema:
                mal(problema)
                continue
        return valor


def validar_casa(valor):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,30}", valor):
        return "sólo minúsculas, números y guiones (ej: casa-salvador)"
    return None


def validar_url_cd(valor):
    partes = urllib.parse.urlparse(valor)
    if partes.scheme != "http" or not partes.hostname:
        return "tiene que ser http://<ip>:<puerto>"
    return None


def validar_redis(valor):
    if not valor.startswith("redis://"):
        return "tiene que empezar con redis://"
    if "@" not in valor:
        return "falta la contraseña: redis://:<clave>@<ip>:6379/0"
    return None


def validar_registry(valor):
    if not re.fullmatch(r"[\w.-]+(:\d+)?", valor):
        return "tiene que ser <ip>:<puerto>"
    return None


# ------------------------------------------------------------------ comprobaciones
def probar_docker():
    version = version_docker()
    if not version:
        mal("no se puede hablar con Docker. ¿Está corriendo? ¿Tu usuario está en el grupo docker?")
        nota("sudo usermod -aG docker $USER   (y volvé a entrar a la sesión)")
        return False
    grupo = grupo_socket_docker()
    if not grupo:
        mal(f"no existe {SOCKET_DOCKER}: el agente necesita el socket de esta máquina")
        return False
    ok(f"docker {version}, grupo del socket {grupo}")
    return True


def probar_cd(cfg):
    """Conectividad y token, en un solo paso.

    El truco del token: un pedido con token inválido se contesta 403 al instante,
    mientras que uno válido puede quedarse colgado en el long-poll. Así que un
    timeout corto sin respuesta es, justamente, la señal de que pasó.
    """
    url = f"{cfg['AG_CD']}/health"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            salud = json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError) as e:
        mal(f"el CD no responde en {cfg['AG_CD']}: {e}")
        nota("revisá Tailscale, y que en Plataforma CICD_BIND sea su IP del tailnet")
        return False

    casas = (salud.get("equipos", {}).get(cfg["AG_EQUIPO"], {}) or {}).get("casas", [])
    ok(f"el CD responde · casas del equipo {cfg['AG_EQUIPO']}: {' '.join(casas) or 'ninguna'}")

    if casas and cfg["AG_CASA"] not in casas:
        mal(f"'{cfg['AG_CASA']}' no está en la lista de casas del CD")
        nota(f"en Plataforma: CICD_NODOS_{cfg['AG_EQUIPO'].upper()} tiene que incluirla, y hay que reiniciar el CD")
        return False
    if casas:
        ok(f"'{cfg['AG_CASA']}' figura en el CD")

    objetivo = (f"{cfg['AG_CD']}/deploy/{cfg['AG_EQUIPO']}/objetivo"
                f"?casa={cfg['AG_CASA']}&generacion=0")
    cabeceras = {"X-Casa-Token": cfg["AG_TOKEN"]} if cfg.get("AG_TOKEN") else {}
    try:
        urllib.request.urlopen(urllib.request.Request(objetivo, headers=cabeceras), timeout=5)
        ok("el token sirve" if cabeceras else "el CD no pide token")
    except urllib.error.HTTPError as e:
        if e.code == 403:
            mal("el CD rechazó el token de esta casa")
            nota("pedíselo de nuevo a Plataforma: es el de CICD_TOKENS para tu casa")
            return False
        mal(f"el CD contestó HTTP {e.code}")
        return False
    except (urllib.error.URLError, OSError, socket.timeout):
        # Se quedó colgado: el long-poll aceptó el pedido, o sea que el token pasó.
        ok("el token sirve" if cabeceras else "el CD no pide token")
    return True


def probar_tcp(host, puerto, que):
    try:
        with socket.create_connection((host, int(puerto)), timeout=6):
            ok(f"{que} alcanzable en {host}:{puerto}")
            return True
    except OSError as e:
        mal(f"{que} no responde en {host}:{puerto} ({e})")
        return False


def probar_redis(url):
    partes = urllib.parse.urlparse(url)
    return probar_tcp(partes.hostname, partes.port or 6379, "redis")


def probar_registry(registry):
    host, _, puerto = registry.partition(":")
    if not probar_tcp(host, puerto or 5000, "registry"):
        return False
    if not registry_inseguro_configurado(registry):
        aviso(f"{registry} no figura en insecure-registries: el docker pull va a fallar")
        nota("sudo mkdir -p /etc/docker && echo '{\"insecure-registries\": [\"%s\"]}' "
             "| sudo tee /etc/docker/daemon.json && sudo systemctl restart docker" % registry)
        return False
    ok("el registry está declarado como inseguro (sin TLS; el cifrado lo pone Tailscale)")
    return True


def puertos_asignados(cfg):
    """Los puertos que el CD le asignó a esta casa, o los de por defecto.

    Se le preguntan al CD en vez de suponerlos: si Plataforma declaró PUERTOS_CASA
    para esta casa, comprobar el 8080 sería mirar el puerto equivocado.
    """
    try:
        with urllib.request.urlopen(f"{cfg['AG_CD']}/health", timeout=8) as r:
            salud = json.loads(r.read())
        mios = salud["equipos"][cfg["AG_EQUIPO"]]["puertos"][cfg["AG_CASA"]]
        return [int(mios["blue"]), int(mios["green"])]
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError):
        return [8080, 8081]


def probar_puertos(cfg):
    """Que los puertos de la réplica estén libres.

    Si están ocupados, el `docker run` del agente falla con 'port is already
    allocated' en medio de un deploy — y ese deploy aborta para todas las casas,
    no sólo para ésta. Mejor enterarse ahora.
    """
    ocupados = []
    puertos = puertos_asignados(cfg)
    for puerto in puertos:
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("0.0.0.0", puerto))
            except OSError:
                ocupados.append(puerto)
    if ocupados:
        aviso(f"puerto(s) {', '.join(map(str, ocupados))} ocupado(s) en esta máquina")
        nota("son los que el CD le asignó a esta casa para blue/green. Decile a Plataforma")
        nota(f"que agregue  PUERTOS_CASA=\"... {cfg['AG_CASA']}=<blue>:<green>\"  al CD, con dos libres")
        return False
    ok(f"los puertos {puertos[0]} y {puertos[1]} de la réplica están libres")
    return True


def comprobar_todo(cfg):
    titulo("Comprobaciones")
    resultados = [probar_docker(), probar_cd(cfg), probar_redis(cfg["TP_REDIS_URL"]),
                  probar_registry(cfg["AG_REGISTRY"]), probar_puertos(cfg)]
    return all(resultados)


# ------------------------------------------------------------------ archivos
def leer_env(ruta):
    datos = {}
    try:
        with open(ruta, encoding="utf-8") as f:
            for linea in f:
                linea = linea.strip()
                if linea and not linea.startswith("#") and "=" in linea:
                    clave, _, valor = linea.partition("=")
                    datos[clave.strip()] = valor.strip()
    except OSError:
        pass
    return datos


def escribir_env(ruta, datos, cabecera):
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    with open(ruta, "w", encoding="utf-8") as f:
        f.write(f"# {cabecera}\n# Generado por configurar.py. No se versiona.\n")
        for clave, valor in datos.items():
            f.write(f"{clave}={valor}\n")
    os.chmod(ruta, 0o600)  # lleva el token y la clave de Redis


def cargar(carpeta=CARPETA_POR_DEFECTO):
    cfg = leer_env(os.path.join(carpeta, ARCHIVO_AGENTE))
    cfg.update(leer_env(os.path.join(carpeta, ARCHIVO_APP)))
    return cfg


def guardar(cfg):
    carpeta = cfg["AG_DIR_HOST"]
    for color in ("blue", "green"):
        # Creados por el usuario y no por Docker: si los crea Docker quedan de
        # root y la réplica no puede escribir su bitácora.
        os.makedirs(os.path.join(carpeta, "logs", color), exist_ok=True)

    # Lo que va al contenedor del agente. AG_DIR es la ruta de adentro; AG_DIR_HOST
    # la de afuera, porque el bind mount de los logs lo resuelve el daemon.
    escribir_env(os.path.join(carpeta, ARCHIVO_AGENTE), {
        "AG_CASA": cfg["AG_CASA"], "AG_EQUIPO": cfg["AG_EQUIPO"],
        "AG_CD": cfg["AG_CD"], "AG_TOKEN": cfg.get("AG_TOKEN", ""),
        "AG_REGISTRY": cfg["AG_REGISTRY"],
        "AG_DIR": "/casa", "AG_DIR_HOST": carpeta,
    }, "Configuración del agente de esta casa")

    # El de la réplica: el agente lo monta pero nunca lo toca.
    escribir_env(os.path.join(carpeta, ARCHIVO_APP), {"TP_REDIS_URL": cfg["TP_REDIS_URL"]},
                 "Configuración de la réplica (la lee la app, no el agente)")
    return carpeta


# ------------------------------------------------------------------ levantar
def orden_docker(cfg):
    return ["docker", "run", "-d", "--name", "sdypp-agente", "--restart", "unless-stopped",
            "--network", "host", "--group-add", grupo_socket_docker(),
            "-v", f"{SOCKET_DOCKER}:{SOCKET_DOCKER}",
            "-v", f"{cfg['AG_DIR_HOST']}:/casa",
            "--env-file", os.path.join(cfg["AG_DIR_HOST"], ARCHIVO_AGENTE),
            "sdypp-agente:local"]


def levantar(cfg):
    titulo("Levantando el agente")
    aqui = os.path.dirname(os.path.abspath(__file__))
    print("  construyendo la imagen...")
    construccion = subprocess.run(["docker", "build", "-t", "sdypp-agente:local", aqui],
                                  capture_output=True, text=True)
    if construccion.returncode != 0:
        mal("falló el build")
        print(construccion.stderr[-1500:])
        return False
    ok("imagen sdypp-agente:local")

    subprocess.run(["docker", "rm", "-f", "sdypp-agente"], capture_output=True)
    arranque = subprocess.run(orden_docker(cfg), capture_output=True, text=True)
    if arranque.returncode != 0:
        mal("no arrancó")
        print(arranque.stderr[-1500:])
        return False
    ok("sdypp-agente corriendo")
    print()
    nota("docker logs -f sdypp-agente        para verlo trabajar")
    nota(f"tail -f {os.path.join(cfg['AG_DIR_HOST'], 'logs', 'agente.log')}")
    print()
    print("  La réplica todavía no existe: la levanta el agente en el próximo deploy.")
    return True


# ------------------------------------------------------------------ asistente
def asistente():
    previo = cargar()
    mia = ip_tailscale()

    titulo("Configuración del agente de casa")
    print("  Esto configura la máquina para que reciba despliegues del CD.")
    print("  No hay que abrir ningún puerto: el agente sólo hace conexiones salientes.")
    if mia:
        nota(f"tu IP en el tailnet: {mia}")
    if previo:
        nota("hay una configuración previa; ⏎ acepta cada valor")

    cfg = {}
    cfg["AG_CASA"] = preguntar(
        "Nombre de esta casa",
        "Tiene que coincidir, letra por letra, con lo que Plataforma puso en "
        "CICD_NODOS_PYTHON.\n  Es la clave del archivo de estado: si no coincide, el deploy "
        "aborta culpando\n  a otra máquina.",
        default=previo.get("AG_CASA", ""), validar=validar_casa)

    cfg["AG_EQUIPO"] = preguntar(
        "Equipo", "python o java. Decide a qué pipeline del CD le pregunta.",
        default=previo.get("AG_EQUIPO", "python"),
        validar=lambda v: None if v in ("python", "java") else "python o java")

    cd_previo = previo.get("AG_CD", "")
    cfg["AG_CD"] = preguntar(
        "Dirección del CD (Plataforma)",
        f"Con el puerto de los agentes ({PUERTO_CD}). Ej: http://100.101.15.93:{PUERTO_CD}",
        default=cd_previo or f"http://100.101.15.93:{PUERTO_CD}", validar=validar_url_cd)

    # Se le pregunta al CD si autentica, en vez de hacer tipear un token que no
    # va a usar. Si no contesta, se pregunta igual: mejor pedirlo de más que
    # dejar la casa sin poder hablar.
    pide = cd_pide_token(cfg["AG_CD"])
    if pide is False:
        cfg["AG_TOKEN"] = ""
        print()
        nota("este CD no usa tokens: no hace falta ninguno")
    else:
        if pide is None:
            print()
            aviso("no pude preguntarle al CD si usa tokens; lo pregunto por las dudas")
        cfg["AG_TOKEN"] = preguntar(
            "Token de esta casa",
            "Te lo pasó Plataforma por privado. Si el CD no usa tokens, dejalo vacío.",
            default=previo.get("AG_TOKEN", ""), obligatorio=False, secreto=True)

    cfg["TP_REDIS_URL"] = preguntar(
        "URL de Redis",
        "La que va al .env de la réplica. Ej: redis://:<clave>@100.91.228.65:6379/0",
        default=previo.get("TP_REDIS_URL", ""), validar=validar_redis)

    cfg["AG_REGISTRY"] = preguntar(
        "Registry", "De dónde baja las imágenes. El agente rechaza cualquier otra.",
        default=previo.get("AG_REGISTRY", REGISTRY_POR_DEFECTO), validar=validar_registry)

    cfg["AG_DIR_HOST"] = preguntar(
        "Carpeta de esta casa", "Ahí van el .env, la configuración del agente y los logs.",
        default=previo.get("AG_DIR_HOST", CARPETA_POR_DEFECTO))

    carpeta = guardar(cfg)
    titulo("Guardado")
    ok(f"{os.path.join(carpeta, ARCHIVO_AGENTE)}  (600)")
    ok(f"{os.path.join(carpeta, ARCHIVO_APP)}  (600)")
    ok(f"{os.path.join(carpeta, 'logs')}/blue · green")

    if not comprobar_todo(cfg):
        print()
        aviso("Hay cosas que no dieron. La configuración quedó guardada:")
        nota("arreglá lo de arriba y corré  python3 configurar.py --levantar")
        return 1

    print()
    respuesta = input(pintar("  ¿Levanto el agente ahora? [S/n] ", "1")).strip().lower()
    if respuesta in ("", "s", "si", "sí", "y"):
        return 0 if levantar(cfg) else 1
    print()
    nota("cuando quieras:  python3 configurar.py --levantar")
    return 0


def mostrar():
    cfg = cargar()
    if not cfg:
        mal("no hay configuración guardada. Corré  python3 configurar.py")
        return 1
    titulo("Configuración guardada")
    for clave in ("AG_CASA", "AG_EQUIPO", "AG_CD", "AG_REGISTRY", "AG_DIR_HOST"):
        print(f"  {clave:<14} {cfg.get(clave, '-')}")
    print(f"  {'AG_TOKEN':<14} {'(puesto)' if cfg.get('AG_TOKEN') else '(vacío)'}")
    print(f"  {'TP_REDIS_URL':<14} {re.sub(r'://[^@]*@', '://···@', cfg.get('TP_REDIS_URL', '-'))}")
    print()
    nota("el docker run equivalente:")
    print("  " + " ".join(orden_docker(cfg)))
    return 0


def main():
    modo = sys.argv[1] if len(sys.argv) > 1 else ""
    if modo in ("-h", "--help"):
        print(__doc__)
        return 0
    if modo == "--mostrar":
        return mostrar()
    cfg = cargar()
    if modo in ("--levantar", "--probar"):
        if not cfg.get("AG_CASA"):
            mal("no hay configuración guardada. Corré  python3 configurar.py")
            return 1
        cfg.setdefault("AG_DIR_HOST", CARPETA_POR_DEFECTO)
        if not comprobar_todo(cfg):
            return 1
        if modo == "--probar":
            return 0
        return 0 if levantar(cfg) else 1
    if modo:
        mal(f"no conozco la opción {modo}")
        print(__doc__)
        return 1
    return asistente()


if __name__ == "__main__":
    sys.exit(main())
