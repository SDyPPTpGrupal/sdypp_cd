# Réplica de mentira para el e2e. La versión se hornea en el build: dos versiones
# son dos imágenes con digests distintos.
FROM python:3.13-slim AS builder
WORKDIR /build
RUN pip install --no-cache-dir grpcio-tools==1.68.1
COPY contrato.proto .
RUN python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. contrato.proto

FROM python:3.13-slim
ARG VERSION=0
ENV PYTHONUNBUFFERED=1 VERSION=${VERSION}
WORKDIR /app
RUN pip install --no-cache-dir grpcio==1.68.1 grpcio-health-checking==1.68.1
COPY --from=builder /build/contrato_pb2.py /build/contrato_pb2_grpc.py ./
COPY servidor.py healthcheck.py ./
RUN useradd --system --uid 1000 --user-group sdypp && mkdir -p /app/logs && chown -R sdypp:sdypp /app
USER sdypp
EXPOSE 8080
HEALTHCHECK --interval=3s --timeout=3s --start-period=2s --retries=3 CMD ["python", "healthcheck.py"]
STOPSIGNAL SIGTERM
CMD ["python", "servidor.py"]
