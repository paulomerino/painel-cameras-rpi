#!/usr/bin/env python3
"""Painel de câmeras no HDMI: rodízio de snapshots RTSP desenhado no framebuffer.

- Uma thread por câmera mantém um ffmpeg conectado (reinicia se cair ou travar)
  que grava 1 quadro a cada PAINEL_PAUSA s em /run/painel (RAM, não desgasta o
  cartão SD): o quadro reduzido para a tela e o quadro em resolução original,
  que é o que vai para o disco.
- O laço principal atualiza, 1x por segundo, só a barra com nome, relógio e
  "atualizado há Ns" direto em /dev/fb0 (sem desktop, sem navegador); a tela
  inteira é reescrita só quando a imagem muda.
- Opcionalmente grava cada snapshot em disco, com retenção por idade e por tamanho.
- Toda a configuração fica em /etc/painel-cameras.env (modo 600). Ver
  painel-cameras.env.example.

Uso:
  painel-cameras.py              roda o painel (é o que o serviço systemd faz)
  painel-cameras.py --verificar  lê a configuração e testa uma captura por câmera
"""
import fcntl
import functools
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse

from PIL import Image, ImageDraw, ImageFont

ENV_ARQ = os.environ.get("PAINEL_CONFIG", "/etc/painel-cameras.env")
FB = "/dev/fb0"
TTY = "/dev/tty1"
PASTA = "/run/painel"
FONTE = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
EX_CONFIG = 78  # código de saída "configuração inválida" (o systemd não reinicia)


def log(msg):
    print(msg, flush=True)


def ler_env(arquivo=None):
    dados = {}
    arquivo = arquivo or ENV_ARQ
    try:
        with open(arquivo, encoding="utf-8") as f:
            for linha in f:
                linha = linha.rstrip("\n")
                if "=" in linha and not linha.lstrip().startswith("#"):
                    k, v = linha.split("=", 1)
                    dados[k.strip()] = v
    except OSError as e:
        log(f"não consegui ler {arquivo}: {e.strerror}")
    return dados


_CFG = ler_env()


def opcao(nome, padrao):
    """Opção global: arquivo de configuração > variável de ambiente > padrão."""
    return _CFG.get(nome, os.environ.get(nome, padrao))


ROTACAO = int(opcao("PAINEL_ROTACAO", "10"))  # s de cada câmera na tela
PAUSA = int(opcao("PAINEL_PAUSA", "2"))  # s entre capturas
SEM_SINAL = 60  # s sem snapshot novo => "SEM SINAL"
BARRA = 48  # altura da barra superior (px)

# Gravação das fotos: <GRAVAR_DIR>/<CAM>/<AAAA-MM-DD>/<HH-MM-SS>.jpg
GRAVAR = opcao("PAINEL_GRAVAR", "1") == "1"
GRAVAR_DIR = opcao("PAINEL_GRAVAR_DIR", "/var/lib/painel")
RETENCAO_DIAS = int(opcao("PAINEL_RETENCAO_DIAS", "5"))
LIMITE_GB = float(opcao("PAINEL_LIMITE_GB", "8"))
MIN_LIVRE_GB = 2  # não grava se o disco tiver menos que isso livre
GRAVAR_INTERVALO = int(opcao("PAINEL_GRAVAR_INTERVALO", "10"))  # s entre fotos gravadas, por câmera
# Conexão contínua: o ffmpeg fica conectado e decodifica só os quadros-chave
# (barato no Pi 1). Desligar (0) decodifica todos os quadros.
SO_CHAVES = opcao("PAINEL_SO_QUADROS_CHAVE", "1") == "1"
TRAVADO = max(45, PAUSA * 4)  # s sem quadro novo => reinicia o ffmpeg

KDSETMODE, KD_TEXT, KD_GRAPHICS = 0x4B3A, 0, 1

parar = threading.Event()
CAMERAS = []
estado = {}


def carregar_cameras(env):
    """Descobre as câmeras pelas chaves CAMn_USER: precisa de CAMn_HOST também."""
    ids = sorted(
        (k[:-5] for k in env if re.fullmatch(r"CAM\d+_USER", k)),
        key=lambda c: int(c[3:]),
    )
    cams = []
    for cid in ids:
        host = env.get(cid + "_HOST", "").strip()
        if not host:
            log(f"{cid}: falta {cid}_HOST no arquivo de configuração (câmera ignorada)")
            continue
        transporte = env.get(cid + "_TRANSPORTE", "udp").strip().lower()
        if transporte not in ("udp", "tcp"):
            log(f"{cid}: {cid}_TRANSPORTE deve ser udp ou tcp (usando udp)")
            transporte = "udp"
        cams.append({
            "id": cid,
            "nome": env.get(cid + "_NOME", f"Câmera {cid[3:]}"),
            "host": host,
            "porta": int(env.get(cid + "_PORTA", "554")),
            "caminho": env.get(cid + "_CAMINHO", "/onvif1"),
            "transporte": transporte,
        })
    return cams


def tela_info():
    base = "/sys/class/graphics/fb0/"
    larg, alt = (int(x) for x in open(base + "virtual_size").read().split(","))
    bpp = int(open(base + "bits_per_pixel").read())
    stride = int(open(base + "stride").read())
    return larg, alt, bpp, stride


def modo_tty(modo):
    try:
        fd = os.open(TTY, os.O_RDWR)
        try:
            fcntl.ioctl(fd, KDSETMODE, modo)
        finally:
            os.close(fd)
    except OSError:
        pass


def limpar(texto, senha):
    if senha:
        texto = texto.replace(senha, "***").replace(urllib.parse.quote(senha, safe=""), "***")
    return " ".join(texto.split())[:160]


def url_rtsp(cam, usuario, senha):
    return "rtsp://%s:%s@%s:%d%s" % (
        urllib.parse.quote(usuario, safe=""),
        urllib.parse.quote(senha, safe=""),
        cam["host"],
        cam["porta"],
        cam["caminho"],
    )


def capturar(cam, usuario, senha, larg, alt):
    """Uma captura avulsa (usada só por --verificar)."""
    url = url_rtsp(cam, usuario, senha)
    destino =f"{PASTA}/{cam['id']}.jpg"
    tmp = f"{PASTA}/{cam['id']}.tmp.jpg"
    destino_full = f"{PASTA}/{cam['id']}.full.jpg"
    tmp_full = f"{PASTA}/{cam['id']}.full.tmp.jpg"
    # Uma única captura, duas saídas: o quadro original (para gravar em disco)
    # e o reduzido para a tela (TV).
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-rtsp_transport", cam["transporte"], "-timeout", "15000000",
        "-i", url,
        "-map", "0:v:0", "-frames:v", "1", "-q:v", "1", tmp_full,
        "-map", "0:v:0", "-frames:v", "1",
        "-vf", f"scale={larg}:{alt}:force_original_aspect_ratio=decrease:flags=lanczos",
        "-q:v", "2", tmp,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
    except subprocess.TimeoutExpired:
        return "tempo esgotado"
    if r.returncode != 0:
        return limpar(r.stderr, senha) or f"ffmpeg saiu com {r.returncode}"
    os.replace(tmp_full, destino_full)
    os.replace(tmp, destino)
    return None


_relogio = {"t": -1e9, "ok": False}
_aviso = {"disco": False, "hora": False}
_ultima_gravacao = {}  # id da câmera -> time.monotonic() da última foto gravada


def relogio_sincronizado():
    """True se o NTP já acertou a hora (o Pi não tem relógio de bateria)."""
    agora = time.monotonic()
    if agora - _relogio["t"] > (300 if _relogio["ok"] else 15):
        _relogio["t"] = agora
        try:
            r = subprocess.run(
                ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                capture_output=True, text=True, timeout=10,
            )
            _relogio["ok"] = r.stdout.strip() == "yes"
        except (OSError, subprocess.TimeoutExpired):
            _relogio["ok"] = False
    return _relogio["ok"]


def gravar(cam):
    """Copia o último snapshot (que está na RAM) para o disco, com data e hora."""
    if not GRAVAR:
        return
    if not relogio_sincronizado():
        if not _aviso["hora"]:
            log("gravação em espera: relógio ainda não sincronizado")
            _aviso["hora"] = True
        return
    _aviso["hora"] = False
    agora_m = time.monotonic()
    if agora_m - _ultima_gravacao.get(cam["id"], -1e9) < GRAVAR_INTERVALO:
        return
    try:
        if shutil.disk_usage(GRAVAR_DIR).free < MIN_LIVRE_GB * 1024**3:
            if not _aviso["disco"]:
                log(f"gravação pausada: menos de {MIN_LIVRE_GB} GB livres no disco")
                _aviso["disco"] = True
            return
        _aviso["disco"] = False
        agora = time.localtime()
        pasta = os.path.join(GRAVAR_DIR, cam["id"], time.strftime("%Y-%m-%d", agora))
        os.makedirs(pasta, exist_ok=True)
        destino = os.path.join(pasta, time.strftime("%H-%M-%S", agora) + ".jpg")
        tmp = destino + ".tmp"
        origem = f"{PASTA}/{cam['id']}.full.jpg"  # resolução original da câmera
        if not os.path.exists(origem):
            origem = f"{PASTA}/{cam['id']}.jpg"
        shutil.copyfile(origem, tmp)
        os.replace(tmp, destino)
        _ultima_gravacao[cam["id"]] = agora_m
    except OSError as e:
        log(f"{cam['nome']}: não consegui gravar a foto ({e.strerror})")


def _apagar(caminho):
    try:
        os.unlink(caminho)
        return True
    except OSError:
        return False


def _dias(base):
    try:
        return sorted(e.path for e in os.scandir(base) if e.is_dir())
    except OSError:
        return []


def limpar_gravacoes():
    """Apaga fotos com mais de RETENCAO_DIAS e, se ainda passar do limite, as mais antigas.

    Não monta lista de arquivos em memória (a RAM do Pi é curta): os nomes já
    ordenam por data, então basta percorrer as pastas em ordem.
    """
    corte = time.time() - RETENCAO_DIAS * 86400
    limite = int(LIMITE_GB * 1024**3)
    total = apagadas_idade = apagadas_tam = 0
    camaras = _dias(GRAVAR_DIR)
    for cdir in camaras:  # 1ª passada: por idade, somando o que sobra
        for ddir in _dias(cdir):
            for e in os.scandir(ddir):
                st = e.stat()
                if st.st_mtime < corte:
                    apagadas_idade += _apagar(e.path)
                else:
                    total += st.st_size
    if total > limite:  # 2ª passada: por tamanho, do mais antigo
        alvo = int(limite * 0.9)
        for cdir in camaras:
            for ddir in _dias(cdir):
                for nome in sorted(os.listdir(ddir)):
                    if total <= alvo:
                        break
                    caminho = os.path.join(ddir, nome)
                    try:
                        tam = os.stat(caminho).st_size
                    except OSError:
                        continue
                    if _apagar(caminho):
                        total -= tam
                        apagadas_tam += 1
    for cdir in camaras:  # remove pastas de dia vazias
        for ddir in _dias(cdir):
            try:
                os.rmdir(ddir)
            except OSError:
                pass
    if apagadas_idade or apagadas_tam:
        log(f"limpeza: {apagadas_idade} fotos por idade (>{RETENCAO_DIAS}d), {apagadas_tam} por tamanho; "
            f"restam {total / 1024**2:.1f} MB")


def laco_limpeza():
    while not parar.is_set():
        try:
            limpar_gravacoes()
        except Exception as e:  # a limpeza nunca pode derrubar o painel
            log(f"erro na limpeza: {e!r}")
        parar.wait(3600)


def comando_continuo(cam, usuario, senha, larg, alt):
    """ffmpeg que fica conectado e grava 1 quadro a cada PAUSA s (original + reduzido).

    - `-skip_frame nokey`: decodifica só quadros-chave, o que o Pi 1 aguenta.
    - `select`: pega um quadro a cada PAUSA s sem duplicar nenhum (o filtro fps
      repetiria o último quadro e a imagem velha pareceria "atualizada agora").
    - `-atomic_writing 1`: grava em arquivo temporário e renomeia, então o laço
      de desenho nunca lê um JPEG pela metade.
    """
    destino = f"{PASTA}/{cam['id']}.jpg"
    destino_full = f"{PASTA}/{cam['id']}.full.jpg"
    grafo = (
        f"[0:v:0]select='isnan(prev_selected_t)+gte(t-prev_selected_t,{PAUSA})',split=2[f][r];"
        f"[r]scale={larg}:{alt}:force_original_aspect_ratio=decrease:flags=bilinear[t]"
    )
    saida = ["-fps_mode", "passthrough", "-q:v", "3", "-f", "image2", "-update", "1", "-atomic_writing", "1"]
    cmd = ["ffmpeg", "-y", "-v", "error", "-rtsp_transport", cam["transporte"], "-timeout", "15000000"]
    if SO_CHAVES:
        cmd += ["-skip_frame", "nokey"]
    cmd += ["-i", url_rtsp(cam, usuario, senha), "-filter_complex", grafo]
    cmd += ["-map", "[f]", *saida, destino_full, "-map", "[t]", *saida, destino]
    return cmd


def _encerrar(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def laco_camera(cam, larg, alt):
    """Mantém o ffmpeg da câmera conectado; reinicia se cair ou travar."""
    st = estado[cam["id"]]
    destino = f"{PASTA}/{cam['id']}.jpg"
    log_err = f"{PASTA}/{cam['id']}.err"
    while not parar.is_set():
        env = ler_env()
        usuario, senha = env.get(cam["id"] + "_USER"), env.get(cam["id"] + "_PASS")
        if not usuario or not senha:
            st["erro"] = "sem credenciais"
            if st["falhas"] == 0:
                log(f"{cam['nome']}: sem credenciais em {ENV_ARQ}")
            st["falhas"] += 1
            parar.wait(10)
            continue

        def mtime():
            try:
                return os.stat(destino).st_mtime_ns
            except OSError:
                return None

        visto = mtime()  # quadro de uma execução anterior não conta como novo
        inicio = time.monotonic()
        recebeu = False
        with open(log_err, "w") as ferr:
            proc = subprocess.Popen(
                comando_continuo(cam, usuario, senha, larg, alt),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=ferr,
            )
        try:
            while not parar.is_set() and proc.poll() is None:
                parar.wait(1)
                mt = mtime()
                if mt is not None and mt != visto:
                    visto = mt
                    recebeu = True
                    if st["falhas"]:
                        log(f"{cam['nome']}: sinal restabelecido")
                    elif st["ok"] is None:
                        log(f"{cam['nome']}: primeiro snapshot ok")
                    st.update(ok=time.monotonic(), erro=None, falhas=0)
                    gravar(cam)
                ultimo = max(inicio, st["ok"] or 0)
                if time.monotonic() - ultimo > TRAVADO:
                    st["erro"] = "sem quadros novos"
                    break  # o finally derruba o ffmpeg travado
        finally:
            _encerrar(proc)
        if parar.is_set():
            break
        try:
            with open(log_err, encoding="utf-8", errors="replace") as f:
                erro = limpar(f.read(), senha)
        except OSError:
            erro = ""
        erro = erro or st["erro"] or f"ffmpeg saiu com {proc.returncode}"
        if st["falhas"] == 0:
            log(f"{cam['nome']}: conexão perdida ({erro})")
        st["erro"] = erro
        st["falhas"] += 1
        parar.wait(min(60, 5 * st["falhas"]))


@functools.lru_cache(maxsize=None)
def fonte(tam):  # em cache: abrir o arquivo da fonte a cada desenho pesa no Pi 1
    try:
        return ImageFont.truetype(FONTE, tam)
    except OSError:
        return ImageFont.load_default()


def centralizar(draw, larg, alt, texto, f, cor):
    caixa = draw.textbbox((0, 0), texto, font=f)
    draw.text(((larg - (caixa[2] - caixa[0])) // 2, (alt - (caixa[3] - caixa[1])) // 2), texto, font=f, fill=cor)


def carregar_fundo(cam, larg, alt, cache):
    """Atualiza o cache com o snapshot mais recente da câmera.

    Devolve (fundo, base_da_barra, mt): a imagem da câmera centralizada na tela
    (ou None), o trecho da barra já escurecido e o mtime do snapshot, que serve
    para saber se a imagem mudou desde o último desenho.
    """
    arq = f"{PASTA}/{cam['id']}.jpg"
    try:
        mt = os.stat(arq).st_mtime_ns
    except OSError:
        mt = None
    if mt is not None and cache.get(cam["id"], (None,))[0] != mt:
        try:
            snap = Image.open(arq).convert("RGB")
            fundo = Image.new("RGB", (larg, alt), (0, 0, 0))
            fundo.paste(snap, ((larg - snap.width) // 2, (alt - snap.height) // 2))
            topo = fundo.crop((0, 0, larg, BARRA))
            base = Image.blend(topo, Image.new("RGB", topo.size, (0, 0, 0)), 0.6)
            cache[cam["id"]] = (mt, fundo, base)
        except OSError:
            pass  # arquivo sendo trocado; mantém o anterior
    ent = cache.get(cam["id"])
    if ent is None:
        return None, Image.new("RGB", (larg, BARRA), (0, 0, 0)), None
    return ent[1], ent[2], ent[0]


def montar_quadro(cam, indice, larg, alt, fundo, base_barra, idade):
    """Devolve a imagem RGB completa da tela para a câmera da vez."""
    st = estado[cam["id"]]
    quadro = fundo.copy() if fundo else Image.new("RGB", (larg, alt), (0, 0, 0))
    draw = ImageDraw.Draw(quadro)
    if fundo is None:
        centralizar(draw, larg, alt, "Conectando à câmera…" if not st["erro"] else "SEM SINAL", fonte(40), (200, 200, 200))
    elif idade is not None and idade > SEM_SINAL:
        draw.rectangle((0, alt - 70, larg, alt), fill=(150, 0, 0))
        centralizar(draw, larg, alt * 2 - 70, "SEM SINAL — imagem desatualizada", fonte(30), (255, 255, 255))
    quadro.paste(desenhar_barra(cam, indice, larg, base_barra, idade), (0, 0))
    return quadro


def desenhar_barra(cam, indice, larg, base_barra, idade):
    """Barra superior (nome, 'atualizado há Ns', relógio): é só ela que muda a cada segundo."""
    barra = base_barra.copy()
    dbarra = ImageDraw.Draw(barra)
    titulo = cam["nome"] + (f"  ({indice + 1}/{len(CAMERAS)})" if len(CAMERAS) > 1 else "")
    dbarra.text((14, 8), titulo, font=fonte(28), fill=(255, 255, 255))
    if idade is not None:
        txt = "atualizado agora" if idade < 3 else f"atualizado há {int(idade)}s"
        cor = (255, 120, 120) if idade > SEM_SINAL else (190, 190, 190)
        caixa = dbarra.textbbox((0, 0), txt, font=fonte(18))
        dbarra.text((larg // 2 - (caixa[2] - caixa[0]) // 2, 15), txt, font=fonte(18), fill=cor)
    relogio = time.strftime("%d/%m  %H:%M:%S")
    caixa = dbarra.textbbox((0, 0), relogio, font=fonte(28))
    dbarra.text((larg - (caixa[2] - caixa[0]) - 14, 8), relogio, font=fonte(28), fill=(255, 255, 255))
    return barra


def verificar(cams):
    """Modo --verificar: mostra a configuração (sem senhas) e testa uma captura por câmera."""
    os.makedirs(PASTA, exist_ok=True)
    try:
        larg, alt = tela_info()[:2]
    except (OSError, ValueError):
        larg, alt = 1280, 720
    log(f"configuração: {ENV_ARQ}")
    log(f"rodízio {ROTACAO}s | gravação {'ligada em ' + GRAVAR_DIR if GRAVAR else 'desligada'}"
        f" | guarda {RETENCAO_DIAS} dias, limite {LIMITE_GB:g} GB")
    falhas = 0
    for cam in cams:
        usuario, senha = _CFG.get(cam["id"] + "_USER"), _CFG.get(cam["id"] + "_PASS")
        destino = f"rtsp://{cam['host']}:{cam['porta']}{cam['caminho']} ({cam['transporte']})"
        if not usuario or not senha:
            log(f"[FALHOU] {cam['nome']} {destino}: falta {cam['id']}_USER ou {cam['id']}_PASS")
            falhas += 1
            continue
        t = time.monotonic()
        erro = capturar(cam, usuario, senha, larg, alt)
        if erro:
            log(f"[FALHOU] {cam['nome']} {destino}: {erro}")
            falhas += 1
        else:
            kb = os.path.getsize(f"{PASTA}/{cam['id']}.jpg") / 1024
            log(f"[OK]     {cam['nome']} {destino}: {kb:.0f} KB em {time.monotonic() - t:.1f}s")
    return 1 if falhas else 0


def main(argv):
    cams = carregar_cameras(_CFG)
    if not cams:
        log(f"nenhuma câmera configurada em {ENV_ARQ} (precisa de CAM1_USER, CAM1_PASS e CAM1_HOST)")
        return EX_CONFIG
    CAMERAS[:] = cams
    estado.update({c["id"]: {"ok": None, "erro": None, "falhas": 0} for c in cams})
    if "--verificar" in argv:
        return verificar(cams)

    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, lambda *_: parar.set())
    os.makedirs(PASTA, exist_ok=True)
    subprocess.run(["/usr/bin/vcgencmd", "display_power", "1"], capture_output=True)
    larg, alt, bpp, stride = tela_info()
    if bpp != 32 or stride % 4 or stride < larg * 4:
        log(f"framebuffer inesperado: {larg}x{alt} {bpp}bpp stride={stride}")
        return EX_CONFIG
    larg_buf = stride // 4  # o fb tem preenchimento no fim de cada linha (ex.: 1184 px para 1174)
    log(f"painel iniciado: tela {larg}x{alt}, {len(CAMERAS)} câmera(s), rodízio {ROTACAO}s")

    if GRAVAR:
        os.makedirs(GRAVAR_DIR, exist_ok=True)
        log(f"gravação ligada: {GRAVAR_DIR}, guarda {RETENCAO_DIAS} dias, limite {LIMITE_GB:g} GB")
        threading.Thread(target=laco_limpeza, daemon=True).start()

    modo_tty(KD_GRAPHICS)  # impede o console de desenhar por cima
    for cam in CAMERAS:
        threading.Thread(target=laco_camera, args=(cam, larg, alt), daemon=True).start()
    cache = {}
    fb = os.open(FB, os.O_WRONLY)

    def escrever(img, altura):
        """Escreve `img` (larg x altura) a partir do topo do framebuffer."""
        if larg_buf != larg:  # o fb tem preenchimento no fim de cada linha
            tela = Image.new("RGB", (larg_buf, altura))
            tela.paste(img, (0, 0))
            img = tela
        os.pwrite(fb, img.tobytes("raw", "BGRX"), 0)

    # Só a barra muda a cada segundo (relógio e "atualizado há Ns"). A tela
    # inteira só é reescrita quando a imagem muda: foto nova, troca de câmera ou
    # de aviso. Como as linhas do fb são contíguas, as primeiras BARRA linhas
    # são os primeiros bytes do buffer. Refresh completo a cada REFRESH_S s
    # cura qualquer coisa que tenha estragado o framebuffer.
    REFRESH_S = 60
    chave_anterior, ultimo_total = None, 0.0
    try:
        while not parar.is_set():
            indice = int(time.time() // ROTACAO) % len(CAMERAS)
            cam = CAMERAS[indice]
            st = estado[cam["id"]]
            fundo, base_barra, mt = carregar_fundo(cam, larg, alt, cache)
            idade = (time.monotonic() - st["ok"]) if st["ok"] else None
            chave = (
                indice, mt, fundo is None,
                bool(st["erro"]) if fundo is None else None,
                idade is not None and idade > SEM_SINAL,
            )
            agora = time.monotonic()
            if chave != chave_anterior or agora - ultimo_total > REFRESH_S:
                escrever(montar_quadro(cam, indice, larg, alt, fundo, base_barra, idade), alt)
                chave_anterior, ultimo_total = chave, agora
            else:
                escrever(desenhar_barra(cam, indice, larg, base_barra, idade), BARRA)
            parar.wait(1 - (time.time() % 1))
    finally:
        os.close(fb)
        modo_tty(KD_TEXT)
        log("painel encerrado")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
