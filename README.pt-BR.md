# painel-cameras-rpi

Português · [English](README.md)

Transforma um Raspberry Pi antigo (testado num **Pi 1 Model B, ARMv6, ~175 MB de RAM**) num painel de
câmeras IP para uma TV/monitor HDMI: mostra **snapshots em rodízio**, grava as fotos em disco e apaga as
antigas sozinho. Sem desktop, sem navegador, sem Docker: um script Python que desenha direto no
framebuffer, e o `ffmpeg` para buscar os quadros.

```
┌──────────────────────────────────────────────────────────────┐
│ Câmera 1        atualizado há 4s            14/11  22:26:38 │  ← barra: nome, idade da imagem, relógio
├──────────────────────────────────────────────────────────────┤
│                                                              │
│                  último snapshot da câmera                   │
│                  (a imagem não é vídeo)                      │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

## O que faz

- **Rodízio de snapshots RTSP** entre uma ou mais câmeras. Chega uma imagem nova a cada ~12 s num Pi 1 com a
  câmera de teste: o ritmo segue os quadros-chave da câmera (ver "Como funciona").
- **Barra** com nome da câmera, "atualizado há Ns" e relógio. Mostra **SEM SINAL** se a imagem ficar velha (60 s).
- **Grava as fotos** em `/var/lib/painel/<câmera>/<AAAA-MM-DD>/<HH-MM-SS>.jpg` (no máximo uma a cada
  `PAINEL_GRAVAR_INTERVALO`, padrão 10 s), com **retenção por idade** (padrão 5 dias) e **limite de tamanho**
  (padrão 8 GB, apaga as mais antigas primeiro).
  As fotos são gravadas na **resolução original da câmera**; a TV recebe uma cópia reduzida do mesmo quadro.
- Só grava com o **relógio sincronizado** (o Pi não tem relógio de bateria) e com mais de 2 GB livres.
- A configuração é um único arquivo `/etc/painel-cameras.env` (modo 600), com as senhas fora do código.

> **Não é vídeo ao vivo.** É um quadro parado que se renova a cada ~10 s ou mais. O painel mantém um `ffmpeg`
> conectado por câmera e decodifica **só os quadros-chave** (`-skip_frame nokey`). Decodificar todos os quadros
> não foi testado num Pi 1; se desligar o modo de quadros-chave (`PAINEL_SO_QUADROS_CHAVE=0`), espere um uso de
> CPU muito maior.

## Requisitos

- Raspberry Pi com **Raspberry Pi OS Lite** (Debian 12 *bookworm*) e saída HDMI. Testado em Pi 1 Model B (armv6).
- Câmera(s) IP com **RTSP** e **H.264** (testado com uma Yoosee, `rtsp://IP:554/onvif1`, autenticação Digest).
- Acesso `sudo` no Pi. As dependências (`ffmpeg`, `python3-pil`) são instaladas pelo `install.sh`.

## Instalação

No Pi:

```bash
git clone https://github.com/paulomerino/painel-cameras-rpi.git
cd painel-cameras-rpi
sudo ./install.sh
sudo nano /etc/painel-cameras.env                       # coloque IP, usuário e senha da câmera
sudo python3 /usr/local/bin/painel-cameras.py --verificar   # testa uma captura por câmera
sudo systemctl start painel-cameras
```

O serviço fica habilitado no boot. Acompanhe com `journalctl -u painel-cameras -f`.

## Configuração

Tudo fica em `/etc/painel-cameras.env` (veja [`painel-cameras.env.example`](painel-cameras.env.example)).
Uma câmera por prefixo `CAM1_`, `CAM2_`, …; a senha é lida literalmente (sem aspas nem escape).

| Chave | Padrão | O que faz |
|---|---|---|
| `CAMn_HOST` | — (obrigatória) | IP ou nome da câmera |
| `CAMn_USER` / `CAMn_PASS` | — (obrigatórias) | Credenciais RTSP |
| `CAMn_NOME` | `Câmera n` | Nome na barra |
| `CAMn_PORTA` | `554` | Porta RTSP |
| `CAMn_CAMINHO` | `/onvif1` | Caminho do stream (varia por fabricante) |
| `CAMn_TRANSPORTE` | `udp` | `udp` ou `tcp` (ver "Notas técnicas") |
| `PAINEL_ROTACAO` | `10` | Segundos de cada câmera na tela |
| `PAINEL_PAUSA` | `2` | Segundos mínimos entre os quadros que o `ffmpeg` entrega (no Pi 1, `10` é um bom valor; o ritmo real segue os quadros-chave da câmera) |
| `PAINEL_SO_QUADROS_CHAVE` | `1` | `1` decodifica só quadros-chave (leve, necessário no Pi 1); `0` decodifica todos |
| `PAINEL_GRAVAR` | `1` | `0` desliga a gravação |
| `PAINEL_GRAVAR_DIR` | `/var/lib/painel` | Onde guardar as fotos |
| `PAINEL_GRAVAR_INTERVALO` | `10` | Segundos mínimos entre fotos gravadas, por câmera |
| `PAINEL_RETENCAO_DIAS` | `5` | Apaga fotos mais antigas que isso |
| `PAINEL_LIMITE_GB` | `8` | Teto de espaço; passou, apaga as mais antigas |

Depois de mudar o arquivo: `sudo systemctl restart painel-cameras`.

## Como funciona

```
 câmera(s) ──RTSP/UDP──► ffmpeg (fica conectado, só quadros-chave) ──► /run/painel/CAMn.jpg       (RAM, não gasta o SD)
                                                                    │   /run/painel/CAMn.full.jpg  (resolução original)
                                                                    │                │
                                              redesenha a barra 1x/s │                └─► copia para /var/lib/painel/... (se gravar)
                                                                    ▼
                                                  PIL ──► /dev/fb0 (framebuffer) ──► HDMI
```

- Uma thread **por câmera** mantém um `ffmpeg` conectado. Ele decodifica só os quadros-chave
  (`-skip_frame nokey`), pega um quadro a cada `PAINEL_PAUSA` segundos sem nunca repetir um quadro (um quadro
  repetido pareceria "atualizado agora" estando velho) e grava de forma atômica (`-atomic_writing 1`), então o
  painel nunca lê um JPEG pela metade.
- Um vigia reinicia o `ffmpeg` se ele sair ou se ficar `max(45, 4 × PAINEL_PAUSA)` s sem quadro novo, com espera
  crescente de até 60 s enquanto a câmera estiver fora do ar.
- O laço principal reescreve **só as 48 linhas da barra superior** uma vez por segundo (relógio e "atualizado
  há Ns"). A tela inteira só é reescrita quando a imagem muda (quadro novo, troca de câmera, SEM SINAL), mais um
  refresh completo a cada 60 s por segurança.
- Com várias câmeras há um `ffmpeg` por câmera; isso só foi medido com uma.
- O console de texto é colocado em modo gráfico (`KD_GRAPHICS`) para não desenhar por cima do painel.
- A limpeza das fotos roda de hora em hora e **não carrega a lista de arquivos na memória**: os nomes já
  ordenam por data, então basta percorrer as pastas em ordem.
- `--verificar` continua fazendo uma única captura avulsa por câmera.

## Desempenho medido (Pi 1 Model B, uma câmera)

| Item | Valor |
|---|---|
| Fluxo da câmera de teste | H.264, 1280×720, 10 fps (`ffprobe`) |
| Tempo até o primeiro quadro após (re)iniciar | ~18–24 s (conectar + esperar um quadro-chave) |
| Quadro novo | a cada ~12 s (segue os quadros-chave da câmera) |
| CPU do `ffmpeg` (contínuo, só quadros-chave) | ~24 % de um núcleo (teste de 90 s) |
| CPU do serviço todo | ~27–29 % de um núcleo (janelas de 60 s); a versão anterior, de uma captura por foto, usava ~60 % com `PAINEL_PAUSA=10` |
| Carga média | ~1 (era ~3,5) |
| Tamanho médio de uma foto | ~200 KB na resolução original (amostra de um quadro: 198 KB; amostra antiga de 6.447 fotos: ~200 KB) |

A RAM não foi medida de novo nesta versão. Avisos do decodificador como `Too short data for FU-A H.264 RTP
packet` aparecem de vez em quando (perda de pacotes UDP); o `ffmpeg` continua rodando, e um quadro danificado
pode mostrar artefatos.

## Espaço das fotos

Estimativa com ~200 KB por foto (varia com a cena; à noite, com infravermelho, pode mudar). Com a câmera de
teste, uma foto é gravada a cada ~12 s:

| Intervalo | Fotos por dia | Por dia | 5 dias (padrão) |
|---|---|---|---|
| ~12 s (todo quadro) | ~7.200 | ~1,4 GB | ~7,2 GB |
| 30 s | 2.880 | ~0,6 GB | ~2,9 GB |
| 1 min | 1.440 | ~0,3 GB | ~1,4 GB |

Com o teto padrão de 8 GB, 5 dias de fotos a cada ~12 s ficam no limite: se passar, as fotos mais antigas são
apagadas primeiro. Aumente `PAINEL_GRAVAR_INTERVALO` ou reduza `PAINEL_RETENCAO_DIAS` para ter mais folga.

## Acessar as fotos

Não precisa instalar FTP: o SSH do Pi já traz **SFTP**, com a mesma chave SSH e criptografia.

```bash
scp -r pi@IP-DO-PI:/var/lib/painel/CAM1/2026-11-14 ./fotos
```

Ou use um cliente gráfico em modo **SFTP** (FileZilla, WinSCP). Evite FTP simples: manda a senha sem
criptografia. Não exponha o Pi à internet; para acesso remoto, use uma VPN (WireGuard, Tailscale).

## Notas técnicas (armadilhas já encontradas)

- **RTSP só funcionou em UDP** na câmera de teste. Com `tcp`, o `ffmpeg` falha com
  `Nonmatching transport in server reply`. Se a sua câmera for o contrário, use `CAMn_TRANSPORTE=tcp`.
- **Credencial errada** em algumas câmeras devolve `400 Bad Request` (não `401`); confira usuário e senha
  antes de suspeitar do protocolo. Só o caminho certo do stream funciona; outros passam no `DESCRIBE`
  e falham no `SETUP`.
- **Framebuffer com preenchimento (stride):** numa TV de 1174×624 o stride era 4736 bytes (1184 px), e não
  1174×4. O código calcula o stride lendo `/sys/class/graphics/fb0/stride`.
- **Formato dos pixels:** o fb está em BGRA (vermelho no offset 16), então o painel escreve `BGRX`.
- **O Pi 1 não detecta sub-tensão:** `vcgencmd get_throttled` mostra `0x0` mesmo com a fonte fraca, então
  **não é prova de que a energia está boa**. No nosso caso, alimentar o Pi pela USB de uma TV fez o hub USB
  interno (onde fica o chip de rede) cair e reconectar sob carga, derrubando a rede por segundos
  (`Network is unreachable`) e causando quedas totais. **Use uma fonte de tomada de 5 V, ≥ 1 A (ideal 2 A) e
  um bom cabo.**
- **A TV pode desligar o Pi:** se o HDMI é cortado (por exemplo, por um script de economia de tela), a TV
  pode entrar em espera e cortar a USB que alimenta o Pi. Mantenha a tela ligada.
- **Gravar no cartão SD** escreve centenas de MB por dia; quedas de energia durante a escrita podem
  corromper arquivos ou o cartão. Mais um motivo para uma boa fonte.
- **A senha aparece na linha de comando do `ffmpeg`** e, como ele agora fica conectado o tempo todo, ela fica
  visível em `ps` para qualquer usuário do Pi **o tempo todo**. Veja "Esconder a senha do `ps`" abaixo.
- **RTSP em `tcp` falhou na câmera de teste** (`Nonmatching transport in server reply`), então a conexão
  contínua também usa UDP. Perda de pacotes aparece como avisos do decodificador, não como conexão caída.

## Segurança e privacidade

- As senhas ficam só em `/etc/painel-cameras.env` (modo 600, dono root). O `.gitignore` bloqueia `*.env`
  (exceto o `.example`), fotos e imagens. **Nunca** versione esse arquivo.
- Mensagens de erro e o modo `--verificar` mascaram a senha.
- As fotos ficam no cartão SD: quem tiver acesso físico a ele vê as imagens.

### Esconder a senha do `ps` (recomendado)

O `ffmpeg` só aceita as credenciais dentro da URL RTSP, então elas vão parar nos argumentos dele. Montar o
`/proc` com `hidepid=2` faz os processos dos outros ficarem invisíveis para quem não é root. **Não** protege
contra o root, e a senha continua na linha de comando do `ffmpeg`.

```bash
sudo cp -p /etc/fstab /etc/fstab.bak
sudo groupadd -r proc                       # quem estiver neste grupo continua vendo todos os processos
GID=$(getent group proc | cut -d: -f3)
# o systemd-logind precisa continuar vendo todos os processos:
sudo mkdir -p /etc/systemd/system/systemd-logind.service.d
printf '[Service]\nSupplementaryGroups=proc\n' | sudo tee /etc/systemd/system/systemd-logind.service.d/hidepid.conf
sudo systemctl daemon-reload && sudo systemctl restart systemd-logind
# persistir nos reboots e aplicar agora:
sudo sed -i -E "s#^proc[[:space:]]+/proc[[:space:]]+proc[[:space:]]+defaults[[:space:]]#proc /proc proc defaults,hidepid=2,gid=$GID #" /etc/fstab
sudo mount -o remount,hidepid=2,gid=$GID /proc
```

Testado no Raspberry Pi OS (Debian 12, systemd 252): depois disso, o `ps` de um usuário comum não mostra mais o
`ffmpeg` do root, e a configuração sobreviveu a um reinício. Para desfazer:
`sudo mount -o remount,hidepid=0 /proc`, restaure o `/etc/fstab` e apague o arquivo `hidepid.conf`.

## Testes

```bash
python3 -m unittest discover -s tests -v
```

Cobrem a leitura da configuração (inclusive senha com `$`, `"` e `\`), a ordem numérica das câmeras, a
máscara da senha, a limpeza de fotos por idade e por tamanho, o comando contínuo do `ffmpeg` (só quadros-chave,
gravação atômica, escape da URL), o redesenho só da barra (a barra isolada é idêntica, byte a byte, ao topo de um
quadro completo) e o intervalo entre fotos gravadas. Precisam do Pillow (no Pi: `python3-pil`) e de **Linux**,
porque o script importa `fcntl`; no Windows, rode no Pi.

## Desinstalar

```bash
sudo ./uninstall.sh          # remove programa e serviço; mantém configuração e fotos
sudo ./uninstall.sh --tudo   # remove também /etc/painel-cameras.env e /var/lib/painel
```

## Licença

[MIT](LICENSE). O `ffmpeg`, o Pillow e a fonte DejaVu **não** fazem parte deste repositório: são instalados
pelo apt e têm as próprias licenças.
