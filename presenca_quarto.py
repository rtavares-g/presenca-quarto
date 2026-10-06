#!/usr/bin/env python3
"""
Sensor de presença mmWave (C4001 25m) - Raspberry Pi + Home Assistant
Lê o pino digital OUT do sensor e publica a presença no Home Assistant via
MQTT (descoberta automática). Cada nova detecção só vira presença depois que
o Kinect (projeto kinect) confirmar uma pessoa. Tem um pequeno painel web e
console remoto para depuração.
"""

from __future__ import annotations

import asyncio
import faulthandler
import json
import os
import socket
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from aiohttp import web
from gpiozero import InputDevice  # type: ignore[import-untyped]
import paho.mqtt.client as mqtt  # type: ignore[import-untyped]

from DFRobot_C4001 import DFRobot_C4001_UART, EXIST_MODE

# =========================================================================
# CONFIGURACAO
# =========================================================================

RAIZ = Path(__file__).resolve().parent


def carregar_env(caminho: Path) -> None:
    """Lê o .env sem depender de biblioteca externa.

    Variáveis já presentes no ambiente vencem o arquivo, para o systemd
    poder sobrescrever qualquer coisa sem editar o .env.
    """
    if not caminho.is_file():
        return
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        chave = chave.strip()
        valor = valor.strip().strip('"').strip("'")
        if chave and chave not in os.environ:
            os.environ[chave] = valor


def env_int(nome: str, padrao: int) -> int:
    try:
        return int(os.environ.get(nome, padrao))
    except ValueError:
        return padrao


def env_float(nome: str, padrao: float) -> float:
    try:
        return float(os.environ.get(nome, padrao))
    except ValueError:
        return padrao


def env_bool(nome: str, padrao: bool) -> bool:
    return os.environ.get(nome, "1" if padrao else "0").lower() in ("1", "true", "sim", "yes")


carregar_env(Path(os.environ.get("PRESENCA_ENV", RAIZ / ".env")))

OUT_GPIO = env_int("OUT_GPIO", 27)
OUT_ATIVO_ALTO = env_bool("OUT_ATIVO_ALTO", True)
ATRASO_AUSENCIA_SEG = env_float("ATRASO_AUSENCIA_SEG", 3.0)
PORTA_WEB = env_int("PORTA_WEB", 8081)
HOST_WEB = os.environ.get("HOST_WEB", "127.0.0.1")

# Se o serviço/Pi reiniciar e voltar dentro desse tempo, o estado anterior
# (presença, contagem e validação) é mantido. Passou disso, começa do zero.
TOLERANCIA_OFFLINE_SEG = env_float("TOLERANCIA_OFFLINE_SEG", 300.0)
ARQUIVO_ESTADO = Path(os.environ.get("ARQUIVO_ESTADO", RAIZ / "estado.json"))
# Tempo que o sensor leva para voltar a detectar depois de reiniciar
# (boot do serviço ou comando UART): ausência nesse intervalo é ignorada.
SENSOR_AQUECIMENTO_SEG = 10.0

# Home Assistant via MQTT (broker Mosquitto do HA). Arquivo JSON com
# host/port/usuario/senha, fora do git.
MQTT_CONFIG = Path(os.environ.get("MQTT_CONFIG", Path.home() / ".config" / "mqtt-ha.json")).expanduser()

# Validação pelo Kinect (projeto kinect): cada nova detecção do mmWave só
# vira presença no HA depois que o Kinect confirmar uma pessoa. Se o
# serviço do Kinect estiver parado (estado.json velho) por mais que
# KINECT_PARADO_SEG, a presença usa só o mmWave para não ficar travada.
KINECT_VALIDAR = env_bool("KINECT_VALIDAR", True)
KINECT_ESTADO = Path(os.environ.get("KINECT_ESTADO", Path.home() / "kinect" / "estado.json")).expanduser()
KINECT_PARADO_SEG = env_float("KINECT_PARADO_SEG", 30.0)

SENSOR_UART_BAUD = env_int("SENSOR_UART_BAUD", 9600)

# Se o ciclo principal ficar esse tempo sem girar, loga onde ele parou e
# encerra o processo para o systemd reiniciar (o estado é retomado do disco).
CICLO_TRAVADO_SEG = 20.0

# =========================================================================
# LOGGING - Console remoto
# =========================================================================

class Estado:
    """Estado compartilhado da aplicação."""
    def __init__(self) -> None:
        self.presenca = False
        self.ha_ok = False
        # presença validada (o que vai para o HA): mmWave detectou E o Kinect
        # confirmou uma pessoa desde o início desta detecção
        self.validada = False
        self.validacao = ""  # "kinect", "mmwave" (Kinect fora do ar), "pendente" ou ""
        self.presenca_desde: float | None = None  # time.monotonic() de quando a presença atual começou
        self.segurar_ausencia_ate = 0.0  # time.monotonic() até quando ignorar ausência (sensor reiniciando)
        self.ciclo_em = time.monotonic()  # último giro do ciclo principal (ver vigiar_ciclo)

estado = Estado()

def salvar_estado() -> None:
    """Grava o estado em disco para sobreviver a um reinício do serviço.
    Usa relógio de parede (time.time) porque o monotonic zera no boot."""
    agora_mono = time.monotonic()
    agora = time.time()
    desde = None
    if estado.presenca_desde is not None:
        desde = agora - (agora_mono - estado.presenca_desde)
    dados = {
        "presenca": estado.presenca,
        "presenca_desde": desde,
        "presenca_validada": estado.presenca and estado.validada,
        "validacao": estado.validacao,
        "salvo_em": agora,
    }
    try:
        tmp = ARQUIVO_ESTADO.with_suffix(".tmp")
        tmp.write_text(json.dumps(dados), encoding="utf-8")
        os.replace(tmp, ARQUIVO_ESTADO)
    except OSError as e:
        log(f"ESTADO: falha ao salvar ({e})")

def restaurar_estado() -> None:
    """Se o último estado salvo tiver menos de TOLERANCIA_OFFLINE_SEG,
    retoma presença, contagem e validação de onde parou."""
    try:
        dados = json.loads(ARQUIVO_ESTADO.read_text(encoding="utf-8"))
        salvo_em = float(dados["salvo_em"])
    except FileNotFoundError:
        return
    except (OSError, ValueError, KeyError, TypeError) as e:
        log(f"ESTADO: arquivo inválido, ignorando ({e})")
        return

    offline = time.time() - salvo_em
    # offline negativo = relógio voltou (Pi sem RTC antes do NTP): não dá
    # para saber quanto tempo passou, então trata como expirado
    if not (0 <= offline <= TOLERANCIA_OFFLINE_SEG):
        log(f"ESTADO: último estado salvo expirado ({offline:.0f}s), começando do zero")
        return

    agora_mono = time.monotonic()
    estado.presenca = bool(dados.get("presenca"))
    desde = dados.get("presenca_desde")
    if estado.presenca and desde is not None:
        estado.presenca_desde = agora_mono - (time.time() - float(desde))
    elif estado.presenca:
        estado.presenca_desde = agora_mono
    estado.validada = estado.presenca and bool(dados.get("presenca_validada"))
    estado.validacao = (str(dados.get("validacao") or "") or ("" if estado.validada else "pendente")) if estado.presenca else ""
    estado.segurar_ausencia_ate = agora_mono + SENSOR_AQUECIMENTO_SEG
    log(
        f"ESTADO: retomado após {offline:.0f}s parado "
        f"(presença {'SIM' if estado.presenca else 'NÃO'}, validação {estado.validacao or '-'})"
    )

class Console:
    """Redireciona print() e erros (stdout/stderr) para log e WebSocket."""
    def __init__(self) -> None:
        self.linhas: deque[str] = deque(maxlen=1000)
        self.clientes: set[web.WebSocketResponse] = set()
        self.original_stdout = sys.stdout

    def stream(self, original, prefixo: str = "") -> "_Fluxo":
        return _Fluxo(self, original, prefixo)

    def registrar(self, texto: str, original) -> None:
        hora = datetime.now().strftime("%H:%M:%S")
        linha = f"[{hora}] {texto}"
        self.linhas.append(linha)
        original.write(linha + "\n")
        original.flush()
        try:
            asyncio.get_running_loop().create_task(self._broadcast(linha))
        except RuntimeError:
            pass  # fora do event loop: fica só no histórico

    async def _broadcast(self, msg: str) -> None:
        mortos = set()
        for ws in self.clientes:
            try:
                await asyncio.wait_for(ws.send_str(msg), ENVIO_WS_TIMEOUT_SEG)
            except Exception:
                mortos.add(ws)
        self.clientes -= mortos

    def historico(self) -> list[str]:
        return list(self.linhas)

class _Fluxo:
    """Arquivo que junta escritas parciais e registra cada linha completa."""
    def __init__(self, console: Console, original, prefixo: str) -> None:
        self.console = console
        self.original = original
        self.prefixo = prefixo
        self.buffer = ""

    def write(self, msg: str) -> int:
        self.buffer += msg
        *completas, self.buffer = self.buffer.split("\n")
        for linha in completas:
            if linha.strip():
                self.console.registrar(self.prefixo + linha.rstrip(), self.original)
        return len(msg)

    def flush(self) -> None:
        if self.buffer.strip():
            self.console.registrar(self.prefixo + self.buffer.rstrip(), self.original)
        self.buffer = ""
        self.original.flush()

console = Console()
sys.stdout = console.stream(sys.stdout)
sys.stderr = console.stream(sys.stderr, "STDERR: ")
# se o systemd matar o processo pelo watchdog (SIGABRT), despeja a pilha de
# todas as threads no journal - o stderr "de verdade", não o redirecionado
if sys.__stderr__ is not None:
    faulthandler.enable(file=sys.__stderr__)

def log(msg: str) -> None:
    print(msg)

# Envio a um navegador do painel/logs que demore mais que isso = cliente
# morto (conexão meio aberta): é descartado (o heartbeat do aiohttp fecha).
ENVIO_WS_TIMEOUT_SEG = 5.0

# =========================================================================
# SENSOR DE PRESENCA (pino OUT do C4001)
# =========================================================================

class LeitorPresenca:
    """Lê o pino digital OUT do sensor mmWave C4001.

    Leitura direta do nível do pino (InputDevice), sem detecção de borda nem
    debounce do lgpio: o ciclo já consulta o pino a cada 300ms e filtra
    oscilações com ATRASO_AUSENCIA_SEG, então as threads de alerta/callback
    do lgpio só acrescentariam pontos onde a leitura pode empacar."""
    def __init__(self, gpio: int, ativo_alto: bool) -> None:
        self.ativo_alto = ativo_alto
        self.pino = InputDevice(gpio, pull_up=False)
        log(f"SENSOR: lendo OUT no GPIO {gpio} (ativo em {'alto' if ativo_alto else 'baixo'})")

    def detectado(self) -> bool:
        bruto = self.pino.is_active  # True = pino em nível alto
        return bruto if self.ativo_alto else not bruto

class LeitorSimulado:
    """Simula presença alternando a cada poucos ciclos (para testes sem hardware)."""
    def __init__(self) -> None:
        self.contador = 0

    def detectado(self) -> bool:
        self.contador += 1
        return (self.contador // 20) % 2 == 0

def montar_leitor(simular: bool):
    if simular:
        return LeitorSimulado()
    return LeitorPresenca(OUT_GPIO, OUT_ATIVO_ALTO)

# =========================================================================
# CONFIGURACAO DO SENSOR (UART - alcance/sensibilidade, pinos RX/TX)
# =========================================================================

RANGE_MIN_CM = (30, 2000)
RANGE_MAX_CM = (240, 2000)
SENSIBILIDADE_LIMITE = (0, 9)
RETENCAO_LIMITE_SEG = (2, 1500)
# Valores de referência restaurados pelo botão "Restaurar padrão" do painel
# (os mesmos padrões do configurar_sensor.py). Meio-termo para um quarto com
# gatos: disparo baixo e atraso filtram animais passando, e a manutenção
# segura alguém parado detectado.
CONFIG_PADRAO = {
    "min_cm": 30,
    "max_cm": 300,
    "sens_disparo": 1,
    "sens_manutencao": 4,
    "atraso_disparo_ms": 1500,
    "retencao_seg": 30,
}
ATRASO_DISPARO_LIMITE_MS = (0, 2000)

def ler_config_sensor(radar) -> dict:
    """Configuração atual do C4001. O atraso de disparo é quanto tempo a
    detecção precisa durar para o pino OUT subir (filtra movimentos rápidos,
    como um gato passando); a retenção (keep timeout) é quanto tempo o OUT
    segue alto depois da última detecção. A biblioteca devolve os dois em
    unidades de 10ms e 0,5s."""
    return {
        "min_cm": int(radar.get_min_range()),
        "max_cm": int(radar.get_max_range()),
        "sens_disparo": int(radar.get_trig_sensitivity()),
        "sens_manutencao": int(radar.get_keep_sensitivity()),
        "atraso_disparo_ms": int(radar.get_trig_delay()) * 10,
        "retencao_seg": int(radar.get_keep_timerout()) // 2,
    }

class SensorUART:
    """Lê/ajusta alcance, sensibilidade e retenção do C4001 pela UART (RX/TX) -
    separado da detecção pelo pino OUT. Abre e fecha a porta a cada
    operação: evita manter uma conexão ociosa e disputar com o
    configurar_sensor.py (CLI) se alguém rodar os dois ao mesmo tempo."""

    def ler(self) -> dict:
        radar = DFRobot_C4001_UART(SENSOR_UART_BAUD)
        try:
            return ler_config_sensor(radar)
        except Exception as e:
            raise RuntimeError("sensor não respondeu (confira a fiação RX/TX)") from e
        finally:
            radar.ser.close()

    def aplicar(self, min_cm: int, max_cm: int, sens_disparo: int,
                sens_manutencao: int, atraso_disparo_ms: int, retencao_seg: int) -> dict:
        if not (RANGE_MIN_CM[0] <= min_cm <= RANGE_MIN_CM[1]):
            raise ValueError(f"alcance mínimo deve ser {RANGE_MIN_CM[0]}-{RANGE_MIN_CM[1]}cm")
        if not (RANGE_MAX_CM[0] <= max_cm <= RANGE_MAX_CM[1]):
            raise ValueError(f"alcance máximo deve ser {RANGE_MAX_CM[0]}-{RANGE_MAX_CM[1]}cm")
        if min_cm > max_cm:
            raise ValueError("alcance mínimo não pode ser maior que o máximo")
        for nome, valor in (("disparo", sens_disparo), ("manutenção", sens_manutencao)):
            if not (SENSIBILIDADE_LIMITE[0] <= valor <= SENSIBILIDADE_LIMITE[1]):
                raise ValueError(f"sensibilidade de {nome} deve ser {SENSIBILIDADE_LIMITE[0]}-{SENSIBILIDADE_LIMITE[1]}")
        if not (ATRASO_DISPARO_LIMITE_MS[0] <= atraso_disparo_ms <= ATRASO_DISPARO_LIMITE_MS[1]):
            raise ValueError(f"atraso de disparo deve ser {ATRASO_DISPARO_LIMITE_MS[0]}-{ATRASO_DISPARO_LIMITE_MS[1]}ms")
        if not (RETENCAO_LIMITE_SEG[0] <= retencao_seg <= RETENCAO_LIMITE_SEG[1]):
            raise ValueError(f"retenção deve ser {RETENCAO_LIMITE_SEG[0]}-{RETENCAO_LIMITE_SEG[1]}s")

        radar = DFRobot_C4001_UART(SENSOR_UART_BAUD)
        try:
            radar.set_sensor_mode(EXIST_MODE)
            radar.set_detection_range(min_cm, max_cm, max_cm)
            radar.set_trig_sensitivity(sens_disparo)
            radar.set_keep_sensitivity(sens_manutencao)
            radar.set_delay(atraso_disparo_ms // 10, retencao_seg * 2)
            time.sleep(0.3)
            return ler_config_sensor(radar)
        except Exception as e:
            raise RuntimeError("sensor não respondeu (confira a fiação RX/TX)") from e
        finally:
            radar.ser.close()

sensor_uart = SensorUART()
sensor_uart_lock = asyncio.Lock()  # evita duas abas mexendo na UART ao mesmo tempo

# =========================================================================
# HOME ASSISTANT (MQTT com descoberta automática)
# =========================================================================

class HomeAssistant:
    """Publica a presença no Home Assistant pelo broker MQTT.

    Entidades criadas sozinhas no HA (descoberta MQTT), num dispositivo
    "Presença quarto":
    - binary_sensor.presenca_quarto: presença validada pelo Kinect
    - binary_sensor.presenca_quarto_mmwave: leitura bruta do mmWave
    Os estados vão com retain, então o HA tem o valor certo mesmo depois de
    reiniciar. Se o Pi cair, o "last will" marca as entidades indisponíveis."""
    BASE = "presenca-quarto"
    DESCOBERTA = "homeassistant/binary_sensor/presenca_quarto"

    def __init__(self) -> None:
        self.cliente: mqtt.Client | None = None
        self.publicado: dict[str, str] = {}
        self.loop: asyncio.AbstractEventLoop | None = None

    def iniciar(self) -> None:
        try:
            cfg = json.loads(MQTT_CONFIG.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log(f"HA: sem configuração do MQTT em {MQTT_CONFIG} ({e})")
            return
        self.loop = asyncio.get_running_loop()
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="presenca-quarto")
        c.username_pw_set(cfg["usuario"], cfg["senha"])
        c.will_set(f"{self.BASE}/disponivel", "offline", qos=1, retain=True)
        c.on_connect = self._ao_conectar
        c.on_disconnect = self._ao_desconectar
        c.reconnect_delay_set(1, 30)
        log(f"HA: conectando ao MQTT {cfg['host']}:{cfg.get('port', 1883)}...")
        c.connect_async(cfg["host"], int(cfg.get("port", 1883)), keepalive=30)
        c.loop_start()
        self.cliente = c

    def _avisar_painel(self) -> None:
        if self.loop:
            self.loop.call_soon_threadsafe(notificar_painel)

    def _ao_conectar(self, cliente, _userdata, _flags, motivo, _props) -> None:
        if motivo.is_failure:
            log(f"HA: MQTT recusou a conexão ({motivo})")
            return
        dispositivo = {
            "identifiers": ["presenca-quarto"],
            "name": "Presença quarto",
            "manufacturer": "DFRobot C4001 + Kinect",
            "model": "mmWave validado pelo Kinect",
        }
        disponivel = f"{self.BASE}/disponivel"
        configs = {
            f"{self.DESCOBERTA}/config": {
                "name": None,
                "unique_id": "presenca_quarto",
                "default_entity_id": "binary_sensor.presenca_quarto",
                "device_class": "occupancy",
                "state_topic": f"{self.BASE}/presenca",
                "json_attributes_topic": f"{self.BASE}/atributos",
                "availability_topic": disponivel,
                "device": dispositivo,
            },
            f"{self.DESCOBERTA}_mmwave/config": {
                "name": "mmWave",
                "unique_id": "presenca_quarto_mmwave",
                "default_entity_id": "binary_sensor.presenca_quarto_mmwave",
                "device_class": "motion",
                "state_topic": f"{self.BASE}/mmwave",
                "availability_topic": disponivel,
                "entity_category": "diagnostic",
                "device": dispositivo,
            },
        }
        for topico, cfg in configs.items():
            cliente.publish(topico, json.dumps(cfg), qos=1, retain=True)
        cliente.publish(disponivel, "online", qos=1, retain=True)
        self.publicado.clear()  # o ciclo principal republica tudo no próximo giro
        estado.ha_ok = True
        log("HA: conectado")
        self._avisar_painel()

    def _ao_desconectar(self, _cliente, _userdata, _flags, motivo, _props) -> None:
        if estado.ha_ok:
            log(f"HA: desconectado do MQTT ({motivo}), tentando de novo...")
        estado.ha_ok = False
        self._avisar_painel()

    def publicar(self, topico: str, valor: str) -> None:
        """Publica só quando muda (ou depois de reconectar)."""
        if not self.cliente or not estado.ha_ok or self.publicado.get(topico) == valor:
            return
        info = self.cliente.publish(f"{self.BASE}/{topico}", valor, qos=1, retain=True)
        if info.rc == mqtt.MQTT_ERR_SUCCESS:
            self.publicado[topico] = valor

    def parar(self) -> None:
        if self.cliente:
            self.cliente.publish(f"{self.BASE}/disponivel", "offline", qos=1, retain=True).wait_for_publish(2)
            self.cliente.loop_stop()
            self.cliente.disconnect()

ha = HomeAssistant()


class LeitorKinect:
    """Lê o estado.json do serviço kinect-quarto."""
    def __init__(self) -> None:
        self.lido_em = 0.0
        self.dados: dict | None = None

    def situacao(self) -> str:
        """"pessoa" (Kinect confirmou alguém), "ninguem" ou "parado"
        (serviço do Kinect fora do ar / arquivo velho)."""
        agora = time.monotonic()
        if agora - self.lido_em >= 0.5:
            self.lido_em = agora
            try:
                self.dados = json.loads(KINECT_ESTADO.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.dados = None
        d = self.dados
        if not d or time.time() - float(d.get("salvo_em", 0)) > KINECT_PARADO_SEG:
            return "parado"
        return "pessoa" if d.get("pessoa") else "ninguem"

kinect = LeitorKinect()

# =========================================================================
# PRESENCA - estado em JSON e broadcast em tempo real (WebSocket)
# =========================================================================

presenca_clientes: set[web.WebSocketResponse] = set()

def estado_presenca_json() -> dict:
    duracao_seg = None
    if estado.presenca and estado.presenca_desde is not None:
        duracao_seg = round(time.monotonic() - estado.presenca_desde)
    return {
        "presence": estado.presenca,
        "ha": estado.ha_ok,
        "validada": estado.presenca and estado.validada,
        "validacao": estado.validacao,
        "duracao_seg": duracao_seg,
    }

async def broadcast_presenca() -> None:
    dados = estado_presenca_json()
    mortos = set()
    for ws in presenca_clientes:
        try:
            await asyncio.wait_for(ws.send_json(dados), ENVIO_WS_TIMEOUT_SEG)
        except Exception:
            mortos.add(ws)
    presenca_clientes.difference_update(mortos)

def notificar_painel() -> None:
    """Dispara o broadcast sem esperar: um navegador que sumiu da rede (ex:
    celular com o painel aberto saindo do Wi-Fi) não pode segurar quem chama."""
    try:
        asyncio.get_running_loop().create_task(broadcast_presenca())
    except RuntimeError:
        pass  # fora do event loop (ex: chamado antes do loop iniciar)

# =========================================================================
# HTTP ROUTES
# =========================================================================

async def rota_painel(_: web.Request) -> web.Response:
    html = """<!DOCTYPE html><html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Presença Quarto</title>
<style>
body { font-family: sans-serif; margin: 20px; background: #0a1e3c; color: #fff; }
.container { max-width: 600px; margin: 0 auto; }
h1 { color: #64c8ff; }
.stat { background: #1a3a5c; padding: 20px; border-radius: 10px; text-align: center; margin: 20px 0; }
.stat-value { font-size: 2em; font-weight: bold; }
.stat-value.on { color: #3fb950; }
.stat-value.off { color: #999; }
.stat-label { font-size: 0.9em; color: #999; margin-top: 5px; }
.card-sensor { text-align: left; }
.card-sensor h2 { font-size: 1em; color: #64c8ff; margin: 0 0 12px; }
.campo { display: flex; flex-direction: column; gap: 4px; margin-bottom: 10px; font-size: 0.85em; color: #ccc; }
.campo input { background: #0a1e3c; color: #fff; border: 1px solid #2a4a6c; border-radius: 6px; padding: 6px 8px; font-size: 1em; }
.btn-salvar { background: #1f6feb; color: #fff; border: none; border-radius: 6px; padding: 8px 16px;
              font-size: 0.9em; cursor: pointer; width: 100%; }
.btn-salvar:hover { background: #388bfd; }
.btn-salvar:disabled { opacity: 0.6; cursor: default; }
.msg-sensor { font-size: 0.8em; margin-top: 8px; min-height: 1.2em; }
.msg-sensor.ok { color: #3fb950; }
.msg-sensor.erro { color: #f85149; }
.links { margin-top: 20px; }
a { color: #64c8ff; text-decoration: none; margin-right: 20px; }
a:hover { text-decoration: underline; }
</style>
</head><body>
<div class='container'>
<h1>📡 Presença Quarto</h1>
<div class='stat'>
<div class='stat-value' id='presenca'>--</div>
<div class='stat-label'>Presença detectada</div>
<div class='stat-label' id='duracao'></div>
</div>
<div class='stat card-sensor'>
<h2>⚙️ Ajustes do sensor</h2>
<div class='campo'>
  <label for='minCm'>Alcance mínimo (cm)</label>
  <input type='number' id='minCm' min='30' max='2000' step='10'>
</div>
<div class='campo'>
  <label for='maxCm'>Alcance máximo (cm)</label>
  <input type='number' id='maxCm' min='240' max='2000' step='10'>
</div>
<div class='campo'>
  <label for='sensDisparo'>Sensibilidade de disparo (0-9)</label>
  <input type='number' id='sensDisparo' min='0' max='9' step='1'>
</div>
<div class='campo'>
  <label for='sensManutencao'>Sensibilidade de manutenção (0-9)</label>
  <input type='number' id='sensManutencao' min='0' max='9' step='1'>
</div>
<div class='campo'>
  <label for='atrasoDisparo'>Atraso de disparo (ms)</label>
  <input type='number' id='atrasoDisparo' min='0' max='2000' step='10'>
</div>
<div class='campo'>
  <label for='retencao'>Retenção após a última detecção (s)</label>
  <input type='number' id='retencao' min='2' max='1500' step='1'>
</div>
<div style='display:flex; gap:8px;'>
<button class='btn-salvar' id='btnCarregarSensor' disabled style='background:#21262d;'>Carregar atual</button>
<button class='btn-salvar' id='btnSalvarSensor' disabled>Salvar no sensor</button>
<button class='btn-salvar' id='btnPadraoSensor' disabled style='background:#21262d;'>Restaurar padrão</button>
</div>
<div class='msg-sensor' id='msgSensor'>Ler ou salvar interrompe a detecção do sensor por um instante.</div>
</div>
<div class='links'>
<a href='/presenca'>📊 JSON</a>
<a href='/logs'>📝 Logs</a>
</div>
</div>
<script>
function formatarDuracao(seg) {
    const h = Math.floor(seg / 3600);
    const m = Math.floor((seg % 3600) / 60);
    const s = seg % 60;
    if (h > 0) return `há ${h}h ${m}min`;
    if (m > 0) return `há ${m}min ${s}s`;
    return `há ${s}s`;
}

let ultimoEstado = null;
let ultimoEstadoEm = 0;

function renderizarPresenca() {
    if (!ultimoEstado) return;
    const el = document.getElementById('presenca');
    el.textContent = ultimoEstado.presence ? 'SIM' : 'NÃO';
    el.className = 'stat-value ' + (ultimoEstado.presence ? 'on' : 'off');
    const dur = document.getElementById('duracao');
    if (ultimoEstado.presence && ultimoEstado.duracao_seg != null) {
        const decorrido = Math.floor((performance.now() - ultimoEstadoEm) / 1000);
        dur.textContent = formatarDuracao(ultimoEstado.duracao_seg + decorrido);
    } else {
        dur.textContent = '';
    }
}

function conectarPresenca() {
    const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/presenca-ws');
    ws.onmessage = e => {
        ultimoEstado = JSON.parse(e.data);
        ultimoEstadoEm = performance.now();
        renderizarPresenca();
    };
    ws.onclose = () => setTimeout(conectarPresenca, 3000);
}
conectarPresenca();
setInterval(renderizarPresenca, 1000);

let wsSensor;
function habilitarBotoesSensor(habilitar) {
    for (const id of ['btnCarregarSensor', 'btnSalvarSensor', 'btnPadraoSensor']) {
        document.getElementById(id).disabled = !habilitar;
    }
}

function conectarSensor() {
    wsSensor = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/sensor-ws');
    const msg = document.getElementById('msgSensor');
    wsSensor.onopen = () => habilitarBotoesSensor(true);
    wsSensor.onmessage = e => {
        const d = JSON.parse(e.data);
        habilitarBotoesSensor(true);
        if (d.ok) {
            document.getElementById('minCm').value = d.min_cm;
            document.getElementById('maxCm').value = d.max_cm;
            document.getElementById('sensDisparo').value = d.sens_disparo;
            document.getElementById('sensManutencao').value = d.sens_manutencao;
            document.getElementById('atrasoDisparo').value = d.atraso_disparo_ms;
            document.getElementById('retencao').value = d.retencao_seg;
            msg.textContent = d.padrao ? 'Configuração padrão restaurada no sensor.'
                : d.aplicado ? 'Configuração salva no sensor.' : 'Configuração atual do sensor.';
            msg.className = 'msg-sensor ok';
        } else {
            msg.textContent = d.erro;
            msg.className = 'msg-sensor erro';
        }
    };
    wsSensor.onclose = () => {
        habilitarBotoesSensor(false);
        setTimeout(conectarSensor, 3000);
    };
}
conectarSensor();

document.getElementById('btnCarregarSensor').onclick = () => {
    if (!wsSensor || wsSensor.readyState !== WebSocket.OPEN) return;
    habilitarBotoesSensor(false);
    const msg = document.getElementById('msgSensor');
    msg.textContent = 'Lendo...';
    msg.className = 'msg-sensor';
    wsSensor.send(JSON.stringify({ acao: 'ler' }));
};

document.getElementById('btnSalvarSensor').onclick = () => {
    if (!wsSensor || wsSensor.readyState !== WebSocket.OPEN) return;
    const msg = document.getElementById('msgSensor');
    const minCm = parseInt(document.getElementById('minCm').value, 10);
    const maxCm = parseInt(document.getElementById('maxCm').value, 10);
    const sensDisparo = parseInt(document.getElementById('sensDisparo').value, 10);
    const sensManutencao = parseInt(document.getElementById('sensManutencao').value, 10);
    const atrasoDisparo = parseInt(document.getElementById('atrasoDisparo').value, 10);
    const retencao = parseInt(document.getElementById('retencao').value, 10);
    if ([minCm, maxCm, sensDisparo, sensManutencao, atrasoDisparo, retencao].some(isNaN)) {
        msg.textContent = 'Preencha todos os campos (ou clique em "Carregar atual" primeiro).';
        msg.className = 'msg-sensor erro';
        return;
    }
    habilitarBotoesSensor(false);
    msg.textContent = 'Salvando...';
    msg.className = 'msg-sensor';
    wsSensor.send(JSON.stringify({
        acao: 'salvar', min_cm: minCm, max_cm: maxCm,
        sens_disparo: sensDisparo, sens_manutencao: sensManutencao,
        atraso_disparo_ms: atrasoDisparo, retencao_seg: retencao,
    }));
};

document.getElementById('btnPadraoSensor').onclick = () => {
    if (!wsSensor || wsSensor.readyState !== WebSocket.OPEN) return;
    if (!confirm('Restaurar a configuração padrão no sensor? Os ajustes atuais serão substituídos.')) return;
    habilitarBotoesSensor(false);
    const msg = document.getElementById('msgSensor');
    msg.textContent = 'Restaurando padrão...';
    msg.className = 'msg-sensor';
    wsSensor.send(JSON.stringify({ acao: 'padrao' }));
};
</script>
</body></html>"""
    return web.Response(text=html, content_type="text/html")

async def rota_presenca(_: web.Request) -> web.Response:
    return web.json_response(estado_presenca_json())

async def rota_presenca_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    presenca_clientes.add(ws)

    try:
        await ws.send_json(estado_presenca_json())
        async for msg in ws:
            pass
    finally:
        presenca_clientes.discard(ws)

    return ws

async def rota_logs(request: web.Request) -> web.StreamResponse:
    if request.headers.get("Upgrade", "").lower() != "websocket":
        historico = json.dumps(console.historico()).replace("</", "<\\/")
        html = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Logs - Presença Quarto</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { background: #0d1117; color: #c9d1d9; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       height: 100vh; display: flex; flex-direction: column; }
header { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; padding: 10px 14px;
         background: #161b22; border-bottom: 1px solid #30363d; }
header h1 { font-size: 17px; margin-right: auto; }
.estado { font-size: 12px; color: #8b949e; display: flex; align-items: center; gap: 6px; }
.ponto { width: 9px; height: 9px; border-radius: 50%; background: #f85149; }
.ponto.on { background: #3fb950; }
input[type=search] { background: #0d1117; color: #c9d1d9; border: 1px solid #30363d; border-radius: 6px;
                     padding: 6px 10px; font-size: 13px; min-width: 160px; }
button, a.btn { background: #21262d; color: #c9d1d9; border: 1px solid #30363d; border-radius: 6px;
                padding: 6px 10px; font-size: 13px; cursor: pointer; text-decoration: none; }
button:hover, a.btn:hover { background: #30363d; }
button.ativo { background: #1f6feb; border-color: #1f6feb; color: #fff; }
#logs { flex: 1; overflow-y: auto; padding: 10px 14px; font-family: ui-monospace, Menlo, Consolas, monospace;
        font-size: 12.5px; line-height: 1.5; white-space: pre-wrap; word-break: break-word; }
.linha.erro { color: #f85149; }
.linha.aviso { color: #d29922; }
.linha.ok { color: #3fb950; }
.linha.leitura { color: #79c0ff; }
.linha .hora { color: #6e7681; }
footer { font-size: 12px; color: #8b949e; padding: 6px 14px; background: #161b22; border-top: 1px solid #30363d; }
</style>
</head>
<body>
<header>
  <h1>📜 Console Remoto</h1>
  <span class="estado"><span class="ponto" id="ponto"></span><span id="conexao">conectando...</span></span>
  <input type="search" id="filtro" placeholder="Filtrar...">
  <button id="btnRolar" class="ativo" title="Rolar automaticamente para o fim">⬇ Auto</button>
  <button id="btnPausar">⏸ Pausar</button>
  <button id="btnLimpar">🗑 Limpar</button>
  <button id="btnBaixar">💾 Baixar</button>
  <a class="btn" href="/">← Voltar</a>
</header>
<div id="logs"></div>
<footer><span id="contagem">0 linhas</span></footer>
<script>
const HISTORICO = __HISTORICO__;
const MAX = 2000;
const logs = document.getElementById('logs');
const filtro = document.getElementById('filtro');
const ponto = document.getElementById('ponto');
const conexao = document.getElementById('conexao');
const contagem = document.getElementById('contagem');
const btnRolar = document.getElementById('btnRolar');
const btnPausar = document.getElementById('btnPausar');
let linhas = [];
let pendentes = [];
let rolar = true;
let pausado = false;

function classe(texto) {
  const t = texto.toLowerCase();
  if (t.includes('presenca:')) return 'leitura';
  if (/erro|falha|traceback|exception|stderr|desconectado/.test(t)) return 'erro';
  if (/aviso|warning|reconect|sem dados|não configurad/.test(t)) return 'aviso';
  if (/conectado|ok\b|iniciado/.test(t)) return 'ok';
  return '';
}

function criar(texto) {
  const div = document.createElement('div');
  div.className = 'linha ' + classe(texto);
  const m = texto.match(/^(\[[^\]]+\])(.*)$/);
  if (m) {
    const hora = document.createElement('span');
    hora.className = 'hora';
    hora.textContent = m[1];
    div.append(hora, m[2]);
  } else {
    div.textContent = texto;
  }
  div.dataset.texto = texto.toLowerCase();
  return div;
}

function visivel(div) {
  const f = filtro.value.trim().toLowerCase();
  return !f || div.dataset.texto.includes(f);
}

function adicionar(texto) {
  linhas.push(texto);
  const div = criar(texto);
  div.hidden = !visivel(div);
  logs.appendChild(div);
  while (linhas.length > MAX) { linhas.shift(); logs.firstChild.remove(); }
}

function atualizarRodape() {
  const vis = logs.querySelectorAll('.linha:not([hidden])').length;
  contagem.textContent = (vis === linhas.length ? linhas.length + ' linhas' : vis + ' de ' + linhas.length + ' linhas')
    + (pausado && pendentes.length ? ' · ' + pendentes.length + ' novas em espera' : '');
}

function fim() { if (rolar) logs.scrollTop = logs.scrollHeight; }

HISTORICO.forEach(adicionar);
atualizarRodape();
fim();

filtro.addEventListener('input', () => {
  logs.querySelectorAll('.linha').forEach(d => d.hidden = !visivel(d));
  atualizarRodape();
  fim();
});

btnRolar.onclick = () => { rolar = !rolar; btnRolar.classList.toggle('ativo', rolar); fim(); };

logs.addEventListener('scroll', () => {
  const noFim = logs.scrollHeight - logs.scrollTop - logs.clientHeight < 30;
  if (rolar !== noFim) { rolar = noFim; btnRolar.classList.toggle('ativo', rolar); }
});

btnPausar.onclick = () => {
  pausado = !pausado;
  btnPausar.textContent = pausado ? '▶ Continuar' : '⏸ Pausar';
  btnPausar.classList.toggle('ativo', pausado);
  if (!pausado) { pendentes.forEach(adicionar); pendentes = []; fim(); }
  atualizarRodape();
};

document.getElementById('btnLimpar').onclick = () => {
  linhas = []; pendentes = []; logs.innerHTML = ''; atualizarRodape();
};

document.getElementById('btnBaixar').onclick = () => {
  const blob = new Blob([linhas.join('\n') + '\n'], { type: 'text/plain' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'presenca-quarto-' + new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-') + '.log';
  a.click();
  URL.revokeObjectURL(a.href);
};

function conectar() {
  const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/logs');
  ws.onopen = () => { ponto.classList.add('on'); conexao.textContent = 'ao vivo'; };
  ws.onmessage = e => {
    if (pausado) { pendentes.push(e.data); }
    else { adicionar(e.data); fim(); }
    atualizarRodape();
  };
  ws.onclose = () => {
    ponto.classList.remove('on');
    conexao.textContent = 'desconectado, tentando de novo...';
    setTimeout(conectar, 3000);
  };
}
conectar();
</script>
</body>
</html>""".replace("__HISTORICO__", historico)
        return web.Response(text=html, content_type="text/html")

    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    console.clientes.add(ws)

    try:
        async for msg in ws:
            pass
    finally:
        console.clientes.discard(ws)

    return ws

async def falar_com_sensor(funcao, *args) -> dict:
    """Roda um comando UART segurando a ausência: o sensor para e reinicia
    a detecção a cada comando, e o pino OUT cai enquanto isso."""
    async with sensor_uart_lock:
        try:
            return await asyncio.to_thread(funcao, *args)
        finally:
            estado.segurar_ausencia_ate = time.monotonic() + SENSOR_AQUECIMENTO_SEG

async def rota_sensor_ws(request: web.Request) -> web.WebSocketResponse:
    """Só fala com o sensor (UART) quando o cliente pede - ler ou salvar
    interrompem a detecção por um instante (o sensor para/reinicia a
    cada comando), então nada acontece sozinho ao só abrir a conexão."""
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    async for msg in ws:
        if msg.type != web.WSMsgType.TEXT:
            continue
        try:
            dados = json.loads(msg.data)
            acao = dados["acao"]
        except (json.JSONDecodeError, KeyError, TypeError):
            await ws.send_json({"ok": False, "erro": "dados inválidos"})
            continue

        if acao == "ler":
            log("SENSOR-UART: lendo configuração atual")
            try:
                atual = await falar_com_sensor(sensor_uart.ler)
                await ws.send_json({"ok": True, "aplicado": False, **atual})
            except Exception as e:
                log(f"SENSOR-UART: falha ao ler ({e})")
                await ws.send_json({"ok": False, "erro": str(e)})
            continue

        if acao == "padrao":
            log("SENSOR-UART: restaurando configuração padrão")
            dados = {**dados, **CONFIG_PADRAO}
        elif acao != "salvar":
            await ws.send_json({"ok": False, "erro": "ação inválida"})
            continue

        try:
            min_cm = int(dados["min_cm"])
            max_cm = int(dados["max_cm"])
            sens_disparo = int(dados["sens_disparo"])
            sens_manutencao = int(dados["sens_manutencao"])
            atraso_disparo_ms = int(dados["atraso_disparo_ms"])
            retencao_seg = int(dados["retencao_seg"])
        except (KeyError, TypeError, ValueError):
            await ws.send_json({"ok": False, "erro": "dados inválidos"})
            continue

        log(
            f"SENSOR-UART: aplicando alcance {min_cm}-{max_cm}cm, sensibilidade "
            f"disparo {sens_disparo} / manutenção {sens_manutencao}, "
            f"atraso de disparo {atraso_disparo_ms}ms, retenção {retencao_seg}s"
        )
        try:
            novo = await falar_com_sensor(
                sensor_uart.aplicar, min_cm, max_cm, sens_disparo, sens_manutencao,
                atraso_disparo_ms, retencao_seg)
        except Exception as e:
            log(f"SENSOR-UART: falha ao aplicar ({e})")
            await ws.send_json({"ok": False, "erro": str(e)})
            continue

        log(
            f"SENSOR-UART: configuração salva (min={novo['min_cm']}cm "
            f"max={novo['max_cm']}cm disparo={novo['sens_disparo']} "
            f"manutenção={novo['sens_manutencao']} atraso={novo['atraso_disparo_ms']}ms "
            f"retenção={novo['retencao_seg']}s)"
        )
        await ws.send_json({"ok": True, "aplicado": True, "padrao": acao == "padrao", **novo})

    return ws

# =========================================================================
# SERVIDOR
# =========================================================================

async def subir_servidor() -> web.AppRunner:
    app = web.Application()
    app.add_routes([
        web.get("/", rota_painel),
        web.get("/presenca", rota_presenca),
        web.get("/presenca-ws", rota_presenca_ws),
        web.get("/logs", rota_logs),
        web.get("/sensor-ws", rota_sensor_ws),
    ])

    runner = web.AppRunner(app)
    await runner.setup()

    await web.TCPSite(runner, HOST_WEB, PORTA_WEB).start()
    log(f"WEB: http://{HOST_WEB}:{PORTA_WEB}/")
    log(f"WEB: http://{HOST_WEB}:{PORTA_WEB}/logs")

    return runner

# =========================================================================
# WATCHDOG (systemd WatchdogSec + vigia interno do ciclo)
# =========================================================================

def avisar_systemd(msg: str) -> None:
    """sd_notify sem depender de biblioteca: manda um datagrama para o
    socket do systemd. Fora do systemd (NOTIFY_SOCKET vazio) não faz nada."""
    endereco = os.environ.get("NOTIFY_SOCKET")
    if not endereco:
        return
    if endereco.startswith("@"):
        endereco = "\0" + endereco[1:]  # socket abstrato
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.setblocking(False)
            s.sendto(msg.encode(), endereco)
    except OSError:
        pass

async def vigiar_ciclo(tarefa: asyncio.Task) -> None:
    """Se o ciclo parar de girar, registra onde ele empacou e encerra o
    processo para o systemd subir de novo (o estado é retomado do disco).

    Cobre o ciclo preso num await; se o event loop inteiro travar (chamada
    síncrona presa), este vigia também para e quem pega é o WatchdogSec do
    systemd, que mata com SIGABRT e o faulthandler despeja as pilhas."""
    while True:
        await asyncio.sleep(5)
        parado = time.monotonic() - estado.ciclo_em
        if parado < CICLO_TRAVADO_SEG:
            continue
        log(f"TRAVADO: ciclo principal sem girar há {parado:.0f}s, reiniciando. Pilha:")
        for quadro in tarefa.get_stack():
            log(f"TRAVADO:   {quadro.f_code.co_filename}:{quadro.f_lineno} em {quadro.f_code.co_name}")
        salvar_estado()
        sys.stdout.flush()
        os._exit(1)  # systemd (Restart=always) sobe de novo

# =========================================================================
# LOOP PRINCIPAL
# =========================================================================

async def ciclo(leitor) -> None:
    """Le o sensor a cada 300ms. Presenca liga na hora; desliga só depois
    de ficar continuamente ausente por 'ATRASO_AUSENCIA_SEG' (evita flicker
    quando a pessoa fica parada e o mmWave perde o rastreio por instantes).

    Cada nova detecção só vira presença no Home Assistant depois que o
    Kinect confirmar uma pessoa (ver HomeAssistant / LeitorKinect). O que é
    publicado é conferido a cada ciclo, então nada fica perdido se o MQTT
    cair e voltar.

    O estado vai para disco a cada mudança e periodicamente, para um
    reinício rápido retomar de onde parou (ver restaurar_estado)."""
    ausente_desde: float | None = None
    salvo_em = 0.0

    while True:
        await asyncio.sleep(0.3)
        estado.ciclo_em = time.monotonic()
        avisar_systemd("WATCHDOG=1")

        try:
            detectado = leitor.detectado()
        except Exception as e:
            log(f"SENSOR: erro na leitura ({e})")
            continue

        agora = time.monotonic()

        if detectado:
            ausente_desde = None
            if not estado.presenca:
                estado.presenca = True
                estado.presenca_desde = agora
                estado.validada = False
                estado.validacao = "pendente" if KINECT_VALIDAR else ""
                log("PRESENCA: detectada pelo mmWave" + (", aguardando o Kinect" if KINECT_VALIDAR else ""))
                salvar_estado()
                notificar_painel()
        else:
            if estado.presenca:
                if sensor_uart_lock.locked() or agora < estado.segurar_ausencia_ate:
                    ausente_desde = None  # sensor reiniciando: OUT baixo não é ausência
                elif ausente_desde is None:
                    ausente_desde = agora
                elif agora - ausente_desde >= ATRASO_AUSENCIA_SEG:
                    estado.presenca = False
                    estado.presenca_desde = None
                    estado.validada = False
                    estado.validacao = ""
                    log(f"PRESENCA: ausente (sem detecção por {ATRASO_AUSENCIA_SEG:.0f}s)")
                    salvar_estado()
                    notificar_painel()

        if estado.presenca and not estado.validada:
            validacao = None
            if not KINECT_VALIDAR:
                validacao = "mmwave"
            else:
                situacao = kinect.situacao()
                if situacao == "pessoa":
                    validacao = "kinect"
                    log("PRESENCA: confirmada pelo Kinect")
                elif (situacao == "parado" and estado.presenca_desde is not None
                      and agora - estado.presenca_desde >= KINECT_PARADO_SEG):
                    validacao = "mmwave"
                    log("PRESENCA: Kinect fora do ar, usando só o mmWave")
            if validacao:
                estado.validada = True
                estado.validacao = validacao
                salvar_estado()
                notificar_painel()

        ha.publicar("mmwave", "ON" if estado.presenca else "OFF")
        ha.publicar("presenca", "ON" if estado.presenca and estado.validada else "OFF")
        ha.publicar("atributos", json.dumps({"validacao": estado.validacao or None}))

        # marca "ainda rodando" para o restaurar_estado medir o tempo parado
        if agora - salvo_em >= 30:
            salvar_estado()
            salvo_em = agora

async def principal(simular: bool) -> None:
    leitor = montar_leitor(simular)
    log(f"SENSOR: {'simulado' if simular else 'GPIO'}")
    restaurar_estado()

    try:
        ip = socket.gethostbyname(socket.gethostname())
        log(f"REDE: IP {ip}")
    except Exception:
        pass

    ha.iniciar()
    runner = await subir_servidor()
    estado.ciclo_em = time.monotonic()
    tarefa_ciclo = asyncio.create_task(ciclo(leitor))
    vigia_ciclo = asyncio.create_task(vigiar_ciclo(tarefa_ciclo))
    avisar_systemd("READY=1")

    try:
        await tarefa_ciclo
    except KeyboardInterrupt:
        log("Encerrando...")
    finally:
        vigia_ciclo.cancel()
        salvar_estado()
        await runner.cleanup()
        ha.parar()

# =========================================================================

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--simular", action="store_true", help="Modo simulado (sem hardware)")
    args = parser.parse_args()

    try:
        asyncio.run(principal(args.simular))
    except KeyboardInterrupt:
        log("Encerrado pelo usuário")
    except Exception as e:
        log(f"ERRO: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
