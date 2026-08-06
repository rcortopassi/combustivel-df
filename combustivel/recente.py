#!/usr/bin/env python3
"""Diz se o painel publicado ja foi atualizado ha pouco.

O cron do GitHub some com frequencia (medido no painel de passagens em
05/08/2026: de oito horarios previstos, um rodou). A resposta e dar varias
chances por hora; para as chances extras nao virarem varias rodadas, cada
uma confere primeiro o historico publicado. Se for recente, a rodada termina
sem coletar nada.

Escreve "fresco=1" ou "fresco=0" no GITHUB_OUTPUT. Qualquer problema na
consulta responde 0: e melhor coletar de novo do que ficar sem atualizar.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.request import urlopen

JANELA_MIN = 45


def responde(fresco, motivo):
    print(f"{'fresco' if fresco else 'velho'}: {motivo}")
    saida = os.environ.get("GITHUB_OUTPUT")
    if saida:
        with open(saida, "a", encoding="utf-8") as f:
            f.write(f"fresco={1 if fresco else 0}\n")
    return 0


def main():
    # O usuario do PythonAnywhere chega por secret: o repositorio e publico.
    user = os.environ.get("PA_USER", "").strip()
    if not user:
        return responde(False, "sem PA_USER, nao da para conferir o publicado")
    url = f"https://{user}.pythonanywhere.com/combustivel/historico.json"
    try:
        with urlopen(url, timeout=60) as r:
            ultima = (json.load(r) or {}).get("ultima")
    except Exception as e:
        return responde(False, f"nao consegui ler o historico publicado ({type(e).__name__})")
    if not ultima:
        return responde(False, "historico publicado sem campo ultima")
    try:
        quando = datetime.fromisoformat(ultima)
    except ValueError:
        return responde(False, f"campo ultima ilegivel ({ultima})")
    if quando.tzinfo is None:
        quando = quando.replace(tzinfo=timezone.utc)
    idade = datetime.now(timezone.utc) - quando
    minutos = int(idade.total_seconds() // 60)
    if idade < timedelta(minutes=JANELA_MIN):
        return responde(True, f"ultima coleta ha {minutos} min, menos que {JANELA_MIN}")
    return responde(False, f"ultima coleta ha {minutos} min")


if __name__ == "__main__":
    sys.exit(main())
