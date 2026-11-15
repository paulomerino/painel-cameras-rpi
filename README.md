# painel-cameras-rpi

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

- **Rodízio de snapshots RTSP** entre uma ou mais câmeras (a cada ~8–15 s por imagem no Pi 1).
- **Barra** com nome da câmera, "atualizado há Ns" e relógio. Mostra **SEM SINAL** se a imagem ficar velha (60 s).
- **Grava as fotos** em `/var/lib/painel/<câmera>/<AAAA-MM-DD>/<HH-MM-SS>.jpg`, com **retenção por idade**
  (padrão 7 dias) e **limite de tamanho** (padrão 8 GB, apaga as mais antigas primeiro).
- Só grava com o **relógio sincronizado** (o Pi não tem relógio de bateria) e com mais de 2 GB livres.
- A configuração é um único arquivo `/etc/painel-cameras.env` (modo 600), com as senhas fora do código.

> **Não é vídeo ao vivo.** É um quadro parado que se renova a cada poucos segundos. Num Pi 1, a
> decodificação contínua não é viável (um snapshot leva ~8 s só para conectar e decodificar).

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
| `PAINEL_PAUSA` | `2` | Pausa entre capturas |
| `PAINEL_GRAVAR` | `1` | `0` desliga a gravação |
| `PAINEL_GRAVAR_DIR` | `/var/lib/painel` | Onde guardar as fotos |
| `PAINEL_RETENCAO_DIAS` | `7` | Apaga fotos mais antigas que isso |
| `PAINEL_LIMITE_GB` | `8` | Teto de espaço; passou, apaga as mais antigas |

Depois de mudar o arquivo: `sudo systemctl restart painel-cameras`.

## Como funciona

```
 câmera(s) ──RTSP/UDP──► ffmpeg (1 quadro) ──► /run/painel/CAMn.jpg   (RAM, não gasta o SD)
                                                  │            │
                                     desenha 1x/s │            └─► copia para /var/lib/painel/... (se gravar)
                                                  ▼
                               PIL ──► /dev/fb0 (framebuffer) ──► HDMI
```

- Uma thread captura (uma câmera por vez, para limitar CPU e RAM); o laço principal só desenha.
- O console de texto é colocado em modo gráfico (`KD_GRAPHICS`) para não desenhar por cima do painel.
- A limpeza das fotos roda de hora em hora e **não carrega a lista de arquivos na memória**: os nomes já
  ordenam por data, então basta percorrer as pastas em ordem.

## Desempenho medido (Pi 1 Model B)

| Item | Valor |
|---|---|
| Fluxo da câmera de teste | H.264 Main, 1280×720, 15 fps |
| Tempo por snapshot | ~8 s (chegou a ~13 s com o Pi ocupado) |
| RAM do `ffmpeg` | pico de ~46 MB |
| RAM do painel (Python) | ~14 MB |
| CPU do Python | ~27–32 % |
| Tamanho médio de uma foto | ~87 KB (amostra pequena: 68–108 KB) |

## Espaço das fotos

Estimativa com ~87 KB por foto (varia com a cena; à noite, com infravermelho, pode mudar):

| Intervalo | Por dia | 7 dias |
|---|---|---|
| 10 s | ~730 MB | ~5 GB |
| 30 s | ~245 MB | ~1,7 GB |
| 1 min | ~120 MB | ~0,8 GB |

O ciclo real no Pi 1 é de pelo menos ~10 s (a captura sozinha leva ~8 s), então o consumo real tende a ser menor.

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
- **A senha aparece na linha de comando do `ffmpeg`** (visível em `ps` por outros usuários do Pi, durante
  alguns segundos por captura). Em um Pi de uso pessoal o risco é baixo, mas é bom saber.

## Segurança e privacidade

- As senhas ficam só em `/etc/painel-cameras.env` (modo 600, dono root). O `.gitignore` bloqueia `*.env`
  (exceto o `.example`), fotos e imagens. **Nunca** versione esse arquivo.
- Mensagens de erro e o modo `--verificar` mascaram a senha.
- As fotos ficam no cartão SD: quem tiver acesso físico a ele vê as imagens.

## Testes

```bash
python3 -m unittest discover -s tests -v
```

Cobrem a leitura da configuração (inclusive senha com `$`, `"` e `\`), a ordem numérica das câmeras, a
máscara da senha e a limpeza de fotos por idade e por tamanho. Precisam do Pillow (no Pi: `python3-pil`).

## Desinstalar

```bash
sudo ./uninstall.sh          # remove programa e serviço; mantém configuração e fotos
sudo ./uninstall.sh --tudo   # remove também /etc/painel-cameras.env e /var/lib/painel
```

## Licença

[MIT](LICENSE). O `ffmpeg`, o Pillow e a fonte DejaVu **não** fazem parte deste repositório: são instalados
pelo apt e têm as próprias licenças.
