#!/usr/bin/env python3
"""Consola de Plataforma: configura, levanta y opera el CD.

El `docker run` del CD tiene doce variables y una lista de casas adentro de un
string. Tipearlo a mano es la forma más rápida de romper un despliegue, así que
acá se configura una vez, se guarda en `plataforma.env` —que es literalmente el
`--env-file` del contenedor— y después se opera por menú.

    python3 consola.py

Sólo biblioteca estándar. No pide sudo.
"""
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

RAIZ = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(RAIZ, "plataforma.env")
IMAGEN = "sdypp-cd:local"
CONTENEDOR = "sdypp-cd"
BITACORA = os.path.join(RAIZ, "logs", "cicd.log")
EQUIPOS = ("python", "java")

COLOR = sys.stdout.isatty() and os.environ.get("TERM") not in (None, "dumb")


def pintar(t, c):
    return f"\033[{c}m{t}\033[0m" if COLOR else t


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


def consola_vecina():
    """La consola del balanceador, si el repo está al lado. Ver el equivalente
    en `sdypp_balanceador/consola.py`: Plataforma corre los dos."""
    ruta = os.path.join(os.path.dirname(RAIZ), "sdypp_balanceador", "consola.py")
    return ruta if os.path.isfile(ruta) else None


def abrir_vecina():
    ruta = consola_vecina()
    if not ruta:
        mal("no encuentro el repo `sdypp_balanceador` al lado de este")
        return
    print()
    nota(f"abriendo {ruta} — al salir volvés acá")
    try:
        subprocess.run([sys.executable, ruta], check=False)
    except (OSError, KeyboardInterrupt):
        pass


def pausa():
    try:
        input(pintar("\n  ⏎ para volver al menú ", "90"))
    except (EOFError, KeyboardInterrupt):
        pass


# ------------------------------------------------------------------ configuración
DEFAULTS = {
    "CASA": "casa-tomas",
    "CICD_BIND": "127.0.0.1",
    "BALANCEADORES": "http://127.0.0.1:8081",
    "REGISTRY": "100.91.228.65:5000",
    "CICD_PUERTO_AGENTES": "8082",
    "CICD_PUERTO_DISPARO": "8083",
    "CICD_PUERTO_SSH": "2222",
    "CICD_ESPERA_BARRERA": "180",
    "CICD_ESPERA_LONGPOLL": "30",
    "CASAS": "",
    "CICD_NODOS_PYTHON": "",
    "CICD_NODOS_JAVA": "",
    "PUERTOS_CASA": "",
    "CICD_TOKENS": "",
}


def leer_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG, encoding="utf-8") as f:
            for linea in f:
                linea = linea.rstrip("\n")
                if linea and not linea.startswith("#") and "=" in linea:
                    clave, _, valor = linea.partition("=")
                    cfg[clave.strip()] = valor
    except OSError:
        pass
    return cfg


def guardar_config(cfg):
    with open(CONFIG, "w", encoding="utf-8") as f:
        f.write("# Configuración del CD de Plataforma.\n"
                "# Es el --env-file del contenedor. Generado por consola.py; no se versiona.\n")
        for clave in DEFAULTS:
            f.write(f"{clave}={cfg.get(clave, '')}\n")
    os.chmod(CONFIG, 0o600)


def hay_config():
    return os.path.exists(CONFIG) and leer_config().get("CICD_NODOS_PYTHON", "") != ""


# ------------------------------------------------------------------ utilidades
def correr(*orden, timeout=20):
    try:
        r = subprocess.run(orden, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def ip_tailscale():
    salida = correr("tailscale", "ip", "-4")
    if salida:
        return salida.splitlines()[0].strip()
    for linea in correr("ip", "-4", "-o", "addr", "show", "tailscale0").split("\n"):
        hallado = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", linea)
        if hallado:
            return hallado.group(1)
    return ""


def pedir(etiqueta, ayuda=None, default="", obligatorio=True, validar=None):
    while True:
        print()
        print(pintar(etiqueta, "1"))
        if ayuda:
            nota(ayuda)
        if default:
            nota(f"[{default}]  ⏎ para aceptar")
        try:
            leido = input("  > ").strip()
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


def pedir_si(pregunta, por_defecto=True):
    sufijo = "[S/n]" if por_defecto else "[s/N]"
    try:
        r = input(pintar(f"  {pregunta} {sufijo} ", "1")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    if not r:
        return por_defecto
    return r in ("s", "si", "sí", "y", "yes")


def url_disparo(cfg):
    return f"http://127.0.0.1:{cfg['CICD_PUERTO_DISPARO']}"


def url_agentes(cfg):
    """El socket que ven las casas. Va por CICD_BIND y no por 127.0.0.1: el CD
    bindea ahí, así que preguntarle por loopback lo da por caído estando sano."""
    return f"http://{cfg.get('CICD_BIND') or '127.0.0.1'}:{cfg['CICD_PUERTO_AGENTES']}"


def pedir_json(url, metodo="GET", cuerpo=None, timeout=15):
    pedido = urllib.request.Request(url, data=cuerpo, method=metodo,
                                    headers={"Content-Type": "application/json"} if cuerpo else {})
    with urllib.request.urlopen(pedido, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


# ------------------------------------------------------------------ estado del CD
def cd_corriendo():
    return correr("docker", "inspect", "-f", "{{.State.Running}}", CONTENEDOR) == "true"


def salud_cd(cfg):
    try:
        return pedir_json(f"{url_agentes(cfg)}/health", timeout=6)
    except (urllib.error.URLError, OSError, ValueError):
        return None


def salud_balanceador(cfg):
    base = cfg["BALANCEADORES"].split()[0]
    try:
        return pedir_json(f"{base}/admin/backends", timeout=6)
    except (urllib.error.URLError, OSError, ValueError):
        return None


def encabezado(cfg):
    salud = salud_cd(cfg)
    if salud:
        generaciones = " ".join(f"{e}={salud['equipos'][e]['generacion']}" for e in EQUIPOS)
        casas = len(salud["equipos"]["python"]["casas"]) + len(salud["equipos"]["java"]["casas"])
        estado = pintar(f"CD arriba · generaciones {generaciones} · {casas} casas", "32")
    elif cd_corriendo():
        estado = pintar("CD arrancando o sin responder", "33")
    else:
        estado = pintar("CD abajo", "31")

    bal = salud_balanceador(cfg)
    if bal is None:
        estado_bal = pintar("balanceador sin responder", "31")
        if consola_vecina():
            estado_bal += pintar("  → opción b para levantarlo", "90")
    else:
        sanos = sum(1 for b in bal["backends"] if b.get("sano"))
        color = "32" if sanos and sanos == len(bal["backends"]) else "33"
        estado_bal = pintar(f"balanceador {sanos}/{len(bal['backends'])} sanas", color)

    print()
    print(pintar(f"  CD · Plataforma · {cfg['CASA']}", "1"))
    print(f"  {estado}  ·  {estado_bal}")

    for equipo in EQUIPOS:
        curso = deploy_en_curso(cfg, equipo)
        if curso:
            esperando = " ".join(curso.get("esperando") or []) or "—"
            print(pintar(f"  ⏳ deploy de {equipo} v{curso['version']} en curso hace "
                         f"{curso['segundos']} s · esperando a: {esperando}", "33"))

    faltan = []
    if not any(os.path.exists(ruta_publica(e)) for e in EQUIPOS):
        faltan.append("clave de publicación (opción 6) — sin esto nadie puede correr publicar.sh")
    elif not manifiestos("python") and not manifiestos("java"):
        faltan.append("publicar una versión — todavía no hay ninguna imagen que desplegar")
    for linea in faltan:
        print(pintar(f"  ! falta: {linea}", "33"))


# ------------------------------------------------------------------ casas
def casas_de(cfg, equipo):
    return cfg.get(f"CICD_NODOS_{equipo.upper()}", "").split()


def ip_de(cfg, casa):
    for par in cfg["CASAS"].split():
        nombre, _, resto = par.partition("=")
        if nombre == casa:
            return resto.split("@")[-1]
    return "?"


def puertos_de(cfg, casa):
    for par in cfg["PUERTOS_CASA"].split():
        nombre, _, resto = par.partition("=")
        if nombre == casa and ":" in resto:
            return resto
    return "8080:8081"


def agregar_casa(cfg):
    titulo("Agregar una casa")
    nota("La casa tiene que estar en el tailnet antes: necesitás su IP.")

    nombre = pedir("Nombre de la casa",
                   "Es la clave de todo: el agente tiene que usar exactamente este nombre.",
                   validar=lambda v: None if re.fullmatch(r"[a-z0-9][a-z0-9-]{1,30}", v)
                   else "minúsculas, números y guiones (ej: casa-juan)")
    if nombre in casas_de(cfg, "python") + casas_de(cfg, "java"):
        mal(f"'{nombre}' ya existe")
        return cfg

    equipo = pedir("Equipo", "python o java", default="python",
                   validar=lambda v: None if v in EQUIPOS else "python o java")
    ip = pedir("IP de Tailscale de esa máquina", "La que devuelve `tailscale ip -4` allá.",
               validar=lambda v: None if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", v) else "una IPv4")
    usuario = pedir("Usuario de esa máquina",
                    "Ya no se usa para nada (no hay SSH), pero el formato lo pide.",
                    default="sdypp")

    puertos = ""
    if not pedir_si("¿Tiene libres el 8080 y el 8081 para la réplica?"):
        puertos = pedir("Puertos blue:green a usar", "Dos puertos libres. Ej: 8090:8091",
                        default="8090:8091",
                        validar=lambda v: None if re.fullmatch(r"\d+:\d+", v) else "formato blue:green")

    cfg["CASAS"] = " ".join(filter(None, [cfg["CASAS"], f"{nombre}={usuario}@{ip}"]))
    clave = f"CICD_NODOS_{equipo.upper()}"
    cfg[clave] = " ".join(filter(None, [cfg[clave], nombre]))
    if puertos:
        cfg["PUERTOS_CASA"] = " ".join(filter(None, [cfg["PUERTOS_CASA"], f"{nombre}={puertos}"]))
    guardar_config(cfg)

    ok(f"'{nombre}' agregada")
    print()
    nota("Lo que le tenés que pasar a esa máquina:")
    print(f"    nombre de casa : {nombre}")
    print(f"    CD             : http://{cfg['CICD_BIND']}:{cfg['CICD_PUERTO_AGENTES']}")
    print( "    URL de Redis   : redis://:<clave>@<ip de Datos>:6379/0")
    print()
    nota("Y ahí: git clone del repo cd, y `python3 agente/configurar.py`")
    print()
    if pedir_si("¿Reinicio el CD ahora para que la registre?"):
        levantar_cd(cfg)
        print()
        nota("Falta un deploy para que la casa levante su réplica (opción 1 del menú).")
    return cfg


def quitar_casa(cfg):
    titulo("Quitar una casa")
    todas = [(c, "python") for c in casas_de(cfg, "python")] + \
            [(c, "java") for c in casas_de(cfg, "java")]
    if not todas:
        aviso("no hay casas")
        return cfg
    for i, (casa, equipo) in enumerate(todas, 1):
        print(f"  {i}  {casa}  ({equipo})")
    try:
        elegida = int(input("\n  ¿Cuál? [0 cancela] > ").strip() or "0")
    except (ValueError, EOFError, KeyboardInterrupt):
        return cfg
    if not 1 <= elegida <= len(todas):
        return cfg
    casa, equipo = todas[elegida - 1]

    nota(f"Su réplica va a quedar corriendo en {ip_de(cfg, casa)} pero fuera de la rotación")
    nota("en el próximo deploy. Al agente de esa máquina hay que bajarlo allá.")
    if not pedir_si(f"¿Quito '{casa}'?", por_defecto=False):
        return cfg

    clave = f"CICD_NODOS_{equipo.upper()}"
    cfg[clave] = " ".join(c for c in cfg[clave].split() if c != casa)
    cfg["CASAS"] = " ".join(p for p in cfg["CASAS"].split() if not p.startswith(f"{casa}="))
    cfg["PUERTOS_CASA"] = " ".join(p for p in cfg["PUERTOS_CASA"].split()
                                   if not p.startswith(f"{casa}="))
    cfg["CICD_TOKENS"] = " ".join(p for p in cfg["CICD_TOKENS"].split()
                                  if not p.startswith(f"{casa}="))
    guardar_config(cfg)
    ok(f"'{casa}' quitada")
    if pedir_si("¿Reinicio el CD?"):
        levantar_cd(cfg)
    return cfg


def menu_casas(cfg):
    while True:
        titulo("Casas")
        for equipo in EQUIPOS:
            casas = casas_de(cfg, equipo)
            if casas:
                print(f"\n  {equipo}:")
                for casa in casas:
                    print(f"    {casa:<18} {ip_de(cfg, casa):<16} puertos {puertos_de(cfg, casa)}")
        if not (casas_de(cfg, "python") or casas_de(cfg, "java")):
            nota("todavía no hay ninguna")
        print("""
  1  Agregar una casa
  2  Quitar una casa
  0  Volver""")
        try:
            opcion = input("\n  > ").strip()
        except (EOFError, KeyboardInterrupt):
            return cfg
        if opcion == "1":
            cfg = agregar_casa(cfg)
        elif opcion == "2":
            cfg = quitar_casa(cfg)
        elif opcion == "0":
            return cfg


# ------------------------------------------------------------------ el contenedor
def construir():
    print("  construyendo la imagen...")
    r = subprocess.run(["docker", "build", "-t", IMAGEN, RAIZ], capture_output=True, text=True)
    if r.returncode != 0:
        mal("falló el build")
        print(r.stderr[-1500:])
        return False
    ok(f"imagen {IMAGEN}")
    return True


def orden_docker():
    return ["docker", "run", "-d", "--name", CONTENEDOR, "--restart", "unless-stopped",
            "--network", "host", "--env-file", CONFIG,
            "-v", f"{RAIZ}/claves:/cicd/claves", "-v", f"{RAIZ}/python:/cicd/python",
            "-v", f"{RAIZ}/java:/cicd/java", "-v", f"{RAIZ}/logs:/cicd/logs",
            IMAGEN]


def levantar_cd(cfg):
    titulo("Levantando el CD")
    if not casas_de(cfg, "python") and not casas_de(cfg, "java"):
        mal("no hay ninguna casa configurada: agregá una primero (opción 5)")
        return False
    if cfg["CICD_BIND"] in ("0.0.0.0", ""):
        aviso("CICD_BIND en 0.0.0.0 expone el plano de control a la red física de tu casa")
        if not pedir_si("¿Seguir igual?", por_defecto=False):
            return False
    if not construir():
        return False

    for carpeta in ("claves", "python/entrante", "python/estado",
                    "java/entrante", "java/estado", "logs"):
        os.makedirs(os.path.join(RAIZ, carpeta), exist_ok=True)

    subprocess.run(["docker", "rm", "-f", CONTENEDOR], capture_output=True)
    r = subprocess.run(orden_docker(), capture_output=True, text=True)
    if r.returncode != 0:
        mal("no arrancó")
        print(r.stderr[-1500:])
        return False

    for _ in range(40):
        if salud_cd(cfg):
            ok(f"CD arriba · agentes en {cfg['CICD_BIND']}:{cfg['CICD_PUERTO_AGENTES']}")
            return True
        time.sleep(0.5)
    mal("arrancó pero no responde. Últimas líneas:")
    print(correr("docker", "logs", "--tail", "15", CONTENEDOR))
    return False


def menu_contenedor(cfg):
    titulo("El contenedor del CD")
    print(f"  estado: {'corriendo' if cd_corriendo() else 'parado o inexistente'}")
    print("""
  1  Levantar / reiniciar
  2  Bajar
  3  Ver los logs del contenedor
  0  Volver""")
    try:
        opcion = input("\n  > ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if opcion == "1":
        levantar_cd(cfg)
        pausa()
    elif opcion == "2":
        if pedir_si("¿Bajo el CD? Las réplicas y el balanceador siguen sirviendo.", False):
            subprocess.run(["docker", "rm", "-f", CONTENEDOR], capture_output=True)
            ok("bajado")
            pausa()
    elif opcion == "3":
        print()
        print(correr("docker", "logs", "--tail", "40", CONTENEDOR))
        pausa()


# ------------------------------------------------------------------ claves de publicación
def ruta_publica(equipo):
    return os.path.join(RAIZ, "claves", f"deploy-{equipo}.pub")


def generar_clave(cfg, equipo):
    """Un par para que un dev pueda correr publicar.sh.

    La pública queda en claves/ y el arranque del CD la instala como
    authorized_keys del usuario deploy-<equipo>; la privada se guarda aparte para
    que se la lleve el dev. Es el único secreto del sistema: quien la tenga puede
    disparar despliegues.
    """
    destino = os.path.join(RAIZ, "claves")
    os.makedirs(destino, exist_ok=True)
    privada = os.path.join(destino, f"id_deploy_{equipo}")

    if os.path.exists(privada) or os.path.exists(ruta_publica(equipo)):
        aviso(f"ya hay una clave para {equipo}")
        nota("generar otra invalida la anterior: quien la tenga deja de poder publicar")
        if not pedir_si("¿Generar una nueva igual?", por_defecto=False):
            return
        for ruta in (privada, privada + ".pub", ruta_publica(equipo)):
            try:
                os.remove(ruta)
            except OSError:
                pass

    r = subprocess.run(["ssh-keygen", "-t", "ed25519", "-f", privada, "-N", "",
                        "-C", f"deploy-{equipo}@sdypp", "-q"], capture_output=True, text=True)
    if r.returncode != 0:
        mal(f"ssh-keygen falló: {r.stderr.strip()[:200]}")
        return
    os.replace(privada + ".pub", ruta_publica(equipo))
    os.chmod(privada, 0o600)
    ok(f"par generado para {equipo}")

    print()
    nota("La PÚBLICA ya quedó en el CD:")
    print(f"    {ruta_publica(equipo)}")
    nota("La PRIVADA hay que pasársela al dev por un canal privado, y borrar esta copia:")
    print(pintar(f"    {privada}", "33"))
    print()
    aviso("quien tenga esa clave puede disparar un deploy: no va al repo ni a un chat de grupo")
    print()
    if pedir_si("¿Reinicio el CD para que instale la clave nueva?"):
        levantar_cd(cfg)


def instrucciones_dev(cfg, equipo):
    titulo(f"Lo que necesita el dev de {equipo}")
    print(f"""
  1. Guardar la clave privada en ~/.ssh/id_deploy  (chmod 600)

  2. Agregar a ~/.ssh/config:

       Host cd
           HostName {cfg['CICD_BIND']}
           Port {cfg['CICD_PUERTO_SSH']}
           User deploy-{equipo}
           IdentityFile ~/.ssh/id_deploy

  3. Declarar el registry como inseguro en /etc/docker/daemon.json:

       {{"insecure-registries": ["{cfg['REGISTRY']}"]}}

  4. Y desde el repo de la app:

       ./deploy/publicar.sh""")
    print()
    nota("publicar.sh construye, prueba, hace docker push y deja el manifiesto acá.")
    nota("De ahí en adelante es todo del CD: se sigue desde la opción 1 o la 6.")


def menu_claves(cfg):
    while True:
        titulo("Claves de publicación")
        nota("Sin esto nadie puede correr publicar.sh, y sin publicar no hay imagen que desplegar.")
        print()
        for equipo in EQUIPOS:
            estado = "instalada" if os.path.exists(ruta_publica(equipo)) else "sin clave"
            marca = "✓" if os.path.exists(ruta_publica(equipo)) else "—"
            print(f"    {marca} {equipo:<8} {estado}")
        print("""
  1  Generar un par para python
  2  Generar un par para java
  3  Ver lo que necesita el dev
  0  Volver""")
        try:
            opcion = input("\n  > ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if opcion == "1":
            generar_clave(cfg, "python")
            pausa()
        elif opcion == "2":
            generar_clave(cfg, "java")
            pausa()
        elif opcion == "3":
            equipo = pedir("¿De qué equipo?", default="python",
                           validar=lambda v: None if v in EQUIPOS else "python o java")
            instrucciones_dev(cfg, equipo)
            pausa()
        elif opcion == "0":
            return


# ------------------------------------------------------------------ opciones del menú
def manifiestos(equipo):
    carpeta = os.path.join(RAIZ, equipo, "entrante", "historial")
    try:
        archivos = [os.path.join(carpeta, f) for f in os.listdir(carpeta) if f.endswith(".json")]
    except OSError:
        return []
    return sorted(archivos, key=os.path.getmtime, reverse=True)


def desplegar(cfg):
    titulo("Desplegar")
    nota("Lo normal es que el deploy lo dispare `publicar.sh` desde la máquina de un dev.")
    nota("Esto es para redesplegar algo ya publicado: sumar una casa nueva, o reintentar.")

    equipo = "python"
    if casas_de(cfg, "java"):
        equipo = pedir("Equipo", default="python",
                       validar=lambda v: None if v in EQUIPOS else "python o java")

    curso = deploy_en_curso(cfg, equipo)
    if curso:
        esperando = " ".join(curso.get("esperando") or []) or "—"
        aviso(f"ya hay un deploy de la v{curso['version']} corriendo hace {curso['segundos']} s")
        nota(f"esperando a: {esperando}")
        nota("hasta que termine no se puede lanzar otro. Se cancela desde la opción 4.")
        return

    archivos = manifiestos(equipo)
    if not archivos:
        mal(f"no hay manifiestos en {equipo}/entrante/historial/")
        nota("hace falta que alguien haya corrido publicar.sh al menos una vez")
        if not os.path.exists(ruta_publica(equipo)):
            nota(f"y todavía no hay clave para {equipo}: generala en la opción 6")
        return
    print()
    for i, ruta in enumerate(archivos[:8], 1):
        try:
            m = json.load(open(ruta, encoding="utf-8"))
            print(f"  {i}  v{m.get('version'):<4} {m.get('imagen', '')}")
            nota(f"     publicado por {m.get('publicado_por', '?')} el {m.get('publicado_en', '?')}")
        except (OSError, ValueError):
            print(f"  {i}  {os.path.basename(ruta)} (ilegible)")
    try:
        elegido = int(input("\n  ¿Cuál? [0 cancela] > ").strip() or "0")
    except (ValueError, EOFError, KeyboardInterrupt):
        return
    if not 1 <= elegido <= min(8, len(archivos)):
        return
    ruta = archivos[elegido - 1]

    print()
    print(f"  Casas que participan: {' '.join(casas_de(cfg, equipo))}")
    if not pedir_si("¿Despliego?"):
        return

    with open(ruta, "rb") as f:
        cuerpo = f.read()

    resultado = {}

    def lanzar():
        try:
            resultado.update(pedir_json(f"{url_disparo(cfg)}/deploy/{equipo}/desplegar",
                                        "POST", cuerpo,
                                        timeout=int(cfg["CICD_ESPERA_BARRERA"]) + 120))
        except urllib.error.HTTPError as e:
            try:
                resultado.update(json.loads(e.read()))
            except (ValueError, OSError):
                resultado.update({"ok": False, "detalle": f"HTTP {e.code}"})
        except (urllib.error.URLError, OSError, ValueError) as e:
            resultado.update({"ok": False, "detalle": str(e)})

    print()
    hilo = threading.Thread(target=lanzar, daemon=True)
    hilo.start()
    seguir_bitacora(hilo)

    print()
    if resultado.get("ok"):
        ok(f"Desplegado · {resultado.get('detalle', '')}")
    else:
        mal(f"Falló · {resultado.get('detalle', 'sin detalle')}")
        nota("las versiones viejas siguen sirviendo")


def seguir_bitacora(hilo, limite=600):
    """Muestra lo que se va escribiendo mientras el hilo trabaja.

    Al final se drena lo que quedó sin leer: un deploy donde todas las casas
    contestan "ya coincide" termina en menos de un segundo, y si sólo se mirara
    mientras el hilo vive no se vería una sola línea de lo que pasó.
    """
    def mostrar(linea):
        partes = linea.strip().split(" | ")
        if len(partes) >= 4:
            color = {"OK": "32", "FALLO": "31", "RECHAZADO": "31"}.get(partes[3], "90")
            print(pintar(f"    {' | '.join(partes[2:])}", color))

    try:
        with open(BITACORA, encoding="utf-8") as f:
            f.seek(0, os.SEEK_END)
            fin = time.monotonic() + limite
            while hilo.is_alive() and time.monotonic() < fin:
                linea = f.readline()
                if linea:
                    mostrar(linea)
                else:
                    time.sleep(0.2)
            hilo.join(timeout=30)
            time.sleep(0.3)   # el CD escribe la última línea justo antes de contestar
            for linea in f:
                mostrar(linea)
    except OSError:
        print("    desplegando...")
        hilo.join(timeout=limite)


def deploy_en_curso(cfg, equipo):
    try:
        return pedir_json(f"{url_disparo(cfg)}/deploy/{equipo}/estado", timeout=6).get("enCurso")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def cancelar_deploy(cfg, equipo):
    """Cierra la barrera ya, en vez de esperar el timeout completo.

    Sirve cuando ya se sabe que una casa no va a reportar —su agente está caído—
    y no tiene sentido esperar los minutos que faltan: el deploy aborta por el
    camino de siempre, baja el color nuevo y no toca el balanceador.
    """
    curso = deploy_en_curso(cfg, equipo)
    if not curso:
        aviso(f"no hay ningún deploy de {equipo} en curso")
        return
    esperando = " ".join(curso.get("esperando") or []) or "—"
    print()
    print(f"  deploy de la v{curso['version']}, generación {curso['generacion']}, "
          f"hace {curso['segundos']} s")
    print(f"  esperando a: {esperando}")
    print()
    nota("Cancelar lo hace abortar: las casas que ya levantaron el color nuevo lo bajan,")
    nota("el balanceador no se toca y las versiones viejas siguen sirviendo.")
    if not pedir_si("¿Cancelo?", por_defecto=False):
        return
    try:
        r = pedir_json(f"{url_disparo(cfg)}/deploy/{equipo}/cancelar", "POST", b"{}", timeout=20)
        (ok if r.get("ok") else mal)(r.get("detalle", ""))
    except (urllib.error.URLError, OSError, ValueError) as e:
        mal(str(e))


def ver_nodos(cfg):
    titulo("Nodos conocidos")
    for equipo in EQUIPOS:
        casas = casas_de(cfg, equipo)
        if not casas:
            continue
        try:
            estado = pedir_json(f"{url_disparo(cfg)}/deploy/{equipo}/estado")
        except (urllib.error.URLError, OSError, ValueError) as e:
            mal(f"el CD no contesta: {e}")
            return
        print(f"\n  {equipo} · objetivo publicado: generación {estado['objetivo']['generacion']}")
        print(pintar(f"    {'casa':<16}{'ip':<17}{'color':<8}{'ver':<6}{'agente':<14}último reporte", "90"))
        agentes = estado.get("agentes", {})
        tope = float(estado.get("esperaLongPoll", 30)) * 2 + 10
        for casa in casas:
            e = estado["estadoPorCasa"].get(casa, {})
            reporte = estado["ultimosReportes"].get(casa, {})
            cual = reporte.get("estado", "—")
            marca = {"listo": "32", "fallo": "31"}.get(cual, "90")
            visto = agentes.get(casa)
            if visto is None:
                agente = pintar(f"{'sin contacto':<14}", "31")
            elif visto <= tope:
                agente = pintar(f"{'hace ' + str(int(visto)) + ' s':<14}", "32")
            else:
                agente = pintar(f"{'hace ' + str(int(visto)) + ' s':<14}", "33")
            print(f"    {casa:<16}{ip_de(cfg, casa):<17}"
                  f"{e.get('COLOR_ACTIVO', '—'):<8}{e.get('VERSION_ACTIVA', '—'):<6}"
                  + agente + pintar(cual, marca))
        mudas = [c for c in casas if c not in agentes]
        if mudas:
            print()
            aviso(f"sin agente conectado: {' '.join(mudas)}")
            nota("publicar ahora terminaría en un deploy abortado por falta de reporte")
        sin_estado = [c for c in casas if not estado["estadoPorCasa"].get(c, {}).get("COLOR_ACTIVO")]
        if sin_estado:
            print()
            nota(f"sin réplica todavía: {' '.join(sin_estado)} — les falta un deploy (opción 1)")


def ver_balanceador(cfg):
    titulo("Balanceador")
    datos = salud_balanceador(cfg)
    if datos is None:
        mal(f"no contesta en {cfg['BALANCEADORES'].split()[0]}")
        nota("el plano de control escucha en loopback: esta consola tiene que correr en Plataforma")
        return
    if not datos["backends"]:
        aviso("el pool está vacío: el balanceador responde 503 hasta que haya una réplica")
        return
    print(pintar(f"\n    {'destino':<24}{'app':<10}{'sano':<7}{'worker':<12}atendidos", "90"))
    for b in datos["backends"]:
        print(f"    {b['destino']:<24}{b.get('app', '?'):<10}"
              + pintar(f"{str(b.get('sano')):<7}", "32" if b.get("sano") else "31")
              + f"{b.get('worker', '—'):<12}{b.get('atendidos', '—')}")


def hacer_rollback(cfg):
    titulo("Rollback")
    equipo = "python"
    if casas_de(cfg, "java"):
        equipo = pedir("Equipo", default="python",
                       validar=lambda v: None if v in EQUIPOS else "python o java")
    try:
        estado = pedir_json(f"{url_disparo(cfg)}/deploy/{equipo}/estado")
    except (urllib.error.URLError, OSError, ValueError) as e:
        mal(f"el CD no contesta: {e}")
        return
    print()
    for casa in casas_de(cfg, equipo):
        e = estado["estadoPorCasa"].get(casa, {})
        print(f"  {casa:<18} ahora {e.get('COLOR_ACTIVO', '—')} v{e.get('VERSION_ACTIVA', '—')}"
              f"  →  volvería a {e.get('COLOR_ANTERIOR', '—')} v{e.get('VERSION_ANTERIOR', '—')}")
    print()
    nota("La versión anterior ya está corriendo: esto sólo cambia quién está en rotación.")
    if not pedir_si("¿Hago el rollback?", por_defecto=False):
        return
    try:
        r = pedir_json(f"{url_disparo(cfg)}/deploy/{equipo}/rollback", "POST", b"{}", timeout=120)
        (ok if r.get("ok") else mal)(r.get("detalle", ""))
    except urllib.error.HTTPError as e:
        try:
            mal(json.loads(e.read()).get("detalle", f"HTTP {e.code}"))
        except (ValueError, OSError):
            mal(f"HTTP {e.code}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        mal(str(e))


def menu_rollback(cfg):
    titulo("Rollback / cancelar")
    hay = [e for e in EQUIPOS if deploy_en_curso(cfg, e)]
    if hay:
        aviso(f"hay un deploy en curso: {' '.join(hay)}")
    print("""
  1  Rollback: volver a la versión anterior
  2  Cancelar el deploy en curso
  0  Volver""")
    try:
        opcion = input("\n  > ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if opcion == "1":
        hacer_rollback(cfg)
        pausa()
    elif opcion == "2":
        equipo = "python"
        if casas_de(cfg, "java"):
            equipo = pedir("¿Qué equipo?", default="python",
                           validar=lambda v: None if v in EQUIPOS else "python o java")
        cancelar_deploy(cfg, equipo)
        pausa()


def ver_bitacora():
    titulo("Bitácora")
    try:
        with open(BITACORA, encoding="utf-8") as f:
            lineas = f.readlines()[-30:]
    except OSError:
        aviso("todavía no hay bitácora")
        return
    for linea in lineas:
        partes = linea.strip().split(" | ")
        if len(partes) >= 4:
            color = {"OK": "32", "FALLO": "31", "RECHAZADO": "31"}.get(partes[3], "0")
            print(pintar(f"  {' | '.join([partes[0][11:19]] + partes[2:])}", color))
        else:
            print(f"  {linea.rstrip()}")


def ver_config(cfg):
    titulo("Configuración")
    for clave in DEFAULTS:
        valor = cfg.get(clave, "")
        if clave == "CICD_TOKENS" and valor:
            valor = f"({len(valor.split())} tokens)"
        print(f"  {clave:<22} {valor or '—'}")
    print()
    nota(f"archivo: {CONFIG}")
    nota("es el --env-file del contenedor; el docker run equivalente:")
    print("  " + " ".join(orden_docker()))
    print()
    if pedir_si("¿Reconfigurar lo básico?", por_defecto=False):
        return configurar(cfg)
    return cfg


# ------------------------------------------------------------------ asistente inicial
def configurar(cfg=None):
    cfg = cfg or leer_config()
    titulo("Configuración de Plataforma")
    print("  Plataforma es la máquina que corre el balanceador y el CD.")
    print("  Las casas se agregan después, desde el menú.")

    mia = ip_tailscale()
    cfg["CASA"] = pedir("Nombre de esta máquina", "Sale en la bitácora.",
                        default=cfg.get("CASA") or "casa-tomas")
    cfg["CICD_BIND"] = pedir(
        "IP en la que el CD escucha a los agentes",
        "Tiene que ser la IP de Tailscale, no 0.0.0.0: con --network host, bindear a\n"
        "  todas las interfaces expone el plano de control a la red física de tu casa.",
        default=cfg.get("CICD_BIND") if cfg.get("CICD_BIND") != "127.0.0.1" else (mia or "127.0.0.1"),
        validar=lambda v: None if re.fullmatch(r"[\d.]+", v) else "una IP")
    if cfg["CICD_BIND"] == "0.0.0.0":
        aviso("0.0.0.0 expone el plano de control a toda tu red, no sólo al tailnet")

    cfg["BALANCEADORES"] = pedir(
        "Plano de control del balanceador",
        "Separá con espacios si hubiera más de uno (Etapa 3).",
        default=cfg.get("BALANCEADORES") or "http://127.0.0.1:8081")
    cfg["REGISTRY"] = pedir(
        "Registry", "El CD rechaza cualquier manifiesto cuya imagen no salga de acá.",
        default=cfg.get("REGISTRY") or "100.91.228.65:5000",
        validar=lambda v: None if re.fullmatch(r"[\w.-]+(:\d+)?", v) else "<ip>:<puerto>")

    if pedir_si("¿Cambiar los puertos del CD (ssh 2222, agentes 8082, disparo 8083)?", False):
        cfg["CICD_PUERTO_SSH"] = pedir("Puerto del sshd (manifiestos)",
                                       default=cfg.get("CICD_PUERTO_SSH") or "2222")
        cfg["CICD_PUERTO_AGENTES"] = pedir("Puerto de los agentes",
                                           default=cfg.get("CICD_PUERTO_AGENTES") or "8082")
        cfg["CICD_PUERTO_DISPARO"] = pedir("Puerto de disparo (loopback)",
                                           default=cfg.get("CICD_PUERTO_DISPARO") or "8083")

    guardar_config(cfg)
    ok(f"guardado en {CONFIG}  (600)")

    if not casas_de(cfg, "python") and not casas_de(cfg, "java"):
        print()
        nota("Falta al menos una casa. Vamos con la primera.")
        cfg = agregar_casa(cfg)

    # La clave de publicación es el otro requisito para que el sistema sirva de
    # algo: sin ella nadie puede correr publicar.sh, y sin publicar no hay imagen
    # que desplegar. Se ofrece acá para no descubrirlo tres pasos después.
    if not any(os.path.exists(ruta_publica(e)) for e in EQUIPOS):
        print()
        nota("Falta la clave con la que un dev publica versiones.")
        if pedir_si("¿La genero ahora?"):
            equipo = pedir("¿Para qué equipo?", default="python",
                           validar=lambda v: None if v in EQUIPOS else "python o java")
            generar_clave(cfg, equipo)
    return cfg


# ------------------------------------------------------------------ menú
MENU = """
  1  Desplegar                redesplegar un manifiesto ya publicado
  2  Nodos conocidos          casas, color activo, versión y último reporte
  3  Balanceador              qué réplicas están en rotación
  4  Rollback / cancelar      volver a la anterior, o abortar el deploy en curso
  5  Casas                    agregar, quitar, ver
  6  Claves de publicación    para que un dev pueda correr publicar.sh
  7  Bitácora                 últimas 30 líneas
  8  Contenedor del CD        levantar, reiniciar, bajar, logs
  9  Configuración            ver y editar
  b  Consola del balanceador  el otro componente de Plataforma
  0  Salir"""


def main():
    if not hay_config():
        titulo("Primera vez")
        print("  No hay configuración todavía. Vamos a armarla.")
        cfg = configurar()
        print()
        if pedir_si("¿Levanto el CD ahora?"):
            levantar_cd(cfg)
    else:
        cfg = leer_config()

    while True:
        cfg = leer_config()
        encabezado(cfg)
        print(MENU)
        try:
            opcion = input("\n  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if opcion == "1":
            desplegar(cfg)
            pausa()
        elif opcion == "2":
            ver_nodos(cfg)
            pausa()
        elif opcion == "3":
            ver_balanceador(cfg)
            pausa()
        elif opcion == "4":
            menu_rollback(cfg)
        elif opcion == "5":
            menu_casas(cfg)
        elif opcion == "6":
            menu_claves(cfg)
        elif opcion == "7":
            ver_bitacora()
            pausa()
        elif opcion == "8":
            menu_contenedor(cfg)
        elif opcion == "9":
            ver_config(cfg)
        elif opcion in ("b", "B"):
            abrir_vecina()
        elif opcion == "0":
            return 0
        elif opcion:
            mal("no conozco esa opción")


if __name__ == "__main__":
    sys.exit(main())
