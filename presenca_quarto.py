#!/usr/bin/env python3
"""
Sensor de presença mmWave (C4001 25m) - Raspberry Pi + Sinric Pro
Lê o pino digital OUT do sensor e envia eventos de movimento (motion) para
a Sinric Pro (SDK oficial `sinricpro`), com um pequeno painel web e console
remoto para depuração.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from aiohttp import web
from gpiozero import DigitalInputDevice
from sinricpro import SinricPro, SinricProConfig, SinricProMotionSensor

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

SINRIC_DEVICE_ID = os.environ.get("SINRIC_DEVICE_ID", "")
SINRIC_APP_KEY = os.environ.get("SINRIC_APP_KEY", "")
SINRIC_APP_SECRET = os.environ.get("SINRIC_APP_SECRET", "")
SINRIC_DEBUG = env_bool("SINRIC_DEBUG", False)

SENSOR_UART_BAUD = env_int("SENSOR_UART_BAUD", 9600)

# =========================================================================
# LOGGING - Console remoto
# =========================================================================

class Estado:
    """Estado compartilhado da aplicação."""
    def __init__(self) -> None:
        self.presenca = False
        self.sinric_ok = False
        self.sinric_confirmado: bool | None = None  # último valor confirmado (enviado com sucesso)
        self.presenca_desde: float | None = None  # time.monotonic() de quando a presença atual começou

estado = Estado()

class Console:
    """Redireciona print() e erros (stdout/stderr) para log e WebSocket."""
    def __init__(self) -> None:
        self.linhas = deque(maxlen=1000)
        self.clientes = set()
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
                await ws.send_str(msg)
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

def log(msg: str) -> None:
    print(msg)

# =========================================================================
# SENSOR DE PRESENCA (pino OUT do C4001)
# =========================================================================

class LeitorPresenca:
    """Lê o pino digital OUT do sensor mmWave C4001."""
    def __init__(self, gpio: int, ativo_alto: bool) -> None:
        self.ativo_alto = ativo_alto
        self.pino = DigitalInputDevice(gpio, pull_up=False, bounce_time=0.05)
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

class SensorUART:
    """Lê/ajusta alcance e sensibilidade do C4001 pela UART (RX/TX) -
    separado da detecção pelo pino OUT. Abre e fecha a porta a cada
    operação: evita manter uma conexão ociosa e disputar com o
    configurar_sensor.py (CLI) se alguém rodar os dois ao mesmo tempo."""

    def ler(self) -> dict:
        radar = DFRobot_C4001_UART(SENSOR_UART_BAUD)
        try:
            return {
                "min_cm": int(radar.get_min_range()),
                "max_cm": int(radar.get_max_range()),
                "sensibilidade": int(radar.get_trig_sensitivity()),
            }
        except Exception as e:
            raise RuntimeError("sensor não respondeu (confira a fiação RX/TX)") from e
        finally:
            radar.ser.close()

    def aplicar(self, min_cm: int, max_cm: int, sensibilidade: int) -> dict:
        if not (RANGE_MIN_CM[0] <= min_cm <= RANGE_MIN_CM[1]):
            raise ValueError(f"alcance mínimo deve ser {RANGE_MIN_CM[0]}-{RANGE_MIN_CM[1]}cm")
        if not (RANGE_MAX_CM[0] <= max_cm <= RANGE_MAX_CM[1]):
            raise ValueError(f"alcance máximo deve ser {RANGE_MAX_CM[0]}-{RANGE_MAX_CM[1]}cm")
        if min_cm > max_cm:
            raise ValueError("alcance mínimo não pode ser maior que o máximo")
        if not (SENSIBILIDADE_LIMITE[0] <= sensibilidade <= SENSIBILIDADE_LIMITE[1]):
            raise ValueError(f"sensibilidade deve ser {SENSIBILIDADE_LIMITE[0]}-{SENSIBILIDADE_LIMITE[1]}")

        radar = DFRobot_C4001_UART(SENSOR_UART_BAUD)
        try:
            radar.set_sensor_mode(EXIST_MODE)
            radar.set_detection_range(min_cm, max_cm, max_cm)
            radar.set_trig_sensitivity(sensibilidade)
            radar.set_keep_sensitivity(sensibilidade)
            time.sleep(0.3)
            return {
                "min_cm": int(radar.get_min_range()),
                "max_cm": int(radar.get_max_range()),
                "sensibilidade": int(radar.get_trig_sensitivity()),
            }
        except Exception as e:
            raise RuntimeError("sensor não respondeu (confira a fiação RX/TX)") from e
        finally:
            radar.ser.close()

sensor_uart = SensorUART()
sensor_uart_lock = asyncio.Lock()  # evita duas abas mexendo na UART ao mesmo tempo

# =========================================================================
# SINRIC PRO (capacidade Motion Sensor)
# =========================================================================

class Sinric:
    """Publica o estado de presença na Sinric Pro via SDK oficial (sinricpro)."""
    def __init__(self) -> None:
        self.sensor: SinricProMotionSensor | None = None

    @property
    def configurado(self) -> bool:
        return bool(SINRIC_DEVICE_ID and SINRIC_APP_KEY and SINRIC_APP_SECRET)

    async def iniciar(self) -> None:
        if not self.configurado:
            log("SINRIC: credenciais não configuradas")
            return

        self.sensor = SinricProMotionSensor(SINRIC_DEVICE_ID)

        sinric_pro = SinricPro.get_instance()
        sinric_pro.on_connected(self._ao_conectar)
        sinric_pro.on_disconnected(self._ao_desconectar)
        sinric_pro.add(self.sensor)

        log("SINRIC: conectando...")
        await sinric_pro.begin(SinricProConfig(
            app_key=SINRIC_APP_KEY,
            app_secret=SINRIC_APP_SECRET,
            debug=SINRIC_DEBUG,
        ))

    def _ao_conectar(self) -> None:
        estado.sinric_ok = True
        log("SINRIC: conectado")
        # invalida a confirmação: o ciclo principal reenvia o estado atual
        # sozinho no próximo giro (evita disputar o rate limit de eventos
        # com uma detecção real que aconteça no mesmo instante)
        estado.sinric_confirmado = None
        self._notificar_painel()

    def _ao_desconectar(self) -> None:
        estado.sinric_ok = False
        log("SINRIC: desconectado")
        self._notificar_painel()

    def _notificar_painel(self) -> None:
        try:
            asyncio.get_running_loop().create_task(broadcast_presenca())
        except RuntimeError:
            pass  # fora do event loop (ex: chamado antes do loop iniciar)

    async def enviar_presenca(self, ativo: bool) -> bool:
        if not self.sensor:
            return False
        try:
            return await self.sensor.send_motion_event(ativo)
        except Exception as erro:
            log(f"SINRIC: falha ao enviar ({erro})")
            return False

    async def parar(self) -> None:
        if self.sensor:
            await SinricPro.get_instance().stop()

sinric = Sinric()

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
        "sinric": estado.sinric_ok,
        "duracao_seg": duracao_seg,
    }

async def broadcast_presenca() -> None:
    dados = estado_presenca_json()
    mortos = set()
    for ws in presenca_clientes:
        try:
            await ws.send_json(dados)
        except Exception:
            mortos.add(ws)
    presenca_clientes.difference_update(mortos)

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
<h2>⚙️ Alcance e sensibilidade do sensor</h2>
<div class='campo'>
  <label for='minCm'>Alcance mínimo (cm)</label>
  <input type='number' id='minCm' min='30' max='2000' step='10'>
</div>
<div class='campo'>
  <label for='maxCm'>Alcance máximo (cm)</label>
  <input type='number' id='maxCm' min='240' max='2000' step='10'>
</div>
<div class='campo'>
  <label for='sensib'>Sensibilidade (0-9)</label>
  <input type='number' id='sensib' min='0' max='9' step='1'>
</div>
<button class='btn-salvar' id='btnSalvarSensor' disabled>Salvar no sensor</button>
<div class='msg-sensor' id='msgSensor'>Lendo configuração atual...</div>
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
function conectarSensor() {
    wsSensor = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/sensor-ws');
    const msg = document.getElementById('msgSensor');
    const btn = document.getElementById('btnSalvarSensor');
    wsSensor.onmessage = e => {
        const d = JSON.parse(e.data);
        btn.disabled = false;
        if (d.ok) {
            document.getElementById('minCm').value = d.min_cm;
            document.getElementById('maxCm').value = d.max_cm;
            document.getElementById('sensib').value = d.sensibilidade;
            msg.textContent = d.aplicado ? 'Configuração salva no sensor.' : 'Configuração atual do sensor.';
            msg.className = 'msg-sensor ok';
        } else {
            msg.textContent = d.erro;
            msg.className = 'msg-sensor erro';
        }
    };
    wsSensor.onclose = () => { btn.disabled = true; setTimeout(conectarSensor, 3000); };
}
conectarSensor();

document.getElementById('btnSalvarSensor').onclick = () => {
    if (!wsSensor || wsSensor.readyState !== WebSocket.OPEN) return;
    const btn = document.getElementById('btnSalvarSensor');
    const msg = document.getElementById('msgSensor');
    btn.disabled = true;
    msg.textContent = 'Salvando...';
    msg.className = 'msg-sensor';
    wsSensor.send(JSON.stringify({
        min_cm: parseInt(document.getElementById('minCm').value, 10),
        max_cm: parseInt(document.getElementById('maxCm').value, 10),
        sensibilidade: parseInt(document.getElementById('sensib').value, 10),
    }));
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

async def rota_sensor_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    async with sensor_uart_lock:
        try:
            atual = await asyncio.to_thread(sensor_uart.ler)
            await ws.send_json({"ok": True, "aplicado": False, **atual})
        except Exception as e:
            await ws.send_json({"ok": False, "erro": str(e)})

    async for msg in ws:
        if msg.type != web.WSMsgType.TEXT:
            continue
        try:
            dados = json.loads(msg.data)
            min_cm = int(dados["min_cm"])
            max_cm = int(dados["max_cm"])
            sensibilidade = int(dados["sensibilidade"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            await ws.send_json({"ok": False, "erro": "dados inválidos"})
            continue

        log(f"SENSOR-UART: aplicando alcance {min_cm}-{max_cm}cm, sensibilidade {sensibilidade}")
        async with sensor_uart_lock:
            try:
                novo = await asyncio.to_thread(sensor_uart.aplicar, min_cm, max_cm, sensibilidade)
            except Exception as e:
                log(f"SENSOR-UART: falha ao aplicar ({e})")
                await ws.send_json({"ok": False, "erro": str(e)})
                continue

        log(
            f"SENSOR-UART: configuração salva (min={novo['min_cm']}cm "
            f"max={novo['max_cm']}cm sensibilidade={novo['sensibilidade']})"
        )
        await ws.send_json({"ok": True, "aplicado": True, **novo})

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

    await web.TCPSite(runner, "0.0.0.0", PORTA_WEB).start()
    log(f"WEB: http://0.0.0.0:{PORTA_WEB}/")
    log(f"WEB: http://0.0.0.0:{PORTA_WEB}/logs")

    return runner

# =========================================================================
# LOOP PRINCIPAL
# =========================================================================

async def ciclo(leitor) -> None:
    """Le o sensor a cada 300ms. Presenca liga na hora; desliga só depois
    de ficar continuamente ausente por 'ATRASO_AUSENCIA_SEG' (evita flicker
    quando a pessoa fica parada e o mmWave perde o rastreio por instantes).

    O envio à Sinric é conferido a cada ciclo (não só na transição): se o
    último envio falhou (rate limit do SDK, reconexão etc.) ele é repetido
    até ser confirmado, para nenhuma mudança de estado ficar perdida."""
    ausente_desde: float | None = None
    falha_avisada = False

    while True:
        await asyncio.sleep(0.3)

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
                log("PRESENCA: detectada")
                await broadcast_presenca()
        else:
            if estado.presenca:
                if ausente_desde is None:
                    ausente_desde = agora
                elif agora - ausente_desde >= ATRASO_AUSENCIA_SEG:
                    estado.presenca = False
                    estado.presenca_desde = None
                    log(f"PRESENCA: ausente (sem detecção por {ATRASO_AUSENCIA_SEG:.0f}s)")
                    await broadcast_presenca()

        if estado.sinric_confirmado != estado.presenca:
            if await sinric.enviar_presenca(estado.presenca):
                estado.sinric_confirmado = estado.presenca
                falha_avisada = False
            elif not falha_avisada:
                log("SINRIC: evento não enviado (desconectado ou rate limit), tentando de novo...")
                falha_avisada = True

async def principal(simular: bool) -> None:
    leitor = montar_leitor(simular)
    log(f"SENSOR: {'simulado' if simular else 'GPIO'}")

    try:
        ip = socket.gethostbyname(socket.gethostname())
        log(f"REDE: IP {ip}")
    except Exception:
        pass

    await sinric.iniciar()
    runner = await subir_servidor()

    try:
        await ciclo(leitor)
    except KeyboardInterrupt:
        log("Encerrando...")
    finally:
        await runner.cleanup()
        await sinric.parar()

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
