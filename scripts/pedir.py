"""Envia um pedido ao backend e imprime a resposta. Uso: python scripts/pedir.py "texto do pedido" """
import json
import os
import sys
import urllib.error
import urllib.request

URL = os.getenv("API_URL", "http://localhost:8000") + "/requisicoes"  # outra porta: defina API_URL

pedido = " ".join(sys.argv[1:]) or "Preciso de 3 laptops para o time de vendas, até US$ 2.000 cada."
req = urllib.request.Request(URL, data=json.dumps({"pedido": pedido}).encode(),
                             headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=120) as r:
        print(r.status, json.dumps(json.load(r), ensure_ascii=False, indent=2))
except urllib.error.HTTPError as e:
    print(e.code, json.dumps(json.load(e), ensure_ascii=False, indent=2))
