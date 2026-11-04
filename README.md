# Wodby services

This repository is the index of Wodby-managed service repositories for Wodby
2.0.

Services are reusable definitions for application runtimes, databases, search
engines, infrastructure components, and integrations.

Application services are assembled into
[stacks](https://github.com/wodby/stacks) and deployed as part of user
applications. Kubernetes system services are different: Wodby installs and
manages them as cluster infrastructure when required by the cluster provider
and configuration. They are not user-deployable catalog components.

- Service catalog: https://wodby.com/services
- Service docs: https://wodby.com/docs/2.0/services/
- Service template reference: https://wodby.com/docs/2.0/services/template/
- Service boilerplate: https://github.com/wodby/service

Wodby-maintained images use revision tags such as `11.4-r0`. Service option
versions continue to identify the application version; each option's `tag`
selects a published image revision. The updater preserves variants such as
PostGIS and checks that revision tags exist in both the image registry and Git.
Service releases and Helm chart versions continue to use semantic versions.

The Service Update workflow checks service repositories daily and after each
push to `master`, then emails a report of available updates. It releases
planned updates only when the `SERVICE_AUTO_RELEASE` repository variable is
`true` or a manual run enables **release**. A release commits the updates to
the service repository and pushes a new patch tag.

Repository discovery checks that every active public `service-*` repository is
listed below. A missing entry fails the workflow so it cannot silently lose
update coverage. External services have no managed software to update.

GitLab and GitLab Runner are planned and released as one batch. Filtering for
either service includes both. The updater reads both charts' application
versions, keeps their major/minor release line aligned, and verifies the exact
Alpine Runner image tag. Patch versions may differ between the two products.
It prepares both repositories before publishing either release. Minor and major
application upgrades require review of GitLab's
[upgrade path](https://docs.gitlab.com/update/upgrade_paths/).

Supabase updates use all eight component images from one published Wodby chart,
checked against that chart's exact upstream self-hosted release. The chart,
application option, component images, and any Studio bootstrap override advance
together. Updates that change the upstream database image, roll back an image, or
require upstream manual steps appear as review-only diffs. New upstream releases without
a compatible published chart are reported for review; individual components are
not advanced independently.

New application release lines are reported for manual review, using upstream
tags or published image versions when end-of-life data has no version source.
Missing end-of-life dates still require review; the updater does not invent them.

Each repository's branch and release tag are pushed atomically. GitHub cannot
publish multiple repositories atomically. If a later batch push fails, the
report preserves successful releases; rerun the workflow to plan the remaining
updates from the current repository state.

## Create a service

Use the [service boilerplate](https://github.com/wodby/service) when you want to
create a custom Git-backed service. It includes a valid `service.yml`, starter
build files, and README guidance.

For a single service repository, put `service.yml` at the repository root. For a
repository that contains multiple services, add an `index.yml` with service
directories:

```yaml
services:
- api
- worker
```

Before publishing a service for others to use, review:

- [Service template reference](https://wodby.com/docs/2.0/services/template/)
- [Service build configuration](https://wodby.com/docs/2.0/services/build/)
- [Service links](https://wodby.com/docs/2.0/services/links/)
- [Naming rules](https://wodby.com/docs/2.0/naming/)

## Managed services

### Application runtimes

| Service | Repository |
| --- | --- |
| PHP | [wodby/service-php](https://github.com/wodby/service-php) |
| Drupal PHP | [wodby/service-drupal-php](https://github.com/wodby/service-drupal-php) |
| WordPress PHP | [wodby/service-wordpress-php](https://github.com/wodby/service-wordpress-php) |
| Laravel PHP | [wodby/service-laravel-php](https://github.com/wodby/service-laravel-php) |
| Symfony PHP | [wodby/service-symfony-php](https://github.com/wodby/service-symfony-php) |
| Matomo PHP | [wodby/service-matomo](https://github.com/wodby/service-matomo) |
| Moodle PHP | [wodby/service-moodle](https://github.com/wodby/service-moodle) |
| Nextcloud | [wodby/service-nextcloud](https://github.com/wodby/service-nextcloud) |
| Odoo | [wodby/service-odoo](https://github.com/wodby/service-odoo) |
| Ghost | [wodby/service-ghost](https://github.com/wodby/service-ghost) |
| Python | [wodby/service-python](https://github.com/wodby/service-python) |
| Django | [wodby/service-django](https://github.com/wodby/service-django) |
| FastAPI | [wodby/service-fastapi](https://github.com/wodby/service-fastapi) |
| Flask | [wodby/service-flask](https://github.com/wodby/service-flask) |
| Ruby | [wodby/service-ruby](https://github.com/wodby/service-ruby) |
| Rails | [wodby/service-rails](https://github.com/wodby/service-rails) |
| Discourse | [wodby/service-discourse](https://github.com/wodby/service-discourse) |
| Go | [wodby/service-go](https://github.com/wodby/service-go) |
| Rust | [wodby/service-rust](https://github.com/wodby/service-rust) |
| Java | [wodby/service-java](https://github.com/wodby/service-java) |
| Spring Boot | [wodby/service-spring-boot](https://github.com/wodby/service-spring-boot) |
| Kotlin | [wodby/service-kotlin](https://github.com/wodby/service-kotlin) |
| .NET | [wodby/service-dotnet](https://github.com/wodby/service-dotnet) |
| ASP.NET Core | [wodby/service-aspnetcore](https://github.com/wodby/service-aspnetcore) |
| Node.js | [wodby/service-node](https://github.com/wodby/service-node) |
| Next.js | [wodby/service-nextjs](https://github.com/wodby/service-nextjs) |
| TanStack Start | [wodby/service-tanstack-start](https://github.com/wodby/service-tanstack-start) |
| Nuxt | [wodby/service-nuxt](https://github.com/wodby/service-nuxt) |
| SvelteKit | [wodby/service-sveltekit](https://github.com/wodby/service-sveltekit) |
| Astro | [wodby/service-astro](https://github.com/wodby/service-astro) |
| Strapi | [wodby/service-strapi](https://github.com/wodby/service-strapi) |
| Angular | [wodby/service-angular](https://github.com/wodby/service-angular) |
| React | [wodby/service-react](https://github.com/wodby/service-react) |
| Vue | [wodby/service-vue](https://github.com/wodby/service-vue) |
| Dagster | [wodby/service-dagster](https://github.com/wodby/service-dagster) |
| Chatwoot | [wodby/service-chatwoot](https://github.com/wodby/service-chatwoot) |
| Zammad | [wodby/service-zammad](https://github.com/wodby/service-zammad) |
| Mattermost | [wodby/service-mattermost](https://github.com/wodby/service-mattermost) |
| Metabase | [wodby/service-metabase](https://github.com/wodby/service-metabase) |
| n8n | [wodby/service-n8n](https://github.com/wodby/service-n8n) |
| Slack Inviter | [wodby/service-slack-inviter](https://github.com/wodby/service-slack-inviter) |

### Web and edge

| Service | Repository |
| --- | --- |
| Nginx | [wodby/service-nginx](https://github.com/wodby/service-nginx) |
| Nginx Proxy | [wodby/service-nginx-proxy](https://github.com/wodby/service-nginx-proxy) |
| Nginx for PHP | [wodby/service-php-nginx](https://github.com/wodby/service-php-nginx) |
| Nginx for Drupal | [wodby/service-drupal-nginx](https://github.com/wodby/service-drupal-nginx) |
| Nginx for WordPress | [wodby/service-wordpress-nginx](https://github.com/wodby/service-wordpress-nginx) |
| Nginx for Laravel | [wodby/service-laravel-nginx](https://github.com/wodby/service-laravel-nginx) |
| Nginx for Matomo | [wodby/service-matomo-nginx](https://github.com/wodby/service-matomo-nginx) |
| Nginx for Moodle | [wodby/service-moodle-nginx](https://github.com/wodby/service-moodle-nginx) |
| Nginx for Odoo | [wodby/service-odoo-nginx](https://github.com/wodby/service-odoo-nginx) |
| Apache HTTPD | [wodby/service-httpd](https://github.com/wodby/service-httpd) |
| Apache HTTPD for PHP | [wodby/service-php-httpd](https://github.com/wodby/service-php-httpd) |
| Vinyl | [wodby/service-vinyl](https://github.com/wodby/service-vinyl) |
| Vinyl for Drupal | [wodby/service-drupal-vinyl](https://github.com/wodby/service-drupal-vinyl) |
| Vinyl for WordPress | [wodby/service-wordpress-vinyl](https://github.com/wodby/service-wordpress-vinyl) |
| Varnish | [wodby/service-varnish](https://github.com/wodby/service-varnish) |
| Varnish for Drupal | [wodby/service-drupal-varnish](https://github.com/wodby/service-drupal-varnish) |
| Varnish for WordPress | [wodby/service-wordpress-varnish](https://github.com/wodby/service-wordpress-varnish) |

### Networking

| Service | Repository |
| --- | --- |
| Tailscale | [wodby/service-tailscale](https://github.com/wodby/service-tailscale) |
| NetBird | [wodby/service-netbird](https://github.com/wodby/service-netbird) |
| 3X UI | [wodby/service-3xui](https://github.com/wodby/service-3xui) |
| FRP Server | [wodby/service-frps](https://github.com/wodby/service-frps) |
| RustDesk Server | [wodby/service-rustdesk](https://github.com/wodby/service-rustdesk) |

### Backend platforms

| Service | Repository |
| --- | --- |
| GitLab Community Edition | [wodby/service-gitlab](https://github.com/wodby/service-gitlab) |
| GitLab Runner | [wodby/service-gitlab-runner](https://github.com/wodby/service-gitlab-runner) |
| Gitea | [wodby/service-gitea](https://github.com/wodby/service-gitea) |
| Forgejo | [wodby/service-forgejo](https://github.com/wodby/service-forgejo) |
| Supabase | [wodby/service-supabase](https://github.com/wodby/service-supabase) |

### Data, search, and messaging

| Service | Repository |
| --- | --- |
| MariaDB | [wodby/service-mariadb](https://github.com/wodby/service-mariadb) |
| MongoDB | [wodby/service-mongodb](https://github.com/wodby/service-mongodb) |
| MySQL | [wodby/service-mysql](https://github.com/wodby/service-mysql) |
| PostgreSQL | [wodby/service-postgres](https://github.com/wodby/service-postgres) |
| Supabase PostgreSQL | [wodby/service-supabase-postgres](https://github.com/wodby/service-supabase-postgres) |
| PostGIS | [wodby/service-postgis](https://github.com/wodby/service-postgis) |
| Cloud MySQL | [wodby/service-cloud-mysql](https://github.com/wodby/service-cloud-mysql) |
| Cloud MariaDB | [wodby/service-cloud-mariadb](https://github.com/wodby/service-cloud-mariadb) |
| Cloud PostgreSQL | [wodby/service-cloud-postgres](https://github.com/wodby/service-cloud-postgres) |
| Valkey | [wodby/service-valkey](https://github.com/wodby/service-valkey) |
| Redis | [wodby/service-redis](https://github.com/wodby/service-redis) |
| Memcached | [wodby/service-memcached](https://github.com/wodby/service-memcached) |
| RabbitMQ | [wodby/service-rabbitmq](https://github.com/wodby/service-rabbitmq) |
| Distribution Registry | [wodby/service-distribution](https://github.com/wodby/service-distribution) |
| Solr | [wodby/service-solr](https://github.com/wodby/service-solr) |
| Elasticsearch | [wodby/service-elasticsearch](https://github.com/wodby/service-elasticsearch) |
| OpenSearch | [wodby/service-opensearch](https://github.com/wodby/service-opensearch) |
| Meilisearch | [wodby/service-meilisearch](https://github.com/wodby/service-meilisearch) |
| ClickHouse | [wodby/service-clickhouse](https://github.com/wodby/service-clickhouse) |
| Apache Kafka | [wodby/service-kafka](https://github.com/wodby/service-kafka) |
| Apache Spark | [wodby/service-spark](https://github.com/wodby/service-spark) |
| Apache Airflow | [wodby/service-airflow](https://github.com/wodby/service-airflow) |
| ZooKeeper | [wodby/service-zookeeper](https://github.com/wodby/service-zookeeper) |
| Gotenberg | [wodby/service-gotenberg](https://github.com/wodby/service-gotenberg) |

### Identity and access

| Service | Repository |
| --- | --- |
| Keycloak | [wodby/service-keycloak](https://github.com/wodby/service-keycloak) |

### Database administration

| Service | Repository |
| --- | --- |
| Adminer | [wodby/service-adminer](https://github.com/wodby/service-adminer) |
| Mongoku | [wodby/service-mongoku](https://github.com/wodby/service-mongoku) |
| phpMyAdmin | [wodby/service-phpmyadmin](https://github.com/wodby/service-phpmyadmin) |
| pgAdmin | [wodby/service-pgadmin](https://github.com/wodby/service-pgadmin) |

### Observability apps

| Service | Repository |
| --- | --- |
| Prometheus | [wodby/service-prometheus](https://github.com/wodby/service-prometheus) |
| Grafana | [wodby/service-grafana](https://github.com/wodby/service-grafana) |
| Kibana | [wodby/service-kibana](https://github.com/wodby/service-kibana) |
| OpenSearch Dashboards | [wodby/service-opensearch-dashboards](https://github.com/wodby/service-opensearch-dashboards) |
| Grafana Loki | [wodby/service-loki](https://github.com/wodby/service-loki) |
| VictoriaMetrics | [wodby/service-victoria-metrics](https://github.com/wodby/service-victoria-metrics) |
| Grafana Tempo | [wodby/service-tempo](https://github.com/wodby/service-tempo) |
| OpenTelemetry Collector | [wodby/service-opentelemetry-collector](https://github.com/wodby/service-opentelemetry-collector) |

### Mail

| Service | Repository |
| --- | --- |
| Mailpit | [wodby/service-mailpit](https://github.com/wodby/service-mailpit) |
| OpenSMTPD | [wodby/service-opensmtpd](https://github.com/wodby/service-opensmtpd) |

### Kubernetes system services

These services back Wodby-managed Kubernetes infrastructure. They are installed
through system stacks during cluster provisioning and are not intended for
user-created application stacks.

| Service | Repository |
| --- | --- |
| Monitoring | [wodby/service-monitoring](https://github.com/wodby/service-monitoring) |
| Metrics Server | [wodby/service-metrics-server](https://github.com/wodby/service-metrics-server) |
| Node Exporter | [wodby/service-node-exporter](https://github.com/wodby/service-node-exporter) |
| Kube State Metrics | [wodby/service-kube-state-metrics](https://github.com/wodby/service-kube-state-metrics) |
| AWS LB Controller | [wodby/service-aws-lb-controller](https://github.com/wodby/service-aws-lb-controller) |
| Envoy Gateway | [wodby/service-envoy-gateway](https://github.com/wodby/service-envoy-gateway) |
| Ingress Nginx | [wodby/service-ingress-nginx](https://github.com/wodby/service-ingress-nginx) |
| FRPC | [wodby/service-frpc](https://github.com/wodby/service-frpc) |

### Storage

| Service | Repository |
| --- | --- |
| NFS Provisioner | [wodby/service-nfs-provisioner](https://github.com/wodby/service-nfs-provisioner) |

### AI

| Service | Repository |
| --- | --- |
| Open WebUI | [wodby/service-open-webui](https://github.com/wodby/service-open-webui) |
| OpenClaw | [wodby/service-openclaw](https://github.com/wodby/service-openclaw) |
| T3 Code | [wodby/service-t3code](https://github.com/wodby/service-t3code) |
| vLLM (preview) | [wodby/service-vllm](https://github.com/wodby/service-vllm) |
