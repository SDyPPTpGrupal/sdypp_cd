#!/usr/bin/env python3
"""Pruebas de la consola de Plataforma.

Se prueba lo que arma y lo que lee, no el menú: el archivo que termina siendo el
`--env-file` del CD, y cómo se representan las casas adentro de esos strings —que
es donde un error a mano rompe un despliegue.

    python3 tests/consola.py
"""
import os
import shutil
import sys
import tempfile
import unittest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
import consola  # noqa: E402


class PruebaConfig(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="consola-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.original = consola.CONFIG
        consola.CONFIG = os.path.join(self.tmp, "plataforma.env")

    def tearDown(self):
        consola.CONFIG = self.original

    def test_sin_archivo_devuelve_los_defaults(self):
        cfg = consola.leer_config()
        self.assertEqual(cfg["CICD_PUERTO_AGENTES"], "8082")
        self.assertEqual(cfg["CICD_NODOS_PYTHON"], "")

    def test_guarda_todas_las_claves_aunque_estén_vacías(self):
        """El archivo es el --env-file del contenedor: una clave que falta es una
        variable que el CD no ve, y algunas cambian su comportamiento."""
        consola.guardar_config({"CASA": "casa-tomas"})
        guardado = consola.leer_config()
        for clave in consola.DEFAULTS:
            self.assertIn(clave, guardado)

    def test_ida_y_vuelta_con_espacios(self):
        """CASAS y CICD_NODOS_* son listas adentro de un string: si se recortaran
        los espacios al leer, quedaría una sola casa pegada."""
        cfg = dict(consola.DEFAULTS)
        cfg["CASAS"] = "casa-a=u@10.0.0.1 casa-b=u@10.0.0.2"
        cfg["CICD_NODOS_PYTHON"] = "casa-a casa-b"
        consola.guardar_config(cfg)
        leido = consola.leer_config()
        self.assertEqual(leido["CASAS"], "casa-a=u@10.0.0.1 casa-b=u@10.0.0.2")
        self.assertEqual(consola.casas_de(leido, "python"), ["casa-a", "casa-b"])

    def test_el_archivo_queda_en_600(self):
        consola.guardar_config(dict(consola.DEFAULTS))
        self.assertEqual(os.stat(consola.CONFIG).st_mode & 0o777, 0o600)

    def test_hay_config_exige_al_menos_una_casa(self):
        consola.guardar_config(dict(consola.DEFAULTS))
        self.assertFalse(consola.hay_config())
        cfg = dict(consola.DEFAULTS, CICD_NODOS_PYTHON="casa-a")
        consola.guardar_config(cfg)
        self.assertTrue(consola.hay_config())


class PruebaLecturaDeCasas(unittest.TestCase):

    def setUp(self):
        self.cfg = dict(
            consola.DEFAULTS,
            CASAS="casa-a=juan@10.0.0.1 casa-b=ana@10.0.0.2 casa-j=x@10.0.0.9",
            CICD_NODOS_PYTHON="casa-a casa-b",
            CICD_NODOS_JAVA="casa-j",
            PUERTOS_CASA="casa-b=8090:8091")

    def test_separa_los_equipos(self):
        self.assertEqual(consola.casas_de(self.cfg, "python"), ["casa-a", "casa-b"])
        self.assertEqual(consola.casas_de(self.cfg, "java"), ["casa-j"])

    def test_ip_sin_el_usuario(self):
        self.assertEqual(consola.ip_de(self.cfg, "casa-a"), "10.0.0.1")
        self.assertEqual(consola.ip_de(self.cfg, "no-existe"), "?")

    def test_puertos_declarados_y_por_defecto(self):
        self.assertEqual(consola.puertos_de(self.cfg, "casa-b"), "8090:8091")
        self.assertEqual(consola.puertos_de(self.cfg, "casa-a"), "8080:8081")

    def test_un_prefijo_no_se_confunde_con_otra_casa(self):
        """casa-a y casa-alta empiezan igual: buscar por prefijo daría la IP de la
        que no es."""
        cfg = dict(self.cfg, CASAS="casa-alta=u@10.0.0.5 casa-a=u@10.0.0.1")
        self.assertEqual(consola.ip_de(cfg, "casa-a"), "10.0.0.1")
        self.assertEqual(consola.ip_de(cfg, "casa-alta"), "10.0.0.5")


class PruebaOrdenDocker(unittest.TestCase):

    def test_monta_los_volumenes_y_el_env_file(self):
        orden = " ".join(consola.orden_docker())
        self.assertIn("--env-file", orden)
        self.assertIn(consola.CONFIG, orden)
        for volumen in ("claves", "python", "java", "logs"):
            self.assertIn(f"{volumen}:/cicd/{volumen}", orden)

    def test_no_monta_el_socket_de_docker(self):
        """El CD no lo necesita y montarlo sería root en Plataforma: está en la
        lista negra del diseño (00-COMUN.md §8)."""
        orden = " ".join(consola.orden_docker())
        self.assertNotIn("docker.sock", orden)
        self.assertNotIn("--privileged", orden)

    def test_usa_network_host(self):
        """Para alcanzar el tailnet y el loopback del balanceador sin publicar nada."""
        self.assertIn("--network host", " ".join(consola.orden_docker()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
