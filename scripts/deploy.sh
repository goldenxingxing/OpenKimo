#!/usr/bin/env bash
# ============================================================
#  OpenKimo 一键部署脚本
#  - 检查 Docker / Docker Compose 环境
#  - 终端交互式向导生成 .env
#  - 构建镜像并启动容器
# ============================================================
set -euo pipefail

# ---------------- ANSI Colors ----------------
if [ -t 1 ]; then
  C_RESET='\033[0m'
  C_BOLD='\033[1m'
  C_DIM='\033[2m'
  C_RED='\033[31m'
  C_GREEN='\033[32m'
  C_YELLOW='\033[33m'
  C_BLUE='\033[34m'
  C_MAGENTA='\033[35m'
  C_CYAN='\033[36m'
else
  C_RESET=''; C_BOLD=''; C_DIM=''
  C_RED=''; C_GREEN=''; C_YELLOW=''
  C_BLUE=''; C_MAGENTA=''; C_CYAN=''
fi

# ---------------- Paths ----------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$PROJECT_ROOT/.env"
ENV_EXAMPLE="$PROJECT_ROOT/.env.example"
COMPOSE_FILE="$PROJECT_ROOT/docker-compose.yml"

cd "$PROJECT_ROOT"

# ---------------- UI Helpers ----------------
hr() { printf "${C_DIM}%s${C_RESET}\n" "------------------------------------------------------------"; }

banner() {
  printf "${C_CYAN}${C_BOLD}"
  cat <<'EOF'
   ___                  _  ___
  / _ \ _ __   ___ _ __| |/ (_)_ __ ___   ___
 | | | | '_ \ / _ \ '_ \ ' /| | '_ ` _ \ / _ \
 | |_| | |_) |  __/ | | . \ | | | | | | | (_) |
  \___/| .__/ \___|_| |_|_|\_\_|_| |_| |_|\___/
       |_|        One Docker. Zero Worries.
EOF
  printf "${C_RESET}\n"
}

section() {
  echo
  printf "${C_MAGENTA}${C_BOLD}▸ %s${C_RESET}\n" "$1"
  hr
}

info()    { printf "${C_BLUE}ℹ${C_RESET}  %s\n" "$*"; }
ok()      { printf "${C_GREEN}✔${C_RESET}  %s\n" "$*"; }
warn()    { printf "${C_YELLOW}⚠${C_RESET}  %s\n" "$*"; }
err()     { printf "${C_RED}✖${C_RESET}  %s\n" "$*" >&2; }

# Read with default value. Usage: ask VAR "Prompt" "default"
ask() {
  local __var="$1"; local __prompt="$2"; local __default="${3:-}"
  local __input
  if [ -n "$__default" ]; then
    printf "${C_BOLD}?${C_RESET} %s ${C_DIM}[%s]${C_RESET}: " "$__prompt" "$__default"
  else
    printf "${C_BOLD}?${C_RESET} %s: " "$__prompt"
  fi
  # 记下读到 EOF：管道里跑（CI、`yes | ./deploy.sh`）时 read 立刻失败，
  # 而校验循环会拿着同一个空值一遍遍重问 —— 无限循环，屏幕上只有同一行警告。
  if IFS= read -r __input; then
    ASK_EOF=0
  else
    ASK_EOF=1
  fi
  printf -v "$__var" "%s" "${__input:-$__default}"
}

# Abort a retry loop when there is no one left to answer it.
ask_eof_guard() {
  if [ "${ASK_EOF:-0}" = "1" ]; then
    err "输入已结束（非交互环境？），无法继续询问"
    exit 1
  fi
}

# Read secret (no echo). Usage: ask_secret VAR "Prompt"
ask_secret() {
  local __var="$1"; local __prompt="$2"
  local __input
  printf "${C_BOLD}?${C_RESET} %s ${C_DIM}(隐藏输入, 回车跳过)${C_RESET}: " "$__prompt"
  IFS= read -rs __input || true
  echo
  printf -v "$__var" "%s" "$__input"
}

# Yes/No prompt. Usage: ask_yn VAR "Prompt" "y|n"
ask_yn() {
  local __var="$1"; local __prompt="$2"; local __default="${3:-n}"
  local __hint __input
  case "$__default" in
    y|Y) __hint="Y/n" ;;
    *)   __hint="y/N" ;;
  esac
  while true; do
    printf "${C_BOLD}?${C_RESET} %s ${C_DIM}[%s]${C_RESET}: " "$__prompt" "$__hint"
    IFS= read -r __input || true
    __input="${__input:-$__default}"
    case "$__input" in
      y|Y|yes|YES) printf -v "$__var" "true";  return 0 ;;
      n|N|no|NO)   printf -v "$__var" "false"; return 0 ;;
      *) ask_eof_guard; warn "请输入 y 或 n" ;;
    esac
  done
}

# Single-choice menu. Usage: ask_choice VAR "Prompt" "opt1" "opt2" ...
ask_choice() {
  local __var="$1"; local __prompt="$2"; shift 2
  local __opts=("$@")
  local i __input
  echo
  printf "${C_BOLD}?${C_RESET} %s\n" "$__prompt"
  for i in "${!__opts[@]}"; do
    printf "    ${C_CYAN}%d)${C_RESET} %s\n" "$((i+1))" "${__opts[$i]}"
  done
  while true; do
    printf "  选择 ${C_DIM}[1-%d, 默认 1]${C_RESET}: " "${#__opts[@]}"
    IFS= read -r __input || true
    __input="${__input:-1}"
    if [[ "$__input" =~ ^[0-9]+$ ]] && [ "$__input" -ge 1 ] && [ "$__input" -le "${#__opts[@]}" ]; then
      printf -v "$__var" "%s" "${__opts[$((__input-1))]}"
      return 0
    fi
    ask_eof_guard
    warn "无效选项"
  done
}

# Read an integer within [min,max], re-prompting until it is one.
# Usage: ask_int VAR "Prompt" default min max
ask_int() {
  local __var="$1"; local __prompt="$2"; local __default="$3"
  local __min="$4"; local __max="$5"
  local __value
  while true; do
    ask __value "$__prompt" "$__default"
    if [[ "$__value" =~ ^[0-9]+$ ]] && [ "$((10#$__value))" -ge "$__min" ] && [ "$((10#$__value))" -le "$__max" ]; then
      printf -v "$__var" "%s" "$__value"
      return 0
    fi
    ask_eof_guard
    warn "请输入 $__min 到 $__max 之间的整数"
  done
}

# Read a non-negative decimal within [min,max], where min and max are whole
# numbers. Usage: ask_float VAR "Prompt" default min max
#
# Pure bash on purpose. Delegating the comparison to awk means a host without
# awk fails every value, including the default — the prompt then loops forever
# repeating a range the input already satisfies, with nothing on screen saying
# why.
ask_float() {
  local __var="$1"; local __prompt="$2"; local __default="$3"
  local __min="$4"; local __max="$5"
  local __value __int __frac
  while true; do
    ask __value "$__prompt" "$__default"
    if [[ "$__value" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
      __int="${__value%%.*}"
      __frac=""
      case "$__value" in *.*) __frac="${__value#*.}" ;; esac
      # At the top of the range the fraction has to be zero: 2.5 is not in
      # [0,2] even though its integer part is.
      if [ "$((10#$__int))" -ge "$__min" ] \
         && { [ "$((10#$__int))" -lt "$__max" ] \
              || { [ "$((10#$__int))" -eq "$__max" ] && [ -z "${__frac//0/}" ]; }; }; then
        printf -v "$__var" "%s" "$__value"
        return 0
      fi
    fi
    ask_eof_guard
    warn "请输入 $__min 到 $__max 之间的数值"
  done
}

#: Shortest bearer token the installer will accept when one is typed in.
#: The generated one is 64 hex characters; this is the floor, not the target.
MIN_TOKEN_LENGTH=16

random_token() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
  fi
}

# ---------------- Prerequisite Check ----------------
check_prereqs() {
  section "环境检测"
  local missing=0

  if command -v docker >/dev/null 2>&1; then
    ok "docker: $(docker --version)"
  else
    err "未找到 docker，请先安装 Docker Engine: https://docs.docker.com/get-docker/"
    missing=1
  fi

  if docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
    ok "compose: $(docker compose version --short 2>/dev/null || echo plugin)"
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_CMD="docker-compose"
    ok "compose: $(docker-compose version --short)"
  else
    err "未找到 docker compose，请安装: https://docs.docker.com/compose/install/"
    missing=1
  fi

  if command -v docker >/dev/null 2>&1 && ! docker info >/dev/null 2>&1; then
    err "Docker daemon 未运行，请启动 Docker Desktop / dockerd 后重试"
    missing=1
  fi

  [ "$missing" -eq 0 ] || exit 1
}

# ---------------- .env Onboarding ----------------
onboard_env() {
  section ".env 配置向导"

  if [ -f "$ENV_FILE" ]; then
    warn "已存在 .env 文件: $ENV_FILE"
    local _overwrite
    ask_yn _overwrite "是否覆盖现有配置？(选择 n 将保留现有 .env)" "n"
    if [ "$_overwrite" != "true" ]; then
      info "保留现有 .env，跳过向导"
      return 0
    fi
    cp "$ENV_FILE" "$ENV_FILE.bak.$(date +%Y%m%d%H%M%S)"
    ok "已备份原配置到 .env.bak.*"
  fi

  # ---- LLM Provider ----
  echo
  info "至少需要配置一个 LLM Provider 的 API Key"
  ask_choice LLM_PROVIDER "请选择默认 LLM Provider" \
    "openai" "anthropic"

  OPENAI_API_KEY=""; ANTHROPIC_API_KEY=""
  KIMO_MODEL_MAX_CONTEXT_SIZE="262144"
  OPENAI_BASE_URL="https://api.openai.com/v1"
  ANTHROPIC_BASE_URL="https://api.anthropic.com"

  echo
  info "现在录入 API Key（隐藏输入；非默认 provider 也可填，留空跳过）"
  case "$LLM_PROVIDER" in
    openai)
      ask_secret OPENAI_API_KEY    "OpenAI API Key (必填)"
      ask_secret ANTHROPIC_API_KEY "Anthropic API Key (可选)"
      ask OPENAI_BASE_URL "OpenAI Base URL" "$OPENAI_BASE_URL"
      ;;
    anthropic)
      ask_secret ANTHROPIC_API_KEY "Anthropic API Key (必填)"
      ask_secret OPENAI_API_KEY    "OpenAI API Key (可选)"
      ask ANTHROPIC_BASE_URL "Anthropic Base URL" "$ANTHROPIC_BASE_URL"
      ;;
  esac

  # Validate at least one key
  if [ -z "$OPENAI_API_KEY" ] && [ -z "$ANTHROPIC_API_KEY" ]; then
    err "至少需要配置一个 API Key"
    exit 1
  fi

  # 上面每个分支都把当前默认 provider 的 key 标成「必填」，却只检查了「至少
  # 有一个 key」：选 openai 作默认、只填了 anthropic 的 key，一样能过 —— 部署出来
  # 的服务每一次请求都会失败，而这里什么都没说。
  local _selected_key=""
  case "$LLM_PROVIDER" in
    openai)    _selected_key="$OPENAI_API_KEY" ;;
    anthropic) _selected_key="$ANTHROPIC_API_KEY" ;;
  esac
  if [ -z "$_selected_key" ]; then
    err "默认 provider 是 $LLM_PROVIDER，但它的 API Key 是空的"
    exit 1
  fi

  ask_float LLM_TEMPERATURE "采样温度 (0.0 - 2.0)" "0.7" 0 2
  local _thinking
  ask_yn _thinking "启用 thinking 模式？" "n"
  LLM_THINKING="$_thinking"

  # ---- Web Server ----
  section "Web 服务配置"
  ask_int KIMO_WEB_PORT "Web UI 监听端口" "5494" 1 65535

  local _gen_token
  ask_yn _gen_token "自动生成强随机 Session Token？(推荐)" "y"
  if [ "$_gen_token" = "true" ]; then
    KIMO_WEB_SESSION_TOKEN="$(random_token)"
    ok "已生成 Token: ${KIMO_WEB_SESSION_TOKEN:0:12}…"
  else
    # 不接受空值，不接受 change-me（.env.example 里就写着的公开占位符），
    # 也不接受短到可以猜的。摘要里只显示前 12 位，运维会以为自己设过了
    # Token，而对外暴露的主机上它可能是直接能猜出来的。
    while true; do
      ask KIMO_WEB_SESSION_TOKEN "自定义 Session Token (至少 ${MIN_TOKEN_LENGTH} 位)" ""
      ask_eof_guard
      case "$KIMO_WEB_SESSION_TOKEN" in
        "")        warn "Token 不能为空" ;;
        change-me) warn "change-me 是公开的占位符，请换一个" ;;
        # 粘贴时捎上的空格不会被悄悄吃掉：它是 Token 的一部分，之后每次
        # 请求都得一模一样地带着，而复制过去的那一份多半不带。
        *[[:space:]]*) warn "Token 不能包含空白字符" ;;
        *)
          if [ "${#KIMO_WEB_SESSION_TOKEN}" -lt "$MIN_TOKEN_LENGTH" ]; then
            warn "Token 太短了（至少 ${MIN_TOKEN_LENGTH} 位），或者上一步选 y 自动生成"
          # 长度挡不住 "aaaaaaaaaaaaaaaa"：够长，也一样是猜得到的。
          # 引号不能省：${var//pattern/} 里的 pattern 是 glob，首字符是 `*`
          # 的 Token 会把自己整个匹配掉，然后被当成「同一个字符重复」拒掉。
          elif [ -z "${KIMO_WEB_SESSION_TOKEN//"${KIMO_WEB_SESSION_TOKEN:0:1}"/}" ]; then
            warn "Token 不能是同一个字符重复，建议上一步选 y 自动生成"
          else
            break
          fi
          ;;
      esac
    done
  fi

  # The initial admin account is created on first boot. A published
  # default password ("admin123") on a privileged account is a credential
  # everyone already knows, so the deployer picks or generates one here and
  # it lands in .env for the first boot to pick up.
  local _gen_admin
  ask_yn _gen_admin "自动生成强随机管理员初始口令？(推荐)" "y"
  if [ "$_gen_admin" = "true" ]; then
    KIMO_INITIAL_ADMIN_PASSWORD="$(random_token)"
    ok "已生成管理员初始口令: ${KIMO_INITIAL_ADMIN_PASSWORD:0:12}…"
  else
    while true; do
      ask KIMO_INITIAL_ADMIN_PASSWORD "自定义管理员初始口令 (至少 12 位)" ""
      ask_eof_guard
      case "$KIMO_INITIAL_ADMIN_PASSWORD" in
        "")        warn "口令不能为空" ;;
        admin123)  warn "admin123 是公开的默认口令，请换一个" ;;
        *)
          if [ "${#KIMO_INITIAL_ADMIN_PASSWORD}" -lt 12 ]; then
            warn "口令太短了（至少 12 位），或者上一步选 y 自动生成"
          else
            break
          fi
          ;;
      esac
    done
  fi

  local _lan_only
  ask_yn _lan_only "仅允许局域网访问？(对外暴露选 n)" "n"
  KIMO_WEB_LAN_ONLY="$_lan_only"

  local _restrict
  ask_yn _restrict "启用敏感 API 限制？(生产环境推荐)" "n"
  KIMO_WEB_RESTRICT_SENSITIVE_APIS="$_restrict"

  # ---- Sandbox Resources ----
  section "Sandbox 资源限制 (回车使用默认)"
  # 只问真正会被执行的三项。SANDBOX_DISK_LIMIT / MAX_SESSION_CONTAINERS /
  # SANDBOX_TIMEOUT_SECONDS 在 kimo-cli/src、kimo-cli/packages、app/ 和
  # packaging/ 里都没有任何消费者（container.py 只施加 --cpus / --memory /
  # --pids-limit，既没有容器数量上限，也没有 --storage-opt，更没有闲置回收）。
  # 问了却不生效比不问更糟：运维会以为自己设了 50 个容器的上限和 1 小时的
  # 空闲回收，然后被 200 个永不回收的容器把宿主机内存吃光。
  ask SANDBOX_CPU_LIMIT       "每个 session 容器的 CPU 核数" "2"
  ask SANDBOX_MEMORY_LIMIT    "每个 session 容器的内存限制" "4g"
  ask SANDBOX_PID_LIMIT       "每个 session 容器的进程数上限" "1000"

  # ---- Optional Mounts ----
  section "可选挂载 (留空跳过)"
  ask CUSTOM_SKILLS_HOST_PATH "自定义 Skill 目录绝对路径" ""
  ask HF_CACHE_HOST_PATH      "HuggingFace 缓存目录绝对路径" ""
  ask KIMO_SESSION_DATA_DIR   "Session 数据持久化目录" "$PROJECT_ROOT/data/sessions"

# Single-quote a value for a .env file that is BOTH `source`d by
# scripts/start.sh and parsed by `docker compose --env-file`. Both understand
# single quotes; neither understands a bare value containing a space.
#
# Unquoted, a checkout under "~/My Projects/OpenKimo" wrote
#   KIMO_SESSION_DATA_DIR=/Users/x/My Projects/OpenKimo/data/sessions
# and start.sh's `source` parsed that as an assignment plus the command
# `Projects/OpenKimo/data/sessions`, dying under `set -e` before starting
# anything.
envq() {
  # Compose's dotenv parser does not interpret backslash escapes, so split a
  # single quote into adjacent quoted segments ('"'"') that both bash and
  # compose concatenate back into a literal "'".
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\"'\"'/g")"
}

  # ---- Write .env ----
  section "写入 .env"
  cat > "$ENV_FILE" <<EOF
# Generated by scripts/deploy.sh on $(date '+%Y-%m-%d %H:%M:%S')
# 重新运行 ./scripts/deploy.sh 可重新生成

# ---------------- LLM Providers ----------------
LLM_PROVIDER=$(envq "$LLM_PROVIDER")
LLM_THINKING=$(envq "$LLM_THINKING")
LLM_TEMPERATURE=$(envq "$LLM_TEMPERATURE")

KIMO_MODEL_MAX_CONTEXT_SIZE=$(envq "$KIMO_MODEL_MAX_CONTEXT_SIZE")

OPENAI_API_KEY=$(envq "$OPENAI_API_KEY")
OPENAI_BASE_URL=$(envq "$OPENAI_BASE_URL")

ANTHROPIC_API_KEY=$(envq "$ANTHROPIC_API_KEY")
ANTHROPIC_BASE_URL=$(envq "$ANTHROPIC_BASE_URL")

# ---------------- Web Server ----------------
KIMO_WEB_PORT=$(envq "$KIMO_WEB_PORT")
KIMO_WEB_SESSION_TOKEN=$(envq "$KIMO_WEB_SESSION_TOKEN")
KIMO_INITIAL_ADMIN_PASSWORD=$(envq "$KIMO_INITIAL_ADMIN_PASSWORD")
KIMO_WEB_ALLOWED_ORIGINS=*
KIMO_WEB_ENFORCE_ORIGIN=false
KIMO_WEB_RESTRICT_SENSITIVE_APIS=$(envq "$KIMO_WEB_RESTRICT_SENSITIVE_APIS")
KIMO_WEB_MAX_PUBLIC_PATH_DEPTH=6
KIMO_WEB_LAN_ONLY=$(envq "$KIMO_WEB_LAN_ONLY")

# ---------------- Sandbox ----------------
SANDBOX_IMAGE=ghcr.io/goldenxingxing/kimo-agent-sandbox:latest
SANDBOX_WORK_DIR=/app
SANDBOX_CPU_LIMIT=$(envq "$SANDBOX_CPU_LIMIT")
SANDBOX_MEMORY_LIMIT=$(envq "$SANDBOX_MEMORY_LIMIT")
SANDBOX_PID_LIMIT=$(envq "$SANDBOX_PID_LIMIT")

# ---------------- Feature Flags ----------------
ENABLE_BROWSER=true
ENABLE_JUPYTER=true
ENABLE_SHELL_SANDBOX=true
BLOCK_DANGEROUS_COMMANDS=true

# ---------------- Optional Mounts ----------------
CUSTOM_SKILLS_HOST_PATH=$(envq "$CUSTOM_SKILLS_HOST_PATH")
HF_CACHE_HOST_PATH=$(envq "$HF_CACHE_HOST_PATH")
KIMO_SESSION_DATA_DIR=$(envq "$KIMO_SESSION_DATA_DIR")
EOF
  chmod 600 "$ENV_FILE"
  ok "已写入 $ENV_FILE (权限 600)"

  # Ensure session data dir exists
  if [ -n "${KIMO_SESSION_DATA_DIR:-}" ]; then
    mkdir -p "$KIMO_SESSION_DATA_DIR"
    ok "已创建 session 数据目录: $KIMO_SESSION_DATA_DIR"
  fi
}

# ---------------- Build & Up ----------------
build_and_up() {
  section "获取镜像与启动"

  local _use_prebuilt _up
  ask_yn _use_prebuilt "使用预构建镜像（从 ghcr.io 拉取，无需本地编译）？ 选 N 则本地构建" "y"

  if [ "$_use_prebuilt" = "true" ]; then
    info "拉取预构建镜像…"
    $COMPOSE_CMD --env-file "$ENV_FILE" -f "$COMPOSE_FILE" pull
    ok "镜像拉取完成"
  else
    info "开始本地构建镜像（首次构建可能需要较长时间）…"
    $COMPOSE_CMD --env-file "$ENV_FILE" -f "$COMPOSE_FILE" build
    ok "镜像构建完成"
  fi

  ask_yn _up "现在执行 $COMPOSE_CMD up -d？" "y"
  if [ "$_up" = "true" ]; then
    $COMPOSE_CMD --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d
    ok "容器已启动"
  else
    info "跳过启动；稍后可手动执行: $COMPOSE_CMD up -d"
    return 0
  fi
}

# ---------------- Summary ----------------
print_summary() {
  local port token
  # -f2- keeps a value containing '=' (base64 padding is the common case);
  # `|| true` because a grep miss is exit 1 and, under `set -euo pipefail`,
  # that killed the script here — after `up -d` had already succeeded, so a
  # working deploy printed no URL and looked failed.
  # The tr strips the surrounding single quotes envq() writes.
  read_env_value() {
    grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2- | sed "s/^'//; s/'$//" || true
  }
  port="$(read_env_value KIMO_WEB_PORT)"
  token="$(read_env_value KIMO_WEB_SESSION_TOKEN)"

  section "部署完成 🎉"
  printf "  Web UI:    ${C_CYAN}http://localhost:%s${C_RESET}\n" "${port:-5494}"
  if [ -n "$token" ] && [ "$token" != "change-me" ]; then
    # Percent-encode: a custom token containing '&', '#', '=' or '%' would
    # otherwise truncate the URL or be read as extra query parameters.
    local _enc="" _c _i
    for ((_i = 0; _i < ${#token}; _i++)); do
      _c="${token:_i:1}"
      case "$_c" in
        [A-Za-z0-9._~-]) _enc+="$_c" ;;
        *) printf -v _c '%%%02X' "'$_c"; _enc+="$_c" ;;
      esac
    done
    printf "  带 Token:  ${C_CYAN}http://localhost:%s/?token=%s${C_RESET}\n" "${port:-5494}" "$_enc"
  fi
  echo
  local admin_password
  admin_password="$(read_env_value KIMO_INITIAL_ADMIN_PASSWORD)"
  printf "  初始管理员账号: ${C_BOLD}admin${C_RESET}"
  if [ -n "$admin_password" ]; then
    printf "  初始口令: ${C_BOLD}%s${C_RESET}" "$admin_password"
  else
    printf "  ${C_YELLOW}(未在 .env 中设置 KIMO_INITIAL_ADMIN_PASSWORD，首次启动时随机生成并打印)${C_RESET}"
  fi
  printf " ${C_YELLOW}(请尽快通过 /admin 修改)${C_RESET}\n"
  echo
  printf "  常用命令:\n"
  printf "    查看日志:   ${C_DIM}%s logs -f gateway${C_RESET}\n" "$COMPOSE_CMD"
  printf "    停止服务:   ${C_DIM}%s down${C_RESET}\n" "$COMPOSE_CMD"
  printf "    重新部署:   ${C_DIM}./scripts/deploy.sh${C_RESET}\n"
  echo
}

# ---------------- Main ----------------
main() {
  banner
  check_prereqs
  onboard_env
  build_and_up
  print_summary
}

main "$@"
