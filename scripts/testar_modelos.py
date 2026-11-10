"""Descobre quais modelos a chave da OpenRouter aceita COM tool calling.
A chave do case tem guardrail de Zero Data Retention: só passam endpoints ZDR.
Uso (dentro do container do backend): python /scripts/testar_modelos.py modelo1 modelo2 ..."""
import os
import sys

from openai import OpenAI

cli = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=os.environ["OPENROUTER_API_KEY"], timeout=60)
TOOL = [{"type": "function", "function": {"name": "buscar_produtos", "description": "busca",
         "parameters": {"type": "object", "properties": {"termo": {"type": "string"}}, "required": ["termo"]}}}]

for modelo in sys.argv[1:]:
    try:
        r = cli.chat.completions.create(model=modelo, tools=TOOL, max_tokens=200,
                                        messages=[{"role": "user", "content": "Busque laptops no catálogo."}])
        chamou = bool(r.choices[0].message.tool_calls)
        print(f"OK   {modelo}  (chamou ferramenta: {chamou})")
    except Exception as e:
        print(f"FALHA {modelo}  {type(e).__name__}: {str(e)[:90]}")
