"""PoC LOCAL: HMAC + SQLite, sem Pix, SPI, dinheiro real ou assinatura pública.

Python 3.10+, somente biblioteca padrão. Um único domínio transacional:
estado da obrigação, autorização e ledger simulados na mesma base SQLite.
Uso: python poc_protocol.py --output resultados_poc.json
"""
import argparse
import copy
import hashlib
import hmac
import json
import secrets
import sqlite3
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path


class Refused(Exception):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


class Lab:
    def __init__(self, path):
        self.path = str(path)
        self.key = secrets.token_bytes(32)  # Segredo compartilhado SÓ de laboratório.
        with self.tx() as db:
            db.executescript("""
            CREATE TABLE issuer(id TEXT PRIMARY KEY, active INTEGER NOT NULL);
            CREATE TABLE obligation(id TEXT PRIMARY KEY, institution TEXT NOT NULL,
              destination TEXT NOT NULL, debtor TEXT NOT NULL, amount INTEGER NOT NULL,
              revision INTEGER NOT NULL, state TEXT NOT NULL);
            CREATE TABLE authorization(id TEXT PRIMARY KEY, obligation TEXT NOT NULL,
              payload_hash TEXT NOT NULL, state TEXT NOT NULL);
            CREATE TABLE confirmation(id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL,
              payer TEXT NOT NULL);
            CREATE TABLE ledger(id TEXT PRIMARY KEY, obligation TEXT UNIQUE NOT NULL,
              authorization TEXT UNIQUE NOT NULL, amount INTEGER NOT NULL,
              payer TEXT NOT NULL, destination TEXT NOT NULL);
            CREATE TABLE refund(id TEXT PRIMARY KEY, payment TEXT NOT NULL,
              amount INTEGER NOT NULL, destination TEXT NOT NULL);
            INSERT INTO issuer VALUES ('instituicao-ficticia', 1);
            INSERT INTO obligation VALUES ('divida-1','instituicao-ficticia',
              'conta-institucional','pessoa-1',10000,1,'open');
            """)

    @contextmanager
    def tx(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def issue(self, auth_id, expires=1200):
        with self.tx() as db:
            o = db.execute("SELECT * FROM obligation WHERE id='divida-1'").fetchone()
            if o["state"] != "open":
                raise Refused("obrigacao_nao_aberta")
            p = {"authorization": auth_id, "obligation": o["id"],
                 "institution": o["institution"], "destination": o["destination"],
                 "debtor": o["debtor"], "amount": o["amount"],
                 "revision": o["revision"], "currency": "BRL",
                 "audience": "simulador-local", "issued": 900, "expires": expires}
            db.execute("INSERT INTO authorization VALUES (?,?,?,'issued')",
                       (auth_id, o["id"], digest(p)))
        return {"payload": p, "mac": hmac.new(self.key, canonical(p), hashlib.sha256).hexdigest()}

    def check(self, db, token, now):
        p = token["payload"]
        expected = hmac.new(self.key, canonical(p), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, token["mac"]):
            raise Refused("autenticacao_invalida")
        a = db.execute("SELECT * FROM authorization WHERE id=?",
                       (p["authorization"],)).fetchone()
        if a is None or a["payload_hash"] != digest(p):
            raise Refused("autorizacao_desconhecida_ou_divergente")
        if a["state"] in ("revoked", "cancelled"):
            raise Refused("autorizacao_revogada_ou_cancelada")
        if not (p["issued"] <= now < p["expires"]) or p["audience"] != "simulador-local":
            raise Refused("tempo_ou_audiencia_invalidos")
        issuer = db.execute("SELECT active FROM issuer WHERE id=?", (p["institution"],)).fetchone()
        if issuer is None or not issuer["active"]:
            raise Refused("emissor_revogado")
        o = db.execute("SELECT * FROM obligation WHERE id=?", (p["obligation"],)).fetchone()
        fields = ("institution", "destination", "debtor", "amount", "revision")
        if o is None or any(p[k] != o[k] for k in fields):
            raise Refused("obrigacao_divergente")
        return p, o

    def confirm(self, token, payer="pessoa-1", selected_obligation="divida-1",
                selected_debtor="pessoa-1", now=1000):
        # No mundo real, seleção e confirmação vêm da interface bancária confiável.
        with self.tx() as db:
            p, _ = self.check(db, token, now)
            if p["obligation"] != selected_obligation or p["debtor"] != selected_debtor:
                raise Refused("cobranca_diferente_da_intencao_confirmada")
            if payer != p["debtor"]:  # Política explícita do laboratório: sem terceiros.
                raise Refused("terceiros_fora_do_escopo")
            cid = secrets.token_hex(12)
            db.execute("INSERT INTO confirmation VALUES (?,?,?)", (cid, digest(p), payer))
            return cid

    def pay(self, token, confirmation, payer="pessoa-1", now=1000, lose_response=False):
        with self.tx() as db:
            p, o = self.check(db, token, now)
            c = db.execute("SELECT * FROM confirmation WHERE id=?", (confirmation,)).fetchone()
            if c is None or c["payload_hash"] != digest(p) or c["payer"] != payer:
                raise Refused("confirmacao_ausente_ou_divergente")
            previous = db.execute("SELECT id FROM ledger WHERE authorization=?",
                                  (p["authorization"],)).fetchone()
            if previous:
                return previous["id"]  # Repetição idempotente, não outro débito.
            if o["state"] != "open":
                raise Refused("obrigacao_ja_quitada_ou_fechada")
            order = "ordem-" + p["authorization"]
            db.execute("INSERT INTO ledger VALUES (?,?,?,?,?,?)",
                       (order, p["obligation"], p["authorization"], p["amount"], payer, p["destination"]))
            db.execute("UPDATE obligation SET state='paid' WHERE id=?", (p["obligation"],))
            db.execute("UPDATE authorization SET state='consumed' WHERE id=?", (p["authorization"],))
        if lose_response:
            raise TimeoutError("resposta_perdida_DEPOIS_do_commit")
        return order

    def reconcile(self, authorization):
        with self.tx() as db:
            row = db.execute("SELECT * FROM ledger WHERE authorization=?", (authorization,)).fetchone()
            return dict(row) if row else None

    def cancel(self, authorization):
        with self.tx() as db:
            a = db.execute("SELECT state FROM authorization WHERE id=?", (authorization,)).fetchone()
            if a is None or a["state"] != "issued":
                raise Refused("nao_cancelavel")
            db.execute("UPDATE authorization SET state='cancelled' WHERE id=?", (authorization,))

    def refund(self, order, refund_id, amount):
        with self.tx() as db:
            old = db.execute("SELECT * FROM refund WHERE id=?", (refund_id,)).fetchone()
            if old:
                if old["payment"] != order or old["amount"] != amount:
                    raise Refused("chave_idempotente_com_dados_diferentes")
                return dict(old)
            payment = db.execute("SELECT * FROM ledger WHERE id=?", (order,)).fetchone()
            if payment is None or type(amount) is not int or amount <= 0:
                raise Refused("devolucao_invalida")
            total = db.execute("SELECT COALESCE(SUM(amount),0) FROM refund WHERE payment=?", (order,)).fetchone()[0]
            if total + amount > payment["amount"]:
                raise Refused("devolucao_excede_pagamento")
            db.execute("INSERT INTO refund VALUES (?,?,?,?)", (refund_id, order, amount, payment["payer"]))
            return {"id": refund_id, "payment": order, "amount": amount, "destination": payment["payer"]}

    def count(self):
        with self.tx() as db:
            return db.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def refused(call):
    try:
        call()
    except Refused as exc:
        return str(exc)
    raise AssertionError("Operação deveria ter sido recusada")


def suite():
    cases = []

    def run(name, test):
        with tempfile.TemporaryDirectory(prefix="cobranca-poc-") as tmp:
            lab = Lab(Path(tmp) / "lab.db")
            try:
                details = test(lab)
                cases.append({"case": name, "passed": True, "observed": details})
            except Exception as exc:
                cases.append({"case": name, "passed": False, "error": repr(exc)})

    def normal(lab):
        t = lab.issue("a1"); c = lab.confirm(t)
        order = lab.pay(t, c)
        require(lab.count() == 1, "Deve haver um pagamento")
        return {"ledger_rows": lab.count(), "order": order}
    run("pagamento_valido", normal)

    for field, value in (("destination", "conta-atacante"), ("amount", 99999),
                         ("obligation", "divida-outra"), ("revision", 2)):
        def tamper(lab, field=field, value=value):
            t = lab.issue("a1"); c = lab.confirm(t)
            changed = copy.deepcopy(t); changed["payload"][field] = value
            reason = refused(lambda: lab.pay(changed, c))
            require(lab.count() == 0, "Alteração não pode liquidar")
            return {"reason": reason, "ledger_rows": 0}
        run("alteracao_" + field, tamper)

    def expired(lab):
        t = lab.issue("a1", expires=1050); c = lab.confirm(t)
        reason = refused(lambda: lab.pay(t, c, now=1050))
        require(lab.count() == 0, "Expiração deve impedir execução")
        return {"reason": reason, "ledger_rows": 0}
    run("expiracao", expired)

    def revoked(lab):
        t = lab.issue("a1"); c = lab.confirm(t)
        with lab.tx() as db:
            db.execute("UPDATE issuer SET active=0")
        reason = refused(lambda: lab.pay(t, c))
        require(lab.count() == 0, "Revogação deve impedir execução")
        return {"reason": reason, "ledger_rows": 0}
    run("revogacao_emissor", revoked)

    def no_confirmation(lab):
        t = lab.issue("a1")
        reason = refused(lambda: lab.pay(t, "referencia-publica-nao-autoriza"))
        require(lab.count() == 0, "Referência não autoriza débito")
        return {"reason": reason, "ledger_rows": 0}
    run("ausencia_confirmacao", no_confirmation)

    def wrong_debt(lab):
        t = lab.issue("a1")
        reason = refused(lambda: lab.confirm(t, selected_debtor="pessoa-2"))
        require(lab.count() == 0, "Dívida de terceiro não pode passar")
        return {"reason": reason, "ledger_rows": 0}
    run("cobranca_autentica_devedor_incorreto", wrong_debt)

    def stale(lab):
        t = lab.issue("a1"); c = lab.confirm(t)
        with lab.tx() as db:
            db.execute("UPDATE obligation SET revision=2")
        reason = refused(lambda: lab.pay(t, c))
        require(lab.count() == 0, "Revisão obsoleta não deve pagar")
        return {"reason": reason, "ledger_rows": 0}
    run("revisao_obsoleta", stale)

    def concurrency(lab, same):
        t1 = lab.issue("a1"); t2 = t1 if same else lab.issue("a2")
        pairs = [(t1, lab.confirm(t1)), (t2, lab.confirm(t2))]
        barrier = threading.Barrier(2)
        def worker(pair):
            barrier.wait(timeout=10)
            try:
                return {"accepted": True, "order": lab.pay(*pair)}
            except Refused as exc:
                return {"accepted": False, "reason": str(exc)}
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(worker, pairs))
        require(lab.count() == 1, "Concorrência deve produzir somente um lançamento")
        require(sum(x["accepted"] for x in outcomes) == (2 if same else 1), "Resultado inesperado")
        return {"concurrent_requests": 2, "ledger_rows": 1,
                "idempotent_replies" if same else "blocked_requests": 1}
    run("mesma_autorizacao_concorrente", lambda l: concurrency(l, True))
    run("duas_autorizacoes_mesma_obrigacao", lambda l: concurrency(l, False))

    def timeout(lab):
        t = lab.issue("a1"); c = lab.confirm(t)
        try:
            lab.pay(t, c, lose_response=True)
        except TimeoutError:
            pass
        else:
            raise AssertionError("Falha de resposta não foi injetada")
        recovered = lab.reconcile("a1")
        require(recovered is not None, "Deve recuperar pagamento confirmado")
        require(lab.pay(t, c) == recovered["id"] and lab.count() == 1, "Retry duplicou")
        return {"recovered": True, "ledger_rows_after_retry": 1}
    run("timeout_apos_commit_e_reconciliacao", timeout)

    def cancel(lab):
        t = lab.issue("a1"); c = lab.confirm(t); lab.cancel("a1")
        reason = refused(lambda: lab.pay(t, c))
        require(lab.count() == 0, "Cancelada não executa")
        return {"reason": reason, "ledger_rows": 0}
    run("cancelamento_antes_execucao", cancel)

    def refunds(lab):
        t = lab.issue("a1"); c = lab.confirm(t); order = lab.pay(t, c)
        refused(lambda: lab.cancel("a1"))
        lab.refund(order, "r1", 4000); lab.refund(order, "r1", 4000)
        refused(lambda: lab.refund(order, "r1", 4001))
        lab.refund(order, "r2", 6000)
        refused(lambda: lab.refund(order, "r3", 1))
        with lab.tx() as db:
            total, rows = db.execute("SELECT SUM(amount),COUNT(*) FROM refund").fetchone()
        require(total == 10000 and rows == 2 and lab.count() == 1, "Devolução incorreta")
        refused(lambda: lab.issue("a2"))
        return {"refunded_cents": total, "refund_rows": rows, "original_not_reopened": True}
    run("devolucao_idempotente_limitada", refunds)

    def indistinguishable(_):
        legitimate = {"destination": "conta-x", "amount": 10000}
        fraudulent = {"destination": "conta-x", "amount": 10000}
        def bank(observed):
            return "origem_institucional_nao_verificada"
        require(legitimate == fraudulent and bank(legitimate) == bank(fraudulent), "Entradas iguais")
        return {"same_observation": True, "both": bank(legitimate),
                "limitation_demonstrated": "Sem informação adicional não se identifica a finalidade real"}
    run("contraexemplo_pix_comum_indistinguivel", indistinguishable)
    return {"scope": "Laboratório local HMAC + SQLite; nenhuma integração financeira",
            "cases": cases, "passed": sum(c["passed"] for c in cases), "total": len(cases),
            "limitations": ["HMAC simétrico, não assinatura pública institucional",
                            "Ledger e autorização na mesma transação SQLite",
                            "Não prova coordenação entre PSPs, escala ou redução real de fraude",
                            "Testes escolhidos não são amostra estatística de produção"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = suite()
    output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    print(output)
    raise SystemExit(0 if result["passed"] == result["total"] else 1)
