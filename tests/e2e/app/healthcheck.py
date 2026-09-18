#!/usr/bin/env python3
"""HEALTHCHECK del contenedor: pregunta por el health estándar de gRPC."""
import os
import sys

import grpc
from grpc_health.v1 import health_pb2, health_pb2_grpc

try:
    canal = grpc.insecure_channel(f"127.0.0.1:{os.environ.get('PUERTO', '8080')}")
    respuesta = health_pb2_grpc.HealthStub(canal).Check(
        health_pb2.HealthCheckRequest(service=""), timeout=3)
    sys.exit(0 if respuesta.status == health_pb2.HealthCheckResponse.SERVING else 1)
except Exception:
    sys.exit(1)
