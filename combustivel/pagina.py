#!/usr/bin/env python3
"""
Gera combustivel.html a partir do template.html + historico.json E PUBLICA.

O HTML sai autonomo: os dados vao embutidos no proprio arquivo, sem fetch,
para ser servido como estatico no PythonAnywhere (mesmo esquema dos paineis
de precos, de passagens e de alugueis). Tambem escreve stamp.txt com o
instante da ultima coleta, que o proprio painel consulta para se recarregar
sozinho quando ha dado novo.

Publicar e o comportamento PADRAO: pagina gerada e pagina no ar.

Uso:
  python3 combustivel/pagina.py               # gera, valida e publica
  python3 combustivel/pagina.py --sem-deploy  # so gera, para inspecionar local
  python3 combustivel/pagina.py --force       # publica mesmo com o JS quebrado
"""
import json
import re
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).parent
TEMPLATE = BASE / "template.html"
HIST = BASE / "historico.json"
SAIDA = BASE / "combustivel.html"
STAMP = BASE / "stamp.txt"
MARCA = "/*__DADOS__*/{}"


def valida_js(path):
    """new Function(script) sem erro de sintaxe, igual aos outros paineis."""
    m = re.search(r"<script>([\s\S]*)</script>", path.read_text(encoding="utf-8"))
    if not m:
        return False, "nao achei o <script> inline"
    try:
        p = subprocess.run(
            ["node", "-e", "let s='';process.stdin.on('data',d=>s+=d).on('end',()=>{"
                           "try{new Function(s);console.log('OK')}"
                           "catch(e){console.log('ERRO: '+e.message);process.exit(2)}});"],
            input=m.group(1), text=True, capture_output=True, timeout=60)
    except FileNotFoundError:
        return True, "node nao encontrado; pulei a validacao"
    except subprocess.TimeoutExpired:
        return False, "validacao do JS estourou o tempo"
    return p.returncode == 0, (p.stdout or p.stderr).strip()


def historico():
    """Mesma arbitragem do coletor: uniao do historico local com o publicado.
    Rodar so o pagina.py para mexer no visual nao pode publicar um historico
    parado por cima do que o Actions juntou no meio tempo."""
    try:
        sys.path.insert(0, str(BASE))
        from coletor import carrega
    except Exception as e:
        print(f"  ..   Sem a arbitragem do coletor ({type(e).__name__}); "
              f"seguindo so com o historico local.")
        return json.loads(HIST.read_text(encoding="utf-8")) if HIST.exists() else {}
    h = carrega()
    if h.get("ultima"):
        HIST.write_text(json.dumps(h, ensure_ascii=False, separators=(",", ":")),
                        encoding="utf-8")
    return h


def enxuga(h):
    """Tira do JSON embutido o que a pagina nao le."""
    h = dict(h)
    for campo in ("semanas", "semanas_ruins", "sementes"):
        h.pop(campo, None)
    return h


def main():
    h = historico()
    if not h.get("coletas"):
        print("ERRO: historico sem nenhuma coleta. Rode o coletor primeiro.")
        return 1
    tpl = TEMPLATE.read_text(encoding="utf-8")
    if MARCA not in tpl:
        print(f"ERRO: nao achei a marca {MARCA} no template.")
        return 1
    dados = json.dumps(enxuga(h), ensure_ascii=False, separators=(",", ":"))
    dados = dados.replace("</", "<\\/")
    SAIDA.write_text(tpl.replace(MARCA, dados), encoding="utf-8")
    STAMP.write_text((h.get("ultima") or "").strip() + "\n", encoding="utf-8")

    kb = SAIDA.stat().st_size / 1024
    print(f"Gerado {SAIDA.name}: {kb:.0f} KB, {len(h['coletas'])} coletas, "
          f"{len(h.get('postos') or {})} postos, ultima coleta {h.get('ultima')}.")

    ok, msg = valida_js(SAIDA)
    print(f"Validacao do JS: {msg}")
    if not ok and "--force" not in sys.argv:
        print("ABORTADO: o JS esta quebrado, nao publiquei. Corrija (ou use --force).")
        return 1

    if "--sem-deploy" in sys.argv:
        print("Deploy pulado (--sem-deploy).")
        return 0

    p = subprocess.run([sys.executable, str(BASE / "deploy.py")],
                       capture_output=True, text=True)
    print(p.stdout.rstrip())
    if p.stderr.strip():
        print(p.stderr.rstrip(), file=sys.stderr)
    return p.returncode


if __name__ == "__main__":
    sys.exit(main())
