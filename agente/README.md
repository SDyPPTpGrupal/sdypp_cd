# `sdypp-agente` — el agente de casa

Lo único que una casa entrega al sistema de despliegue. Reemplaza a lo que antes eran
`sshd` escuchando, una clave ajena en tu `authorized_keys` y tu usuario en el grupo `docker`.

**No escucha en ningún puerto.** Todas las conexiones las inicia él: pregunta al CD si hay una
generación nueva y le reporta cuando terminó.

---

## 1. Qué hace

En loop, para siempre:

```
GET /deploy/<equipo>/objetivo?generacion=<la última que apliqué>   ← se cuelga hasta 30 s
    │
    ├── sin novedad ──────────────────► volver a preguntar
    ├── generación ≤ la aplicada ─────► ignorar (un objetivo viejo no puede revertirme)
    └── generación nueva
          │
          ├── ¿lo que corre ya coincide con lo que pide? ──► no hacer nada
          └── si no: docker pull @digest · docker run · esperar healthy
          │
          └── POST /deploy/<equipo>/reporte {generacion, estado, version, detalle}
```

No recibe órdenes: recibe una descripción de cómo tiene que verse la casa y la compara con lo que
hay. De ahí salen tres propiedades que un `ssh docker run` no tenía:

- **Es idempotente.** Aplicar dos veces el mismo objetivo no hace nada la segunda vez.
- **Retoma solo.** Si el agente se reinicia a mitad de un deploy, arranca pidiendo el objetivo
  vigente, converge y reporta. Nadie tiene que empujarlo.
- **Un color que no está en el objetivo es un color que no debe existir.** Por eso abortar un
  deploy es publicar el objetivo sin el color nuevo: no hace falta un comando de aborto.

**Si el CD no responde, el agente no toca nada.** No revierte, no limpia, no baja réplicas: la que
está sirviendo sigue sirviendo. Un agente que "ordena" cuando pierde contacto con el control plane
es cómo se caen los sistemas de verdad.

---

## 2. Qué necesita la casa

- Docker corriendo, y el usuario en el grupo `docker`.
- `~/sdypp/.env` con `TP_REDIS_URL=...`, permisos 600. Es del nodo, no del release: el agente lo
  monta pero nunca lo toca.
- `~/sdypp/logs/blue` y `~/sdypp/logs/green` creados **por el usuario** (si los crea Docker quedan
  de root y la réplica no puede escribir su bitácora).
- `/etc/docker/daemon.json` con `{"insecure-registries": ["100.91.228.65:5000"]}` y Docker
  reiniciado. "Insecure" = sin TLS: el cifrado lo pone Tailscale.
- Las reglas de `ufw` de siempre para `tailscale0`, que ya hacían falta para que el balanceador
  llegara a las réplicas.

**Nada de `sshd`, ninguna clave ajena, ningún puerto nuevo.**

---

## 3. Instalación

```bash
git clone https://github.com/SDyPPTpGrupal/cd && cd cd/agente
python3 configurar.py
```

El asistente pregunta lo que no puede averiguar solo (nombre de casa, dirección del CD, URL de
Redis), detecta el resto (IP de Tailscale, grupo del socket de Docker, `$HOME`, y si el CD pide
token o no), y antes de levantar nada **comprueba contra el sistema de verdad**:

```
=== Comprobaciones ===
  ✓ docker 29.8.0, grupo del socket 950
  ✓ el CD responde · casas del equipo python: casa-salvador casa-net
  ✓ 'casa-salvador' figura en el CD
  ✓ el CD no pide token
  ✓ redis alcanzable en 100.91.228.65:6379
  ✓ el registry está declarado como inseguro (sin TLS; el cifrado lo pone Tailscale)
  ✓ los puertos 8080 y 8081 de la réplica están libres
```

Los puertos que comprueba son **los que el CD le asignó a esta casa**, no los de por defecto: se
los pregunta al `/health`. Si están ocupados, el `docker run` fallaría en medio de un deploy y ese
deploy abortaría para todas las casas, no sólo para ésta.

Deja `~/sdypp/agente.env` y `~/sdypp/.env` en 600, crea `logs/blue` y `logs/green` con tu
usuario, y levanta el contenedor.

| | |
|---|---|
| `python3 configurar.py` | pregunta, comprueba y ofrece levantar |
| `python3 configurar.py --probar` | sólo las comprobaciones, sin tocar nada |
| `python3 configurar.py --levantar` | levanta con lo guardado, sin preguntar |
| `python3 configurar.py --mostrar` | la configuración actual y el `docker run` equivalente |

El asistente existe por una razón concreta: el `docker run` del agente tiene diez variables, y
`AG_CASA` copiado de otra casa se manifiesta como un deploy que aborta **culpando a la máquina
equivocada**. Por eso lo primero que hace es preguntarle al CD si tu casa figura en su lista.

### A mano, si preferís

```bash
docker build -t sdypp-agente:local .
docker run -d --name sdypp-agente --restart unless-stopped --network host \
    --group-add "$(stat -c '%g' /var/run/docker.sock)" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$HOME/sdypp:/casa" \
    -e AG_CASA=casa-salvador \
    -e AG_EQUIPO=python \
    -e AG_CD=http://100.101.15.93:8082 \
    -e AG_TOKEN=<sólo si el CD usa tokens; hoy no> \
    -e AG_DIR=/casa -e "AG_DIR_HOST=$HOME/sdypp" \
    sdypp-agente:local
```

Tres cosas que parecen detalles y no lo son:

- **`--group-add`** en vez de correr como root: el contenedor usa el uid 1000 y se le da nada más
  que el grupo dueño del socket de esa casa.
- **`AG_DIR` y `AG_DIR_HOST`** son dos rutas distintas a propósito. El agente corre en un
  contenedor pero le habla al daemon del host: `--env-file` lo lee el cliente (ruta de adentro) y
  el `-v` lo resuelve el daemon (ruta del host). Si se pone una sola, el bind mount de los logs
  apunta a un directorio que no existe. Corriendo el agente suelto en la máquina, con `AG_DIR`
  alcanza.
- **`--network host`** para alcanzar el tailnet sin publicar nada.

---

## 4. Variables

| Variable | Default | Qué |
|---|---|---|
| `AG_CASA` | *(obligatoria)* | El nombre de esta casa, tal como figura en `CICD_NODOS_*` del CD. |
| `AG_EQUIPO` | `python` | `python` o `java`. Decide a qué pipeline le pregunta. |
| `AG_CD` | `http://127.0.0.1:8082` | El socket de agentes del CD. |
| `AG_TOKEN` | `""` | Va en `X-Casa-Token`. **Hoy el CD no pide token**, así que va vacío; el asistente consulta `pideToken` del `/health` y ni lo pregunta. |
| `AG_REGISTRY` | `100.91.228.65:5000` | El agente **rechaza** cualquier imagen que no salga de acá. |
| `AG_DIR` | `$HOME/sdypp` | Dónde ve el agente el `.env` y los `logs/`. |
| `AG_DIR_HOST` | *(igual que `AG_DIR`)* | La misma carpeta, vista desde el host. |
| `AG_CONTENEDOR` | `sdypp-{color}-app-1` | Plantilla del nombre. `{color}` se reemplaza por blue/green. |
| `AG_PUERTO_INTERNO` | `8080` | El puerto de la réplica adentro del contenedor. |
| `AG_INTENTOS_SALUD` / `AG_ESPERA_SALUD` | `30` / `2` | Un minuto de espera máxima a que esté `healthy`. |
| `AG_ESPERA_REINTENTO` | `5` | Cuánto espera antes de volver a intentar cuando el CD no contesta. |
| `AG_STOP_TIMEOUT` | `15` | Va al `docker run`. Mayor que el *grace* del servidor gRPC, o el drenado se corta. |
| `AG_BITACORA` | `$AG_DIR/logs/agente.log` | Formato común del grupo. |
| `AG_LATIDO` | `$AG_DIR/agente.latido` | Lo mira el `HEALTHCHECK`. |

---

## 5. Qué mira el `HEALTHCHECK`

La antigüedad del latido, no que el proceso exista: así detecta un agente **colgado** y no sólo uno
muerto. El agente toca el archivo en cada vuelta del loop; si pasan más de 120 segundos sin
tocarlo (contra un long-poll de 30), el contenedor pasa a `unhealthy`.

---

## 6. Seguridad — qué puede hacer el agente

El socket de Docker es equivalente a root en la casa, así que la pregunta correcta no es si el
agente tiene privilegios sino **qué puede llegar a ejecutar**:

- Sólo imágenes cuyo nombre empiece con `AG_REGISTRY/sdypp-app-<equipo>:`, y siempre bajadas
  **por digest**: corre exactamente el contenido que se publicó, aunque alguien reescriba el tag
  en el registry.
- El digest tiene que matchear `^sha256:[0-9a-f]{64}$` y el puerto ser un número.
- El objetivo no lleva comandos: lleva imagen, digest, puerto y versión. No hay forma de pedirle
  al agente que ejecute otra cosa.

Es decir: aunque alguien tomara el control del CD, lo más que puede hacer es desplegar una imagen
del registry del grupo. Con el esquema anterior tenía una shell en cada casa.

---

## 6.1 Si el CD rechaza el token

```
objetivo | RECHAZADO | el CD rechazó el token de casa-salvador — que AG_TOKEN coincida con
                       el de esta casa en CICD_TOKENS del CD. No se toca nada
```

No es un problema de red: el CD contestó, y contestó que no. Reintenta cada 30 segundos (más
espaciado que un fallo de conectividad, porque esto no se arregla solo) y **mientras tanto no
toca nada**: la réplica que esté sirviendo sigue sirviendo.

Ojo con un caso que confunde: si Plataforma configuró tokens para algunas casas y no para otras,
las que no tienen quedan afuera. Los tokens son todo o nada.

## 7. Para mirarlo

```bash
docker logs -f sdypp-agente
tail -f ~/sdypp/logs/agente.log
docker ps --filter label=sdypp.color            # las réplicas que maneja
docker inspect -f '{{index .Config.Labels "sdypp.generacion"}}' sdypp-green-app-1
```

Las etiquetas `sdypp.digest`, `sdypp.puerto`, `sdypp.generacion` y `sdypp.color` las pone el agente
al crear cada contenedor: son lo que le permite comparar contra el objetivo sin preguntarle a
nadie. `docker inspect` por sí solo da el image id, que no sirve para compararlo con el repo digest
del manifiesto.
