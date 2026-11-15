#!/usr/bin/env python3
"""Painel de câmeras no HDMI: rodízio de snapshots RTSP desenhado no framebuffer.

- Uma thread captura 1 quadro por câmera (ffmpeg) e grava em /run/painel
  (RAM, não desgasta o cartão SD).
- O laço principal desenha, 1x por segundo, a câmera da vez + barra com nome,
  relógio e "atualizado há Ns" direto em /dev/fb0 (sem desktop, sem navegador).
- Opcionalmente grava cada snapshot em disco, com retenção por idade e por tamanho.
- Toda a configuração fica em /etc/painel-cameras.env (modo 600). Ver
  painel-cameras.env.example.

Uso:
  painel-cameras.py              roda o painel (é o que o serviço systemd faz)
  painel-cameras.py --verificar  lê a configuração e testa uma captura por câmera
"""
import fcntl
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
RETENCAO_DIAS = int(opcao("PAINEL_RETENCAO_DIAS", "7"))
LIMITE_GB = float(opcao("PAINEL_LIMITE_GB", "8"))
MIN_LIVRE_GB = 2  # não grava se o disco tiver menos que isso livre

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


def capturar(cam, usuario, senha, larg, alt):
    url = "rtsp://%s:%s@%s:%d%s" % (
        urllib.parse.quote(usuario, safe=""),
        urllib.parse.quote(senha, safe=""),
        cam["host"],
        cam["porta"],
        cam["caminho"],
    )
    destino = f"{PASTA}/{cam['id']}.jpg"
    tmp = f"{PASTA}/{cam['id']}.tmp.jpg"
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-rtsp_transport", cam["transporte"], "-timeout", "15000000",
        "-i", url, "-frames:v", "1",
        "-vf", f"scale={larg}:{alt}:force_original_aspect_ratio=decrease",
        "-q:v", "4", tmp,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
    except subprocess.TimeoutExpired:
        return "tempo esgotado"
    if r.returncode != 0:
        return limpar(r.stderr, senha) or f"ffmpeg saiu com {r.returncode}"
    os.replace(tmp, destino)
    return None


_relogio = {"t": -1e9, "ok": False}
_aviso = {"disco": False, "hora": False}


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
        shutil.copyfile(f"{PASTA}/{cam['id']}.jpg", tmp)
        os.replace(tmp, destino)
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


def laco_captura(larg, alt):
    env = ler_env()
    while not parar.is_set():
        for cam in CAMERAS:
            if parar.is_set():
                break
            st = estado[cam["id"]]
            usuario = env.get(cam["id"] + "_USER")
            senha = env.get(cam["id"] + "_PASS")
            if not usuario or not senha:
                st["erro"] = "sem credenciais"
                if st["falhas"] == 0:
                    log(f"{cam['nome']}: sem credenciais em {ENV_ARQ}")
                st["falhas"] += 1
                parar.wait(10)
                env = ler_env()
                continue
            erro = capturar(cam, usuario, senha, larg, alt)
            if erro is None:
                if st["falhas"]:
                    log(f"{cam['nome']}: sinal restabelecido")
                elif st["ok"] is None:
                    log(f"{cam['nome']}: primeiro snapshot ok")
                st.update(ok=time.monotonic(), erro=None, falhas=0)
                gravar(cam)
            else:
                if st["falhas"] == 0:
                    log(f"{cam['nome']}: falha na captura ({erro})")
                st["erro"] = erro
                st["falhas"] += 1
            parar.wait(min(60, PAUSA * (1 + st["falhas"])))


def fonte(tam):
    try:
        return ImageFont.truetype(FONTE, tam)
    except OSError:
        return ImageFont.load_default()


def centralizar(draw, larg, alt, texto, f, cor):
    caixa = draw.textbbox((0, 0), texto, font=f)
    draw.text(((larg - (caixa[2] - caixa[0])) // 2, (alt - (caixa[3] - caixa[1])) // 2), texto, font=f, fill=cor)


def montar_quadro(cam, indice, larg, alt, cache):
    """Devolve a imagem RGB completa da tela para a câmera da vez."""
    st = estado[cam["id"]]
    arq = f"{PASTA}/{cam['id']}.jpg"
    try:
        mt = os.stat(arq).st_mtime_ns
    except OSError:
        mt = None
    if mt is not None and cache.get(cam["id"], (None, None))[0] != mt:
        try:
            snap = Image.open(arq).convert("RGB")
            fundo = Image.new("RGB", (larg, alt), (0, 0, 0))
            fundo.paste(snap, ((larg - snap.width) // 2, (alt - snap.height) // 2))
            cache[cam["id"]] = (mt, fundo)
        except OSError:
            pass  # arquivo sendo trocado; mantém o anterior
    fundo = cache.get(cam["id"], (None, None))[1]
    quadro = fundo.copy() if fundo else Image.new("RGB", (larg, alt), (0, 0, 0))
    draw = ImageDraw.Draw(quadro)

    agora = time.monotonic()
    idade = (agora - st["ok"]) if st["ok"] else None
    if fundo is None:
        centralizar(draw, larg, alt, "Conectando à câmera…" if not st["erro"] else "SEM SINAL", fonte(40), (200, 200, 200))
    elif idade is not None and idade > SEM_SINAL:
        draw.rectangle((0, alt - 70, larg, alt), fill=(150, 0, 0))
        centralizar(draw, larg, alt * 2 - 70, "SEM SINAL — imagem desatualizada", fonte(30), (255, 255, 255))

    barra = quadro.crop((0, 0, larg, BARRA))
    barra = Image.blend(barra, Image.new("RGB", barra.size, (0, 0, 0)), 0.6)
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
    quadro.paste(barra, (0, 0))
    return quadro


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
    threading.Thread(target=laco_captura, args=(larg, alt), daemon=True).start()
    cache = {}
    fb = os.open(FB, os.O_WRONLY)
    try:
        while not parar.is_set():
            indice = int(time.time() // ROTACAO) % len(CAMERAS)
            quadro = montar_quadro(CAMERAS[indice], indice, larg, alt, cache)
            if larg_buf != larg:
                tela = Image.new("RGB", (larg_buf, alt))
                tela.paste(quadro, (0, 0))
                quadro = tela
            os.pwrite(fb, quadro.tobytes("raw", "BGRX"), 0)
            parar.wait(1 - (time.time() % 1))
    finally:
        os.close(fb)
        modo_tty(KD_TEXT)
        log("painel encerrado")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
