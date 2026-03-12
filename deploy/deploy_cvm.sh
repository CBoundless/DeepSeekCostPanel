#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${VENV_DIR:-${PROJECT_DIR}/.venv}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
ENV_TARGET="${ENV_TARGET:-${PROJECT_DIR}/deploy/deepseek-autotrade.env}"
SERVICE_NAME="${SERVICE_NAME:-deepseek-autotrade}"
SERVICE_USER="${SERVICE_USER:-$(id -un)}"
INSTALL_SYSTEMD="${INSTALL_SYSTEMD:-0}"
START_NOW="${START_NOW:-0}"
RUN_VALIDATE="${RUN_VALIDATE:-0}"

log() {
  printf '[deploy] %s\n' "$*"
}

ensure_python() {
  if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
    echo "未找到 Python：${PYTHON_BIN}" >&2
    exit 1
  fi
}

create_venv() {
  log "创建虚拟环境：${VENV_DIR}"
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
  "${VENV_DIR}/bin/python" -m pip install --upgrade pip
  "${VENV_DIR}/bin/pip" install -r "${PROJECT_DIR}/requirements.txt"
}

prepare_runtime_files() {
  mkdir -p "${PROJECT_DIR}/logs"
  if [[ ! -f "${ENV_TARGET}" ]]; then
    cp "${PROJECT_DIR}/deploy/deepseek-autotrade.env.example" "${ENV_TARGET}"
    log "已创建环境变量模板：${ENV_TARGET}"
  else
    log "环境变量文件已存在：${ENV_TARGET}"
  fi
}

validate_runtime() {
  log "校验当前环境配置"
  "${VENV_DIR}/bin/python" "${PROJECT_DIR}/run_autotrade.py" --env-file "${ENV_TARGET}" --validate-only
}

install_systemd_service() {
  local tmp_file
  tmp_file="$(mktemp)"

  sed \
    -e "s|__PROJECT_DIR__|${PROJECT_DIR}|g" \
    -e "s|__ENV_FILE__|${ENV_TARGET}|g" \
    -e "s|__SERVICE_USER__|${SERVICE_USER}|g" \
    "${PROJECT_DIR}/deploy/deepseek-autotrade.service" > "${tmp_file}"

  log "安装 systemd 服务：${SERVICE_NAME}"
  sudo cp "${tmp_file}" "/etc/systemd/system/${SERVICE_NAME}.service"
  rm -f "${tmp_file}"

  sudo systemctl daemon-reload
  sudo systemctl enable "${SERVICE_NAME}"

  if [[ "${START_NOW}" == "1" ]]; then
    sudo systemctl restart "${SERVICE_NAME}"
    sudo systemctl status "${SERVICE_NAME}" --no-pager | cat
  else
    log "服务已安装但未启动。可执行：sudo systemctl start ${SERVICE_NAME}"
  fi
}

print_next_steps() {
  cat <<EOF

部署准备完成。

下一步：
1. 编辑环境变量文件：${ENV_TARGET}
2. 手工校验配置：RUN_VALIDATE=1 bash deploy/deploy_cvm.sh
3. 前台试跑：${VENV_DIR}/bin/python run_autotrade.py --env-file ${ENV_TARGET}
4. 如需 systemd：INSTALL_SYSTEMD=1 START_NOW=1 bash deploy/deploy_cvm.sh
5. 查看服务日志：journalctl -u ${SERVICE_NAME} -f
6. 确认日志包含版本标记：AUTOTRADE_RELEASE_TAG=risk-controls-reapply-20260312

EOF
}

main() {
  ensure_python
  create_venv
  prepare_runtime_files

  if [[ "${RUN_VALIDATE}" == "1" ]]; then
    validate_runtime
  else
    log "跳过配置校验；编辑好 ${ENV_TARGET} 后可用 RUN_VALIDATE=1 再跑一次"
  fi

  if [[ "${INSTALL_SYSTEMD}" == "1" ]]; then
    install_systemd_service
  else
    log "跳过 systemd 安装；如需安装可加 INSTALL_SYSTEMD=1"
  fi

  print_next_steps
}

main "$@"
