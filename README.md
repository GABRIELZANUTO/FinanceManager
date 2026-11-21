# Finanças Pessoais (Finance Manager)

Controle financeiro pessoal feito em Django. Reúne contas, cartões de crédito, transações, orçamentos, metas, contas
recorrentes e dívidas num lugar só, responde "como estou indo neste mês?" em linguagem simples e roda com um único arquivo
SQLite, sem serviços externos. Nada é carregado de terceiros: fontes, ícones, gráficos e scripts são servidos pelo próprio
app.

O mesmo código roda de três jeitos:

- **Site** (servidor com gunicorn, por exemplo na Discloud);
- **Aplicativo Android** com os dados no próprio celular, funcionando sem internet;
- **Os dois juntos**, sincronizados: o que você lança no celular aparece no site e vice-versa.

Visual simples e leve, sem rastreamento, sem chamadas de rede do navegador a não ser para o seu próprio servidor.

## Telas

Capturas feitas com os dados de demonstração (`python manage.py seed_demo`); para gerar de novo, veja
[Regerar as capturas de tela](#regerar-as-capturas-de-tela).

| | |
|---|---|
| ![Painel](docs/screenshots/dashboard.png) **Painel** com observações em linguagem simples, próximos vencimentos, metas e assinaturas | ![Transações](docs/screenshots/transactions.png) **Transações** com filtros, ordenação, filtros salvos e ações em lote |
| ![Nova transação](docs/screenshots/add-transaction.png) **Nova transação** com valores sugeridos, dicas de regra e "mais opções" | ![Orçamentos](docs/screenshots/budgets.png) **Orçamentos** com progresso, saldo acumulado, quanto ainda pode gastar por dia, projeção e histórico |
| ![Análises](docs/screenshots/analytics.png) **Análises**: gráficos SVG próprios, filtros guardados na URL | ![Relatórios](docs/screenshots/reports.png) **Relatórios**: demonstrativo, tendências, patrimônio, fluxo de caixa, CSV e impressão |
| ![Contas](docs/screenshots/accounts.png) **Contas** e transferências; o cartão mostra o limite já descontando as parcelas a vencer | ![Metas](docs/screenshots/goals.png) **Metas de poupança** com anéis de progresso |
| ![Cartões](docs/screenshots/cards.png) **Faturas de cartão**: importação de CSV ou PDF | ![Conflito](docs/screenshots/conflict.png) **Conflito de edição**: o registro mudou em outro lugar e você escolhe o que fica |
| ![Categorias](docs/screenshots/categories.png) **Categorias**, subcategorias, regras e tags | ![Diálogo de categoria](docs/screenshots/category-dialog.png) Escolha de cor e ícone |
| ![Recorrentes](docs/screenshots/recurring.png) **Contas recorrentes** | ![Pessoas](docs/screenshots/people.png) **Dívidas e contas divididas** |
| ![Resumo semanal](docs/screenshots/digest.png) **Resumo semanal** | ![Lixeira](docs/screenshots/trash.png) **Lixeira** (30 dias) e histórico de alterações |
| ![Importação](docs/screenshots/import-preview.png) **Importação de CSV** com prévia, erros por linha e detecção de duplicadas | ![Backup](docs/screenshots/backup.png) **Backup** e restauração (JSON) |
| ![Configurações](docs/screenshots/settings.png) **Configurações**: moeda, formato dos números, meses fechados, apagar dados | ![Busca](docs/screenshots/search.png) **Busca rápida** (`/`) entende frases como "restaurante mês passado acima de 500" |
| ![Boas-vindas](docs/screenshots/welcome.png) **Configuração inicial** | ![Conciliação](docs/screenshots/transactions-tools.png) **Conciliação** com o extrato do banco |
| ![Cadastro](docs/screenshots/register-invalid.png) **Cadastro** com verificações específicas na hora | ![Cadastro válido](docs/screenshots/register-valid.png) Cadastro com tudo certo |
| ![Entrar](docs/screenshots/login.png) **Entrar** | ![Criar conta](docs/screenshots/register.png) **Criar conta**, mostrar ou esconder a senha e medidor de força |

### Menu lateral e celular

| Expandido | Recolhido | Gaveta no celular |
|---|---|---|
| ![Menu expandido](docs/screenshots/sidebar-expanded.png) | ![Menu recolhido](docs/screenshots/sidebar-collapsed.png) | ![Gaveta no celular](docs/screenshots/mobile-drawer.png) |

| Painel no celular | Transações no celular | Análises no celular | Orçamentos no celular |
|---|---|---|---|
| ![Painel no celular](docs/screenshots/mobile-dashboard.png) | ![Transações no celular](docs/screenshots/mobile-transactions.png) | ![Análises no celular](docs/screenshots/mobile-analytics.png) | ![Orçamentos no celular](docs/screenshots/mobile-budgets.png) |

## Funcionalidades

**Dia a dia**
- Configuração inicial: moeda, contas, categorias iniciais, orçamento mensal opcional e dados de exemplo opcionais.
- Nova transação de qualquer tela (tecla `n`, botão Adicionar ou o botão flutuante no celular), com valores sugeridos: hoje,
  a última conta e categoria usadas, e o tipo segue a categoria.
- **Lançamento rápido** numa linha só: "Bolo 50,00 restaurante conta principal" vira nome, valor, categoria e conta, mesmo
  com o texto aproximado.
- Contas (banco, dinheiro, carteira digital, cartão de crédito) com saldo próprio, transferências que nunca contam como
  receita ou despesa e **conta padrão** para os lançamentos sem conta escolhida.
- Categorias com cor, ícone, tipo (receita, despesa ou ambos) e um nível de subcategoria; tags e favorecidos com
  sugestão; regras de categorização automática (também na importação).
- Dividir uma compra entre categorias, duplicar, trocar a categoria na própria lista, excluir ou recategorizar em lote,
  desfazer e lixeira de 30 dias. Toda alteração fica registrada e pode ser vista por transação.
- Contas recorrentes (semanais, mensais ou uma vez só) lançadas automaticamente, com pausa e retomada. Quando uma
  ocorrência parece já ter sido lançada à mão, o programa **pergunta** se é o mesmo lançamento em vez de contar duas vezes.
- Fechar o mês para não editar por engano (dá para reabrir) e conciliar a conta com o saldo do extrato.

**Cartão de crédito**
- Limite, dia de vencimento e **dia de fechamento da fatura**. Compra feita depois do fechamento vai para a fatura
  seguinte; sem o dia preenchido, a fatura fecha **7 dias antes do vencimento**.
- As compras contam **no mês da fatura** (data de vencimento, com a data real da compra guardada) ou no mês da compra,
  à sua escolha.
- **Limite inteligente:** as **parcelas que ainda vão vencer** já ocupam o limite, como no banco. A tela mostra quanto
  é de compras, quanto é de parcelas a vencer e cada compra parcelada com as parcelas que faltam.
- Importação da fatura em CSV (com leitura opcional de PDF): separa as compras por categoria, reconhece parcelas
  ("Loja 3/10"), agenda as próximas e evita contar duas vezes o que já foi lançado à mão.

**Planejamento e análise**
- **Planejamento do mês:** o que já entrou e saiu, o que ainda vai vencer (salário, contas fixas, parcelas, faturas),
  quanto vai ter nos bancos no fim do mês e quanto fica livre depois dos cartões.
- Frases no Painel como "Você já gastou 62% do orçamento de Mercado e faltam 11 dias", previsão do fim do mês,
  comparação com o mês anterior, gastos fora do comum, detecção de assinaturas, próximos vencimentos e metas.
- Orçamentos por mês, gerais e por categoria: saldo acumulado, alertas, copiar do mês anterior, projeção, quanto ainda
  pode gastar por dia, semana a semana, previsto x realizado.
- Metas de poupança com data, valor mensal necessário e histórico de aportes.
- Análises com gráficos (receitas e despesas, fluxo de caixa, categorias, mapa de calor diário, dia da semana, saldo no
  tempo, favorecidos), todos com dicas ao passar o mouse e versão em tabela.
- Relatórios com comparação ao período anterior, patrimônio e fluxo de caixa por conta, exportação em CSV e impressão.
- Resumo semanal.

**Dinheiro com outras pessoas e dados**
- Dívidas e contas divididas: emprestar, pegar emprestado ou dividir uma conta, combinar parcelas, quitar e mandar um
  resumo pelo WhatsApp.
- Exportação CSV (respeita os filtros e é segura contra fórmulas) e importação CSV com prévia.
- Backup JSON com um clique e restauração validada. **Backup diário automático no Google Drive** (opcional).
- Segunda moeda opcional no Painel, com cotação digitada por você.

**Site, celular e sincronização**
- **Aplicativo Android** (projeto separado) que roda este mesmo código no celular: os dados ficam no aparelho e tudo
  funciona sem internet.
- **Sincronização** entre o celular e o site: o que você lança offline fica numa fila e vai para o site quando a internet
  volta. Mudanças em campos diferentes do mesmo registro se juntam sozinhas; quando os dois lados mudam a mesma coisa, o
  conflito aparece para você escolher ("usar a deste celular", "usar a do servidor", "lançar manualmente").
- Tela "Sincronizando" que bloqueia o app até terminar, e um ícone na barra do topo que sincroniza na hora.
- **Controle de conflitos de edição:** se duas abas, o site e o celular mudam o mesmo registro, a segunda gravação não
  apaga a primeira; aparece "Este registro foi alterado em outro lugar" com os campos diferentes e quem alterou.
- **API** (`/api/v1/`) com chaves de acesso por aparelho ou programa, permissões (ler, alterar, sincronizar), limite de
  uso por chave e documentação em `/api/v1/docs/`.

**Interface**
- Visual claro e simples com menu lateral recolhível, gaveta em telas pequenas, navegação inferior no celular, atalhos
  de teclado (`n` nova transação, `/` busca, `[` recolher o menu, `?` ajuda) e respeito a `prefers-reduced-motion`.
- Busca rápida em transações, favorecidos, categorias, tags, contas e metas; filtros salvos.
- Mostrar ou esconder senha, aviso de Caps Lock, medidor de força e verificações específicas no cadastro.
- Sem scripts ou estilos embutidos, Content Security Policy rígida e nenhuma requisição a terceiros.

## Requisitos

Python 3.9 ou mais novo e Django 4.2 a 5.2 (o Django 5 precisa de Python 3.10 ou mais novo). Tudo está no
`requirements.txt`: Django; `gunicorn` e `whitenoise` para servir em produção; e o SDK usado pela leitura opcional de
faturas em PDF (só é chamado quando a chave da API dele está configurada). O resto usa apenas a biblioteca padrão do
Python, e por isso o mesmo código roda também dentro do aplicativo Android.

## Como rodar

```bash
git clone https://github.com/GABRIELZANUTO/FinanceManager.git
cd FinanceManager
scripts/bootstrap.sh --demo --run      # cria o .venv, instala, escreve o .env, migra, cria um usuário de demonstração e inicia
```

Abra http://127.0.0.1:8000/ e entre com `demo` / `demo-password-123`, ou crie a sua conta.
Sem `--demo` o banco começa vazio. Passo a passo manual:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # depois preencha SECRET_KEY (e DEBUG=1 para uso local)
python manage.py migrate
python manage.py createsuperuser   # opcional, para o /admin/
python manage.py runserver
```

No Windows, ative com `.venv\Scripts\activate` e copie com `copy .env.example .env`.

## Configuração

As configurações vêm de variáveis de ambiente, opcionalmente de um arquivo `.env` na raiz do projeto (as variáveis de
ambiente de verdade têm prioridade). Veja o `.env.example`.

| Variável | Padrão | Para que serve |
|---|---|---|
| `SECRET_KEY` | nenhum | Obrigatória, a não ser com `DEBUG=1`. Use um valor longo e aleatório. |
| `DEBUG` | `0` | Modo de desenvolvimento. Nunca ligue em produção. |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` com debug, vazio sem | Nomes de host separados por vírgula. |
| `CSRF_TRUSTED_ORIGINS` | vazio | Origens com esquema, para HTTPS atrás de um proxy. |
| `DATABASE_PATH` | `db.sqlite3` | Onde fica o arquivo SQLite. |
| `TIME_ZONE` | `Asia/Kathmandu` | Usado para "hoje" e para a virada do mês (use `America/Sao_Paulo`). |
| `ALLOW_NEGATIVE_BALANCE` | `1` | Política padrão de saldo negativo (cada usuário pode mudar). |
| `ALLOW_REGISTRATION` | `1` | `0` fecha novos cadastros. |
| `DEFAULT_CURRENCY_CODE`, `DEFAULT_CURRENCY_SYMBOL` | `NPR`, `Rs` | Padrões para novos usuários (use `BRL`, `R$`). |
| `LOGIN_MAX_ATTEMPTS`, `LOGIN_LOCKOUT_SECONDS` | `5`, `900` | Proteção contra tentativas de senha. |
| `REGISTER_MAX_PER_HOUR` | `10` | Cadastros por endereço por hora. |
| `SECURE_SSL_REDIRECT` | ligado com `DEBUG=0` | Redireciona HTTP para HTTPS. |
| `SECURE_HSTS_SECONDS` | `31536000` com `DEBUG=0` | Duração do HSTS. |
| `TRUST_X_FORWARDED_PROTO` | `0` | Ligue só atrás de um proxy que sempre envia esse cabeçalho. |
| `API_TOKEN_RATE`, `API_TOKEN_WINDOW` | `600`, `60` | Requisições por chave da API por janela (segundos). |
| `SYNC_REQUIRE_PLAN` | `0` | Só usuários com `Profile.plan = "sync"` podem sincronizar um celular. |
| `SYNC_SERVER_URL` | vazio | Site padrão do aplicativo do celular (ou use `expensemanager/local_settings.py`). |
| `GDRIVE_CLIENT_ID`, `GDRIVE_CLIENT_SECRET`, `GDRIVE_REFRESH_TOKEN` | vazio | Backup diário no Google Drive (veja abaixo). |
| `GDRIVE_FOLDER`, `GDRIVE_KEEP`, `GDRIVE_BACKUP_HOUR` | veja o `.env.example` | Nome da pasta, quantos backups guardar e a hora do dia. |

O que é só da sua instalação e não deve ir para o git (o site padrão do app do celular, por exemplo) fica em
`expensemanager/local_settings.py`; copie de `expensemanager/local_settings.example.py`.

## API, sincronização e backup

- **API:** `/api/v1/` com uma chave (`Authorization: Bearer fp_...`), sem cookie e sem token CSRF. Crie chaves em
  Configurações, "Acesso pela API e dispositivos", ou com `POST /api/v1/auth/token/`. Documentação legível em
  `/api/v1/docs/` (gerada de `app/static/app/api/openapi.json`; rode `scripts/gerar_openapi.py` depois de criar uma rota).
- **Conflitos de edição:** todo registro tem `uid` e `version`. Uma gravação baseada numa versão antiga recebe HTTP 409
  com o que está salvo agora, e as telas perguntam o que manter.
- **Sincronização com o celular:** o app Android roda este mesmo código no aparelho (`APP_MODE=1`) e guarda uma fila das
  alterações locais; `/api/v1/sync/` envia, recebe e baixa tudo em páginas, junta campo a campo e informa os conflitos.
  O código está em `app/sync/`; o teste de ponta a ponta roda um "celular" em outro processo (`app/tests/test_sync_e2e.py`).
- **Backup diário no Google Drive:** `python manage.py backup_drive` (ou `--local PASTA` para só gerar o zip) cria um zip
  com o banco inteiro em JSON e o backup de cada usuário. Autorize uma vez com
  `python scripts/autorizar_google_drive.py client_secret.json` (um cliente OAuth do tipo "App para computador"); em
  produção o próprio site envia um backup por dia. Para restaurar: `python manage.py loaddata banco.json` num banco novo
  já migrado.

## Segurança

- A chave secreta nunca fica no código. `SECRET_KEY` é obrigatória, a não ser com `DEBUG=1`.
- Com `DEBUG=0`: cookies seguros e HttpOnly, SameSite, HSTS, redirecionamento para HTTPS, nosniff,
  `X-Frame-Options: DENY` e referrer same-origin. `python manage.py check --deploy` passa limpo em produção.
- Toda consulta é limitada ao usuário logado, e objetos de outros usuários respondem 404. Toda rota que altera dados exige
  POST ou DELETE com proteção CSRF (ou uma chave da API); sair é só por POST.
- As chaves da API ficam guardadas só como hash; a chave completa aparece uma única vez. Só HTTPS em produção.
- Os dados do usuário são guardados como vieram e escapados nos templates. O JavaScript monta a página com a API do DOM,
  nunca com strings de HTML.
- Content Security Policy rígida (`default-src 'self'`, sem script ou estilo embutido). O admin recebe a mesma política mais
  estilos embutidos, que ele ainda precisa.
- O bloqueio de login e o limite de cadastros usam o cache. O cache padrão é por processo; com vários workers, use um
  cache compartilhado.
- A exportação CSV prefixa células que começam com `=`, `+`, `-` ou `@`, para as planilhas não executarem como fórmula.
- Com `DEBUG=0` o Django não serve os arquivos estáticos: rode `python manage.py collectstatic` (o `whitenoise` serve a
  pasta `staticfiles/`).

## Problemas comuns

**`OperationalError: no such table` ao abrir o site**
O banco ainda não foi migrado, o que acontece num clone novo. Rode:

```bash
python manage.py migrate
```

e inicie o servidor de novo. O app detecta isso e mostra uma página "Banco de dados não configurado" (HTTP 503) com a
mesma instrução, e `python manage.py check` mostra um aviso (`app.W001`).

**`SECRET_KEY is not set`** Copie o `.env.example` para `.env` e preencha `SECRET_KEY`, ou rode `scripts/bootstrap.sh`.

**Python 3.9 e Django 4.1** Uma instalação antiga do Django 4.1 não funciona. Rode `pip install -r requirements.txt` para
ter o Django 4.2 ou mais novo.

**Páginas sem estilo com `DEBUG=0`** Rode `python manage.py collectstatic`.

## Estrutura do projeto

```
manage.py
expensemanager/      configurações, leitor do .env, middlewares de segurança, URLs, servidor local do app Android
app/
  models.py          contas, categorias, transações, orçamentos, metas, recorrências, pessoas, faturas ...
  services.py        saldos, política de saldo negativo, travas, registro de alterações, recorrências
  planning.py        planejamento do mês, faturas, fechamento, limite com parcelas
  cards.py           importação de faturas de cartão       quick.py      lançamento rápido
  budgeting.py       orçamentos, metas, alertas            insights.py   observações em linguagem simples
  reports.py         relatórios                            backup.py     backup e restauração em JSON
  api.py             rotas JSON                            views.py      páginas HTML
  sync/              uid, versão e registro de mudanças; servidor e cliente da sincronização
  api_auth.py tokens.py api_docs.py urls_api_v1.py         a API /api/v1/
  concurrency.py     conflitos de edição (HTTP 409)        drive_backup.py   backup diário no Google Drive
  localapp.py        regras do modo aplicativo (usuário único no celular)
  csvio.py nlq.py filters.py forms.py ratelimit.py
  templates/ static/app/{css,js,icons,api}
  management/commands/   apply_recurring, seed_demo, backup_drive
  tests/
scripts/             bootstrap.sh, montar_android.py, montar_discloud.py, gerar_openapi.py,
                     autorizar_google_drive.py, check_hygiene.sh, capture_screenshots.py, build_icons.py
docs/screenshots/
```

## Testes

```bash
python manage.py test
```

São mais de 470 testes: modelos, serviços, formulários, API e chaves de acesso, páginas, orçamentos e metas,
recorrências, cartões e faturas, importação e exportação, relatórios, backup e restauração, conflitos de edição,
sincronização (servidor, cliente e ponta a ponta com o celular em outro processo), backup no Google Drive (sem acessar a
internet), segurança (IDOR em todos os objetos, CSRF, métodos, XSS, injeção em CSV, limites de tentativas), migração de
bancos antigos e um limite de tamanho para CSS e JavaScript. O CI roda no Python 3.9 a 3.13 com Django 4.2 e 5.2
(`.github/workflows/ci.yml`).

## Regerar as capturas de tela

```bash
python scripts/capture_screenshots.py            # precisa do Chromium ou do Chrome
python scripts/capture_screenshots.py --only dashboard,budgets
```

O script inicia o app numa porta livre com um banco temporário e dados de demonstração, entra definindo um cookie de
sessão pelo protocolo DevTools (o app não tem porta dos fundos de login) e grava os PNG em `docs/screenshots/`.

## Créditos e licença

Projeto derivado do [Finance Manager](https://github.com/himaldahal/finance-manager) de Himal Dahal.

Ícones do [Lucide](https://lucide.dev) (licença ISC, cópia em `app/static/app/icons/LICENSE`), embutidos num sprite
próprio gerado por `scripts/build_icons.py`.

Distribuído sob a licença MIT. Veja o [LICENSE](LICENSE). Copyright (c) Himal Dahal.
