"""Todo erro do backend sai neste formato, com um código fixo e uma mensagem para a pessoa:

    {"detail": {"codigo": "...", "mensagem": "...", "ferramentas": [...]}}

e é registrado no log com o mesmo código. Nenhum caminho devolve erro sem nome."""
import logging

from fastapi import HTTPException

log = logging.getLogger("agente")


def erro(status: int, codigo: str, mensagem: str, ferramentas=None, causa: BaseException | None = None):
    if status >= 500:
        log.error("%s: %s", codigo, mensagem, exc_info=causa)
    else:
        log.warning("%s: %s", codigo, mensagem)
    return HTTPException(status, {"codigo": codigo, "mensagem": mensagem, "ferramentas": ferramentas or []})
