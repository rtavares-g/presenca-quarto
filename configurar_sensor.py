#!/usr/bin/env python3
"""
Ajusta alcance e sensibilidade do sensor mmWave C4001 via UART (pinos RX/TX).

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


def analisar_argumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--baud", type=int, default=9600, help="Baud rate da UART (padrão 9600)")
    parser.add_argument("--min-cm", type=int, default=30, help="Distância mínima de detecção, em cm (padrão 30)")
    parser.add_argument("--max-cm", type=int, default=300, help="Distância máxima de detecção, em cm (padrão 300 = 3m)")
    parser.add_argument("--sensibilidade", type=int, default=2, help="Sensibilidade 0-9: quanto menor, mais difícil disparar (padrão 2)")
    return parser.parse_args()


def validar(args: argparse.Namespace) -> None:
    if not (MIN_CM_LIMITE[0] <= args.min_cm <= MIN_CM_LIMITE[1]):
        sys.exit(f"--min-cm precisa estar entre {MIN_CM_LIMITE[0]} e {MIN_CM_LIMITE[1]}")
    if not (MAX_CM_LIMITE[0] <= args.max_cm <= MAX_CM_LIMITE[1]):
        sys.exit(f"--max-cm precisa estar entre {MAX_CM_LIMITE[0]} e {MAX_CM_LIMITE[1]}")
    if args.min_cm > args.max_cm:
        sys.exit("--min-cm não pode ser maior que --max-cm")
    if not (SENSIBILIDADE_LIMITE[0] <= args.sensibilidade <= SENSIBILIDADE_LIMITE[1]):
        sys.exit(f"--sensibilidade precisa estar entre {SENSIBILIDADE_LIMITE[0]} e {SENSIBILIDADE_LIMITE[1]}")


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

    print(f"Sensibilidade (disparo e manutenção): {args.sensibilidade}")
    radar.set_trig_sensitivity(args.sensibilidade)
    radar.set_keep_sensitivity(args.sensibilidade)

    time.sleep(0.3)
    print()
    print("Configuração salva no sensor:")
    print(f"  alcance mínimo          = {radar.get_min_range()} cm")
    print(f"  alcance máximo          = {radar.get_max_range()} cm")
    print(f"  alcance de disparo      = {radar.get_trig_range()} cm")
    print(f"  sensibilidade disparo   = {radar.get_trig_sensitivity()}")
    print(f"  sensibilidade manutenção = {radar.get_keep_sensitivity()}")


if __name__ == "__main__":
    main()
