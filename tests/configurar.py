#!/usr/bin/env python3
"""Pruebas del asistente de configuración del agente.

Se prueba lo que decide, no lo que pregunta: las validaciones que impiden guardar
una configuración rota, y los archivos que deja.

    python3 tests/configurar.py
"""
import os
import shutil
import sys
import tempfile
import unittest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(RAIZ, "agente"))
import configurar  # noqa: E402


class PruebaValidaciones(unittest.TestCase):

    def test_nombre_de_casa(self):
        self.assertIsNone(configurar.validar_casa("casa-salvador"))
        self.assertIsNone(configurar.validar_casa("casa-justi1"))
        for malo in ("Casa-Salvador", "casa salvador", "c", "casa_salvador", ""):
            self.assertIsNotNone(configurar.validar_casa(malo), malo)

    def test_url_del_cd(self):
        self.assertIsNone(configurar.validar_url_cd("http://100.101.15.93:8082"))
        for malo in ("100.101.15.93:8082", "https://100.101.15.93:8082", "http://", "pepe"):
            self.assertIsNotNone(configurar.validar_url_cd(malo), malo)

    def test_url_de_redis(self):
        self.assertIsNone(configurar.validar_redis("redis://:clave@100.91.228.65:6379/0"))
        # Sin contraseña no sirve: el Redis del grupo pide auth, y el error que da
        # la app después es mucho menos claro que este.
        self.assertIsNotNone(configurar.validar_redis("redis://100.91.228.65:6379/0"))
        self.assertIsNotNone(configurar.validar_redis("http://100.91.228.65:6379"))

    def test_registry(self):
        self.assertIsNone(configurar.validar_registry("100.91.228.65:5000"))
        self.assertIsNone(configurar.validar_registry("localhost:5000"))
        self.assertIsNotNone(configurar.validar_registry("100.91.228.65:5000/sdypp-app-python"))

    def test_puertos_por_defecto_si_el_cd_no_contesta(self):
        """Sin CD no se puede saber qué puertos tocan: se caen a los de siempre en
        vez de dar por buena la comprobación."""
        cfg = {"AG_CD": "http://127.0.0.1:1", "AG_EQUIPO": "python", "AG_CASA": "casa-x"}
        self.assertEqual(configurar.puertos_asignados(cfg), [8080, 8081])

    def test_localhost_no_necesita_insecure_registries(self):
        """Docker ya los trata como inseguros: exigir daemon.json ahí sería un
        aviso que manda a tocar la configuración del daemon sin motivo."""
        self.assertTrue(configurar.registry_inseguro_configurado("localhost:5000"))
        self.assertTrue(configurar.registry_inseguro_configurado("127.0.0.1:5000"))


class PruebaArchivos(unittest.TestCase):

    def setUp(self):
        self.carpeta = tempfile.mkdtemp(prefix="casa-")
        self.addCleanup(shutil.rmtree, self.carpeta, True)
        self.cfg = {
            "AG_CASA": "casa-salvador", "AG_EQUIPO": "python",
            "AG_CD": "http://100.101.15.93:8082", "AG_TOKEN": "secreto",
            "AG_REGISTRY": "100.91.228.65:5000", "AG_DIR_HOST": self.carpeta,
            "TP_REDIS_URL": "redis://:clave@100.91.228.65:6379/0",
        }

    def test_guarda_y_relee(self):
        configurar.guardar(self.cfg)
        leido = configurar.cargar(self.carpeta)
        self.assertEqual(leido["AG_CASA"], "casa-salvador")
        self.assertEqual(leido["AG_TOKEN"], "secreto")
        self.assertEqual(leido["TP_REDIS_URL"], "redis://:clave@100.91.228.65:6379/0")

    def test_los_archivos_con_secretos_quedan_en_600(self):
        configurar.guardar(self.cfg)
        for nombre in (configurar.ARCHIVO_AGENTE, configurar.ARCHIVO_APP):
            modo = os.stat(os.path.join(self.carpeta, nombre)).st_mode & 0o777
            self.assertEqual(modo, 0o600, nombre)

    def test_crea_los_logs_por_color(self):
        """Tienen que existir antes del primer deploy: si los crea Docker al montar,
        quedan de root y la réplica no puede escribir su bitácora."""
        configurar.guardar(self.cfg)
        for color in ("blue", "green"):
            self.assertTrue(os.path.isdir(os.path.join(self.carpeta, "logs", color)))

    def test_separa_lo_del_agente_de_lo_de_la_replica(self):
        """El .env es del nodo, no del release: lo lee la app, y el agente lo monta
        pero nunca lo toca. El token del agente no tiene por qué estar ahí."""
        configurar.guardar(self.cfg)
        del_app = configurar.leer_env(os.path.join(self.carpeta, configurar.ARCHIVO_APP))
        self.assertEqual(list(del_app), ["TP_REDIS_URL"])
        del_agente = configurar.leer_env(os.path.join(self.carpeta, configurar.ARCHIVO_AGENTE))
        self.assertNotIn("TP_REDIS_URL", del_agente)

    def test_las_dos_rutas_del_bind_mount(self):
        """AG_DIR es la de adentro del contenedor y AG_DIR_HOST la de afuera: el
        --env-file lo lee el cliente y el -v lo resuelve el daemon."""
        configurar.guardar(self.cfg)
        leido = configurar.cargar(self.carpeta)
        self.assertEqual(leido["AG_DIR"], "/casa")
        self.assertEqual(leido["AG_DIR_HOST"], self.carpeta)

    def test_el_docker_run_monta_lo_que_corresponde(self):
        orden = " ".join(configurar.orden_docker(self.cfg))
        self.assertIn(f"{self.carpeta}:/casa", orden)
        self.assertIn("/var/run/docker.sock:/var/run/docker.sock", orden)
        self.assertIn(os.path.join(self.carpeta, configurar.ARCHIVO_AGENTE), orden)
        self.assertIn("--network host", orden)
        self.assertNotIn("--privileged", orden)   # nunca: el socket alcanza


if __name__ == "__main__":
    unittest.main(verbosity=2)
