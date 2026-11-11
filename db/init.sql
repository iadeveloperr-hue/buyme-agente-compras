-- Roda sozinho na primeira subida do Postgres (docker-entrypoint-initdb.d).
CREATE TABLE IF NOT EXISTS requisicoes (
    id          SERIAL PRIMARY KEY,
    pedido      TEXT           NOT NULL,          -- o texto que a pessoa digitou
    resumo      TEXT,                             -- a resposta do agente
    total_usd   NUMERIC(12, 2) NOT NULL,
    criada_em   TIMESTAMPTZ    NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS requisicao_itens (
    id                 SERIAL PRIMARY KEY,
    requisicao_id      INTEGER        NOT NULL REFERENCES requisicoes(id) ON DELETE CASCADE,
    produto_id         INTEGER        NOT NULL,   -- id na DummyJSON
    titulo             TEXT           NOT NULL,
    quantidade         INTEGER        NOT NULL CHECK (quantidade > 0),
    preco_unitario_usd NUMERIC(12, 2) NOT NULL,
    subtotal_usd       NUMERIC(12, 2) NOT NULL,
    prazo_envio        TEXT                       -- como o catálogo informa ("Ships in 2 weeks")
);

CREATE INDEX IF NOT EXISTS idx_itens_requisicao ON requisicao_itens(requisicao_id);
