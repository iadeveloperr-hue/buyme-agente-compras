# Agente de compras

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
cd mcp-server && pip install -r requirements-dev.txt && pytest -q   # 6 testes das ferramentas
cd backend    && pip install -r requirements-dev.txt && pytest -q   # 4 testes das regras
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
3. **Cada falha tem nome e mensagem própria.** O servidor MCP transforma timeout (5 s) ou erro da
   DummyJSON em `CATALOGO_INDISPONIVEL`, que é diferente de "nada encontrado" (lista vazia). O backend
   responde 503 para catálogo ou MCP fora do ar, 502 para modelo fora ou resposta fora do formato e
   422 para escolha que não confere. O chat mostra a mensagem e diz que nada foi gravado.
   Para ver a falha acontecendo: `DUMMYJSON_URL=http://10.255.255.1 docker compose up -d mcp-server`.
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
