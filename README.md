# 📖 Manual do Script `staking-reward.py`

## ⚡ Visão Geral

O `staking-reward.py` distribui uma **porcentagem das fees de forwards Lightning** recebidas pelo seu node LND para os **usuários do LNbits**, proporcionalmente ao saldo total que cada um mantém nas wallets.

Além disso, o script:

- Gera **logs detalhados** e um arquivo de **auditoria em JSONL**.
- Possui modo **`--dry-run`** para simulação completa sem pagamentos.
- Envia, opcionalmente, um **relatório diário para o Telegram** com:
  - Lista dos contemplados do dia (TOP N).
  - Quantidade de sats recebida por cada um.
  - Total de sats já pagos no **mês corrente**.

---

## 🧠 O que o script faz (resumo do fluxo)

1. **Autentica no LNbits** via OAuth2 (`POST /api/v1/auth`) usando `--username` e `--password`.
2. **Coleta as fees de forwards** do LND dos últimos `N` horas via:
   - `lncli fwdinghistory --start_time=... --end_time=...`
   - Soma `fee_msat`/`fee`/`fee_sat` de todos os eventos retornados.
3. **Aplica o percentual definido** (`--percent`) sobre o total de fees do período e calcula:
   - `fees_to_distribute` = montante em sats que será distribuído.
4. **Descobre e consolida saldos dos usuários LNbits**:
   - Lista usuários / contas (`GET /users/api/v1/user` ou variações).
   - Para cada usuário, lista wallets (`GET /users/api/v1/user/{user_id}/wallet`).
   - Soma o saldo (em sats) de todas as wallets do usuário.
5. **Exclui o usuário de funding/admin** do ranking:
   - Se `--exclude-user-id` foi passado, ele é ignorado diretamente.
   - Se NÃO foi passado, o script descobre quem é o dono da `--fund-wallet-id` e exclui esse usuário automaticamente.
   - Também exclui qualquer usuário que possua a carteira `--fund-wallet-id`.
6. **Monta o ranking TOP-N**:
   - Considera apenas usuários com saldo total > 0 sats.
   - Ordena por saldo total (descendente).
   - Pega os primeiros `--top` usuários (padrão: 10).
7. **Calcula a alocação proporcional**:
   - Cada usuário recebe uma fração de `fees_to_distribute` proporcional ao seu saldo.
   - Faz o arredondamento em sats e distribui o resto 1 sat por vez até fechar 100%.
8. **Escolhe a wallet de destino** de cada usuário:
   - Sempre a **wallet com maior saldo** dentro daquele usuário.
9. **Gera o memo da invoice** com uma mensagem de Staking:
   - Inclui posição no ranking, percentual da share e total de sats roteados no período.
10. **Cria e paga invoices via API do LNbits** (modo normal):
    - Cria invoice na wallet destino usando `inkey`.
    - Paga a invoice usando `adminkey` da carteira de funding.
11. **Registra tudo em auditoria**:
    - Cada invoice criada.
    - Cada pagamento efetuado.
    - Um resumo final da execução (`action = "summary"`).
12. **Envia mensagem opcional para o Telegram**:
    - Lista dos contemplados (simulados ou pagos).
    - Posição no ranking e sats recebidos.
    - Total pago no mês corrente (soma de `distributed_sats` nas execuções reais daquele mês).

---

## 🚀 Execução Básica

### Exemplo típico em modo DRY-RUN (simulação)

```bash
/usr/bin/python3 /home/<USER>/staking-brln/staking-reward.py \
  --username admin \
  --password 'SUA_SENHA_AQUI' \
  --percent 50 \
  --since-hours 24 \
  --top 10 \
  --lncli lncli \
  --fund-wallet-id 0669f3ae71b64cfd840d8702b371ff30 \
  --dry-run
````

### Exemplo com pagamentos reais (NÃO usa `--dry-run`)

```bash
/usr/bin/python3 /home/<USER>/staking-brln/staking-reward.py \
  --username admin \
  --password 'SUA_SENHA_AQUI' \
  --percent 50 \
  --since-hours 24 \
  --top 10 \
  --lncli lncli \
  --lnbits-base https://wallet.br-ln.com \
  --fund-wallet-id 0669f3ae71b64cfd840d8702b371ff30 \
  --fund-admin-key 'SUA_ADMINKEY_DA_CARTEIRA_FUNDING' 
```

> 🔐 **Atenção:** Use `--fund-admin-key` **somente** quando NÃO for dry-run. Em `--dry-run` ele é ignorado.

---

## 📣 Execução com saída para Telegram

Você pode configurar o script para mandar o resumo da rodada direto em um grupo/canal do Telegram.

### Opção 1 — Passando token e chat via CLI

```bash
/usr/bin/python3 /home/<USER>/staking-brln/staking-reward.py \
  --username admin \
  --password 'SUA_SENHA_AQUI' \
  --percent 50 \
  --since-hours 24 \
  --top 10 \
  --fund-wallet-id 0669f3ae71b64cfd840d8702b371ff30 \
  --fund-admin-key 'SUA_ADMINKEY_DA_CARTEIRA_FUNDING' \
  --telegram-token 'SEU_TELEGRAM_BOT_TOKEN' \
  --telegram-chat '-100XXXXXXXXXX'
```

### Opção 2 — Usando variáveis de ambiente

No shell / crontab:

```bash
export TELEGRAM_TOKEN="SEU_TELEGRAM_BOT_TOKEN"
export TELEGRAM_CHAT="-100XXXXXXXXXX"
```

E então rodar o script **sem** passar esses parâmetros na linha de comando (eles serão lidos do ambiente):

```bash
/usr/bin/python3 /home/<USER>/staking-brln/staking-reward.py \
  --username admin \
  --password 'SUA_SENHA_AQUI' \
  --percent 50 \
  --since-hours 24 \
  --top 10 \
  --fund-wallet-id 0669f3ae71b64cfd840d8702b371ff30 \
  --fund-admin-key 'SUA_ADMINKEY_DA_CARTEIRA_FUNDING'
```

Se `TELEGRAM_TOKEN` ou `TELEGRAM_CHAT` não estiverem configurados (nem via CLI, nem via env), o script apenas registra no log que a saída do Telegram foi ignorada.

---

## ⚙️ Parâmetros CLI

| Parâmetro           | Obrigatório | Descrição                                                                                                                                               | Padrão                                   |
| ------------------- | ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------- |
| `--username`        | ✅           | Usuário LNbits para autenticação (OAuth2 password flow). Normalmente: `admin`.                                                                          | —                                        |
| `--password`        | ✅           | Senha do usuário LNbits usado para autenticação.                                                                                                        | —                                        |
| `--lncli`           | ❌           | Caminho/comando para o `lncli`.                                                                                                                         | `lncli`                                  |
| `--lnbits-base`     | ❌           | URL base da sua instância LNbits.                                                                                                                       | `https://wallet.br-ln.com`               |
| `--fund-wallet-id`  | ❌           | WalletID da **carteira-fonte** (pagadora). É de onde sairão os sats.                                                                                    | `0669f3ae71b64cfd840d8702b371ff30`       |
| `--fund-admin-key`  | ❌           | **AdminKey** da carteira-fonte. Obrigatória quando **não** estiver em `--dry-run` (para pagar as invoices).                                             | `None` (não definido)                    |
| `--exclude-user-id` | ❌           | `user_id` explícito a ser excluído do ranking (ex.: o admin). Se omitido, o script tenta descobrir automaticamente quem é o dono da `--fund-wallet-id`. | `None`                                   |
| `--percent`         | ✅           | Percentual das fees a distribuir. Ex.: `50` significa 50% das fees do período.                                                                          | —                                        |
| `--since-hours`     | ❌           | Janela em horas para somar fees de forwards. Ex.: `24` = últimas 24h.                                                                                   | `24`                                     |
| `--top`             | ❌           | Número máximo de usuários elegíveis no ranking.                                                                                                         | `10`                                     |
| `--dry-run`         | ❌           | Simula a distribuição **sem criar ou pagar invoices**. Ainda gera logs, tabelas e auditoria (com `distributed_sats = 0`).                               | `False`                                  |
| `--debug-api`       | ❌           | Salva respostas brutas de algumas chamadas de descoberta em `./debug/*.json` (users, wallets, etc). Útil para troubleshooting.                          | `False`                                  |
| `--telegram-token`  | ❌           | Token do bot do Telegram (se não informado, tenta usar `TELEGRAM_TOKEN` do ambiente).                                                                   | `os.environ["TELEGRAM_TOKEN"]` ou `None` |
| `--telegram-chat`   | ❌           | Chat ID do Telegram (se não informado, tenta usar `TELEGRAM_CHAT` do ambiente).                                                                         | `os.environ["TELEGRAM_CHAT"]` ou `None`  |

---

## 🧮 Lógica de Cálculo e Distribuição

### 1. Coleta das fees

* O script chama `lncli fwdinghistory` para o intervalo entre:

  * `end_ts = agora`
  * `start_ts = agora - (since_hours * 3600)`
* Ele tenta diferentes formatos de chamada (`--start_time=...` / `--start_time ...`) para se adaptar ao seu ambiente.
* Para cada evento, busca a fee em:

  * `fee_msat` (preferencial; convertido para sats com `// 1000`),
  * ou `fee`,
  * ou `fee_sat`.

Se `total_fees_sats <= 0`:

* Registra uma entrada na auditoria com `action: "no_fees"`.
* Encerra a execução.

### 2. Montante a distribuir

* Recebe `--percent` como `float`, ex.: `50.0`.

* Calcula:

  ```python
  numerator = total_fees_sats * int(round(percent_to_use * 100))
  fees_to_distribute = numerator // 10000
  ```

* Ou seja, `fees_to_distribute` já é o valor FINAL em sats a ser distribuído (ex.: 50% das fees do período, arredondado para baixo).

Se `fees_to_distribute <= 0`:

* Registra `action: "eligible_zero"` na auditoria.
* Encerra a execução.

### 3. Exclusão do usuário de funding

* Primeiro, o script tenta determinar quem é o `admin_user_id`:

  * Se `--exclude-user-id` foi passado → usa esse valor.
  * Caso contrário:

    * Chama `get_user_id_from_wallet()` passando `--fund-wallet-id` e descobre o `user_id` dono dessa carteira.
* Esse `admin_user_id` é **removido do ranking**.
* Além disso, **qualquer usuário que tenha uma wallet com o `fund_wallet_id`** também é excluído.

### 4. Ranking Top-N

* Para cada usuário:

  * Soma o saldo das suas wallets, em sats.
* Filtra apenas usuários com saldo > 0.
* Ordena em ordem decrescente de saldo.
* Pega os primeiros `--top` usuários.

O script loga uma tabela como:

```text
TOP-10 por saldo (excl. admin/funder):
+-----------------------+----------------------+-----------+------+
 # | user_id            | saldo_total (sat)    | wallets   |
+-----------------------+----------------------+-----------+------+
 1 | cdcec1b4...        | 3.000.000            | 2         |
 2 | 30ca2634...        |   500.000            | 1         |
...
+-----------------------+----------------------+-----------+------+
```

Se o TOP estiver vazio (ninguém com saldo > 0), registra `action: "top_empty"` na auditoria e encerra.

### 5. Alocação proporcional

* Com `fees_to_distribute` e a soma total dos saldos dos TOP usuários (`total_top_balances`), o script calcula a share inicial:

  ```python
  share = (fees_to_distribute * saldo_user) // total_top_balances
  ```

* Soma todas as shares e calcula o resto:

  ```python
  remainder = fees_to_distribute - allocated_sum
  ```

* Se houver resto (`remainder > 0`), ele distribui 1 sat adicional por vez, ciclando pela lista de alocações, até o resto chegar a zero.

A tabela de alocação fica algo como:

```text
Alocação proporcional:
+-----------------------+----------------------+--------------+-------------+
 user_id                | saldo_total (sat)    | share (sat)  | qtde_wallets|
+-----------------------+----------------------+--------------+-------------+
 cdcec1b4...            | 3.000.000            | 15           | 2           |
 30ca2634...            |   500.000            |  2           | 1           |
...
+-----------------------+----------------------+--------------+-------------+
```

---

## 💸 Criação e Pagamento das Invoices (LNbits)

Para cada usuário com `share > 0` e pelo menos uma wallet:

1. **Escolhe a wallet destino:**

   * A wallet com **maior saldo** do usuário.

2. **Gera o memo da invoice:**

   O memo tem o formato:

   > `BR⚡LN staking - Parabéns {user_id}! Você está na {posição}ª posição dos TOP {top_n} investidores; sua share é de {share_pct:.2f}% do total de {fees_to_distribute} sats roteados nas últimas {since_hours} horas`

   Onde:

   * `{user_id}` é o identificador retornado pelo LNbits.
   * `{posição}` é a posição do usuário no ranking (1, 2, 3...).
   * `{share_pct}` é o percentual do montante distribuído que esse usuário recebeu.
   * `{fees_to_distribute}` é o **total de sats distribuído** na rodada (já após aplicar o `--percent`).
   * `{since_hours}` é o intervalo em horas usado para somar as fees.

3. **Modo DRY-RUN**

   * Não cria invoice nem paga.
   * Apenas registra na auditoria e no log algo como:

   ```text
   [DRY-RUN] Criaria invoice (13 sats) com memo='BR⚡LN staking - ...' inkey=****abcd e pagaria via fund_admin_key=****wxyz
   ```

   E grava uma entrada:

   ```json
   {
     "action": "dry_run_allocation",
     "user": "...",
     "wallet": "...",
     "share_sats": 13,
     "memo": "..."
   }
   ```

4. **Modo real (sem `--dry-run`)**

   * **(1) Criar invoice** usando o `inkey` da wallet destino:

     ```http
     POST /api/v1/payments
     X-Api-Key: <inkey>
     {
       "out": false,
       "amount": <share>,
       "memo": "<memo>",
       "unit": "sat"
     }
     ```

   * **(2) Pagar invoice** usando o `adminkey` da carteira de funding:

     ```http
     POST /api/v1/payments
     X-Api-Key: <fund_admin_key>
     {
       "out": true,
       "bolt11": "<payment_request>"
     }
     ```

   * Cada passo é registrado na auditoria como:

     * `action: "create_invoice_api_key"`
     * `action: "pay_invoice_api_key"`

   * Somente invoices com `status: "paid"` entram em `distributed_sats`.

---

## 📂 Logs e Auditoria

Os arquivos são criados no **mesmo diretório do script**:

* **Log principal:** `lnbits_payout.log`
  Contém:

  * Início e fim da execução.
  * Parâmetros usados.
  * Tabelas do Top-N e da alocação.
  * Erros de API, lncli, etc.
  * Status de cada pagamento (paid, invoice_failed, pay_failed, etc).

* **Auditoria JSONL:** `audit_payouts.jsonl`
  Formato **JSONL** (um JSON por linha). Exemplos de ações:

  * `no_fees`
  * `eligible_zero`
  * `users_map_empty`
  * `top_empty`
  * `dry_run_allocation`
  * `create_invoice_api_key`
  * `pay_invoice_api_key`
  * `summary`

  Exemplo de linha com pagamento:

  ```json
  {
    "run_id": "2025-10-03T20:22:58Z",
    "action": "pay_invoice_api_key",
    "user": "cdcec1b41...",
    "wallet": "e8b6c4a0...",
    "amount_sats": 4,
    "resp": {
      "status": 200,
      "resp": {
        "payment_hash": "xyz..."
      }
    }
  }
  ```

  Exemplo de linha de resumo:

  ```json
  {
    "run_id": "2025-10-03T20:22:58Z",
    "action": "summary",
    "period_hours": 24,
    "fees_window": { "start_ts": 1696368000, "end_ts": 1696454400 },
    "total_fees_sats": 35,
    "percent": 50.0,
    "fees_to_distribute": 17,
    "eligible_total_sats": 17,
    "distributed_sats": 17,
    "results": [ ... ]
  }
  ```

---

## 📆 Cálculo do Total Mensal (para o Telegram)

A função `get_monthly_total_from_audit()`:

* Lê o arquivo `audit_payouts.jsonl`.
* Filtra apenas linhas com `action: "summary"`.
* Converte o `run_id` (UTC, ex.: `"2025-10-03T20:22:58Z"`) para o fuso `America/Sao_Paulo`.
* Soma o campo `distributed_sats` **apenas das execuções reais** daquele mês (em dry-run, esse valor é 0).
* O resultado é o **total de sats já pagos no mês corrente**.

Esse total é incluído na mensagem Telegram:

> `📅 Total pago em Outubro: 123.456 sats`

---

## 📨 Mensagem enviada para o Telegram

A mensagem gerada segue o formato:

```text
🏆 BR⚡LN Staking – TOP 10 Stakers - Resultado do dia 14/11

⚠️ SIMULAÇÃO (DRY-RUN) – Nenhum pagamento foi efetuado.   # (apenas se for dry-run)

1º — cdcec1b4... • 15 sats
2º — 30ca2634... •  2 sats
...

📅 Total pago em Novembro: 123.456 sats
```

* Em **modo real**, a lista inclui apenas usuários com `status == "paid"`.
* Em **dry-run**, a lista inclui todos os usuários do TOP-N com suas shares simuladas.
* A ordem é sempre pela posição no ranking (1º, 2º, 3º...).

---

## ⏰ Sugestão de Crontab (rodar todo dia 23:00)

Exemplo de entrada no `crontab -e`:

```cron
# Staking Reward diário às 23:00
0 23 * * * TELEGRAM_TOKEN="SEU_TOKEN" TELEGRAM_CHAT="-100XXXXXXXXXX" /usr/bin/python3 /root/node-check/staking-reward.py \
  --username admin \
  --password 'SUA_SENHA_AQUI' \
  --percent 50 \
  --since-hours 24 \
  --top 10 \
  --lncli lncli \
  --lnbits-base https://wallet.br-ln.com \
  --fund-wallet-id 0669f3ae71b64cfd840d8702b371ff30 \
  --fund-admin-key 'SUA_ADMINKEY_DA_CARTEIRA_FUNDING' >> /root/node-check/staking-cron.log 2>&1
```

> 💡 No cron, é comum redirecionar a saída para um log separado (`staking-cron.log`) além do próprio `lnbits_payout.log`.

---

## ✅ Boas Práticas

* Sempre rodar **primeiro com `--dry-run`** depois de qualquer alteração no ambiente (node, LNbits, carteiras).
* Validar se a `--fund-wallet-id` **realmente é a carteira de funding** e se está associada ao usuário correto.
* Proteger o arquivo que contém `--fund-admin-key` (ou variáveis de ambiente) — é a chave que paga tudo.
* Usar `--debug-api` quando houver dúvida sobre:

  * Estrutura de retorno de usuários / wallets no LNbits.
  * Falha para localizar inkeys ou saldos.
* Acompanhar o arquivo `audit_payouts.jsonl` para auditoria e conciliação.
* Verificar periodicamente se o total mensal mostrado no Telegram bate com a soma esperada dos pagamentos.

---




