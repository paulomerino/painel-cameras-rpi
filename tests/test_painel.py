"""Testes das partes puras do painel (configuração e limpeza de fotos).

Rodam em qualquer máquina com Pillow instalado (no Pi: python3-pil):
    python3 -m unittest discover -s tests -v
"""
import importlib.util
import os
import tempfile
import time
import unittest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def carregar(env=None):
    """Importa painel-cameras.py como módulo, com variáveis de ambiente de teste."""
    antigas = {k: os.environ.get(k) for k in (env or {})}
    os.environ.update(env or {})
    try:
        spec = importlib.util.spec_from_file_location("painel", os.path.join(RAIZ, "painel-cameras.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in antigas.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def criar(caminho, tamanho=1000, dias_atras=0):
    os.makedirs(os.path.dirname(caminho), exist_ok=True)
    with open(caminho, "wb") as f:
        f.write(b"x" * tamanho)
    t = time.time() - dias_atras * 86400
    os.utime(caminho, (t, t))


class TestCameras(unittest.TestCase):
    def test_descobre_cameras_em_ordem_numerica(self):
        m = carregar({"PAINEL_CONFIG": "/nao/existe"})
        env = {}
        for n in (10, 2, 1):
            env.update({f"CAM{n}_USER": "u", f"CAM{n}_PASS": "p", f"CAM{n}_HOST": f"10.0.0.{n}"})
        self.assertEqual([c["id"] for c in m.carregar_cameras(env)], ["CAM1", "CAM2", "CAM10"])

    def test_padroes_e_opcionais(self):
        m = carregar({"PAINEL_CONFIG": "/nao/existe"})
        env = {"CAM1_USER": "u", "CAM1_PASS": "p", "CAM1_HOST": "10.0.0.1"}
        c = m.carregar_cameras(env)[0]
        self.assertEqual((c["porta"], c["caminho"], c["transporte"], c["nome"]), (554, "/onvif1", "udp", "Câmera 1"))
        env.update({"CAM1_PORTA": "8554", "CAM1_CAMINHO": "/stream", "CAM1_TRANSPORTE": "TCP", "CAM1_NOME": "Sala"})
        c = m.carregar_cameras(env)[0]
        self.assertEqual((c["porta"], c["caminho"], c["transporte"], c["nome"]), (8554, "/stream", "tcp", "Sala"))

    def test_ignora_camera_sem_host(self):
        m = carregar({"PAINEL_CONFIG": "/nao/existe"})
        self.assertEqual(m.carregar_cameras({"CAM1_USER": "u", "CAM1_PASS": "p"}), [])

    def test_senha_some_dos_erros(self):
        m = carregar({"PAINEL_CONFIG": "/nao/existe"})
        msg = m.limpar("falhou rtsp://admin:a b/c@host e a b/c de novo", "a b/c")
        self.assertNotIn("a b/c", msg)
        self.assertIn("***", msg)

    def test_senha_com_caracteres_especiais_no_arquivo(self):
        with tempfile.TemporaryDirectory() as d:
            arq = os.path.join(d, "x.env")
            with open(arq, "w", encoding="utf-8") as f:
                f.write("# comentario\nCAM1_PASS=a$b\"c\\d=e\n")
            m = carregar({"PAINEL_CONFIG": arq})
            self.assertEqual(m._CFG["CAM1_PASS"], 'a$b"c\\d=e')


class TestLimpeza(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def fotos(self):
        return sorted(
            os.path.relpath(os.path.join(r, f), self.dir).replace(os.sep, "/")
            for r, _, fs in os.walk(self.dir) for f in fs
        )

    def test_apaga_por_idade_e_remove_pastas_vazias(self):
        criar(f"{self.dir}/CAM1/2026-11-01/10-00-00.jpg", dias_atras=12)
        criar(f"{self.dir}/CAM1/2026-11-14/10-00-00.jpg", dias_atras=0)
        m = carregar({"PAINEL_CONFIG": "/nao/existe", "PAINEL_GRAVAR_DIR": self.dir, "PAINEL_RETENCAO_DIAS": "7"})
        m.limpar_gravacoes()
        self.assertEqual(self.fotos(), ["CAM1/2026-11-14/10-00-00.jpg"])
        self.assertFalse(os.path.exists(f"{self.dir}/CAM1/2026-11-01"))

    def test_apaga_por_tamanho_as_mais_antigas_primeiro(self):
        for dia, hora in (("13", "10"), ("13", "11"), ("14", "10"), ("14", "11"), ("14", "12")):
            criar(f"{self.dir}/CAM1/2026-11-{dia}/{hora}-00-00.jpg", tamanho=100_000)
        # limite de ~300 KB (alvo 90% = ~270 KB): sobram as 2 mais novas
        m = carregar({"PAINEL_CONFIG": "/nao/existe", "PAINEL_GRAVAR_DIR": self.dir, "PAINEL_LIMITE_GB": "0.0003"})
        m.limpar_gravacoes()
        self.assertEqual(self.fotos(), ["CAM1/2026-11-14/11-00-00.jpg", "CAM1/2026-11-14/12-00-00.jpg"])

    def test_nao_apaga_nada_dentro_dos_limites(self):
        criar(f"{self.dir}/CAM1/2026-11-14/10-00-00.jpg")
        m = carregar({"PAINEL_CONFIG": "/nao/existe", "PAINEL_GRAVAR_DIR": self.dir})
        m.limpar_gravacoes()
        self.assertEqual(len(self.fotos()), 1)

    def test_pasta_inexistente_nao_quebra(self):
        m = carregar({"PAINEL_CONFIG": "/nao/existe", "PAINEL_GRAVAR_DIR": self.dir + "/nada"})
        m.limpar_gravacoes()


CAM = {"id": "CAM1", "nome": "Câmera 1", "host": "10.0.0.1", "porta": 554, "caminho": "/onvif1", "transporte": "udp"}


class TestConexaoContinua(unittest.TestCase):
    def test_padroes_de_gravacao(self):
        m = carregar({"PAINEL_CONFIG": "/nao/existe"})
        self.assertEqual((m.GRAVAR_INTERVALO, m.RETENCAO_DIAS, m.SO_CHAVES), (10, 5, True))

    def test_url_escapa_usuario_e_senha(self):
        m = carregar({"PAINEL_CONFIG": "/nao/existe"})
        url = m.url_rtsp(CAM, "adm@in", "p#s/s:w")
        self.assertEqual(url, "rtsp://adm%40in:p%23s%2Fs%3Aw@10.0.0.1:554/onvif1")

    def test_comando_decodifica_so_quadros_chave_antes_da_entrada(self):
        m = carregar({"PAINEL_CONFIG": "/nao/existe", "PAINEL_PAUSA": "10"})
        cmd = m.comando_continuo(CAM, "u", "p", 1174, 624)
        self.assertEqual(cmd[cmd.index("-skip_frame") + 1], "nokey")
        self.assertLess(cmd.index("-skip_frame"), cmd.index("-i"))  # é opção de entrada
        grafo = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("gte(t-prev_selected_t,10)", grafo)  # 1 quadro a cada PAUSA s
        self.assertIn("scale=1174:624", grafo)
        self.assertEqual(cmd.count("-atomic_writing"), 2)  # a tela nunca lê JPEG pela metade
        self.assertEqual(cmd[-1], f"{m.PASTA}/CAM1.jpg")
        saida_original = cmd[cmd.index("[f]") + 11]  # logo depois das opções do -map [f]
        self.assertTrue(saida_original.endswith("CAM1.full.jpg"))

    def test_quadros_chave_pode_ser_desligado(self):
        m = carregar({"PAINEL_CONFIG": "/nao/existe", "PAINEL_SO_QUADROS_CHAVE": "0"})
        self.assertNotIn("-skip_frame", m.comando_continuo(CAM, "u", "p", 1174, 624))


class TestBarra(unittest.TestCase):
    def test_barra_isolada_igual_ao_topo_do_quadro_completo(self):
        from unittest import mock
        from PIL import Image
        m = carregar({"PAINEL_CONFIG": "/nao/existe"})
        m.CAMERAS[:] = [CAM]
        m.estado["CAM1"] = {"ok": 1.0, "erro": None, "falhas": 0}
        larg, alt = 400, 300
        fundo = Image.effect_noise((larg, alt), 80).convert("RGB")
        topo = fundo.crop((0, 0, larg, m.BARRA))
        base = Image.blend(topo, Image.new("RGB", topo.size, (0, 0, 0)), 0.6)
        with mock.patch.object(m.time, "strftime", return_value="14/11  22:26:38"):
            quadro = m.montar_quadro(CAM, 0, larg, alt, fundo, base, 5.0)
            barra = m.desenhar_barra(CAM, 0, larg, base, 5.0)
        self.assertEqual(quadro.crop((0, 0, larg, m.BARRA)).tobytes(), barra.tobytes())

    def test_sem_sinal_nao_mexe_na_barra(self):
        from unittest import mock
        from PIL import Image
        m = carregar({"PAINEL_CONFIG": "/nao/existe"})
        m.CAMERAS[:] = [CAM]
        m.estado["CAM1"] = {"ok": 1.0, "erro": None, "falhas": 0}
        fundo = Image.new("RGB", (400, 300), (30, 60, 90))
        base = Image.new("RGB", (400, m.BARRA), (0, 0, 0))
        with mock.patch.object(m.time, "strftime", return_value="x"):
            ok = m.montar_quadro(CAM, 0, 400, 300, fundo, base, 5.0)
            velho = m.montar_quadro(CAM, 0, 400, 300, fundo, base, 5.0 + m.SEM_SINAL + 1)
        self.assertNotEqual(ok.crop((0, 230, 400, 300)).tobytes(), velho.crop((0, 230, 400, 300)).tobytes())


class TestGravacaoIntervalo(unittest.TestCase):
    def test_respeita_o_intervalo_entre_fotos(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as pasta, tempfile.TemporaryDirectory() as grav:
            m = carregar({"PAINEL_CONFIG": "/nao/existe", "PAINEL_GRAVAR_DIR": grav})
            m.PASTA, m.MIN_LIVRE_GB = pasta, 0
            criar(f"{pasta}/CAM1.full.jpg")
            with mock.patch.object(m, "relogio_sincronizado", return_value=True), \
                    mock.patch.object(m.shutil, "copyfile", wraps=m.shutil.copyfile) as cp:
                m.gravar(CAM)
                m.gravar(CAM)  # dentro do intervalo: não grava
                self.assertEqual(cp.call_count, 1)
                m._ultima_gravacao["CAM1"] -= m.GRAVAR_INTERVALO + 1  # intervalo vencido
                m.gravar(CAM)
                self.assertEqual(cp.call_count, 2)

    def test_nao_grava_com_relogio_nao_sincronizado(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as pasta, tempfile.TemporaryDirectory() as grav:
            m = carregar({"PAINEL_CONFIG": "/nao/existe", "PAINEL_GRAVAR_DIR": grav})
            m.PASTA = pasta
            criar(f"{pasta}/CAM1.full.jpg")
            with mock.patch.object(m, "relogio_sincronizado", return_value=False):
                m.gravar(CAM)
            self.assertEqual(os.listdir(grav), [])


if __name__ == "__main__":
    unittest.main()
