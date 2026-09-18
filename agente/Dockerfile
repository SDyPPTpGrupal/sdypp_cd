# Agente de casa: converge los contenedores blue/green hacia el objetivo del CD.
#
# No corre un daemon de Docker adentro (nada de Docker-in-Docker): le habla al
# daemon de la propia casa por el socket que se le monta. Por eso alcanza con el
# cliente, que se baja pineado y verificado por sha256, igual que grpcurl en el CD.
FROM python:3.13-slim

ENV DEBIAN_FRONTEND=noninteractive

ARG DOCKER_VERSION=28.5.2
ARG DOCKER_SHA256=ea90cfd12e1eeb12aa1c971741adb8bd4ed88e2a574eaac13f5029a1dbc6300d

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_VERSION}.tgz" \
       -o /tmp/docker.tgz \
    && echo "${DOCKER_SHA256}  /tmp/docker.tgz" | sha256sum -c - \
    && tar -xzf /tmp/docker.tgz -C /usr/local/bin --strip-components=1 docker/docker \
    && chmod +x /usr/local/bin/docker \
    && rm -f /tmp/docker.tgz \
    && apt-get purge -y --auto-remove curl \
    && rm -rf /var/lib/apt/lists/*

# No corre como root: el acceso al socket se da en el `docker run` con
# --group-add $(stat -c '%g' /var/run/docker.sock), que es el grupo de esa casa.
#
# El uid es 1000 por la misma razón que en la app: el agente escribe su bitácora
# y su latido en un bind mount del disco de la casa, y el uid de adentro tiene que
# coincidir con el del dueño de ese directorio afuera o no puede escribir. 1000 es
# el primer usuario de cualquier Linux de escritorio.
RUN useradd --system --create-home --uid 1000 --user-group agente

WORKDIR /agente
COPY agente.py /agente/agente.py
RUN chown -R agente:agente /agente

USER agente

# Mira la antigüedad del latido, no que el proceso exista: así detecta un agente
# colgado. El umbral es holgado contra el long-poll (30 s por defecto).
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python3 -c "import os,sys,time; r=os.environ.get('AG_LATIDO') or os.path.join(os.path.expandvars(os.environ.get('AG_DIR','/casa')),'agente.latido'); sys.exit(0 if os.path.exists(r) and time.time()-os.path.getmtime(r) < 120 else 1)"

ENTRYPOINT ["python3", "-u", "/agente/agente.py"]
