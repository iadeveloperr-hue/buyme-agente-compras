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
