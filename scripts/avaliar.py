"""Roda 5 pedidos de teste contra o backend no ar e mostra quantos o agente acertou.

Cada caso diz o que se espera, sem fixar qual produto: atendido ou não, a quantidade
total e o teto de preço unitário. Uso: python scripts/avaliar.py
Atenção: os casos atendidos gravam requisições de verdade no banco."""
import json
import sys
import urllib.error
import urllib.request

URL = "http://localhost:8000/requisicoes"

CASOS = [
    {"pedido": "Preciso de 3 laptops para o time de vendas, até US$ 2.000 cada.",
     "atendido": True, "quantidade": 3, "teto_unitario": 2000},
    {"pedido": "Preciso de 5 smartphones de até US$ 250 cada para a equipe de campo.",
     "atendido": True, "quantidade": 5, "teto_unitario": 250},
    {"pedido": "Quero 4 relógios de até US$ 150 cada para brinde de clientes.",
     "atendido": True, "quantidade": 4, "teto_unitario": 150},
    {"pedido": "Preciso de 2 tratores agrícolas.", "atendido": False},
    {"pedido": "Quero 10 laptops de no máximo US$ 500 cada.", "atendido": False},
]


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
