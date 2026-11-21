"""Create or complete app/static/app/api/openapi.json from the /api/v1/ routes.

Run after adding a route:  venv/Scripts/python.exe scripts/gerar_openapi.py
What is already in the file (descriptions edited by hand, schemas) is kept; only missing routes
and methods are added. The test test_api_contract checks that every route is documented.
"""
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "expensemanager.settings")

import django  # noqa: E402

django.setup()

from app import urls_api_v1  # noqa: E402

OUT = ROOT / "app" / "static" / "app" / "api" / "openapi.json"

SUMMARIES = {
    "auth_token": "Gera uma chave de acesso com usuário e senha",
    "auth_register": "Cria uma conta e já devolve uma chave de acesso",
    "auth_me": "Usuário, plano e dados da chave usada",
    "auth_revoke": "Revoga a chave usada na requisição",
    "auth_tokens": "Lista as chaves ativas do usuário",
    "auth_token_detail": "Revoga uma chave do usuário",
    "sync_push": "Sincronização: envia as alterações do aparelho (até 200 por vez)",
    "sync_pull": "Sincronização: o que mudou desde o cursor",
    "sync_snapshot": "Sincronização: download completo, em páginas",
    "sync_object": "Sincronização: um registro pelo uid",
    "me_wipe": "Apaga todos os dados da conta (pede a senha)",
    "me_delete": "Exclui a conta online e todos os dados (pede a senha)",
    "openapi": "Este documento",
    "docs": "Documentação legível desta API",
    "summary": "Resumo do mês: receitas, despesas, saldo e gráficos",
    "analytics": "Análises de um período",
    "plan": "Planejamento do mês: o que aconteceu e o que ainda vem",
    "quick": "Lançamento rápido: interpreta uma linha de texto e, com save, grava",
    "notifications": "Avisos de orçamento, vencimentos e confirmações pendentes",
    "search": "Busca em transações, categorias, contas e pessoas",
    "transactions": "Lista (com filtros) e cria transações",
    "transactions_bulk": "Ação em lote sobre várias transações",
    "transaction_split": "Divide uma transação em várias categorias",
    "transaction": "Lê, altera ou manda para a lixeira uma transação",
    "transaction_restore": "Tira uma transação da lixeira",
    "transaction_history": "Histórico de alterações de uma transação",
    "activity": "Últimas alterações em transações",
    "trash": "Transações na lixeira",
    "trash_empty": "Esvazia a lixeira",
    "trash_item": "Apaga de vez uma transação da lixeira",
    "reconcile": "Saldo conciliado de uma conta",
    "insights": "Observações sobre os gastos",
    "people": "Lista e cria pessoas (dívidas e contas divididas)",
    "person": "Lê, renomeia ou apaga uma pessoa",
    "person_entry": "Registra um lançamento com uma pessoa",
    "entry": "Altera ou apaga um lançamento de dívida",
    "entry_confirm": "Confirma uma parcela prevista",
    "person_settle": "Acerta as contas com uma pessoa",
    "person_summary": "Resumo em texto para mandar à pessoa",
    "people_split": "Divide uma conta entre várias pessoas",
    "categories": "Lista e cria categorias",
    "category": "Altera ou apaga (ou mescla) uma categoria",
    "accounts": "Lista e cria contas",
    "account": "Altera ou apaga uma conta",
    "transfers": "Lista e cria transferências entre contas",
    "transfer": "Apaga uma transferência",
    "tags": "Lista e cria tags",
    "tag": "Renomeia ou apaga uma tag",
    "rules": "Lista e cria regras de categorização",
    "rule_suggest": "Sugere a categoria para um texto",
    "rule": "Altera ou apaga uma regra",
    "views": "Lista e cria filtros salvos",
    "view": "Altera ou apaga um filtro salvo",
    "budgets": "Orçamentos do mês e criação",
    "budget_copy": "Copia os orçamentos de um mês para outro",
    "budget_history": "Histórico dos orçamentos",
    "budget": "Altera ou apaga um orçamento",
    "goals": "Lista e cria metas",
    "goal": "Altera ou apaga uma meta",
    "goal_contribute": "Registra um aporte numa meta",
    "contribution": "Apaga um aporte",
    "recurring": "Lista e cria recorrências",
    "recurring_item": "Altera ou apaga uma recorrência",
    "recurring_toggle": "Pausa ou retoma uma recorrência",
    "recurring_match": "Confirma se um lançamento é a ocorrência da recorrência",
}

PUBLIC = {"auth_token", "auth_register", "openapi", "docs"}


def openapi_path(route):
    return "/api/v1/" + re.sub(r"<(?:\w+:)?(\w+)>", r"{\1}", route)


def main():
    doc = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    doc.setdefault("openapi", "3.1.0")
    doc.setdefault("info", {"title": "Finanças Pessoais", "version": "1",
                            "description": "Envie a chave no cabeçalho Authorization: Bearer fp_... "
                                           "Erros: {\"ok\": false, \"message\", \"errors\", \"code\"}."})
    doc.setdefault("components", {"securitySchemes": {"chave": {"type": "http", "scheme": "bearer"}}})
    doc.setdefault("security", [{"chave": []}])
    paths = doc.setdefault("paths", {})
    for pattern in urls_api_v1.urlpatterns:
        route = str(pattern.pattern)
        name = pattern.name[len("v1_"):]
        methods = getattr(pattern.callback, "api_methods", None) or (["POST"] if name in ("auth_token", "auth_register") else ["GET"])
        entry = paths.setdefault(openapi_path(route), {})
        params = re.findall(r"<(?:\w+:)?(\w+)>", route)
        for method in methods:
            if method == "HEAD":
                continue
            op = entry.setdefault(method.lower(), {})
            op.setdefault("operationId", f"{name}_{method.lower()}")
            op.setdefault("summary", SUMMARIES.get(name, name))
            if params:
                op.setdefault("parameters", [{"name": p, "in": "path", "required": True, "schema": {"type": "integer"}}
                                             for p in params])
            if name in PUBLIC:
                op["security"] = []
            op.setdefault("responses", {"200": {"description": "ok: true e os dados"},
                                        "4XX": {"description": "ok: false, message, errors e code"}})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{len(paths)} rotas em {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
