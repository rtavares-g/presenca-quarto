# Presença Quarto — Raspberry Pi

Sensor de presença mmWave DFRobot **C4001 (25m)** ligado ao Raspberry Pi,
publicando o estado de presença na **Sinric Pro** (capacidade *Motion
Sensor*), com um pequeno painel web e console remoto de logs — mesmo
padrão do [`sensor-pi`](../sensor-pi).

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

- **Pelo painel web** (`http://<ip-do-pi>:8081/`): tem um card "Alcance e
  sensibilidade do sensor" que lê os valores atuais e salva os novos via
  WebSocket, sem precisar de SSH.
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
./venv/bin/python configurar_sensor.py --max-cm 300 --sensibilidade 2
```

- `--min-cm` / `--max-cm`: alcance de detecção, em cm (mínimo 30, máximo
  2000 - o `--max-cm` também vira o alcance de disparo).
- `--sensibilidade`: 0 a 9, quanto menor mais difícil disparar.
- `--baud`: baud rate da UART (padrão 9600, o mesmo do sensor).

O script imprime os valores confirmados pelo próprio sensor no final.

## Instalação automática

```bash
git clone https://github.com/rtavares-g/presenca-quarto.git ~/presenca-quarto
cd ~/presenca-quarto
./install.sh
```

O `install.sh` instala as dependências do sistema, cria `.env` a
partir do exemplo e pede no terminal o **Device ID**, **App Key** e **App
Secret** do Sinric Pro (se deixar algum campo em branco, edite depois com
`nano .env`), monta o venv e instala o serviço.

## Instalação manual (passo a passo)

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip python3-lgpio

mkdir -p ~/presenca-quarto && cd ~/presenca-quarto
# copie presenca_quarto.py, .env (baseado no .env.example),
# requirements.txt e presenca-quarto.service para cá

cp .env.example .env
nano .env                    # preencha SINRIC_DEVICE_ID / SINRIC_APP_KEY / SINRIC_APP_SECRET
chmod 600 .env                # o arquivo guarda as chaves do Sinric

python3 -m venv --system-site-packages venv
./venv/bin/pip install -r requirements.txt
```

Teste:

```bash
./venv/bin/python presenca_quarto.py
```

Deve aparecer `SENSOR: lendo OUT no GPIO 27...` e, ao se mexer na frente
do sensor, `PRESENCA: detectada`.

## Criando o dispositivo na Sinric Pro

1. No [portal da Sinric Pro](https://portal.sinric.pro), crie um novo
   dispositivo do tipo **Motion Sensor**.
2. Copie o **Device ID** gerado e, na aba de credenciais do app,
   **App Key** e **App Secret** (a mesma da sua conta, compartilhada com
   os outros dispositivos Sinric já configurados neste Raspberry Pi).
3. Coloque os três valores em `.env` (ou informe durante o
   `install.sh`).

## Serviço automático

```bash
sudo cp presenca-quarto.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now presenca-quarto
systemctl status presenca-quarto
journalctl -u presenca-quarto -f
```

## Endereços

| Caminho | Conteúdo |
|---|---|
| `/` | painel com o estado atual de presença e o ajuste de alcance/sensibilidade |
| `/presenca` | JSON com `presence` e `sinric` |
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
  rastreio por um instante. Aumente se a Sinric estiver alternando
  demais; diminua se a resposta parecer lenta.
- `PORTA_WEB`: porta do painel HTTP (padrão 8081 — o `sensor-pi` já usa a
  8080 neste mesmo Raspberry Pi).
- `SINRIC_DEBUG`: ponha `1` para logs detalhados do SDK da Sinric Pro.
