FROM debian:13-slim

# Evitar prompts interactivos durante la instalación de paquetes
ENV DEBIAN_FRONTEND=noninteractive

# Instalar dependencias del sistema:
# - openssh-server y openssh-client para el túnel y comandos remotos
# - inotify-tools para detectar subida de manifiestos
# - curl y jq para interactuar con balanceador y validar manifiestos
# - procps para supervisión de procesos (pgrep en HEALTHCHECK)
# - ca-certificates y tar para descarga y descompresión de grpcurl
# - python3 para el servidor de control (objetivo + barrera), sólo stdlib
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-server \
    openssh-client \
    python3 \
    inotify-tools \
    curl \
    jq \
    procps \
    ca-certificates \
    tar \
    && rm -rf /var/lib/apt/lists/*

# Instalación de grpcurl pineado y verificado por sha256
ARG GRPCURL_VERSION=1.9.2
ARG GRPCURL_SHA256=1c7caf2628d8607d8a3bbee5ce7786bba4879abe566b075a4f129a97ccfa8465

RUN curl -fsSL "https://github.com/fullstorydev/grpcurl/releases/download/v${GRPCURL_VERSION}/grpcurl_${GRPCURL_VERSION}_linux_x86_64.tar.gz" -o /tmp/grpcurl.tar.gz \
    && echo "${GRPCURL_SHA256}  /tmp/grpcurl.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/grpcurl.tar.gz -C /usr/local/bin grpcurl \
    && chmod +x /usr/local/bin/grpcurl \
    && rm -f /tmp/grpcurl.tar.gz

# Configuración de usuarios para acceso scp restringido por equipo
RUN useradd -m -s /bin/bash deploy-python \
    && useradd -m -s /bin/bash deploy-java \
    && mkdir -p /run/sshd

# Estructura de directorios bajo /cicd
WORKDIR /cicd
RUN mkdir -p /cicd/bin \
             /cicd/claves/host \
             /cicd/python/entrante/rechazados /cicd/python/entrante/historial /cicd/python/estado \
             /cicd/java/entrante/rechazados /cicd/java/entrante/historial /cicd/java/estado \
             /cicd/logs

# Copiar scripts del sistema, el servidor de control y el contrato
COPY bin/ /cicd/bin/
COPY app/ /cicd/app/
COPY deploy.sh /cicd/bin/deploy.sh
RUN chmod +x /cicd/bin/*.sh

# Puertos: sshd para el dev, control para los agentes del tailnet.
# El socket de disparo (8083) no se expone: vive en el loopback del contenedor.
EXPOSE 2222 8082

# HEALTHCHECK: sshd vivo, los dos vigilantes vivos y el control respondiendo
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD pgrep sshd >/dev/null \
     && pgrep -f "vigilante.sh python" >/dev/null \
     && pgrep -f "vigilante.sh java" >/dev/null \
     && curl -sf -o /dev/null "http://127.0.0.1:${CICD_PUERTO_DISPARO:-8083}/deploy/python/estado" \
     || exit 1

ENTRYPOINT ["/cicd/bin/arranque.sh"]
