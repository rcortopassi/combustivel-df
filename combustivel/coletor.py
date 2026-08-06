#!/usr/bin/env python3
"""
Coletor do painel /combustivel/: gasolina comum e aditivada nos postos da
regiao central sul de Brasilia, mais a parcela Petrobras de refinaria no DF.

Fontes, todas validadas em 06/08/2026:
- ANP, Levantamento de Precos de Combustiveis: arquivos semanais
  revendas_lpc_<ini>_<fim>.xlsx listados na pagina das ultimas semanas
  pesquisadas. Preco POR POSTO (CNPJ, bairro, bandeira). Amostra rotativa,
  ~50 postos do DF por semana. Publicado as segundas.
- ANP, serie historica semestral (ca-AAAA-SS.zip): mesma granularidade,
  usada como semente (--seed) para o grafico nascer com meses de historia.
- Petrobras (precos.petrobras.com.br): parcela da refinaria por litro de
  gasolina no DF, embutida no HTML estatico da pagina.

A ANP nao separa a Podium da aditivada generica: o produto e "GASOLINA
ADITIVADA". A aditivada entra como referencia e o painel avisa que a Podium
costuma custar mais. Menor Preco Brasil (NFC-e) foi testado e descartado:
so existe como aplicativo, sem endpoint web publico, e a versao atual exige
login gov.br. Nao reinserir sem tecnica nova.

Historico e append-only: fonte que falha nao apaga nada, so nao acrescenta.
Rodada em que TODAS as fontes falham nao carimba o campo "ultima".

Uso:
  python3 combustivel/coletor.py           # rodada normal (semanais novas + Petrobras)
  python3 combustivel/coletor.py --seed    # inclui a semente semestral da ANP
"""
import io
import json
import os
import re
import ssl
import sys
import time
import unicodedata
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

BASE = Path(__file__).parent
HIST = BASE / "historico.json"

ANP_SEMANAS = ("https://www.gov.br/anp/pt-br/assuntos/precos-e-defesa-da-concorrencia/"
               "precos/levantamento-de-precos-de-combustiveis-ultimas-semanas-pesquisadas")
ANP_SEMENTES = ["https://www.gov.br/anp/pt-br/centrais-de-conteudo/dados-abertos/"
                "arquivos/shpc/dsas/ca/ca-2026-01.zip"]
PETROBRAS = "https://precos.petrobras.com.br/web/precos-dos-combustiveis/w/gasolina/df"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# Do IP de datacenter (GitHub Actions) nao vale insistir muito: a variavel
# encurta as retentativas, mesma ideia do PRECOS_DATACENTER do painel de
# precos. Explicita de proposito, nunca autodetectada.
DATACENTER = os.environ.get("COMBUSTIVEL_DATACENTER") == "1"
TENTATIVAS = 1 if DATACENTER else 3

# Piso e teto por litro: piso barra erro de digitacao, teto barra absurdo.
# Licao do painel de precos: filtro que so valida por baixo deixa passar o
# absurdo por cima.
PRECO_MIN, PRECO_MAX = 4.50, 9.50
PETRO_MIN, PETRO_MAX = 1.00, 3.50

PRODUTOS = {"GASOLINA": "c", "GASOLINA COMUM": "c", "GASOLINA ADITIVADA": "a"}

# Regiao definida pelo usuario em 06/08/2026 num recorte de mapa: Plano
# Piloto sul e adjacencias. Jardim Botanico e Altiplano Leste estao no mapa
# mas nunca apareceram na amostra da ANP; ficam na lista para quando
# aparecerem. Asa Norte fica FORA (o recorte para no Eixo Monumental).
REGIAO_EXATA = {
    "ASA SUL", "SETOR SUDOESTE", "SUDOESTE", "OCTOGONAL",
    "CRUZEIRO", "CRUZEIRO VELHO", "CRUZEIRO NOVO",
    "GUARA", "GUARA I", "GUARA II", "ZONA INDUSTRIAL (GUARA)",
    "SIA", "SIG", "SETORES COMPLEMENTARES",
    "N BANDEIRANTE", "NUCLEO BANDEIRANTE", "CANDANGOLANDIA",
    "PARK WAY", "LAGO SUL", "SETOR DE HABITACOES INDIVIDUAIS SUL",
    "VILA PLANALTO", "JARDIM BOTANICO", "ALTIPLANO LESTE",
}
REGIAO_CONTEM = ("HABITACOES INDIVIDUAIS SUL", "JARDIM BOTANICO", "ALTIPLANO",
                 "ZONA INDUSTRIAL (GUARA)", "BANDEIRANTE", "CANDANGOLANDIA")
REGIAO_NUNCA = ("NORTE", "AGUAS CLARAS")  # barra Lago Norte, SHIN, Aguas Claras


def normaliza(txt):
    txt = unicodedata.normalize("NFD", str(txt or ""))
    txt = "".join(c for c in txt if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", txt).strip().upper()


def na_regiao(bairro):
    b = normaliza(bairro)
    if not b:
        return False
    if any(n in b for n in REGIAO_NUNCA):
        return False
    if b in REGIAO_EXATA:
        return True
    return any(c in b for c in REGIAO_CONTEM)


def baixa(url, binario=False):
    """GET com cara de navegador e retentativas. Devolve bytes ou texto."""
    erro = None
    for t in range(1, TENTATIVAS + 1):
        try:
            req = Request(url, headers={"User-Agent": UA, "Accept": "*/*",
                                        "Accept-Language": "pt-BR,pt;q=0.9"})
            ctx = ssl.create_default_context()
            with urlopen(req, timeout=60 if binario else 30, context=ctx) as r:
                dados = r.read()
            return dados if binario else dados.decode("utf-8", errors="replace")
        except Exception as e:
            erro = e
            if t < TENTATIVAS:
                time.sleep(10 * t)
    raise RuntimeError(f"{type(erro).__name__}: {erro}")


def limpa_cnpj(v):
    dig = re.sub(r"\D", "", str(v or ""))
    return dig.zfill(14) if dig else ""


def carrega():
    """Uniao do historico local com o publicado. Append-only dos dois lados,
    entao a uniao e sempre segura; "ultima" fica com o instante mais recente.
    Evita o acidente do painel de precos, em que rodar so a pagina apagava
    as coletas que o Actions tinha juntado no meio tempo."""
    locais = {}
    if HIST.exists():
        try:
            locais = json.loads(HIST.read_text(encoding="utf-8"))
        except Exception:
            locais = {}
    user = os.environ.get("PA_USER", "rafaelcortopassi")
    publicado = {}
    try:
        with urlopen(Request(f"https://{user}.pythonanywhere.com/combustivel/historico.json",
                             headers={"User-Agent": UA}), timeout=30) as r:
            publicado = json.load(r)
    except Exception:
        publicado = {}
    return _funde(locais, publicado)


def _funde(a, b):
    h = {"postos": {}, "coletas": [], "semanas": [], "semanas_ruins": [],
         "sementes": [], "petrobras": [], "ultima": None}
    for fonte in (a, b):
        if not isinstance(fonte, dict):
            continue
        h["postos"].update(fonte.get("postos") or {})
        h["semanas"] = sorted(set(h["semanas"]) | set(fonte.get("semanas") or []))
        h["semanas_ruins"] = sorted(set(h["semanas_ruins"])
                                    | set(fonte.get("semanas_ruins") or []))
        h["sementes"] = sorted(set(h["sementes"]) | set(fonte.get("sementes") or []))
        for c in (fonte.get("coletas") or []):
            h["coletas"].append(tuple(c))
        for p in (fonte.get("petrobras") or []):
            h["petrobras"].append(tuple(p))
        u = fonte.get("ultima")
        if u and (not h["ultima"] or u > h["ultima"]):
            h["ultima"] = u
    h["coletas"] = sorted(set(map(tuple, h["coletas"])), key=lambda c: (c[2], c[0], c[1]))
    # Petrobras: um valor por dia, o ultimo lido vale.
    por_dia = {}
    for d, v in sorted(h["petrobras"]):
        por_dia[d] = v
    h["petrobras"] = sorted(por_dia.items())
    h["coletas"] = [list(c) for c in h["coletas"]]
    h["petrobras"] = [list(p) for p in h["petrobras"]]
    return h


def registra(h, cnpj, nome, endereco, bairro, bandeira, produto, data_iso, preco):
    if not cnpj or produto not in PRODUTOS:
        return 0
    if not (PRECO_MIN <= preco <= PRECO_MAX):
        return 0
    if not na_regiao(bairro):
        return 0
    p = h["postos"].setdefault(cnpj, {})
    # O cadastro do posto e atualizado sempre que vem preenchido: o arquivo
    # semanal as vezes traz FANTASIA vazia que a semente semestral tem.
    if nome:
        p["nome"] = normaliza(nome)
    if endereco:
        p.setdefault("endereco", normaliza(endereco))
    if bairro:
        p["bairro"] = normaliza(bairro)
    if bandeira:
        p["bandeira"] = normaliza(bandeira)
    c = [cnpj, PRODUTOS[produto], data_iso, round(float(preco), 3)]
    if c not in h["coletas"]:
        h["coletas"].append(c)
        return 1
    return 0


def anp_semanais(h):
    """Descobre na pagina da ANP as semanas publicadas e processa as que o
    historico ainda nao tem. E a conferencia horaria: quase sempre nao ha
    arquivo novo e a rodada termina em um GET."""
    pagina = baixa(ANP_SEMANAS)
    links = re.findall(r'href="(https://[^"]*revendas_lpc_(\d{4}-\d{2}-\d{2})_'
                       r'(\d{4}-\d{2}-\d{2})\.xlsx)"', pagina)
    novas, coletas = 0, 0
    vistos = set()
    for url, ini, fim in links:
        semana = f"{ini}_{fim}"
        if semana in vistos or semana in h["semanas"] or semana in h["semanas_ruins"]:
            continue
        vistos.add(semana)
        try:
            corpo = baixa(url, binario=True)
        except Exception as e:
            # Falha de rede fica de fora da lista ruim: vale tentar de novo
            # na proxima rodada.
            print(f"  ANP semana {semana}: download FALHOU ({e})")
            continue
        try:
            coletas += _processa_xlsx(h, corpo)
            h["semanas"].append(semana)
            h["semanas"].sort()
            novas += 1
            print(f"  ANP semana {semana}: processada")
        except Exception as e:
            # XLSX que o openpyxl nao abre (formato antigo de 2022/2023).
            # Entra na lista ruim para a rodada horaria nao rebaixar para
            # sempre um arquivo que nunca vai abrir.
            h["semanas_ruins"].append(semana)
            h["semanas_ruins"].sort()
            print(f"  ANP semana {semana}: XLSX ilegivel, na lista ruim ({e})")
    return len(links), novas, coletas


def _processa_xlsx(h, corpo):
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(corpo), read_only=True)
    ws = wb.worksheets[0]
    n, comecou = 0, False
    for row in ws.iter_rows(values_only=True):
        if not comecou:
            comecou = str(row[0]).strip() == "CNPJ"
            continue
        if not row[0]:
            continue
        estado = normaliza(row[9])
        if estado != "DISTRITO FEDERAL":
            continue
        data = row[14]
        data_iso = data.strftime("%Y-%m-%d") if hasattr(data, "strftime") else str(data)[:10]
        try:
            preco = float(row[13])
        except (TypeError, ValueError):
            continue
        endereco = " ".join(str(x) for x in (row[3], row[4]) if x)
        n += registra(h, limpa_cnpj(row[0]), row[2] or row[1], endereco,
                      row[6], row[10], normaliza(row[11]), data_iso, preco)
    return n


def anp_semente(h):
    """Serie semestral por posto, para o grafico nascer com meses. Roda uma
    vez por arquivo; o nome do zip fica em h["sementes"]."""
    import csv
    total = 0
    for url in ANP_SEMENTES:
        rotulo = url.rsplit("/", 1)[-1].replace(".zip", "")
        if rotulo in h["sementes"]:
            continue
        corpo = baixa(url, binario=True)
        z = zipfile.ZipFile(io.BytesIO(corpo))
        nome = z.namelist()[0]
        with z.open(nome) as f:
            texto = io.TextIOWrapper(f, encoding="utf-8", errors="replace")
            leitor = csv.reader(texto, delimiter=";")
            next(leitor)
            n = 0
            for row in leitor:
                if len(row) < 16 or row[1].strip() != "DF":
                    continue
                produto = normaliza(row[10])
                if produto not in PRODUTOS:
                    continue
                try:
                    preco = float(row[12].replace(",", "."))
                    d, m, a = row[11].strip().split("/")
                    data_iso = f"{a}-{m}-{d}"
                except (ValueError, IndexError):
                    continue
                endereco = " ".join(x.strip() for x in (row[5], row[6]) if x.strip())
                n += registra(h, limpa_cnpj(row[4]), row[3], endereco,
                              row[8], row[15], produto, data_iso, preco)
        h["sementes"].append(rotulo)
        total += n
        print(f"  Semente {rotulo}: {n} coletas da regiao")
    return total


def petrobras(h):
    """Parcela da refinaria por litro de gasolina no DF. Valor raro de mudar;
    guarda um por dia."""
    pagina = baixa(PETROBRAS)
    m = (re.search(r'id="preco1"[^>]*>\s*([\d]+,[\d]+)\s*<', pagina)
         or re.search(r'telafinal-tarifa5-numero"[^>]*>\s*([\d]+,[\d]+)\s*<', pagina))
    if not m:
        raise RuntimeError("nao achei o valor na pagina")
    valor = float(m.group(1).replace(",", "."))
    if not (PETRO_MIN <= valor <= PETRO_MAX):
        raise RuntimeError(f"valor fora da faixa de sanidade: {valor}")
    hoje = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    por_dia = {d: v for d, v in h["petrobras"]}
    mudou = por_dia.get(hoje) != valor
    por_dia[hoje] = valor
    h["petrobras"] = [list(p) for p in sorted(por_dia.items())]
    return valor, mudou


def main():
    h = carrega()
    antes = len(h["coletas"])
    fontes_ok = 0

    if "--seed" in sys.argv:
        try:
            anp_semente(h)
            fontes_ok += 1
        except Exception as e:
            print(f"  Semente semestral FALHOU ({e})")

    try:
        listadas, novas, coletas = anp_semanais(h)
        fontes_ok += 1
        print(f"  ANP: {listadas} semanas listadas, {novas} novas, "
              f"{coletas} coletas novas da regiao")
    except Exception as e:
        print(f"  ANP semanal FALHOU ({e})")

    try:
        valor, mudou = petrobras(h)
        fontes_ok += 1
        print(f"  Petrobras DF: R$ {valor:.2f}/litro na refinaria"
              + (" (novo)" if mudou else ""))
    except Exception as e:
        print(f"  Petrobras FALHOU ({e})")

    if fontes_ok == 0:
        print("NADA COLETADO: todas as fontes falharam; historico intocado.")
        return 1

    h["ultima"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    HIST.write_text(json.dumps(h, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8")
    print(f"Historico: {len(h['coletas'])} coletas ({len(h['coletas']) - antes} novas), "
          f"{len(h['postos'])} postos, {len(h['semanas'])} semanas, "
          f"{len(h['petrobras'])} dias de Petrobras.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
