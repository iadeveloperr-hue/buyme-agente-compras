"""Roda os pedidos e conversas de teste contra o backend no ar e mostra quantos o agente acertou.

Cada caso diz o que se espera, sem fixar qual produto: atendido ou não, a quantidade total,
o teto de preço unitário e, quando não atende, se sugeriu produtos reais. Os casos de conversa
mandam as mensagens em sequência, com o histórico como o chat manda, e conferem a última.
Os casos marcados como "QA" vieram da bateria scripts/qa_riscos.py, e os de conversa, da sessão
de uso como comprador (scripts/jornadas.py): eram falhas e ficam aqui para não voltarem.
Uso: python scripts/avaliar.py
Atenção: os casos atendidos gravam requisições de verdade no banco."""
import json
import os
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")  # acentos legíveis também no console do Windows
URL = os.getenv("API_URL", "http://localhost:8000") + "/requisicoes"  # outra porta: defina API_URL

CASOS = [
    # O exemplo do enunciado. Sem necessidade dita, o critério é o mais barato que atende.
    {"pedido": "Preciso de 3 laptops para o time de vendas, até US$ 2.000 cada.",
     "atendido": True, "quantidade": 3, "teto_unitario": 2000, "mais_barato": True},
    {"pedido": "Preciso de 5 smartphones de até US$ 250 cada para a equipe de campo.",
     "atendido": True, "quantidade": 5, "teto_unitario": 250},
    # Era "4 relógios": os dois relógios até US$ 150 exigem compra mínima de 7 e de 5 unidades.
    {"pedido": "Quero 8 relógios de até US$ 150 cada para brinde de clientes.",
     "atendido": True, "quantidade": 8, "teto_unitario": 150},
    {"pedido": "Preciso de 2 tratores agrícolas.", "atendido": False},
    {"pedido": "Quero 10 laptops de no máximo US$ 500 cada.", "atendido": False},
    # Não existe, mas há parecidos: não cria requisição E sugere modelos reais do catálogo.
    {"pedido": "Quero um iPhone 19.", "atendido": False, "sugere_de": "iphone"},
    # QA: orçamento em reais virava compra em dólar, cerca de 4x acima do limite.
    {"pedido": "Preciso de 3 laptops de até R$ 2.000 cada.", "atendido": False},
    # QA: o Realme C35 tem compra mínima de 3 unidades; 2 seria recusado pelo fornecedor.
    {"pedido": "Quero 2 Realme C35.", "atendido": False},
    # Conversa: a resposta curta só faz sentido com a mensagem anterior.
    {"conversa": ["Preciso de notebooks para a equipe nova.", "5 unidades, até US$ 1.500 cada."],
     "atendido": True, "quantidade": 5, "teto_unitario": 1500},
    # Conversa: o mínimo do AirPods é 4; a pessoa aceita e o agente monta com 4.
    {"conversa": ["Quero 3 Apple AirPods.", "Sim, pode aumentar para 4 unidades."],
     "atendido": True, "quantidade": 4},
    # Com quantidade e teto, escolhe (o mais barato) em vez de perguntar "qual você prefere?".
    {"pedido": "Preciso de 2 celulares até US$ 1.000 cada.",
     "atendido": True, "quantidade": 2, "teto_unitario": 1000, "mais_barato": True},
    # Conversa: em reais não compra; quando a pessoa passa o valor em dólar, monta.
    {"conversa": ["Preciso de 2 celulares até R$ 2.000 cada.", "Então até US$ 350 cada."],
     "atendido": True, "quantidade": 2, "teto_unitario": 350},
    # Sem critério: pergunta mostrando celulares reais do catálogo, sem escolher às cegas.
    {"pedido": "Quero 2 celulares.", "atendido": False, "sugere_categoria": "smartphones"},
    # Antes ele inventava categorias ("monitores, impressoras") que o catálogo não tem.
    {"pedido": "Quais tipos de produto vocês têm?", "atendido": False,
     "nao_cita": ["impressora", "monitor", "printer"]},
]


def _dummyjson(caminho: str) -> list[dict]:
    # A DummyJSON recusa (403) o User-Agent padrão do urllib.
    req = urllib.request.Request(f"https://dummyjson.com/{caminho}",
                                 headers={"User-Agent": "buyme-avaliacao/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)["products"]


def titulos_do_catalogo(termo: str) -> list[str]:
    """Os nomes reais que a resposta pode citar, lidos direto da DummyJSON."""
    return [p["title"] for p in _dummyjson(f"products/search?q={termo}&select=title&limit=50")]


def titulos_da_categoria(categoria: str) -> list[str]:
    return [p["title"] for p in _dummyjson(f"products/category/{categoria}?select=title&limit=0")]


def enviar(pedido: str, historico: list[dict] | None = None) -> tuple[int, dict]:
    corpo = {"pedido": pedido, "historico": historico or []}
    req = urllib.request.Request(URL, data=json.dumps(corpo).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def resumo_do_agente(status: int, corpo: dict) -> str:
    """A resposta do agente como o chat a manda no histórico (ver resumoDoAgente no App.jsx)."""
    if status >= 400:
        d = corpo.get("detail") or {}
        return f"Erro {d.get('codigo')}: {d.get('mensagem')}"
    t = corpo.get("resposta") or ""
    req = corpo.get("requisicao")
    if req:
        itens = "; ".join(f"{i['quantidade']}x {i['titulo']} a US$ {i['preco_unitario_usd']}"
                          for i in req["itens"])
        t += f"\n[Requisição nº {req['id']} gravada: {itens}; total US$ {req['total_usd']}]"
    else:
        t += "\n[Nenhuma requisição foi criada]"
    alts = corpo.get("alternativas") or []
    if alts:
        t += "\n[Também cabiam: " + "; ".join(f"{a['titulo']} US$ {a['preco_unitario_usd']}"
                                              for a in alts) + "]"
    return t[:3900]


def conversar(mensagens: list[str]) -> tuple[int, dict]:
    """Manda as mensagens em sequência, com o histórico, e devolve a última resposta."""
    historico: list[dict] = []
    for m in mensagens:
        status, corpo = enviar(m, historico)
        historico += [{"autor": "pessoa", "texto": m},
                      {"autor": "agente", "texto": resumo_do_agente(status, corpo)}]
    return status, corpo


def conferir(caso: dict, status: int, corpo: dict) -> str | None:
    """Devolve None se acertou, ou o motivo do erro."""
    if status >= 400:
        return f"erro {status}: {corpo.get('detail', {}).get('mensagem')}"
    if corpo["atendido"] != caso["atendido"]:
        return f"esperava atendido={caso['atendido']}, veio {corpo['atendido']}"
    texto = (corpo.get("resposta") or "").lower()
    if not caso["atendido"]:
        if "sugere_de" in caso or "sugere_categoria" in caso:
            reais = (titulos_do_catalogo(caso["sugere_de"]) if "sugere_de" in caso
                     else titulos_da_categoria(caso["sugere_categoria"]))
            if not any(t.lower() in texto for t in reais):
                return f"não sugeriu nenhum produto real; esperava algum de {reais}"
        citou = [p for p in caso.get("nao_cita", []) if p in texto]
        if citou:
            return f"citou o que o catálogo não tem: {citou}"
        return None
    itens = corpo["requisicao"]["itens"]
    qtd = sum(i["quantidade"] for i in itens)
    if qtd != caso["quantidade"]:
        return f"quantidade {qtd}, esperava {caso['quantidade']}"
    teto = caso.get("teto_unitario")
    caros = [i["titulo"] for i in itens if teto is not None and i["preco_unitario_usd"] > teto]
    if caros:
        return f"acima do teto de US$ {teto}: {caros}"
    if caso.get("mais_barato"):
        escolhido = min(i["preco_unitario_usd"] for i in itens)
        mais_baratas = [a["titulo"] for a in corpo.get("alternativas") or []
                        if a["preco_unitario_usd"] < escolhido]
        if mais_baratas:
            return f"sem necessidade dita, devia escolher o mais barato; havia {mais_baratas}"
    return None


def main() -> int:
    acertos = 0
    for n, caso in enumerate(CASOS, 1):
        mensagens = caso.get("conversa") or [caso["pedido"]]
        status, corpo = conversar(mensagens)
        erro = conferir(caso, status, corpo)
        acertos += erro is None
        chamadas = ", ".join(f["nome"] for f in corpo.get("ferramentas") or
                             corpo.get("detail", {}).get("ferramentas", []))
        print(f"{'OK ' if erro is None else 'ERRO'} {n}. {' → '.join(mensagens)}")
        print(f"     ferramentas: {chamadas or '-'}")
        if erro:
            print(f"     motivo: {erro}")
    print(f"\nAcertos: {acertos}/{len(CASOS)}")
    return 0 if acertos == len(CASOS) else 1


if __name__ == "__main__":
    sys.exit(main())
