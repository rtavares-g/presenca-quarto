# Presença Quarto — Raspberry Pi

Sensor de presença mmWave DFRobot **C4001 (25m)** ligado ao Raspberry Pi,
publicando a presença no **Home Assistant** (MQTT, descoberta automática),
com um pequeno painel web e console remoto de logs.

Cada nova detecção do mmWave só vira presença no HA depois que o
[Kinect](../kinect) confirmar uma pessoa (ver
[Validação pelo Kinect](#validação-pelo-kinect)).

## Sobre o sensor

A placa tem 5 pinos expostos (serigrafados da esquerda pra direita, de
cima pra baixo): `VIN`, `GND`, `RX`, `TX`, `OUT`.

- **OUT** é a saída digital pronta de presença: nível alto quando detecta
  alguém, baixo quando não detecta (o script usa só esse pino).
- **RX/TX** são a UART do módulo, usada apenas para configuração avançada
  (sensibilidade, alcance, modo). O `presenca_quarto.py` (loop principal)
  não usa esses pinos; quem fala com eles é o `configurar_sensor.py` — veja
  [Ajustando alcance e sensibilidade](#ajustando-alcance-e-sensibilidade-uart).

## Ligações

| Pino do sensor | Pino do Raspberry (BCM) | Pino físico | Observação |
|---|---|---|---|
| VIN | 5V | pino 2 ou 4 | módulo alimenta em 4,4–6 V; use o 5V, não o 3V3 |
| GND | GND | pino 6 | |
| OUT | GPIO 27 | pino 13 | saída digital 3,3 V, liga o script |
| RX | GPIO 14 (TXD) | pino 8 | opcional, não usado pelo script (config. avançada via UART) |
| TX | GPIO 15 (RXD) | pino 10 | opcional, não usado pelo script (config. avançada via UART) |

Se o painel mostrar presença invertida (SIM quando vazio, NÃO quando tem
alguém), troque `OUT_ATIVO_ALTO` para `0` no `.env`.

## Ajustando alcance e sensibilidade (UART)

O pino OUT só dá um sim/não de presença; alcance e sensibilidade são
configurados à parte, pela UART (RX/TX). É uma configuração que fica salva
na memória do próprio sensor - só precisa aplicar de novo se quiser mudar o
ajuste, não a cada boot. Duas formas de fazer isso:

- **Pelo painel web** (`https://presenca-quarto.tavares.nz/`): tem um card "Alcance e
  sensibilidade do sensor" com dois botões - "Carregar atual" (lê os valores
  do sensor) e "Salvar no sensor" (aplica os campos). Nenhum dos dois roda
  sozinho ao abrir a página - cada comando UART para e reinicia a detecção
  do sensor por um instante, então só acontece quando você clica.
  O botão "Restaurar padrão" grava de volta a configuração de referência
  (alcance 30-300cm, sensibilidade de disparo 1 e de manutenção 4, atraso de
  disparo 1500ms, retenção 30s), útil se algo foi mudado à mão e não
  funcionou bem. Esses valores ficam em `CONFIG_PADRAO`, no
  `presenca_quarto.py`, e são os mesmos padrões do `configurar_sensor.py`.
- **Por linha de comando**, com o script `configurar_sensor.py` deste
  repositório (útil para automatizar ou rodar sem o serviço web no ar).

Os dois falam com o sensor pela mesma UART - evite rodar o script ao mesmo
tempo que estiver mexendo no card do painel, para não disputar a porta.

**Habilitar a UART no Raspberry Pi** (uma vez só):

```bash
sudo raspi-config nonint do_serial_hw 0   # habilita a UART
sudo raspi-config nonint do_serial_cons 1 # desliga o console serial
```

No Pi 3B/3B+/Zero W/4 a UART completa (PL011, usada pelo GPIO14/15) é
compartilhada com o Bluetooth por padrão; se depois de reiniciar o script
não conseguir falar com o sensor, desligue o Bluetooth em
`/boot/firmware/config.txt`:

```
dtoverlay=disable-bt
```

e reinicie (`sudo reboot`).

**Rodar o ajuste pela linha de comando:**

```bash
./venv/bin/python configurar_sensor.py                 # aplica os valores padrão
./venv/bin/python configurar_sensor.py --retencao-seg 60 # muda só a retenção; o resto volta ao padrão
```

- `--min-cm` / `--max-cm`: alcance de detecção, em cm (mínimo 30, máximo
  2000 - o `--max-cm` também vira o alcance de disparo).
- `--sensibilidade-disparo`: 0 a 9, o quanto de movimento é preciso para
  *começar* a detectar. Baixa evita disparos por animais e cortinas.
- `--sensibilidade-manutencao`: 0 a 9, o quanto o sensor consegue *continuar*
  detectando alguém parado (respiração, pequenos movimentos). Baixa demais
  faz a presença cair enquanto a pessoa está quieta.
- `--sensibilidade`: atalho que usa o mesmo valor nas duas.
- `--atraso-disparo-ms`: 0 a 2000, quanto tempo a detecção precisa durar
  para contar. Filtra movimentos rápidos, como um animal passando.
- `--retencao-seg`: 2 a 1500, quanto tempo a presença segue marcada depois
  da última detecção.
- `--baud`: baud rate da UART (padrão 9600, o mesmo do sensor).

O script imprime os valores confirmados pelo próprio sensor no final.

## Instalação automática

```bash
git clone https://github.com/rtavares-g/presenca-quarto.git ~/presenca-quarto
cd ~/presenca-quarto
./install.sh
```

O `install.sh` instala as dependências do sistema, cria `.env` a
partir do exemplo, pede o **host, usuário e senha do MQTT** do Home
Assistant (se `~/.config/mqtt-ha.json` ainda não existir), monta o venv e
instala o serviço.

No final, ele pergunta se você quer configurar HTTPS com Nginx + Let's
Encrypt. Responda **não**: o acesso externo é feito pelo Cloudflare Tunnel
(veja [Acesso remoto](#acesso-remoto-cloudflare-tunnel)), que não precisa
de Nginx nem de portas abertas no roteador.

## Instalação manual (passo a passo)

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip python3-lgpio

mkdir -p ~/presenca-quarto && cd ~/presenca-quarto
# copie presenca_quarto.py, .env (baseado no .env.example),
# requirements.txt e presenca-quarto.service para cá

cp .env.example .env
nano .env                    # ajustes (opcional)
# MQTT do HA em ~/.config/mqtt-ha.json:
#   {"host": "192.168.1.211", "port": 1883, "usuario": "...", "senha": "..."}

python3 -m venv --system-site-packages venv
./venv/bin/pip install -r requirements.txt
```

Teste:

```bash
./venv/bin/python presenca_quarto.py
```

Deve aparecer `SENSOR: lendo OUT no GPIO 27...` e, ao se mexer na frente
do sensor, `PRESENCA: detectada`.

## Home Assistant

O HA precisa do add-on **Mosquitto broker** e da integração **MQTT**. O
usuário do Pi fica nas opções do add-on (`logins`). As entidades aparecem
sozinhas, num dispositivo "Presença quarto":

| Entidade | O que é |
|---|---|
| `binary_sensor.presenca_quarto` | presença **validada** (use esta nas automações e na Alexa) |
| `binary_sensor.presenca_quarto_mmwave` | leitura bruta do mmWave (diagnóstico) |

O atributo `validacao` diz como a presença foi confirmada: `kinect`,
`mmwave` (Kinect fora do ar) ou `pendente`. Os estados vão com *retain*,
então o HA tem o valor certo mesmo depois de reiniciar. Se o Pi cair, as
entidades ficam indisponíveis.

### Validação pelo Kinect

1. O mmWave detecta alguém: `presenca_quarto_mmwave` liga na hora e a
   presença fica **pendente**.
2. O serviço `kinect-quarto` confirma uma pessoa (silhueta humana se
   mexendo): `presenca_quarto` liga.
3. Daí em diante a presença só desliga quando o mmWave marcar ausência. O
   Kinect perder a pessoa de vista não desliga.
4. Se o Kinect estiver fora do ar (`~/kinect/estado.json` sem atualizar por
   `KINECT_PARADO_SEG`), a presença usa só o mmWave, para não ficar travada.

Por enquanto o Kinect só confirma quem está no campo de visão dele. Quem
estiver fora (ou deitado de um jeito que ele não reconhece) fica pendente.

## Serviço automático

```bash
sudo cp presenca-quarto.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now presenca-quarto
systemctl status presenca-quarto
journalctl -u presenca-quarto -f
```

Se já tinha o serviço instalado, copie o `.service` de novo e rode o
`daemon-reload` sempre que ele mudar (o watchdog abaixo depende disso).

### Watchdog (reinício automático se travar)

O serviço se recupera sozinho se o loop de leitura do sensor parar de
girar, sem precisar reiniciar à mão:

- **Vigia interno:** se o ciclo principal ficar 20s sem girar, o script
  registra no log `TRAVADO:` com a linha onde ele parou e encerra; o
  systemd (`Restart=always`) sobe de novo em 5s.
- **Watchdog do systemd** (`WatchdogSec=60` no `.service`): o script avisa
  o systemd a cada giro do ciclo. Se o processo inteiro congelar e ficar
  60s sem avisar, o systemd mata com `SIGABRT` (a pilha de todas as
  threads vai para o journal) e reinicia.

Nesses reinícios a presença e a validação continuam de onde pararam: o
estado é salvo em `estado.json` e retomado dentro do
`TOLERANCIA_OFFLINE_SEG` (veja [Ajustes finos](#ajustes-finos)).

Para ver se já aconteceu algum:

```bash
journalctl -u presenca-quarto | grep -E "TRAVADO|watchdog"
systemctl show presenca-quarto -p NRestarts
```

## Acesso remoto (Cloudflare Tunnel)

O painel escuta só em `127.0.0.1:8081` - não é acessível pela rede
local nem pela internet diretamente. O acesso externo passa pelo
Cloudflare Tunnel (`cloudflared`, rodando como serviço neste Raspberry Pi)
e é protegido pelo Cloudflare Access (login antes de chegar ao painel):

| | |
|---|---|
| Domínio | `https://presenca-quarto.tavares.nz/` |
| Rota no túnel (Public Hostname) | `presenca-quarto.tavares.nz` → `HTTP` `localhost:8081` |
| Autenticação | aplicação no Cloudflare Access (Zero Trust → Access → Applications) |
| Certificado | Advanced Certificate Manager (subdomínio de dois níveis não é coberto pelo Universal SSL) |

Nenhuma porta precisa ficar aberta no roteador. Para voltar a liberar o
painel na rede local, troque `HOST_WEB` no `.env` para `0.0.0.0` e reinicie o serviço.

## Endereços

No próprio Pi, em `http://127.0.0.1:8081`, ou de fora em
`https://presenca-quarto.tavares.nz`:

| Caminho | Conteúdo |
|---|---|
| `/` | painel com o estado atual de presença e os ajustes do sensor |
| `/presenca` | JSON com `presence` (mmWave), `validada`, `validacao`, `ha` (MQTT conectado) e `duracao_seg` |
| `/presenca-ws` | WebSocket que o painel usa para atualizar em tempo real (sem polling) |
| `/logs` | console remoto ao vivo (WebSocket) |
| `/sensor-ws` | WebSocket usado pelo painel para ler/salvar alcance e sensibilidade |

## Testar sem hardware

```bash
./venv/bin/python presenca_quarto.py --simular
```

Alterna presença simulada a cada ~6s, sem precisar do sensor nem do GPIO.

## Ajustes finos

Todas as opções ficam em `.env` (veja `.env.example`):

- `OUT_ATIVO_ALTO`: inverta (`0`) se a lógica do OUT estiver "ao contrário".
- `ATRASO_AUSENCIA_SEG`: quanto tempo sem detecção até marcar "ausente".
  A detecção de presença é imediata; só a ausência tem esse atraso, para
  não ficar piscando quando a pessoa fica parada e o mmWave perde o
  rastreio por um instante. Aumente se a presença estiver alternando
  demais; diminua se a resposta parecer lenta.
- `TOLERANCIA_OFFLINE_SEG` (padrão 300 = 5 min): se o serviço/Pi reiniciar
  e voltar dentro desse tempo, a presença, a contagem e a validação
  continuam de onde pararam. Passou disso, começa do zero. O estado fica
  salvo em `estado.json`. Depois de um reinício (ou de ler/salvar a
  configuração pela UART) a ausência é ignorada por 10s, enquanto o sensor
  volta a detectar.
- `PORTA_WEB`: porta do painel HTTP (padrão 8081 — o `sensor-pi` já usa a
  8080 neste mesmo Raspberry Pi).
- `HOST_WEB`: endereço em que o painel escuta (padrão `127.0.0.1`, só o
  próprio Pi - o acesso externo vem pelo Cloudflare Tunnel). Use `0.0.0.0`
  para liberar na rede local.
- `KINECT_VALIDAR` (padrão `1`): `0` desliga a validação pelo Kinect (a
  presença vai direto do mmWave para o HA).
- `KINECT_PARADO_SEG` (padrão 30): quanto tempo sem notícia do Kinect até
  usar só o mmWave.
- `MQTT_CONFIG`: caminho do JSON do MQTT (padrão `~/.config/mqtt-ha.json`).
