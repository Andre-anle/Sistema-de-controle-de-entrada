def resumo_filtros(itens: list[tuple[str, str]]) -> str:
    ativos = [f"{nome}: {valor.strip()}" for nome, valor in itens if (valor or "").strip()]
    return " | ".join(ativos) if ativos else "Nenhum (todos os registros)"


def resumo_mercadorias(filtros: dict[str, str]) -> str:
    return resumo_filtros(
        [
            ("Data inicial", filtros.get("data_inicio", "")),
            ("Data final", filtros.get("data_fim", "")),
            ("Mercadoria", filtros.get("ean", "")),
            ("Nome", filtros.get("descricao", "")),
            ("Colaborador", filtros.get("colaborador", "")),
        ]
    )


def resumo_visitantes(filtros: dict[str, str]) -> str:
    return resumo_filtros(
        [
            ("Data inicial", filtros.get("data_inicio", "")),
            ("Data final", filtros.get("data_fim", "")),
            ("Empresa", filtros.get("empresa", "")),
            ("Visitante", filtros.get("nome", "")),
        ]
    )
