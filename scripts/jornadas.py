"""Sessão de uso repetível: as conversas que um comprador teria no chat, com o histórico enviado
como o chat envia. Não dá nota. Imprime cada troca para alguém ler e julgar se a resposta ajuda
a pessoa a terminar o que começou: foi assim, usando o chat como comprador, que apareceram
falhas que a avaliação e o QA não pegavam (ver README > Como usei IA).
Uso: python scripts/jornadas.py   (cerca de 20 chamadas ao LLM; grava requisições no banco)"""
import sys

from avaliar import enviar, resumo_do_agente

sys.stdout.reconfigure(encoding="utf-8")

JORNADAS = [
    ("Exemplo do enunciado", ["Preciso de 3 laptops para o time de vendas, até US$ 2.000 cada."]),
    ("Pedido vago e resposta curta", ["Preciso de notebooks para a equipe nova.",
                                      "5 unidades, até US$ 1.500 cada."]),
    ("Abaixo do mínimo e aceite", ["Quero 3 Apple AirPods.", "Sim, pode aumentar para 4 unidades."]),
    ("Sem critério", ["Quero 2 celulares.", "Até US$ 300 cada."]),
    ("Troca depois de gravar", ["Preciso de 2 celulares até US$ 1.000 cada.",
                                "Troca pelo mais potente que couber."]),
    ("Orçamento em reais", ["Preciso de 2 celulares até R$ 2.000 cada.", "Então até US$ 350 cada."]),
    ("Kit com item que não existe", ["Kit home office para 2 pessoas: notebook até US$ 1.500, "
                                     "mouse e fone de ouvido."]),
    ("O que vocês têm?", ["Quais tipos de produto vocês têm?"]),
    ("Histórico", ["Quero ver as requisições que já fiz."]),
]


def main() -> None:
    for nome, mensagens in JORNADAS:
        print(f"\n=== {nome}")
        historico: list[dict] = []
        for m in mensagens:
            status, corpo = enviar(m, historico)
            texto = resumo_do_agente(status, corpo)
            print(f"  pessoa: {m}\n  agente: " + texto.replace("\n", "\n          "))
            historico += [{"autor": "pessoa", "texto": m}, {"autor": "agente", "texto": texto}]


if __name__ == "__main__":
    main()
