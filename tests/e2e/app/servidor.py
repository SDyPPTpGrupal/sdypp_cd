#!/usr/bin/env python3
"""Réplica de mentira para la prueba de punta a punta del CD con agentes.

No es la app del TP: implementa lo mínimo que el CD y el balanceador le miran a
una réplica —`Identidad` (de donde sale la versión que verifica el CD), `Salud` y
el health estándar de gRPC— para que el e2e no dependa del repo de ninguna app.

La versión se hornea en el build (ARG VERSION): dos versiones son dos imágenes
con digests distintos, que es justamente lo que el deploy tiene que distinguir.
"""
import os
import signal
import threading
from concurrent import futures
from datetime import datetime, timezone

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

import contrato_pb2
import contrato_pb2_grpc

VERSION = int(os.environ.get("VERSION", "0"))
HOST = os.environ.get("HOST_NAME", "sin-nombre")
PUERTO = os.environ.get("PUERTO", "8080")
ARRANCADO = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

# Para probar el camino de aborto: con TARDA_EN_SANAR la réplica levanta pero no
# se declara sana hasta pasados esos segundos.
TARDA_EN_SANAR = float(os.environ.get("TARDA_EN_SANAR", "0"))


class Servicio(contrato_pb2_grpc.ServicioServicer):
    def Identidad(self, pedido, contexto):
        return contrato_pb2.Instancia(
            app="python", lenguaje="python (falsa, e2e)", version=VERSION,
            mensaje="réplica de prueba", host=HOST, arrancado=ARRANCADO)

    def Salud(self, pedido, contexto):
        return contrato_pb2.EstadoSalud(estado="sano", host=HOST)

    def Echo(self, pedido, contexto):
        return contrato_pb2.PongRespuesta(mensaje=getattr(pedido, "mensaje", ""), host=HOST)


def main():
    servidor = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
    contrato_pb2_grpc.add_ServicioServicer_to_server(Servicio(), servidor)

    salud = health.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(salud, servidor)
    servidor.add_insecure_port(f"[::]:{PUERTO}")
    servidor.start()

    def declarar_sano():
        salud.set("", health_pb2.HealthCheckResponse.SERVING)
        salud.set("sdypp.Servicio", health_pb2.HealthCheckResponse.SERVING)
        print(f"réplica {HOST} version={VERSION} sana en :{PUERTO}", flush=True)

    if TARDA_EN_SANAR:
        salud.set("", health_pb2.HealthCheckResponse.NOT_SERVING)
        threading.Timer(TARDA_EN_SANAR, declarar_sano).start()
    else:
        declarar_sano()

    terminar = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: terminar.set())
    try:
        terminar.wait()
    except KeyboardInterrupt:
        pass
    servidor.stop(10).wait()


if __name__ == "__main__":
    main()
