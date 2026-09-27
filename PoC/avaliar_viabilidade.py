"""Calculadora determinística de hipóteses, não estimador de dados reais.

Python 3.10+, biblioteca padrão. Custos em BRL constantes; períodos anuais.
python avaliar_viabilidade.py --output resultados_modelo.json
"""
import argparse
import json
import math
from pathlib import Path


def validate(p):
    for k in ("p", "a", "s", "f"):
        if not math.isfinite(p[k]) or not 0 <= p[k] <= 1:
            raise ValueError(k + " deve estar em [0,1]")
    for k in ("N", "L", "m", "h", "c", "F", "I", "S", "r",
              "I_publico", "F_publico", "c_publico", "S_publico"):
        if not math.isfinite(p[k]) or p[k] < 0:
            raise ValueError(k + " deve ser finito e não negativo")
    if p["N"] <= 0 or type(p["T"]) is not int or p["T"] < 1:
        raise ValueError("N > 0 e T inteiro positivo são necessários")
    for sub, total in (("I_publico", "I"), ("F_publico", "F"),
                       ("c_publico", "c"), ("S_publico", "S")):
        if p[sub] > p[total]:
            raise ValueError(sub + " deve ser parcela do total, sem dupla contagem")


def evaluate(p):
    validate(p)
    n, a = p["N"], p["a"]
    af = sum((1 + p["r"]) ** (-year) for year in range(1, p["T"] + 1))
    gross_avoided = n * p["p"] * a * p["s"] * p["L"]
    migrated = gross_avoided * p["m"]
    fp_count = n * (1 - p["p"]) * a * p["f"]
    fp_cost = fp_count * p["h"]
    variable_cost = n * a * p["c"]
    annual = gross_avoided - migrated + p["S"] - p["F"] - variable_cost - fp_cost
    npv = -p["I"] + annual * af
    public_annual = p["S_publico"] - p["F_publico"] - n * a * p["c_publico"]
    # Cenário público: nenhuma perda privada é tratada como receita do Tesouro.
    public_npv = -p["I_publico"] + af * public_annual
    margin = n * (p["p"] * p["s"] * p["L"] * (1 - p["m"])
                  - p["c"] - (1 - p["p"]) * p["f"] * p["h"])
    required = p["I"] / af + p["F"] - p["S"]
    if margin > 0:
        cutoff = required / margin
        feasible = [max(0.0, cutoff), 1.0] if cutoff <= 1 else None
        relation = "a >= limite"
    elif margin < 0:
        cutoff = required / margin
        feasible = [0.0, min(1.0, cutoff)] if cutoff >= 0 else None
        relation = "a <= limite; aumentar cobertura piora resultado"
    else:
        cutoff = None
        feasible = [0.0, 1.0] if required <= 0 else None
        relation = "independe de a; exige I/AF + F - S <= 0"
    return {
        "perda_baseline_ano": n * p["p"] * p["L"],
        "perda_inicialmente_evitada": gross_avoided,
        "perda_deslocada_ou_migrada": migrated,
        "falsos_bloqueios_esperados": fp_count,
        "custo_falsos_bloqueios": fp_cost,
        "custo_variavel": variable_cost,
        "beneficio_liquido_anual_conjunto": annual,
        "fator_desconto": af,
        "vpl_conjunto": npv,
        "vpl_orcamento_publico": public_npv,
        "resultado_conjunto": "positivo" if npv > 0 else "negativo" if npv < 0 else "equilibrio",
        "equilibrio": {"A": required, "D": margin, "limite": cutoff,
                       "relacao": relation, "intervalo_viavel_0_a_1": feasible}
    }


def zero_failure_upper(n, alpha=0.05):
    if n < 1 or not 0 < alpha < 1:
        raise ValueError("n positivo, 0 < alpha < 1")
    return -math.expm1(math.log(alpha) / n)


def sample_zero_failures(qmax, alpha=0.05):
    if not 0 < qmax < 1 or not 0 < alpha < 1:
        raise ValueError("Probabilidades devem estar em (0,1)")
    return math.ceil(math.log(alpha) / math.log1p(-qmax))


def sanity_checks(base):
    """Casos-limite e oráculo contábil independente para um exemplo de brinquedo."""
    toy = {**base, "N": 100, "p": 0.1, "a": 1, "s": 1, "L": 10,
           "m": 0, "f": 0, "c": 0, "F": 0, "I": 0, "S": 0,
           "T": 1, "r": 0, "I_publico": 0, "F_publico": 0,
           "c_publico": 0, "S_publico": 0}
    if evaluate(toy)["vpl_conjunto"] != 100:
        raise AssertionError("10 perdas de R$10 evitadas devem resultar em R$100")
    if evaluate({**toy, "m": 1})["vpl_conjunto"] != 0:
        raise AssertionError("Migração integral anula o benefício nesse caso")
    if evaluate({**toy, "m": 1.5})["vpl_conjunto"] != -50:
        raise AssertionError("Dano deslocado maior pode produzir saldo negativo")
    if evaluate({**toy, "a": 0, "I": 50, "F": 10})["vpl_conjunto"] != -60:
        raise AssertionError("Sem cobertura não há benefício de prevenção")
    if evaluate({**toy, "p": 0, "c": 2})["vpl_conjunto"] != -200:
        raise AssertionError("Sem fraude, custo unitário continua existindo")
    n = sample_zero_failures(0.001)
    if not (zero_failure_upper(n) <= 0.001 < zero_failure_upper(n - 1)):
        raise AssertionError("Amostra mínima incorreta")
    return 6


def report(config):
    base = config["base"]
    checks = sanity_checks(base)
    scenarios = [{"nome": row["nome"], "parametros": {**base, **row["alteracoes"]},
                  "resultado": evaluate({**base, **row["alteracoes"]})}
                 for row in config["cenarios"]]
    sensitivity = [{"cobertura": i / 100, "vpl": evaluate({**base, "a": i / 100})["vpl_conjunto"]}
                   for i in range(0, 101, 5)]
    return {"aviso": config["aviso"], "natureza": "Cálculo de hipóteses, não pesquisa empírica",
            "checks_passed": checks, "cenarios": scenarios, "sensibilidade": sensitivity,
            "binomial": {"alpha": 0.05, "qmax": 0.001,
                         "n_minimo_zero_falhas": sample_zero_failures(0.001),
                         "limite_com_1000_sem_falhas": zero_failure_upper(1000),
                         "limite_com_2995_sem_falhas": zero_failure_upper(2995),
                         "condicao": "Observações independentes e representativas, não contagem de testes de software"}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path(__file__).with_name("cenarios.json"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = report(json.loads(args.input.read_text(encoding="utf-8")))
    text = json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text)
