#!/usr/bin/env bash
#
# kiki 本地部署脚本
#   1) 同步远端（git fetch + fast-forward，只前进不合并分叉）
#   2) 后端代码 / 配置（.env、requirements）有变化 -> supervisorctl restart kiki-backend + 健康检查
#   3) 前端代码有变化 -> npm run build，产物 frontend/dist 由 nginx 直接提供
#
# 用法:
#   scripts/deploy.sh                   # 同步并部署有变化的部分
#   scripts/deploy.sh --dry-run         # 只打印计划，不动任何东西
#   scripts/deploy.sh --offline         # 不联网，按本地已有的 origin/main 部署
#   scripts/deploy.sh --force-backend   # 强制重启后端
#   scripts/deploy.sh --force-frontend  # 强制重新编译前端
#   scripts/deploy.sh --no-backend      # 本次只处理前端
#
# 状态文件: ~/.local/state/kiki/deploy-state  （后端/前端各自部署到哪个 commit + .env 指纹）
# 日志文件: ~/.local/state/kiki/deploy.log
#
# 可用环境变量覆盖: KIKI_BRANCH / KIKI_BACKEND_PROGRAM / KIKI_HEALTH_URL /
#                  KIKI_STATE_DIR / KIKI_NODE_BIN / KIKI_SUPERVISOR_CONF

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRANCH="${KIKI_BRANCH:-main}"
BACKEND_PROGRAM="${KIKI_BACKEND_PROGRAM:-kiki-backend}"
HEALTH_URL="${KIKI_HEALTH_URL:-http://127.0.0.1:8000/api/health}"
STATE_DIR="${KIKI_STATE_DIR:-$HOME/.local/state/kiki}"
STATE_FILE="$STATE_DIR/deploy-state"
LOG_FILE="$STATE_DIR/deploy.log"
LOCK_DIR="$STATE_DIR/.lock"
ENV_FILE="$ROOT/.env"

DRY_RUN=0
OFFLINE=0
DO_BACKEND=1
DO_FRONTEND=1
FORCE_BACKEND=0
FORCE_FRONTEND=0
SKIP_DEPS=0
LOCK_HELD=0

# ---------------------------------------------------------------- 基础输出

log()  { local m="[$(date '+%H:%M:%S')] $*"; printf '%s\n' "$m"; printf '%s\n' "$m" >> "$LOG_FILE"; }
warn() { local m="[$(date '+%H:%M:%S')] WARN  $*"; printf '%s\n' "$m" >&2; printf '%s\n' "$m" >> "$LOG_FILE"; }
die()  { local m="[$(date '+%H:%M:%S')] ERROR $*"; printf '%s\n' "$m" >&2; printf '%s\n' "$m" >> "$LOG_FILE"; exit 1; }

usage() { sed -n '3,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while (( $# > 0 )); do
  case "$1" in
    -n|--dry-run)       DRY_RUN=1 ;;
    --offline)          OFFLINE=1 ;;
    --no-backend)       DO_BACKEND=0 ;;
    --no-frontend)      DO_FRONTEND=0 ;;
    --force-backend)    FORCE_BACKEND=1 ;;
    --force-frontend)   FORCE_FRONTEND=1 ;;
    --skip-deps)        SKIP_DEPS=1 ;;
    -h|--help)          usage; exit 0 ;;
    *)                  die "未知参数: $1（--help 查看用法）" ;;
  esac
  shift
done

# ---------------------------------------------------------------- 环境检查

mkdir -p "$STATE_DIR"
touch "$LOG_FILE"

cd "$ROOT"
git rev-parse --git-dir >/dev/null 2>&1 || die "$ROOT 不是 git 仓库"

if [[ -x "$ROOT/.venv/bin/supervisorctl" ]]; then
  SUPERVISORCTL="$ROOT/.venv/bin/supervisorctl"
elif command -v supervisorctl >/dev/null 2>&1; then
  SUPERVISORCTL="$(command -v supervisorctl)"
else
  die "找不到 supervisorctl"
fi
SUP_CONF="${KIKI_SUPERVISOR_CONF:-$HOME/.config/supervisor/supervisord.conf}"
[[ -f "$SUP_CONF" ]] || die "找不到 supervisor 配置: $SUP_CONF"

# ---------------------------------------------------------------- 工具函数

acquire_lock() {
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    printf '%s\n' "$$" > "$LOCK_DIR/pid"
    LOCK_HELD=1
    return 0
  fi
  local pid
  pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    die "已有部署任务在运行 (pid $pid)"
  fi
  log "清理残留锁"
  rm -rf "$LOCK_DIR"
  mkdir "$LOCK_DIR"
  printf '%s\n' "$$" > "$LOCK_DIR/pid"
  LOCK_HELD=1
}

release_lock() { (( LOCK_HELD )) && rm -rf "$LOCK_DIR" || true; }
trap release_lock EXIT

state_get() {
  [[ -f "$STATE_FILE" ]] || return 0
  sed -n "s/^$1=//p" "$STATE_FILE" | tail -n 1
}

state_put() { # $1=backend_commit $2=frontend_commit $3=env_sha
  {
    printf 'BACKEND_COMMIT=%s\n'  "$1"
    printf 'FRONTEND_COMMIT=%s\n' "$2"
    printf 'ENV_SHA=%s\n'         "$3"
    printf 'UPDATED_AT=%s\n'      "$(date '+%Y-%m-%d %H:%M:%S')"
  } > "$STATE_FILE"
}

env_sha() {
  if [[ -f "$ENV_FILE" ]]; then
    shasum -a 256 "$ENV_FILE" | awk '{print $1}'
  else
    printf 'none'
  fi
}

# 校验历史 commit 还在仓库里，返回 commit 或空
resolve_base() {
  local c="${1:-}"
  [[ -n "$c" ]] || return 0
  if git rev-parse --verify --quiet "$c^{commit}" >/dev/null 2>&1; then
    printf '%s\n' "$c"
  fi
}

is_ancestor() { git merge-base --is-ancestor "$1" "$2" 2>/dev/null; }

diff_paths() { # $1=old $2=new $3..=pathspec
  local old="$1" new="$2"; shift 2
  git diff --name-only "$old" "$new" -- "$@"
}

describe_files() {
  local files="$1" total first
  if [[ -z "$files" ]]; then printf '无'; return; fi
  total="$(printf '%s\n' "$files" | wc -l | tr -d ' ')"
  first="$(printf '%s\n' "$files" | head -n 3 | tr '\n' ' ')"
  if (( total > 3 )); then
    printf '%s 等 %d 个文件' "$first" "$total"
  else
    printf '%s' "$first"
  fi
}

has_path() { # $1=文件列表 $2=精确路径
  [[ -n "$1" ]] && printf '%s\n' "$1" | grep -qx -- "$2"
  return 1
}

find_node() {
  if [[ -n "${KIKI_NODE_BIN:-}" && -x "$KIKI_NODE_BIN" ]]; then printf '%s\n' "$KIKI_NODE_BIN"; return 0; fi
  if command -v node >/dev/null 2>&1; then command -v node; return 0; fi
  local c
  for c in /opt/homebrew/bin/node /usr/local/bin/node "$HOME/.volta/bin/node"; do
    [[ -x "$c" ]] && { printf '%s\n' "$c"; return 0; }
  done
  for c in "$HOME"/.nvm/versions/node/*/bin/node "$HOME"/.local/share/fnm/node-versions/*/installation/bin/node; do
    [[ -x "$c" ]] && { printf '%s\n' "$c"; return 0; }
  done
  return 1
}

wait_health() {
  local tries="${1:-30}" i
  for (( i = 1; i <= tries; i++ )); do
    if curl -fsS -m 2 "$HEALTH_URL" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  return 1
}

restart_backend() {
  log "重启后端: $BACKEND_PROGRAM  ($1)"
  if (( DRY_RUN )); then
    log "would run: $SUPERVISORCTL -c $SUP_CONF restart $BACKEND_PROGRAM"
    return 0
  fi
  "$SUPERVISORCTL" -c "$SUP_CONF" restart "$BACKEND_PROGRAM" 2>&1 | tee -a "$LOG_FILE" \
    || die "supervisorctl restart $BACKEND_PROGRAM 失败"
  if wait_health 30; then
    log "后端健康检查通过: $HEALTH_URL"
  else
    warn "后端健康检查失败，最近错误日志:"
    tail -n 20 "$HOME/Library/Logs/kiki/kiki-backend.err.log" 2>/dev/null | tee -a "$LOG_FILE" >&2 || true
    die "后端未能在 30s 内恢复，已保留状态待下次重试"
  fi
}

build_frontend() {
  log "编译前端: $1"
  if (( DRY_RUN )); then
    log "would run: cd frontend && npm run build"
    return 0
  fi
  ( cd "$ROOT/frontend" && npm run build ) 2>&1 | tee -a "$LOG_FILE" \
    || die "前端编译失败，dist 保持上一次可用版本"
  [[ -f "$ROOT/frontend/dist/index.html" ]] || die "编译结束但 frontend/dist/index.html 不存在"
  log "前端产物已更新: frontend/dist ($(stat -f '%Sm' -t '%Y-%m-%d %H:%M:%S' "$ROOT/frontend/dist/index.html"))"
}

# ---------------------------------------------------------------- 0. 加锁

if NODE_BIN="$(find_node)"; then
  PATH="$(dirname "$NODE_BIN"):$PATH"
  export PATH
else
  warn "没找到 node，前端编译会失败（可用 KIKI_NODE_BIN=/path/to/node 指定）"
fi

acquire_lock

# ---------------------------------------------------------------- 1. 同步远端

HEAD_BEFORE="$(git rev-parse HEAD)"

dirty="$(git status --porcelain --untracked-files=no || true)"
if [[ -n "$dirty" ]]; then
  warn "工作区有未提交改动（远端若改到同一文件，fast-forward 会失败）:"
  printf '%s\n' "$dirty" | sed 's/^/        /' | tee -a "$LOG_FILE" >&2
  warn "建议先 git stash；脚本先继续，冲突时再处理"
fi

if (( OFFLINE )); then
  log "offline 模式，跳过 fetch"
else
  log "同步远端: origin/$BRANCH"
  git fetch --prune origin 2>&1 | tee -a "$LOG_FILE" >/dev/null \
    || die "git fetch 失败（网络问题可加 --offline 跳过）"
fi

TARGET_REF="origin/$BRANCH"
git rev-parse --verify --quiet "$TARGET_REF" >/dev/null || die "找不到远端分支 $TARGET_REF"
TARGET_SHA="$(git rev-parse "$TARGET_REF")"

if [[ "$TARGET_SHA" == "$HEAD_BEFORE" ]]; then
  log "已是最新提交 ${HEAD_BEFORE:0:7}"
elif is_ancestor "$TARGET_SHA" "$HEAD_BEFORE"; then
  log "本地领先远端 $(git rev-list --count "$TARGET_SHA..$HEAD_BEFORE") 个提交，不拉取"
elif is_ancestor "$HEAD_BEFORE" "$TARGET_SHA"; then
  log "远端新增 $(git rev-list --count "$HEAD_BEFORE..$TARGET_SHA") 个提交: ${HEAD_BEFORE:0:7} -> ${TARGET_SHA:0:7}"
  if (( DRY_RUN )); then
    log "would run: git merge --ff-only $TARGET_REF"
  else
    git merge --ff-only "$TARGET_REF" 2>&1 | tee -a "$LOG_FILE" \
      || die "fast-forward 失败（工作区冲突? 先 git stash 后重试）"
  fi
else
  die "本地 $BRANCH 与 $TARGET_REF 已分叉，请手工处理后重跑"
fi

if (( DRY_RUN )); then NEW_SHA="$TARGET_SHA"; else NEW_SHA="$(git rev-parse HEAD)"; fi

# ---------------------------------------------------------------- 2. 变化判定

BE_BASE="$(resolve_base "$(state_get BACKEND_COMMIT)")"
FE_BASE="$(resolve_base "$(state_get FRONTEND_COMMIT)")"
OLD_ENV_SHA="$(state_get ENV_SHA)"
NEW_ENV_SHA="$(env_sha)"

# 状态里的 commit 必须仍是当前 commit 的祖先，否则视为“无法比较” -> 全量部署
if [[ -n "$BE_BASE" ]] && ! is_ancestor "$BE_BASE" "$NEW_SHA"; then
  warn "后端状态 commit 不是当前历史祖先，按全量处理"; BE_BASE=""
fi
if [[ -n "$FE_BASE" ]] && ! is_ancestor "$FE_BASE" "$NEW_SHA"; then
  warn "前端状态 commit 不是当前历史祖先，按全量处理"; FE_BASE=""
fi

BE_FILES=""
FE_FILES=""
# 后端只看运行时会加载的代码与依赖；纯测试改动不需要重启服务
[[ -n "$BE_BASE" ]] && BE_FILES="$(diff_paths "$BE_BASE" "$NEW_SHA" backend/app backend/requirements.txt)"
[[ -n "$FE_BASE" ]] && FE_FILES="$(diff_paths "$FE_BASE" "$NEW_SHA" frontend)"

BE_REASON=""
if (( DO_BACKEND )); then
  if (( FORCE_BACKEND )); then
    BE_REASON="--force-backend"
  elif [[ -z "$BE_BASE" ]]; then
    BE_REASON="首次部署（无状态记录）"
  elif [[ -n "$BE_FILES" ]]; then
    BE_REASON="$(describe_files "$BE_FILES")"
  fi
  if [[ -z "$BE_REASON" && "$NEW_ENV_SHA" != "$OLD_ENV_SHA" ]]; then
    BE_REASON=".env 配置变化"
  fi
fi

FE_REASON=""
if (( DO_FRONTEND )); then
  if (( FORCE_FRONTEND )); then
    FE_REASON="--force-frontend"
  elif [[ -z "$FE_BASE" ]]; then
    FE_REASON="首次部署（无状态记录）"
  elif [[ -n "$FE_FILES" ]]; then
    FE_REASON="$(describe_files "$FE_FILES")"
  fi
fi

log "变化概览: 后端=[${BE_REASON:-无变化}]  前端=[${FE_REASON:-无变化}]"

# ---------------------------------------------------------------- 3. 后端

BE_STATE="$(state_get BACKEND_COMMIT)"
if [[ -n "$BE_REASON" ]]; then
  if (( ! SKIP_DEPS )) && has_path "$BE_FILES" "backend/requirements.txt"; then
    log "后端依赖变化 -> pip install"
    if (( DRY_RUN )); then
      log "would run: .venv/bin/python -m pip install -r backend/requirements.txt"
    else
      "$ROOT/.venv/bin/python" -m pip install -q -r "$ROOT/backend/requirements.txt" 2>&1 | tee -a "$LOG_FILE" \
        || die "pip install 失败"
    fi
  fi
  restart_backend "$BE_REASON"
  BE_STATE="$NEW_SHA"
else
  log "后端无变化，跳过重启"
  # 状态未记录过时，补一个基线，避免下次误判全量
  [[ -z "$BE_STATE" ]] && BE_STATE="$NEW_SHA"
fi

# ---------------------------------------------------------------- 4. 前端

FE_STATE="$(state_get FRONTEND_COMMIT)"
if [[ -n "$FE_REASON" ]]; then
  if (( ! SKIP_DEPS )) && { has_path "$FE_FILES" "frontend/package.json" || has_path "$FE_FILES" "frontend/package-lock.json"; }; then
    log "前端依赖变化 -> npm install"
    if (( DRY_RUN )); then
      log "would run: cd frontend && npm install"
    else
      ( cd "$ROOT/frontend" && npm install --no-audit --no-fund ) 2>&1 | tee -a "$LOG_FILE" \
        || die "npm install 失败"
    fi
  fi
  build_frontend "$FE_REASON"
  FE_STATE="$NEW_SHA"
else
  log "前端无变化，跳过编译"
  [[ -z "$FE_STATE" ]] && FE_STATE="$NEW_SHA"
fi

# ---------------------------------------------------------------- 5. 记录状态

if (( DRY_RUN )); then
  log "dry-run 结束，未写入状态文件"
else
  state_put "${BE_STATE:-$NEW_SHA}" "${FE_STATE:-$NEW_SHA}" "$NEW_ENV_SHA"
  log "状态已记录: backend=${BE_STATE:-$NEW_SHA} frontend=${FE_STATE:-$NEW_SHA}"
fi

log "完成。nginx 直接读 frontend/dist，无需 reload；后端由 supervisor 托管。"
