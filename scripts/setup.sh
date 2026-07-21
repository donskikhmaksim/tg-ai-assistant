#!/bin/bash
# tg-ai-assistant («Большой Брат») — автоматическая установка своего бота.
# Разворачивает ТВОЙ личный экземпляр на Railway: свой бот, своя база, свои ключи.
# Совместим со старым bash 3.2 (macOS по умолчанию).
#
# Безопасно перезапускать: проект и сервис переиспользуются, а не плодятся.

set -e

# ── Парсинг аргументов ─────────────────────────────────────────────────────
# Личные секреты друга — НЕ общие. Их подставляет бот-онбординг в персональную
# команду (в самоуничтожающейся заметке).
BOT_TOKEN=""
ANTHROPIC_KEY=""
TIMEZONE="Europe/Moscow"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bot-token)     BOT_TOKEN="$2";     shift 2 ;;
    --anthropic-key) ANTHROPIC_KEY="$2"; shift 2 ;;
    --timezone)      TIMEZONE="$2";      shift 2 ;;
    *) shift ;;
  esac
done

if [[ -z "$BOT_TOKEN" || -z "$ANTHROPIC_KEY" ]]; then
  echo "❌ Скрипт должен быть запущен с ключами --bot-token и --anthropic-key"
  echo ""
  echo "   Перед запуском подготовь два своих секрета:"
  echo "   1. BOT_TOKEN — создай своего бота в @BotFather (/newbot), скопируй токен."
  echo "   2. ANTHROPIC_API_KEY — свой ключ на console.anthropic.com (биллинг на тебя)."
  echo ""
  echo "   Затем возьми персональную команду у того, кто прислал тебе инструкцию."
  exit 1
fi

PROJECT_NAME="tg-ai-assistant"
SERVICE="tg-ai-assistant"
REPO="donskikhmaksim/tg-ai-assistant"

# ── Цвета ──────────────────────────────────────────────────────────────────
BOLD="\033[1m"; GREEN="\033[0;32m"; YELLOW="\033[0;33m"; RED="\033[0;31m"; CYAN="\033[0;36m"; RESET="\033[0m"
step() { echo -e "\n${BOLD}${CYAN}▶ $1${RESET}"; }
ok()   { echo -e "${GREEN}✓ $1${RESET}"; }
ask()  { echo -e "${YELLOW}➜ $1${RESET}"; }
fail() {
  echo -e "${RED}✗ $1${RESET}" >&2
  if [[ -n "$2" && -f "$2" ]]; then
    echo "--- подробности ---" >&2; tail -20 "$2" >&2; echo "" >&2
    echo -e "${YELLOW}Полный лог сохранён в: $2${RESET}" >&2
    echo "Если пишешь тому, кто прислал скрипт — пришли этот файл целиком." >&2
  fi
  exit 1
}

LOG="$HOME/tg-ai-assistant-setup.log"; : > "$LOG"

clear 2>/dev/null || true
echo -e "${BOLD}╔══════════════════════════════════════════╗"
echo -e "║   Большой Брат (tg-ai-assistant) — установка ║"
echo -e "╚══════════════════════════════════════════╝${RESET}"
echo ""
echo "Скрипт задеплоит ТВОЙ личный экземпляр бота на Railway (бот + MongoDB)"
echo "и подключит его к твоему GitHub-исходнику. Займёт ~4-6 минут."

# ── Шаг 1: Railway CLI ─────────────────────────────────────────────────────
MIN_MAJOR=5
step "1/4  Проверяю Railway CLI"
if command -v railway &>/dev/null; then
  CURRENT_VERSION=$(railway --version 2>/dev/null | awk '{print $2}')
  MAJOR=$(echo "$CURRENT_VERSION" | cut -d. -f1)
  if [[ -z "$MAJOR" || "$MAJOR" -lt "$MIN_MAJOR" ]]; then
    echo -e "${YELLOW}⚠️  Старая версия Railway CLI ($CURRENT_VERSION). Обнови:${RESET}"
    echo "   brew upgrade railway   (или npm i -g @railway/cli@latest), затем запусти снова."
    fail "Нужен Railway CLI ${MIN_MAJOR}.x или новее." "$LOG"
  fi
else
  echo "Railway CLI не найден — устанавливаю..."
  if command -v brew &>/dev/null; then
    brew install railway >>"$LOG" 2>&1 || fail "Не смог установить Railway CLI через brew." "$LOG"
  elif command -v npm &>/dev/null; then
    npm install -g @railway/cli >>"$LOG" 2>&1 || fail "Не смог установить Railway CLI через npm." "$LOG"
  else
    curl -fsSL https://railway.app/install.sh | sh >>"$LOG" 2>&1 || fail "Не смог установить Railway CLI." "$LOG"
    export PATH="$HOME/.railway/bin:$PATH"
  fi
  command -v railway &>/dev/null || fail "Railway CLI не установился." "$LOG"
fi
ok "Railway CLI $(railway --version 2>&1 | head -1)"

# ── Шаг 2: Логин в Railway ─────────────────────────────────────────────────
step "2/4  Войди в Railway"
if railway whoami &>/dev/null; then
  ok "Уже авторизован в Railway ($(railway whoami 2>/dev/null | tail -1))"
else
  echo ""
  echo "Сейчас откроется браузер — войди в свой аккаунт Railway."
  echo "(Если аккаунта нет — создай на railway.app, это бесплатно)"
  echo ""
  if [[ -t 0 ]]; then ask "Нажми Enter чтобы открыть браузер..."; read -r; fi
  railway login || fail "Не удалось войти в Railway." "$LOG"
  ok "Авторизован в Railway"
fi

# ── Шаг 3: Деплой ───────────────────────────────────────────────────────────
step "3/4  Деплою бота (самая долгая часть, ~3-5 минут)"
WORK_DIR=$(mktemp -d); cd "$WORK_DIR"

# Переиспользуем проект, если он уже был создан прошлым запуском
# (grep/sed вместо python3 — его нет из коробки на свежих macOS).
EXISTING_PROJECT_ID=$(railway list --json 2>>"$LOG" \
  | grep -B1 "\"name\": *\"$PROJECT_NAME\"" \
  | grep '"id"' | head -1 \
  | sed -E 's/.*"id": *"([^"]+)".*/\1/' || true)

LINKED=false
if [[ -n "$EXISTING_PROJECT_ID" ]]; then
  echo "Нашёл существующий проект, переиспользую его..."
  if railway link --project "$EXISTING_PROJECT_ID" --environment production --json >>"$LOG" 2>&1; then
    LINKED=true
  else
    echo "  Похоже, этот проект уже удалён — создаю новый."
  fi
fi
if [[ "$LINKED" == false ]]; then
  echo "Создаю проект..."
  railway init --name "$PROJECT_NAME" --json >>"$LOG" 2>&1 || fail "Не смог создать проект на Railway." "$LOG"
fi

# MongoDB: добавляем только если ещё нет.
HAS_MONGO=$(railway service list --json 2>>"$LOG" | grep -c '"name": *"MongoDB"' || true)
if [[ "$HAS_MONGO" -eq 0 ]]; then
  echo "Добавляю базу MongoDB..."
  railway add --database mongo --json >>"$LOG" 2>&1 || fail "Не смог добавить MongoDB." "$LOG"
else
  echo "База MongoDB уже есть, пропускаю."
fi

redeploy_with_retry() {
  local attempt=0 max_attempts=24
  while true; do
    if railway redeploy --service "$SERVICE" --yes --json >>"$LOG" 2>&1; then return 0; fi
    attempt=$((attempt + 1))
    [[ $attempt -ge $max_attempts ]] && fail "Не получилось запустить сборку после $max_attempts попыток." "$LOG"
    sleep 10
  done
}

# Создаём сервис бота (если ещё нет).
ALREADY_EXISTS=$(railway service list --json 2>>"$LOG" | grep -c "\"name\": *\"$SERVICE\"" || true)
if [[ "$ALREADY_EXISTS" -eq 0 ]]; then
  echo "Создаю сервис бота..."
  STEP_OUT=$(mktemp)
  if ! railway add --service "$SERVICE" --json >"$STEP_OUT" 2>&1; then
    grep -qi "already exists" "$STEP_OUT" || { cat "$STEP_OUT" >>"$LOG"; rm -f "$STEP_OUT"; fail "Не смог создать сервис $SERVICE." "$LOG"; }
  fi
  cat "$STEP_OUT" >>"$LOG"; rm -f "$STEP_OUT"
else
  echo "Сервис уже существует, обновляю переменные и передеплою."
fi

# Подключаем источник ВСЕГДА (прошлая неудачная попытка могла оставить пустой сервис).
echo "Подключаю код из $REPO (автообновление при твоих пушах)..."
STEP_OUT=$(mktemp)
if ! railway service source connect --repo "$REPO" --branch main --service "$SERVICE" --json >"$STEP_OUT" 2>&1; then
  grep -qi "already" "$STEP_OUT" || { cat "$STEP_OUT" >>"$LOG"; rm -f "$STEP_OUT"; fail "Не смог подключить GitHub-репозиторий." "$LOG"; }
fi
cat "$STEP_OUT" >>"$LOG"; rm -f "$STEP_OUT"

# Домен нужен для WEBAPP_URL (кнопка Mini App), поэтому генерируем ДО переменных.
echo "Генерирую домен..."
DOMAIN_JSON=$(railway domain --service "$SERVICE" --json 2>>"$LOG") || fail "Не смог создать домен." "$LOG"
DOMAIN=$(echo "$DOMAIN_JSON" | grep -oE '[a-z0-9-]+\.up\.railway\.app' | head -1)
if [[ -z "$DOMAIN" ]]; then
  DOMAIN=$(railway domain list --service "$SERVICE" --json 2>>"$LOG" | grep -oE '[a-z0-9-]+\.up\.railway\.app' | head -1)
fi
[[ -z "$DOMAIN" ]] && fail "Не удалось получить домен сервиса." "$LOG"

echo "Задаю переменные окружения..."
railway variable set "BOT_TOKEN=$BOT_TOKEN" --service "$SERVICE" --skip-deploys --json >>"$LOG" 2>&1 \
  || fail "Не смог задать BOT_TOKEN." "$LOG"
railway variable set "ANTHROPIC_API_KEY=$ANTHROPIC_KEY" --service "$SERVICE" --skip-deploys --json >>"$LOG" 2>&1 \
  || fail "Не смог задать ANTHROPIC_API_KEY." "$LOG"
railway variable set "MONGO_URL=\${{MongoDB.MONGO_URL}}" --service "$SERVICE" --skip-deploys --json >>"$LOG" 2>&1 \
  || fail "Не смог задать MONGO_URL." "$LOG"
railway variable set "DEFAULT_TIMEZONE=$TIMEZONE" --service "$SERVICE" --skip-deploys --json >>"$LOG" 2>&1 \
  || fail "Не смог задать DEFAULT_TIMEZONE." "$LOG"
railway variable set "WEBAPP_URL=https://$DOMAIN" --service "$SERVICE" --skip-deploys --json >>"$LOG" 2>&1 \
  || fail "Не смог задать WEBAPP_URL." "$LOG"

echo "Запускаю сборку (может занять пару попыток, это нормально)..."
redeploy_with_retry

echo ""
echo "Жду, пока бот поднимется..."
waited=0
until curl -sf "https://$DOMAIN/health" &>/dev/null; do
  sleep 5; waited=$((waited + 5))
  [[ $waited -ge 300 ]] && fail "Бот не поднялся за 5 минут. Логи: railway logs --service $SERVICE" ""
done
cd /; rm -rf "$WORK_DIR"
ok "Бот запущен на https://$DOMAIN"

# ── Шаг 4: Ручные шаги (их нельзя автоматизировать) ────────────────────────
step "4/4  Два ручных шага в Telegram"
echo ""
echo -e "${BOLD}1. Подключи бота к своему Telegram (Business Mode):${RESET}"
echo "   Telegram → Настройки → Telegram Business → Чат-боты → выбери своего бота"
echo "   → дай права read/reply/manage, охват «все чаты». Так бот сможет читать твои ЛС."
echo "   (Событие подключения ловится только пока сервис запущен — он уже запущен.)"
echo ""
echo -e "${BOLD}2. Подключи свой TickTick:${RESET}"
echo "   Напиши боту в личку:  /connect https://<твой-ticktick-mcp>.up.railway.app/mcp/<секрет>"
echo "   ⚠️  Только СВОЙ ticktick-mcp — иначе задачи полетят в чужой аккаунт."
echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════╗"
echo -e "║   ✅  Бот развёрнут!                     ║"
echo -e "╚══════════════════════════════════════════╝${RESET}"
echo ""
echo -e "${BOLD}Проверка:${RESET} напиши боту /start — придёт приветствие и меню."
echo ""
echo -e "${YELLOW}Обновления прилетают сами:${RESET} сервис подключён к $REPO (ветка main),"
echo "Railway передеплоит при каждом апстрим-пуше. Твои данные/ключи это не трогает."
