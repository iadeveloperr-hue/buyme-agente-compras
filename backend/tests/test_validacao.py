from decimal import Decimal

import pytest
from agente import id_sem_busca, registrar_vistos
from validacao import EscolhaInvalida, montar_itens, total

VISTOS = {78: {"id": 78, "titulo": "MacBook Pro 14", "preco_usd": 1999.99, "estoque": 24},
          81: {"id": 81, "titulo": "Lenovo Yoga 920", "preco_usd": 1099.99, "estoque": 2}}


def test_preco_e_total_vem_do_catalogo_e_do_codigo():
    itens = montar_itens([{"produto_id": 81, "quantidade": 2}], VISTOS)
    assert itens[0].preco_unitario_usd == Decimal("1099.99")
    assert total(itens) == Decimal("2199.98")


def test_id_que_o_catalogo_nao_devolveu_e_invencao():
    with pytest.raises(EscolhaInvalida, match="não veio do catálogo"):
        montar_itens([{"produto_id": 999, "quantidade": 1}], VISTOS)


def test_quantidade_acima_do_estoque():
    with pytest.raises(EscolhaInvalida, match="estoque 2"):
        montar_itens([{"produto_id": 81, "quantidade": 3}], VISTOS)


def test_registrar_vistos_le_lista_e_produto_unico():
    vistos: dict = {}
    registrar_vistos('{"result": [{"id": 1, "preco_usd": 10}]}', vistos)
    registrar_vistos('{"id": 2, "preco_usd": 20, "descricao": "x"}', vistos)
    registrar_vistos('texto que não é JSON', vistos)
    assert set(vistos) == {1, 2}


def test_detalhar_id_que_nao_veio_de_busca_e_recusado():
    assert "RECUSADO" in id_sem_busca("detalhar_produto", {"id": 101}, VISTOS)
    assert id_sem_busca("detalhar_produto", {"id": 78}, VISTOS) is None
    assert id_sem_busca("buscar_produtos", {"termo": "mouse"}, VISTOS) is None


# --- Achados da bateria de QA: cada regra de dinheiro conferida no código, não no modelo.
from validacao import conferir_moeda_do_pedido, conferir_orcamento  # noqa: E402

REALME = {128: {"id": 128, "titulo": "Realme C35", "preco_usd": 149.99, "estoque": 48, "pedido_minimo": 3}}


def test_abaixo_do_pedido_minimo_do_fornecedor():
    with pytest.raises(EscolhaInvalida, match="mínimo do fornecedor 3"):
        montar_itens([{"produto_id": 128, "quantidade": 2}], REALME)
    assert montar_itens([{"produto_id": 128, "quantidade": 3}], REALME)[0].quantidade == 3


def test_preco_acima_do_limite_por_unidade():
    itens = montar_itens([{"produto_id": 78, "quantidade": 1}], VISTOS)  # US$ 1.999,99
    with pytest.raises(EscolhaInvalida, match="por unidade"):
        conferir_orcamento(itens, {"moeda": "USD", "max_unitario": 1200, "max_total": None})
    conferir_orcamento(itens, {"moeda": "USD", "max_unitario": 2000, "max_total": None})


def test_total_acima_do_orcamento_total():
    itens = montar_itens([{"produto_id": 81, "quantidade": 2}], VISTOS)  # US$ 2.199,98
    with pytest.raises(EscolhaInvalida, match="orçamento total"):
        conferir_orcamento(itens, {"moeda": "USD", "max_unitario": None, "max_total": 2000})


def test_orcamento_em_reais_nunca_vira_compra():
    itens = montar_itens([{"produto_id": 81, "quantidade": 1}], VISTOS)
    with pytest.raises(EscolhaInvalida, match="BRL"):
        conferir_orcamento(itens, {"moeda": "BRL", "max_unitario": 2000, "max_total": None})


def test_orcamento_ausente_ou_mal_formado():
    itens = montar_itens([{"produto_id": 81, "quantidade": 1}], VISTOS)
    conferir_orcamento(itens, None)  # pedido sem limite: nada a conferir
    with pytest.raises(EscolhaInvalida, match="mal formado"):
        conferir_orcamento(itens, {"moeda": "USD", "max_unitario": "dois mil"})


@pytest.mark.parametrize("pedido", ["Preciso de 3 laptops de até R$ 2.000 cada.",
                                    "3 notebooks, uns 5 mil reais no total",
                                    "Quero 2 monitores de até 300 euros"])
def test_valor_em_outra_moeda_no_texto_e_bloqueado(pedido):
    with pytest.raises(EscolhaInvalida, match="outra moeda"):
        conferir_moeda_do_pedido(pedido)


@pytest.mark.parametrize("pedido", ["Preciso de 3 laptops, até US$ 2.000 cada.",
                                    "Preciso de 3 laptops reais, não de demonstração",
                                    "3 laptops até R$ 10.000 (uns 2 mil dólares)",
                                    "Preciso de 3 laptops"])
def test_dolar_ou_sem_valor_passa(pedido):
    conferir_moeda_do_pedido(pedido)


def test_conferencia_dentro_do_loop_devolve_o_motivo_para_o_modelo_corrigir():
    from agente import Resultado, conferir_escolha
    abaixo = Resultado(itens=[{"produto_id": 128, "quantidade": 2}], resposta="", vistos=dict(REALME))
    assert "mínimo do fornecedor 3" in conferir_escolha("Quero 2 Realme C35", abaixo)
    ok = Resultado(itens=[{"produto_id": 128, "quantidade": 3}], resposta="", vistos=dict(REALME))
    assert conferir_escolha("Quero 3 Realme C35", ok) is None
    em_reais = Resultado(itens=[{"produto_id": 128, "quantidade": 3}], resposta="", vistos=dict(REALME))
    assert "outra moeda" in conferir_escolha("3 Realme C35 até R$ 900 cada", em_reais)
