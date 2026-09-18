#!/usr/bin/env python3
"""Pruebas del servidor de control: objetivo, generación y barrera.

Sin Docker y sin red: lo que se prueba acá es la lógica que decide, no el
transporte. El camino completo con contenedores de verdad está en tests/e2e.sh.

    python3 -m unittest discover -s tests -p 'control.py' -v
"""
import atexit
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = tempfile.mkdtemp(prefix="cd-tests-")
atexit.register(shutil.rmtree, BASE, True)

# El módulo lee su configuración al importarse: se prepara antes.
os.environ.update({
    "CICD_BASE": BASE,
    "CICD_BITACORA": os.path.join(BASE, "logs", "cicd.log"),
    "CICD_NODOS_PYTHON": "casa-a casa-b",
    "CICD_NODOS_JAVA": "casa-j",
    "CASAS": "casa-a=nadie@10.0.0.1 casa-b=nadie@10.0.0.2 casa-j=nadie@10.0.0.3",
    "PUERTOS_CASA": "casa-a=8090:8091",
    "REGISTRY": "10.0.0.9:5000",
})
sys.path.insert(0, os.path.join(RAIZ, "app"))
import control  # noqa: E402

DIGESTO = "sha256:" + "a" * 64
OTRO_DIGESTO = "sha256:" + "b" * 64


def manifiesto(version=7, digest=DIGESTO, equipo="python", imagen=None):
    return {"equipo": equipo,
            "imagen": imagen or f"10.0.0.9:5000/sdypp-app-{equipo}:v{version}-abc1234",
            "digest": digest, "version": version, "commit": "abc1234"}


class PruebaValidacion(unittest.TestCase):
    """El manifiesto se valida también acá y no sólo en el vigilante: al control
    también se le puede hablar por el socket de disparo."""

    def test_valido(self):
        self.assertIsNone(control.validar_manifiesto("python", manifiesto()))

    def test_equipo_cruzado(self):
        self.assertIn("equipo", control.validar_manifiesto("java", manifiesto()))

    def test_imagen_de_otro_registry(self):
        m = manifiesto(imagen="docker.io/sdypp-app-python:v7-abc1234")
        self.assertIn("imagen", control.validar_manifiesto("python", m))

    def test_imagen_del_otro_equipo(self):
        m = manifiesto(imagen="10.0.0.9:5000/sdypp-app-java:v7-abc1234")
        self.assertIn("imagen", control.validar_manifiesto("python", m))

    def test_digest_mal_formado(self):
        self.assertIn("digest", control.validar_manifiesto("python", manifiesto(digest="sha256:cortito")))

    def test_version_no_entera(self):
        m = manifiesto()
        m["version"] = "siete"
        self.assertIn("version", control.validar_manifiesto("python", m))


class PruebaPuertos(unittest.TestCase):
    def test_puerto_configurado(self):
        self.assertEqual(control.puerto_de("casa-a", "blue"), "8090")
        self.assertEqual(control.puerto_de("casa-a", "green"), "8091")

    def test_puerto_por_defecto(self):
        self.assertEqual(control.puerto_de("casa-b", "blue"), "8080")
        self.assertEqual(control.puerto_de("casa-b", "green"), "8081")

    def test_ip_desde_casas(self):
        self.assertEqual(control.ip_de("casa-b"), "10.0.0.2")


class PruebaEstado(unittest.TestCase):
    def test_ida_y_vuelta(self):
        control.escribir_estado("python", "casa-a", {
            "COLOR_ACTIVO": "green", "COLOR_ANTERIOR": "blue",
            "VERSION_ACTIVA": "7", "IMAGEN_ACTIVA": "img:v7", "DIGEST_ACTIVO": DIGESTO})
        leido = control.leer_estado("python", "casa-a")
        self.assertEqual(leido["COLOR_ACTIVO"], "green")
        self.assertEqual(leido["DIGEST_ACTIVO"], DIGESTO)

    def test_sin_estado_previo_arranca_en_blue(self):
        activo, nuevo = control.color_de({}, "casa-x", "python")
        self.assertEqual(activo, "")
        self.assertEqual(nuevo, "blue")

    def test_con_estado_alterna(self):
        _, nuevo = control.color_de({"COLOR_ACTIVO": "blue"}, "casa-a", "python")
        self.assertEqual(nuevo, "green")


class PruebaObjetivo(unittest.TestCase):
    def setUp(self):
        self.pipeline = control.Pipeline("python")

    def test_la_generacion_sube_de_a_uno(self):
        self.assertEqual(self.pipeline.publicar({}, "una"), 1)
        self.assertEqual(self.pipeline.publicar({}, "otra"), 2)

    def test_sin_novedad_devuelve_none(self):
        self.pipeline.publicar({"casa-a": {}}, "una")
        inicio = time.monotonic()
        self.assertIsNone(self.pipeline.leer_objetivo(desde=1, espera=0.3))
        self.assertGreaterEqual(time.monotonic() - inicio, 0.25)

    def test_una_generacion_nueva_contesta_al_toque(self):
        self.pipeline.publicar({"casa-a": {}}, "una")
        objetivo = self.pipeline.leer_objetivo(desde=0, espera=5)
        self.assertEqual(objetivo["generacion"], 1)

    def test_el_long_poll_se_despierta_al_publicar(self):
        """Lo que hace que un deploy sea inmediato sin que los agentes hagan polling
        corto: el GET queda colgado y lo despierta la publicación."""
        recibido = []

        def agente():
            recibido.append(self.pipeline.leer_objetivo(desde=0, espera=5))

        hilo = threading.Thread(target=agente)
        hilo.start()
        time.sleep(0.2)
        self.pipeline.publicar({"casa-a": {"blue": {}}}, "deploy")
        hilo.join(timeout=3)
        self.assertEqual(len(recibido), 1)
        self.assertEqual(recibido[0]["generacion"], 1)

    def test_abortar_es_publicar_sin_el_color_nuevo(self):
        deseado = {"casa-a": {"blue": {"imagen": "vieja"}, "green": {"imagen": "nueva"}}}
        planes = {"casa-a": {"activo": "blue", "nuevo": "green"}}
        recortado = control.objetivo_sin("python", deseado, planes)
        self.assertEqual(list(recortado["casa-a"]), ["blue"])


class PruebaGeneracionPersistida(unittest.TestCase):
    """Un CD reiniciado tiene que seguir donde quedó. Si volviera a empezar en 1,
    los agentes —que recuerdan haber aplicado la 12— ignorarían todo lo que
    publicara después, y el sistema quedaría sin poder desplegar."""

    def test_se_guarda_al_publicar(self):
        pipeline = control.Pipeline("python", control.leer_generacion("python"))
        pipeline.publicar({}, "una")
        generacion = pipeline.publicar({}, "otra")
        self.assertEqual(control.leer_generacion("python"), generacion)

    def test_un_pipeline_nuevo_retoma_donde_quedo(self):
        pipeline = control.Pipeline("python", control.leer_generacion("python"))
        ultima = pipeline.publicar({}, "antes del reinicio")

        reiniciado = control.Pipeline("python", control.leer_generacion("python"))
        self.assertEqual(reiniciado.objetivo["generacion"], ultima)
        self.assertEqual(reiniciado.publicar({}, "después del reinicio"), ultima + 1)

    def test_sin_archivo_arranca_en_cero(self):
        self.assertEqual(control.leer_generacion("java"), 0)


class PruebaBarrera(unittest.TestCase):
    def setUp(self):
        self.pipeline = control.Pipeline("python")
        self.pipeline.abrir_barrera(generacion=5, casas=["casa-a", "casa-b"])

    def reporte(self, casa, generacion=5, estado="listo"):
        return self.pipeline.anotar_reporte(
            casa, {"casa": casa, "generacion": generacion, "estado": estado})

    def test_cierra_recien_con_todas(self):
        self.assertEqual(self.reporte("casa-a"), "anotado")
        self.assertFalse(self.pipeline.completa.is_set())
        self.assertEqual(self.reporte("casa-b"), "anotado")
        self.assertTrue(self.pipeline.completa.is_set())

    def test_un_fallo_no_hace_esperar_al_resto(self):
        """Si una casa ya falló, esperar a las demás sólo retrasa el aborto."""
        self.assertEqual(self.reporte("casa-a", estado="fallo"), "anotado")
        self.assertTrue(self.pipeline.completa.is_set())

    def test_ignora_generaciones_viejas(self):
        """Un reporte que llegó tarde de un deploy anterior no puede cerrar esta
        barrera: si no, un deploy conmutaría con réplicas de la versión anterior."""
        self.assertEqual(self.reporte("casa-a", generacion=4), "generacion-vieja")
        self.assertFalse(self.pipeline.completa.is_set())

    def test_ignora_casas_ajenas(self):
        self.assertEqual(self.reporte("casa-intrusa"), "casa-no-esperada")
        self.assertFalse(self.pipeline.completa.is_set())

    def test_no_cierra_si_falta_una(self):
        self.reporte("casa-a")
        self.assertFalse(self.pipeline.completa.wait(timeout=0.3))


class PruebaArmarObjetivo(unittest.TestCase):
    def setUp(self):
        for casa in ("casa-a", "casa-b"):
            ruta = control.ruta_estado("python", casa)
            if os.path.exists(ruta):
                os.remove(ruta)

    def test_primer_deploy_solo_describe_el_color_nuevo(self):
        deseado, planes = control.armar_objetivo("python", ["casa-a"], manifiesto(5))
        self.assertEqual(list(deseado["casa-a"]), ["blue"])
        self.assertEqual(planes["casa-a"]["nuevo"], "blue")
        self.assertEqual(deseado["casa-a"]["blue"]["puerto"], "8090")

    def test_segundo_deploy_describe_los_dos_colores(self):
        """El color activo se describe con la imagen que ya está sirviendo, para
        que el agente lo reconozca y no lo vuelva a levantar."""
        control.escribir_estado("python", "casa-a", {
            "COLOR_ACTIVO": "blue", "VERSION_ACTIVA": "5",
            "IMAGEN_ACTIVA": "10.0.0.9:5000/sdypp-app-python:v5-aaa1111",
            "DIGEST_ACTIVO": OTRO_DIGESTO})
        deseado, planes = control.armar_objetivo("python", ["casa-a"], manifiesto(6))
        self.assertEqual(sorted(deseado["casa-a"]), ["blue", "green"])
        self.assertEqual(deseado["casa-a"]["blue"]["digest"], OTRO_DIGESTO)
        self.assertEqual(deseado["casa-a"]["green"]["version"], "6")
        self.assertEqual(planes["casa-a"]["nuevo"], "green")


if __name__ == "__main__":
    unittest.main(verbosity=2)
