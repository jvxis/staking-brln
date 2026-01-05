#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
staking-reward.py

Distribui uma % das fees de forwards (últimas N horas) para os Top-N usuários do LNbits,
proporcional ao saldo consolidado de cada usuário. Pagamentos 100% via API do LNbits.

Leitura/descoberta:
- OAuth2 (password flow) -> POST /api/v1/auth => Authorization: Bearer <token>
- Lista de usuários:      GET  /users/api/v1/user
- Wallets por usuário:    GET  /users/api/v1/user/{user_id}/wallet   (retorna inkey/adminkey/balance_msat)

Pagamento (API Keys, por carteira):
- Criar invoice (destinatário): POST /api/v1/payments, header X-Api-Key: <inkey>, body {"out": false, "amount": <int>, "memo": <str>}
- Pagar invoice (funding):      POST /api/v1/payments, header X-Api-Key: <fund_admin_key>, body {"out": true, "bolt11": <string>}

Recursos CLI:
- --username / --password   (OAuth2)
- --percent <float>         (obrigatório)
- --since-hours <int>       (padrão 24)
- --top <int>               (padrão 10)
- --lncli <path>            (padrão lncli)
- --lnbits-base <url>       (padrão https://wallet.br-ln.com)
- --fund-wallet-id          (wallet pagadora)
- --fund-admin-key          (adminkey da carteira pagadora; obrigatório quando não for --dry-run)
- --exclude-user-id         (opcional; exclui um user_id específico do ranking)
- --dry-run                 (não cria/paga invoices)
- --debug-api               (salva JSONs crus em ./debug/)

Logs:
- Arquivo: lnbits_payout.log
- Auditoria JSONL: audit_payouts.jsonl
- Console com tabelas bonitas :)
"""

import argparse
import subprocess
import json
import time
import datetime as dt
import requests
import logging
import sys
import os
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional
from zoneinfo import ZoneInfo  # novo


# =======================
# DEFAULTS
# =======================
LNBITS_BASE_DEFAULT = "https://wallet.br-ln.com"
FUND_WALLET_ID_DEFAULT = "0669f3ae71b64cfd840d8702b371ff30"
LNCLI_DEFAULT = "lncli"

# Salvar logs/auditoria no mesmo diretório do script (robusto para cron)
BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = str(BASE_DIR / "lnbits_payout.log")
AUDIT_FILE = str(BASE_DIR / "audit_payouts.jsonl")

# =======================
# Logging
# =======================
logger = logging.getLogger("lnbits_payout")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")

fh = logging.FileHandler(LOG_FILE)
fh.setFormatter(_fmt)
logger.addHandler(fh)

ch = logging.StreamHandler(sys.stdout)
ch.setFormatter(_fmt)
logger.addHandler(ch)

# =======================
# Utils
# =======================
def now_unix() -> int:
    return int(time.time())

def unix_hours_ago(h: int) -> int:
    return int(time.time()) - int(h) * 3600

def write_audit(entry: Dict[str, Any]):
    try:
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("Falha ao gravar auditoria: %s", e)

def _dump_debug(name: str, payload: Any, enable: bool):
    if not enable:
        return
    try:
        os.makedirs(BASE_DIR / "debug", exist_ok=True)
        path = BASE_DIR / "debug" / f"{name}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        logger.info("[DEBUG-API] Salvo: %s", path)
    except Exception as e:
        logger.warning("[DEBUG-API] Falha ao salvar %s: %s", name, e)

def mask_key(key: Optional[str]) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * max(0, len(key) - 2) + key[-2:]
    return key[:4] + "***" + key[-4:]

def fmt_sat(n: int) -> str:
    return f"{n:,}".replace(",", ".")

def print_table(headers: List[str], rows: List[List[Any]]):
    # calculo de larguras
    cols = len(headers)
    widths = [len(str(h)) for h in headers]
    for row in rows:
        for i in range(cols):
            widths[i] = max(widths[i], len(str(row[i])))
    sep = "+".join("-" * (w + 2) for w in widths)
    def fmt_row(vals):
        return " | ".join(str(vals[i]).ljust(widths[i]) for i in range(cols))
    logger.info(sep)
    logger.info(" " + fmt_row(headers))
    logger.info(sep)
    for r in rows:
        logger.info(" " + fmt_row(r))
    logger.info(sep)
    
# =======================
# Telegram + Total Mensal
# =======================
LOCAL_TZ = ZoneInfo("America/Sao_Paulo")

def send_telegram_message(token: str, chat_id: str, text: str) -> bool:
    """
    Envia mensagem simples via Telegram Bot API (sem depender de telebot).
    Retorna True/False conforme sucesso do POST.
    """
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        r = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=15)
        if r.status_code == 200:
            logger.info("[TELEGRAM] Mensagem enviada com sucesso.")
            return True
        logger.warning("[TELEGRAM] Falha ao enviar (HTTP %s): %s", r.status_code, r.text)
    except Exception as e:
        logger.exception("[TELEGRAM] Erro no envio: %s", e)
    return False

def _parse_iso_utc_z(s: str) -> Optional[dt.datetime]:
    """
    run_id tem formato 'YYYY-MM-DDTHH:MM:SSZ'. Converte para datetime timezone-aware (UTC).
    """
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None

def get_monthly_total_from_audit(audit_path: str, now_local: Optional[dt.datetime] = None) -> int:
    """
    Soma 'distributed_sats' das entradas de 'action': 'summary' no mês corrente (fuso America/Sao_Paulo).
    Observação: em dry-run, distributed_sats=0, então o total mensal não cresce (coerente).
    """
    if not os.path.isfile(audit_path):
        return 0
    if now_local is None:
        now_local = dt.datetime.now(tz=LOCAL_TZ)
    cur_year  = now_local.year
    cur_month = now_local.month

    total = 0
    try:
        with open(audit_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("action") != "summary":
                    continue

                run_id = obj.get("run_id")
                ts = _parse_iso_utc_z(run_id) if run_id else None
                if not ts:
                    # fallback: considera agora (raríssimo, mas não quebra)
                    continue

                # Converte UTC -> America/Sao_Paulo para comparar mês/ano local
                ts_local = ts.astimezone(LOCAL_TZ)
                if ts_local.year == cur_year and ts_local.month == cur_month:
                    total += int(obj.get("distributed_sats", 0) or 0)
    except Exception as e:
        logger.warning("Falha ao ler total mensal do audit: %s", e)
    return total

def _iter_summary_entries(audit_path: str):
    if not os.path.isfile(audit_path):
        return
    try:
        with open(audit_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("action") != "summary":
                    continue
                run_id = obj.get("run_id") or obj.get("ts")
                ts = _parse_iso_utc_z(run_id) if run_id else None
                if not ts:
                    continue
                yield ts.astimezone(LOCAL_TZ), obj
    except Exception as e:
        logger.warning("Falha ao ler audit: %s", e)

def get_period_totals_from_audit(
    audit_path: str,
    reference_local: Optional[dt.datetime] = None,
) -> Dict[str, int]:
    totals = {"prev_year": 0, "prev_month": 0, "cur_month": 0, "cur_year": 0}
    if reference_local is None:
        reference_local = dt.datetime.now(tz=LOCAL_TZ)
    cur_year = reference_local.year
    cur_month = reference_local.month
    prev_year = cur_year - 1
    prev_month_year = cur_year - 1 if cur_month == 1 else cur_year
    prev_month = 12 if cur_month == 1 else cur_month - 1

    for ts_local, obj in _iter_summary_entries(audit_path):
        distributed = int(obj.get("distributed_sats", 0) or 0)
        if ts_local.year == cur_year:
            totals["cur_year"] += distributed
            if ts_local.month == cur_month:
                totals["cur_month"] += distributed
        if ts_local.year == prev_year:
            totals["prev_year"] += distributed
        if ts_local.year == prev_month_year and ts_local.month == prev_month:
            totals["prev_month"] += distributed

    return totals

def _normalize_result_pos(value: Any, fallback: int) -> int:
    try:
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str) and value.isdigit():
            return int(value)
    except Exception:
        pass
    return fallback

def build_staking_results_message(
    summary: Dict[str, Any],
    audit_path: str,
    reference_local: Optional[dt.datetime] = None,
) -> str:
    results = summary.get("results")
    if not isinstance(results, list):
        results = []

    top_n = summary.get("top_n")
    try:
        top_n = int(top_n)
    except Exception:
        top_n = max(len(results), 10) if results else 10

    if reference_local is None:
        run_id = summary.get("run_id")
        run_ts = _parse_iso_utc_z(run_id) if run_id else None
        reference_local = run_ts.astimezone(LOCAL_TZ) if run_ts else dt.datetime.now(tz=LOCAL_TZ)

    data_str = reference_local.strftime("%d/%m")
    title = f"\U0001F3C6 BRLN Staking \U0001F947 TOP {top_n} Stakers - Resultado do dia {data_str}"

    dry_run = summary.get("dry_run")
    if dry_run is None:
        dry_run = any(isinstance(r, dict) and r.get("status") == "dry-run" for r in results)

    lines = [title, ""]
    if dry_run:
        lines.append("\u26a0\ufe0f <b>SIMULACAO (DRY-RUN)</b> \U0001F62C Nenhum pagamento foi efetuado.")
        lines.append("")

    if dry_run:
        contemplados = results
    else:
        contemplados = [r for r in results if isinstance(r, dict) and r.get("status") == "paid"]

    if contemplados:
        entries = []
        for idx, r in enumerate(contemplados, 1):
            pos = _normalize_result_pos(r.get("pos"), idx)
            entries.append((pos, r))
        entries.sort(key=lambda item: item[0] if isinstance(item[0], int) else 99999)
        for pos, r in entries:
            u = r.get("user", "unknown")
            s = r.get("share", 0)
            try:
                s_int = int(s)
            except Exception:
                s_int = 0
            pos_label = pos if isinstance(pos, int) else "?"
            lines.append(f"{pos_label}\u00ba \U0001F947 <code>{u}</code> \U0001F4B0 {fmt_sat(s_int)} sats")
    else:
        lines.append("Nenhum contemplado hoje.")

    totals = get_period_totals_from_audit(audit_path, reference_local)
    prev_month_dt = reference_local.replace(day=1) - dt.timedelta(days=1)
    prev_month_name = prev_month_dt.strftime("%B").capitalize()
    cur_month_name = reference_local.strftime("%B").capitalize()
    prev_year = reference_local.year - 1
    cur_year = reference_local.year

    lines.append("")
    lines.append(f"\U0001F4C8 Total pago no ano {prev_year}: <b>{fmt_sat(int(totals.get('prev_year', 0)))} sats</b>")
    lines.append(
        f"\U0001F4C5 Total pago em {prev_month_name} {prev_month_dt.year}: "
        f"<b>{fmt_sat(int(totals.get('prev_month', 0)))} sats</b>"
    )
    lines.append(f"\U0001F4C5 Total pago em {cur_month_name}: <b>{fmt_sat(int(totals.get('cur_month', 0)))} sats</b>")
    lines.append(f"\U0001F4C8 Total pago no ano {cur_year}: <b>{fmt_sat(int(totals.get('cur_year', 0)))} sats</b>")

    return "\n".join(lines)



# =======================
# lncli (fees últimos N h)
# =======================
def run_lncli_fwdinghistory(lncli_cmd: str, start_ts: int, end_ts: int) -> Dict[str, Any]:
    variants = [
        [lncli_cmd, "fwdinghistory",
         f"--start_time={start_ts}", f"--end_time={end_ts}", "--max_events=100000"],
        [lncli_cmd, "fwdinghistory",
         "--start_time", str(start_ts), "--end_time", str(end_ts), "--max_events", "100000"],
        [lncli_cmd, "fwdinghistory",
         f"--start_time={start_ts}", f"--end_time={end_ts}", "--max_events", "100000"],
    ]
    last_out = ""
    for cmd in variants:
        logger.info("Executando lncli fwdinghistory: %s", " ".join(cmd))
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
            out = (proc.stdout or proc.stderr or "").strip()
            last_out = out
            if not out:
                continue
            try:
                return json.loads(out)
            except Exception:
                logger.warning("Saída não-JSON nesta variação; tentando próxima...")
        except Exception as e:
            logger.warning("Falha ao executar lncli: %s", e)
    if last_out:
        idx = last_out.find("{")
        if idx >= 0:
            try:
                return json.loads(last_out[idx:])
            except Exception:
                logger.exception("Falha no parse (fallback).")
    return {}

def sum_fees_from_fwd(raw: Dict[str, Any]) -> int:
    candidates = []
    for k in ("forwarding_events", "forward_events", "events"):
        if isinstance(raw, dict) and k in raw and isinstance(raw[k], list):
            candidates = raw[k]; break
    if not candidates and isinstance(raw, list):
        candidates = raw
    total = 0
    for e in candidates:
        if not isinstance(e, dict):
            continue
        fee_sat = 0
        if "fee_msat" in e and e["fee_msat"] is not None:
            try:
                fee_sat = int(e["fee_msat"]) // 1000
            except:
                fee_sat = 0
        elif "fee" in e and e["fee"] is not None:
            try:
                fee_sat = int(e["fee"])
            except:
                fee_sat = 0
        elif "fee_sat" in e and e["fee_sat"] is not None:
            try:
                fee_sat = int(e["fee_sat"])
            except:
                fee_sat = 0
        total += fee_sat
    return total

# =======================
# LNbits OAuth2 (password flow)
# =======================
def get_access_token(lnbits_base: str, username: str, password: str) -> str:
    url = f"{lnbits_base.rstrip('/')}/api/v1/auth"
    r = requests.post(url, json={"username": username, "password": password}, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"Auth falhou ({r.status_code}): {r.text}")
    js = r.json()
    token = js.get("access_token")
    if not token:
        raise RuntimeError(f"Auth sem access_token: {js}")
    return token

def _headers_bearer(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

def _headers_xapikey(xkey: str) -> Dict[str, str]:
    return {"X-Api-Key": xkey, "Content-Type": "application/json"}

def _get(url: str, headers: Dict[str, str], timeout=15) -> Tuple[int, Any]:
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
        ctype = r.headers.get("Content-Type","")
        return r.status_code, r.json() if ctype.startswith("application/json") else r.text
    except Exception as e:
        logger.debug("GET %s falhou: %s", url, e)
        return 0, None

def _post(url: str, headers: Dict[str, str], body: Dict[str, Any], timeout=20) -> Tuple[int, Any]:
    try:
        r = requests.post(url, headers=headers, json=body, timeout=timeout)
        ctype = r.headers.get("Content-Type","")
        return r.status_code, r.json() if ctype.startswith("application/json") else r.text
    except Exception as e:
        logger.debug("POST %s falhou: %s", url, e)
        return 0, None

# =======================
# LNbits endpoints (Bearer)
# =======================
def get_wallet_detail(lnbits_base: str, token: str, wallet_id: str) -> Dict[str, Any]:
    for path in (f"/api/v1/wallets/{wallet_id}", f"/api/v1/wallet/{wallet_id}",
                 f"/users/api/v1/wallets/{wallet_id}", f"/users/api/v1/wallet/{wallet_id}"):
        url = f"{lnbits_base.rstrip('/')}{path}"
        code, resp = _get(url, _headers_bearer(token))
        if code == 200 and isinstance(resp, dict):
            return resp
    return {}

def get_wallet_balance_sats(detail: Dict[str, Any]) -> int:
    if "balance_msat" in detail and detail["balance_msat"] is not None:
        try:
            return int(detail["balance_msat"]) // 1000
        except:
            pass
    for k in ("balance", "wallet_balance", "balance_sats", "sats"):
        if k in detail and detail[k] is not None:
            try:
                return int(detail[k])
            except:
                pass
    return 0

def get_user_id_from_wallet(lnbits_base: str, token: str, wallet_id: str) -> Optional[str]:
    detail = get_wallet_detail(lnbits_base, token, wallet_id)
    if isinstance(detail, dict):
        uid = detail.get("user") or detail.get("user_id")
        if uid:
            return str(uid)
    return None

# --------- Users / Wallets por usuário ----------
def list_users_accounts(lnbits_base: str, token: str) -> List[Dict[str, Any]]:
    headers = _headers_bearer(token)
    candidate_paths = [
        "/users/api/v1/user",
        "/users/api/v1/users",
        "/users/api/v1/accounts",
        "/api/v1/user",
        "/api/v1/users",
        "/api/v1/accounts",
    ]
    for path in candidate_paths:
        url = f"{lnbits_base.rstrip('/')}{path}"
        code, resp = _get(url, headers)
        if code == 200:
            if isinstance(resp, dict) and isinstance(resp.get("data"), list):
                data = resp["data"]
                if data and isinstance(data[0], str):
                    users = [{"id": str(uid)} for uid in data]
                    logger.info("Users list OK via %s (objects=%d)", path, len(users))
                    return users
                if data and isinstance(data[0], dict):
                    logger.info("Users list OK via %s (objects=%d)", path, len(data))
                    return data
                logger.info("Users list OK via %s (vazio)", path)
                return []
            if isinstance(resp, list):
                logger.info("Users list OK via %s (count=%d)", path, len(resp))
                return resp
            if isinstance(resp, dict):
                for k in ("users", "accounts", "items", "results"):
                    if k in resp and isinstance(resp[k], list):
                        logger.info("Users list OK via %s (wrapper '%s', count=%d)", path, k, len(resp[k]))
                        return resp[k]
    logger.warning("Não foi possível listar usuários/contas via endpoints conhecidos.")
    return []

def list_wallets_for_user(lnbits_base: str, token: str, user_id: str, debug_api: bool=False) -> List[Dict[str, Any]]:
    """
    GET /users/api/v1/user/{user_id}/wallet
    (tenta retornar lista com campos: id, name, adminkey, inkey, balance_msat, user, ...)
    """
    headers = _headers_bearer(token)
    main_path = f"/users/api/v1/user/{user_id}/wallet"
    main_url  = f"{lnbits_base.rstrip('/')}{main_path}"
    code, resp = _get(main_url, headers)
    _dump_debug(f"user_{user_id}_wallets_primary", {"code": code, "resp": resp}, debug_api)
    if code == 200:
        if isinstance(resp, list):
            logger.info("Wallets de %s via %s -> %d", user_id, main_path, len(resp))
            return resp
        if isinstance(resp, dict):
            for k in ("wallets", "data", "items", "results"):
                if k in resp and isinstance(resp[k], list):
                    logger.info("Wallets de %s via %s (wrapper '%s') -> %d", user_id, main_path, k, len(resp[k]))
                    return resp[k]
    logger.warning("Nenhuma wallet encontrada para user_id=%s (code=%s).", user_id, code)
    return []

def collect_users_with_wallets(
    lnbits_base: str,
    token: str,
    exclude_user_id: Optional[str] = None,
    debug_api: bool=False,
    fund_wallet_id: Optional[str]=None,
) -> Dict[str, Dict[str, Any]]:
    """
    Retorna:
      user_key -> {
        "display": <str>,
        "wallets": [{"id":..., "balance": int, "inkey": str?, "adminkey": str?}, ...],
        "total": int
      }
    Exclui:
      - user_id explicitamente informado
      - qualquer user que possua a fund_wallet_id
    """
    users = list_users_accounts(lnbits_base, token)
    result: Dict[str, Dict[str, Any]] = {}

    if users:
        for u in users:
            user_id = str(u.get("id") or u.get("user_id") or u.get("userid") or u.get("user") or u.get("uuid") or u or "")
            display = u.get("username") or u.get("email") or u.get("name") or user_id or "unknown"
            if not user_id:
                user_id = display

            if exclude_user_id and str(user_id) == str(exclude_user_id):
                logger.info("Ignorando usuário excluído explicitamente: %s", user_id)
                continue

            wallets = list_wallets_for_user(lnbits_base, token, user_id, debug_api=debug_api)

            # Se alguma carteira desse user for a fund_wallet_id, exclui este user
            if fund_wallet_id and any((w.get("id") == fund_wallet_id or w.get("wallet") == fund_wallet_id or w.get("wallet_id") == fund_wallet_id) for w in wallets):
                logger.info("Ignorando usuário %s por ser dono da fund_wallet_id %s", user_id, fund_wallet_id)
                continue

            norm_wallets: List[Dict[str, Any]] = []
            total_bal = 0
            for w in wallets:
                wid = w.get("id") or w.get("wallet") or w.get("wallet_id")
                if not wid:
                    continue
                bal = get_wallet_balance_sats(w)
                inkey = w.get("inkey")
                adminkey = w.get("adminkey")
                norm_wallets.append({"id": wid, "balance": int(bal or 0), "inkey": inkey, "adminkey": adminkey})
                total_bal += int(bal or 0)

            result[user_id] = {"display": str(display), "wallets": norm_wallets, "total": total_bal}
            logger.info("User %s -> wallets=%d total=%d sats", user_id, len(norm_wallets), total_bal)

        if result:
            return result

    return result  # se vazio, caller lida

# =======================
# Pagamentos via API-Key
# =======================
def create_invoice_with_inkey(lnbits_base: str, inkey: str, amount_sats: int, memo: str, expiry: Optional[int]=None, unit: str="sat") -> Tuple[bool, Dict[str, Any]]:
    url = f"{lnbits_base.rstrip('/')}/api/v1/payments"
    body = {"out": False, "amount": int(amount_sats), "memo": memo}
    if expiry is not None:
        body["expiry"] = int(expiry)
    if unit:
        body["unit"] = unit  # "sat" por padrão
    code, resp = _post(url, _headers_xapikey(inkey), body)
    return (code in (200, 201) and isinstance(resp, dict)), {"status": code, "resp": resp}

def pay_invoice_with_adminkey(lnbits_base: str, adminkey: str, bolt11: str) -> Tuple[bool, Dict[str, Any]]:
    url = f"{lnbits_base.rstrip('/')}/api/v1/payments"
    body = {"out": True, "bolt11": bolt11}
    code, resp = _post(url, _headers_xapikey(adminkey), body)
    return (code in (200, 201) and isinstance(resp, dict)), {"status": code, "resp": resp}

# =======================
# Core
# =======================
def compute_and_distribute(
    lncli_cmd: str,
    lnbits_base: str,
    lnbits_user: str,
    lnbits_pass: str,
    fund_wallet_id: str,
    fund_admin_key: Optional[str],
    percent_to_use: float,
    since_hours: int,
    top_n: int,
    dry_run: bool,
    exclude_user_id: Optional[str] = None,
    debug_api: bool=False,
    telegram_token: Optional[str] = None,   # << NOVO
    telegram_chat: Optional[str] = None,    # << NOVO
):
    logger.info("=== Início rotina de distribuição ===")
    logger.info("Parâmetros: percent=%.4f since_hours=%dh top=%d dry_run=%s", percent_to_use, since_hours, top_n, dry_run)

    # fallback para env vars se não vier por CLI
    if not telegram_token:
        telegram_token = os.environ.get("TELEGRAM_TOKEN")
    if not telegram_chat:
        telegram_chat  = os.environ.get("TELEGRAM_CHAT")

    run_id = f"{dt.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')}"

    # 0) Auth OAuth2 (para descoberta)
    token = get_access_token(lnbits_base, lnbits_user, lnbits_pass)
    logger.info("Autenticado no LNbits (Bearer token obtido).")

    # 0.1) Descobrir admin_user_id (se não foi passado) pela carteira-fonte
    admin_user_id = exclude_user_id
    if not admin_user_id and fund_wallet_id:
        admin_user_id = get_user_id_from_wallet(lnbits_base, token, fund_wallet_id)
        if admin_user_id:
            logger.info("Identificado admin_user_id via fund wallet: %s (será excluído)", admin_user_id)

    # 1) Fees no período
    end_ts = now_unix()
    start_ts = unix_hours_ago(since_hours)
    raw = run_lncli_fwdinghistory(lncli_cmd, start_ts, end_ts)
    total_fees_sats = sum_fees_from_fwd(raw)
    logger.info("Fees no período (%dh): %s sats", since_hours, fmt_sat(total_fees_sats))

    # >>> Auditoria quando não houver fees
    if total_fees_sats <= 0:
        write_audit({
            "run_id": run_id,
            "ts": dt.datetime.utcnow().isoformat() + "Z",
            "action": "no_fees",
            "period_hours": since_hours,
            "fees_window": {"start_ts": start_ts, "end_ts": end_ts},
            "total_fees_sats": 0
        })
        logger.info("Nenhuma fee encontrada; encerrando.")
        return

    # 2) Montante a distribuir (já multiplicado por --percent)
    numerator = total_fees_sats * int(round(percent_to_use * 100))
    fees_to_distribute = numerator // 10000
    eligible_total = fees_to_distribute  # total elegível pelo %

    logger.info("Total a distribuir (após aplicar percent): %s sats", fmt_sat(fees_to_distribute))
    if fees_to_distribute <= 0:
        write_audit({
            "run_id": run_id,
            "ts": dt.datetime.utcnow().isoformat() + "Z",
            "action": "eligible_zero",
            "period_hours": since_hours,
            "fees_window": {"start_ts": start_ts, "end_ts": end_ts},
            "total_fees_sats": total_fees_sats,
            "percent": percent_to_use
        })
        logger.info("Montante calculado 0 sats; encerrando.")
        return

    # 3) Consolidar saldos por usuário, excluindo admin/funder
    users_map = collect_users_with_wallets(
        lnbits_base,
        token,
        exclude_user_id=admin_user_id,
        debug_api=debug_api,
        fund_wallet_id=fund_wallet_id,
    )
    if not users_map:
        write_audit({
            "run_id": run_id,
            "ts": dt.datetime.utcnow().isoformat() + "Z",
            "action": "users_map_empty",
            "period_hours": since_hours,
            "total_fees_sats": total_fees_sats,
            "percent": percent_to_use
        })
        logger.warning("Não foi possível obter usuários e wallets do LNbits; encerrando.")
        return

    # 4) Ranking
    ranking = [(k, v["total"], v) for k, v in users_map.items() if v["total"] > 0]
    ranking.sort(key=lambda x: x[1], reverse=True)
    top = ranking[:top_n]
    total_top_balances = sum(x[1] for x in top)

    # NEW: mapa de posições no TOP
    pos_map = {uid: pos for pos, (uid, _, _) in enumerate(top, 1)}

    # ----- Tabela: Top-N por saldo -----
    rows = []
    for pos, (uid, bal, info) in enumerate(top, 1):
        rows.append([pos, uid, fmt_sat(bal), len(info["wallets"])])
    if rows:
        logger.info("TOP-%d por saldo (excl. admin/funder):", len(rows))
        print_table(["#", "user_id", "saldo_total (sat)", "wallets"], rows)
    else:
        write_audit({
            "run_id": run_id,
            "ts": dt.datetime.utcnow().isoformat() + "Z",
            "action": "top_empty",
            "period_hours": since_hours,
            "fees_window": {"start_ts": start_ts, "end_ts": end_ts},
            "total_fees_sats": total_fees_sats,
            "percent": percent_to_use
        })
        logger.info("Top vazio (nenhum usuário com saldo > 0). Encerrando.")
        return

    logger.info("Soma saldos dos elegíveis: %s sats", fmt_sat(total_top_balances))

    # 5) Saldo da carteira-fonte (ignorado no dry-run, mas checado na execução real)
    if dry_run:
        logger.info("[DRY-RUN] Ignorando saldo da carteira-fonte para simular a distribuição completa.")
    else:
        if not fund_admin_key:
            raise RuntimeError("Pagamento real exige --fund-admin-key (adminkey da carteira de funding).")

    # 6) Alocação proporcional (inteiro) + ajuste de resto
    allocations: List[Dict[str, Any]] = []
    allocated_sum = 0
    for key, total_bal, info in top:
        share = (fees_to_distribute * total_bal) // total_top_balances
        allocations.append({"user_key": key, "user_total_bal": total_bal, "share": int(share), "info": info})
        allocated_sum += int(share)

    remainder = fees_to_distribute - allocated_sum
    i = 0
    while remainder > 0 and allocations:
        allocations[i % len(allocations)]["share"] += 1
        remainder -= 1
        i += 1

    # ----- Tabela: Alocação -----
    rows = []
    for a in allocations:
        rows.append([
            a["user_key"],
            fmt_sat(a["user_total_bal"]),
            fmt_sat(a["share"]),
            len(a["info"]["wallets"]),
        ])
    logger.info("Alocação proporcional:")
    print_table(["user_id", "saldo_total (sat)", "share (sat)", "qtde_wallets"], rows)

    # 7) Criar/pagar invoices (ou dry-run)
    results = []
    for a in allocations:
        user_key = a["user_key"]
        share = int(a["share"])
        wallets_of_user = a["info"]["wallets"]
        pos = pos_map.get(user_key, "?")
        pos_val = pos if isinstance(pos, int) else None
        if share <= 0 or not wallets_of_user:
            results.append({"user": user_key, "share": share, "status": "skipped", "pos": pos_val})
            continue

        # carteira destino = de MAIOR saldo
        target_wallet = max(wallets_of_user, key=lambda w: int(w.get("balance", 0)))
        target_wallet_id = target_wallet.get("id")
        target_inkey = target_wallet.get("inkey")

        if not target_wallet_id or not target_inkey:
            # tentativa de pegar inkey via detalhe (se não veio no payload)
            detail = get_wallet_detail(lnbits_base, token, target_wallet_id) if target_wallet_id else {}
            target_inkey = target_inkey or (detail.get("inkey") if isinstance(detail, dict) else None)

        # NEW: construir memo conforme pedido (sem exibir o --percent; usa fees_to_distribute já multiplicado)
        pos = pos_map.get(user_key, "?")
        share_pct = (share / fees_to_distribute * 100.0) if fees_to_distribute > 0 else 0.0
        memo = (
            f"BR⚡LN staking - Parabéns {user_key}! "
            f"Você está na {pos}ª posição dos TOP {top_n} investidores; "
            f"sua share é de {share_pct:.2f}% do total de {fmt_sat(fees_to_distribute)} sats "
            f"roteados nas últimas {since_hours} horas"
        )

        logger.info(">> Destino user=%s wallet=%s share=%s sat | inkey=%s",
                    user_key, target_wallet_id, fmt_sat(share), mask_key(target_inkey))

        if dry_run:
            write_audit({
                "run_id": run_id, "ts": dt.datetime.utcnow().isoformat() + "Z",
                "action": "dry_run_allocation", "user": user_key, "wallet": target_wallet_id,
                "share_sats": share, "memo": memo
            })
            logger.info("[DRY-RUN] Criaria invoice (%s sats) com memo='%s', inkey=%s e pagaria via fund_admin_key=%s",
                        fmt_sat(share), memo, mask_key(target_inkey), mask_key(fund_admin_key or ""))
            results.append({"user": user_key, "wallet": target_wallet_id, "share": share, "status": "dry-run", "pos": pos_val})
            continue

        if not fund_admin_key:
            logger.error("Sem --fund-admin-key; impossível pagar invoice.")
            results.append({"user": user_key, "wallet": target_wallet_id, "share": share, "status": "missing_fund_admin_key", "pos": pos_val})
            continue
        if not target_inkey:
            logger.error("Wallet destino sem inkey; impossível gerar invoice.")
            results.append({"user": user_key, "wallet": target_wallet_id, "share": share, "status": "missing_inkey", "pos": pos_val})
            continue

        # (1) criar invoice com INKEY da wallet destino
        ok_inv, inv_obj = create_invoice_with_inkey(lnbits_base, target_inkey, share, memo, unit="sat")
        write_audit({
            "run_id": run_id, "ts": dt.datetime.utcnow().isoformat() + "Z",
            "action": "create_invoice_api_key", "user": user_key, "wallet": target_wallet_id,
            "amount_sats": share, "memo": memo, "resp": inv_obj
        })
        if not ok_inv or not isinstance(inv_obj.get("resp"), dict):
            logger.error("Falha ao criar invoice (inkey). Resp=%s", inv_obj)
            results.append({"user": user_key, "wallet": target_wallet_id, "share": share, "status": "invoice_failed", "resp": inv_obj, "pos": pos_val})
            continue

        pr = inv_obj["resp"].get("payment_request")
        if not pr or not isinstance(pr, str) or not pr.startswith("ln"):
            logger.error("Invoice sem payment_request válido: %s", inv_obj["resp"])
            results.append({"user": user_key, "wallet": target_wallet_id, "share": share, "status": "invoice_no_pr", "pos": pos_val})
            continue

        # (2) pagar invoice com ADMKEY da carteira de funding
        ok_pay, pay_obj = pay_invoice_with_adminkey(lnbits_base, fund_admin_key, pr)
        write_audit({
            "run_id": run_id, "ts": dt.datetime.utcnow().isoformat() + "Z",
            "action": "pay_invoice_api_key", "user": user_key, "wallet": target_wallet_id,
            "amount_sats": share, "resp": pay_obj
        })
        if ok_pay:
            logger.info("Pagamento OK user=%s amount=%s sat | payment_hash=%s",
                        user_key, fmt_sat(share), pay_obj["resp"].get("payment_hash"))
            results.append({"user": user_key, "wallet": target_wallet_id, "share": share, "status": "paid", "pay_resp": pay_obj, "pos": pos_val})
        else:
            logger.error("Falha no pagamento user=%s amount=%s sat resp=%s", user_key, fmt_sat(share), pay_obj)
            results.append({"user": user_key, "wallet": target_wallet_id, "share": share, "status": "pay_failed", "pay_resp": pay_obj, "pos": pos_val})
        

    # 8) Sumário
    logger.info("=== Resumo da execução ===")

    # Totais do resumo
    eligible_total_sats = sum(a.get("share", 0) for a in allocations)  # pós-arredondamento
    distributed = 0 if dry_run else sum(r.get("share", 0) for r in results if r.get("status") == "paid")

    summary = {
        "run_id": run_id,
        "period_hours": since_hours,
        "fees_window": {"start_ts": start_ts, "end_ts": end_ts},
        "total_fees_sats": total_fees_sats,
        "percent": percent_to_use,
        "top_n": top_n,
        "dry_run": dry_run,
        "fees_to_distribute": eligible_total,           # elegível pelo %
        "eligible_total_sats": eligible_total_sats,     # soma dos shares pós-arredondamento
        "distributed_sats": distributed,                # pagos de fato (0 em dry-run)
        "results": results
    }
    logger.info(json.dumps(summary, ensure_ascii=False))

    # ----- Tabela final -----
    rows = []
    for r in results:
        rows.append([
            r.get("user"),
            r.get("wallet",""),
            fmt_sat(r.get("share",0)),
            r.get("status"),
        ])
    logger.info("Resultados por usuário:")
    print_table(["user_id", "wallet_id", "share (sat)", "status"], rows)

    write_audit({"action": "summary", **summary})
    
    # ===== Saída Telegram (opcional) =====
    msg = build_staking_results_message(
        summary,
        audit_path=AUDIT_FILE,
        reference_local=dt.datetime.now(tz=LOCAL_TZ),
    )

    if telegram_token and telegram_chat:
        send_telegram_message(telegram_token, telegram_chat, msg)
    else:
        logger.info("[TELEGRAM] Parâmetros não fornecidos; saída não enviada.")

    logger.info("=== Fim ===")

# =======================
# CLI
# =======================
def parse_args():
    p = argparse.ArgumentParser(description="Distribui % das fees (últimas N horas) para Top-N usuários do LNbits. Pagamento via LNbits API Keys (inkey/adminkey).")
    p.add_argument("--username", required=True, help="Usuário LNbits (OAuth2 password flow).")
    p.add_argument("--password", required=True, help="Senha LNbits (OAuth2 password flow).")
    p.add_argument("--lncli", default=LNCLI_DEFAULT, help="Comando/path do lncli.")
    p.add_argument("--lnbits-base", default=LNBITS_BASE_DEFAULT, help="Base URL do LNbits (ex.: https://wallet.br-ln.com).")
    p.add_argument("--fund-wallet-id", default=FUND_WALLET_ID_DEFAULT, help="WalletID da carteira-fonte (pagadora).")
    p.add_argument("--fund-admin-key", default=None, help="AdminKey da carteira-fonte (obrigatória quando não for --dry-run).")
    p.add_argument("--exclude-user-id", default=None, help="Opcional: user_id a excluir (ex.: admin). Se ausente, tento deduzir pela fund wallet.")
    p.add_argument("--percent", type=float, required=True, help="Percentual das fees a distribuir (ex.: 10 = 10%%).")
    p.add_argument("--since-hours", type=int, default=24, help="Janela de horas para somar fees (padrão 24).")
    p.add_argument("--top", type=int, default=10, help="Quantidade de usuários elegíveis (padrão 10).")
    p.add_argument("--dry-run", action="store_true", help="Simula tudo sem criar/pagar invoices.")
    p.add_argument("--debug-api", action="store_true", help="Salva respostas brutas de algumas chamadas de descoberta em ./debug/")
    p.add_argument("--telegram-token", default=os.environ.get("TELEGRAM_TOKEN"), help="Token do bot do Telegram (opcional).")
    p.add_argument("--telegram-chat",  default=os.environ.get("TELEGRAM_CHAT"),  help="Chat ID do Telegram (opcional).")

    return p.parse_args()

if __name__ == "__main__":
    args = parse_args()
    try:
        compute_and_distribute(
            lncli_cmd=args.lncli,
            lnbits_base=args.lnbits_base,
            lnbits_user=args.username,
            lnbits_pass=args.password,
            fund_wallet_id=args.fund_wallet_id,
            fund_admin_key=args.fund_admin_key,
            percent_to_use=args.percent,
            since_hours=args.since_hours,
            top_n=args.top,
            dry_run=args.dry_run,
            exclude_user_id=args.exclude_user_id,
            debug_api=args.debug_api,
            telegram_token=args.telegram_token,
            telegram_chat=args.telegram_chat,
        )
    except Exception as e:
        logger.exception("Erro não tratado: %s", e)
        sys.exit(1)
