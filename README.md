# BuyME: agente de compras

*Buy* + *ME*: o agente de compras pensado para o dia a dia da Mercado Eletrônico.

A pessoa escreve o que precisa num chat ("Preciso de 3 laptops para o time de vendas, até US$ 2.000 cada.")
e o agente busca no catálogo da [DummyJSON](https://dummyjson.com/docs/products) por um servidor MCP,
escolhe os itens e salva uma requisição de compra no Postgres. Se nada atende, ele diz o que faltou
e nenhuma requisição é criada.

## Como rodar

Precisa de Docker com o Compose.

```bash
cp .env.example .env        # e cole a chave da OpenRouter em OPENROUTER_API_KEY
docker compose up --build
```

Abra **http://localhost:3000** e envie o pedido de exemplo, que já vem preenchido.
Se a chave estiver vazia ou expirada, o chat avisa isso em vez de travar.

| Peça | Porta | O que é |
|---|---|---|
| `frontend` | 3000 | chat em React (nginx serve o build e repassa `/api` ao backend) |
| `backend` | 8000 | FastAPI: `POST /requisicoes`, `GET /requisicoes`; o agente mora aqui |
| `mcp-server` | 8001 | servidor MCP com `buscar_produtos(termo)` e `detalhar_produto(id)` |
| `db` | — | Postgres 16; as tabelas nascem do `db/init.sql` na primeira subida |

Testes (não precisam de chave de LLM nem de rede):

```bash
cd mcp-server && pip install -r requirements-dev.txt && pytest -q   # 7 testes das ferramentas
cd backend    && pip install -r requirements-dev.txt && pytest -q   # 18 testes: regras e erros
python scripts/avaliar.py      # com o ambiente no ar: 5 pedidos de teste, mostra os acertos
```

## Principais decisões

1. **O modelo decide, o código confere.** O loop de tool calling é escrito à mão
   ([backend/agente.py](backend/agente.py)), sem framework: o backend pega as ferramentas que o
   servidor MCP anuncia, entrega ao modelo e executa o que ele pedir, até ele responder com o JSON final.
   Não há ordem fixa de chamadas: nos testes, ele chamou só a busca em alguns pedidos, a busca
   e o `detalhar_produto` em outros, e quatro buscas com sinônimos para "tratores".
2. **Preço, estoque e total nunca vêm do LLM.** O modelo devolve só `produto_id` e `quantidade`.
   O backend guarda todo produto que as ferramentas devolveram na conversa e monta os itens a partir
   deles ([backend/validacao.py](backend/validacao.py)). Um id que o catálogo não devolveu é recusado
   como invenção, e uma quantidade acima do estoque também é recusada. A conta é feita em `Decimal`.
   A mesma ideia vale para `detalhar_produto`: num teste com "mouses" (que a DummyJSON não tem), o
   modelo chutava ids (1 a 5, depois 101 a 104) para "explorar" o catálogo, mesmo com o prompt
   proibindo. Então a regra foi para o código: id que não veio de uma busca é recusado antes de chegar
   ao catálogo, e o modelo recebe o motivo e segue.
3. **Nenhum erro silencioso.** Cada falha tem um código, uma mensagem para a pessoa e uma linha no
   log, como mostra a seção [Tratamento de erros](#tratamento-de-erros) logo abaixo.
4. **Modelo `anthropic/claude-haiku-4.5`.** A chave do case tem um guardrail de *Zero Data Retention*
   na OpenRouter, e ele recusa o `gpt-4o-mini` e o Gemini. Testei quais modelos passam
   ([scripts/testar_modelos.py](scripts/testar_modelos.py)): Claude Haiku/Sonnet 4.5, GPT-4.1-mini e
   GPT-5-mini. Fiquei com o Haiku por ser rápido e barato e bom em chamar ferramentas. Para trocar,
   basta mudar `OPENROUTER_MODEL` no `.env`.
5. **Sem a terceira ferramenta (`criar_requisicao`) de propósito.** Gravar pelo MCP deixaria o modelo
   decidir quando escrever no banco, e a escrita é justamente o ponto em que quero a validação do
   item 2 antes. O MCP fica só com leitura do catálogo, e a gravação acontece no backend, numa
   transação (requisição e itens juntos ou nada).
6. **Testes das ferramentas pelo protocolo MCP de verdade**, com um cliente em memória e a DummyJSON
   simulada (`respx`): cobrem resultado, lista vazia, produto inexistente, timeout e erro 503.

## Tratamento de erros

Regra do projeto: **toda falha devolve algo**. Isso quer dizer um código fixo, uma mensagem que
diz o que aconteceu e se algo foi gravado, e uma linha no log (`docker compose logs backend
mcp-server`). Todo erro do backend tem o mesmo formato, que o chat sabe ler:

```json
{"detail": {"codigo": "CATALOGO_INDISPONIVEL", "mensagem": "...", "ferramentas": [...]}}
```

| O que falhou | Código | HTTP | O que a pessoa vê no chat |
|---|---|---|---|
| DummyJSON lenta (> 5 s), fora do ar, erro 5xx ou resposta que não é JSON | `CATALOGO_INDISPONIVEL` | 503 | "O catálogo está fora do ar ou lento. Nenhuma requisição foi criada." |
| Servidor MCP fora do ar ou caiu no meio | `FERRAMENTAS_INDISPONIVEIS` | 503 | "O serviço de catálogo (MCP) não respondeu." |
| Chave ausente, inválida/expirada (401), sem crédito (402), modelo bloqueado (403/404), limite (429) | `LLM_INDISPONIVEL` | 502 | o motivo exato, por exemplo "a chave da OpenRouter é inválida ou expirou" |
| Modelo não devolveu o JSON final, mesmo depois de uma segunda chance, ou não concluiu em 8 passos | `RESPOSTA_INVALIDA` | 502 | "O agente não conseguiu concluir o pedido. Tente reformular." |
| Agente passou de 100 s no total | `TEMPO_ESGOTADO` | 504 | "O agente não concluiu em 100 s." |
| Modelo escolheu id que não veio do catálogo, quantidade acima do estoque ou item mal formado | `ESCOLHA_INVALIDA` | 422 | o motivo, por exemplo "pedido 3, estoque 2" |
| Postgres fora do ar ao gravar ou listar | `BANCO_INDISPONIVEL` | 503 | "Nada foi salvo." (a gravação é uma transação: requisição e itens juntos, ou nada) |
| Pedido vazio ou com mais de 1000 caracteres | `PEDIDO_INVALIDO` | 422 | "Escreva o pedido com 3 a 1000 caracteres." |
| Qualquer erro não previsto | `ERRO_INTERNO` | 500 | "Erro inesperado no servidor." (o stack completo vai para o log) |
| Backend caiu (o nginx responde no lugar dele) ou o navegador perdeu a conexão | `HTTP_502`, `SEM_CONEXAO` | — | "O backend está fora do ar ou não respondeu." |

Também é registrado, sem interromper a conversa, o que o modelo pode corrigir sozinho. Isso vale
para produto não encontrado, `detalhar_produto` com id que não veio de uma busca e argumentos de
ferramenta mal formados. Em todos esses casos, o modelo recebe o motivo como resultado da
ferramenta, tenta de novo, e a chamada aparece marcada em vermelho na lista "Ferramentas chamadas".

"Nada encontrado" **não** é erro: a busca devolve lista vazia, o agente diz que o catálogo não tem
o item e responde 201 com `atendido: false`. Se o modelo não escrever o motivo, o backend escreve um
texto padrão, para a resposta nunca ficar em branco.

Cada linha da tabela tem um teste ([backend/tests/test_erros.py](backend/tests/test_erros.py) e
[mcp-server/tests/test_tools.py](mcp-server/tests/test_tools.py)). Para ver a falha do catálogo
acontecendo no ambiente:

```bash
DUMMYJSON_URL=http://10.255.255.1 docker compose up -d mcp-server   # IP que não responde
# envie um pedido no chat; depois volte ao normal:
docker compose up -d mcp-server
```

Também entraram três dos itens opcionais: o chat mostra as ferramentas chamadas, há um script de
avaliação com 5 pedidos (5/5 na última execução) e um CI com lint e testes
([.github/workflows/ci.yml](.github/workflows/ci.yml)).

**Limites conhecidos:** a DummyJSON tem cerca de 190 produtos e a busca é por palavra em inglês. O
prompt orienta a traduzir ("notebook" → "laptop"), mas um item que existe com outro nome pode não
ser achado. Os preços estão em USD e não há conversão de moeda. Não há autenticação.

## Como usei IA

O projeto foi feito com o **Claude Code** (agente de programação da Anthropic), do jeito que trabalho
no dia a dia:

- **Eu defini** o escopo a partir do enunciado, a ordem das peças (MCP → backend → chat → extras,
  um commit por peça) e os critérios de pronto de cada uma. Também revisei cada decisão da lista acima.
- **O Claude Code escreveu** a maior parte do código, rodou os testes e o ambiente, e investigou os
  erros que apareceram.
- **O que os testes pegaram no caminho** (e que está no histórico de commits):
  - a chave com guardrail ZDR recusando o primeiro modelo;
  - o cliente MCP embrulhando erros num `ExceptionGroup`, o que mascarava a causa real;
  - o resultado em lista do MCP chegando como um bloco por produto. Isso fez a trava anti-invenção
    recusar um produto legítimo, e corrigi a leitura, não a trava.
- O script `avaliar.py` é a forma de checar o agente sem confiar só na impressão: cinco pedidos com
  resultado esperado, incluindo dois que **não** devem ser atendidos.
