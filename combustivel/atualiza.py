#!/usr/bin/env python3
"""
Roda a rodada completa do painel de combustivel: coleta, gera e publica.

A ultima linha e sempre "ALERTA: ..." ou "SEM ALERTA: ...", para a tarefa
agendada diaria decidir se avisa o usuario. Ha alerta quando entrou semana
nova da ANP com leituras na regiao ou quando a parcela Petrobras mudou.
"""
import json
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).parent
HIST = BASE / "historico.json"


def estado():
    if not HIST.exists():
        return set(), {}
    try:
        h = json.loads(HIST.read_text(encoding="utf-8"))
    except Exception:
        return set(), {}
    return set(h.get("semanas") or []), dict(h.get("petrobras") or [])


def main():
    semanas_antes, petro_antes = estado()

    r = subprocess.run([sys.executable, str(BASE / "coletor.py")],
                       capture_output=True, text=True)
    print(r.stdout.rstrip())
    if r.stderr.strip():
        print(r.stderr.rstrip(), file=sys.stderr)
    if r.returncode != 0:
        print("SEM ALERTA: coleta falhou, historico intocado.")
        return r.returncode

    semanas_depois, petro_depois = estado()

    p = subprocess.run([sys.executable, str(BASE / "pagina.py")],
                       capture_output=True, text=True)
    print(p.stdout.rstrip())
    if p.stderr.strip():
        print(p.stderr.rstrip(), file=sys.stderr)
    if p.returncode != 0:
        print("SEM ALERTA: pagina nao publicou.")
        return p.returncode

    novas = sorted(semanas_depois - semanas_antes)
    # Compara o valor mais recente, nao a chave de data: uma leitura nova de hoje
    # com o mesmo valor de ontem nao e mudanca de parcela.
    antes = petro_antes[sorted(petro_antes)[-1]] if petro_antes else None
    depois_d = sorted(petro_depois)[-1] if petro_depois else None
    depois = petro_depois[depois_d] if depois_d else None
    if novas:
        print(f"ALERTA: semana nova da ANP no painel ({', '.join(novas)}).")
    elif antes is not None and depois is not None and depois != antes:
        print(f"ALERTA: parcela Petrobras mudou de R$ {antes:.2f} para "
              f"R$ {depois:.2f} em {depois_d}.")
    else:
        print("SEM ALERTA: nada novo alem da conferencia de rotina.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
