# Wodby services

This repository is the index of Wodby-managed service repositories for Wodby
2.0.

Services are reusable definitions for application runtimes, databases, search
engines, infrastructure components, and integrations. A service becomes useful
inside a [stack](https://github.com/wodby/stacks), where it is configured as a
stack service and later deployed as an app service.

- Service catalog: https://wodby.com/services
- Service docs: https://wodby.com/docs/2.0/services/
- Service template reference: https://wodby.com/docs/2.0/services/template/
- Service boilerplate: https://github.com/wodby/service

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
| Matomo PHP | [wodby/service-matomo](https://github.com/wodby/service-matomo) |
| Python | [wodby/service-python](https://github.com/wodby/service-python) |
| Django | [wodby/service-django](https://github.com/wodby/service-django) |
| FastAPI | [wodby/service-fastapi](https://github.com/wodby/service-fastapi) |
| Flask | [wodby/service-flask](https://github.com/wodby/service-flask) |
| Ruby | [wodby/service-ruby](https://github.com/wodby/service-ruby) |
| Rails | [wodby/service-rails](https://github.com/wodby/service-rails) |
| Go | [wodby/service-go](https://github.com/wodby/service-go) |
| Node.js | [wodby/service-node](https://github.com/wodby/service-node) |
| Next.js | [wodby/service-nextjs](https://github.com/wodby/service-nextjs) |
| Dagster | [wodby/service-dagster](https://github.com/wodby/service-dagster) |

### Web and edge

| Service | Repository |
| --- | --- |
| Nginx | [wodby/service-nginx](https://github.com/wodby/service-nginx) |
| Nginx for PHP | [wodby/service-php-nginx](https://github.com/wodby/service-php-nginx) |
| Nginx for Drupal | [wodby/service-drupal-nginx](https://github.com/wodby/service-drupal-nginx) |
| Nginx for WordPress | [wodby/service-wordpress-nginx](https://github.com/wodby/service-wordpress-nginx) |
| Nginx for Laravel | [wodby/service-laravel-nginx](https://github.com/wodby/service-laravel-nginx) |
| Nginx for Matomo | [wodby/service-matomo-nginx](https://github.com/wodby/service-matomo-nginx) |
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
| 3X UI | [wodby/service-3xui](https://github.com/wodby/service-3xui) |

### Data, search, and messaging

| Service | Repository |
| --- | --- |
| MariaDB | [wodby/service-mariadb](https://github.com/wodby/service-mariadb) |
| PostgreSQL | [wodby/service-postgres](https://github.com/wodby/service-postgres) |
| PostGIS | [wodby/service-postgis](https://github.com/wodby/service-postgis) |
| Cloud MySQL | [wodby/service-cloud-mysql](https://github.com/wodby/service-cloud-mysql) |
| Cloud MariaDB | [wodby/service-cloud-mariadb](https://github.com/wodby/service-cloud-mariadb) |
| Cloud PostgreSQL | [wodby/service-cloud-postgres](https://github.com/wodby/service-cloud-postgres) |
| Valkey | [wodby/service-valkey](https://github.com/wodby/service-valkey) |
| Redis | [wodby/service-redis](https://github.com/wodby/service-redis) |
| Solr | [wodby/service-solr](https://github.com/wodby/service-solr) |
| ZooKeeper | [wodby/service-zookeeper](https://github.com/wodby/service-zookeeper) |
| Gotenberg | [wodby/service-gotenberg](https://github.com/wodby/service-gotenberg) |

### Mail

| Service | Repository |
| --- | --- |
| Mailpit | [wodby/service-mailpit](https://github.com/wodby/service-mailpit) |
| OpenSMTPD | [wodby/service-opensmtpd](https://github.com/wodby/service-opensmtpd) |

### Kubernetes and platform

| Service | Repository |
| --- | --- |
| Monitoring | [wodby/service-monitoring](https://github.com/wodby/service-monitoring) |
| Metrics Server | [wodby/service-metrics-server](https://github.com/wodby/service-metrics-server) |
| Node Exporter | [wodby/service-node-exporter](https://github.com/wodby/service-node-exporter) |
| Kube State Metrics | [wodby/service-kube-state-metrics](https://github.com/wodby/service-kube-state-metrics) |
| AWS LB Controller | [wodby/service-aws-lb-controller](https://github.com/wodby/service-aws-lb-controller) |
| Envoy Gateway | [wodby/service-envoy-gateway](https://github.com/wodby/service-envoy-gateway) |
| FRPC | [wodby/service-frpc](https://github.com/wodby/service-frpc) |

### Storage

| Service | Repository |
| --- | --- |
| NFS Provisioner | [wodby/service-nfs-provisioner](https://github.com/wodby/service-nfs-provisioner) |

### AI

| Service | Repository |
| --- | --- |
| OpenClaw | [wodby/service-openclaw](https://github.com/wodby/service-openclaw) |
