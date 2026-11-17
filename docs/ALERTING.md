# 邮件告警配置

本批提供 Prometheus → Alertmanager → 邮件的可选配置，复用现有 SMTP。默认生产部署保持原样，只有叠加 `docker-compose.alerts.yml` 才启用通知。配置生成和自动测试不发送邮件，也未对线上服务进行修改。

## 告警范围

| 告警 | 触发条件 |
| --- | --- |
| API 不可用 | 指标抓取失败持续 2 分钟 |
| API 错误率 | 5 分钟 HTTP 5xx 比例超过 5%，持续 5 分钟 |
| 任务积压 | 当前最老 queued 任务自创建以来等待超过 300 秒，持续 2 分钟；重试任务沿用原创建时间 |
| 任务失败 | 最近 15 分钟至少 5 条失败记录、失败率超过 20%，持续 5 分钟；分母为 succeeded + failed，不含取消 |

任务指标按数据库中仍保留的记录重新计算，是可下降的 Gauge；历史清理会缩小窗口样本，不能使用 `rate()` 或 `increase()` 当作累计失败事件。它们反映任务执行结果，不代表答案正确率。当前不包含 Worker/Scheduler 独立心跳、磁盘、备份失败和证书到期告警。

相同告警名称、严重程度会聚合：首次等待 30 秒，组更新间隔 5 分钟，持续故障每 4 小时重复；故障恢复也发送通知。通知仅使用规则名称、严重程度和固定摘要，不加入提示词、用户名或知识库正文。整台主机或网络离线时，同机监控无法发送邮件；这需要另行配置异机可用性监测。

## 私有配置

在已有 `.env.production` 中填写单个收件邮箱 `ALERT_EMAIL_TO`，并核对 `SMTP_HOST`、`SMTP_PORT`、`SMTP_TLS`、`SMTP_USERNAME`、`SMTP_PASSWORD`、`SMTP_SENDER`。本版只接受不带显示名称的单个收件邮箱。不要把地址、授权码或生成的私有文件提交 Git。

`SMTP_TLS=ssl` 使用隐式 TLS，`starttls` 使用 STARTTLS；端口沿用现有配置，默认 465。两种方式均要求 TLS 并验证服务器证书。本配置固定 Alertmanager 0.34.2，TLS 配置项见[官方说明](https://prometheus.io/docs/alerting/latest/configuration/#email_config)。

下面命令在承载生产环境的 Linux 主机、项目根目录执行。配置目录默认 `data/production`，若修改了 `PROD_DATA_ROOT`，同步替换命令中的路径。

```bash
# 只校验声明，不写文件、不启动服务、不发送通知
backend/.venv/bin/python scripts/configure-alerts.py --env-file .env.production --check
# 生成/更新两个告警专用文件，不修改其他生产配置
backend/.venv/bin/python scripts/configure-alerts.py --env-file .env.production
sudo chown 65534:65534 data/production/config/alertmanager.json
sudo chmod 600 data/production/config/alertmanager.json
```

`alertmanager.json` 含 SMTP 凭证，生成权限为 600；Alertmanager 以 UID/GID 65534 运行，Linux 上需要上面的属主设置。`prometheus-alerting.yaml` 不含凭证，保留已有抓取和规则配置，仅追加内部 Alertmanager 地址。每次修改基础 Prometheus 配置或邮件参数后，重新生成、设置属主并重建下面两个监控容器；单独 `restart` 不保证文件绑定指向新配置。

## 验证与启用

```bash
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.alerts.yml -p yugitalk-prod pull alertmanager
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.alerts.yml -p yugitalk-prod run --rm --no-deps --entrypoint /bin/amtool alertmanager check-config /etc/alertmanager/alertmanager.json
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.alerts.yml -p yugitalk-prod up -d --no-deps --force-recreate --wait alertmanager prometheus
```

这里只重建两个监控组件，不运行数据库迁移或重启业务服务。新增任务指标的 API 代码仍需随正常应用发布部署，否则对应规则没有数据，不能标记为已经生效。

Alertmanager 没有宿主端口；通过 private 网络接收告警，通过 default 网络访问外部 SMTP。通知分组、静默和去重状态保存在项目命名卷 `alertmanager_data`，保留 120 小时；现有业务冷备不包含该卷，恢复后可能重新通知。私有配置本身在原有 config 备份范围内。不要对运行中的服务使用 `down -v`。

固定镜像摘要便于复现，不代表漏洞扫描通过；上线前需把这个新增镜像纳入现有安全扫描流程。

以下是由部署者手动执行的真实收件验证，会向已配置邮箱发送一条不含业务数据的测试告警。先确认收件地址属于预期接收者，再执行：

```bash
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.alerts.yml -p yugitalk-prod exec alertmanager /bin/amtool --alertmanager.url=http://127.0.0.1:9093 alert add alertname=YugiTalkNotificationTest severity=warning --annotation=summary=邮件告警连通测试 --end="$(date -u -d '+3 minutes' +%Y-%m-%dT%H:%M:%SZ)"
```

确认收到触发邮件以及稍后的恢复邮件。此命令只验证 Alertmanager → SMTP → 邮箱；完整链路还需核对 Prometheus 的 Alertmanager 发现状态、实际规则状态和通知错误日志。SMTP 网络、凭证、垃圾邮件和供应商限流都需要实际环境验收，`amtool check-config` 仅验证格式。

后续所有针对该部署的 Compose 命令应保持这三个文件；现有升级脚本可用 `--override docker-compose.alerts.yml` 携带覆盖文件，其外部 S3 备份限制仍然适用。停用邮件时先用三个文件停止 `alertmanager`，再用原有两个生产文件重建 `prometheus`；私有文件和通知状态保留供后续启用。

## 离线回归

```bash
backend/.venv/bin/python -m pytest backend/tests/test_alerting.py backend/tests/test_observability.py -q
docker run --rm --network none --read-only --tmpfs /tmp --entrypoint /bin/promtool --mount type=bind,source="$PWD/deploy",target=/validation,readonly --workdir /validation prom/prometheus:v3.13.4 test rules alerts.test.yaml
```

规则测试覆盖持续时间、最小失败样本、20% 边界和恢复。上述测试不会发送通知，不代表线上已经启用邮件。
