#!/bin/sh
# Render the configuration (fails closed on missing/unsafe settings), validate it, then run nginx
# in the foreground. SIGQUIT (STOPSIGNAL) = graceful shutdown: in-flight requests finish.
set -eu
python3 /opt/gateway/render_config.py --output "${GATEWAY_RUN_DIR}/nginx.conf"
nginx -e stderr -t -c "${GATEWAY_RUN_DIR}/nginx.conf" -g "daemon off;"
exec nginx -e stderr -c "${GATEWAY_RUN_DIR}/nginx.conf" -g "daemon off;"
