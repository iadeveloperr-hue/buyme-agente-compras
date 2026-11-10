"""Regras que NÃO ficam a cargo do LLM: preço, estoque e conta vêm do catálogo e do código.

O modelo só escolhe QUAIS produtos e QUANTOS. Um id que ele não tenha recebido
num resultado de ferramenta é tratado como invenção e recusado."""
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal


@dataclass
class Item:
    produto_id: int
    titulo: str
    quantidade: int
    preco_unitario_usd: Decimal

    @property
    def subtotal_usd(self) -> Decimal:
        return (self.preco_unitario_usd * self.quantidade).quantize(Decimal("0.01"), ROUND_HALF_UP)


class EscolhaInvalida(Exception):
    pass


def montar_itens(escolhidos: list[dict], vistos: dict[int, dict]) -> list[Item]:
    """escolhidos: [{"produto_id", "quantidade"}] vindo do modelo.
    vistos: produtos que as ferramentas realmente devolveram nesta conversa, por id."""
    itens = []
    for e in escolhidos:
        try:
            pid = int(e["produto_id"])
            qtd = int(e["quantidade"])
        except (KeyError, TypeError, ValueError):
            raise EscolhaInvalida(f"item mal formado na resposta do agente: {e!r}") from None
        p = vistos.get(pid)
        if p is None:
            raise EscolhaInvalida(f"produto {pid} não veio do catálogo nesta busca")
        if qtd <= 0:
            raise EscolhaInvalida(f"quantidade inválida para o produto {pid}")
        estoque = p.get("estoque")
        if estoque is not None and qtd > estoque:
            raise EscolhaInvalida(f"{p['titulo']}: pedido {qtd}, estoque {estoque}")
        itens.append(Item(pid, p["titulo"], qtd, Decimal(str(p["preco_usd"]))))
    return itens


def total(itens: list[Item]) -> Decimal:
    return sum((i.subtotal_usd for i in itens), Decimal("0.00"))
