"""Gráfico opcional das hipóteses: requer matplotlib; cálculos não dependem dele.
Uso: python gerar_grafico.py
"""
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from avaliar_viabilidade import evaluate

root = Path(__file__).resolve().parent
base = json.loads((root / "cenarios.json").read_text(encoding="utf-8"))["base"]
coverage = list(range(101))
fig, ax = plt.subplots(figsize=(10, 5.8), layout="constrained")
fig.set_facecolor("#f8fafc")
ax.set_facecolor("#f8fafc")
series = [
    ("Conjunto: migração de 20%", "#0369a1", {"m": 0.2}, "vpl_conjunto"),
    ("Conjunto: migração de 80%", "#c2410c", {"m": 0.8}, "vpl_conjunto"),
    ("Orçamento público: sem economia fiscal", "#475569", {}, "vpl_orcamento_publico"),
]
for label, color, changes, key in series:
    y = [evaluate({**base, **changes, "a": x / 100})[key] / 1e6 for x in coverage]
    ax.plot(coverage, y, label=label, color=color, linewidth=2.5)
ax.axhline(0, color="#111827", linewidth=1)
cutoff = evaluate({**base, "m": 0.2})["equilibrio"]["limite"] * 100
ax.axvline(cutoff, color="#0369a1", linestyle="--", alpha=0.6)
ax.annotate(f"Equilíbrio: {cutoff:.2f}%".replace(".", ","),
            xy=(cutoff, 0), xytext=(cutoff + 8, 0.7), fontsize=11,
            arrowprops={"arrowstyle": "->", "color": "#0369a1"})
ax.set(xlim=(0, 100), xlabel="Tentativas cobertas pelo protocolo (%)",
       ylabel="Valor presente líquido em 3 anos (R$ milhões)")
ax.set_title("A viabilidade muda com cobertura e migração da fraude",
             loc="left", fontsize=15, fontweight="bold", pad=18)
ax.grid(axis="y", alpha=0.18)
ax.spines[["top", "right"]].set_visible(False)
ax.legend(loc="upper left", frameon=False, fontsize=10)
fig.supxlabel("HIPÓTESES FICTÍCIAS • Não são estimativas do Brasil nem evidência de economia real.\n"
              "Parâmetros em cenarios.json; perdas privadas evitadas não são receita pública.",
              fontsize=10, color="#475569")
fig.savefig(root / "sensibilidade.png", dpi=180)
plt.close(fig)
print("sensibilidade.png gerado a partir das equações de avaliar_viabilidade.py")
