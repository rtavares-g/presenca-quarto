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

# =========================================================================
# LOGGING - Console remoto
# =========================================================================

class Estado:
    """Estado compartilhado da aplicação."""
    def __init__(self) -> None:
        self.presenca = False
        self.sinric_ok = False

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
        # sincroniza o estado atual assim que (re)conecta
        asyncio.get_running_loop().create_task(self.enviar_presenca(estado.presenca))

    def _ao_desconectar(self) -> None:
        estado.sinric_ok = False
        log("SINRIC: desconectado")

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
</div>
<div class='links'>
<a href='/presenca'>📊 JSON</a>
<a href='/logs'>📝 Logs</a>
</div>
</div>
<script>
async function atualizar() {
    const r = await fetch('/presenca');
    const d = await r.json();
    const el = document.getElementById('presenca');
    el.textContent = d.presence ? 'SIM' : 'NÃO';
    el.className = 'stat-value ' + (d.presence ? 'on' : 'off');
}
atualizar();
setInterval(atualizar, 2000);
</script>
</body></html>"""
    return web.Response(text=html, content_type="text/html")

async def rota_presenca(_: web.Request) -> web.Response:
    return web.json_response({
        "presence": estado.presenca,
        "sinric": estado.sinric_ok,
    })

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

# =========================================================================
# SERVIDOR
# =========================================================================

async def subir_servidor() -> web.AppRunner:
    app = web.Application()
    app.add_routes([
        web.get("/", rota_painel),
        web.get("/presenca", rota_presenca),
        web.get("/logs", rota_logs),
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
    quando a pessoa fica parada e o mmWave perde o rastreio por instantes)."""
    ausente_desde: float | None = None

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
                log("PRESENCA: detectada")
                enviado = await sinric.enviar_presenca(True)
                if not enviado:
                    log("SINRIC: evento não enviado (desconectado ou rate limit)")
        else:
            if estado.presenca:
                if ausente_desde is None:
                    ausente_desde = agora
                elif agora - ausente_desde >= ATRASO_AUSENCIA_SEG:
                    estado.presenca = False
                    log(f"PRESENCA: ausente (sem detecção por {ATRASO_AUSENCIA_SEG:.0f}s)")
                    enviado = await sinric.enviar_presenca(False)
                    if not enviado:
                        log("SINRIC: evento não enviado (desconectado ou rate limit)")

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
