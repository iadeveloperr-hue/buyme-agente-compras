"""Roda 6 pedidos de teste contra o backend no ar e mostra quantos o agente acertou.

Cada caso diz o que se espera, sem fixar qual produto: atendido ou não, a quantidade
total, o teto de preço unitário e, quando não atende, se sugeriu produtos reais.
Uso: python scripts/avaliar.py
Atenção: os casos atendidos gravam requisições de verdade no banco."""
import json
import os
import sys
import urllib.error
import urllib.request

URL = os.getenv("API_URL", "http://localhost:8000") + "/requisicoes"  # outra porta: defina API_URL

CASOS = [
    {"pedido": "Preciso de 3 laptops para o time de vendas, até US$ 2.000 cada.",
     "atendido": True, "quantidade": 3, "teto_unitario": 2000},
    {"pedido": "Preciso de 5 smartphones de até US$ 250 cada para a equipe de campo.",
     "atendido": True, "quantidade": 5, "teto_unitario": 250},
    {"pedido": "Quero 4 relógios de até US$ 150 cada para brinde de clientes.",
     "atendido": True, "quantidade": 4, "teto_unitario": 150},
    {"pedido": "Preciso de 2 tratores agrícolas.", "atendido": False},
    {"pedido": "Quero 10 laptops de no máximo US$ 500 cada.", "atendido": False},
    # Não existe, mas há parecidos: não cria requisição E sugere modelos reais do catálogo.
    {"pedido": "Quero um iPhone 19.", "atendido": False, "sugere_de": "iphone"},
]


def titulos_do_catalogo(termo: str) -> list[str]:
    """Os nomes reais que a resposta pode citar, lidos direto da DummyJSON."""
    url = f"https://dummyjson.com/products/search?q={termo}&select=title&limit=50"
    # A DummyJSON recusa (403) o User-Agent padrão do urllib.
    req = urllib.request.Request(url, headers={"User-Agent": "buyme-avaliacao/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return [p["title"] for p in json.load(r)["products"]]


def enviar(pedido: str) -> tuple[int, dict]:
    req = urllib.request.Request(URL, data=json.dumps({"pedido": pedido}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def conferir(caso: dict, status: int, corpo: dict) -> str | None:
    """Devolve None se acertou, ou o motivo do erro."""
    if status >= 400:
        return f"erro {status}: {corpo.get('detail', {}).get('mensagem')}"
    if corpo["atendido"] != caso["atendido"]:
        return f"esperava atendido={caso['atendido']}, veio {corpo['atendido']}"
    if not caso["atendido"]:
        if "sugere_de" in caso:
            texto = corpo["resposta"].lower()
            reais = titulos_do_catalogo(caso["sugere_de"])
            if not any(t.lower() in texto for t in reais):
                return f"não sugeriu nenhum produto real; esperava algum de {reais}"
        return None
    itens = corpo["requisicao"]["itens"]
    qtd = sum(i["quantidade"] for i in itens)
    if qtd != caso["quantidade"]:
        return f"quantidade {qtd}, esperava {caso['quantidade']}"
    caros = [i["titulo"] for i in itens if i["preco_unitario_usd"] > caso["teto_unitario"]]
    if caros:
        return f"acima do teto de US$ {caso['teto_unitario']}: {caros}"
    return None


def main() -> int:
    acertos = 0
    for n, caso in enumerate(CASOS, 1):
        status, corpo = enviar(caso["pedido"])
        erro = conferir(caso, status, corpo)
        acertos += erro is None
        chamadas = ", ".join(f["nome"] for f in corpo.get("ferramentas") or
                             corpo.get("detail", {}).get("ferramentas", []))
        print(f"{'OK ' if erro is None else 'ERRO'} {n}. {caso['pedido']}")
        print(f"     ferramentas: {chamadas or '-'}")
        if erro:
            print(f"     motivo: {erro}")
    print(f"\nAcertos: {acertos}/{len(CASOS)}")
    return 0 if acertos == len(CASOS) else 1


if __name__ == "__main__":
    sys.exit(main())
