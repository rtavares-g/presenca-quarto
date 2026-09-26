#!/usr/bin/env python3
"""
Ajusta alcance, sensibilidade, atraso de disparo e retenção do sensor mmWave
C4001 via UART (pinos RX/TX).

É uma ferramenta separada do presenca_quarto.py: o script principal só lê o
pino digital OUT e não fala com o sensor por UART. Os parâmetros abaixo
ficam salvos na memória do próprio sensor, então só é preciso rodar isto
uma vez (ou de novo quando quiser mudar o ajuste) - não precisa rodar a
cada boot.

Requer:
- UART habilitada no Raspberry Pi (enable_uart=1 e Bluetooth desligado no
  Pi 3B+, veja o README) e RX/TX do sensor ligados no GPIO14/15.
- Biblioteca DFRobot_C4001.py (incluída neste repositório).
"""

from __future__ import annotations

import argparse
import sys
import time

from DFRobot_C4001 import DFRobot_C4001_UART, EXIST_MODE

MIN_CM_LIMITE = (30, 2000)
MAX_CM_LIMITE = (240, 2000)
SENSIBILIDADE_LIMITE = (0, 9)
ATRASO_DISPARO_LIMITE_MS = (0, 2000)
RETENCAO_LIMITE_SEG = (2, 1500)


def analisar_argumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--baud", type=int, default=9600, help="Baud rate da UART (padrão 9600)")
    parser.add_argument("--min-cm", type=int, default=30, help="Distância mínima de detecção, em cm (padrão 30)")
    parser.add_argument("--max-cm", type=int, default=300, help="Distância máxima de detecção, em cm (padrão 300 = 3m)")
    parser.add_argument("--sensibilidade-disparo", type=int, default=2,
                        help="Sensibilidade 0-9 para começar a detectar: quanto menor, mais movimento é preciso (padrão 2)")
    parser.add_argument("--sensibilidade-manutencao", type=int, default=2,
                        help="Sensibilidade 0-9 para continuar detectando alguém parado (padrão 2)")
    parser.add_argument("--sensibilidade", type=int,
                        help="Atalho: usa o mesmo valor para disparo e manutenção")
    parser.add_argument("--atraso-disparo-ms", type=int, default=0,
                        help="Tempo que a detecção precisa durar para o OUT subir, 0-2000ms (padrão 0)")
    parser.add_argument("--retencao-seg", type=int, default=15,
                        help="Tempo que o OUT segue alto após a última detecção, 2-1500s (padrão 15)")
    args = parser.parse_args()
    if args.sensibilidade is not None:
        args.sensibilidade_disparo = args.sensibilidade_manutencao = args.sensibilidade
    return args


def validar(args: argparse.Namespace) -> None:
    if not (MIN_CM_LIMITE[0] <= args.min_cm <= MIN_CM_LIMITE[1]):
        sys.exit(f"--min-cm precisa estar entre {MIN_CM_LIMITE[0]} e {MIN_CM_LIMITE[1]}")
    if not (MAX_CM_LIMITE[0] <= args.max_cm <= MAX_CM_LIMITE[1]):
        sys.exit(f"--max-cm precisa estar entre {MAX_CM_LIMITE[0]} e {MAX_CM_LIMITE[1]}")
    if args.min_cm > args.max_cm:
        sys.exit("--min-cm não pode ser maior que --max-cm")
    for valor in (args.sensibilidade_disparo, args.sensibilidade_manutencao):
        if not (SENSIBILIDADE_LIMITE[0] <= valor <= SENSIBILIDADE_LIMITE[1]):
            sys.exit(f"As sensibilidades precisam estar entre {SENSIBILIDADE_LIMITE[0]} e {SENSIBILIDADE_LIMITE[1]}")
    if not (ATRASO_DISPARO_LIMITE_MS[0] <= args.atraso_disparo_ms <= ATRASO_DISPARO_LIMITE_MS[1]):
        sys.exit(f"--atraso-disparo-ms precisa estar entre {ATRASO_DISPARO_LIMITE_MS[0]} e {ATRASO_DISPARO_LIMITE_MS[1]}")
    if not (RETENCAO_LIMITE_SEG[0] <= args.retencao_seg <= RETENCAO_LIMITE_SEG[1]):
        sys.exit(f"--retencao-seg precisa estar entre {RETENCAO_LIMITE_SEG[0]} e {RETENCAO_LIMITE_SEG[1]}")


def main() -> None:
    args = analisar_argumentos()
    validar(args)

    print(f"Conectando ao sensor via UART ({args.baud} bps)...")
    radar = DFRobot_C4001_UART(args.baud)

    tentativas = 0
    while not radar.begin():
        tentativas += 1
        if tentativas > 5:
            sys.exit("Sensor não respondeu - confira a fiação RX/TX e se a UART está habilitada (README).")
        print("Sensor não respondeu, tentando de novo...")
        time.sleep(1)

    radar.set_sensor_mode(EXIST_MODE)

    print(f"Alcance: {args.min_cm / 100:.2f}m a {args.max_cm / 100:.2f}m")
    radar.set_detection_range(args.min_cm, args.max_cm, args.max_cm)

    print(f"Sensibilidade: disparo {args.sensibilidade_disparo}, manutenção {args.sensibilidade_manutencao}")
    radar.set_trig_sensitivity(args.sensibilidade_disparo)
    radar.set_keep_sensitivity(args.sensibilidade_manutencao)

    print(f"Atraso de disparo: {args.atraso_disparo_ms}ms, retenção: {args.retencao_seg}s")
    radar.set_delay(args.atraso_disparo_ms // 10, args.retencao_seg * 2)

    time.sleep(0.3)
    print()
    print("Configuração salva no sensor:")
    print(f"  alcance mínimo          = {radar.get_min_range()} cm")
    print(f"  alcance máximo          = {radar.get_max_range()} cm")
    print(f"  alcance de disparo      = {radar.get_trig_range()} cm")
    print(f"  sensibilidade disparo   = {radar.get_trig_sensitivity()}")
    print(f"  sensibilidade manutenção = {radar.get_keep_sensitivity()}")
    print(f"  atraso de disparo       = {int(radar.get_trig_delay()) * 10} ms")
    print(f"  retenção                = {int(radar.get_keep_timerout()) // 2} s")


if __name__ == "__main__":
    main()
